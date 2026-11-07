import ast
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request


SOURCE = Path(__file__).parent/'runner-overlay/scripts/runner.py'
tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'read_log_tail')
namespace = {'os': os}
exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), 'exec'), namespace)
read_log_tail = namespace['read_log_tail']


class MeasuredPath:
    def __init__(self, path, truncate=False):
        self.path, self.truncate = path, truncate
        self.bytes_read, self.requests, self.offsets = 0, [], []

    def open(self, mode):
        assert mode == 'rb'
        owner = self
        stream = self.path.open(mode)
        class Reader:
            def __enter__(self): return self
            def __exit__(self, *args): stream.close()
            def fileno(self): return stream.fileno()
            def seek(self, offset):
                owner.offsets.append(offset)
                stream.seek(offset)
                if owner.truncate:
                    owner.truncate = False
                    owner.path.write_bytes(b'new log\n')
            def read(self, size):
                assert 0 <= size <= 800004, 'unbounded or oversized read'
                owner.requests.append(size)
                result = stream.read(size)
                owner.bytes_read += len(result)
                return result
        return Reader()


class LogTailTests(unittest.TestCase):
    def test_utf8_ansi_and_newlines_keep_previous_character_semantics(self):
        raw = ('头部😀\r\n\x1b[32mINFO\x1b[0m\rProgress: ███ 中文😀\n'*120).encode()+b'bad:\xff\xe4\xb8'
        expected = raw.decode('utf-8', errors='replace').replace('\r\n','\n').replace('\r','\n')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'execution.log';path.write_bytes(raw)
            for count in range(1, 170):
                with self.subTest(count=count):
                    self.assertEqual(read_log_tail(path, count), expected[-count:])
            self.assertEqual(read_log_tail(path), expected)

    def test_half_gib_log_reads_only_fixed_tail_and_does_not_modify_file(self):
        tail = ('原生日志😀\r\nPOST=130 FIN=20\n'*50000).encode()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'execution.log'
            with path.open('wb') as stream:
                stream.truncate(512*1024**2);stream.seek(0,2);stream.write(tail)
            before = path.stat();measured = MeasuredPath(path)
            expected = tail.decode().replace('\r\n','\n')[-200000:]
            self.assertEqual(read_log_tail(measured), expected)
            self.assertLessEqual(measured.bytes_read, 800004)
            self.assertEqual(len(measured.requests), 1)
            self.assertGreater(measured.offsets[0], 500*1024**2)
            after = path.stat()
            self.assertEqual((before.st_size,before.st_mtime_ns),(after.st_size,after.st_mtime_ns))

    def test_truncation_during_read_retries_with_another_bounded_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'execution.log';path.write_bytes(b'x'*1000)
            measured=MeasuredPath(path, truncate=True)
            self.assertEqual(read_log_tail(measured, 10), 'new log\n')
            self.assertEqual(len(measured.requests), 2)

    def test_missing_empty_and_zero_length(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'execution.log'
            self.assertEqual(read_log_tail(path), '')
            path.write_bytes(b'')
            self.assertEqual(read_log_tail(path), '')
            self.assertEqual(read_log_tail(path, 0), '')

    def test_web_and_terminal_record_use_bounded_reader(self):
        source=SOURCE.read_text(encoding='utf-8')
        self.assertIn("row['log'] = read_log_tail(log)", source)
        self.assertIn("record['log'] = read_log_tail(log_path)", source)
        self.assertNotIn("read_text(errors='replace')[-200000:]", source)

    def test_real_http_job_polls_read_only_tail_and_see_new_appends(self):
        spec=importlib.util.spec_from_file_location('tail_test_runner', SOURCE)
        runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
        with tempfile.TemporaryDirectory() as folder:
            runner.RUNS=Path(folder);runner.TOKEN='test-token'
            job=runner.RUNS/'real-123-test';job.mkdir()
            (job/'record.json').write_text(json.dumps({'id':job.name,'status':'running','log':''}))
            log=job/'execution.log'
            with log.open('wb') as stream:
                stream.truncate(512*1024**2);stream.seek(0,2);stream.write(b'x'*900000)
            measurements=[]
            def bounded(path, count=200000):
                measured=MeasuredPath(path);measurements.append(measured)
                return read_log_tail(measured,count)
            runner.read_log_tail=bounded
            class Handler(runner.Handler):
                def log_message(self, *args): pass
            server=runner.RunnerHTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def poll():
                port=server.server_port
                request=urllib.request.Request('http://127.0.0.1:'+str(port)+'/api/jobs/'+job.name,
                    headers={'Cookie':'runner_session_'+str(port)+'=test-token'})
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(request,timeout=3) as response:return json.load(response)
            try:
                with ThreadPoolExecutor(max_workers=3) as pool:
                    results=list(pool.map(lambda _:poll(),range(3)))
                self.assertTrue(all(r['log']=='x'*200000 and r['status']=='running' for r in results))
                self.assertEqual(sum(m.bytes_read for m in measurements),3*800004)
                with log.open('ab') as stream:stream.write('\n最新进度😀\n'.encode())
                self.assertTrue(poll()['log'].endswith('\n最新进度😀\n'))
                self.assertEqual(json.loads((job/'record.json').read_text())['status'],'running')
            finally:
                server.shutdown();server.server_close();thread.join(timeout=2)
