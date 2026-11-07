"""Manual expiry cleanup, exact selection, and administrator boundary tests."""
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).parent/'runner-overlay/scripts'
spec=importlib.util.spec_from_file_location('runner_storage_cleanup',SCRIPTS/'storage_cleanup.py')
storage=importlib.util.module_from_spec(spec);spec.loader.exec_module(storage)
sys.path.insert(0,str(SCRIPTS))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name).resolve();self.now=time.time();self.old=self.now-9*86400
    def tearDown(self):self.tmp.cleanup()
    def fixture(self,name='real-1234-abcd',kind='job',when=None,status='completed'):
        when=when or self.old
        p=self.base/('results/jobs' if kind=='job' else 'datasets/perf')/name;p.mkdir(parents=True)
        if kind=='job':
            (p/'record.json').write_text(json.dumps({'id':name,'status':status,'completed_at':datetime.fromtimestamp(when,timezone.utc).isoformat()}))
            (p/'execution.log').write_text('Native AISBench log\n')
            (p/'config.py').write_text('models=[]')
        else:
            (p/'data.jsonl').write_text('{"text":"fixture"}\n');(p/'dataset-manifest.json').write_text('[]')
        self.age(p,when);return p
    def age(self,p,when):
        for f in p.rglob('*'):os.utime(f,(when,when))
        os.utime(p,(when,when))
    def scan(self,days=7):return storage.scan(self.base,days,'token',self.now)
    def delete(self,rows):
        # Production refuses unsafe Windows rmtree. Linux integration separately
        # verifies the actual fd-based deletion, not just this portable logic.
        with patch.object(storage.shutil.rmtree,'avoids_symlink_attacks',True,create=True):
            return storage.delete(self.base,rows,'token',self.now)
    def test_days_and_expiry_selection(self):
        self.fixture();self.fixture('real-2-aa',when=self.now-2*86400)
        self.assertEqual(len(self.scan()['items']),1);self.assertEqual(len(self.scan(1)['items']),2)
        for value in (0,-1,True,1.5,'7',3651):
            with self.assertRaises(ValueError):self.scan(value)
    def test_delete_selected_only_removes_entire_job_history(self):
        a=self.fixture();b=self.fixture('real-2-ab');rows=self.scan()['items']
        result=self.delete([next(r for r in rows if r['name']==a.name)])
        self.assertEqual(result['results'][0]['status'],'deleted');self.assertFalse(a.exists());self.assertTrue((b/'record.json').exists())
    def test_running_unknown_and_recent_updates_protected(self):
        self.fixture(status='running');self.fixture('real-2-aa',status='unknown');p=self.fixture('real-3-aa');(p/'execution.log').touch()
        self.assertEqual(self.scan()['items'],[])
    def test_job_change_after_scan_and_plan_tampering_skipped(self):
        p=self.fixture();row=self.scan()['items'][0];(p/'execution.log').write_text('updated')
        self.assertEqual(self.delete([row])['results'][0]['status'],'skipped')
        row['relative']='../../elsewhere';self.assertEqual(self.delete([row])['results'][0]['status'],'skipped');self.assertTrue(p.exists())
    def test_cached_reader_and_generation_gate_protect_cache(self):
        p=self.fixture('cache-a','cache');(p/'.dataset-reader-own').write_text('active');self.age(p,self.old)
        self.assertFalse(self.scan()['items']);(p/'.dataset-reader-own').unlink();(p/'.dataset-write.owner').write_text('{}');self.age(p,self.old)
        self.assertFalse(self.scan()['items'])
    def test_other_writer_between_validation_and_lock_is_not_deleted(self):
        from contextlib import contextmanager
        p=self.fixture('cache-a','cache');row=self.scan()['items'][0]
        @contextmanager
        def writer_then_acquire(*args):
            (p/'data.jsonl').write_text('newly generated\n')
            yield
        with patch('dataset_lock.cache_write_lock',writer_then_acquire):
            self.assertEqual(self.delete([row])['results'][0]['status'],'skipped')
        self.assertEqual((p/'data.jsonl').read_text(),'newly generated\n')
    def test_cache_delete_keeps_lock_inode_and_removes_payload(self):
        p=self.fixture('cache-a','cache');(p/'.dataset-write.lock').write_bytes(b' ');self.age(p,self.old)
        inode=(p/'.dataset-write.lock').stat().st_ino;row=self.scan()['items'][0]
        self.assertEqual(self.delete([row])['results'][0]['status'],'deleted')
        self.assertFalse((p/'data.jsonl').exists());self.assertEqual((p/'.dataset-write.lock').stat().st_ino,inode)
        self.assertFalse((p/'.dataset-write.owner').exists())
    def test_fresh_reuse_and_expired_plan_skip(self):
        p=self.fixture('cache-a','cache');row=self.scan()['items'][0];(p/'.last-used').touch()
        self.assertEqual(self.delete([row])['results'][0]['status'],'skipped')
        self.age(p,self.old);row=storage.scan(self.base,7,'token',self.now-7200)['items'][0]
        self.assertEqual(self.delete([row])['results'][0]['status'],'skipped')
    def test_symlink_or_outside_roots_never_followed(self):
        outside=self.base/'important';outside.mkdir();(outside/'keep').write_text('keep')
        root=self.base/'datasets/perf';root.mkdir(parents=True)
        try:(root/'linked').symlink_to(outside,target_is_directory=True)
        except OSError:self.skipTest('Windows symlink privilege unavailable')
        self.assertFalse(self.scan()['items']);self.assertTrue((outside/'keep').exists())


