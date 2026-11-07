import importlib.util
from pathlib import Path
import types
import unittest


def runtime():
    path = Path(__file__).parent / 'runner-overlay/scripts/aisbench_runtime.py'
    spec = importlib.util.spec_from_file_location('http_runtime_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Payload:
    def __init__(self, eof=False, error=None):
        self.eof, self.error, self.delivered = eof, error, []

    def is_eof(self):
        return self.eof

    def exception(self):
        return self.error

    def set_exception(self, error, *cause):
        self.error = error
        self.delivered.append((error, cause))


class DisconnectTests(unittest.TestCase):
    def handler(self, payload):
        class Handler:
            def set_exception(self, error, *args, **kwargs):
                self.error = error

            def connection_lost(self, error):
                self._payload = None
        module = types.SimpleNamespace(ResponseHandler=Handler)
        runtime().patch_http_disconnect(module)
        handler = Handler()
        handler._payload = payload
        return module, handler

    def test_parser_error_delivers_original_exception_and_cause(self):
        payload = Payload()
        _, handler = self.handler(payload)
        error, cause = ValueError('invalid HTTP chunk'), OSError('original')
        handler.set_exception(error, cause)
        self.assertEqual(payload.delivered, [(error, (cause,))])

    def test_connection_loss_wakes_pending_reader(self):
        payload = Payload()
        _, handler = self.handler(payload)
        error = ConnectionResetError('peer reset')
        handler.connection_lost(error)
        self.assertEqual(payload.delivered, [(error, ())])

    def test_successful_eof_and_existing_error_are_preserved(self):
        for payload in (Payload(eof=True), Payload(error=ValueError('already delivered'))):
            _, handler = self.handler(payload)
            handler.set_exception(OSError('later'))
            handler.connection_lost(None)
            self.assertEqual(payload.delivered, [])

    def test_hook_is_idempotent_and_does_not_change_retry_or_timeout(self):
        module, handler = self.handler(Payload())
        error_method, lost_method = module.ResponseHandler.set_exception, module.ResponseHandler.connection_lost
        runtime().patch_http_disconnect(module)
        self.assertIs(error_method, module.ResponseHandler.set_exception)
        self.assertIs(lost_method, module.ResponseHandler.connection_lost)
        self.assertNotIn('retry', module.ResponseHandler.__dict__)
        self.assertNotIn('timeout', module.ResponseHandler.__dict__)
