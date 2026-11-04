"""Local-only v1 console. Python 3.12+, no third-party dependencies."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request
import uuid

from .transport import Transport, load_profiles, validate_spec, validate_install_network
from . import bootstrap
from .session_ssh import SessionSSH, RemoteCommandError, probe, validate_endpoint, INSPECT_SCRIPT

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / 'fleet/static'
PICKER_HTML = '''<section class="panel" id="fleetPicker" style="margin-bottom:20px"><div class="panelhead"><h2>步骤1 · 选择测试 Runner</h2></div><div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap"><select aria-label="测试 Runner" style="min-width:270px;flex:1" disabled><option value="">请先刷新状态</option></select><button class="secondary" data-refresh>刷新状态</button><button class="primary" data-select disabled>使用所选 Runner</button></div><p data-message role="status" style="font-size:12px;margin-top:10px">请先刷新状态，再选择健康空闲的 Runner。</p></section>'''


def workbench_html(runner_id=None):
    raw = (ROOT / 'fleet/runner-overlay/website/index.html').read_text(encoding='utf-8')
    raw = re.sub(r'步骤([123])', lambda m: '步骤' + str(int(m[1]) + 1), raw)
    raw = raw.replace('<section class="content">', '<section class="content">' + PICKER_HTML)
    raw = raw.replace('<div id="editor"', '<div inert aria-disabled="true" id="editor"')
    raw = raw.replace('</head>', '<style>#editor[inert]{opacity:.45;pointer-events:none}</style></head>')
    scripts = '<script src="/fleet-picker.js"></script>'
    if runner_id:
        scripts += f'<script src="/r/{runner_id}/runner-client.js"></script>'
    return raw.replace('</body>', scripts + '</body>').encode()


class Store:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(directory / 'fleet.sqlite3', check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS objects(id TEXT PRIMARY KEY, kind TEXT, body TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS claims(resource TEXT PRIMARY KEY, runner TEXT, job TEXT)')
        (directory / 'fleet.sqlite3').chmod(0o600)
        self.lock = threading.RLock()
        # A center restart never implies that an install or test has stopped.
        for task in self.list('deployment'):
            if task['state'] not in ('ready', 'failed', 'unknown'):
                task.update(state='unknown', detail='中心重启，远端可能仍在安装；请核对后重试')
                self.save('deployment', task)

    def save(self, kind, value):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?)', (value['id'], kind, json.dumps(value)))

    def get(self, kind, object_id):
        with self.lock:
            row = self.db.execute('SELECT body FROM objects WHERE kind=? AND id=?', (kind, object_id)).fetchone()
        if not row:
            raise ValueError('记录不存在')
        return json.loads(row[0])

    def list(self, kind):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT body FROM objects WHERE kind=? ORDER BY rowid DESC', (kind,))]

    def claim(self, runner, target, job):
        with self.lock, self.db:
            try:
                self.db.execute('INSERT INTO claims VALUES(?,?,?)', ('runner:' + runner, runner, job))
                self.db.execute('INSERT INTO claims VALUES(?,?,?)', ('target:' + target, runner, job))
            except sqlite3.IntegrityError:
                raise ValueError('Runner 或目标服务已被占用；状态未知时请先刷新核对')

    def request_for(self, runner):
        with self.lock:
            row = self.db.execute('SELECT job FROM claims WHERE resource=?', ('runner:' + runner,)).fetchone()
        return row[0] if row else None

    def release(self, runner, request_id):
        with self.lock, self.db:
            self.db.execute('DELETE FROM claims WHERE runner=? AND job=?', (runner, request_id))


class Console:
    def __init__(self, config, directory, demo=False, archive=None):
        self.demo = demo
        self.config_path = config
        self.directory = directory
        self.store = Store(directory)
        self.profiles = load_profiles(config, tool_only=True) if not demo else [{
            'id': 'demo-1', 'name': 'HY4 · 测试服务器', 'host': '192.0.2.10', 'user': 'demo',
            'containers': ['ascend-test'], 'base_roots': ['/home/benchops/ascend-bench'],
            'runner_ports': [18787, 18788], 'ais_paths': []}]
        self.transport = Transport(self.profiles)
        self.executor = ThreadPoolExecutor(max_workers=2)
        self.mutex = threading.RLock()
        self.archive = archive or ROOT / 'dist/ascend-bench-fleet-kit.tar.gz'
        token_path = directory / 'console-token'
        if not token_path.exists():
            token_path.write_text(secrets.token_urlsafe(32), encoding='utf-8')
            token_path.chmod(0o600)
        self.token = token_path.read_text().strip()
        self.sessions = {}
        self.user_sessions = {}
        self.login_attempts = []
        self.machine_inventory = {}
        if demo and not self.store.list('runner'):
            spec = {'profile_id': 'demo-1', 'container': 'ascend-test', 'base': '/home/benchops/ascend-bench', 'port': 18787, 'ais_path': ''}
            rid = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]
            self.store.save('runner', {'id': rid, 'spec': spec, 'token': '', 'observed_at': time.time(), 'error': '',
                'status': {'activity': 'idle', 'version': 'fleet-v1', 'service': {'host': '192.0.2.10', 'port': 8000, 'model': 'Qwen3-32B'}, 'active_job': None}})

    def public_profiles(self):
        return [{**{k: p[k] for k in ('id', 'name', 'host', 'containers', 'base_roots', 'runner_ports', 'ais_paths', 'mode', 'user', 'ssh_port') if k in p},
                 'connected': p.get('mode') != 'session' or p['id'] in self.transport.sessions} for p in self.profiles]

    def probe_machine(self, body):
        host, port = validate_endpoint(body)
        result = probe(host, port)
        known = next((k for k in self.store.list('hostkey') if k['host'] == host and k['port'] == port), None)
        if known and known['fingerprint'] != result['fingerprint']:
            raise ValueError('服务器 SSH 指纹与已登记值不同，已阻止连接；请管理员核对，不会自动替换')
        return {**result, 'trusted': bool(known)}

    def connect_machine(self, body):
        host, port = validate_endpoint(body)
        user = str(body.get('user', 'root')).strip()
        if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_-]*', user):
            raise ValueError('SSH 用户名无效')
        fingerprint = str(body.get('fingerprint', ''))
        if not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', fingerprint):
            raise ValueError('请先检查服务器指纹')
        known = next((k for k in self.store.list('hostkey') if k['host'] == host and k['port'] == port), None)
        if known and known['fingerprint'] != fingerprint:
            raise ValueError('服务器指纹变化，拒绝连接')
        if not known and body.get('confirm_host_key') is not True:
            raise ValueError('首次连接请先核对并确认服务器公钥指纹')
        password = body.pop('password', '')
        if not isinstance(password, str) or not password or len(password) > 1024:
            raise ValueError('请输入一次性使用的服务器登录密码')
        try:
            session = SessionSSH(host, port, user, password, fingerprint)
        except ValueError:
            raise
        except Exception:
            raise ValueError('SSH 登录失败：请核对用户名、密码及服务器网络；密码未保存') from None
        finally:
            password = None
        try:
            inventory = json.loads(session.execute(INSPECT_SCRIPT))
        except Exception:
            session.close()
            raise ValueError('已登录，但无法读取 Docker 容器/镜像；请检查 Docker 权限')
        profile_id = 'session-' + hashlib.sha256(f'{host}:{port}:{user}'.encode()).hexdigest()[:12]
        inventory = self.annotate_inventory(profile_id, inventory)
        profile = {'id': profile_id, 'name': str(body.get('name') or host)[:80], 'host': host, 'user': user,
                   'ssh_port': port, 'mode': 'session', 'containers': [c['name'] for c in inventory['containers']],
                   'base_roots': ['/home', '/mnt'], 'runner_ports': [18787], 'ais_paths': [],
                   'service_hosts': [host, '127.0.0.1', 'localhost'], 'tokenizer_roots': ['/models', '/mnt', '/home']}
        with self.mutex:
            old = self.transport.sessions.get(profile_id)
            if old:
                old.close()
            self.transport.sessions[profile_id] = session
            self.transport.profiles[profile_id] = profile
            self.machine_inventory[profile_id] = inventory
            self.profiles = [p for p in self.profiles if p['id'] != profile_id] + [profile]
            temporary = self.config_path.with_name(self.config_path.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                temporary.write_text(json.dumps({'profiles': self.profiles}, ensure_ascii=False, indent=2), encoding='utf-8')
                temporary.chmod(0o600)
                temporary.replace(self.config_path)
            finally:
                temporary.unlink(missing_ok=True)
            self.store.save('hostkey', {'id': hashlib.sha256(f'{host}:{port}'.encode()).hexdigest(), 'host': host, 'port': port, 'fingerprint': fingerprint})
        return {'profile_id': profile_id, 'inventory': inventory}

    def direct_spec(self, body, profile):
        base = str(body.get('base', '')).strip()
        package_dir = str(body.get('package_dir') or '/mnt/share/ascend_bench/packages').strip()
        port, container = body.get('port'), str(body.get('container', '')).strip()
        for path in (base, package_dir):
            if not path.startswith(('/home/', '/mnt/')) or not re.fullmatch(r'/[a-zA-Z0-9_/-]+', path) or '//' in path or path.endswith('/') or len(path.split('/')) < 4:
                raise ValueError('请使用专用 Linux 项目目录，不含空格、点号或特殊字符')
        if type(port) is not int or not 1 <= port <= 65535 or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}', container):
            raise ValueError('容器名或 Runner 端口无效')
        inventory = self.machine_inventory.get(profile['id'])
        if not inventory or profile['id'] not in self.transport.sessions:
            raise ValueError('请先重新连接机器以读取当前容器与本地镜像')
        mode = body.get('container_mode', 'existing')
        if mode == 'existing' and container not in [c['name'] for c in inventory['containers']]:
            raise ValueError('所选已有容器不在当前机器中')
        if mode not in ('existing', 'new'):
            raise ValueError('请选择已有容器或新建容器模式')
        image = str(body.get('image', ''))
        host_path = str(body.get('host_path') or base).strip()
        if mode == 'new':
            if image not in inventory['images']:
                raise ValueError('只能使用该机器已存在的本地镜像，不自动拉取')
            if container in [c['name'] for c in inventory['containers']]:
                raise ValueError('新容器名已经存在')
            if not host_path.startswith(('/home/', '/mnt/')) or not re.fullmatch(r'/[a-zA-Z0-9_/-]+', host_path) or '//' in host_path or len(host_path.split('/')) < 4 or host_path.endswith('/'):
                raise ValueError('宿主机挂载路径无效')
        return {'profile_id': profile['id'], 'container': container, 'base': base, 'port': port, 'ais_path': '',
                'container_mode': mode, 'image': image, 'host_path': host_path, 'package_dir': package_dir,
                'network': validate_install_network(body.get('network', {}))}

    def inspect_machine(self, profile_id):
        bases = {r['spec']['container']: r['spec']['base'] for r in self.store.list('runner') + self.store.list('removed-runner') if r['spec']['profile_id'] == profile_id}
        script = 'RUNNER_BASES=' + repr(bases) + '\n' + INSPECT_SCRIPT
        inventory = self.annotate_inventory(profile_id, json.loads(self.transport.session(profile_id).execute(script)))
        self.machine_inventory[profile_id] = inventory
        self.transport.profiles[profile_id]['containers'] = [c['name'] for c in inventory['containers']]
        return inventory

    def annotate_inventory(self, profile_id, inventory):
        known = {r['spec']['container'] for r in self.store.list('runner') + self.store.list('removed-runner') if r['spec']['profile_id'] == profile_id}
        known.update(t['spec']['container'] for t in self.store.list('deployment') if t['spec']['profile_id'] == profile_id and t['state'] == 'ready')
        for container in inventory['containers']:
            container['known_runner'] = container['name'] in known or container.get('runner_installed', False)
        return inventory

    def enrollments(self):
        return [{k: e[k] for k in ('id', 'spec', 'expires_at', 'sha256', 'state')} for e in self.store.list('enrollment')]

    def create_enrollment(self, body):
        if self.demo:
            raise ValueError('请在真实模式生成接入包；演示不会创建授权密钥')
        spec = bootstrap.validate(body)
        if not self.archive.is_file():
            raise ValueError('部署包不存在，请先构建')
        if any(p['host'] == spec['host'] for p in self.profiles):
            raise ValueError('机器已登记，请使用已有档案')
        if any(e['spec']['host'] == spec['host'] and e['expires_at'] > time.time() for e in self.store.list('enrollment')):
            raise ValueError('这台机器已有待接入安装包，请在列表中继续，避免重复授权')
        directory = self.directory / 'enrollments' / uuid.uuid4().hex
        entry = bootstrap.generate(directory, self.archive, spec)
        self.store.save('enrollment', entry)
        return next(e for e in self.enrollments() if e['id'] == entry['id'])

    def import_receipt(self, receipt):
        if not isinstance(receipt, dict):
            raise ValueError('请选择安装器生成的 JSON 回执')
        entry = self.store.get('enrollment', receipt.get('id', ''))
        if entry['state'] == 'connected':
            raise ValueError('该机器已接入，不重复修改服务器公钥')
        if entry['expires_at'] < time.time():
            raise ValueError('接入包已过期，请先在服务器撤销旧授权，再生成新包')
        spec = entry['spec']
        host_keys = bootstrap.known_hosts(receipt, spec)
        known_file = Path(entry['key_path']).with_name('known_hosts')
        known_file.write_text(host_keys, encoding='utf-8')
        known_file.chmod(0o600)
        profile_id = 'enrolled-' + entry['id'][:12]
        profile = {'id': profile_id, 'name': spec['name'], 'host': spec['host'], 'user': spec['user'], 'ssh_port': spec['ssh_port'],
                   'mode': 'restricted', 'identity_file': entry['key_path'], 'known_hosts_file': str(known_file.resolve()),
                   'containers': [spec['container']], 'base_roots': [spec['base']], 'runner_ports': [spec['port']], 'ais_paths': [],
                   'service_hosts': spec['service_hosts'], 'service_ports': spec['service_ports'], 'tokenizer_roots': spec['tokenizer_roots']}
        runner_spec = {k: spec[k] for k in ('container', 'base', 'port', 'ais_path')}
        runner_spec['profile_id'] = profile_id
        with self.mutex:
            self.transport.profiles[profile_id] = profile
            try:
                result = self.transport.status(runner_spec)
                profiles = [p for p in self.profiles if p['id'] != profile_id] + [profile]
                temporary = self.config_path.with_name(self.config_path.name + '.' + uuid.uuid4().hex + '.tmp')
                try:
                    with temporary.open('x', encoding='utf-8') as stream:
                        temporary.chmod(0o600)
                        json.dump({'profiles': profiles}, stream, ensure_ascii=False, indent=2)
                    load_profiles(temporary)
                    temporary.replace(self.config_path)
                finally:
                    temporary.unlink(missing_ok=True)
                self.profiles = profiles
                runner_id = hashlib.sha256(json.dumps(runner_spec, sort_keys=True).encode()).hexdigest()[:16]
                self.store.save('runner', {'id': runner_id, 'spec': runner_spec, 'token': '', 'status': result['status'], 'observed_at': time.time(), 'error': ''})
                self.store.save('deployment', {'id': entry['id'], 'runner_id': runner_id, 'spec': runner_spec, 'state': 'ready',
                    'detail': '服务器工具包安装完成，回执与 Runner 身份检查通过', 'created_at': time.time(),
                    'logs': '安装器在服务器执行原 setup.sh；完整安装日志保存在服务器安装包目录 install.log。\n中心已校验服务器公钥和 Runner 容器/目录身份。\n'})
                entry['state'] = 'connected'
                self.store.save('enrollment', entry)
                return {'runner_id': runner_id}
            except Exception:
                if not any(p['id'] == profile_id for p in self.profiles):
                    self.transport.profiles.pop(profile_id, None)
                raise

    def log(self, task, message):
        # Install scripts must never print runner tokens. Also redact current known secrets.
        for runner in self.store.list('runner'):
            message = message.replace(runner.get('token') or '\0', '[REDACTED]')
        task['logs'] = (task['logs'] + time.strftime('%H:%M:%S ') + message + '\n')[-40000:]
        self.store.save('deployment', task)

    def deploy(self, body):
        profile = next((p for p in self.profiles if p['id'] == body.get('profile_id')), None)
        spec = self.direct_spec(body, profile) if profile and profile.get('mode') == 'session' else validate_spec(body, self.profiles)
        identity = {k: spec[k] for k in ('profile_id', 'container', 'base', 'port', 'ais_path')}
        runner_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
        previous = next((r for r in self.store.list('runner') + self.store.list('removed-runner') if all(r['spec'].get(k) == v for k, v in identity.items())), None)
        if previous:
            runner_id = previous['id']
        with self.mutex:
            if self.store.request_for(runner_id):
                raise ValueError('Runner 有测试或待核对占用，暂不能部署/升级')
            for t in self.store.list('deployment'):
                same = t['spec']['profile_id'] == spec['profile_id'] and (t['spec']['port'] == spec['port'] or (t['spec']['container'] == spec['container'] and t['spec']['base'] == spec['base']))
                if same and t['state'] not in ('ready', 'failed'):
                    raise ValueError('该端口/目录已有部署或待核对记录，请勿重复安装')
            if not self.demo and not self.archive.is_file():
                raise ValueError('未生成部署包，请先执行 python -m fleet.build_kit')
            task = {'id': uuid.uuid4().hex, 'runner_id': runner_id, 'spec': spec, 'state': 'queued',
                    'detail': '等待部署', 'logs': '', 'created_at': time.time()}
            self.store.save('deployment', task)
            self.executor.submit(self.worker, task)
        return task

    def worker(self, task):
        def step(state, detail):
            task.update(state=state, detail=detail)
            self.log(task, detail)
        try:
            if self.demo:
                for state, detail in [('checking', '演示：SSH 与容器检查通过'), ('uploading', '演示：传输工具包'), ('installing', '演示：setup.sh 安装 AISBench'), ('health', '演示：身份校验通过')]:
                    step(state, detail)
                    time.sleep(.7)
                result = {'status': {'activity': 'idle', 'root': task['spec']['base'], 'container': task['spec']['container'], 'version': 'fleet-v1'}, 'token': ''}
            else:
                result = self.transport.deploy(task['spec'], self.archive, lambda msg: self.log(task, msg), step)
            runner = {'id': task['runner_id'], 'spec': task['spec'], 'token': result['token'],
                      'status': result['status'], 'observed_at': time.time(), 'error': ''}
            self.store.save('runner', runner)
            step('ready', '部署完成，已加入 Runner 列表')
        except Exception as exc:
            # SSH failures may leave a running remote installation. Retry must be deliberate.
            state = 'failed' if self.demo or isinstance(exc, RemoteCommandError) or task['state'] in ('queued', 'checking', 'uploading') else 'unknown'
            reason = str(exc).strip().splitlines()[-1] if str(exc).strip() else '请检查连接和部署日志'
            reason = re.sub(r'^(ValueError|RuntimeError):\s*', '', reason)
            step(state, '部署未完成：' + reason)

    def runners(self):
        return [{k: v for k, v in r.items() if k != 'token'} for r in self.store.list('runner')]

    def user_view(self):
        profiles = [{k: p[k] for k in ('id', 'name', 'host', 'connected')} for p in self.public_profiles()]
        entries = self.runners()
        runners = [{'id': r['id'], 'spec': {k: r['spec'][k] for k in ('profile_id', 'port', 'container')},
                    'status': {**{k: r['status'].get(k) for k in ('activity', 'version')},
                               'active_job': {'target': {k: (r['status'].get('active_job') or {}).get('target', {}).get(k) for k in ('host', 'port', 'model')}} if r['status'].get('active_job') else None},
                    'observed_at': r['observed_at'], 'error': '连接待管理员恢复' if r.get('error') else ''}
                   for r in entries]
        return {'demo': self.demo, 'profiles': profiles, 'runners': runners}

    def remove_runner(self, runner_id):
        with self.mutex:
            runner = self.store.get('runner', runner_id)
            if self.store.request_for(runner_id) or runner['status'].get('activity') != 'idle':
                raise ValueError('Runner 有任务或待核对占用，请先确认任务结束')
            if any(t['runner_id'] == runner_id and t['state'] not in ('ready', 'failed') for t in self.store.list('deployment')):
                raise ValueError('Runner 仍有部署或待核对记录，不能移除')
            runner['removed_at'] = time.time()
            self.store.save('removed-runner', runner)
        return {'ok': True, 'detail': '已移出测试环境；容器、安装目录、Runner进程和测试结果保留，重新部署可接回'}

    def refresh(self, runner_id):
        runner = self.store.get('runner', runner_id)
        try:
            if not self.demo:
                result = self.transport.status(runner['spec'])
                runner.update(token=result['token'], status=result['status'])
            runner.update(error='', observed_at=time.time())
            # A new process instance in a shared container is not evidence that old children stopped.
            old = self.store.get('runner', runner_id)['status']
            status = runner['status']
            request_id = self.store.request_for(runner_id)
            result = status.get('fleet_requests', {}).get(request_id, {})
            if not self.demo and status.get('instance_id') == old.get('instance_id') and status.get('activity') == 'idle' and status.get('process_tree_confirmed_stopped') and result.get('state') in ('finished', 'rejected'):
                self.store.release(runner_id, request_id)
        except Exception as exc:
            runner.update(error=str(exc), observed_at=time.time())
        with self.mutex:
            self.store.get('runner', runner_id)  # A late refresh must not revive a removed entry.
            self.store.save('runner', runner)
        return {k: v for k, v in runner.items() if k != 'token'}

    def reconcile(self, task_id):
        task = self.store.get('deployment', task_id)
        if task['state'] != 'unknown':
            raise ValueError('只核对状态未知的部署')
        if self.demo:
            raise ValueError('演示模式没有真实远端结果')
        result = self.transport.status(task['spec'])
        runner = {'id': task['runner_id'], 'spec': task['spec'], 'token': result['token'], 'status': result['status'],
                  'observed_at': time.time(), 'error': ''}
        self.store.save('runner', runner)
        task.update(state='ready', detail='核对成功，已接入现有 Runner')
        self.log(task, '按需核对确认 Runner 已就绪')
        return task

    def proxy(self, runner_id, method, path, body):
        if self.demo:
            raise ValueError('演示模式不连接真实 Runner；请用真实模式测试部署与评测')
        runner = self.store.get('runner', runner_id)
        spec = runner['spec']
        profile = self.transport.profiles[spec['profile_id']]
        if method == 'POST':
            if path == '/api/login':
                raise ValueError('中心入口不支持 Runner 凭据登录')
            if path == '/api/config':
                raise ValueError('第一版中心模式禁止修改执行文件；请通过服务面板检查连接')
            if path == '/api/check-service':
                service = body.get('service') or {}
                if service.get('host') not in profile.get('service_hosts', [profile['host'], '127.0.0.1', 'localhost']):
                    raise ValueError('服务地址未获管理员批准，请配置 service_hosts')
                if profile.get('mode') != 'session' and service.get('port') not in profile.get('service_ports', [8000]):
                    raise ValueError('服务端口未获管理员批准，请配置 service_ports')
                tokenizer = service.get('tokenizer_path', '')
                if '..' in tokenizer.split('/') or not any(tokenizer == root or tokenizer.startswith(root.rstrip('/') + '/') for root in profile.get('tokenizer_roots', ['/models', '/mnt/weight'])):
                    raise ValueError('tokenizer 路径未获管理员批准，请配置 tokenizer_roots')
            if path == '/api/jobs':
                target = body.get('execution', {}).get('target', {})
                if target.get('aisbench_path'):
                    raise ValueError('中心模式不允许请求覆盖 AISBench 可执行路径')
                host = body.get('host')
                if host not in profile.get('service_hosts', [profile['host'], '127.0.0.1', 'localhost']):
                    raise ValueError('服务地址未获管理员批准')
                if profile.get('mode') != 'session' and body.get('port') not in profile.get('service_ports', [8000]):
                    raise ValueError('服务端口未获管理员批准')
                normalized = profile['host'] if host in ('127.0.0.1', 'localhost') else host
                normalized = profile.get('service_aliases', {}).get(normalized, normalized)
                target_key = f'{normalized}:{body.get("port")}'
                request_id = uuid.uuid4().hex
                with self.mutex:
                    self.store.get('runner', runner_id)
                    self.store.claim(runner_id, target_key, request_id)
                body['fleet_request_id'] = request_id
        if profile.get('mode') in ('restricted', 'session'):
            return self.transport.request(spec, method, path, body)
        port = self.transport.tunnel(runner_id, spec)
        headers = {'Host': f'127.0.0.1:{spec["port"]}', 'Cookie': f'runner_session_{spec["port"]}=' + runner['token']}
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers['Content-Type'] = 'application/json'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        req = urllib.request.Request(f'http://127.0.0.1:{port}' + path, data=data, headers=headers, method=method)
        try:
            response = opener.open(req, timeout=300)
        except urllib.error.HTTPError as exc:
            response = exc
            # Do not unlock on an error response: request processing may have begun.
        except Exception:
            raise ValueError('Runner 请求结果未知，请刷新核对；占用不会自动释放')
        with response:
            content = response.read(4_000_001)
            if len(content) > 4_000_000:
                raise ValueError('上游响应过大')
            return response.status, response.headers.get('Content-Type', 'application/octet-stream'), content


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = 'AscendFleet/0.1'

    def log_message(self, *args):
        pass  # Do not log credentials or query strings.

    @property
    def app(self):
        return self.server.app

    def send(self, status, content, content_type='application/json; charset=utf-8', cookie=None, filename=None):
        if not isinstance(content, bytes):
            content = json.dumps(content, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        if filename:
            self.send_header('Content-Disposition', 'attachment; filename="' + filename + '"')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(content)

    def handle_request(self, method):
        try:
            host = self.headers.get('Host', '')
            if host not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
                return self.send(403, {'error': '仅允许本机访问'})
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + host:
                return self.send(403, {'error': 'Origin 不匹配'})
            path = urlsplit(self.path).path
            if method == 'GET' and path in ('/', '/style.css', '/app.js'):
                file = STATIC / {'/': 'index.html', '/style.css': 'style.css', '/app.js': 'app.js'}[path]
                mime = {'/': 'text/html; charset=utf-8', '/style.css': 'text/css; charset=utf-8', '/app.js': 'text/javascript; charset=utf-8'}[path]
                return self.send(200, file.read_bytes(), mime)
            body = None
            if method == 'POST':
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    return self.send(403, {'error': '需要 JSON 请求'})
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 100000:
                    raise ValueError('请求大小无效')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError('请求必须为 JSON 对象')
            if path == '/api/mode' and method == 'GET':
                return self.send(200, {'demo': self.app.demo})
            if path == '/api/login' and method == 'POST':
                now = time.time()
                with self.app.mutex:
                    self.app.login_attempts = [t for t in self.app.login_attempts if now - t < 60]
                    if len(self.app.login_attempts) >= 10:
                        return self.send(429, {'error': '尝试过多，请一分钟后重试'})
                    self.app.login_attempts.append(now)
                supplied_token = str(body.get('token', '')).strip().encode('utf-8')
                expected_token = ('demo' if self.app.demo else self.app.token).encode('utf-8')
                if not secrets.compare_digest(supplied_token, expected_token):
                    return self.send(403, {'error': '访问凭据不匹配。请粘贴 console-token 文件中的完整字符串，不是文件路径或说明文字。'})
                session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
                self.app.sessions[session] = (csrf, now + 12 * 3600)
                return self.send(200, {'csrf': csrf}, cookie=f'fleet_session_{self.server.server_port}={session}; HttpOnly; SameSite=Strict; Path=/')
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            user_cookie = cookie.get(f'fleet_user_{self.server.server_port}')
            user = self.app.user_sessions.get(user_cookie.value) if user_cookie else None
            if user and user['expires'] < time.time():
                user = None
            if path == '/api/user/bootstrap' and method == 'GET':
                new_cookie = None
                if not user:
                    sid = secrets.token_urlsafe(32)
                    user = {'csrf': secrets.token_urlsafe(24), 'expires': time.time() + 12 * 3600,
                            'refreshed': {}, 'selected': None}
                    with self.app.mutex:
                        self.app.user_sessions = {k: v for k, v in self.app.user_sessions.items() if v['expires'] > time.time()}
                        if len(self.app.user_sessions) >= 256:
                            self.app.user_sessions.pop(next(iter(self.app.user_sessions)))
                        self.app.user_sessions[sid] = user
                    new_cookie = f'fleet_user_{self.server.server_port}={sid}; HttpOnly; SameSite=Strict; Path=/'
                return self.send(200, {**self.app.user_view(), 'csrf': user['csrf'],
                                       'selected_runner_id': user['selected']}, cookie=new_cookie)
            # Evaluation pages and assets are public on this loopback-only server.
            # Their API session has no machine, installation or administrator privileges.
            route = re.fullmatch(r'/r/([a-f0-9]{16})(/.*)', path)
            if method == 'GET' and path == '/fleet-picker.js':
                return self.send(200, (STATIC / 'fleet-picker.js').read_bytes(), 'text/javascript; charset=utf-8')
            if method == 'GET' and path == '/workbench/':
                return self.send(200, workbench_html(), 'text/html; charset=utf-8')
            asset_name = None
            if method == 'GET' and re.fullmatch(r'/workbench/[a-zA-Z0-9_-]+\.(css|js)', path):
                asset_name = path.rsplit('/', 1)[1]
            if route and method == 'GET':
                rid, tail = route.groups()
                self.app.store.get('runner', rid)
                if tail == '/':
                    return self.send(200, workbench_html(rid), 'text/html; charset=utf-8')
                if tail == '/runner-client.js':
                    return self.send(200, (ROOT / 'fleet/runner-overlay/scripts/runner-client.js').read_bytes(), 'text/javascript; charset=utf-8')
                if re.fullmatch(r'/[a-zA-Z0-9_-]+\.(css|js)', tail):
                    asset_name = tail[1:]
            if asset_name:
                asset = ROOT / 'fleet/runner-overlay/website' / asset_name
                if asset.is_file():
                    return self.send(200, asset.read_bytes(), 'text/css' if asset_name.endswith('.css') else 'text/javascript')
            session = cookie.get(f'fleet_session_{self.server.server_port}')
            admin_auth = self.app.sessions.get(session.value) if session else None
            user_route = path.startswith('/api/user/') or bool(route)
            auth = (user['csrf'], user['expires']) if user_route and user else admin_auth
            if not auth or auth[1] < time.time():
                return self.send(401, {'error': '请刷新用户页面' if user_route else '请登录本机控制台'})
            if method == 'POST' and not secrets.compare_digest(self.headers.get('X-CSRF-Token', '').encode('utf-8'), auth[0].encode('utf-8')):
                return self.send(403, {'error': 'CSRF 校验失败，请重新登录'})
            if path == '/api/logout' and method == 'POST':
                self.app.sessions.pop(session.value, None)
                return self.send(200, {'ok': True}, cookie=f'fleet_session_{self.server.server_port}=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/')
            if path == '/api/user/refresh' and method == 'POST' and user:
                result = self.app.refresh(body['runner_id'])
                if result.get('error'):
                    user['refreshed'].pop(body['runner_id'], None)
                    raise ValueError('Runner 连接不可用，请联系管理员恢复连接')
                user['refreshed'][body['runner_id']] = time.time()
                return self.send(200, {'ok': True})
            if path == '/api/user/select' and method == 'POST' and user:
                rid = body.get('runner_id')
                if time.time() - user['refreshed'].get(rid, 0) > 60:
                    raise ValueError('请先刷新状态，再选择 Runner；状态超过60秒后需重新刷新')
                result = self.app.refresh(rid)
                profile = self.app.transport.profiles[result['spec']['profile_id']]
                if result.get('error') or profile.get('mode') == 'session' and profile['id'] not in self.app.transport.sessions:
                    raise ValueError('Runner 连接不可用，请联系管理员恢复连接')
                if result['status'].get('activity') != 'idle':
                    raise ValueError('Runner 当前非空闲，请刷新后选择其他 Runner')
                user['selected'] = rid
                return self.send(200, {'runner_id': rid})
            if method == 'GET' and path == '/fleet-picker.js':
                return self.send(200, (STATIC / 'fleet-picker.js').read_bytes(), 'text/javascript; charset=utf-8')
            if method == 'GET' and path == '/workbench/':
                return self.send(200, workbench_html(), 'text/html; charset=utf-8')
            if method == 'GET' and re.fullmatch(r'/workbench/[a-zA-Z0-9_-]+\.(css|js)', path):
                asset = ROOT / 'fleet/runner-overlay/website' / path.rsplit('/', 1)[1]
                if asset.is_file():
                    return self.send(200, asset.read_bytes(), 'text/css' if path.endswith('.css') else 'text/javascript')
            if path == '/api/bootstrap' and method == 'GET':
                return self.send(200, {'demo': self.app.demo, 'profiles': self.app.public_profiles(), 'csrf': auth[0],
                                       'runners': self.app.runners(), 'deployments': self.app.store.list('deployment'), 'enrollments': self.app.enrollments(), 'inventory': self.app.machine_inventory})
            if path == '/api/machines/probe' and method == 'POST':
                return self.send(200, self.app.probe_machine(body))
            if path == '/api/machines/connect' and method == 'POST':
                return self.send(200, self.app.connect_machine(body))
            if path == '/api/machines/inspect' and method == 'POST':
                return self.send(200, self.app.inspect_machine(body['profile_id']))
            if path == '/api/enrollments' and method == 'POST':
                return self.send(201, self.app.create_enrollment(body))
            if path == '/api/enrollments/import' and method == 'POST':
                return self.send(200, self.app.import_receipt(body.get('receipt')))
            if re.fullmatch(r'/api/enrollments/[a-f0-9]{32}/download', path) and method == 'GET':
                entry = self.app.store.get('enrollment', path.split('/')[-2])
                if entry['expires_at'] < time.time():
                    raise ValueError('接入包已过期')
                return self.send(200, Path(entry['archive']).read_bytes(), 'application/gzip', filename='install-bundle.tar.gz')
            if path == '/api/deployments' and method == 'POST':
                return self.send(202, self.app.deploy(body))
            if re.fullmatch(r'/api/runners/[a-f0-9]{16}/remove', path) and method == 'POST':
                return self.send(200, self.app.remove_runner(path.split('/')[3]))
            if path == '/api/profiles' and method == 'POST':
                raise ValueError('已改为专用接入包方式，请使用“接入新机器”；无需个人密钥路径')
            if path.startswith('/api/deployments/') and method == 'GET':
                return self.send(200, self.app.store.get('deployment', path.rsplit('/', 1)[-1]))
            if re.fullmatch(r'/api/deployments/[a-f0-9]+/reconcile', path) and method == 'POST':
                return self.send(200, self.app.reconcile(path.split('/')[-2]))
            if path == '/api/refresh' and method == 'POST':
                return self.send(200, self.app.refresh(body['runner_id']))
            match = re.fullmatch(r'/r/([a-f0-9]{16})(/.*)', path)
            if match:
                rid, tail = match.groups()
                self.app.store.get('runner', rid)
                if method == 'POST' and user and user.get('selected') != rid:
                    return self.send(403, {'error': '请先刷新状态并选择此 Runner'})
                kit = ROOT / 'fleet/runner-overlay'
                if method == 'GET' and tail == '/':
                    return self.send(200, workbench_html(rid), 'text/html; charset=utf-8')
                if method == 'GET' and tail == '/runner-client.js':
                    return self.send(200, (kit / 'scripts/runner-client.js').read_bytes(), 'text/javascript; charset=utf-8')
                if method == 'GET' and re.fullmatch(r'/[a-zA-Z0-9_-]+\.(css|js)', tail):
                    asset = kit / 'website' / tail[1:]
                    if asset.is_file():
                        return self.send(200, asset.read_bytes(), 'text/css' if tail.endswith('.css') else 'text/javascript')
                allowed = (method == 'GET' and (tail in ('/api/info', '/api/config', '/api/jobs') or re.fullmatch(r'/api/jobs/real-[a-z0-9-]+', tail))) or (method == 'POST' and (tail in ('/api/check-service', '/api/jobs', '/api/datasets/check', '/api/datasets/prepare', '/api/datasets/generate', '/api/config') or re.fullmatch(r'/api/jobs/real-[a-z0-9-]+/stop', tail)))
                if not allowed:
                    raise ValueError('不支持的代理接口')
                status, mime, raw = self.app.proxy(rid, method, tail, body)
                return self.send(status, raw, mime)
            return self.send(404, {'error': '页面不存在'})
        except (ValueError, KeyError, TypeError) as exc:
            return self.send(400, {'error': str(exc)})
        except Exception:
            return self.send(502, {'error': '请求未完成，请查看部署记录或刷新连接'})

    def do_GET(self):
        self.handle_request('GET')

    def do_POST(self):
        self.handle_request('POST')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=19881)
    parser.add_argument('--config', type=Path, default=ROOT / 'fleet-config.json')
    parser.add_argument('--data', type=Path)
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    app = Console(args.config, args.data or ROOT / ('.fleet-demo' if args.demo else '.fleet-data'), args.demo)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.daemon_threads = True
    server.app = app
    print(f'本机控制台：http://127.0.0.1:{args.port}', flush=True)
    print(f'凭据文件：{app.directory / "console-token"}（请在本机读取并登录）', flush=True)
    print('模式：' + ('演示，不连接服务器' if args.demo else '真实 SSH 部署'), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.transport.close()
        app.executor.shutdown(wait=False)


if __name__ == '__main__':
    main()
