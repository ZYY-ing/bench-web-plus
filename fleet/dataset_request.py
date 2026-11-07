"""Confirm a deterministic dataset after an ambiguous proxy read timeout.

Only the generate endpoint is replayable: the Runner serializes generation and
verifies its parameter-keyed cache before returning it. Never replay jobs here.
"""
import http.client
import json
import ssl
import time
import urllib.error


def _ambiguous_timeout(exc):
    if isinstance(exc, urllib.error.HTTPError):
        # The TLS gateway returns this exact envelope when its upstream wait
        # expires. Runner validation errors are ordinary response tuples instead.
        if exc.code != 400:
            return False
        try:
            envelope = json.loads(exc.read(4096))
        except (ValueError, OSError):
            return False
        return envelope.get('error') == '请求未完成，请核对允许的参数及 Runner 状态'
    if isinstance(exc, ssl.SSLError):
        return False
    if isinstance(exc, urllib.error.URLError):
        return _ambiguous_timeout(exc.reason)
    return isinstance(exc, (TimeoutError, http.client.RemoteDisconnected, ConnectionResetError))


def confirm_generation(request, *, budget=1800, interval=3, clock=time.monotonic, sleep=time.sleep):
    """Keep the original body unchanged and wait for its verified cached result.

    An initial busy response belongs to an existing task and is returned as-is.
    A busy response after a lost read can be our still-running generator. Hard
    validation/authentication errors are always returned immediately.
    """
    deadline = clock() + budget
    uncertain = False
    while True:
        try:
            result = request()
        except Exception as exc:
            if not _ambiguous_timeout(exc):
                raise
            uncertain = True
        else:
            if not (uncertain and result[0] == 409):
                return result
        remaining = deadline - clock()
        if remaining <= 0:
            raise ValueError('数据集生成结果尚未确认；后台生成不会因此取消，请稍后用相同参数再次确认缓存')
        sleep(min(interval, remaining))
