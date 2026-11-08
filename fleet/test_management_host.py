"""Broker regressions; Linux tests use real flock, OpenSSL and child processes."""
import base64
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).with_name('management_host.py')


class ChallengeCleanupTests(unittest.TestCase):
    def test_disappearing_entry_is_ignored_under_shared_lock(self):
        state = {'locked': False, 'visited': False}
        def flock(*args):
            state['locked'] = True
        spec = importlib.util.spec_from_file_location('cleanup_broker', SOURCE)
        broker = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(LOCK_EX=1, flock=flock)}):
            spec.loader.exec_module(broker)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'challenges').mkdir()
            old = root / 'challenges' / ('a' * 64)
            old.touch()
            original_stat = Path.stat
            def disappearing_stat(path, *args, **kwargs):
                if path == old:
                    self.assertTrue(state['locked'])
                    # Disappear specifically after is_file(), before explicit stat().
                    if kwargs.get('follow_symlinks', True):
                        if state['visited']:
                            old.unlink(missing_ok=True)
                            raise FileNotFoundError(str(old))
                        state['visited'] = True
                return original_stat(path, *args, **kwargs)
            with patch.object(broker.os, 'geteuid', return_value=root.stat().st_uid, create=True), \
                    patch.object(Path, 'stat', disappearing_stat):
                nonce = broker.process(root, {'action': 'challenge'})['nonce']
            self.assertTrue(state['visited'])
            self.assertTrue((root / 'challenges' / nonce).is_file())


@unittest.skipUnless(os.name == 'posix' and shutil.which('openssl'), 'real Linux flock/OpenSSL integration')
class BrokerConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        shutil.copyfile(SOURCE, self.root / 'management_host.py')
        self.key = self.root / 'test-private.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048',
                        '-out', str(self.key)], check=True, capture_output=True)
        subprocess.run(['openssl', 'pkey', '-in', str(self.key), '-pubout',
                        '-out', str(self.root / 'signer.pem')], check=True, capture_output=True)

    def request(self, request):
        return subprocess.run([sys.executable, str(self.root / 'management_host.py')],
                              input=json.dumps(request), text=True, capture_output=True, timeout=30)

    def challenge(self):
        result = self.request({'action': 'challenge'})
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['nonce']

    def signed(self, nonce, script=None):
        if script is None:
            script = b"import os\nfrom pathlib import Path\np=Path(os.environ['BENCHWEB_MANAGEMENT_ROOT'])/'executed'\nwith p.open('a') as f: f.write('once\\n')\n"
        signature = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(self.key)],
                                   input=nonce.encode() + b'\0' + script, capture_output=True, check=True).stdout
        return {'action': 'execute', 'nonce': nonce, 'script': base64.b64encode(script).decode(),
                'signature': base64.b64encode(signature).decode()}

    def test_parallel_replay_executes_exactly_once(self):
        request = self.signed(self.challenge())
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(self.request, [request] * 32))
        self.assertEqual(sum(r.returncode == 0 for r in results), 1)
        self.assertTrue(all(r.returncode == 0 or 'Expired or consumed challenge' in r.stderr for r in results))
        self.assertEqual((self.root / 'executed').read_text(), 'once\n')
        self.assertEqual(len((self.root / 'audit.jsonl').read_text().splitlines()), 1)

    def test_parallel_cleanup_issuance_and_consumption(self):
        nonces = [self.challenge() for _ in range(12)]
        for index in range(80):
            path = self.root / 'challenges' / ('stale-' + str(index))
            path.touch()
            os.utime(path, (time.time() - 700,) * 2)
        requests = [self.signed(nonce) for nonce in nonces] + [{'action': 'challenge'}] * 32
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(self.request, requests))
        self.assertTrue(all(r.returncode == 0 for r in results), [r.stderr for r in results if r.returncode])
        self.assertEqual((self.root / 'executed').read_text().splitlines(), ['once'] * 12)
        self.assertEqual(len(list((self.root / 'challenges').iterdir())), 32)

    def test_running_script_does_not_hold_challenge_lock(self):
        script = b"import os,time\nfrom pathlib import Path\nr=Path(os.environ['BENCHWEB_MANAGEMENT_ROOT'])\n(r/'ready').touch()\ndeadline=time.monotonic()+10\nwhile not (r/'release').exists():\n if time.monotonic()>deadline: raise RuntimeError('release timeout')\n time.sleep(.01)\n"
        request = self.signed(self.challenge(), script)
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.request, request)
            try:
                deadline = time.monotonic() + 5
                while not (self.root / 'ready').exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((self.root / 'ready').exists())
                self.challenge()
                self.assertFalse(running.done())
            finally:
                (self.root / 'release').touch()
            result = running.result()
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_expired_missing_and_symlink_never_execute(self):
        nonce = self.challenge()
        request = self.signed(nonce)
        tampered = {**request, 'script': base64.b64encode(b"print('tampered')").decode()}
        self.assertIn('signature verification failed', self.request(tampered).stderr)
        path = self.root / 'challenges' / nonce
        os.utime(path, (time.time() - 301,) * 2)
        self.assertIn('Expired or consumed', self.request(request).stderr)
        path.unlink()
        self.assertIn('Expired or consumed', self.request(request).stderr)
        target = self.root / 'keep'
        target.touch()
        path.symlink_to(target)
        self.assertIn('Expired or consumed', self.request(request).stderr)
        self.assertTrue(target.exists())
        self.assertFalse((self.root / 'executed').exists())


if __name__ == '__main__':
    unittest.main()