class CleanupAdminBoundary(unittest.TestCase):
    def test_coordinator_scans_all_and_deletes_only_selected_runner(self):
        from types import SimpleNamespace
        import threading
        from .app import Store
        from .storage_cleanup import StorageCleanup
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp),recover=False)
            app=SimpleNamespace(store=store,mutex=threading.RLock(),transport=SimpleNamespace(profiles={'p':{'id':'p','host':'fixture'}},session=lambda profile:object()))
            for rid in ('a','b'):store.save('runner',{'id':rid,'spec':{'profile_id':'p','container':rid,'base':'/fixture/'+rid,'port':1}})
            calls=[]
            def remote(session,spec,action,body):
                calls.append((spec['container'],action,body))
                if action=='scan':return {'items':[{'kind':'job','name':'real-old','path':spec['base']+'/real-old','bytes':10,'time':1,'signature':'server'}]}
                return {'results':[{'path':spec['base']+'/real-old','status':'deleted','bytes':10}],'deleted_bytes':10}
            cleanup=StorageCleanup(app)
            def wait(task):
                for _ in range(200):
                    state=cleanup.view(task['id'])
                    if state['state']=='completed':return state
                    time.sleep(.005)
                self.fail('cleanup coordinator timed out')
            with patch('fleet.storage_cleanup.remote_request',remote):
                scanned=wait(cleanup.scan(14));self.assertEqual(len(scanned['items']),2)
                self.assertNotIn('signature',json.dumps(scanned))
                selected=next(r for r in scanned['items'] if r['runner_id']=='b')
                result=wait(cleanup.delete(scanned['id'],[selected['id']]))
                self.assertEqual(result['deleted_bytes'],10)
                self.assertEqual([r[0] for r in calls if r[1]=='delete'],['b'])
                self.assertIsNone(store.request_for('b'))
                with self.assertRaises(ValueError):cleanup.delete(scanned['id'],['arbitrary-path'])
            store.db.close()

    def test_http_requires_administrator_and_csrf(self):
        from .test_shared import SharedTests
        fixture=SharedTests();fixture.setUp()
        try:
            import urllib.error
            browser=fixture.browser();user=fixture.request(browser,'/api/user/bootstrap')
            for path in ('/api/storage/scan','/api/storage/delete'):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    fixture.request(browser,path,{'days':7},user['csrf'])
                self.assertEqual(error.exception.code,401)
            login=fixture.request(browser,'/api/login',{'token':'demo'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                fixture.request(browser,'/api/storage/scan',{'days':7},'invalid')
            self.assertEqual(error.exception.code,403)
            fixture.app.demo=False
            from types import SimpleNamespace
            fixture.app.storage_cleanup=SimpleNamespace(scan=lambda days:{'days':days,'state':'completed'})
            result=fixture.request(browser,'/api/storage/scan',{'days':12},login['csrf'])
            self.assertEqual(result['days'],12)
        finally:fixture.tearDown()
    def test_gateway_never_exposes_cleanup_to_test_users(self):
        from .gateway import validate_request
        for name in ('scan','delete'):
            with self.assertRaises(ValueError):validate_request({'method':'POST','path':'/api/maintenance/storage/'+name,'body':{}},{})


if __name__=='__main__':unittest.main()
