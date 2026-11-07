"""Sitecustomize for the AISBench venv（每个使用 bench-venv 的 python 进程自动加载）。

1. 应用请求超时：设置共享会话读取的 REQUEST_TIME_OUT 及 base_api.AIOHTTP_TIMEOUT。
2. 报错详情补丁：ais_bench 默认在非 200 时只记 response.reason（如 "Bad Request"），
   把真正的报错正文丢掉。这里替换 BaseAPIModel.text_infer / stream_infer，
   非 200 时把响应正文（前 2000 字节）写进 output.error_info，
   让失败原因（如 "maximum context length is 32768 tokens"）直接出现在日志里。
"""
import os
import sys

# These processes only tokenize text and call remote HTTP APIs. MMEngine also
# explicitly imports torch_npu, bypassing PyTorch's backend-autoload flag. Mark
# that optional backend unavailable in this process before either library loads.
# Never change an already imported backend or the server's Python installation.
if os.environ.get('ASCEND_BENCH_API_ONLY') == '1':
    os.environ['TORCH_DEVICE_BACKEND_AUTOLOAD'] = '0'
    if 'torch_npu' not in sys.modules:
        sys.modules['torch_npu'] = None

_is_runner = os.environ.get('ASCEND_BENCH_PROCESS_ROLE') == 'runner'
if os.environ.get('ASCEND_BENCH_PROCESS_ROLE') == 'aisbench':
    from aisbench_runtime import install as _install_runtime
    _install_runtime()
    # Forward the unmodified AISBench task logs/status into the web log. This
    # observer makes no changes to inference, warmup, scheduling or evaluation.
    try:
        from aisbench_progress import start_for_cli as _start_progress
        _start_progress()
    except Exception as _progress_error:
        print('[Runner] AISBench log relay unavailable: ' + str(_progress_error), file=sys.stderr)
_t = None if _is_runner else os.environ.get('AIS_BENCH_TOTAL_TIMEOUT')
if _t and str(_t).lower() != 'native':
    try:
        _t = None if str(_t).lower() in ('none', 'null') else float(_t)
        import aiohttp
        from ais_bench.benchmark import global_consts as _global_consts
        # Shared warmup/inference sessions read this value at creation time.
        _global_consts.REQUEST_TIME_OUT = _t
        import ais_bench.benchmark.models.api_models.base_api as _b
        _b.AIOHTTP_TIMEOUT = aiohttp.ClientTimeout(total=_t)
    except Exception:
        pass

try:
    if _is_runner:
        raise ImportError('AISBench patches belong to job subprocesses, not the management server')
    from ais_bench.benchmark.models.api_models import base_api as _b

    async def _text_infer(self, request_body, output):
        await output.record_time_point()
        async with self.session.post(url=self.url, json=request_body, headers=self.headers) as response:
            if response.status == 200:
                raw_data = await response.text()
                await output.record_time_point()
                try:
                    data = _b.json.loads(raw_data)
                except Exception:
                    output.success = False
                    output.error_info = f'Unexpected response format: {raw_data}. Please check if server is working correctly.'
                    raise _b.AISBenchValueError(
                        _b.MODEL_CODES.PARSE_TEXT_RSP_INVALID_FORMAT,
                        'Unexpected response format. Please check ***_details.jsonl for more information.',
                    )
                await self.parse_text_response(data, output)
                self._record_response_anomaly_payload(data, output)
                output.success = True
            else:
                try:
                    _body = (await response.text())[:2000]
                except Exception:
                    _body = ''
                output.error_info = f'HTTP {response.status} {response.reason}: {_body}'
                output.success = False

    async def _stream_infer(self, request_body, output):
        await output.record_time_point()
        async with self.session.post(url=self.url, json=request_body, headers=self.headers) as response:
            if response.status == 200:
                async for raw_chunk in self.iter_lines(response.content):
                    chunk = raw_chunk.strip()
                    if not chunk:
                        continue
                    chunk = chunk.decode('utf-8')
                    if chunk.startswith(':') or chunk.startswith('id:'):
                        continue
                    chunk = chunk.removeprefix('data:').strip()
                    if chunk == '[DONE]':
                        break
                    await output.record_time_point()
                    try:
                        data = _b.json.loads(chunk)
                    except Exception:
                        output.success = False
                        output.error_info = f'Unexpected response format: {raw_chunk}. Please check if server is working correctly.'
                        raise _b.AISBenchValueError(
                            _b.MODEL_CODES.PARSE_TEXT_RSP_INVALID_FORMAT,
                            "Unexpected response format. Please check 'error_info' in ***_failed.jsonl for more information.",
                        )
                    await self.parse_stream_response(data, output)
                    self._accumulate_response_anomaly_payload(data, output)
                output.success = True
            else:
                try:
                    _body = (await response.text())[:2000]
                except Exception:
                    _body = ''
                output.error_info = f'HTTP {response.status} {response.reason}: {_body}'
                output.success = False

    _b.BaseAPIModel.text_infer = _text_infer
    _b.BaseAPIModel.stream_infer = _stream_infer
except Exception:
    pass
