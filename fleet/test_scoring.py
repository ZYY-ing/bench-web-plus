import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch


class ScoringTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).parent / 'runner-overlay/scripts/runner.py'
        spec = importlib.util.spec_from_file_location('score_error_runner', source)
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)
        (self.folder / 'output/summary').mkdir(parents=True)
        self.record = {'config': {'mode': 'accuracy'}, 'count': 20}

    def tearDown(self):
        self.directory.cleanup()

    def test_empty_summary_reports_native_subprocess_failure(self):
        (self.folder / 'output/summary/summary.csv').write_text('dataset,metric,model\nGPQA_diamond,-,-\n')
        (self.folder / 'execution.log').write_text("Inconsistency detected by ld.so: dl-tls.c: 618: _dl_allocate_tls_init: Assertion `listp != NULL' failed!\nEvaluation tasks completed.\n")
        self.runner.collect(self.folder, self.record)
        self.assertTrue(any('子进程崩溃' in n and 'ld.so' in n for n in self.record['notices']))
        self.assertIsNone(self.record['metrics']['accuracy'])

    def test_warmup_failure_is_reported_before_missing_predictions(self):
        (self.folder / 'execution.log').write_text('Warmup finished Total Count: 3 Success Count: 0 Failed Count: 3\nNo predictions found.\n')
        self.runner.collect(self.folder, self.record)
        self.assertTrue(any('预热请求全部失败' in n for n in self.record['notices']))

    def test_legitimate_zero_accuracy_is_still_a_valid_score(self):
        result = self.folder / 'output/results/model/data.json'
        result.parent.mkdir(parents=True)
        result.write_text(json.dumps({'accuracy': 0, 'details': {'0': {'correct': [False]}}}))
        self.runner.collect(self.folder, self.record)
        self.assertEqual(self.record['metrics']['accuracy'], {'score_percent': 0, 'correct': 0, 'total': 1})

    def test_performance_length_differences_are_notices_not_failures(self):
        tables = [{'headers': ['Performance Parameters', 'Stage', 'Average', 'Min', 'Max'],
                   'rows': [['InputTokens', 'total', '16386.0', '16386.0', '16386.0']]}]
        config = {'workload': {'input_tokens': 16384}, 'max_out_len': 1024}
        self.assertIn('16386.0', self.runner.verify_performance_lengths(tables, config)[0])
        tables[0]['rows'][0][2:] = ['16384.0', '16383.0', '16385.0']
        self.assertIn('输入长度提示', self.runner.verify_performance_lengths(tables, config)[0])

    def test_output_eos_is_allowed_unless_fixed_output_requested(self):
        tables = [{'headers': ['Performance Parameters', 'Stage', 'Value'], 'rows': [['OutputTokens', 'total', 12]]}]
        config = {'max_out_len': 1024, 'generation_kwargs': {}}
        self.runner.verify_performance_lengths(tables, config)
        config['generation_kwargs']['ignore_eos'] = True
        self.assertIn('输出长度提示', self.runner.verify_performance_lengths(tables, config)[0])

    def test_native_measurements_and_failed_requests_are_preserved(self):
        perf = self.folder / 'output/performances/model'
        perf.mkdir(parents=True)
        native = 'Performance Parameters,Stage,Average,Min,Max\nInputTokens,total,131081.0,131081.0,131081.0\nOutputTokens,total,12,12,12\n'
        (perf / 'synthetic.csv').write_text(native)
        failed = self.folder / 'output/predictions/model/data_failed.jsonl'
        failed.parent.mkdir(parents=True)
        failed.write_text('{"id":1,"error_info":"TimeoutError"}\n')
        self.record['config'] = {'mode': 'performance', 'workload': {'input_tokens': 131072},
                                 'max_out_len': 1024, 'generation_kwargs': {'ignore_eos': True}}
        self.runner.collect(self.folder, self.record)
        self.assertEqual(self.record['failed_requests'], 1)
        self.assertEqual(self.record['raw_tables'][0]['rows'][0][2], '131081.0')
        self.assertEqual((perf / 'synthetic.csv').read_text(), native)
        self.assertTrue(any('输入长度提示' in n and '131081.0' in n for n in self.record['notices']))
        self.assertTrue(any('输出长度提示' in n for n in self.record['notices']))
        self.assertTrue(any('1 条失败请求' in n for n in self.record['notices']))

    def test_failed_jsonl_unicode_separators_are_not_extra_requests(self):
        failed = self.folder / 'output/predictions/model/data_failed.jsonl'
        failed.parent.mkdir(parents=True)
        native = ''.join(json.dumps({'id': i, 'input': 'question\u2028continued\u0085text\u2029end',
                                   'error_info': 'HTTP 500'}, ensure_ascii=False) + '\n'
                         for i in range(10))
        failed.write_text(native, encoding='utf-8')
        self.runner.collect(self.folder, self.record)
        self.assertEqual(self.record['failed_requests'], 10)
        self.assertTrue(any('10 条失败请求' in n for n in self.record['notices']))
        self.assertEqual(failed.read_text(encoding='utf-8'), native)

    def test_invalid_failed_jsonl_is_not_counted_as_a_native_record(self):
        failed = self.folder / 'output/predictions/model/data_failed.jsonl'
        failed.parent.mkdir(parents=True)
        failed.write_text('{"id": 1}\ninvalid JSON\n', encoding='utf-8')
        with self.assertRaises(json.JSONDecodeError):
            self.runner.collect(self.folder, self.record)

    def test_native_exit_code_controls_completion_even_if_report_reader_fails(self):
        runner = self.runner
        runner.BASE = self.folder
        runner.RUNS = self.folder / 'jobs'
        service = {'host': '192.0.2.1', 'port': 8000, 'model': 'test', 'tokenizer_path': '/test'}
        runner.CONFIG = {'service': service, 'ais_bench_path': '/fake/ais_bench'}
        config = {'engine': 'aisbench', 'mode': 'performance', **service, 'max_out_len': 16,
                  'batch_size': 1, 'generation_kwargs': {}, 'api': {'endpoint': 'chat'},
                  'workload': {'input_tokens': 64, 'count': 1}, 'sample_limit': 1,
                  'warmup': {'enabled': False, 'count': 0},
                  'execution': {'mode': 'perf', 'target': {'kind': 'direct'}}}
        for code, expected in ((0, 'completed'), (3, 'failed')):
            record = {'id': 'real-native-' + str(code), 'status': 'running', 'count': 1, 'config': config}
            (runner.RUNS / record['id']).mkdir(parents=True)
            process = MagicMock(pid=218)
            process.wait.return_value = code
            process.poll.return_value = code
            runner.LOCK.acquire()
            with patch.object(runner, 'get_perf_dataset', return_value=(self.folder/'data.jsonl', False)), \
                 patch.object(runner, 'ais_launch_problem', return_value=''), \
                 patch.object(runner, 'check_cli_flags'), \
                 patch.object(runner, 'collect', side_effect=OSError('report unavailable')), \
                 patch.object(runner.subprocess, 'Popen', return_value=process), \
                 patch.object(runner.os, 'killpg', create=True), \
                 patch.object(runner, 'wait_process_group_stopped', return_value=True):
                runner.run_job(record)
            self.assertEqual(record['status'], expected)
            self.assertEqual(record['exit_code'], code)
            if code == 0:
                self.assertIn('report unavailable', record['notices'][0])
            self.assertFalse(runner.LOCK.locked())
