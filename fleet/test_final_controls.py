"""Boundary and generated configuration checks for the final user requirements."""
import ast
import importlib.util
from pathlib import Path
import tempfile
import unittest


class FinalControlsTests(unittest.TestCase):
    def setUp(self):
        source=Path(__file__).parent/'runner-overlay/scripts/runner.py'
        spec=importlib.util.spec_from_file_location('final_controls_runner',source)
        self.runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.runner)
        self.service={'host':'192.0.2.2','port':8000,'model':'test','tokenizer_path':'/weights/test'}
        self.runner.CONFIG={'service':self.service}
        self.config={'mode':'performance','engine':'aisbench',**self.service,'max_out_len':225280,
          'batch_size':1000,'generation_kwargs':{'temperature':3.5,'ignore_eos':True},'stream':True,
          'api':{'endpoint':'chat'},'timeout':600,'retry':3,'sample_limit':1280,
          'warmup':{'enabled':True,'count':1280,'max_out_len':225280},
          'execution':{'mode':'perf','target':{'kind':'direct'}},
          'workload':{'input_tokens':131072,'count':1280,'seed':-12345,'prefix_ratio':1}}

    def test_220k_output_1000_concurrency_and_all_samples_warmup(self):
        self.assertEqual(self.runner.validate(self.config),1280)
        with tempfile.TemporaryDirectory() as directory:
            text=self.runner.make_config(self.config,Path(directory),'/test.jsonl')
        assignment=next(n for n in ast.parse(text).body if isinstance(n,ast.Assign) and n.targets[0].id=='models')
        model=ast.literal_eval(assignment.value)[0]
        self.assertEqual(model['batch_size'],1000)
        self.assertEqual(model['max_out_len'],225280)
        self.assertEqual(model['generation_kwargs'],{'temperature':3.5,'ignore_eos':True})
        self.assertEqual(model['retry'],3)

    def test_zero_warmups_and_nonstream_are_valid(self):
        self.config['warmup']['count']=0
        self.config['stream']=False
        self.assertEqual(self.runner.validate(self.config),1280)

    def test_native_timeout_default_and_explicit_custom_seconds(self):
        for timeout in (None, 1200):
            self.config['timeout'] = timeout
            self.runner.validate(self.config)
            self.assertEqual(self.runner.request_timeout_env(self.config), 'native' if timeout is None else '1200')
        self.config.pop('timeout')
        self.assertEqual(self.runner.request_timeout_env(self.config), 'native')
        for timeout in (0, -1, True, '600', float('nan'), float('inf'), 86401):
            self.config['timeout'] = timeout
            with self.assertRaisesRegex(ValueError, '请求超时'):
                self.runner.validate(self.config)

    def test_pressure_and_statistics_are_independent_in_all_four_combinations(self):
        for pressure in (False, True):
            for steady in (False, True):
                self.config['execution'].update(pressure=pressure, steady_state=steady, pressure_time=15)
                self.runner.validate(self.config)
                with tempfile.TemporaryDirectory() as directory:
                    text=self.runner.make_config(self.config,Path(directory),'/test.jsonl')
                node=next(n for n in ast.parse(text).body if isinstance(n,ast.Assign) and n.targets[0].id=='summarizer')
                result=ast.literal_eval(node.value)
                self.assertEqual(result['calculator']['type'].endswith('StablePerfMetricCalculator'),steady)

    def test_other_native_generation_and_model_options_reach_config(self):
        self.config['generation_kwargs']['guided_json']={'type':'object'}
        self.config['api'].update(enable_ssl=True, verbose=True, meta_template={'round':[]})
        self.runner.validate(self.config)
        with tempfile.TemporaryDirectory() as directory:
            text=self.runner.make_config(self.config,Path(directory),'/test.jsonl')
        node=next(n for n in ast.parse(text).body if isinstance(n,ast.Assign) and n.targets[0].id=='models')
        model=ast.literal_eval(node.value)[0]
        self.assertTrue(model['enable_ssl']);self.assertTrue(model['verbose'])
        self.assertEqual(model['generation_kwargs']['guided_json'],{'type':'object'})

    def test_excess_warmups_are_rejected_before_launch(self):
        self.config['warmup']['count']=1281
        with self.assertRaisesRegex(ValueError,'正式样本总数'):
            self.runner.validate(self.config)

    def test_warmup_summary_is_collected_from_actual_ais_task_log(self):
        self.config['warmup']={'enabled':True,'count':1,'max_out_len':16}
        record={'config':self.config}
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);output=folder/'output';task=output/'logs/infer/model/data.out'
            task.parent.mkdir(parents=True)
            task.write_text('[Runner] Warmup max_out_len=16; formal requests keep their configured output length\nWarmup finished Total Count: 1 Success Count: 1 Failed Count: 0\n')
            self.runner.collect_warmup(output,record,folder)
            self.assertEqual(record['warmup_result']['tasks'][0]['max_out_len'],16)
            self.assertFalse((folder/'execution.log').exists())
            task.write_text('[Runner] Warmup max_out_len=1024\nWarmup finished Total Count: 1 Success Count: 1 Failed Count: 0\n')
            self.runner.collect_warmup(output,record,folder)
            self.assertIn('预热摘要与配置',record['notices'][-1])

    def test_native_debug_warmup_is_verified_from_main_log_without_replay(self):
        self.config['warmup']={'enabled':True,'count':1,'max_out_len':16}
        self.config['execution']['debug']=True
        record={'config':self.config}
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);output=folder/'output';output.mkdir()
            native='[Runner] Warmup max_out_len=16\nWarmup finished Total Count: 1 Success Count: 1 Failed Count: 0\nProgress: 100% 2/2\nResults\n'
            (folder/'execution.log').write_text(native)
            self.runner.collect_warmup(output,record,folder)
            self.assertTrue(record['warmup_result']['performed'])
            self.assertEqual((folder/'execution.log').read_text(),native)
            (folder/'execution.log').write_text(native.replace('Success Count: 1 Failed Count: 0','Success Count: 0 Failed Count: 1'))
            self.runner.collect_warmup(output,record,folder)
            self.assertIn('预热摘要与配置',record['notices'][-1])

    def test_restart_restores_only_confirmed_terminal_request_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            self.runner.RUNS=Path(directory)
            for name,status in [('real-done','stopped'),('real-pending','unknown')]:
                path=Path(directory)/name/'record.json';path.parent.mkdir()
                path.write_text(__import__('json').dumps({'id':name,'status':status,'terminal_status':'stopped',
                    'fleet_request_id':name,'completed_at':'2026-11-05T09:00:00Z'}))
            self.runner.restore_finished_requests()
            self.assertTrue(self.runner.FLEET_REQUESTS['real-done']['restored_after_restart'])
            self.assertNotIn('real-pending',self.runner.FLEET_REQUESTS)
