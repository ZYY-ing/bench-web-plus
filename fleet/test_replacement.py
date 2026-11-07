import json
import ast
import io
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from contextlib import contextmanager
from contextlib import redirect_stdout
from unittest.mock import patch
from unittest.mock import MagicMock

from .app import Console


class ReplacementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.app=Console(self.root/'config.json',self.root/'data',demo=True)
        self.row=self.app.runners()[0];self.profile=self.app.profiles[0]
        self.profile['mode']='session';self.app.transport.sessions[self.profile['id']]=object()
        self.app.machine_inventory[self.profile['id']]={'containers':[{'name':self.row['spec']['container']},{'name':'container-b'}],'images':['local:test']}
        self.body={'profile_id':self.profile['id'],'container':'container-b','base':self.row['spec']['base'],'port':self.row['spec']['port'],'access_port':self.row['spec']['port']+1000,'container_mode':'existing','durable':True}

    def tearDown(self):
        self.app.executor.shutdown(wait=True);self.app.store.db.close();self.tmp.cleanup()

    def test_retaining_previous_requires_distinct_ports_and_paths(self):
        for body in ({**self.body,'port':18788,'access_port':19788},{**self.body,'base':'/home/benchops/independent'}):
            with self.subTest(body=body),self.assertRaisesRegex(ValueError,'保留旧 Runner'):
                self.app.deploy(body)
        body={**self.body,'port':18788,'access_port':19788,'base':'/home/benchops/independent'}
        with patch.object(self.app.executor,'submit'):
            task=self.app.deploy(body)
        self.assertNotEqual(task['runner_id'],self.row['id'])
        self.assertNotIn('_replace',task['spec'])
        self.app.worker(task)
        self.assertEqual(len(self.app.runners()),2)
        self.assertEqual({r['spec']['container'] for r in self.app.runners()},{self.row['spec']['container'],'container-b'})

    def test_stopped_replacement_reuses_registration_without_release_option(self):
        body=self.body
        with patch.object(self.app.executor,'submit'):
            task=self.app.deploy(body)
        self.assertEqual(task['runner_id'],self.row['id'])
        self.assertEqual(task['spec']['_replace']['container'],self.row['spec']['container'])
        self.app.worker(task)
        self.assertEqual(len(self.app.runners()),1)
        self.assertEqual(self.app.runners()[0]['spec']['container'],'container-b')
        self.assertNotIn('_replace',self.app.runners()[0]['spec'])

    def test_claimed_running_runner_cannot_be_released(self):
        self.app.transport.sessions[self.profile['id']]=MagicMock()
        self.app.transport.sessions[self.profile['id']].execute.return_value=json.dumps({'retired':False})
        self.app.store.claim(self.row['id'],'model:8000','active-request')
        body={**self.body,'release_previous':True,'previous_runner_id':self.row['id']}
        with self.assertRaisesRegex(ValueError,'有测试'):
            self.app.deploy(body)
        self.assertEqual(self.app.store.request_for(self.row['id']),'active-request')

    def test_other_container_virtualenv_is_not_reused_even_if_executable(self):
        import sys
        import subprocess
        import types
        tree=ast.parse(Path(__file__).with_name('direct_worker.py').read_text(encoding='utf-8'))
        probe=next(n.value.value for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='probe_code' for t in n.targets))
        base=self.root/'shared';entry=base/'bench-venv/bin/ais_bench';entry.parent.mkdir(parents=True);entry.write_text('old executable');entry.chmod(0o755)
        # The remote payload stores Linux separators; retain them for the path
        # suffix even when this integration probe runs on a Windows test host.
        (base/'runner-config.json').write_text(json.dumps({'container':'container-a','ais_bench_path':str(base)+'/bench-venv/bin/ais_bench'}))
        output=io.StringIO()
        with patch.object(sys,'argv',['probe',str(base),'{}','new-container-id','container-b']),patch.object(subprocess,'run',return_value=types.SimpleNamespace(returncode=0)) as execute,redirect_stdout(output):
            exec(compile(probe,'deployment-probe','exec'),{})
        self.assertEqual(json.loads(output.getvalue())['ais_path'],'');execute.assert_not_called()
        self.assertEqual(entry.read_text(),'old executable')

    def namespace(self,base,state):
        namespace={'Path':Path,'json':json,'os':os,'time':time,'contextmanager':contextmanager}
        exec(compile(Path(__file__).with_name('replacement.py').read_text(encoding='utf-8'),'replacement.py','exec'),namespace)
        old={**self.row['spec'],'container':'container-a'};spec={**old,'container':'container-b','_replace':old}
        namespace.update(host_project=lambda s:base,retired_container=lambda s:{'retired':not state.get('running_container',False)},
            port_listening=lambda port:state['port'],verify_runner_owner=lambda spec:None)
        def read(spec):
            if state['owner']!=spec['container']:raise ValueError('wrong owner')
            return {'status':{'activity':'running' if state.get('busy') else 'idle','process_tree_confirmed_stopped':not state.get('busy')}}
        def stop(spec):
            read(spec);state['stopped'].append(spec['container']);state['port']=False;state['owner']=None
        def start(spec):
            state['owner']=spec['container'];state['port']=True;state['started'].append(spec['container'])
        namespace.update(read_status=read,stop_owned_idle_runner=stop,start_owned_runner=start)
        return namespace,spec

    def test_failed_replacement_restores_config_code_and_original_environment(self):
        base=self.root/'project';(base/'scripts').mkdir(parents=True);(base/'scripts/runner.py').write_text('old runner')
        cfg={'ais_bench_path':'/old/container-a/ais_bench','container':'container-a'}
        (base/'runner-config.json').write_text(json.dumps(cfg));(base/'bench-venv').mkdir();(base/'bench-venv/intact').write_text('original environment')
        state={'owner':None,'port':False,'stopped':[],'started':[]}
        ns,spec=self.namespace(base,state)
        with patch.dict(os.environ,{},clear=True),self.assertRaisesRegex(RuntimeError,'dependency install failed'):
            with ns['replace_runner_transaction'](spec):
                (base/'scripts/runner.py').write_text('broken new runner')
                (base/'runner-config.json').write_text(json.dumps({'ais_bench_path':'/missing/ais_bench'}))
                raise RuntimeError('dependency install failed')
        self.assertEqual(json.loads((base/'runner-config.json').read_text()),cfg)
        self.assertEqual((base/'scripts/runner.py').read_text(),'old runner')
        self.assertEqual((base/'bench-venv/intact').read_text(),'original environment')
        self.assertEqual(state['stopped'],[]);self.assertEqual(state['started'],[])

    def test_busy_or_unknown_owner_is_never_stopped(self):
        for busy,owner in ((True,'container-a'),(False,'unknown')):
            state={'owner':owner,'port':True,'busy':busy,'stopped':[],'started':[],'running_container':True}
            ns,spec=self.namespace(self.root/'untouched',state)
            with self.assertRaises(ValueError):
                with ns['replace_runner_transaction'](spec):self.fail('must not start installation')
            self.assertFalse(state['stopped']);self.assertFalse((self.root/'untouched').exists())

    def test_kernel_listener_probe_ignores_closing_connections(self):
        ns,_=self.namespace(self.root/'unused',{'port':False})
        directory=self.root/'net';directory.mkdir()
        path=directory/'tcp';path.write_text('header\n 0: 0100007F:4963 00000000:0000 06 other\n')
        self.assertFalse(ns['tcp_port_listening'](18787,directory))
        path.write_text('header\n 0: 0100007F:4963 00000000:0000 0A other\n')
        self.assertTrue(ns['tcp_port_listening'](18787,directory))

    def test_rollback_recovers_files_when_new_listener_exits_during_probe(self):
        base=self.root/'closing';(base/'scripts').mkdir(parents=True);(base/'scripts/runner.py').write_text('original code')
        state={'owner':None,'port':False,'stopped':[],'started':[]}
        ns,spec=self.namespace(base,state)
        tree=ast.parse(Path(__file__).with_name('replacement.py').read_text(encoding='utf-8'))
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='stop_owned_idle_runner')
        exec(compile(ast.Module(body=[function],type_ignores=[]),'actual-stop-helper','exec'),ns)
        def closing(_):
            state['port']=False
            raise ValueError('Connection refused')
        ns['read_status']=closing
        with patch.dict(os.environ,{},clear=True),self.assertRaisesRegex(RuntimeError,'original installation failure'):
            with ns['replace_runner_transaction'](spec):
                (base/'scripts/runner.py').write_text('partially installed code')
                state['port']=True
                raise RuntimeError('original installation failure')
        self.assertEqual((base/'scripts/runner.py').read_text(),'original code')
        self.assertEqual(state['stopped'],[]);self.assertEqual(state['started'],[])

    def test_running_previous_container_is_rejected_without_stopping_it(self):
        self.app.demo=False;session=MagicMock();session.execute.return_value=json.dumps({'retired':False});self.app.transport.sessions[self.profile['id']]=session
        with patch.object(self.app.executor,'submit') as submit,self.assertRaisesRegex(ValueError,'请手动停止'):
            self.app.deploy(self.body)
        submit.assert_not_called();self.assertEqual(len(self.app.runners()),1)

    def test_install_ca_path_is_validated_without_disabling_tls(self):
        from .transport import validate_install_network
        self.assertEqual(validate_install_network({'pip_cert':'/mnt/share/certs/company-ca.pem'})['pip_cert'],'/mnt/share/certs/company-ca.pem')
        for path in ('/etc/shadow','/mnt/../etc/file','/home/cert file.pem','https://example/ca.pem'):
            with self.subTest(path=path),self.assertRaises(ValueError):validate_install_network({'pip_cert':path})

    def test_missing_token_is_explained_without_querying_unknown_listener(self):
        from . import remote_helper
        import subprocess
        with patch.object(remote_helper,'container_info'),patch.object(remote_helper,'command',side_effect=subprocess.CalledProcessError(1,['cat'],output=b'No such file')),patch.object(remote_helper.subprocess,'check_output') as request:
            with self.assertRaisesRegex(ValueError,'凭据文件缺失或不可读'):
                remote_helper.read_status(self.row['spec'])
            request.assert_not_called()

    def test_unknown_occupied_port_never_stops_or_overwrites_a_process(self):
        from contextlib import nullcontext
        source=Path(__file__).with_name('direct_worker.py').read_text(encoding='utf-8');calls=[]
        info={'Id':'image-id','State':{'Running':True},'HostConfig':{'NetworkMode':'host'},'Mounts':[{'Destination':self.row['spec']['base'],'RW':True}]}
        def command(*args):
            calls.append(args)
            if args[:2]==('docker','inspect'):return json.dumps([info])
            if any('expected=json.loads' in str(a) for a in args):return json.dumps({'installed':False,'latest':False,'version':'partial','different':[],'ais_path':'/usr/bin/ais_bench','running':False})
            if any('connect_ex' in str(a) for a in args):return 'True'
            return ''
        install=MagicMock();namespace={'json':json,'Path':Path,'time':time,'command':command,'container_info':lambda s:None,'read_status':MagicMock(side_effect=ValueError('凭据文件缺失')),'main':install,'remote_deployment_lock':lambda s:nullcontext()}
        exec(compile(source,'direct-worker','exec'),namespace)
        with self.assertRaisesRegex(ValueError,'已被监听.*凭据文件缺失'):
            namespace['direct_main']({'spec':self.row['spec'],'release':{'id':'release','files':{}},'runner_sha256':'source'})
        install.assert_not_called()
        self.assertFalse(any(args[:3]==('docker','exec','-d') or any('os.kill(' in str(a) for a in args) for args in calls))


if __name__=='__main__':unittest.main()
