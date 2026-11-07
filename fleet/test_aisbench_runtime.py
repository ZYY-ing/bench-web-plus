import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).parent / 'runner-overlay/scripts'


def runtime():
    spec = importlib.util.spec_from_file_location('runtime_adapter_tests', SCRIPTS / 'aisbench_runtime.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeAdapterTests(unittest.TestCase):
    def test_main_script_allocation_uses_private_storage_and_reader_can_open_it(self):
        # Native AISBench runs openicl_api_infer.py as __main__, not by import.
        # Exercise that path in a separate interpreter, including a full write
        # larger than a default Docker /dev/shm and an independent reader.
        with tempfile.TemporaryDirectory() as directory:
            code='''import os,sys,subprocess,json
sys.path.insert(0,sys.argv[1])
from aisbench_runtime import install_shared_memory_storage
from multiprocessing import shared_memory
install_shared_memory_storage()
m=shared_memory.SharedMemory(create=True,size=70*1024*1024)
assert m.name.startswith('fleetfile:')
for offset in range(0,m.size,1024*1024):m.buf[offset:offset+1024*1024]=b'x'*(1024*1024)
reader="import sys;sys.path.insert(0,sys.argv[1]);from aisbench_runtime import install_shared_memory_storage;install_shared_memory_storage();from multiprocessing import shared_memory;m=shared_memory.SharedMemory(sys.argv[2]);assert m.buf[-1]==120;m.close()"
subprocess.run([sys.executable,'-I','-S','-c',reader,sys.argv[1],m.name],check=True)
m.close();m.unlink();print('MAIN_SCRIPT_LARGE_STORAGE_OK')
'''
            env={**os.environ,'ASCEND_BENCH_HOME':directory,'ASCEND_BENCH_JOB_ID':'main-script-test'}
            result=subprocess.run([sys.executable,'-I','-S','-c',code,str(SCRIPTS.resolve())],env=env,capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('MAIN_SCRIPT_LARGE_STORAGE_OK',result.stdout)

    def test_warmup_output_does_not_mutate_formal_data(self):
        observed = []

        class Inferencer:
            async def warmup(self, data, count, concurrency):
                observed.extend(data)
                return {'success': count, 'failed': 0}

        module = types.SimpleNamespace(BaseApiInferencer=Inferencer, shared_memory=types.SimpleNamespace(SharedMemory=lambda **kwargs: None))
        runtime().patch_warmup(module)
        original = [{'prompt': 'first', 'max_out_len': 204800}, {'prompt': 'second', 'max_out_len': 204800}]
        with patch.dict(os.environ, AIS_BENCH_WARMUP_MAX_OUT_LEN='16'):
            result = asyncio.run(Inferencer().warmup(original, 2, 4))
        self.assertEqual(result['success'], 2)
        self.assertEqual([row['max_out_len'] for row in observed], [16, 16])
        self.assertEqual([row['max_out_len'] for row in original], [204800, 204800])

    def test_file_dataset_can_be_read_by_an_independent_worker(self):
        module = runtime()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, ASCEND_BENCH_HOME=directory, ASCEND_BENCH_JOB_ID='runtime-test'):
            storage = module.DatasetMemory(create=True, size=8192)
            try:
                storage.buf[:7] = b'payload'
                code = 'import sys,json;sys.path.insert(0,sys.argv[1]);from aisbench_runtime import DatasetMemory;m=DatasetMemory(sys.argv[2]);print(json.dumps(bytes(m.buf[:7]).decode()));m.close()'
                result = subprocess.run([sys.executable, '-I', '-S', '-c', code, str(SCRIPTS), storage.name], check=True, capture_output=True, text=True)
                self.assertEqual(json.loads(result.stdout), 'payload')
            finally:
                storage.close()
                storage.unlink()
            self.assertFalse(storage.path.exists())
