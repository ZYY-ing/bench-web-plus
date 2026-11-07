import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class ProgressRelayTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('ais_progress_test', Path(__file__).parent / 'runner-overlay/scripts/aisbench_progress.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.folder = self.base / 'results/jobs/real-progress'
        self.output = self.folder / 'output'
        self.exp = self.output / '20261105_120000'
        self.task = self.exp / 'logs/infer/model/gpqa.out'
        self.task.parent.mkdir(parents=True)
        self.status = self.exp / 'status_tmp/tmp_model_gpqa.json'
        self.status.parent.mkdir()
        self.record = {'status': 'running', 'output_root': str(self.output)}
        self.save()
        self.relay = self.module.Relay(self.base, 'real-progress')

    def save(self):
        (self.folder / 'record.json').write_text(json.dumps(self.record))

    def log(self):
        return (self.folder / 'execution.log').read_text(encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def test_native_warmup_and_question_logs_are_forwarded_once(self):
        native = '\rWarmup:  25%|██▌| 1/4 [00:01]\r'
        self.task.write_text(native, encoding='utf-8')
        self.relay.poll()
        first = self.log()
        self.assertIn('Warmup:  25%|██▌| 1/4', first)
        self.relay.poll()
        self.assertEqual(self.log(), first)
        with self.task.open('a', encoding='utf-8') as stream:
            stream.write('Warmup: 100%|████| 4/4\nInference: 10%|█| 2/20\n')
        self.relay.poll()
        self.assertIn('4/4', self.log())
        self.assertIn('2/20', self.log())

    def test_native_status_is_not_reformatted_or_modified(self):
        state = [{'task_name': 'model/gpqa', 'progress_description': 'Inferencing',
                  'finish_count': 7, 'total_count': 20, 'status': 'inferencing'}]
        self.status.write_text(json.dumps(state))
        self.relay.poll()
        self.assertFalse((self.folder / 'execution.log').exists())
        self.assertEqual(json.loads(self.status.read_text()), state)
        self.status.write_text('{partial')
        self.relay.poll()
        self.assertFalse((self.folder / 'execution.log').exists())
        self.status.write_text(json.dumps(state))
        self.relay.poll()
        self.assertFalse((self.folder / 'execution.log').exists())

    def test_original_failure_table_is_preserved_verbatim(self):
        native = 'Task finished, failed reasons summary:\n| HTTP 400 max_tokens=204800 cannot be greater than max_model_len=143360 | 18 |\n'
        self.task.write_text(native, encoding='utf-8')
        self.relay.poll()
        self.assertTrue(self.log().endswith(native))
        self.assertNotIn('[AISBench progress:', self.log())
        with self.task.open('a',encoding='utf-8') as stream:
            stream.write('original traceback\n')
        self.relay.poll()
        self.assertEqual(self.log().count('[Runner log source:'), 1)

    def test_reuse_does_not_replay_previous_inference(self):
        self.task.write_text('old warmup\n')
        self.record.update(reuse_directory=str(self.exp), previous_output_directories=[self.exp.name])
        self.save()
        self.relay.poll()
        self.assertFalse((self.folder / 'execution.log').exists())
        with self.task.open('a') as stream:
            stream.write('new output\n')
        self.relay.poll()
        self.assertNotIn('old warmup', self.log())
        self.assertIn('new output', self.log())

    def test_split_unicode_is_not_corrupted(self):
        text = 'a' * 65535 + '预热进度\n'
        self.task.write_text(text, encoding='utf-8')
        self.relay.poll()
        self.relay.poll()
        self.assertIn('预热进度', self.log())
        self.assertNotIn('\ufffd', self.log())

    def test_only_top_level_job_cli_starts_observer(self):
        environment = {'ASCEND_BENCH_HOME': str(self.base), 'ASCEND_BENCH_JOB_ID': 'real-progress'}
        for argv in [['ais_bench', '--help'], ['infer.py', str(self.folder / 'config.py')],
                     ['ais_bench', str(self.folder / 'config.py'), '--mode', 'all', '--debug']]:
            with patch.dict(self.module.os.environ, environment), patch.object(self.module.sys, 'argv', argv), patch.object(self.module.Relay, 'acquire') as acquire:
                self.module.start_for_cli()
                acquire.assert_not_called()
