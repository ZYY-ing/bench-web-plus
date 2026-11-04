import base64
import concurrent.futures
import http.cookiejar
import importlib.util
import json
from pathlib import Path
import secrets
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from .app import Console, Handler
from .accounts import Accounts
from .certificates import issue
from .http_server import BoundedServer
from .persistent import Channels, identity
from . import gateway, http_server


class SharedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Console(self.root/'config.json', self.root/'data', demo=True)
        self.server = BoundedServer(('127.0.0.1', 0), Handler)
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.app.executor.shutdown(wait=True)
        self.app.store.db.close()
        self.tmp.cleanup()

    def browser(self):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, browser, path, body=None, csrf=None, origin=None):
        headers = {'Content-Type': 'application/json'}
        if csrf:
            headers['X-CSRF-Token'] = csrf
        if origin:
            headers['Origin'] = origin
        with browser.open(urllib.request.Request(self.url+path, data=None if body is None else json.dumps(body).encode(), headers=headers)) as r:
            return json.load(r)

    def test_accounts_hash_namespace_disable_and_last_admin(self):
        accounts = self.app.accounts
        accounts.create('a'*16, 'long-password-one')
        accounts.create('operator2', 'long-password-two')
        self.assertTrue(accounts.authenticate('a'*16, 'long-password-one'))
        self.assertFalse(accounts.authenticate('a'*16, 'wrong'))
        self.assertFalse(accounts.authenticate('missing', 'long-password-one'))
        self.assertNotIn('long-password', json.dumps(self.app.store.list('administrator')))
        accounts.disable('a'*16)
        self.assertFalse(accounts.authenticate('a'*16, 'long-password-one'))
        with self.assertRaises(ValueError):
            accounts.disable('operator2')
        self.assertEqual(len(self.app.runners()), 1)

    def test_multiple_admins_logout_revoke_and_user_independence(self):
        self.app.demo = False  # Disable demo token bypass after accounts exist.
        for name in ('operator1', 'operator2'):
            self.app.accounts.create(name, 'test-password-' + name)
        a, b, user = self.browser(), self.browser(), self.browser()
        ca = self.request(a, '/api/login', {'username':'operator1', 'password':'test-password-operator1'})['csrf']
        cb = self.request(b, '/api/login', {'username':'operator2', 'password':'test-password-operator2'})['csrf']
        self.assertEqual(len(self.request(user, '/api/user/bootstrap')['runners']), 1)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(user, '/api/admins')
        self.assertEqual(error.exception.code, 401)
        self.request(a, '/api/logout', {}, ca)
        self.assertEqual(len(self.request(b, '/api/admins')['administrators']), 2)
        ca = self.request(a, '/api/login', {'username':'operator1', 'password':'test-password-operator1'})['csrf']
        self.request(b, '/api/admins/disable', {'username':'operator1'}, cb)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(a, '/api/admins')
        self.assertEqual(error.exception.code, 401)
        self.assertNotIn('test-password', json.dumps(self.request(b, '/api/audit')))
        self.assertEqual(len(self.request(user, '/api/user/bootstrap')['runners']), 1)

    def test_user_root_before_any_admin_or_runner(self):
        self.app.remove_runner(self.app.runners()[0]['id'])
        user = self.browser()
        with user.open(self.url+'/') as response:
            self.assertTrue(response.url.endswith('/workbench/'))
            self.assertIn('fleetPicker', response.read().decode())
        self.assertEqual(self.request(user, '/api/user/bootstrap')['runners'], [])

    def test_cross_user_cannot_stop_another_users_job(self):
        a, b = self.browser(), self.browser()
        ca = self.request(a, '/api/user/bootstrap')['csrf']
        cb = self.request(b, '/api/user/bootstrap')['csrf']
        rid = self.app.runners()[0]['id']
        for session in self.app.user_sessions.values():
            session['selected'] = rid
        owner = next(u['owner'] for u in self.app.user_sessions.values() if u['csrf'] == ca)
        self.app.store.save('job-owner', {'id':'job-owner:request', 'owner':owner, 'runner':rid})
        stops = []
        def proxy(runner, method, path, body):
            if method == 'POST':stops.append(path)
            return 200, 'application/json', json.dumps({'fleet_request_id':'request', 'status':'stopping'}).encode()
        with patch.object(self.app, 'proxy', side_effect=proxy):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(b, '/r/'+rid+'/api/jobs/real-test/stop', {}, cb)
            self.assertEqual(error.exception.code,403)
            self.assertEqual(stops,[])
            self.request(a, '/r/'+rid+'/api/jobs/real-test/stop', {}, ca)
            self.assertEqual(len(stops),1)

    def test_concurrent_refreshes_coalesce_and_selection_can_force_fresh(self):
        import time
        self.app.demo = False
        row = self.app.runners()[0]
        calls = []
        def status(spec):
            calls.append(1);time.sleep(.03)
            return {'status':row['status'],'token':''}
        with patch.object(self.app.transport,'status',side_effect=status):
            with concurrent.futures.ThreadPoolExecutor(20) as pool:
                rows = list(pool.map(lambda _:self.app.refresh(row['id']),range(20)))
            self.assertEqual(len(calls),1)
            with concurrent.futures.ThreadPoolExecutor(20) as pool:
                list(pool.map(lambda _:self.app.refresh(row['id'],force=True),range(20)))
            self.assertEqual(len(calls),2)
            self.app.refresh(row['id'],force=True)
            self.assertEqual(len(calls),3)
            self.assertEqual(len(rows),20)

    def test_users_are_not_evicted_at_20_or_256(self):
        # Pre-existing valid sessions must not be silently evicted.
        import time
        self.app.user_sessions = {str(i):{'expires':time.time()+600} for i in range(300)}
        def enter(_):
            return self.request(self.browser(), '/api/user/bootstrap')['csrf']
        with concurrent.futures.ThreadPoolExecutor(20) as pool:
            csrf = list(pool.map(enter, range(40)))
        self.assertEqual(len(set(csrf)), 40)
        self.assertEqual(len(self.app.user_sessions), 340)

    def test_disable_enable_and_reset_never_revive_old_login(self):
        self.app.demo = False
        for name in ('operator1','operator2'):
            self.app.accounts.create(name,'test-password-'+name)
        a,b = self.browser(),self.browser()
        ca = self.request(a,'/api/login',{'username':'operator1','password':'test-password-operator1'})['csrf']
        cb = self.request(b,'/api/login',{'username':'operator2','password':'test-password-operator2'})['csrf']
        self.request(b,'/api/admins/disable',{'username':'operator1'},cb)
        self.request(b,'/api/admins/enable',{'username':'operator1'},cb)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(a,'/api/admins')
        self.assertEqual(error.exception.code,401)
        self.request(a,'/api/login',{'username':'operator1','password':'test-password-operator1'})
        self.request(b,'/api/admins/password',{'username':'operator1','password':'different-new-password'},cb)
        with self.assertRaises(urllib.error.HTTPError):self.request(a,'/api/admins')
        with self.assertRaises(urllib.error.HTTPError):
            self.request(a,'/api/login',{'username':'operator1','password':'test-password-operator1'})
        self.request(a,'/api/login',{'username':'operator1','password':'different-new-password'})
        self.assertNotIn('different-new-password',json.dumps(self.request(b,'/api/audit')))

    def test_anonymous_ownership_survives_center_restart_without_stale_selection(self):
        user = self.browser()
        data = self.request(user,'/api/user/bootstrap')
        old = next(s for s in self.app.user_sessions.values() if s['csrf']==data['csrf'])
        rid = self.app.runners()[0]['id']
        old.update(selected=rid,refreshed={rid:123})
        self.app.store.save('user-session',old)
        self.app.store.save('job-owner',{'id':'job-owner:restart-job','runner':rid,'owner':old['owner']})
        other = Console(self.root/'config.json',self.root/'data',demo=True)
        original_app = self.server.app;self.server.app = other
        try:
            restored = self.request(user,'/api/user/bootstrap')
            self.assertEqual(restored['csrf'],data['csrf'])
            self.assertIsNone(restored['selected_runner_id'])
            def proxy(*args):return 200,'application/json',b'{"fleet_request_id":"restart-job","status":"stopping"}'
            with patch.object(other,'proxy',side_effect=proxy):
                result = self.request(user,'/r/'+rid+'/api/jobs/real-test/stop',{},data['csrf'])
                self.assertEqual(result['status'],'stopping')
        finally:
            self.server.app=original_app;other.executor.shutdown(wait=True);other.store.db.close()

    def test_simultaneous_disables_keep_one_administrator(self):
        for name in ('left','right'):self.app.accounts.create(name,'long-test-password')
        def disable(name):
            try:self.app.accounts.disable(name);return True
            except ValueError:return False
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results=list(pool.map(disable,['left','right']))
        self.assertEqual(sum(results),1)
        self.assertEqual(sum(not a['disabled'] for a in self.app.accounts.list()),1)

    def test_initial_token_session_loses_power_after_first_account_created(self):
        self.app.demo=False
        browser=self.browser()
        csrf=self.request(browser,'/api/login',{'token':self.app.token})['csrf']
        result=self.request(browser,'/api/admins',{'username':'first-admin','password':'long-test-password'},csrf)
        self.assertTrue(result['requires_login'])
        with self.assertRaises(urllib.error.HTTPError):self.request(browser,'/api/admins')
        with self.assertRaises(urllib.error.HTTPError):self.request(self.browser(),'/api/login',{'token':self.app.token})

    def test_network_allowlist_is_enforced_before_public_pages(self):
        import ipaddress
        class ExternalClient(Handler):
            def handle_request(self,method):
                self.client_address=('198.51.100.5',12345)
                return super().handle_request(method)
        self.server.RequestHandlerClass=ExternalClient
        self.server.allowed_networks=[ipaddress.ip_network('192.0.2.0/24')]
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.browser().open(self.url+'/')
        self.assertEqual(error.exception.code,403)

    def test_service_definition_has_no_elevation_or_stored_windows_password(self):
        from .service import task_xml, verify_owner
        import xml.etree.ElementTree as ET
        root=ET.fromstring(task_xml('S-1-5-21-100','C:/Program Files/Python/pythonw.exe'))
        fields={e.tag.rsplit('}',1)[-1]:e.text for e in root.iter()}
        self.assertEqual(fields['RunLevel'],'LeastPrivilege')
        self.assertEqual(fields['LogonType'],'InteractiveToken')
        self.assertEqual(fields['MultipleInstancesPolicy'],'IgnoreNew')
        self.assertNotIn('Password',fields)
        verify_owner(task_xml('S-1-5-21-100','pythonw.exe'),'S-1-5-21-100')
        # schtasks may preserve the UTF-16 declaration but emit ASCII bytes.
        query_xml = task_xml('S-1-5-21-100','pythonw.exe').decode('utf-16').encode('ascii')
        verify_owner(query_xml, 'S-1-5-21-100')
        with self.assertRaises(ValueError):
            verify_owner(query_xml, 'S-1-5-21-200')
        with self.assertRaises(ValueError):
            verify_owner(task_xml('S-1-5-21-100','pythonw.exe'),'S-1-5-21-200')

    def test_concurrent_certificate_issue_uses_one_consistent_ca(self):
        directory=self.root/'concurrent-ca'
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            certificates=list(pool.map(lambda host:issue(host,directory),['192.0.2.10','192.0.2.11']))
        self.assertEqual(certificates[0]['ca'],certificates[1]['ca'])
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        ca=x509.load_pem_x509_certificate((directory/'ca.pem').read_bytes())
        key=serialization.load_pem_private_key((directory/'ca-key.pem').read_bytes(),password=None)
        self.assertEqual(ca.public_key().public_numbers(),key.public_key().public_numbers())

    def test_local_account_maintenance_does_not_mark_deployment_unknown(self):
        from .app import Store
        self.app.store.save('deployment',{'id':'pending-maintenance','state':'installing','spec':{},'detail':''})
        local=Store(self.root/'data',recover=False)
        try:
            Accounts(local).create('local-operator','long-test-password')
            self.assertEqual(local.get('deployment','pending-maintenance')['state'],'installing')
        finally:local.db.close()

    def test_tls_origin_and_secure_cookie(self):
        material = issue('127.0.0.1', self.root/'ca')
        cert, key = self.root/'cert.pem', self.root/'key.pem'
        cert.write_text(material['cert']); key.write_text(material['key'])
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.server.tls_context = context
        self.server.tls = True
        self.url = self.url.replace('http:', 'https:')
        trusted = ssl.create_default_context(cadata=material['ca'])
        browser = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=trusted))
        with browser.open(self.url+'/api/user/bootstrap') as r:
            self.assertIn('Secure', r.headers['Set-Cookie'])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(browser, '/api/user/bootstrap', origin='https://evil.example')
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(urllib.error.URLError):
            self.browser().open(self.url+'/api/user/bootstrap')

    def test_durable_gateway_reconnects_without_ssh_and_rejects_bad_auth(self):
        spec = self.app.runners()[0]['spec']
        material = issue('127.0.0.1', self.root/'ca')
        cert, key = self.root/'cert.pem', self.root/'key.pem'
        cert.write_text(material['cert']);key.write_text(material['key'])
        import sys
        with patch.dict(sys.modules, {'gateway':gateway, 'http_server':http_server}):
            location = importlib.util.spec_from_file_location('trial_persistent_gateway', Path(__file__).with_name('persistent_gateway.py'))
            module = importlib.util.module_from_spec(location)
            location.loader.exec_module(module)
        secret = secrets.token_urlsafe(32)
        module.CONFIG = {'secret':secret, 'spec':spec}
        server = BoundedServer(('127.0.0.1', 0), module.Handler)
        server.tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.tls_context.load_cert_chain(cert, key)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        calls = []
        def forward(request, _):
            gateway.validate_request(request, {**spec,'service_hosts':['192.0.2.10'],'tokenizer_roots':['/models']})
            calls.append(request['path'])
            return {'status':200,'content_type':'application/json','body':base64.b64encode(b'{"activity":"idle"}').decode()}
        channels = Channels(self.root/'channels')
        channels.entries[identity(spec)] = {'url':f'https://127.0.0.1:{server.server_port}', 'ca':material['ca'], 'secret':secret, 'profile_id':spec['profile_id']}
        channels.save()
        try:
            # A fresh object represents a center restart with no SSH session.
            restored = Channels(self.root/'channels')
            with patch.object(module, 'forward', side_effect=forward):
                self.assertEqual(restored.request(spec,'GET','/api/status',None)['status'],200)
                with self.assertRaises(urllib.error.HTTPError):
                    restored.request(spec,'POST','/api/config',{})
                restored.entries[identity(spec)]['secret'] = 'wrong'
                with self.assertRaises(urllib.error.HTTPError) as error:
                    restored.request(spec,'GET','/api/status',None)
                self.assertEqual(error.exception.code,403)
                self.assertEqual(calls,['/api/status'])
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__ == '__main__':
    unittest.main()
