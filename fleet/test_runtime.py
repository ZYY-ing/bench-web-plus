import builtins
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).parent / 'runner-overlay/scripts'


class RuntimeTests(unittest.TestCase):
    def test_native_timeout_keeps_upstream_objects_custom_timeout_overrides(self):
        import types
        names = ('ais_bench', 'ais_bench.benchmark', 'ais_bench.benchmark.models',
                 'ais_bench.benchmark.models.api_models', 'ais_bench.benchmark.models.api_models.base_api')
        for value, expected in [('native', None), ('1200', 1200.0), ('None', None)]:
            modules = {name: types.ModuleType(name) for name in names}
            for name in names[1:]:
                parent, child = name.rsplit('.', 1)
                setattr(modules[parent], child, modules[name])
            constants = types.ModuleType('ais_bench.benchmark.global_consts')
            constants.REQUEST_TIME_OUT = None
            modules['ais_bench.benchmark.global_consts'] = constants
            modules['ais_bench.benchmark'].global_consts = constants
            base = modules[names[-1]]
            native_timeout = types.SimpleNamespace(total=None)
            base.AIOHTTP_TIMEOUT = native_timeout
            base.BaseAPIModel = type('Model', (), {})
            modules['aiohttp'] = types.SimpleNamespace(ClientTimeout=lambda **kw: types.SimpleNamespace(**kw))
            with patch.dict(sys.modules, modules), patch.dict(os.environ, ASCEND_BENCH_PROCESS_ROLE='test', AIS_BENCH_TOTAL_TIMEOUT=value):
                runpy.run_path(str(SCRIPTS / 'sitecustomize.py'))
            self.assertEqual(constants.REQUEST_TIME_OUT, expected)
            self.assertEqual(base.AIOHTTP_TIMEOUT.total, expected)
            if value == 'native':
                self.assertIs(base.AIOHTTP_TIMEOUT, native_timeout)

    def test_api_process_disables_optional_npu_import_without_removing_packages(self):
        with patch.dict(os.environ, ASCEND_BENCH_API_ONLY='1', ASCEND_BENCH_PROCESS_ROLE='runner'), patch.dict(sys.modules):
            sys.modules.pop('torch_npu', None)
            runpy.run_path(str(SCRIPTS / 'sitecustomize.py'))
            self.assertIsNone(sys.modules['torch_npu'])
            self.assertEqual(os.environ['TORCH_DEVICE_BACKEND_AUTOLOAD'], '0')
            with self.assertRaises(ModuleNotFoundError):
                __import__('torch_npu')

    def test_api_guard_does_not_unload_an_existing_native_backend(self):
        existing = object()
        with patch.dict(os.environ, ASCEND_BENCH_API_ONLY='1', ASCEND_BENCH_PROCESS_ROLE='runner'), patch.dict(sys.modules, torch_npu=existing):
            runpy.run_path(str(SCRIPTS / 'sitecustomize.py'))
            self.assertIs(sys.modules['torch_npu'], existing)

    def test_management_startup_does_not_import_benchmark_native_dependencies(self):
        real_import = builtins.__import__
        touched = []

        def guarded(name, *args, **kwargs):
            if name.startswith(('ais_bench', 'torch', 'aiohttp')):
                touched.append(name)
                raise AssertionError('Management process loaded benchmark dependencies')
            return real_import(name, *args, **kwargs)

        with patch.dict(os.environ, ASCEND_BENCH_PROCESS_ROLE='runner', AIS_BENCH_TOTAL_TIMEOUT='600'), patch('builtins.__import__', side_effect=guarded):
            runpy.run_path(str(SCRIPTS / 'sitecustomize.py'))
        self.assertEqual(touched, [])

    def test_job_process_retains_benchmark_patches(self):
        real_import = builtins.__import__
        touched = []

        def guarded(name, *args, **kwargs):
            if name.startswith('ais_bench'):
                touched.append(name)
                raise ImportError('Test has no AISBench installation')
            return real_import(name, *args, **kwargs)

        import types
        with patch.dict(sys.modules, aisbench_runtime=types.SimpleNamespace(install=lambda: None)), patch.dict(os.environ, ASCEND_BENCH_PROCESS_ROLE='aisbench', AIS_BENCH_TOTAL_TIMEOUT=''), patch('builtins.__import__', side_effect=guarded):
            runpy.run_path(str(SCRIPTS / 'sitecustomize.py'))
        self.assertIn('ais_bench.benchmark.models.api_models', touched)

    def test_healthy_runtime_does_not_change_process_security(self):
        spec = importlib.util.spec_from_file_location('runtime_compat_test', SCRIPTS / 'runtime_compat.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module.ctypes, 'CDLL', side_effect=AssertionError('Healthy runtime must stay untouched')):
            self.assertFalse(module.ensure_threads())

    def test_python_selection_prefers_complete_default_over_partial_newer_python(self):
        source = (SCRIPTS.parent / 'setup.sh').read_text(encoding='utf-8')
        selector = source.split("<<'PYTHON_SELECT'\n", 1)[1].split('\nPYTHON_SELECT', 1)[0]
        calls = []

        def candidate(argv, **kwargs):
            calls.append(argv)
            self.assertIn('import venv,ensurepip;', argv[-1])
            return subprocess.CompletedProcess(argv, 0, b'/default/python3\n', b'')

        with patch.object(sys, 'argv', ['select', '1']), patch('shutil.which', side_effect=lambda name: '/partial/' + name), patch('subprocess.run', side_effect=candidate), patch('builtins.print') as output:
            exec(compile(selector, 'setup-python-selector', 'exec'), {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], sys.executable)
        output.assert_called_once_with('/default/python3')

    def test_reinstall_probe_uses_verified_thread_compatibility_bootstrap(self):
        source = (SCRIPTS.parent.parent / 'direct_worker.py').read_text(encoding='utf-8')
        probe = source.split("probe_code = '''", 1)[1].split("'''", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            helper = base / 'scripts/runtime_compat.py'
            helper.parent.mkdir()
            helper.write_text('# reviewed bootstrap\n')
            ais = base / 'ais_bench'
            ais.write_text('# executable placeholder\n')
            ais.chmod(0o755)
            expected = {'scripts/runtime_compat.py': hashlib.sha256(helper.read_bytes()).hexdigest()}
            (base / 'runner-config.json').write_text(json.dumps({'container': 'own-runner', 'ais_bench_path': str(ais)}))
            with patch.object(sys, 'argv', ['probe', str(base), json.dumps(expected), 'a' * 64, 'own-runner']), patch('shutil.which', return_value=None), patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0)) as launch, patch('builtins.print') as output:
                exec(compile(probe, 'reinstall-probe', 'exec'), {})
            self.assertEqual(launch.call_args.args[0], [sys.executable, '-I', '-S', str(helper), str(ais), '--help'])
            self.assertEqual(json.loads(output.call_args.args[0])['ais_path'], str(ais))
