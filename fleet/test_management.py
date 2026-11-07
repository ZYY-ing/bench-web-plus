import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

from .managed_ssh import Vault, ManagedSession, Authorizations, make_credentials


class ManagementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = {'id':'verify-one','host':'192.0.2.10','ssh_port':22,'user':'root'}
        cls.credential = make_credentials(cls.profile, 'SHA256:' + 'A' * 43)

    def test_dpapi_roundtrip_is_not_plaintext(self):
        if os.name != 'nt':
            self.skipTest('Windows DPAPI integration')
        with tempfile.TemporaryDirectory() as directory:
            vault = Vault(directory)
            vault.save('test', {'ssh_private':'do-not-store-as-plaintext'})
            raw = vault.path('test').read_bytes()
            self.assertNotIn(b'do-not-store-as-plaintext', raw)
            self.assertEqual(vault.load('test')['ssh_private'],'do-not-store-as-plaintext')
            with self.assertRaises(ValueError):
                vault.unprotect(b'corrupt DPAPI blob')

    def test_no_plaintext_fallback_when_secure_storage_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            def fail(raw):raise ValueError('vault unavailable')
            vault = Vault(directory, protect=fail)
            with self.assertRaises(ValueError):vault.save('test',self.credential)
            self.assertFalse(vault.path('test').exists())

    def test_boot_task_runs_only_from_explicit_protected_directory(self):
        from .service import task_xml,verify_owner
        import xml.etree.ElementTree as ET
        root=Path('C:/ProgramData/BenchWebPlus/app')
        raw=task_xml('S-1-5-18','C:/ProgramData/BenchWebPlus/runtime/pythonw.exe',startup=True,directory=root)
        tree=ET.fromstring(raw)
        self.assertTrue(any(e.tag.endswith('BootTrigger') for e in tree.iter()))
        self.assertFalse(any(e.tag.endswith('LogonType') for e in tree.iter()))
        self.assertEqual(next(e.text for e in tree.iter() if e.tag.endswith('UserId')),'S-1-5-18')
        verify_owner(raw,'S-1-5-18',root)
        with self.assertRaises(ValueError):verify_owner(raw,'S-1-5-18',Path('C:/other/app'))

    def test_server_recovery_uses_system_python_without_interactive_conda(self):
        from .recovery import INSTALL_SERVICE
        self.assertIn("python='/usr/bin/python3'",INSTALL_SERVICE)
        self.assertIn("env=clean",INSTALL_SERVICE)
        self.assertIn("['systemctl','enable','--now',unit]",INSTALL_SERVICE)

    @unittest.skipUnless(os.name == 'nt', 'Windows Task Scheduler schema integration')
    def test_boot_xml_is_accepted_by_real_windows_task_scheduler(self):
        from .service import task_xml,validate_boot_xml
        raw=task_xml('S-1-5-18','C:/ProgramData/BenchWebPlus/runtime/pythonw.exe',startup=True,
                     directory=Path('C:/ProgramData/BenchWebPlus/app'))
        validate_boot_xml(raw)
        broken=raw.decode('utf-16').replace('<RunLevel>','<LogonType>ServiceAccount</LogonType><RunLevel>').encode('utf-16')
        with self.assertRaisesRegex(ValueError,'XML 校验失败'):
            validate_boot_xml(broken)

    def test_boot_install_without_elevation_does_not_change_files_or_tasks(self):
        from . import service
        shell=types.SimpleNamespace(IsUserAnAdmin=lambda:False)
        import ctypes
        with patch.object(ctypes,'windll',types.SimpleNamespace(shell32=shell),create=True),patch.object(service.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'一次 Windows 管理员权限'):
                service.install_boot()
            run.assert_not_called()

    def test_boot_upgrade_preserves_live_authorizations_and_profile_database(self):
        from . import service
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source';dest=Path(directory)/'protected'
            for root in (source,dest):
                (root/'.fleet-data').mkdir(parents=True)
                (root/'fleet').mkdir()
            (source/'fleet/app.py').write_text('new code')
            (source/'.fleet-data/fleet.sqlite3').write_text('stale original database')
            (dest/'.fleet-data/fleet.sqlite3').write_text('live protected database and auth')
            (source/'fleet-config.json').write_text('stale profiles')
            (dest/'fleet-config.json').write_text('live profiles')
            with patch.object(service,'ROOT',source):service.copy_app_sources(dest,preserve_data=True)
            self.assertEqual((dest/'fleet/app.py').read_text(),'new code')
            self.assertEqual((dest/'.fleet-data/fleet.sqlite3').read_text(),'live protected database and auth')
            self.assertEqual((dest/'fleet-config.json').read_text(),'live profiles')

    def test_task_stop_waits_for_delayed_socket_release_without_killing(self):
        from . import service
        probe=MagicMock();probe.__enter__.return_value=probe
        probe.bind.side_effect=[OSError('still closing'),OSError('still closing'),None]
        with patch.object(service.socket,'socket',return_value=probe),patch.object(service.time,'sleep') as sleep,patch.object(service.subprocess,'run') as run:
            service.wait_port_free({'bind':'0.0.0.0','port':19885})
        self.assertEqual(probe.bind.call_count,3);self.assertEqual(sleep.call_count,2);run.assert_not_called()

    def test_busy_port_timeout_preserves_unknown_listener(self):
        from . import service
        probe=MagicMock();probe.__enter__.return_value=probe;probe.bind.side_effect=OSError('address in use')
        with patch.object(service.socket,'socket',return_value=probe),patch.object(service.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'19885.*仍不可用'):
                service.wait_port_free({'bind':'0.0.0.0','port':19885},timeout=0)
        run.assert_not_called()

    def test_missing_credential_reports_reauthorization_instead_of_retrying(self):
        with tempfile.TemporaryDirectory() as directory:
            manager=Authorizations(directory)
            profile={**self.profile,'auto_manage':True};sessions={}
            with patch.object(manager,'watch') as watch:
                manager.restore([profile],sessions)
            self.assertIn('缺少',profile['management_error']);self.assertEqual(sessions,{})
            watch.assert_not_called()

    def test_recovery_never_starts_or_kills_external_container(self):
        fake=types.SimpleNamespace(LOCK_EX=1,LOCK_NB=2,flock=lambda *args:None)
        source=Path(__file__).with_name('recovery_host.py')
        spec=importlib.util.spec_from_file_location('verification_recovery_host',source)
        recovery=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'fcntl':fake}):spec.loader.exec_module(recovery)
        row={'spec':{'container':'external-container'},'active':True,'container_id':'expected-id'}
        info={'Id':'expected-id','State':{'Running':False},'HostConfig':{}}
        with patch.object(recovery,'run',return_value=json.dumps([info])) as command:
            self.assertEqual(recovery.recover(row),'waiting_for_container_owner')
            command.assert_called_once_with(['docker','inspect','external-container'])
        with patch.object(recovery,'run',return_value=json.dumps([{**info,'Id':'recreated-container'}])) as command:
            self.assertEqual(recovery.recover(row),'container_identity_changed')
            self.assertEqual(command.call_count,1)

    def test_restart_loads_key_without_password_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Vault(directory, protect=lambda b:b[::-1], unprotect=lambda b:b[::-1])
            vault.save(self.profile['id'],self.credential)
            manager = Authorizations(directory,vault)
            sessions = {}
            with patch.object(manager,'watch') as watch:
                manager.restore([{**self.profile,'auto_manage':True}],sessions)
            self.assertIsInstance(sessions[self.profile['id']],ManagedSession)
            self.assertNotIn('password',sessions[self.profile['id']].credential)
            watch.assert_called_once()

    def test_connection_loss_is_not_replayed(self):
        session = ManagedSession(self.credential)
        with patch.object(session,'connect'),patch.object(session,'_request',side_effect=[json.dumps({'nonce':'f'*64}),ValueError('result unknown')]) as request:
            with self.assertRaisesRegex(ValueError,'unknown'):
                session.execute("print('mutation')")
        self.assertEqual(request.call_count,2)

    def test_missing_exit_status_is_unknown_not_known_failure(self):
        session = ManagedSession(self.credential)
        channel = MagicMock();channel.recv_ready.return_value=False;channel.exit_status_ready.return_value=True;channel.recv_exit_status.return_value=-1
        session.client = MagicMock();session.client.get_transport.return_value.open_session.return_value=channel
        with self.assertRaisesRegex(ValueError,'结果未知'):
            session._request({'action':'execute'},5)
        session.client.close.assert_called_once()

    def test_network_backoff_does_not_hammer_server(self):
        from .session_ssh import paramiko_module
        module=paramiko_module();client=MagicMock();client.connect.side_effect=OSError('offline')
        session=ManagedSession(self.credential)
        with patch.object(module,'SSHClient',return_value=client),patch('fleet.managed_ssh.time.monotonic',return_value=100):
            with self.assertRaises(ValueError):session.connect()
            with self.assertRaisesRegex(ValueError,'自动重连'):session.connect()
        client.connect.assert_called_once()
        self.assertFalse(session.blocked)

    def test_rejected_key_blocks_automatic_authentication_retries(self):
        from .session_ssh import paramiko_module
        module=paramiko_module();client=MagicMock();client.connect.side_effect=module.AuthenticationException('revoked')
        session=ManagedSession(self.credential)
        with patch.object(module,'SSHClient',return_value=client):
            with self.assertRaises(ValueError):session.connect()
            with self.assertRaises(ValueError):session.connect()
        self.assertTrue(session.blocked);client.connect.assert_called_once()

    def test_broker_rejects_unsigned_and_replayed_requests(self):
        from cryptography.hazmat.primitives import serialization,hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.exceptions import InvalidSignature
        fake=types.SimpleNamespace(LOCK_EX=1,flock=lambda *args:None)
        source=Path(__file__).with_name('management_host.py')
        spec=importlib.util.spec_from_file_location('verification_management_host',source)
        broker=importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules,{'fcntl':fake}):spec.loader.exec_module(broker)
        signer=serialization.load_pem_private_key(self.credential['signer_private'].encode(),password=None)
        def verify(args,**kwargs):
            try:
                signer.public_key().verify(Path(args[-2]).read_bytes(),Path(args[-1]).read_bytes(),padding.PKCS1v15(),hashes.SHA256())
                code=0
            except InvalidSignature:code=1
            return types.SimpleNamespace(returncode=code)
        with tempfile.TemporaryDirectory() as directory,patch.object(broker.os,'geteuid',return_value=Path(directory).stat().st_uid,create=True),patch.object(broker.subprocess,'run',side_effect=verify),patch.object(broker.subprocess,'Popen') as launched:
            root=Path(directory).resolve()
            nonce=broker.process(root,{'action':'challenge'})['nonce'];script=b"print('once')"
            signature=signer.sign(nonce.encode()+bytes([0])+script,padding.PKCS1v15(),hashes.SHA256())
            request={'action':'execute','nonce':nonce,'script':base64.b64encode(script).decode(),'signature':base64.b64encode(signature).decode()}
            with self.assertRaisesRegex(ValueError,'signature'):
                broker.process(root,{**request,'script':base64.b64encode(b"print('injected')").decode()})
            launched.assert_not_called()
            launched.return_value.returncode=0
            self.assertEqual(broker.process(root,request),0)
            with self.assertRaisesRegex(ValueError,'consumed'):broker.process(root,request)
            launched.assert_called_once()


if __name__=='__main__':unittest.main()
