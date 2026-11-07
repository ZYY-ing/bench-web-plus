"""Boot-time recovery for explicitly registered Runner processes and gateways.

Never stops processes or containers. Stopped external containers are left to
their owner. Only new tool-owned containers receive a Docker restart policy.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request


def run(args, timeout=20):
    return subprocess.check_output(args, stderr=subprocess.PIPE, timeout=timeout).decode().strip()


def listening(host, port):
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex((host, port)) == 0


def recover(row):
    if not row.get('active'):
        return 'retired'
    spec = row['spec']
    info = json.loads(run(['docker', 'inspect', spec['container']]))[0]
    if info['Id'] != row['container_id']:
        return 'container_identity_changed'
    if not info['State']['Running'] or info['State'].get('Paused'):
        return 'waiting_for_container_owner'
    if info['HostConfig']['NetworkMode'] != 'host':
        return 'container_network_changed'
    # Same nonblocking lease as deployment; recovery must not race an upgrade.
    key = hashlib.sha256(json.dumps([spec['profile_id'], spec['port']]).encode()).hexdigest()
    fd = os.open('/tmp/bench-web-deploy-' + key + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'deployment_in_progress'
        check = r'''import hashlib,json,os,sys
from pathlib import Path
base=Path(sys.argv[1]);expected=json.loads(sys.argv[2]);container=sys.argv[3]
assert base.resolve()==base
assert all(hashlib.sha256((base/name).read_bytes()).hexdigest()==digest for name,digest in expected.items()), 'Runner files changed'
config=json.loads((base/'runner-config.json').read_text())
assert config['container']==container, 'Runner config identity changed'
print('verified')
'''
        run(['docker', 'exec', '-w', '/', spec['container'], 'python3', '-I', '-S', '-c', check,
             spec['base'], json.dumps(row['runner_hashes']), spec['container']])
        if not listening('127.0.0.1', spec['port']):
            run(['docker', 'exec', '-d', '-w', '/', '-e', 'ASCEND_BENCH_MANAGED=1',
                 '-e', 'ASCEND_BENCH_HOME=' + spec['base'], '-e', 'ASCEND_BENCH_CONTAINER=' + spec['container'],
                 '-e', 'ASCEND_BENCH_PORT=' + str(spec['port']), spec['container'], 'bash', spec['base'] + '/scripts/start-runner.sh'])
            return 'starting_runner'
        token = (Path(row['host_base']) / 'runner-token').read_text().strip()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request('http://127.0.0.1:' + str(spec['port']) + '/api/status',
            headers={'Cookie':'runner_session_' + str(spec['port']) + '=' + token})
        with opener.open(request, timeout=3) as response:
            status = json.load(response)
        if status.get('root') != spec['base'] or status.get('container') != spec['container']:
            return 'runner_port_conflict'
        directory = Path(row['gateway_dir'])
        if directory.resolve() != directory:
            return 'gateway_directory_changed'
        for name, digest in row['gateway_hashes'].items():
            path = directory / name
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                return 'gateway_files_changed'
        if not listening(row['bind'], row['access_port']):
            # Do not launch a second owned gateway if its process is starting.
            pidfile = directory / 'gateway.pid'
            if pidfile.is_file():
                try:
                    proc = Path('/proc') / str(int(pidfile.read_text()))
                    if (proc.exists() and (proc / 'stat').read_text().split()[2] != 'Z'
                            and (proc / 'cwd').resolve() == directory
                            and str(directory / 'persistent_gateway.py').encode() in (proc / 'cmdline').read_bytes().split(bytes([0]))):
                        return 'gateway_process_waiting'
                except (OSError, ValueError):
                    pass
            with (directory / 'gateway.log').open('ab') as output:
                subprocess.Popen([sys.executable, '-u', str(directory / 'persistent_gateway.py')], cwd=directory,
                                 stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True,
                                 env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            return 'starting_gateway'
        # A bound port alone is not proof of gateway ownership.
        pid = int((directory / 'gateway.pid').read_text())
        proc = Path('/proc') / str(pid)
        if pid <= 1 or (proc / 'cwd').resolve() != directory or str(directory / 'persistent_gateway.py').encode() not in (proc / 'cmdline').read_bytes().split(bytes([0])):
            return 'gateway_port_conflict'
        address = socket.inet_aton(socket.gethostbyname(row['bind']))[::-1].hex().upper() + ':' + format(row['access_port'], '04X')
        inodes = {line.split()[9] for line in (proc / 'net/tcp').read_text().splitlines()[1:] if line.split()[1] == address and line.split()[3] == '0A'}
        if not inodes or not any(os.readlink(fd) in {'socket:[' + value + ']' for value in inodes} for fd in (proc / 'fd').iterdir()):
            return 'gateway_port_conflict'
    return 'available'


def main(root=None, once=False):
    root = Path(root or Path(__file__).resolve().parent)
    previous = {}
    while True:
        directory = root / 'runtimes'
        directory.mkdir(mode=0o700, exist_ok=True)
        for path in sorted(directory.glob('*.json')):
            try:
                if path.is_symlink():
                    continue
                row = json.loads(path.read_text())
                state = recover(row)
            except Exception as exc:
                state = 'waiting:' + type(exc).__name__
            if previous.get(path.name) != state:
                print(json.dumps({'runtime': path.stem, 'state': state}), flush=True)
                previous[path.name] = state
        if once:
            return
        time.sleep(5)


if __name__ == '__main__':
    main(once='--once' in sys.argv)
