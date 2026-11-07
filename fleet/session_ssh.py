"""Password-authenticated SSH sessions held only in process memory.

No key discovery, ssh-agent, personal SSH config, saved passwords or remote
authorized_keys changes. Host identity is pinned before password authentication.
"""
import base64
import hashlib
import json
import logging
from pathlib import Path
import re
import socket
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent.parent


def quiet_transport(client):
    # Expected reconnect failures are represented in connection status. Paramiko's
    # transport traceback per retry would otherwise flood a long-running centre.
    logger = logging.getLogger('benchweb.ssh')
    if not any(isinstance(handler, logging.NullHandler) for handler in logger.handlers):
        logger.addHandler(logging.NullHandler())
    logger.propagate = False
    client.set_log_channel('benchweb.ssh')


class RemoteCommandError(ValueError):
    """The authenticated remote command exited with a known nonzero status."""


def paramiko_module():
    dependency = str(ROOT / '.fleet-deps')
    if dependency not in sys.path:
        sys.path.insert(0, dependency)
    try:
        import paramiko
        return paramiko
    except ImportError:
        raise ValueError('网页 SSH 组件未安装，请运行 python -m fleet.setup_admin')


def fingerprint(key):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')


def validate_endpoint(body):
    host = str(body.get('host', '')).strip()
    port = body.get('ssh_port', 22)
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*', host) or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('服务器地址或 SSH 端口格式无效')
    return host, port


def probe(host, port):
    module = paramiko_module()
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            transport = module.Transport(sock)
            try:
                transport.start_client(timeout=10)
                key = transport.get_remote_server_key()
                return {'fingerprint': fingerprint(key), 'key_type': key.get_name(), 'public_key': key.get_base64()}
            finally:
                transport.close()
    except socket.gaierror:
        raise ValueError('无法解析服务器主机名，请核对地址或使用服务器 IP；尚未进行密码认证') from None
    except TimeoutError:
        raise ValueError('检查指纹超时：请确认中心电脑可访问服务器 SSH 端口，并检查 VPN、路由及防火墙；尚未进行密码认证') from None
    except ConnectionRefusedError:
        raise ValueError('服务器拒绝 SSH 连接：请核对端口及 SSH 服务是否启动；尚未进行密码认证') from None
    except module.SSHException:
        raise ValueError('SSH 握手失败：请确认填写的是 SSH 端口，并检查服务器 SSH 日志和算法兼容性；尚未进行密码认证') from None
    except OSError:
        raise ValueError('无法连接服务器 SSH 端口：请检查中心电脑到服务器的网络及访问限制；尚未进行密码认证') from None


