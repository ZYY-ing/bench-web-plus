import io
import json
import ssl
import unittest
import urllib.error
from unittest.mock import Mock

from .dataset_request import confirm_generation


class DatasetRequestTests(unittest.TestCase):
    def test_lost_read_then_busy_then_verified_cache(self):
        success = (200, 'application/json', b'{"reused":true,"count":2560}')
        request = Mock(side_effect=[TimeoutError(), (409, '', b'busy'), success])
        self.assertEqual(confirm_generation(request, sleep=lambda _: None), success)
        self.assertEqual(request.call_count, 3)

    def test_tls_gateway_lost_read_envelope(self):
        raw = json.dumps({'error': '请求未完成，请核对允许的参数及 Runner 状态'}).encode()
        error = urllib.error.HTTPError('https://gateway/request', 400, '', {}, io.BytesIO(raw))
        request = Mock(side_effect=[error, (200, '', b'cache')])
        self.assertEqual(confirm_generation(request, sleep=lambda _: None)[0], 200)

    def test_initial_busy_is_not_taken_over(self):
        request = Mock(return_value=(409, '', b'other task'))
        self.assertEqual(confirm_generation(request)[0], 409)
        request.assert_called_once()

    def test_validation_error_after_timeout_is_not_hidden(self):
        request = Mock(side_effect=[TimeoutError(), (400, '', b'bad connection')])
        self.assertEqual(confirm_generation(request, sleep=lambda _: None)[0], 400)
        self.assertEqual(request.call_count, 2)

    def test_auth_and_certificate_errors_are_not_retried(self):
        errors = [ssl.SSLCertVerificationError('certificate'),
                  urllib.error.URLError(ssl.SSLCertVerificationError('certificate')),
                  urllib.error.HTTPError('https://gateway', 403, '', {}, io.BytesIO(b'{}'))]
        for error in errors:
            with self.subTest(error=type(error)):
                request = Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    confirm_generation(request)
                request.assert_called_once()

    def test_wait_is_bounded_without_cancelling_generator(self):
        now = [0]
        def sleep(seconds):
            now[0] += seconds
        request = Mock(side_effect=TimeoutError())
        with self.assertRaisesRegex(ValueError, '后台生成不会因此取消'):
            confirm_generation(request, budget=5, interval=3, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(now[0], 5)

    def test_jobs_are_never_replayed_and_generation_body_is_frozen(self):
        from .app import Console
        class FakeConsole:
            proxy = Console.proxy
            def __init__(self):
                self.calls = []
            def _proxy_once(self, rid, method, path, body, owner):
                self.calls.append((path, dict(body)))
                body['seed'] = 999
                if len(self.calls) == 1:
                    raise TimeoutError()
                return (200, '', b'cache')
        from unittest.mock import patch
        fake = FakeConsole()
        with patch('fleet.dataset_request.time.sleep'):
            # Inject a zero-wait helper, while exercising the real route dispatch.
            with patch('fleet.app.confirm_generation', lambda request: confirm_generation(request, sleep=lambda _: None)):
                fake.proxy('runner', 'POST', '/api/datasets/generate', {'seed': 123})
        self.assertEqual([body['seed'] for _, body in fake.calls], [123, 123])
        fake = FakeConsole()
        with self.assertRaises(TimeoutError):
            fake.proxy('runner', 'POST', '/api/jobs', {'seed': 123})
        self.assertEqual(len(fake.calls), 1)
