import hashlib
import ipaddress
import base64
import json
from pathlib import Path
import re
import shlex
import socket
import subprocess
import threading
import tarfile
import time
import urllib.request
import uuid
from urllib.parse import urlsplit

HERE = Path(__file__).parent


def validate_install_network(body):
    if not isinstance(body, dict) or set(body) - {'proxy', 'pip_index', 'extra_hosts', 'pip_cert'}:
        raise ValueError('安装网络配置无效')
    result = {}
    certificate=str(body.get('pip_cert','')).strip()
    if certificate:
        if len(certificate)>2048 or not certificate.startswith(('/mnt/','/home/')) or not re.fullmatch(r'/[a-zA-Z0-9_./-]+',certificate) or '..' in certificate.split('/') or certificate.endswith('/'):
            raise ValueError('包源 CA 证书请填写容器可见的 /mnt 或 /home 下绝对文件路径')
        result['pip_cert']=certificate
    for key in ('proxy', 'pip_index'):
        value = str(body.get(key, '')).strip()
        if not value:
            continue
        try:
            parsed = urlsplit(value)
            valid = (len(value) <= 2048 and not any(c.isspace() for c in value)
                     and parsed.scheme in ('http', 'https') and parsed.hostname
                     and re.fullmatch(r'[a-zA-Z0-9.:-]+', parsed.hostname)
                     and not parsed.username and not parsed.password and '@' not in parsed.netloc
                     and not parsed.query and not parsed.fragment
                     and (parsed.port is None or 1 <= parsed.port <= 65535))
        except ValueError:
            valid = False
        if not valid or (key == 'proxy' and parsed.path not in ('', '/')):
            raise ValueError('代理 / pip 源仅接受不含账号密码的 HTTP(S) 地址')
        result[key] = value
    extra = str(body.get('extra_hosts', '')).strip()
    if extra:
        try:
            address, host = extra.split()
            ipaddress.ip_address(address)
            assert re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*', host) and len(host) <= 253
        except (ValueError, AssertionError):
            raise ValueError('域名映射格式为：IP 域名') from None
        result['extra_hosts'] = address + ' ' + host
    return result


def validate_spec(body, profiles):
    profile = next((p for p in profiles if p['id'] == body.get('profile_id')), None)
    if not profile:
        raise ValueError('请先由本机管理员配置 SSH 机器档案')
    container = body.get('container', '')
    base = body.get('base', '')
    port = body.get('port')
    ais = body.get('ais_path', '')
    if container not in profile['containers'] or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}', container):
        raise ValueError('容器不在管理员允许清单内')
    if not re.fullmatch(r'/[a-zA-Z0-9_/-]+', base) or '//' in base or any(x in ('', '.', '..') for x in base[1:].split('/')):
        raise ValueError('目录必须为不带空格、点号和特殊字符的 Linux 绝对路径')
    if not any(base == r or base.startswith(r.rstrip('/') + '/') for r in profile['base_roots']):
        raise ValueError('目录不在管理员允许的安装根目录内')
    if type(port) is not int or port not in profile['runner_ports']:
        raise ValueError('Runner 端口不在管理员允许清单内')
    if ais and ais not in profile.get('ais_paths', []):
        raise ValueError('已有 AISBench 路径未获管理员批准')
    return {'profile_id': profile['id'], 'container': container, 'base': base, 'port': port, 'ais_path': ais}


def load_profiles(path, tool_only=False):
    if not path.exists():
        return []
    profiles = json.loads(path.read_text(encoding='utf-8-sig'))['profiles']
    if tool_only:
        profiles = [p for p in profiles if p.get('mode') in ('restricted', 'session')]
    ids = set()
    for p in profiles:
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', p['id']) or p['id'] in ids:
            raise ValueError('SSH profile id 无效或重复')
        ids.add(p['id'])
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*', p['host']) or not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]*', p['user']):
            raise ValueError('SSH host/user 无效')
        if type(p.get('ssh_port', 22)) is not int or not 1 <= p.get('ssh_port', 22) <= 65535:
            raise ValueError('SSH 端口无效')
        if p.get('mode') == 'session':
            continue
        for field in ('identity_file', 'known_hosts_file'):
            if not Path(p[field]).expanduser().is_file() or any(c in p[field] for c in '\r\n"'):
                raise ValueError(field + ' 不存在或路径无效')
        for root in p['base_roots']:
            if root in ('/', '/etc', '/usr', '/var', '/root') or not re.fullmatch(r'/[a-zA-Z0-9_/-]+', root):
                raise ValueError('请配置专用安装根目录')
        index = p.get('pip_index', '')
        if index and (not index.startswith('https://') or any(c.isspace() for c in index)):
            raise ValueError('pip_index 必须为 HTTPS URL')
    return profiles


class AccessPendingError(RuntimeError):
    def __init__(self, message, runner):
        super().__init__(message)
        self.runner = runner


