"""Durable access uses only a tool-owned TLS identity, never SSH passwords."""
import hashlib
import base64
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
        with opener.open(request, timeout=5 if path == '/api/status' else (320 if path.startswith('/api/jobs') else 30)) as response:
            raw = response.read(5500001)
            if len(raw) > 5500000:
                raise ValueError('Runner 响应过大')
            return json.loads(raw)

    def install(self, session, spec, profile, log):
        key = identity(spec)
        sources = {name:(HERE/name).read_text(encoding='utf-8') for name in ('gateway.py','persistent_gateway.py','http_server.py')}
        port = spec.get('access_port', spec['port'] + 1000)
        if not 1 <= port <= 65535 or port == spec['port']:
            raise ValueError('持久连接端口无效')
        url = 'https://' + profile['host'] + ':' + str(port)
        with self.lock:
            candidates = {k: dict(e) for k,e in self.entries.items() if e['profile_id'] == profile['id'] and e['url'] == url}
        old = candidates.get(key)
        if key in self.entries and not old:
            raise ValueError('已有持久连接的端口不同，请先撤销旧连接后再更改')
        can_reuse = False
        if old:
            try:
                can_reuse = self.request(spec, 'GET', '/api/status', None).get('status') == 200
            except Exception:
                pass
        helper = (HERE/'remote_helper.py').read_text(encoding='utf-8')
        script = helper + '\nspec=json.loads(' + repr(json.dumps(spec)) + ')\nstatus=read_status(spec)["status"]\nverify_runner_owner(spec)\nprint(json.dumps(status))\n'
        try:
            status = json.loads(session.execute(script, timeout=45))
        except Exception:
            raise ValueError('无法核实所选容器的 Runner 身份及端口归属；未迁移连接') from None
        material = issue(profile['host'], self.directory / 'runner-ca')
        secret = old['secret'] if old else secrets.token_urlsafe(32)
        remote = {**spec, 'service_hosts': profile.get('service_hosts', [profile['host'], '127.0.0.1', 'localhost']),
                  'service_ports': profile.get('service_ports'), 'tokenizer_roots': profile.get('tokenizer_roots', ['/models', '/mnt', '/home'])}
        access = {'bind': profile['host'], 'port': port, 'secret': secret, 'spec': remote}
        files = {**sources, 'access.json': json.dumps(access), 'cert.pem': material['cert'], 'key.pem': material['key']}
        # Docker access is needed only by the short-lived deployment SSH helper.
        # The gateway runs on the host, scoped by its API allowlist; it has no
        # endpoint for Docker commands, installation or arbitrary filesystem reads.
        payload = {'container': spec['container'], 'target': spec['base'], 'files': files, 'port': port, 'host': profile['host'], 'access_id': key[:24],
                   'candidates': {k: {'secret':e['secret']} for k,e in candidates.items()},
                   'can_reuse': can_reuse,
                   'restart': status.get('activity') == 'idle' and status.get('process_tree_confirmed_stopped') is True}
        script = helper + '\n' + INSTALL + '\nwith remote_deployment_lock(json.loads(' + repr(json.dumps(spec)) + ')):\n print(json.dumps(install(json.loads(' + repr(json.dumps(payload)) + '))))\n'
        log('配置项目内的 TLS 受限评测连接：不保存 SSH 密码，不修改 SSH 授权')
        entry = {'profile_id': profile['id'], 'url': url, 'secret': secret, 'ca': material['ca']}
        with self.lock:
            if key not in self.entries:
                self.entries[key] = entry
                self.save()  # Keep new identity recoverable without replacing a working one.
        try:
            receipt = json.loads(session.execute(script, timeout=60))
        except Exception:
            raise ValueError('持久连接安装未确认成功，请核对项目目录和接入端口；未修改 SSH 授权') from None
        chosen = candidates.get(receipt.get('credential_key'))
        if chosen:
            entry['secret'] = chosen['secret']
            if receipt.get('reused'):
                entry['ca'] = chosen['ca']
        entry['access_id'] = receipt['access_id']
        entry['spec'] = {k:spec[k] for k in ('profile_id','container','base','port')}
        with self.lock:
            self.entries[key] = entry
            self.save()
        result = self.request(spec, 'GET', '/api/status', None)
        actual = json.loads(base64.b64decode(result['body'])) if result.get('status') == 200 else {}
        if actual.get('root') != spec['base'] or actual.get('container') != spec['container']:
            raise ValueError('持久评测连接健康检查失败')
        from .recovery import register
        if register(session, spec, entry['access_id']):
            log('已登记服务器开机恢复：仅恢复已核验的 Runner 与 TLS 连接进程')
        with self.lock:
            for previous_key in candidates:
                if previous_key != key:
                    self.entries.pop(previous_key, None)
            self.save()
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
 expected=json.loads(payload['files']['access.json'])
 known=[]
 for child in access_root.iterdir():
  if child.is_symlink() or not child.is_dir() or (child/'access.json').is_symlink():continue
  try:current=json.loads((child/'access.json').read_text())
  except (OSError,ValueError):continue
  credential=next((key for key,value in payload['candidates'].items() if value['secret']==current.get('secret')),None)
  original=current.get('spec',{})
  if credential and current.get('bind')==payload['host'] and current.get('port')==payload['port'] and original.get('base')==payload['target'] and original.get('port')==expected['spec']['port'] and original.get('profile_id')==expected['spec']['profile_id']:
   known.append((child,current,credential))
 def owned_pid(destination):
  pidfile=destination/'gateway.pid'
  if not pidfile.is_file():return None
  pid=int(pidfile.read_text());proc=Path('/proc')/str(pid)
  if not proc.exists() or (proc/'stat').read_text().split()[2]=='Z':return None
  assert (proc/'cwd').resolve()==destination
  assert str(destination/'persistent_gateway.py').encode() in (proc/'cmdline').read_bytes().split(bytes([0]))
  addr=socket.inet_aton(payload['host'])[::-1].hex().upper()+':'+format(payload['port'],'04X')
  inodes={line.split()[9] for line in (proc/'net/tcp').read_text().splitlines()[1:] if line.split()[1]==addr and line.split()[3]=='0A'}
  assert inodes and any(os.readlink(p) in {'socket:['+i+']' for i in inodes} for p in (proc/'fd').iterdir())
  return pid
 active=[item for item in known if owned_pid(item[0]) is not None]
 assert len(active)<=1, 'Multiple owned gateways on the selected port'
 if active:chosen=active[0]
 elif len(known)==1:chosen=known[0]
 else:
  assert not known, 'Ambiguous stopped gateway identities'
  chosen=None
 destination=chosen[0] if chosen else access_root/payload['access_id']
 credential_key=chosen[2] if chosen else None
 assert destination.resolve()==destination
 for name in list(payload['files'])+['gateway.pid']:
  assert not (destination/name).is_symlink(), 'Access files must not be symbolic links'
 if destination.exists():
  current=json.loads((destination/'access.json').read_text())
  assert chosen, 'Unrecognized existing identity'
  expected['secret']=current['secret']
  pid=owned_pid(destination)
  same_spec=all(current['spec'].get(k)==expected['spec'].get(k) for k in ('profile_id','container','base','port','tokenizer_roots'))
  same_code=all((destination/name).is_file() and (destination/name).read_text()==payload['files'][name] for name in ('gateway.py','persistent_gateway.py','http_server.py'))
  if pid and same_spec and same_code and payload.get('can_reuse'):
   return {'access_id':destination.name,'credential_key':credential_key,'reused':True}
  assert payload['restart'], 'Runner is not idle; refuse gateway migration'
  if pid:
   proc=Path('/proc')/str(pid)
   os.kill(pid,signal.SIGTERM)
   for _ in range(50):
    if not proc.exists() or (proc/'stat').read_text().split()[2]=='Z':break
    time.sleep(.1)
   else:raise RuntimeError('Owned gateway did not stop; refuse overwrite')

 with socket.socket() as s:
  s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
  s.bind((payload['host'],payload['port']))
 destination.mkdir(mode=0o700,exist_ok=True)
 settings=expected;settings['spec']['token_file']=str(base/'runner-token')
 payload['files']['access.json']=json.dumps(settings)
 for name,raw in payload['files'].items():
  p=destination/name;p.write_text(raw,encoding='utf-8');p.chmod(0o600)
 with (destination/'gateway.log').open('ab') as out:
  process=subprocess.Popen(['python3','-u',str(destination/'persistent_gateway.py')],cwd=destination,stdin=subprocess.DEVNULL,stdout=out,stderr=out,start_new_session=True,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
 for _ in range(100):
  if process.poll() is not None:
   raise ValueError('TLS 连接进程启动退出；请查看 '+str(destination/'gateway.log'))
  if (destination/'gateway.pid').is_file():
   try:
    assert int((destination/'gateway.pid').read_text())==process.pid
    if owned_pid(destination)==process.pid:return {'access_id':destination.name,'credential_key':credential_key,'reused':False}
   except (OSError,ValueError,AssertionError):pass
  time.sleep(.1)
 raise ValueError('TLS 连接启动未在 10 秒内通过 PID 与监听端口归属检查：'+str(destination/'gateway.log'))
'''