class SessionSSH:
    def __init__(self, host, port, user, password, expected_fingerprint):
        module = paramiko_module()
        class PinnedKey(module.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                if fingerprint(key) != expected_fingerprint:
                    raise ValueError('服务器 SSH 指纹变化，拒绝发送密码')
        self.client = module.SSHClient()
        quiet_transport(self.client)
        self.client.set_missing_host_key_policy(PinnedKey())
        # Do not call load_system_host_keys/load_host_keys: those could read personal files.
        try:
            self.client.connect(host, port=port, username=user, password=password, timeout=12,
                                banner_timeout=12, auth_timeout=15, look_for_keys=False, allow_agent=False)
        except module.AuthenticationException:
            self.client.close()
            raise ValueError('SSH 认证失败：服务器拒绝了用户名或密码，请核对后重试；密码未保存') from None
        except (OSError, module.SSHException):
            self.client.close()
            raise ValueError('SSH 连接未建立：请检查服务器地址、端口和网络；密码未保存') from None
        except Exception:
            self.client.close()
            raise
        handler = self.client.get_transport().auth_handler
        if handler and hasattr(handler, 'password'):
            handler.password = None
        self.client.get_transport().set_keepalive(30)
        self.lock = threading.Lock()

    def execute(self, script, timeout=60, log=None):
        with self.lock:
            transport = self.client.get_transport()
            if not transport or not transport.is_active():
                raise ValueError('SSH 会话已断开，请重新连接服务器；密码不会保存')
            channel = transport.open_session(timeout=10)
            channel.settimeout(10)
            channel.set_combine_stderr(True)
            channel.exec_command('python3 -u -')
            channel.sendall(script.encode('utf-8'))
            channel.shutdown_write()
            chunks, line_buffer = [], ''
            deadline = time.monotonic() + timeout
            try:
                while True:
                    if time.monotonic() > deadline:
                        raise ValueError('远端操作超时，结果未知；请核对后重试')
                    if channel.recv_ready():
                        text = channel.recv(32768).decode('utf-8', errors='replace')
                        chunks.append(text)
                        if log:
                            line_buffer += text
                            while '\n' in line_buffer:
                                line, line_buffer = line_buffer.split('\n', 1)
                                log(line)
                    elif channel.exit_status_ready():
                        break
                    else:
                        time.sleep(.05)
                if log and line_buffer:
                    log(line_buffer)
                output = ''.join(chunks)
                if channel.recv_exit_status() != 0:
                    raise RemoteCommandError(output[-2000:] or '远端操作失败')
                return output
            finally:
                channel.close()

    def upload(self, source, directory, log=None):
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        path = directory + '/ascend-bench-kit-' + digest[:12] + '.tar.gz'
        def remote_digest(filename):
            code = '''import hashlib,json,os,stat
from pathlib import Path
p=Path(FILENAME)
for q in [*reversed(p.parents),p]:
 if q.is_symlink():raise ValueError('安装包路径包含符号链接')
if p.is_file():
 h=hashlib.sha256()
 with p.open('rb') as f:
  for block in iter(lambda:f.read(1048576),b''):h.update(block)
 print(json.dumps(h.hexdigest()))
else:print('null')
'''.replace('FILENAME', repr(filename))
            return json.loads(self.execute(code, timeout=60))
        if log:
            log('检查共享路径缓存；目标安装包 SHA256：' + digest)
        if remote_digest(path) == digest:
            if log:
                log('共享路径已有当前版本且 SHA256 一致，直接复用；本次上传 0 B')
            return path
        if log:
            log(f'缓存缺失或校验不符，自动从本机同步安装包（{source.stat().st_size:,} B）')
        with self.lock, self.client.open_sftp() as sftp:
            parts = directory.split('/')
            current = ''
            for part in parts:
                if not part:
                    continue
                current += '/' + part
                try:
                    attrs = sftp.lstat(current)
                    import stat
                    if not stat.S_ISDIR(attrs.st_mode) or stat.S_ISLNK(attrs.st_mode):
                        raise ValueError('工具包目录包含符号链接或非目录')
                except FileNotFoundError:
                    sftp.mkdir(current, mode=0o755)
            # Write temp + rename so two nodes sharing the directory never read partial data.
            temporary = path + '.' + str(time.time_ns()) + '.upload'
            last = [0.0]
            started = time.monotonic()
            def progress(sent, total):
                now = time.monotonic()
                if log and (sent == total or now - last[0] >= 2):
                    last[0] = now
                    log(f'上传进度 {sent * 100 // max(total, 1)}% · {sent:,}/{total:,} B · {sent / max(now-started, .01) / 1024:.0f} KiB/s')
            sftp.put(str(source), temporary, callback=progress)
            sftp.chmod(temporary, 0o644)
            try:
                sftp.posix_rename(temporary, path)
            except IOError:
                # Some SFTP servers do not support the OpenSSH rename extension.
                try:
                    sftp.stat(path)
                    sftp.remove(temporary)
                except FileNotFoundError:
                    sftp.rename(temporary, path)
        if remote_digest(path) != digest:
            raise ValueError('安装包同步后 SHA256 校验失败，停止部署')
        if log:
            log('安装包已自动同步，远端 SHA256 校验通过')
        return path

    def close(self):
        self.client.close()


INSPECT_SCRIPT = '''import json,subprocess
from pathlib import Path
def run(argv):return subprocess.check_output(argv,stderr=subprocess.STDOUT).decode()
ids=run(['docker','ps','-aq']).split()
containers=[]
if ids:
 for c in json.loads(run(['docker','inspect',*ids])):
  containers.append({'name':c['Name'].lstrip('/'),'image':c['Config']['Image'],'state':c['State']['Status'],'managed':(c['Config'].get('Labels') or {}).get('ascend-bench.managed')=='true','network':c['HostConfig']['NetworkMode'],'mounts':[{'source':m['Source'],'destination':m['Destination'],'rw':m.get('RW',False)} for m in c['Mounts']]})
from concurrent.futures import ThreadPoolExecutor
probe_code="""import hashlib,json,os,sys
from pathlib import Path
base=Path(sys.argv[1]);container=sys.argv[2];source=base/'scripts/runner.py';running=False
try:
 pid=int((base/'runner.pid').read_text());proc=Path('/proc')/str(pid)
 env=dict(e.split(b'=',1) for e in (proc/'environ').read_bytes().split(bytes([0])) if b'=' in e)
 port=int(env.get(b'ASCEND_BENCH_PORT',b'18787'))
 inodes={line.split()[9] for line in (proc/'net/tcp').read_text().splitlines()[1:] if line.split()[1]=='0100007F:'+format(port,'04X') and line.split()[3]=='0A'}
 running=env.get(b'ASCEND_BENCH_CONTAINER',b'').decode()==container and (proc/'cwd').resolve()==base and any(a.endswith(b'runner.py') for a in (proc/'cmdline').read_bytes().split(bytes([0]))) and bool(inodes) and any(os.readlink(fd) in {'socket:['+i+']' for i in inodes} for fd in (proc/'fd').iterdir())
except (OSError,ValueError):pass
print(json.dumps({'runner_installed':source.is_file(),'runner_files_visible':source.is_file(),'runner_running':running}))
"""
def check_runner(c):
 if c['state']!='running':return c
 base=globals().get('RUNNER_BASES',{}).get(c['name'],'/home/ascend_bench/runner')
 try:
  raw=subprocess.check_output(['docker','exec','-e','PYTHONDONTWRITEBYTECODE=1',c['name'],'python3','-I','-S','-c',probe_code,base,c['name']],stderr=subprocess.DEVNULL,timeout=8)
  c.update(json.loads(raw));c['runner_checked']=True
 except Exception:c['runner_checked']=False
 return c
with ThreadPoolExecutor(max_workers=4) as pool:containers=list(pool.map(check_runner,containers))
images=[]
for row in run(['docker','image','ls','--format','{{json .}}']).splitlines():
 x=json.loads(row)
 if x['Repository']!='<none>' and x['Tag']!='<none>':images.append(x['Repository']+':'+x['Tag'])
ports=set()
for name in ('tcp','tcp6'):
 try:lines=(Path('/proc/net')/name).read_text().splitlines()[1:]
 except FileNotFoundError:continue
 for line in lines:
  fields=line.split()
  if len(fields)>=4 and fields[3]=='0A':ports.add(int(fields[1].rsplit(':',1)[1],16))
print(json.dumps({'containers':containers,'images':images,'listening_ports':sorted(ports)}))
'''
