"""Explicit tool SSH authorization; no saved server passwords or personal keys."""
import base64
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import threading
import time

from .session_ssh import SessionSSH, RemoteCommandError, fingerprint, paramiko_module, quiet_transport


class Vault:
    """Windows machine DPAPI plus an ACL limited to the owner and SYSTEM.

    Machine scope also permits the optional SYSTEM startup task to recover the
    centre before interactive logon. Copying the database to another PC does not
    copy usable credentials. Other platforms require an explicit secret provider.
    """
    def __init__(self, directory, protect=None, unprotect=None):
        self.directory = Path(directory) / 'management-credentials'
        self.protect = protect or (lambda raw: self.dpapi(raw, True))
        self.unprotect = unprotect or (lambda raw: self.dpapi(raw, False))

    @staticmethod
    def dpapi(raw, encrypt):
        if os.name != 'nt':
            raise ValueError('自动 SSH 授权需要操作系统安全存储；当前实现支持 Windows DPAPI')
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
        buffer = ctypes.create_string_buffer(raw)
        source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        result = Blob()
        library = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        if encrypt:
            operation = library.CryptProtectData
            operation.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
            ok = operation(ctypes.byref(source), 'BenchWeb management credentials', None, None, None, 5, ctypes.byref(result))
        else:
            operation = library.CryptUnprotectData
            operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
            ok = operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
        if not ok:
            raise ValueError('操作系统无法保护或解密管理凭据；未降级为明文保存')
        try:
            return ctypes.string_at(result.data, result.size)
        finally:
            kernel.LocalFree(result.data)

    def path(self, profile_id):
        return self.directory / (hashlib.sha256(profile_id.encode()).hexdigest() + '.dpapi')

    def save(self, profile_id, value):
        encrypted = self.protect(json.dumps(value).encode())
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == 'nt':
            sid = subprocess.check_output(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'], text=True).strip()
            subprocess.run(['icacls.exe', str(self.directory), '/inheritance:r', '/grant:r',
                            '*' + sid + ':(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        path = self.path(profile_id)
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(encrypted)
        temporary.chmod(0o600)
        temporary.replace(path)

    def load(self, profile_id):
        return json.loads(self.unprotect(self.path(profile_id).read_bytes()))

    def has(self, profile_id):
        return self.path(profile_id).is_file()

    def remove(self, profile_id):
        self.path(profile_id).unlink(missing_ok=True)


def make_credentials(profile, expected_fingerprint):
    module = paramiko_module()
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    key = module.RSAKey.generate(3072)
    private = io.StringIO()
    key.write_private_key(private)
    signer = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    return {**{k: profile[k] for k in ('id', 'host', 'ssh_port', 'user')},
            'fingerprint': expected_fingerprint, 'authorization_id': secrets.token_hex(16),
            'ssh_private': private.getvalue(), 'ssh_public': key.get_name() + ' ' + key.get_base64(),
            'signer_private': signer.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption()).decode(),
            'signer_public': signer.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()}


INSTALL = r'''
import json,os,sys,shlex,tempfile,fcntl
from pathlib import Path
p=json.loads(PAYLOAD)
assert os.geteuid()==0, '自动管理首次授权需要 root 安装专用恢复服务；不会修改其他用户授权'
root=Path('/var/lib/bench-web-plus')/p['authorization_id']
root.parent.mkdir(mode=0o700,exist_ok=True)
assert root.parent.resolve()==root.parent and not root.is_symlink()
root.mkdir(mode=0o700,exist_ok=True)
assert root.stat().st_uid==os.geteuid()
assert not (root/'revoked').is_symlink()
(root/'revoked').unlink(missing_ok=True)
for name,text in [('management_host.py',p['source']),('signer.pem',p['signer_public'])]:
 path=root/name
 assert not path.is_symlink()
 path.write_text(text);path.chmod(0o600)
source=os.environ.get('SSH_CONNECTION','').split()[0]
import ipaddress
ipaddress.ip_address(source)
command=shlex.join([sys.executable,'-I','-S',str(root/'management_host.py')])
assert '"' not in command and '\n' not in command
marker='bench-web-plus:'+p['authorization_id']
line='restrict,from="'+source+'",command="'+command+'" '+p['ssh_public']+' '+marker
ssh=Path.home()/'.ssh'
assert not ssh.is_symlink()
ssh.mkdir(mode=0o700,exist_ok=True)
path=ssh/'authorized_keys'
assert not path.is_symlink()
with (ssh/'.bench-web-plus.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 lines=path.read_text().splitlines() if path.exists() else []
 matching=[x for x in lines if x.endswith(' '+marker)]
 assert not matching or (len(matching)==1 and (' '+p['ssh_public']+' ') in matching[0]), 'Existing authorization differs'
 if matching and matching!=[line]:
  path.write_text('\n'.join(line if item.endswith(' '+marker) else item for item in lines)+'\n')
 elif not matching:
  with path.open('a') as out:out.write(('\n' if lines else '')+line+'\n')
 path.chmod(0o600)
print(json.dumps({'server_root':str(root),'source':source,'authorization_id':p['authorization_id']}))
'''


class ManagedSession(SessionSSH):
    """Reconnect before dispatch; never replay a possibly executed operation."""
    def __init__(self, credential):
        self.credential = credential
        self.lock = threading.Lock()
        self.client = None
        self.last_error = ''
        self.next_retry = 0
        self.failures = 0
        self.closed = False
        self.blocked = False
        self.last_probe = 0
        self._signer = None

    def connected(self):
        transport = self.client.get_transport() if self.client else None
        return bool(transport and transport.is_active() and transport.is_authenticated())

    def connect(self):
        if self.closed or self.blocked:
            raise ValueError(self.last_error or '自动管理会话已关闭')
        if self.connected():
            return
        if time.monotonic() < self.next_retry:
            raise ValueError('SSH 正在自动重连，请稍后重试：' + self.last_error)
        module = paramiko_module()
        expected = self.credential['fingerprint']
        class Pinned(module.MissingHostKeyPolicy):
            def missing_host_key(self, client, host, key):
                if fingerprint(key) != expected:
                    raise ValueError('服务器指纹变化，已停止自动重连；需要人工核对')
        client = module.SSHClient()
        quiet_transport(client)
        client.set_missing_host_key_policy(Pinned())
        try:
            key = module.RSAKey.from_private_key(io.StringIO(self.credential['ssh_private']))
            client.connect(self.credential['host'], port=self.credential['ssh_port'], username=self.credential['user'],
                           pkey=key, timeout=10, banner_timeout=10, auth_timeout=10, look_for_keys=False, allow_agent=False)
            client.get_transport().set_keepalive(30)
            if self.closed:
                client.close()
                raise ValueError('自动管理会话已关闭')
            if self.client:
                self.client.close()
            self.client = client
            self.last_error = ''
            self.failures = 0
            self.next_retry = 0
        except Exception as exc:
            client.close()
            self.failures += 1
            self.blocked = isinstance(exc, (module.AuthenticationException, ValueError))
            self.last_error = ('服务器拒绝工具密钥，授权可能已撤销' if isinstance(exc, module.AuthenticationException)
                               else str(exc) if isinstance(exc, ValueError) else '服务器 SSH 暂不可达')
            self.next_retry = time.monotonic() + min(60, 2 ** min(self.failures, 6))
            raise ValueError(self.last_error) from None

    def _request(self, payload, timeout, log=None):
        channel = self.client.get_transport().open_session(timeout=10)
        channel.settimeout(10)
        channel.set_combine_stderr(True)
        started = False
        try:
            channel.exec_command('bench-web-management')
            started = True
            channel.sendall(json.dumps(payload).encode())
            channel.shutdown_write()
            chunks, pending = [], ''
            deadline = time.monotonic() + timeout
            import codecs
            decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError()
                if channel.recv_ready():
                    text = decoder.decode(channel.recv(32768))
                    chunks.append(text)
                    if log:
                        pending += text
                        while '\n' in pending:
                            line, pending = pending.split('\n', 1)
                            log(line)
                elif channel.exit_status_ready():
                    break
                else:
                    time.sleep(.03)
            if log and pending:
                log(pending)
            output = ''.join(chunks)
            code = channel.recv_exit_status()
            if code < 0:
                raise ConnectionError('SSH exit status unknown')
            if code:
                raise RemoteCommandError(output[-2000:] or '远端管理操作失败')
            return output
        except RemoteCommandError:
            raise
        except Exception:
            self.client.close()
            raise ValueError('SSH 通信中断，操作结果未知；未自动重复执行。连接会自动恢复，请查看部署记录') from None
        finally:
            channel.close()

    def execute(self, script, timeout=60, log=None):
        # The deployed broker accepts 32 MiB JSON / 24 MiB decoded scripts.
        # Leave room for base64, signature and envelope; fail before dispatch.
        if len(script.encode()) > 24 * 1024 * 1024 - 8192:
            raise ValueError('管理脚本超过安全请求上限，请使用分块传输；远端未执行')
        paramiko_module()
        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        with self.lock:
            self.connect()
            challenge = json.loads(self._request({'action': 'challenge'}, 15))['nonce']
            raw = script.encode()
            if self._signer is None:
                self._signer = serialization.load_pem_private_key(self.credential['signer_private'].encode(), password=None)
            signature = self._signer.sign(challenge.encode() + bytes([0]) + raw, padding.PKCS1v15(), hashes.SHA256())
            return self._request({'action': 'execute', 'nonce': challenge, 'script': base64.b64encode(raw).decode(),
                                  'signature': base64.b64encode(signature).decode()}, timeout, log)

    def probe(self):
        self.connect()
        if time.monotonic() - self.last_probe < 15:
            return
        try:
            self._request({'action':'challenge'}, 5)
            self.last_probe = time.monotonic()
        except Exception:
            self.client.close()
            self.last_error = 'SSH 探活失败，正在自动重连'
            self.failures += 1
            self.next_retry = time.monotonic() + min(60, 2 ** min(self.failures, 6))
            raise

    def upload(self, source, directory, log=None):
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        path = directory + '/ascend-bench-kit-' + digest[:12] + '.tar.gz'
        # Never embed archive bytes in the cache query. A 38 MiB archive became
        # a ~68 MiB signed JSON request after two base64 layers and was rejected
        # before the remote cache check could even run.
        code = '''import hashlib,json,os,tempfile
from pathlib import Path
a=json.loads(PARAMETERS);p=Path(a['path'])
assert str(p).startswith(('/home/','/mnt/')) and all(q.resolve()==q for q in [p.parent,*p.parent.parents])
if p.is_symlink():raise ValueError('缓存路径包含符号链接')
def digest_file(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for block in iter(lambda:f.read(1048576),b''):h.update(block)
 return h.hexdigest()
if p.is_file() and digest_file(p)==a['digest']:print(json.dumps({'cached':True}))
else:
 p.parent.mkdir(parents=True,exist_ok=True)
 fd,name=tempfile.mkstemp(prefix=p.name+'.',suffix='.upload',dir=p.parent)
 os.close(fd)
 print(json.dumps({'cached':False,'temporary':name}))
'''.replace('PARAMETERS', repr(json.dumps({'path':path,'digest':digest})))
        if log:
            log('检查共享路径缓存；目标安装包 SHA256：' + digest)
        result = json.loads(self.execute(code, timeout=60))
        if result['cached']:
            if log:
                log('共享路径已有当前版本且 SHA256 一致，直接复用；本次上传 0 B')
            return path
        temporary = result['temporary']
        if not (temporary.startswith(path + '.') and temporary.endswith('.upload') and '/' not in temporary[len(path) + 1:]):
            raise ValueError('远端临时安装包路径不符合预期；安装未开始')
        total, sent, started, last_log = source.stat().st_size, 0, time.monotonic(), 0
        if log:
            log(f'缓存缺失，分块同步安装包（{total:,} B，每块最多 1 MiB）')
        with source.open('rb') as stream:
            for raw in iter(lambda: stream.read(1024 * 1024), b''):
                chunk = '''import base64,fcntl,hashlib,json,os,stat
from pathlib import Path
a=json.loads(PARAMETERS);p=Path(a['path'])
assert all(q.resolve()==q for q in [p.parent,*p.parent.parents])
raw=base64.b64decode(a['data'],validate=True)
assert hashlib.sha256(raw).hexdigest()==a['digest']
fd=os.open(p,os.O_RDWR|os.O_NOFOLLOW)
with os.fdopen(fd,'r+b') as f:
 fcntl.flock(f,fcntl.LOCK_EX)
 info=os.fstat(f.fileno())
 assert stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid() and info.st_nlink==1
 assert info.st_size==a['offset'],'Unexpected upload offset; partial archive was not published'
 f.seek(a['offset']);f.write(raw);f.flush()
 print(f.tell())
'''.replace('PARAMETERS', repr(json.dumps({'path':temporary,'offset':sent,'digest':hashlib.sha256(raw).hexdigest(),'data':base64.b64encode(raw).decode()})))
                received = int(self.execute(chunk, timeout=60).strip())
                sent += len(raw)
                if received != sent:
                    raise ValueError('分块上传偏移校验失败；安装未开始')
                now = time.monotonic()
                if log and (sent == total or now - last_log >= 2):
                    last_log = now
                    log(f'上传进度 {sent * 100 // max(total, 1)}% · {sent:,}/{total:,} B · {sent / max(now-started, .01) / 1024:.0f} KiB/s')
        finish = '''import fcntl,hashlib,json,os,stat
from pathlib import Path
a=json.loads(PARAMETERS);p=Path(a['path']);temporary=Path(a['temporary'])
assert all(q.resolve()==q for q in [p.parent,*p.parent.parents]) and temporary.parent==p.parent
if p.is_symlink():raise ValueError('缓存路径包含符号链接')
fd=os.open(temporary,os.O_RDWR|os.O_NOFOLLOW)
with os.fdopen(fd,'r+b') as f:
 fcntl.flock(f,fcntl.LOCK_EX)
 info=os.fstat(f.fileno());assert stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid() and info.st_nlink==1 and info.st_size==a['total']
 h=hashlib.sha256()
 for block in iter(lambda:f.read(1048576),b''):h.update(block)
 assert h.hexdigest()==a['digest'],'Full archive SHA256 mismatch; package not published'
 f.flush();os.fsync(f.fileno());os.fchmod(f.fileno(),0o644)
 os.replace(temporary,p)
print('uploaded')
'''.replace('PARAMETERS', repr(json.dumps({'path':path,'temporary':temporary,'total':total,'digest':digest})))
        self.execute(finish, timeout=60)
        if log:
            log('安装包已分块同步，远端完整 SHA256 校验通过')
        return path

    def close(self):
        self.closed = True
        if self.client:
            self.client.close()


class Authorizations:
    def __init__(self, directory, vault=None):
        self.vault = vault or Vault(directory)
        self.stop = threading.Event()
        self.threads = {}
        self.cancel = {}

    def watch(self, profile_id, session):
        if profile_id in self.cancel:
            self.cancel[profile_id].set()
        cancelled = threading.Event()
        self.cancel[profile_id] = cancelled
        def maintain():
            while not self.stop.wait(2) and not cancelled.is_set():
                if session.lock.acquire(blocking=False):
                    try:
                        session.probe()
                    except Exception:
                        pass  # Bounded backoff, visible status, never password retry.
                    finally:
                        session.lock.release()
        thread = threading.Thread(target=maintain, name='ssh-recovery-' + profile_id, daemon=True)
        self.threads[profile_id] = thread
        thread.start()

    def restore(self, profiles, sessions):
        for profile in profiles:
            if not profile.get('auto_manage'):
                continue
            if not self.vault.has(profile['id']):
                profile['management_error'] = '本中心缺少已授权的管理凭据，请重新进行一次授权'
                continue
            try:
                session = ManagedSession(self.vault.load(profile['id']))
                sessions[profile['id']] = session
                self.watch(profile['id'], session)
            except Exception:
                # Do not overwrite the encrypted credential if OS decryption fails.
                profile['management_error'] = '自动管理凭据无法解密，请在原中心电脑核对安全存储'

    def authorize(self, password_session, profile, expected_fingerprint):
        previous = self.vault.load(profile['id']) if self.vault.has(profile['id']) else None
        credential = dict(previous) if previous else make_credentials(profile, expected_fingerprint)
        if previous and any(previous[k] != profile[k] for k in ('id','host','ssh_port','user')):
            raise ValueError('已保存的自动管理身份与机器不匹配，未替换授权')
        # Test secure storage before making any SSH authorization change.
        self.vault.save(profile['id'], credential)
        payload = {k: credential[k] for k in ('authorization_id', 'signer_public', 'ssh_public')}
        payload['source'] = (Path(__file__).with_name('management_host.py')).read_text(encoding='utf-8')
        managed = None
        try:
            receipt = json.loads(password_session.execute(INSTALL.replace('PAYLOAD', repr(json.dumps(payload))), timeout=45))
            credential.update(receipt)
            self.vault.save(profile['id'], credential)
            managed = ManagedSession(credential)
            proof = managed.execute("print('BENCHWEB_MANAGEMENT_AUTHORIZED')", timeout=20)
            if proof.strip() != 'BENCHWEB_MANAGEMENT_AUTHORIZED':
                raise ValueError('自动管理授权未通过实际连接检查')
            from .recovery import install_service
            credential['recovery_service'] = install_service(managed)['unit']
            self.vault.save(profile['id'], credential)
            return managed
        except Exception:
            if managed:
                managed.close()
            if previous:
                self.vault.save(profile['id'], previous)
                raise ValueError('重新授权未完成，原管理凭据保留；未自动撤销原授权') from None
            # Remove only this newly added key, retaining all unrelated keys.
            try:
                password_session.execute(revoke_script(credential), timeout=20)
            except Exception:
                # Keep encrypted material so a partial authorization remains revocable.
                raise ValueError('自动管理授权未确认，凭据已安全保留以供撤销；请查看连接状态') from None
            self.vault.remove(profile['id'])
            raise

    def revoke(self, profile_id, session):
        credential = self.vault.load(profile_id)
        session.execute(revoke_script(credential), timeout=30)
        if profile_id in self.cancel:
            self.cancel[profile_id].set()
        self.vault.remove(profile_id)
        session.close()

    def close(self):
        self.stop.set()


def revoke_script(credential):
    return r'''import fcntl,json,subprocess
from pathlib import Path
p=json.loads(PAYLOAD)
marker='bench-web-plus:'+p['authorization_id']
root=Path('/var/lib/bench-web-plus')/p['authorization_id']
assert root.resolve()==root and root.stat().st_uid==0 and not (root/'revoked').is_symlink()
(root/'revoked').write_text('revoked');(root/'revoked').chmod(0o600)
ssh=Path.home()/'.ssh';path=ssh/'authorized_keys'
assert not ssh.is_symlink() and not path.is_symlink()
with (ssh/'.bench-web-plus.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 lines=path.read_text().splitlines()
 path.write_text('\n'.join(line for line in lines if not line.endswith(' '+marker))+'\n')
 path.chmod(0o600)
unit='bench-web-'+p['authorization_id']+'.service';unitpath=Path('/etc/systemd/system')/unit
if unitpath.is_file():
 assert not unitpath.is_symlink() and (' '+str(root/'recovery_host.py')+'\n') in unitpath.read_text(), 'Unrecognized recovery service'
 subprocess.run(['systemctl','disable','--now',unit],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
 unitpath.unlink();subprocess.run(['systemctl','daemon-reload'],check=True)
print('revoked')
'''.replace('PAYLOAD', repr(json.dumps({'authorization_id': credential['authorization_id']})))
