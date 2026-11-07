import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).parent/'runner-overlay/scripts'
sys.path.insert(0,str(SCRIPTS))
from dataset_lock import CacheBusyError,cache_write_lock,cleanup_exited_child
sys.path.pop(0)


class CacheLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.folder=Path(self.tmp.name)/'cache'
    def holder(self):
        code="import sys;sys.path.insert(0,sys.argv[1]);from dataset_lock import cache_write_lock\nwith cache_write_lock(sys.argv[2],{'service':'fixture:8000','input_tokens':131072,'count':16,'request_id':'test-child'}):\n print('READY',flush=True);sys.stdin.read()\n"
        p=subprocess.Popen([sys.executable,'-I','-S','-c',code,str(SCRIPTS.resolve()),str(self.folder)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def cleanup():
            if p.poll() is None:p.terminate();p.wait(timeout=5)
            for stream in (p.stdin,p.stdout,p.stderr):stream.close()
        self.addCleanup(cleanup)
        self.assertEqual(p.stdout.readline().strip(),'READY');return p
    def test_other_process_same_directory_rejected_other_cache_allowed(self):
        self.holder()
        with self.assertRaises(CacheBusyError) as ctx:
            with cache_write_lock(self.folder):self.fail('Concurrent writer entered')
        self.assertEqual(ctx.exception.status,409);self.assertIn('fixture:8000',str(ctx.exception))
        with cache_write_lock(self.folder.parent/'independent'):pass
    def test_crashed_owner_releases_os_lock_without_deleting_lock_file(self):
        p=self.holder();lock=self.folder/'.dataset-write.lock'
        self.assertFalse(cleanup_exited_child(self.folder,'test-child',p))
        self.assertTrue(lock.is_file());p.terminate();p.wait(timeout=5)
        self.assertFalse(cleanup_exited_child(self.folder,'other-child',p))
        with self.assertRaises(CacheBusyError):
            with cache_write_lock(self.folder):pass
        self.assertTrue(cleanup_exited_child(self.folder,'test-child',p))
        with cache_write_lock(self.folder,{'service':'new:8000'}):
            self.assertTrue(lock.is_file())
    def test_prepare_cannot_touch_cache_while_another_writer_owns_it(self):
        p=self.holder();spec=importlib.util.spec_from_file_location('locked_cache_runner',SCRIPTS/'runner.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        m.CONFIG={'service':{'host':'fixture','port':8000}}
        c={'workload':{'input_tokens':131072},'max_out_len':1024}
        with patch.object(m,'generate_data') as generate,patch.object(m,'verify_dataset_cache') as verify:
            with self.assertRaises(CacheBusyError):m.prepare_perf_dataset(c,self.folder,16)
            generate.assert_not_called();verify.assert_not_called()
            p.terminate();p.wait(timeout=5)
            self.assertTrue(cleanup_exited_child(self.folder,'test-child',p))
            (self.folder/'data.jsonl').write_text('{}\n')
            self.assertEqual(m.prepare_perf_dataset(c,self.folder,16),(self.folder/'data.jsonl',True))
            verify.assert_called_once();generate.assert_not_called()

if __name__=='__main__':unittest.main()
