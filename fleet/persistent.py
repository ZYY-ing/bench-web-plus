"""Durable access uses only a tool-owned TLS identity, never SSH passwords."""
import hashlib
import json
from pathlib import Path
import secrets
import ssl
import threading
import urllib.request
from .certificates import issue


HERE = Path(__file__).parent


def identity(spec):
    return hashlib.sha256(json.dumps({k: spec[k] for k in ('profile_id', 'container', 'base', 'port')}, sort_keys=True).encode()).hexdigest()


class Channels:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'channels.json'
        self.lock = threading.RLock()
        self.entries = json.loads(self.path.read_text()) if self.path.exists() else {}

    def has(self, spec):
        return identity(spec) in self.entries

    def has_profile(self, profile):
        return any(e['profile_id'] == profile for e in self.entries.values())

    def save(self):
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.entries), encoding='utf-8')
            temporary.chmod(0o600)
            temporary.replace(self.path)

    def request(self, spec, method, path, body):
        entry = self.entries[identity(spec)]
        context = ssl.create_default_context(cadata=entry['ca'])
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), NoRedirect())
        data = json.dumps({'method': method, 'path': path, 'body': body}).encode()
        request = urllib.request.Request(entry['url'] + '/request', data=data, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + entry['secret']})
        with opener.open(request, timeout=320 if path.startswith('/api/jobs') else 30) as response:
            raw = response.read(5500001)
            if len(raw) > 5500000:
                raise ValueError('Runner 响应过大')
            return json.loads(raw)

    def install(self, session, spec, profile, log):
        key = identity(spec)
        old = self.entries.get(key)
        sources = {name:(HERE/name).read_text(encoding='utf-8') for name in ('gateway.py','persistent_gateway.py','http_server.py')}
        release = hashlib.sha256(json.dumps({name:hashlib.sha256(raw.encode()).hexdigest() for name,raw in sources.items()},sort_keys=True).encode()).hexdigest()
        port = spec.get('access_port', spec['port'] + 1000)
        if not 1 <= port <= 65535 or port == spec['port']:
            raise ValueError('持久连接端口无效')
        restart = False
        if old:
            if old['url'] != 'https://' + profile['host'] + ':' + str(port):
                raise ValueError('已有持久连接的端口不同，请先撤销旧连接后再更改')
            try:
                result = self.request(spec, 'GET', '/api/status', None)
            except Exception:
                result = None
            if result and result.get('status') == 200:
                if result.get('_gateway_release') == release:
                    log('已存在最新版受限持久连接，保持原连接身份')
                    return
                import base64
                status = json.loads(base64.b64decode(result['body']))
                if status.get('activity') != 'idle' or not status.get('process_tree_confirmed_stopped'):
                    raise ValueError('Runner 非空闲，不能更新连接组件')
                restart = True
                log('更新本项目的旧版连接组件；Runner 测试进程保持不动')
            else:
                log('原持久连接暂不可达；仅尝试恢复已停止的本项目连接，不覆盖未知进程')
                # An expired certificate or lost gateway must not require saving
                # an SSH password. An administrator can reconnect once; verify
                # the Runner over that pinned SSH session before any restart.
                helper = (HERE/'remote_helper.py').read_text(encoding='utf-8')
                script = helper + '\nprint(json.dumps(read_status(json.loads(' + repr(json.dumps(spec)) + '))["status"]))\n'
                try:
                    status = json.loads(session.execute(script, timeout=45))
                except Exception:
                    raise ValueError('无法核实原 Runner 状态；未重启连接进程，请先检查服务器') from None
                restart = status.get('activity') == 'idle' and status.get('process_tree_confirmed_stopped') is True
        material = issue(profile['host'], self.directory / 'runner-ca')
        secret = old['secret'] if old else secrets.token_urlsafe(32)
        remote = {**spec, 'service_hosts': profile.get('service_hosts', [profile['host'], '127.0.0.1', 'localhost']),
                  'service_ports': profile.get('service_ports'), 'tokenizer_roots': profile.get('tokenizer_roots', ['/models', '/mnt', '/home'])}
        access = {'bind': profile['host'], 'port': port, 'secret': secret, 'spec': remote}
        files = {**sources, 'access.json': json.dumps(access), 'cert.pem': material['cert'], 'key.pem': material['key']}
        # Docker access is needed only by the short-lived deployment SSH helper.
        # The gateway runs on the host, scoped by its API allowlist; it has no
        # endpoint for Docker commands, installation or arbitrary filesystem reads.
        payload = {'base': spec['host_path'] if spec.get('container_mode') == 'new' else '', 'container': spec['container'], 'target': spec['base'], 'files': files, 'port': port, 'host': profile['host'], 'access_id': key[:24], 'restart': restart}
        script = INSTALL + '\ninstall(json.loads(' + repr(json.dumps(payload)) + '))\n'
        log('配置项目内的 TLS 受限评测连接：不保存 SSH 密码，不修改 SSH 授权')
        entry = {'profile_id': profile['id'], 'url': 'https://' + profile['host'] + ':' + str(port), 'secret': secret, 'ca': material['ca']}
        with self.lock:
            self.entries[key] = entry
            self.save()  # Preserve recovery identity even if deployment disconnects.
        try:
            session.execute(script, timeout=60)
        except Exception:
            raise ValueError('持久连接安装未确认成功，请核对项目目录和接入端口；未修改 SSH 授权') from None
        result = self.request(spec, 'GET', '/api/status', None)
        if result.get('status') != 200:
            raise ValueError('持久评测连接健康检查失败')
        log('TLS 受限连接验证通过，中心重启后无需服务器密码即可刷新和测试')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


