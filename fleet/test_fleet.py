import concurrent.futures
import io
import hashlib
from contextlib import redirect_stdout, nullcontext
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import types
from urllib.request import Request, build_opener, HTTPCookieProcessor, ProxyHandler
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from .app import Console, Handler, Store
from .transport import Transport, validate_spec, load_profiles, validate_install_network
from . import bootstrap
from .gateway import validate_request
from .session_ssh import SessionSSH


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Console(Path(self.tmp.name) / 'missing.json', Path(self.tmp.name) / 'data', demo=True)
        self.spec = {'profile_id': 'demo-1', 'container': 'ascend-test', 'base': '/home/benchops/ascend-bench', 'port': 18787, 'ais_path': ''}

    def tearDown(self):
        self.app.executor.shutdown(wait=True)
        self.app.store.db.close()
        self.tmp.cleanup()

    def test_spec_allowlist(self):
        self.assertEqual(validate_spec(self.spec, self.app.profiles), self.spec)
        for key, value in [('container', 'test;touch /tmp/x'), ('base', '/etc'), ('base', '/home/benchops/ascend-bench/../etc'), ('base', '/home/benchops/ascend-bench//x'), ('port', True), ('ais_path', '/bin/bash')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_spec({**self.spec, key: value}, self.app.profiles)

    def test_probe_network_errors_are_actionable_without_credentials(self):
        import socket
        from .session_ssh import probe
        class HandshakeError(Exception):
            pass
        module = types.SimpleNamespace(SSHException=HandshakeError)
        cases = [(socket.gaierror(), '无法解析'), (TimeoutError(), '超时'),
                 (ConnectionRefusedError(), '拒绝'), (OSError(), '无法连接')]
        with patch('fleet.session_ssh.paramiko_module', return_value=module):
            for error, message in cases:
                with self.subTest(error=type(error).__name__), patch('fleet.session_ssh.socket.create_connection', side_effect=error):
                    with self.assertRaisesRegex(ValueError, message + '.*尚未进行密码认证'):
                        probe('192.0.2.1', 22)
            module.Transport = lambda sock: types.SimpleNamespace(start_client=lambda **kwargs: (_ for _ in ()).throw(HandshakeError()), close=lambda: None)
            with patch('fleet.session_ssh.socket.create_connection'):
                with self.assertRaisesRegex(ValueError, 'SSH 握手失败.*尚未进行密码认证'):
                    probe('192.0.2.1', 22)

    def test_password_connection_keeps_no_password_on_disk(self):
        self.app.demo = False
        self.app.profiles = []
        body = {'host': '192.0.2.40', 'ssh_port': 22, 'user': 'root', 'password': 'test-only-secret',
                'fingerprint': 'SHA256:' + 'A' * 43, 'confirm_host_key': True}
        fake = types.SimpleNamespace(execute=lambda code: json.dumps({'containers': [{'name': 'existing'}], 'images': ['local:test']}), close=lambda: None)
        with patch('fleet.app.SessionSSH', return_value=fake):
            result = self.app.connect_machine(body)
        self.assertNotIn('password', body)
        self.assertIn(result['profile_id'], self.app.transport.sessions)
        self.assertNotIn('test-only-secret', self.app.config_path.read_text(encoding='utf-8'))
        self.assertNotIn('test-only-secret', json.dumps(self.app.store.list('hostkey')))

    def test_host_key_change_refused_before_authentication(self):
        self.app.store.save('hostkey', {'id': 'known', 'host': '192.0.2.40', 'port': 22, 'fingerprint': 'SHA256:' + 'A' * 43})
        with patch('fleet.app.SessionSSH') as auth:
            with self.assertRaises(ValueError):
                self.app.connect_machine({'host': '192.0.2.40', 'user': 'root', 'password': 'test-only-secret', 'fingerprint': 'SHA256:' + 'B' * 43})
            auth.assert_not_called()

    def test_paramiko_disables_key_search_and_forgets_password(self):
        from unittest.mock import Mock
        client = Mock()
        handler = types.SimpleNamespace(password='test-only-secret')
        client.get_transport.return_value.auth_handler = handler
        module = types.SimpleNamespace(SSHClient=lambda: client, MissingHostKeyPolicy=object)
        with patch('fleet.session_ssh.paramiko_module', return_value=module):
            SessionSSH('192.0.2.40', 22, 'root', 'test-only-secret', 'SHA256:' + 'A' * 43)
        self.assertFalse(client.connect.call_args.kwargs['look_for_keys'])
        self.assertFalse(client.connect.call_args.kwargs['allow_agent'])
        self.assertIsNone(handler.password)
        client.load_system_host_keys.assert_not_called()
        client.load_host_keys.assert_not_called()

    def test_direct_deploy_has_two_modes_no_service_fields(self):
        p = {'id': 'direct', 'mode': 'session'}
        self.app.transport.sessions['direct'] = object()
        self.app.machine_inventory['direct'] = {'containers': [{'name': 'existing'}], 'images': ['local:test']}
        body = {'container': 'existing', 'base': '/home/test/runner', 'port': 18787}
        existing = self.app.direct_spec(body, p)
        self.assertEqual(existing['container_mode'], 'existing')
        new = self.app.direct_spec({**body, 'container_mode': 'new', 'container': 'new', 'image': 'local:test'}, p)
        self.assertEqual(new['host_path'], body['base'])
        self.assertNotIn('tokenizer_path', new)
        self.assertNotIn('service_port', new)
        with self.assertRaises(ValueError):
            self.app.direct_spec({**body, 'base': '/etc/../bad'}, p)
        with self.assertRaises(ValueError):
            self.app.direct_spec({**body, 'container_mode': 'new', 'container': 'new', 'image': 'unknown:remote'}, p)

    def test_existing_stopped_container_can_be_selected(self):
        profile = {'id': 'direct', 'mode': 'session'}
        self.app.transport.sessions['direct'] = object()
        item = {'name': 'old-service', 'state': 'exited', 'managed': False}
        self.app.machine_inventory['direct'] = {'containers': [item], 'images': []}
        body = {'container': 'old-service', 'base': '/home/test/runner', 'port': 18787}
        self.assertEqual(self.app.direct_spec(body, profile)['container'], 'old-service')
        item['managed'] = True
        self.assertEqual(self.app.direct_spec(body, profile)['container'], 'old-service')

    def test_user_role_cannot_install_and_requires_explicit_fresh_selection(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.app = self.app
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f'http://127.0.0.1:{server.server_port}'
        browser = build_opener(ProxyHandler({}), HTTPCookieProcessor())
        def request(path, body=None, csrf=None):
            headers = {'Content-Type': 'application/json'}
            if csrf:
                headers['X-CSRF-Token'] = csrf
            return browser.open(Request(root + path, data=None if body is None else json.dumps(body).encode(), headers=headers))
        try:
            with request('/workbench/') as response:
                page = response.read().decode()
                self.assertNotIn('管理员部署', page)
                self.assertNotIn('← 管理员', page)
                self.assertIn('<div inert aria-disabled="true" id="editor"', page)
                self.assertIn('步骤1 · 选择测试 Runner', page)
            with request('/api/user/bootstrap') as response:
                data = json.load(response)
            csrf = data['csrf']
            rid = data['runners'][0]['id']
            self.assertIsNone(data['selected_runner_id'])
            self.assertNotIn('inventory', data)
            self.assertNotIn('containers', data['profiles'][0])
            self.assertNotIn('base', data['runners'][0]['spec'])
            for path, body in [('/api/bootstrap', None), ('/api/machines/connect', {}), ('/api/deployments', {}), ('/api/runners/' + rid + '/remove', {}), ('/api/logout', {})]:
                with self.assertRaises(HTTPError) as error:
                    request(path, body, csrf)
                self.assertEqual(error.exception.code, 401)
            with self.assertRaises(HTTPError) as error:
                request('/api/user/select', {'runner_id': rid}, csrf)
            self.assertEqual(error.exception.code, 400)
            with self.assertRaises(HTTPError) as error:
                request('/r/' + rid + '/api/check-service', {}, csrf)
            self.assertEqual(error.exception.code, 403)
            with self.assertRaises(HTTPError) as error:
                request('/api/user/refresh', {'runner_id': rid})
            self.assertEqual(error.exception.code, 403)
            with request('/api/user/refresh', {'runner_id': rid}, csrf) as response:
                self.assertTrue(json.load(response)['ok'])
            with request('/api/user/select', {'runner_id': rid}, csrf) as response:
                self.assertEqual(json.load(response)['runner_id'], rid)
            with patch.object(self.app, 'proxy', return_value=(200, 'application/json', b'{"ok":true}')):
                with request('/r/' + rid + '/api/check-service', {}, csrf) as response:
                    self.assertTrue(json.load(response)['ok'])
            next(iter(self.app.user_sessions.values()))['refreshed'][rid] -= 61
            with self.assertRaises(HTTPError) as error:
                request('/api/user/select', {'runner_id': rid}, csrf)
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_package_cache_hit_skips_upload_and_miss_logs_progress(self):
        source = Path(self.tmp.name) / 'package.tar.gz'
        source.write_bytes(b'verified package')
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        session = SessionSSH.__new__(SessionSSH)
        session.lock = threading.Lock()
        session.client = unittest.mock.MagicMock()
        session.execute = unittest.mock.MagicMock(return_value=json.dumps(digest))
        logs = []
        session.upload(source, '/mnt/share/test/packages', logs.append)
        session.client.open_sftp.assert_not_called()
        self.assertTrue(any('0 B' in line for line in logs))
        session.execute.side_effect = ['null', json.dumps(digest)]
        sftp = session.client.open_sftp.return_value.__enter__.return_value
        import stat
        sftp.lstat.return_value = types.SimpleNamespace(st_mode=stat.S_IFDIR | 0o755)
        sftp.put.side_effect = lambda src, dst, callback: callback(source.stat().st_size, source.stat().st_size)
        session.upload(source, '/mnt/share/test/packages', logs.append)
        sftp.put.assert_called_once()
        self.assertTrue(any('上传进度 100%' in line for line in logs))
        self.assertTrue(any('远端 SHA256 校验通过' in line for line in logs))

    def test_new_container_mounts_host_data_readonly_and_project_writable(self):
        source = (Path(__file__).parent / 'direct_worker.py').read_text(encoding='utf-8')
        class Created(Exception):
            pass
        for missing in (None, '/mnt', '/home'):
            with self.subTest(missing=missing):
                root = Path(self.tmp.name) / str(missing).replace('/', '_')
                root.mkdir()
                mapped = {name: root / name[1:] for name in ('/mnt', '/home')}
                for name, directory in mapped.items():
                    if name != missing:
                        directory.mkdir()
                calls = []
                def command(*args):
                    calls.append(args)
                    if args[:2] == ('docker', 'run'):
                        raise Created()
                    return ''
                namespace = {'Path': lambda name: mapped.get(name, Path(name)), 'command': command, 'remote_deployment_lock':lambda spec:nullcontext(),'port_listening':lambda port:False,
                             'subprocess': types.SimpleNamespace(DEVNULL=-3, run=lambda *a, **kw: types.SimpleNamespace(returncode=1))}
                exec(compile(source, 'direct_worker.py', 'exec'), namespace)
                spec = {**self.spec, 'container_mode': 'new', 'host_path': str(root / 'runner'), 'image': 'local:test'}
                with redirect_stdout(io.StringIO()):
                    if missing:
                        with self.assertRaisesRegex(ValueError, '默认挂载目录不存在'):
                            namespace['direct_main']({'spec': spec})
                        self.assertFalse(any(args[:2] == ('docker', 'run') for args in calls))
                    else:
                        with self.assertRaises(Created):
                            namespace['direct_main']({'spec': spec})
                        run = calls[-1]
                        mounts = [run[i+1] for i, value in enumerate(run) if value == '--mount']
                        self.assertEqual(set(mounts), {'type=bind,src=/mnt,dst=/mnt,readonly', 'type=bind,src=/home,dst=/home,readonly'})
                        self.assertTrue(run[run.index('-v')+1].endswith(':' + self.spec['base'] + ':rw'))
                        self.assertNotIn('--privileged', run)
                        self.assertNotIn('--device', run)

    def test_direct_worker_starts_stopped_container_and_checks_complete_release(self):
        source = (Path(__file__).parent / 'direct_worker.py').read_text(encoding='utf-8')
        for case in ('stopped_latest', 'running_latest', 'old_idle', 'old_busy', 'old_failure_unchanged', 'old_failure_changed'):
            with self.subTest(case=case):
                calls = []
                info = {'State': {'Running': case != 'stopped_latest', 'Status': 'exited' if case == 'stopped_latest' else 'running'}, 'HostConfig': {'NetworkMode': 'host'}, 'Mounts': [{'RW': True, 'Destination': self.spec['base']}]}
                install = unittest.mock.MagicMock()
                if case.startswith('old_failure'):install.side_effect=RuntimeError('setup failed')
                probes=[0]
                def command(*args):
                    calls.append(args)
                    if args[:2] == ('docker', 'inspect'):
                        return json.dumps([info])
                    if any('expected=json.loads' in str(a) for a in args):
                        return json.dumps({'installed': True, 'latest': not case.startswith('old_') or (install.called and not case.startswith('old_failure')), 'version': 'changed' if case=='old_failure_changed' and install.called else 'old', 'different': ['scripts/runner-client.js'], 'ais_path': '/home/benchops/ascend-bench/bench-venv/bin/ais_bench', 'running': case != 'stopped_latest'})
                    if any('connect_ex' in str(a) for a in args):
                        probes[0]+=1
                        return 'False' if case == 'stopped_latest' or case.startswith('old_failure') and probes[0]>1 else 'True'
                    return ''
                status = {'status': {'source_sha256': 'runner-source', 'activity': 'running' if case == 'old_busy' else 'idle', 'process_tree_confirmed_stopped': True}}
                namespace = {'json': json, 'Path': Path, 'command': command, 'container_info': lambda spec: info, 'read_status': lambda spec, **kwargs: status, 'main': install, 'time': time, 'remote_deployment_lock':lambda spec:nullcontext()}
                exec(compile(source, 'direct_worker.py', 'exec'), namespace)
                payload = {'spec': self.spec, 'release': {'id': 'release-id', 'files': {}}, 'runner_sha256': 'runner-source', 'archive': '/mnt/share/test/kit.tar.gz', 'sha256': 'archive-sha'}
                with redirect_stdout(io.StringIO()):
                    if case.startswith('old_failure'):
                        with self.assertRaisesRegex(RuntimeError,'setup failed'):namespace['direct_main'](payload)
                    elif case == 'old_busy':
                        with self.assertRaisesRegex(ValueError, '非空闲'):
                            namespace['direct_main'](payload)
                    else:
                        namespace['direct_main'](payload)
                if case == 'stopped_latest':
                    self.assertIn(('docker', 'start', self.spec['container']), calls)
                    self.assertTrue(any(args[:3] == ('docker', 'exec', '-d') for args in calls))
                if case.startswith('old_failure'):
                    self.assertEqual(any(args[:3]==('docker','exec','-d') for args in calls),case=='old_failure_unchanged')
                elif case == 'old_idle':
                    self.assertTrue(install.called)
                    self.assertTrue(install.call_args.args[0]['spec']['ais_path'])
                else:
                    install.assert_not_called()

    def test_remove_runner_retains_files_and_record_for_redeployment(self):
        runner = self.app.runners()[0]
        rid = runner['id']
        self.app.store.claim(rid, 'test:8000', 'pending')
        with self.assertRaisesRegex(ValueError, '占用'):
            self.app.remove_runner(rid)
        self.app.store.release(rid, 'pending')
        self.assertTrue(self.app.remove_runner(rid)['ok'])
        self.assertEqual(self.app.runners(), [])
        self.assertEqual(self.app.store.get('removed-runner', rid)['spec'], runner['spec'])
        with patch.object(self.app.executor, 'submit'):
            task = self.app.deploy(self.spec)
        self.assertEqual(task['runner_id'], rid)

    def test_late_refresh_does_not_restore_removed_runner(self):
        runner = self.app.runners()[0]
        self.app.demo = False
        def status(spec):
            self.app.remove_runner(runner['id'])
            return {'status': runner['status'], 'token': ''}
        with patch.object(self.app.transport, 'status', side_effect=status):
            with self.assertRaises(ValueError):
                self.app.refresh(runner['id'])
        self.assertEqual(self.app.runners(), [])
        self.assertEqual(self.app.store.get('removed-runner', runner['id'])['id'], runner['id'])

    def test_user_view_shows_container_and_only_active_target_fields(self):
        runner = self.app.runners()[0]
        runner['status']['activity'] = 'running'
        runner['status']['active_job'] = {'id': 'internal', 'target': {'host': '192.0.2.10', 'port': 8000, 'model': 'test-model', 'private': 'must-not-leak'}}
        self.app.store.save('runner', runner)
        view = self.app.user_view()['runners'][0]
        self.assertEqual(view['spec']['container'], 'ascend-test')
        self.assertEqual(view['status']['active_job']['target'], {'host': '192.0.2.10', 'port': 8000, 'model': 'test-model'})
        self.assertNotIn('must-not-leak', json.dumps(view))

    def test_user_view_keeps_unavailable_runners(self):
        runner = self.app.store.list('runner')[0]
        runner['error'] = 'private diagnostic details'
        self.app.store.save('runner', runner)
        view = self.app.user_view()['runners']
        self.assertEqual(len(view), 1)
        self.assertEqual(view[0]['id'], runner['id'])
        self.assertTrue(view[0]['error'])
        self.assertNotIn('private diagnostic details', json.dumps(view))

    def test_user_list_matches_admin_without_failed_history(self):
        runner = self.app.runners()[0]
        self.app.remove_runner(runner['id'])
        self.app.store.save('deployment', {'id': 'old', 'runner_id': runner['id'], 'spec': runner['spec'], 'state': 'failed', 'created_at': 1})
        self.assertEqual(self.app.user_view()['runners'], [])
        task = {'id': 'new', 'runner_id': 'a' * 16, 'spec': {**runner['spec'], 'container': 'replacement'}, 'state': 'failed', 'created_at': time.time()}
        self.app.store.save('deployment', task)
        self.assertEqual(self.app.user_view()['runners'], [])
        self.assertEqual(self.app.runners(), [])

    def test_install_network_rejects_credentials_and_accepts_intranet(self):
        network = {'proxy': 'http://proxy.example:3128', 'pip_index': 'http://mirror.example/simple', 'extra_hosts': '192.0.2.10 mirror.example'}
        self.assertEqual(validate_install_network(network), network)
        for body in ({'proxy': 'http://user:secret@proxy.example:3128'}, {'pip_index': 'https://mirror.example/simple?token=secret'}, {'proxy': 'file:///etc/passwd'}, {'extra_hosts': '192.0.2.10 mirror.example;touch /tmp/x'}, {'proxy': 'http://proxy.example:99999'}, {'proxy': 'http://proxy.example\n'}, {'unknown': 'value'}):
            # Leading/trailing whitespace is intentionally trimmed in form fields.
            if body.get('proxy', '').endswith('\n'):
                body['proxy'] = 'http://proxy.exa\nmple'
            with self.subTest(body=body), self.assertRaises(ValueError):
                validate_install_network(body)

    def test_remote_setup_gets_explicit_network_and_managed_mode(self):
        source = Path(__file__).parent / 'remote_helper.py'
        spec = importlib.util.spec_from_file_location('trial_install_network', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        archive = Path(self.tmp.name) / 'kit.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            data = b'#!/bin/bash\n'
            member = tarfile.TarInfo('ascend-bench-kit/setup.sh')
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
        network = {'proxy': 'http://proxy.example:3128', 'pip_index': 'http://mirror.example/simple', 'extra_hosts': '192.0.2.10 mirror.example'}
        with patch.object(module, 'container_info'), patch.object(module, 'command', return_value='False'), patch.object(module, 'read_status'), patch.object(module.subprocess, 'run') as setup, redirect_stdout(io.StringIO()):
            module.main({'action': 'deploy', 'spec': self.spec, 'archive': str(archive), 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'network': validate_install_network(network), '_deployment_lock_fd':43})
        env = setup.call_args.kwargs['env']
        self.assertEqual(setup.call_args.kwargs['pass_fds'], (43,))
        self.assertEqual(env['ASCEND_BENCH_MANAGED'], '1')
        self.assertEqual(env['ASCEND_BENCH_PROXY'], network['proxy'])
        self.assertEqual(env['ASCEND_BENCH_PIP_INDEX'], network['pip_index'])
        self.assertEqual(env['ASCEND_BENCH_EXTRA_HOSTS'], network['extra_hosts'])

    def test_startup_status_failure_is_captured_instead_of_streaming_traceback(self):
        source = Path(__file__).parent / 'remote_helper.py'
        spec = importlib.util.spec_from_file_location('trial_startup_status', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        error = subprocess.CalledProcessError(1, ['docker', 'exec'], stderr=b'Traceback (most recent call last):\n  child internals\nurllib.error.URLError: Connection refused\n')
        with patch.object(module, 'container_info'), patch.object(module, 'command', return_value='private-runner-token'), patch.object(module.subprocess, 'check_output', side_effect=error) as child:
            with self.assertRaises(ValueError) as raised:
                module.read_status(self.spec)
            self.assertIn('Connection refused', str(raised.exception))
            self.assertNotIn('Traceback', str(raised.exception))
            self.assertNotIn('private-runner-token', str(raised.exception))
            self.assertEqual(child.call_args.kwargs['stderr'], subprocess.PIPE)

    def test_retired_container_requires_confirmed_docker_state(self):
        source=Path(__file__).parent/'remote_helper.py'
        spec=importlib.util.spec_from_file_location('trial_retired_container',source)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for state,pid_mode,expected in [({'Running':False,'Restarting':False,'Pid':0},'',True),
                                        ({'Running':True,'Restarting':False,'Pid':123},'',False),
                                        ({'Running':False,'Restarting':True,'Pid':0},'',False),
                                        ({'Running':False,'Restarting':False,'Pid':0},'host',False),
                                        ({},'',False)]:
            info={'State':state,'HostConfig':{'PidMode':pid_mode}}
            with patch.object(module.subprocess,'run',return_value=types.SimpleNamespace(returncode=0,stdout=json.dumps([info]).encode())):
                self.assertEqual(module.retired_container(self.spec)['retired'],expected)
        with patch.object(module.subprocess,'run',return_value=types.SimpleNamespace(returncode=1)), patch.object(module,'command',return_value='another-container'):
            self.assertTrue(module.retired_container(self.spec)['retired'])
        with patch.object(module.subprocess,'run',return_value=types.SimpleNamespace(returncode=1)), patch.object(module,'command',side_effect=RuntimeError('Docker unavailable')):
            with self.assertRaises(RuntimeError):
                module.retired_container(self.spec)

    def test_runner_status_bypasses_container_proxy_environment(self):
        source = Path(__file__).parent / 'remote_helper.py'
        spec = importlib.util.spec_from_file_location('trial_loopback_status', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        status = {'root': self.spec['base'], 'container': self.spec['container']}
        from http.server import BaseHTTPRequestHandler
        class StatusHandler(BaseHTTPRequestHandler):
            def do_GET(handler):
                handler.send_response(200)
                handler.end_headers()
                handler.wfile.write(json.dumps(status).encode())
            def log_message(handler, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), StatusHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original_check_output = subprocess.check_output
        def run_container_python(*args, **kwargs):
            import os
            env = {**os.environ, 'http_proxy': 'http://127.0.0.1:1',
                   'HTTP_PROXY': 'http://127.0.0.1:1', 'no_proxy': '', 'NO_PROXY': ''}
            return original_check_output([sys.executable, '-I', '-c', kwargs['input'].decode()], env=env, timeout=5)
        try:
            with patch.object(module, 'container_info'), patch.object(module, 'command', return_value='test-token'), patch.object(module.subprocess, 'check_output', side_effect=run_container_python):
                result = module.read_status({**self.spec, 'port': server.server_port})
            self.assertEqual(result['status'], status)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_container_migration_requires_verified_socket_owner(self):
        source = Path(__file__).parent / 'remote_helper.py'
        spec = importlib.util.spec_from_file_location('trial_remote', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        status = {'root': self.spec['base'], 'container': 'old-container', 'activity': 'idle'}
        with patch.object(module, 'container_info'), patch.object(module.subprocess, 'check_output', return_value=json.dumps(status).encode()), patch.object(module, 'command', return_value='test-token') as command:
            with self.assertRaisesRegex(ValueError, '容器身份不匹配'):
                module.read_status(self.spec)
            self.assertTrue(module.read_status(self.spec, allow_legacy_container=True)['legacy_container'])
            self.assertIn("(proc/'fd').iterdir()", command.call_args.args[5])
            command.side_effect = lambda *args: 'test-token' if args[3]=='cat' else (_ for _ in ()).throw(RuntimeError('not socket owner'))
            with self.assertRaisesRegex(ValueError, '未停止或修改'):
                module.read_status(self.spec, allow_legacy_container=True)

    def test_reused_directory_prefers_current_container_and_keeps_service(self):
        import os
        source = Path(__file__).parent / 'runner-overlay/scripts/runner.py'
        spec = importlib.util.spec_from_file_location('trial_config', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.BASE = Path(self.tmp.name)
        module.CONFIG_PATH = module.BASE / 'runner-config.json'
        saved = {'container': 'old-container', 'service': {'host': '192.0.2.10', 'port': 8001, 'model': 'kept-model'}, 'ais_bench_path': '/kept/ais_bench', 'gsm8k_path': '/kept/data'}
        module.CONFIG_PATH.write_text(json.dumps(saved))
        with patch.dict(os.environ, {'ASCEND_BENCH_CONTAINER': 'new-container'}), patch.object(module, 'detect_lan_ip', return_value='127.0.0.1'):
            cfg = module.load_config()
            self.assertEqual(cfg['container'], 'new-container')
            self.assertEqual(cfg['service']['model'], 'kept-model')
            self.assertEqual(cfg['ais_bench_path'], '/kept/ais_bench')
            with patch.object(module.Path, 'is_file', return_value=True), patch.object(module.os, 'access', return_value=True), patch.object(module, 'ais_launch_problem', return_value=''), patch.object(module, 'check_cli_flags'):
                self.assertEqual(module.validate_config({**cfg, 'container': 'old-container'})['container'], 'new-container')
        module.CONFIG_PATH.write_text(json.dumps(saved))
        with patch.dict(os.environ, {'ASCEND_BENCH_CONTAINER': ''}), patch.object(module, 'detect_lan_ip', return_value='127.0.0.1'):
            self.assertEqual(module.load_config()['container'], 'old-container')

    def test_personal_ssh_profile_is_ignored_before_file_access(self):
        config = Path(self.tmp.name) / 'legacy.json'
        config.write_text(json.dumps({'profiles': [{'id': 'old', 'identity_file': '/unavailable/personal-key', 'known_hosts_file': '/unavailable/personal-hosts'}]}))
        self.assertEqual(load_profiles(config, tool_only=True), [])

    def test_bundle_has_public_key_but_never_private_key(self):
        spec = bootstrap.validate({'name': '测试机器', 'host': '192.0.2.25', 'user': 'benchops',
                                   'container': 'test', 'base': '/home/benchops/bench', 'port': 18787})
        directory = Path(self.tmp.name) / 'generated'
        entry = bootstrap.generate(directory, self.app.archive, spec)
        private = Path(entry['key_path']).read_bytes()
        with tarfile.open(entry['archive']) as archive:
            names = archive.getnames()
            self.assertFalse(any(name.endswith('identity') or 'known_hosts' in name for name in names))
            for name in ('manifest.json', 'gateway.py', 'install.py', 'remote_helper.py'):
                raw = archive.extractfile('ascend-bench-install/' + name).read()
                self.assertNotIn(private, raw)
            manifest = json.load(archive.extractfile('ascend-bench-install/manifest.json'))
            self.assertTrue(manifest['public_key'].startswith('ssh-ed25519 '))
        self.app.demo = False
        self.app.profiles = []
        self.app.store.save('enrollment', entry)
        receipt = {'id': entry['id'], 'host': spec['host'], 'user': spec['user'], 'host_keys': [manifest['public_key']]}
        line = bootstrap.known_hosts(receipt, spec)
        self.assertTrue(line.startswith('192.0.2.25 ssh-ed25519 '))
        with self.assertRaises(ValueError):
            bootstrap.known_hosts({**receipt, 'host': 'other'}, spec)
        self.app.transport.status = lambda request: {'token': '', 'status': {'root': spec['base'], 'container': spec['container'], 'activity': 'idle'}}
        result = self.app.import_receipt(receipt)
        self.assertTrue(result['runner_id'])
        self.assertEqual(self.app.profiles[-1]['mode'], 'restricted')
        self.assertEqual(self.app.store.get('enrollment', entry['id'])['state'], 'connected')
        with self.assertRaises(ValueError):
            self.app.import_receipt(receipt)

    def test_gateway_accepts_cross_host_services_and_rejects_invalid_requests(self):
        spec = {'service_hosts': ['192.0.2.25'], 'service_ports': [8000], 'tokenizer_roots': ['/models']}
        self.assertEqual(validate_request({'method': 'GET', 'path': '/api/status'}, spec)[0], 'GET')
        self.assertEqual(validate_request({'method': 'GET', 'path': '/api/jobs'}, spec), ('GET', '/api/jobs', None))
        for host, port in [('192.0.2.99', 31050), ('model.internal', 31051)]:
            service = {'host': host, 'port': port, 'tokenizer_path': '/models/example'}
            self.assertEqual(validate_request({'method': 'POST', 'path': '/api/check-service', 'body': {'service': service}}, spec)[2]['service'], service)
            self.assertEqual(validate_request({'method': 'POST', 'path': '/api/jobs', 'body': service}, spec)[2], service)
        for request in [
            {'method': 'POST', 'path': '/api/config', 'body': {}},
            {'method': 'GET', 'path': '/etc/passwd'},
            {'method': 'POST', 'path': '/api/check-service', 'body': {'service': {'host': 'http://model.internal/v1', 'port': 80, 'tokenizer_path': '/models/example'}}},
            {'method': 'POST', 'path': '/api/check-service', 'body': {'service': {'host': '192.0.2.99', 'port': 0, 'tokenizer_path': '/models/example'}}},
            {'method': 'POST', 'path': '/api/jobs', 'body': {'host': '192.0.2.25', 'port': 8000, 'tokenizer_path': '/models/../etc', 'execution': {}}},
        ]:
            with self.subTest(request=request), self.assertRaises(ValueError):
                validate_request(request, spec)

    def test_center_allows_remote_model_host_and_nondefault_port(self):
        self.app.demo = False
        row = self.app.runners()[0]
        profile = self.app.transport.profiles[row['spec']['profile_id']]
        profile.update(mode='restricted', service_hosts=['192.0.2.10'], service_ports=[8000], tokenizer_roots=['/models'])
        service = {'host': 'model.remote.internal', 'port': 31050, 'tokenizer_path': '/models/example'}
        with patch.object(self.app.transport, 'request', return_value=(200, 'application/json', b'{}')) as remote:
            self.app.proxy(row['id'], 'POST', '/api/check-service', {'service': service})
            self.assertEqual(remote.call_args.args[3]['service'], service)
            body = {**service}
            self.app.proxy(row['id'], 'POST', '/api/jobs', body)
            self.assertEqual(self.app.store.request_for(row['id']), body['fleet_request_id'])
            self.assertEqual(remote.call_args.args[3]['host'], 'model.remote.internal')
            with self.assertRaises(ValueError):
                self.app.proxy(row['id'], 'POST', '/api/check-service', {'service': {**service, 'host': 'http://invalid/path'}})

    def test_ssh_ignores_personal_config_and_agent(self):
        options = Transport([]).options({'identity_file': 'tool/identity', 'known_hosts_file': 'tool/known_hosts'})
        self.assertEqual(options[:2], ['-F', 'none'])
        self.assertIn('IdentityAgent=none', options)
        self.assertIn('GlobalKnownHostsFile=none', options)

    def test_installer_preserves_authorizations_and_revokes_only_its_key(self):
        import hashlib
        target = Path(self.tmp.name) / 'server-bundle'
        target.mkdir()
        home = Path(self.tmp.name) / 'server-home'
        (home / '.ssh').mkdir(parents=True)
        authorized = home / '.ssh/authorized_keys'
        original = '# original admin key\nssh-ed25519 original-public-key admin\n'
        authorized.write_text(original)
        (target / 'runner-kit.tar.gz').write_bytes(b'test kit')
        (target / 'gateway.py').write_text('# mock gateway')
        manifest = {'id': 'b' * 32, 'spec': {'user': 'benchops', 'container': 'test', 'base': '/home/benchops/bench', 'host': '192.0.2.25'},
                    'public_key': 'ssh-ed25519 ' + 'A' * 40, 'kit_sha256': hashlib.sha256(b'test kit').hexdigest(), 'expires_at': time.time() + 60}
        (target / 'manifest.json').write_text(json.dumps(manifest))
        fake_fcntl = types.SimpleNamespace(flock=lambda *a: None, LOCK_EX=1, LOCK_NB=2)
        fake_helper = types.SimpleNamespace(main=lambda payload: None)
        source = Path(__file__).parent / 'install_bundle.py'
        module_spec = importlib.util.spec_from_file_location('server_installer', source)
        module = importlib.util.module_from_spec(module_spec)
        with patch.dict(sys.modules, {'fcntl': fake_fcntl, 'remote_helper': fake_helper}):
            module_spec.loader.exec_module(module)
            module.__file__ = str(target / 'install.py')
            with patch.object(Path, 'home', return_value=home), patch.object(module.getpass, 'getuser', return_value='benchops'), patch.object(module, 'host_public_keys', return_value=['ssh-ed25519 ' + 'A' * 40]), patch('sys.stdout', new_callable=io.StringIO):
                # os.umask has process scope, restore it after exercising the installer.
                old_mask = module.os.umask(0o077)
                try:
                    module.main()
                    module.main()
                finally:
                    module.os.umask(old_mask)
                lines = authorized.read_text().splitlines()
                self.assertEqual(len([line for line in lines if line.startswith('restrict,command=')]), 1)
                self.assertTrue(authorized.read_text().startswith(original))
                receipt = json.loads((target / ('receipt-' + manifest['id'] + '.json')).read_text())
                self.assertEqual(set(receipt), {'id', 'host', 'user', 'host_keys'})
                state = home / '.local/share/ascend-bench-access' / manifest['id']
                exec(compile((state / 'revoke.py').read_text(encoding='utf-8'), 'revoke.py', 'exec'), {})
                self.assertEqual(authorized.read_text(), original)

    def test_atomic_target_claim_and_rollback(self):
        self.app.store.claim('r1', 'service:8000', 'a')
        with self.assertRaises(ValueError):
            self.app.store.claim('r2', 'service:8000', 'b')
        self.assertIsNone(self.app.store.request_for('r2'))
        self.app.store.claim('r2', 'other:8000', 'b')
        self.app.store.release('r1', 'old-request')
        self.assertEqual(self.app.store.request_for('r1'), 'a')

    def test_concurrent_target_claims(self):
        def attempt(n):
            try:
                self.app.store.claim('r' + str(n), 'shared:8000', str(n))
                return True
            except ValueError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt, range(20))), 1)

    def test_no_credentials_in_public_runners(self):
        row = self.app.store.list('runner')[0]
        row['token'] = 'sensitive-value'
        self.app.store.save('runner', row)
        self.assertNotIn('sensitive-value', json.dumps(self.app.runners()))

    def test_restart_keeps_unknown_deployment(self):
        task = {'id': 'task', 'state': 'installing', 'detail': '', 'spec': self.spec}
        self.app.store.save('deployment', task)
        self.app.store.claim('r1', 'service:8000', 'pending')
        other = Store(Path(self.tmp.name) / 'data')
        self.assertEqual(other.get('deployment', 'task')['state'], 'unknown')
        self.assertEqual(other.request_for('r1'), 'pending')
        other.db.close()

    def test_demo_deployment_registers_runner(self):
        self.app.profiles[0]['base_roots'].append('/home/benchops/ascend-second')
        second = {**self.spec,'base':'/home/benchops/ascend-second','port':18788}
        task = self.app.deploy(second)
        with self.assertRaises(ValueError):
            self.app.deploy(second)
        self.app.executor.shutdown(wait=True)
        self.assertEqual(self.app.store.get('deployment', task['id'])['state'], 'ready')
        self.assertEqual(len(self.app.runners()), 2)

    def test_timeout_never_auto_releases(self):
        row = self.app.store.list('runner')[0]
        row['status']['instance_id'] = 'old-instance'
        self.app.store.save('runner', row)
        self.app.store.claim(row['id'], 'target', 'pending')
        self.app.demo = False
        self.app.transport.status = lambda spec: {'token': 'secret', 'status': {'instance_id': 'new-instance', 'activity': 'idle', 'process_tree_confirmed_stopped': True, 'fleet_requests': {'pending': {'state': 'finished'}}}}
        self.app.refresh(row['id'])
        self.assertEqual(self.app.store.request_for(row['id']), 'pending')

    def test_exact_completed_request_releases(self):
        row = self.app.store.list('runner')[0]
        row['status']['instance_id'] = 'instance'
        self.app.store.save('runner', row)
        self.app.store.claim(row['id'], 'target', 'pending')
        self.app.demo = False
        self.app.transport.status = lambda spec: {'token': 'secret', 'status': {'instance_id': 'instance', 'activity': 'idle', 'process_tree_confirmed_stopped': True, 'fleet_requests': {'pending': {'state': 'finished'}}}}
        self.app.refresh(row['id'])
        self.assertIsNone(self.app.store.request_for(row['id']))

    def test_terminal_receipt_restored_after_idle_restart_releases_exact_claim(self):
        row=self.app.store.list('runner')[0]
        row['status']['instance_id']='old';self.app.store.save('runner',row)
        self.app.store.claim(row['id'],'target','pending');self.app.demo=False
        proof={'state':'finished','job_id':'real-proof','restored_after_restart':True,'process_tree_confirmed_stopped':True,
               'terminal_status':'stopped','completed_at':'2026-11-05T09:00:00Z'}
        self.app.transport.status=lambda spec:{'token':'secret','status':{'instance_id':'new','activity':'idle',
            'process_tree_confirmed_stopped':True,'fleet_requests':{'pending':proof}}}
        self.app.refresh(row['id'])
        self.assertIsNone(self.app.store.request_for(row['id']))

    def test_http_auth_csrf_and_host(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.app = self.app
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f'http://127.0.0.1:{server.server_port}'
        browser = build_opener(ProxyHandler({}), HTTPCookieProcessor())
        def request(path, body=None, **headers):
            if body is not None:
                headers['Content-Type'] = 'application/json'
            return browser.open(Request(root + path, data=None if body is None else json.dumps(body).encode(), headers=headers))
        try:
            with self.assertRaises(HTTPError) as err:
                request('/api/bootstrap')
            self.assertEqual(err.exception.code, 401)
            with self.assertRaises(HTTPError) as err:
                request('/api/login', {'token': '输入了中文路径/凭据'})
            self.assertEqual(err.exception.code, 403)
            self.assertIn('文件中的完整字符串', json.loads(err.exception.read())['error'])
            with request('/api/login', {'token': 'demo'}) as r:
                csrf = json.load(r)['csrf']
            with request('/api/bootstrap') as r:
                self.assertNotIn('token', json.dumps(json.load(r)['runners']))
            with self.assertRaises(HTTPError) as err:
                request('/api/refresh', {'runner_id': 'x'})
            self.assertEqual(err.exception.code, 403)
            with self.assertRaises(HTTPError) as err:
                request('/api/refresh', {}, **{'X-CSRF-Token': csrf, 'Origin': 'http://evil.example'})
            self.assertEqual(err.exception.code, 403)
            with self.assertRaises(HTTPError) as err:
                request('/api/bootstrap', Host='evil.example')
            self.assertEqual(err.exception.code, 403)
            rid = self.app.runners()[0]['id']
            with request('/r/' + rid + '/') as r:
                page = r.read().decode()
                self.assertIn('/r/' + rid + '/runner-client.js', page)
            with request('/r/' + rid + '/style.css') as r:
                self.assertIn('text/css', r.headers['Content-Type'])
            with request('/workbench/') as r:
                self.assertIn('fleetPicker', r.read().decode())
            with request('/workbench/style.css') as r:
                self.assertIn('text/css', r.headers['Content-Type'])
            self.app.demo = False
            with request('/api/enrollments', {'name': 'HTTP 接入测试', 'host': '192.0.2.50', 'user': 'benchops', 'container': 'test', 'base': '/home/benchops/bench', 'port': 18787}, **{'X-CSRF-Token': csrf}) as response:
                self.assertEqual(response.status, 201)
                enrollment = json.load(response)
                self.assertNotIn('key_path', enrollment)
            with request('/api/enrollments/' + enrollment['id'] + '/download') as response:
                self.assertIn('install-bundle.tar.gz', response.headers['Content-Disposition'])
                with tarfile.open(fileobj=io.BytesIO(response.read())) as archive:
                    self.assertIn('ascend-bench-install/install.sh', archive.getnames())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_streaming_executor_logs_before_exit(self):
        logs = []
        result = Transport([]).execute([sys.executable, '-u', '-c', 'print("step one"); print("step two")'], log=logs.append)
        self.assertIn('step one', result)
        self.assertEqual(logs, ['step one', 'step two'])

    def test_build_contains_route_and_status_patch(self):
        archive = Path(__file__).resolve().parent.parent / 'dist/ascend-bench-fleet-kit.tar.gz'
        with tarfile.open(archive) as kit:
            runner = kit.extractfile('ascend-bench-kit/scripts/runner.py').read().decode()
            client = kit.extractfile('ascend-bench-kit/scripts/runner-client.js').read().decode()
            self.assertIn("path == '/api/status'", runner)
            self.assertIn('fleetPrefix+path', client)
        self.assertIn("trust_remote_code=bool(c.get('api', {}).get('trust_remote_code'))", runner)

    def test_legacy_recovery_refuses_to_stop_runner_with_live_task_children(self):
        source = Path(__file__).parent / 'remote_helper.py'
        spec = importlib.util.spec_from_file_location('trial_safe_recovery', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        base = Path(self.tmp.name) / 'runner'
        folder = base/'results/jobs/real-test'
        folder.mkdir(parents=True)
        record = {'id': 'real-test', 'fleet_request_id': 'request', 'status': 'unknown', 'exit_code': 0,
                  'metrics': {'accuracy': 93.75}, 'completed_at': 'now', 'pid': 218,
                  'error': '任务进程组未确认退出，请管理员核对'}
        (folder/'record.json').write_text(json.dumps(record))
        proc = Path(self.tmp.name)/'proc'
        (proc/'219').mkdir(parents=True)
        (proc/'219/stat').write_text('219 (child) S 1 218 218 0')
        current = {'instance_id': 'instance', 'active_job': {'id': 'real-test'}, 'source_sha256': 'hash',
                   'fleet_requests': {'request': {'state': 'unknown'}}}
        def execute_proof(args, **kwargs):
            proof = args[6].replace("Path('/proc')", 'Path(' + repr(str(proc)) + ')')
            with patch.object(sys, 'argv', ['proof', str(base), 'real-test', 'request', 'hash']):
                exec(compile(proof, 'recovery-proof', 'exec'), {})
        with patch.object(module, 'read_status', return_value={'status': current}), patch.object(module.subprocess, 'check_output', side_effect=execute_proof), patch.object(module.os, 'kill', create=True) as kill, patch.object(module, 'command') as command:
            with self.assertRaisesRegex(AssertionError, '仍有活跃进程'):
                module.recover_completed_job({**self.spec, 'base': str(base)}, 'request', 'instance', 'source')
            kill.assert_not_called()
            command.assert_not_called()
        self.assertEqual(json.loads((folder/'record.json').read_text())['status'], 'unknown')

    def test_runner_distinguishes_zombie_groups_from_live_or_unreadable_groups(self):
        source = Path(__file__).parent / 'runner-overlay/scripts/runner.py'
        spec = importlib.util.spec_from_file_location('trial_process_groups', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        root = Path(self.tmp.name) / 'proc'
        root.mkdir()
        member = root / '219'
        member.mkdir()
        stat = member / 'stat'
        with patch.object(module.os, 'killpg', create=True):
            stat.write_text('219 (worker (name)) Z 1 218 218 0')
            self.assertFalse(module.process_group_running(218, root))
            stat.write_text('219 (worker) S 1 218 218 0')
            self.assertTrue(module.process_group_running(218, root))
            stat.write_text('219 (worker) D 1 218 218 0')
            self.assertTrue(module.process_group_running(218, root))
            stat.write_text('malformed')
            self.assertIsNone(module.process_group_running(218, root))
            self.assertIsNone(module.process_group_running(218, root / 'missing'))
        with patch.object(module.os, 'killpg', side_effect=ProcessLookupError, create=True):
            self.assertFalse(module.process_group_running(218, root))

    def test_runner_completion_rechecks_after_kill_and_only_releases_confirmed_tasks(self):
        source = Path(__file__).parent / 'runner-overlay/scripts/runner.py'
        for checks, idle in [([True], True), ([False, True], True), ([False, False], False)]:
            with self.subTest(checks=checks):
                spec = importlib.util.spec_from_file_location('trial_cleanup', source)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.BASE = Path(self.tmp.name)
                module.RUNS = module.BASE / 'jobs'
                module.CONFIG = {'service': {'host': '127.0.0.1'}, 'ais_bench_path': '/fake/ais_bench'}
                record = {'id': 'real-test', 'status': 'running', 'count': 1, 'fleet_request_id': 'test-request',
                          'config': {'mode': 'accuracy', 'dataset': {'name': 'GSM8K'}, 'execution': {'mode': 'all'}, 'batch_size': 1, 'max_out_len': 1024}}
                folder = module.RUNS / record['id']
                folder.mkdir(parents=True, exist_ok=True)
                module.ACTIVE_JOB = record
                module.LOCK.acquire()
                process = unittest.mock.MagicMock(pid=218)
                process.wait.return_value = 0
                with patch.object(module, 'prepare_dataset', return_value={'path': '/data'}), patch.object(module, 'make_config', return_value='config'), patch.object(module, 'collect'), patch.object(module, 'ais_launch_problem', return_value=''), patch.object(module, 'check_cli_flags'), patch.object(module.subprocess, 'Popen', return_value=process), patch.object(module.os, 'killpg', create=True), patch.object(module.signal, 'SIGKILL', 9, create=True), patch.object(module, 'wait_process_group_stopped', side_effect=checks):
                    module.run_job(record)
                self.assertEqual(module.SAFE_IDLE, idle)
                self.assertEqual(module.LOCK.locked(), not idle)
                self.assertEqual(record['status'], 'completed' if idle else 'unknown')
                self.assertEqual(module.FLEET_REQUESTS['test-request']['state'], 'finished' if idle else 'unknown')

    def test_status_refresh_automatically_reconciles_finished_zombie_only_task(self):
        source = Path(__file__).parent / 'runner-overlay/scripts/runner.py'
        spec = importlib.util.spec_from_file_location('trial_auto_reconcile', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.RUNS = Path(self.tmp.name) / 'jobs'
        record = {'id': 'real-finished', 'status': 'unknown', 'terminal_status': 'completed',
                  'terminal_error': None, 'completed_at': '2026-11-04T13:16:28Z', 'pid': 218,
                  'fleet_request_id': 'request', 'metrics': {'accuracy': 93.75}}
        module.ACTIVE_JOB = record
        module.SAFE_IDLE = False
        module.LOCK.acquire()
        with patch.object(module, 'process_group_running', return_value=True):
            module.reconcile_finished_task()
        self.assertTrue(module.LOCK.locked())
        with patch.object(module, 'process_group_running', return_value=None):
            module.reconcile_finished_task()
        self.assertTrue(module.LOCK.locked())
        with patch.object(module, 'process_group_running', return_value=False):
            with concurrent.futures.ThreadPoolExecutor(8) as pool:
                list(pool.map(lambda _: module.reconcile_finished_task(), range(8)))
        self.assertFalse(module.LOCK.locked())
        self.assertTrue(module.SAFE_IDLE)
        self.assertIsNone(module.ACTIVE_JOB)
        self.assertEqual(module.FLEET_REQUESTS['request']['state'], 'finished')
        restored = json.loads((module.RUNS/'real-finished/record.json').read_text())
        self.assertEqual(restored['status'], 'completed')
        self.assertEqual(restored['metrics'], record['metrics'])

    def test_ais_entrypoint_with_missing_shebang_interpreter_is_not_usable(self):
        source=Path(__file__).parent/'runner-overlay/scripts/runner.py'
        spec=importlib.util.spec_from_file_location('trial_ais_entrypoint',source)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        entry=Path(self.tmp.name)/'ais_bench'
        entry.write_text('#!/missing/old-container/python\n')
        entry.chmod(0o755)
        self.assertIn('启动解释器不可用',module.ais_launch_problem(str(entry)))
        entry.write_text('#!'+Path(sys.executable).as_posix()+'\n')
        self.assertEqual(module.ais_launch_problem(str(entry)),'')
        self.assertIn('不存在',module.ais_launch_problem(str(entry.parent/'missing')))

    def test_deployment_probes_ais_execution_instead_of_only_file_presence(self):
        import ast
        source=(Path(__file__).parent/'direct_worker.py').read_text(encoding='utf-8')
        assignment=next(n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='probe_code' for t in n.targets))
        code=assignment.value.value
        base=Path(self.tmp.name)
        entry=base/'ais_bench'
        entry.write_text('#!/missing/python\n')
        entry.chmod(0o755)
        (base/'runner-config.json').write_text(json.dumps({'ais_bench_path':str(entry)}))
        for failure in (True,False):
            output=io.StringIO()
            with patch.object(sys,'argv',['probe',str(base),'{}','test-container-id','test-container']), patch.object(subprocess,'run',side_effect=FileNotFoundError if failure else None,return_value=types.SimpleNamespace(returncode=0)) as run, redirect_stdout(output):
                exec(compile(code,'deployment-probe','exec'),{})
            result=json.loads(output.getvalue())
            self.assertEqual(result['ais_path'],'' if failure else str(entry))
            self.assertEqual(run.call_args.args[0],[str(entry),'--help'])

    def test_tokenizer_import_disables_device_autoload_before_loading_transformers(self):
        import os
        source=Path(__file__).parent/'runner-overlay/scripts/runner.py'
        spec=importlib.util.spec_from_file_location('trial_tokenizer_environment',source)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.CONFIG={'service':{'tokenizer_path':'/models/tokenizer'}}
        class Loaded(Exception):pass
        def load(*args,**kwargs):
            self.assertEqual(os.environ['TORCH_DEVICE_BACKEND_AUTOLOAD'],'0')
            raise Loaded()
        fake=types.SimpleNamespace(AutoTokenizer=types.SimpleNamespace(from_pretrained=load))
        with patch.dict(os.environ,{'TORCH_DEVICE_BACKEND_AUTOLOAD':'1'}), patch.dict(sys.modules,{'transformers':fake}):
            with self.assertRaises(Loaded):
                module.generate_data({},Path(self.tmp.name)/'generated',1)

    def test_tokenizer_failure_log_preserves_underlying_import_cause_and_releases_runner(self):
        source=Path(__file__).parent/'runner-overlay/scripts/runner.py'
        spec=importlib.util.spec_from_file_location('trial_import_failure_log',source)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.RUNS=Path(self.tmp.name)/'jobs'
        module.BASE=Path(self.tmp.name).resolve()
        folder=module.RUNS/'real-import-failure'
        folder.mkdir(parents=True)
        record={'id':'real-import-failure','status':'running','count':1,'config':{'mode':'performance','execution':{'mode':'perf'}}}
        def fail(*args):
            try:raise OSError('underlying device backend import error')
            except OSError as cause:raise ModuleNotFoundError('Could not import AutoTokenizer') from cause
        module.ACTIVE_JOB=record
        module.LOCK.acquire()
        with patch.object(module,'get_perf_dataset',side_effect=fail):
            module.run_job(record)
        log=(folder/'execution.log').read_text()
        self.assertIn('underlying device backend import error',log)
        self.assertIn('Could not import AutoTokenizer',log)
        self.assertEqual(record['status'],'failed')
        self.assertFalse(module.LOCK.locked())

    def test_native_retry_is_unchanged_and_pressure_is_forwarded(self):
        import ast
        source=Path(__file__).parent/'runner-overlay/scripts/runner.py'
        spec=importlib.util.spec_from_file_location('trial_pressure_retry',source)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.BASE=Path(self.tmp.name)
        module.RUNS=module.BASE/'jobs'
        module.CONFIG={'service':{'host':'192.0.2.99','port':31050,'model':'test','tokenizer_path':'/models/test'},'ais_bench_path':'/fake/ais_bench'}
        config={'mode':'performance','engine':'aisbench',**module.CONFIG['service'],'max_out_len':1024,'batch_size':4,'retry':0,'stream':True,
                'generation_kwargs':{'temperature':0},'api':{'endpoint':'chat'},'workload':{'input_tokens':16384,'count':32},
                'sample_limit':32,'warmup':{'enabled':True,'count':2},'execution':{'mode':'perf','pressure':True,'pressure_time':15,'target':{'kind':'direct'}}}
        self.assertEqual(module.validate(config),32)
        folder=module.RUNS/'real-arguments'
        folder.mkdir(parents=True)
        for retry in (0,2):
            text=module.make_config({**config,'retry':retry},folder,'/data.jsonl')
            models=next(n for n in ast.parse(text).body if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and n.targets[0].id=='models')
            self.assertEqual(ast.literal_eval(models.value)[0]['retry'],retry)
            self.assertEqual(ast.literal_eval(models.value)[0]['batch_size'],4)
        with self.assertRaises(ValueError):module.validate({**config,'retry':-1})
        for invalid_timeout in (0,-1,float('nan'),float('inf'),True):
            with self.assertRaises(ValueError):module.validate({**config,'timeout':invalid_timeout})
        record={'id':'real-arguments','status':'running','count':32,'config':config}
        module.LOCK.acquire()
        process=unittest.mock.MagicMock(pid=218);process.wait.return_value=0
        with patch.object(module,'get_perf_dataset',return_value=(folder/'data.jsonl',False)),patch.object(module,'ais_launch_problem',return_value=''),patch.object(module,'check_cli_flags'),patch.object(module,'collect'),patch.object(module.subprocess,'Popen',return_value=process) as launch,patch.object(module.os,'killpg',create=True),patch.object(module,'wait_process_group_stopped',return_value=True):
            module.run_job(record)
        argv=launch.call_args.args[0]
        self.assertNotIn('--batch-size',argv)
        # This is the CLI shape reported by older installed AISBench versions.
        # It must accept the actual generated argv without a batch-size option;
        # concurrency is carried by the generated model configuration instead.
        import argparse
        legacy = argparse.ArgumentParser()
        legacy.add_argument('config')
        for flag in ('--mode','--num-prompts','--num-warmups','--work-dir','--pressure-time','--max-num-workers'):
            legacy.add_argument(flag)
        for flag in ('--dump-eval-details','--debug','--pressure'):
            legacy.add_argument(flag, action='store_true')
        parsed = legacy.parse_args(argv[1:])
        self.assertEqual(parsed.config,str(folder/'config.py'))
        self.assertIn('--pressure',argv)
        self.assertEqual(argv[argv.index('--pressure-time')+1],'15')
        self.assertEqual(argv[argv.index('--num-warmups')+1],'2')
        self.assertEqual(launch.call_args.kwargs['env']['AIS_BENCH_TOTAL_TIMEOUT'],'native')
        self.assertEqual(launch.call_args.kwargs['env']['ASCEND_BENCH_PROCESS_ROLE'],'aisbench')
        self.assertTrue(launch.call_args.kwargs['env']['PYTHONPATH'].startswith(str(module.BASE/'scripts')))
        self.assertEqual(launch.call_args.kwargs['cwd'],str(module.BASE))

    def test_runner_listener_does_not_wait_for_reverse_dns(self):
        source=Path(__file__).parent/'runner-overlay/scripts/runner.py'
        spec=importlib.util.spec_from_file_location('runner_listener_without_dns',source)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch('socket.getfqdn',side_effect=AssertionError('reverse DNS must not run')):
            server=module.RunnerHTTPServer(('127.0.0.1',0),module.Handler)
        try:
            self.assertEqual(server.server_name,'localhost')
            self.assertEqual(server.server_port,server.server_address[1])
        finally:server.server_close()

    def test_runner_success_banner_follows_listen_and_pid_record(self):
        source=(Path(__file__).parent/'runner-overlay/scripts/runner.py').read_text(encoding='utf-8')
        start=source.index("if __name__ == '__main__':")
        main=source[start:]
        self.assertLess(main.index('server = RunnerHTTPServer'),main.index('Runner 已启动'))
        self.assertLess(main.index("(BASE / 'runner.pid').write_text"),main.index('Runner 已启动'))

    def test_runner_status_records_explicit_rejection(self):
        source = Path(__file__).parent / 'runner-overlay/scripts/runner.py'
        spec = importlib.util.spec_from_file_location('trial_runner', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.BASE = Path(self.tmp.name)
        module.CONFIG = {'service': {'host': '127.0.0.1', 'port': 8000, 'model': 'test'}, 'container': 'test'}
        module.TOKEN = 'test-credential'
        server = ThreadingHTTPServer(('127.0.0.1', 0), module.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f'http://127.0.0.1:{server.server_port}'
        browser = build_opener(ProxyHandler({}))
        headers = {'Cookie': f'runner_session_{server.server_port}=test-credential', 'Content-Type': 'application/json'}
        try:
            request_id = 'a' * 32
            with patch.object(module.Handler, 'log_message', return_value=None):
                with self.assertRaises(HTTPError) as error:
                    browser.open(Request(root + '/api/jobs', data=json.dumps({'fleet_request_id': request_id, 'connection_id': 'bad'}).encode(), headers=headers))
                self.assertEqual(error.exception.code, 400)
                with browser.open(Request(root + '/api/status', headers=headers)) as response:
                    status = json.load(response)
                self.assertEqual(status['fleet_requests'][request_id]['state'], 'rejected')
                self.assertTrue(status['process_tree_confirmed_stopped'])
                self.assertEqual(status['activity'], 'idle')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
