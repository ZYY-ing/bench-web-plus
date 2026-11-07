"""Audit current locking through isolated HTTP Runners; never run inference."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from .app import Console,Store


class Runner:
    def __init__(self,root):
        spec=importlib.util.spec_from_file_location('audit_'+root.name,Path(__file__).parent/'runner-overlay/scripts/runner.py')
        self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
        m=self.m;m.BASE=root;m.RUNS=root/'results/jobs';m.CONFIG_PATH=root/'runner-config.json';m.TOKEN='isolated-test-token'
        m.CONFIG={'service':{'host':'192.0.2.10','port':8000,'model':'fixture','tokenizer_path':'/same/read-only/tokenizer'},'ais_bench_path':'unused','container':'isolated-fixture'}
        m.CONNECTION={'id':'verified','fingerprint':m.service_fingerprint(m.CONFIG['service'])}
        self.started=threading.Event();self.release=threading.Event();self.calls=0
        def worker(*args):
            self.calls+=1;self.started.set();assert self.release.wait(10)
            return root/'data.jsonl',False
        def job(record):
            self.started.set();self.release.wait(10);m.ACTIVE_JOB=None;m.LOCK.release()
        m.original_validate=m.validate
        m.dataset_worker=worker;m.run_job=job;m.probe_service=lambda service:{'data':[{'id':'fixture'}]};m.validate=lambda c:1;m.validate_config=lambda c,**kw:c
        self.server=m.RunnerHTTPServer(('127.0.0.1',0),m.Handler);m.PORT=self.server.server_port
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def request(self,path,body,owner=None):
        raw=json.dumps(body).encode();port=self.server.server_port
        headers={'Content-Type':'application/json','Cookie':f'runner_session_{port}=isolated-test-token'}
        if owner:headers['X-Test-User']=owner
        req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=raw,headers=headers)
        op=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:r=op.open(req,timeout=5)
        except urllib.error.HTTPError as error:r=error
        with r:return r.status,'application/json',r.read()
    def close(self):
        self.release.set()
        for _ in range(100):
            if not self.m.LOCK.locked():break
            time.sleep(.01)
        self.server.shutdown();self.server.server_close();self.thread.join()


class InterlockAudit(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name)
        self.a=Runner(root/'a');self.b=Runner(root/'b')
        self.store=Store(root/'center',recover=False)
        profiles={'a':{'host':'192.0.2.1','mode':'session'},'b':{'host':'192.0.2.2','mode':'session'}}
        self.runners={'a':self.a,'b':self.b}
        transport=SimpleNamespace(profiles=profiles,request=lambda spec,method,path,body:self.runners[spec['profile_id']].request(path,body))
        self.app=SimpleNamespace(demo=False,store=self.store,mutex=threading.RLock(),transport=transport)
        for name in ('a','b'):self.store.save('runner',{'id':name,'spec':{'profile_id':name,'port':18787}})
        self.body={'input_tokens':131072,'count':16,'max_out_len':1024,'prefix_ratio':.9,'seed':42,'generation_id':'one','connection_id':'verified'}
        self.job={'mode':'performance','host':'192.0.2.10','port':8000,'connection_id':'verified','execution':{'mode':'perf','target':{'kind':'direct'}}}
    def tearDown(self):
        self.a.close();self.b.close();self.store.db.close();self.temp.cleanup()
    def center(self,rid,path,body,owner):return Console._proxy_once(self.app,rid,'POST',path,dict(body),owner=owner)

    def test_different_runners_same_service_tokenizer_generate_concurrently(self):
        with ThreadPoolExecutor(2) as pool:
            responses=list(pool.map(lambda rid:self.center(rid,'/api/datasets/generate',self.body,'user-'+rid),('a','b')))
        self.assertEqual([r[0] for r in responses],[202,202])
        self.assertTrue(self.a.started.wait(1));self.assertTrue(self.b.started.wait(1))
        self.assertIsNone(self.store.request_for('a'));self.assertIsNone(self.store.request_for('b'))

    def test_same_runner_different_users_duplicate_identity_and_other_generation(self):
        self.assertEqual(self.a.request('/api/datasets/generate',self.body,'user-a')[0],202)
        self.assertTrue(self.a.started.wait(1))
        self.assertEqual(self.a.request('/api/datasets/generate',self.body,'user-b')[0],202)
        response=self.a.request('/api/datasets/generate',{**self.body,'generation_id':'new'},'user-b')
        self.assertEqual(response[0],409);self.assertEqual(self.a.calls,1)
        self.assertIn('192.0.2.10:8000',json.loads(response[2])['error']);self.assertIn('131072',json.loads(response[2])['error'])

    def test_generation_blocks_same_runner_service_switch_and_test(self):
        self.a.request('/api/datasets/generate',self.body);self.assertTrue(self.a.started.wait(1))
        reply=self.a.request('/api/check-service',{'service':{'host':'192.0.2.11'}})
        self.assertEqual(reply[0],409);self.assertIn('生成性能数据集',json.loads(reply[2])['error'])
        self.assertEqual(self.a.request('/api/jobs',self.job)[0],409)
        self.assertEqual(self.a.m.CONFIG['service']['host'],'192.0.2.10')

    def test_running_test_blocks_same_runner_generation_and_service_switch(self):
        self.assertEqual(self.center('a','/api/jobs',self.job,'user-a')[0],202)
        self.assertTrue(self.a.started.wait(1))
        self.assertEqual(self.a.request('/api/datasets/generate',self.body)[0],409)
        self.assertEqual(self.a.request('/api/check-service',{'service':{'host':'192.0.2.11'}})[0],409)

    def test_center_blocks_different_runners_same_service_but_other_service_remains_free(self):
        self.assertEqual(self.center('a','/api/jobs',self.job,'user-a')[0],202)
        with self.assertRaisesRegex(ValueError,'Runner 或目标服务已被占用') as ctx:
            self.center('b','/api/jobs',self.job,'user-b')
        self.assertIn('192.0.2.1:18787',str(ctx.exception));self.assertIn('192.0.2.10:8000',str(ctx.exception))
        self.assertIsNone(self.store.request_for('b'))
        self.assertEqual(self.center('b','/api/jobs',{**self.job,'host':'192.0.2.11'},'user-b')[0],202)

    def test_center_blocks_same_runner_different_services(self):
        self.assertEqual(self.center('a','/api/jobs',self.job,'tab-a')[0],202)
        with self.assertRaisesRegex(ValueError,'占用'):
            self.center('a','/api/jobs',{**self.job,'host':'192.0.2.11'},'tab-b')

    def test_generation_on_other_runner_does_not_lock_model_under_test(self):
        self.assertEqual(self.center('a','/api/jobs',self.job,'user-a')[0],202)
        self.assertEqual(self.center('b','/api/datasets/generate',self.body,'user-b')[0],202)

    def test_loopback_cannot_disguise_a_remote_service_and_bypass_center_target_lock(self):
        c={**self.job,'engine':'aisbench','model':'fixture','tokenizer_path':'/same/read-only/tokenizer','batch_size':1,'max_out_len':64,'generation_kwargs':{},'api':{'endpoint':'chat'},'sample_limit':1,'workload':{'input_tokens':64,'count':1}}
        self.assertEqual(self.a.m.original_validate(c),1)
        for host in ('127.0.0.1','localhost'):
            with self.assertRaisesRegex(ValueError,'当前配置只允许'):self.a.m.original_validate({**c,'host':host})

    def test_expired_connection_releases_generation_reservation(self):
        self.a.m.CONNECTION['id']='new-connection'
        with self.assertRaisesRegex(ValueError,'连接未验证'):self.a.m.generation_response(self.body)
        self.assertFalse(self.a.m.LOCK.locked());self.assertIsNone(self.a.m.OPERATION)

    def test_failed_test_preflight_releases_reservation_and_reports_rejection(self):
        with patch.object(self.a.m,'probe_service',side_effect=ValueError('fixture offline')):
            response=self.a.request('/api/jobs',{**self.job,'fleet_request_id':'f'*32})
        self.assertEqual(response[0],400);self.assertFalse(self.a.m.LOCK.locked());self.assertIsNone(self.a.m.OPERATION)
        self.assertEqual(self.a.m.FLEET_REQUESTS['f'*32]['state'],'rejected')

    def test_in_flight_service_check_blocks_generation_and_preserves_configuration(self):
        # Deterministically reproduce the check/use gap with two HTTP clients.
        m=self.a.m;entered=threading.Event();allow_gen=threading.Event();probe_entered=threading.Event();allow_probe=threading.Event()
        original=m.generation_response
        def delayed(body):entered.set();assert allow_gen.wait(4);return original(body)
        def probe(service):probe_entered.set();assert allow_probe.wait(4);return {'data':[{'id':'fixture'}]}
        with patch.object(m,'generation_response',delayed),patch.object(m,'probe_service',probe):
            with ThreadPoolExecutor(2) as pool:
                gen=pool.submit(self.a.request,'/api/datasets/generate',self.body);self.assertTrue(entered.wait(1))
                check=pool.submit(self.a.request,'/api/check-service',{'service':{'host':'192.0.2.11'}});self.assertTrue(probe_entered.wait(1))
                allow_gen.set();reply=gen.result();self.assertEqual(reply[0],409);self.assertIn('检查服务连接',json.loads(reply[2])['error'])
                self.assertFalse(self.a.started.is_set());self.assertEqual(m.CONFIG['service']['host'],'192.0.2.10')
                allow_probe.set();self.assertEqual(check.result()[0],200)
                self.assertFalse(m.LOCK.locked());self.assertEqual(m.CONFIG['service']['host'],'192.0.2.11')


if __name__=='__main__':unittest.main()