INSTALL = r'''
import json,subprocess,os,socket,time,signal
from pathlib import Path
def install(payload):
 info=json.loads(subprocess.check_output(['docker','inspect',payload['container']]))[0]
 matches=[m for m in info['Mounts'] if m.get('RW') and (payload['target']==m['Destination'] or payload['target'].startswith(m['Destination'].rstrip('/')+'/'))]
 assert matches, 'No persistent mount'
 mount=max(matches,key=lambda m:len(m['Destination']))
 base=Path(mount['Source'])/payload['target'][len(mount['Destination']):].lstrip('/')
 assert str(base).startswith(('/home/','/mnt/')) and base.resolve()==base
 access_root=base/'center-access'
 assert access_root.resolve()==access_root
 access_root.mkdir(mode=0o700,exist_ok=True)
 destination=access_root/payload['access_id']
 assert destination.resolve()==destination
 for name in list(payload['files'])+['gateway.pid']:
  assert not (destination/name).is_symlink(), 'Access files must not be symbolic links'
 if destination.exists():
  current=json.loads((destination/'access.json').read_text())
  expected=json.loads(payload['files']['access.json'])
  assert current['secret']==expected['secret'] and current['spec']['container']==payload['container'] and current['spec']['base']==payload['target'], 'Unrecognized existing identity'
  pidfile=destination/'gateway.pid'
  if pidfile.is_file():
   proc=Path('/proc')/pidfile.read_text().strip()
   if proc.exists() and b'persistent_gateway.py' in (proc/'cmdline').read_bytes():
    assert payload['restart'] and (proc/'cwd').resolve()==destination
    assert str(destination/'persistent_gateway.py').encode() in (proc/'cmdline').read_bytes().split(bytes([0]))
    addr=socket.inet_aton(payload['host'])[::-1].hex().upper()+':'+format(payload['port'],'04X')
    inodes={line.split()[9] for line in (proc/'net/tcp').read_text().splitlines()[1:] if line.split()[1]==addr and line.split()[3]=='0A'}
    assert inodes and any(os.readlink(p) in {'socket:['+i+']' for i in inodes} for p in (proc/'fd').iterdir())
    os.kill(int(proc.name),signal.SIGTERM)
    for _ in range(50):
     if not proc.exists() or (proc/'stat').read_text().split()[2]=='Z':break
     time.sleep(.1)
    else:raise RuntimeError('Owned gateway did not stop; refuse overwrite')

 with socket.socket() as s:
  s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
  s.bind((payload['host'],payload['port']))
 destination.mkdir(mode=0o700,exist_ok=True)
 settings=json.loads(payload['files']['access.json']);settings['spec']['token_file']=str(base/'runner-token')
 payload['files']['access.json']=json.dumps(settings)
 for name,raw in payload['files'].items():
  p=destination/name;p.write_text(raw,encoding='utf-8');p.chmod(0o600)
 with (destination/'gateway.log').open('ab') as out:
  subprocess.Popen(['python3','-u',str(destination/'persistent_gateway.py')],cwd=destination,stdin=subprocess.DEVNULL,stdout=out,stderr=out,start_new_session=True,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
 time.sleep(1)
 assert (destination/'gateway.pid').is_file(), 'Gateway startup failed'
'''