class Transport:
    def __init__(self, profiles):
        self.profiles = {p['id']: p for p in profiles}
        self.tunnels = {}
        self.lock = threading.Lock()
        self.helper = (HERE / 'remote_helper.py').read_text(encoding='utf-8')
        self.sessions = {}
        self.channels = None

    def options(self, profile):
        return ['-F', 'none', '-o', 'IdentityAgent=none', '-o', 'GlobalKnownHostsFile=none',
                '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=10', '-o', 'ConnectionAttempts=1',
                '-o', 'ForwardAgent=no', '-o', 'IdentitiesOnly=yes',
                '-o', 'UserKnownHostsFile=' + '"' + str(Path(profile['known_hosts_file']).expanduser()) + '"',
                '-i', str(Path(profile['identity_file']).expanduser())]

    def ssh(self, profile):
        return ['ssh', '-T', *self.options(profile), '-p', str(profile.get('ssh_port', 22)), profile['user'] + '@' + profile['host']]

    def request(self, spec, method, path, body=None):
        p = self.profiles[spec['profile_id']]
        if self.channels and self.channels.has(spec):
            result = self.channels.request(spec, method, path, body)
            if result.get('error'):
                raise ValueError('持久评测连接不可用，请管理员检查')
            return result['status'], result['content_type'], base64.b64decode(result['body'], validate=True)
        if p.get('mode') == 'session':
            gateway = (HERE / 'gateway.py').read_text(encoding='utf-8').split("if __name__ == '__main__':")[0]
            gateway_spec = {**spec, 'service_hosts': p.get('service_hosts', [p['host'], '127.0.0.1', 'localhost']),
                            'service_ports': p.get('service_ports'), 'tokenizer_roots': p.get('tokenizer_roots', ['/models', '/mnt', '/home'])}
            data = {'method': method, 'path': path, 'body': body}
            script = gateway + '\nprint(json.dumps(forward(json.loads(' + repr(json.dumps(data)) + '), json.loads(' + repr(json.dumps(gateway_spec)) + '))))\n'
            raw = self.session(spec['profile_id']).execute(script, timeout=320)
        else:
            raw = self.execute([*self.ssh(p), 'runner-api'], json.dumps({'method': method, 'path': path, 'body': body}).encode(), timeout=320)
        result = json.loads(raw)
        if result.get('error'):
            raise ValueError(result['error'])
        return result['status'], result['content_type'], base64.b64decode(result['body'], validate=True)

    def execute(self, argv, data=None, timeout=60, log=None):
        # Capture within a timeout; remote disconnect is not proof of rollback.
        with subprocess.Popen(argv, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
            if log:
                chunks = []
                def consume():
                    for line in iter(lambda: process.stdout.readline(8192), b''):
                        text = line.decode('utf-8', errors='replace')
                        chunks.append(text)
                        if len(chunks) > 200:
                            del chunks[:100]
                        log(text.rstrip())
                reader = threading.Thread(target=consume, daemon=True)
                reader.start()
                if data is not None:
                    try:
                        process.stdin.write(data)
                        process.stdin.close()
                    except BrokenPipeError:
                        pass
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    reader.join(timeout=5)
                    raise RuntimeError('SSH/安装超时：远端可能仍在执行，请核对后重试')
                reader.join(timeout=5)
                text = ''.join(chunks)
                if process.returncode:
                    raise RuntimeError(text[-2000:] or '远端命令失败')
                return text
            try:
                output, _ = process.communicate(data, timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise RuntimeError('SSH/安装超时：远端可能仍在执行，请核对后重试')
            text = output.decode('utf-8', errors='replace')
            if log:
                log(text[-20000:])
            if process.returncode:
                raise RuntimeError(text[-2000:] or '远端命令失败')
            return text

    def helper_call(self, profile, payload, timeout=60, log=None):
        script = self.helper + '\nmain(json.loads(' + repr(json.dumps(payload)) + '))\n'
        if profile.get('mode') == 'session':
            return self.session(profile['id']).execute(script, timeout, log)
        return self.execute([*self.ssh(profile), 'python3 -'], script.encode(), timeout, log)

    def session(self, profile_id):
        session = self.sessions.get(profile_id)
        if not session:
            raise ValueError('会话已断开，请在网页重新连接机器；不会保存密码')
        return session

    def session_connected(self, profile_id):
        session = self.sessions.get(profile_id)
        if not session:
            return False
        if hasattr(type(session), 'connected'):
            return session.connected()
        try:
            transport = session.client.get_transport()
            return bool(transport and transport.is_active() and transport.is_authenticated())
        except Exception:
            return False

    def deploy(self, spec, archive, log, step):
        p = self.profiles[spec['profile_id']]
        if p.get('mode') == 'session':
            step('uploading', '检查共享路径安装包缓存；缺失时自动同步并校验')
            session = self.session(p['id'])
            path = session.upload(archive, spec['package_dir'], log)
            log('远端安装包：' + path)
            step('installing', '检查最新版 Runner；按已有/新容器模式安装或启动')
            runner_source = (HERE / 'runner-overlay/scripts/runner.py').read_bytes().replace(b'\r\n', b'\n')
            payload = {'spec': spec, 'archive': path, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                       'runner_sha256': hashlib.sha256(runner_source).hexdigest(), 'pip_index': p.get('pip_index')}
            payload['network'] = validate_install_network(spec.get('network', {}))
            from .pip_trust import REMOTE, trust_payload
            payload['pip_trust']=trust_payload(payload['network'])
            with tarfile.open(archive) as package:
                payload['release'] = json.load(package.extractfile('ascend-bench-kit/fleet-release.json'))
            script = self.helper + '\n' + REMOTE + '\n' + (HERE / 'replacement.py').read_text(encoding='utf-8') + '\n' + (HERE / 'direct_worker.py').read_text(encoding='utf-8') + '\ndirect_main(json.loads(' + repr(json.dumps(payload)) + '))\n'
            session.execute(script, timeout=2700, log=log)
            spec = {k:v for k,v in spec.items() if not k.startswith('_')}
            if spec.get('durable'):
                helper = (HERE / 'remote_helper.py').read_text(encoding='utf-8')
                check = helper + '\nprint(json.dumps(read_status(json.loads(' + repr(json.dumps(spec)) + '))["status"]))\n'
                verified = {'status': json.loads(session.execute(check, timeout=45)), 'token': ''}
                step('connecting', 'Runner 已就绪，正在迁移或核对受限持久连接')
                try:
                    self.channels.install(session, spec, p, log)
                except Exception as exc:
                    raise AccessPendingError(str(exc), verified) from None
            step('health', '核对 Runner 身份、版本、健康和空闲状态')
            return self.status(spec)
        if p.get('mode') in ('restricted', 'session'):
            step('health', '专用接入仅允许评测 API；检查工具包已安装的 Runner')
            return self.status(spec)
        remote_dir = '/tmp/ascend-bench-fleet-' + uuid.uuid4().hex
        step('checking', '验证 SSH 主机密钥和远端工具')
        self.execute([*self.ssh(p), 'command -v python3 && command -v docker && command -v bash'], log=log)
        # Python mkdir with private mode; no user-controlled shell text.
        self.execute([*self.ssh(p), 'python3 -'], ('import os\nos.mkdir(' + repr(remote_dir) + ',0o700)\n').encode())
        step('uploading', '上传本地固定版本工具包（不在服务器重复下载）')
        remote_archive = remote_dir + '/kit.tar.gz'
        self.execute(['scp', *self.options(p), '-P', str(p.get('ssh_port', 22)), str(archive),
                      p['user'] + '@' + p['host'] + ':' + remote_archive], timeout=180, log=log)
        step('installing', '执行原 setup.sh；首次安装依赖可能需要数分钟')
        self.helper_call(p, {'action': 'deploy', 'spec': spec, 'archive': remote_archive,
                            'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'pip_index': p.get('pip_index')}, 1800, log)
        step('health', '读取凭据并验证 Runner 身份')
        result = json.loads(self.helper_call(p, {'action': 'status', 'spec': spec}))
        log('健康校验成功；远端临时包位于 ' + remote_dir + '，可在确认后清理')
        return result

    def status(self, spec):
        p = self.profiles[spec['profile_id']]
        if p.get('mode') in ('restricted', 'session'):
            code, _, raw = self.request(spec, 'GET', '/api/status')
            if code != 200:
                raise ValueError('Runner 状态查询失败')
            status = json.loads(raw)
            if status.get('root') != spec['base'] or status.get('container') != spec['container']:
                raise ValueError('Runner 身份与登记的目录/容器不匹配')
            return {'status': status, 'token': ''}
        return json.loads(self.helper_call(p, {'action': 'status', 'spec': spec}))

    def tunnel(self, runner_id, spec):
        with self.lock:
            existing = self.tunnels.get(runner_id)
            if existing and existing[0].poll() is None:
                return existing[1]
            p = self.profiles[spec['profile_id']]
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            argv = self.ssh(p)
            argv = argv[:-1] + ['-N', '-o', 'ExitOnForwardFailure=yes', '-L', f'127.0.0.1:{port}:127.0.0.1:{spec["port"]}', argv[-1]]
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                if process.poll() is not None:
                    raise RuntimeError('SSH 隧道启动失败，请检查转发权限或刷新连接')
                with socket.socket() as sock:
                    if sock.connect_ex(('127.0.0.1', port)) == 0:
                        self.tunnels[runner_id] = (process, port)
                        return port
                time.sleep(.1)
            process.terminate()
            raise RuntimeError('SSH 隧道启动超时')

    def close(self):
        if getattr(self, 'authorizations', None):
            self.authorizations.close()
        for session in self.sessions.values():
            session.close()
        for process, _ in self.tunnels.values():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
