import importlib.util
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


class AsyncDatasetTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('async_dataset_runner',Path(__file__).parent/'runner-overlay/scripts/runner.py')
        self.runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.runner)
        self.tmp=tempfile.TemporaryDirectory();self.runner.BASE=Path(self.tmp.name)
        self.runner.CONFIG={'service':{'tokenizer_path':'/test-tokenizer'}}
        self.runner.CONNECTION={'id':'verified','fingerprint':self.runner.service_fingerprint(self.runner.CONFIG['service'])}
        self.body={'connection_id':'verified','input_tokens':131072,'count':2560,'max_out_len':1024,'prefix_ratio':.9,'seed':17}

    def tearDown(self):
        self.tmp.cleanup()

    def test_slow_generation_is_nonblocking_idempotent_and_releases_reservation(self):
        started=threading.Event();release=threading.Event();calls=[]
        def work(*args):
            calls.append(args);started.set();release.wait(5)
            return Path('/test/data.jsonl'),False
        with patch.object(self.runner,'dataset_worker',side_effect=work):
            before=time.monotonic();reply,status=self.runner.generation_response(self.body)
            self.assertEqual(status,202);self.assertLess(time.monotonic()-before,.5)
            self.assertTrue(started.wait(1));self.assertTrue(self.runner.LOCK.locked())
            self.assertEqual(self.runner.generation_response(self.body)[1],202)
            self.assertEqual(self.runner.generation_response({**self.body,'seed':18})[1],409)
            self.assertEqual(len(calls),1)
            release.set()
            for _ in range(100):
                if not self.runner.LOCK.locked():break
                time.sleep(.01)
            reply,status=self.runner.generation_response(self.body)
            self.assertEqual(status,200);self.assertEqual(reply['count'],2560)
            self.assertFalse(self.runner.LOCK.locked())

    def test_failed_worker_releases_reservation_and_preserves_error(self):
        with patch.object(self.runner,'dataset_worker',side_effect=ValueError('disk full')):
            self.runner.generation_response(self.body)
            for _ in range(100):
                if not self.runner.LOCK.locked():break
                time.sleep(.01)
            reply,status=self.runner.generation_response(self.body)
            self.assertEqual(status,400);self.assertEqual(reply['error'],'disk full')
            self.assertFalse(self.runner.LOCK.locked())
