import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch


class Tokenizer:
    def encode(self, text, add_special_tokens=False):
        return list(range(len(text.split()) + (2 if add_special_tokens else 0)))

    def apply_chat_template(self, messages, **kwargs):
        return 'user ' + messages[0]['content'] + ' assistant'


class LargeDatasetTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).parent / 'runner-overlay/scripts/runner.py'
        spec = importlib.util.spec_from_file_location('large_dataset_runner', source)
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)
        self.directory = tempfile.TemporaryDirectory()
        self.folder = Path(self.directory.name)
        self.runner.CONFIG = {'service': {'tokenizer_path': '/test-tokenizer'}}

    def tearDown(self):
        self.directory.cleanup()

    def test_2560_full_prefix_rows_are_streamed_and_verified(self):
        config = {'workload': {'input_tokens': 64, 'prefix_ratio': 1, 'seed': 2}, 'max_out_len': 204800}
        transformers = types.SimpleNamespace(AutoTokenizer=types.SimpleNamespace(from_pretrained=lambda *a, **k: Tokenizer()))
        with patch.dict('sys.modules', transformers=transformers):
            path = self.runner.generate_data(config, self.folder, 2560)
        with path.open() as stream:
            rows = [json.loads(line) for line in stream]
        self.assertEqual(len(rows), 2560)
        self.assertEqual(len({row['question'] for row in rows}), 1)
        self.assertTrue(all(row['max_out_len'] == 204800 for row in rows))
        self.runner.verify_dataset_cache(self.folder, 2560, 64, 204800)
        with path.open('a') as stream:
            stream.write('{}\n')
        with self.assertRaisesRegex(ValueError, '额外条目'):
            self.runner.verify_dataset_cache(self.folder, 2560, 64, 204800)

    def test_chat_and_completions_have_distinct_dataset_caches(self):
        self.runner.BASE = self.folder
        a = self.runner.perf_dataset_dir(131072, 2560, 1024, prefix_ratio=.9, endpoint='chat')
        b = self.runner.perf_dataset_dir(131072, 2560, 1024, prefix_ratio=.9, endpoint='completions')
        self.assertNotEqual(a, b)

    def test_fractional_prefix_ratios_do_not_share_cache(self):
        self.runner.BASE = self.folder
        a=self.runner.perf_dataset_dir(131072,1280,1024,prefix_ratio=.9)
        b=self.runner.perf_dataset_dir(131072,1280,1024,prefix_ratio=.901)
        self.assertNotEqual(a,b)

    def test_prefix_ratio_is_measured_before_chat_wrapper(self):
        config={'workload':{'input_tokens':64,'prefix_ratio':.5,'seed':2},'max_out_len':1024}
        transformers=types.SimpleNamespace(AutoTokenizer=types.SimpleNamespace(from_pretrained=lambda *a,**k:Tokenizer()))
        with patch.dict('sys.modules',transformers=transformers):
            path=self.runner.generate_data(config,self.folder,2)
        rows=[json.loads(line)['question'] for line in path.read_text().splitlines()]
        self.assertEqual(rows[0].split()[:32],rows[1].split()[:32])
        self.assertTrue(all(self.runner._chat_len(Tokenizer(),row)==64 for row in rows))
