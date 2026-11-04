import concurrent.futures
import io
import hashlib
from contextlib import redirect_stdout
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
                namespace = {'json': json, 'Path': Path, 'command': command, 'container_info': lambda spec: info, 'read_status': lambda spec, **kwargs: status, 'main': install, 'time': time}
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
            module.main({'action': 'deploy', 'spec': self.spec, 'archive': str(archive), 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(), 'network': validate_install_network(network)})
        env = setup.call_args.kwargs['env']
        self.assertEqual(env['ASCEND_BENCH_MANAGED'], '1')
        self.assertEqual(env['ASCEND_BENCH_PROXY'], network['proxy'])
        self.assertEqual(env['ASCEND_BENCH_PIP_INDEX'], network['pip_index'])
        self.assertEqual(env['ASCEND_BENCH_EXTRA_HOSTS'], network['extra_hosts'])

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
            with patch.object(module.Path, 'is_file', return_value=True), patch.object(module.os, 'access', return_value=True):
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

    def test_gateway_rejects_shell_config_and_arbitrary_services(self):
        spec = {'service_hosts': ['192.0.2.25'], 'service_ports': [8000], 'tokenizer_roots': ['/models']}
        self.assertEqual(validate_request({'method': 'GET', 'path': '/api/status'}, spec)[0], 'GET')
        self.assertEqual(validate_request({'method': 'GET', 'path': '/api/jobs'}, spec), ('GET', '/api/jobs', None))
        for request in [
            {'method': 'POST', 'path': '/api/config', 'body': {}},
            {'method': 'GET', 'path': '/etc/passwd'},
            {'method': 'POST', 'path': '/api/check-service', 'body': {'service': {'host': '169.254.169.254', 'port': 80}}},
            {'method': 'POST', 'path': '/api/jobs', 'body': {'host': '192.0.2.25', 'port': 8000, 'tokenizer_path': '/models/../etc', 'execution': {}}},
        ]:
            with self.subTest(request=request), self.assertRaises(ValueError):
                validate_request(request, spec)

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
        task = self.app.deploy({**self.spec, 'port': 18788})
        with self.assertRaises(ValueError):
            self.app.deploy({**self.spec, 'port': 18788})
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
            self.assertIn('trust_remote_code=False', runner)

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
