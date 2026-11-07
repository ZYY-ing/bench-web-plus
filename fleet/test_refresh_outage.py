import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock

from .app import Console
from .persistent import Channels, identity


class RefreshOutageTests(unittest.TestCase):
    def test_offline_collection_does_not_lock_online_collection_or_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Console(Path(directory)/'missing.json', Path(directory)/'data', demo=True)
            old = app.store.list('runner')[0]
            online = {**old, 'id': 'online', 'spec': {**old['spec'], 'container': 'online'}}
            app.store.save('runner', online)
            app.demo = False
            entered, release = threading.Event(), threading.Event()
            calls = []
            def status(spec):
                calls.append(spec['container'])
                if spec['container'] != 'online':
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError('test failed to release collection')
                    raise TimeoutError('machine powered off')
                return {'token': '', 'status': {'activity': 'idle'}}
            app.transport.status = status
            try:
                with ThreadPoolExecutor(max_workers=3) as pool:
                    offline = pool.submit(app.refresh, old['id'])
                    self.assertTrue(entered.wait(1))
                    fast = pool.submit(app.refresh, online['id'])
                    self.assertFalse(fast.result(timeout=1)['error'])
                    self.assertEqual(len(app.user_view()['runners']), 2)
                    release.set()
                    self.assertTrue(offline.result(timeout=1)['error'])
                    self.assertTrue(app.refresh(old['id'])['refresh_info']['cached'])
                    self.assertEqual(calls.count(old['spec']['container']), 1)
            finally:
                release.set()
                app.executor.shutdown(wait=True)
                app.store.db.close()

    def test_health_timeout_is_short_without_changing_job_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            channels = Channels(directory)
            spec = dict(profile_id='test', container='own', base='/home/runner', port=18787)
            channels.entries[identity(spec)] = dict(ca='test', url='https://example.invalid', secret='test')
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value.read.return_value = json.dumps({'status': 200}).encode()
            with patch('fleet.persistent.ssl.create_default_context'), patch('fleet.persistent.urllib.request.build_opener', return_value=opener):
                for path, timeout in [('/api/status', 5), ('/api/jobs/test', 320), ('/api/info', 30)]:
                    channels.request(spec, 'GET', path, None)
                    self.assertEqual(opener.open.call_args.kwargs['timeout'], timeout)
