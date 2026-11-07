import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace


class FixedModesTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('fixed_modes',Path(__file__).parent/'runner-overlay/scripts/runner.py')
        self.runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.runner)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.runner.BASE=Path(self.tmp.name).resolve()
        self.folder=self.runner.BASE/'results/jobs/new'

    def test_only_accuracy_all_and_performance_perf(self):
        for kind,mode in (('accuracy','all'),('performance','perf')):
            c={'mode':kind,'execution':{'mode':mode}}
            self.assertEqual(self.runner.validate_execution_mode(c),mode)
            self.assertEqual(self.runner.output_options(c,self.folder),(self.folder/'output',''))

    def test_reject_other_modes_before_dataset_work(self):
        for kind in ('accuracy','performance'):
            for mode in ('all','perf','infer','eval','viz','perf_viz','judge'):
                if (kind,mode) in (('accuracy','all'),('performance','perf')):continue
                with self.subTest(kind=kind,mode=mode):
                    c={'mode':kind,'execution':{'mode':mode}}
                    with self.assertRaisesRegex(ValueError,'仅支持'):self.runner.output_options(c,self.folder)
    def test_native_reuse_is_preserved(self):
        exp=self.runner.BASE/'saved-output/20261107_032843';exp.mkdir(parents=True)
        for kind,mode in (('accuracy','all'),('performance','perf')):
            c={'mode':kind,'execution':{'mode':mode,'reuse':str(exp)}}
            self.assertEqual(self.runner.output_options(c,self.folder),(exp.parent,exp.name))

    def test_invalid_mode_rejection_leaves_receipt_for_center_unlock(self):
        rid='f'*32;raw=json.dumps({'mode':'performance','execution':{'mode':'perf_viz'},'fleet_request_id':rid}).encode();responses=[]
        handler=SimpleNamespace(path='/api/jobs',headers={'Content-Type':'application/json','Content-Length':str(len(raw))},rfile=io.BytesIO(raw),allowed_origin=lambda:True,authed=lambda:True,json_response=lambda body,status=200:responses.append((status,body)))
        self.runner.Handler.do_POST(handler)
        self.assertEqual(responses[0][0],400)
        self.assertEqual(self.runner.FLEET_REQUESTS[rid]['state'],'rejected')
        self.assertFalse(self.runner.LOCK.locked())

if __name__=='__main__':unittest.main()
