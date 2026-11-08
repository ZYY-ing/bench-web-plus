"""Tool-owned forced SSH command. Only fresh, centre-signed scripts may run.

Installed in a private server directory at explicit first authorization. Never
imports code from the Runner's writable container mount.
"""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

MAX_REQUEST = 32 * 1024 * 1024


def process(root, request, output=None):
    root = Path(root)
    if root.resolve() != root or root.stat().st_uid != os.geteuid():
        raise ValueError('Management directory identity mismatch')
    if (root / 'revoked').exists():
        raise ValueError('Management authorization revoked')
    if request.get('action') == 'challenge':
        nonce = secrets.token_hex(32)
        path = root / 'challenges' / nonce
        path.parent.mkdir(mode=0o700, exist_ok=True)
        # Issuance/cleanup and consumption share a lock. A consumer must not
        # remove a file between the cleanup's directory listing and stat.
        with (root / 'challenge.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for old in path.parent.iterdir():
                try:
                    if not old.is_symlink() and old.is_file() and old.stat().st_mtime < time.time() - 600:
                        old.unlink()
                except FileNotFoundError:
                    # Tolerate an older broker or external cleanup during upgrade.
                    continue
            with path.open('x') as stream:
                stream.write(str(time.time()))
            path.chmod(0o600)
        return {'nonce': nonce}
    if request.get('action') != 'execute':
        raise ValueError('Unsupported management request')
    nonce = request.get('nonce', '')
    if len(nonce) != 64 or any(c not in '0123456789abcdef' for c in nonce):
        raise ValueError('Invalid challenge')
    script = base64.b64decode(request['script'], validate=True)
    signature = base64.b64decode(request['signature'], validate=True)
    if not script or len(script) > 24 * 1024 * 1024 or len(signature) > 1024:
        raise ValueError('Invalid script size')
    with tempfile.TemporaryDirectory(prefix='verify-', dir=root) as temporary:
        data = Path(temporary) / 'data'
        sig = Path(temporary) / 'sig'
        data.write_bytes(nonce.encode() + bytes([0]) + script)
        sig.write_bytes(signature)
        verified = subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(root / 'signer.pem'),
                                   '-signature', str(sig), str(data)], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=10)
        if verified.returncode:
            raise ValueError('Management signature verification failed')
    path = root / 'challenges' / nonce
    # Atomic consumption prevents a signed operation being replayed concurrently.
    with (root / 'challenge.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if path.is_symlink() or not path.is_file() or time.time() - path.stat().st_mtime > 300:
                raise ValueError('Expired or consumed challenge')
            path.unlink()
        except FileNotFoundError:
            raise ValueError('Expired or consumed challenge') from None
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
    env.update(PYTHONDONTWRITEBYTECODE='1', BENCHWEB_MANAGEMENT_ROOT=str(root))
    process = subprocess.Popen([sys.executable, '-u', '-'], stdin=subprocess.PIPE, stdout=output,
                               stderr=subprocess.STDOUT, env=env)
    process.communicate(script)
    audit = root / 'audit.jsonl'
    with (root / 'audit.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if audit.exists() and audit.stat().st_size > 1024 * 1024:
            audit.replace(root / 'audit.previous.jsonl')
        with audit.open('a') as stream:
            stream.write(json.dumps({'time':time.time(),'script_sha256':hashlib.sha256(script).hexdigest(),'exit_code':process.returncode}) + '\n')
        audit.chmod(0o600)
    return process.returncode


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise ValueError('Request too large')
        result = process(Path(__file__).resolve().parent, json.loads(raw))
        if isinstance(result, dict):
            print(json.dumps(result), flush=True)
        else:
            raise SystemExit(result)
    except Exception as exc:
        print('Management request rejected: ' + str(exc), file=sys.stderr, flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
