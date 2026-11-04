"""Project-owned TLS gateway. Only approved evaluation API, never remote shell."""
import hmac
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import threading
from gateway import forward
from http_server import BoundedServer


BASE = Path(__file__).resolve().parent
BUILD = hashlib.sha256(json.dumps({name: hashlib.sha256((BASE/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest() for name in ('gateway.py','persistent_gateway.py','http_server.py')},sort_keys=True).encode()).hexdigest()
CONFIG = {}
SLOTS = threading.BoundedSemaphore(16)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, status, value):
        value['_gateway_release'] = BUILD
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        supplied = self.headers.get('Authorization', '').encode()
        if self.path != '/request' or not hmac.compare_digest(supplied, ('Bearer ' + CONFIG['secret']).encode()):
            return self.respond(403, {'error': '接入凭据无效'})
        if not SLOTS.acquire(blocking=False):
            return self.respond(503, {'error': '查询繁忙，请稍后刷新'})
        try:
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.respond(400, {'error': '需要 JSON'})
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 100000:
                return self.respond(400, {'error': '请求大小无效'})
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError('需要对象')
            self.respond(200, forward(body, CONFIG['spec']))
        except Exception:
            self.respond(400, {'error': '请求未完成，请核对允许的参数及 Runner 状态'})
        finally:
            SLOTS.release()


if __name__ == '__main__':
    os.umask(0o077)
    CONFIG = json.loads((BASE / 'access.json').read_text())
    server = BoundedServer((CONFIG['bind'], CONFIG['port']), Handler)
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(BASE / 'cert.pem', BASE / 'key.pem')
    server.tls_context = context
    (BASE / 'gateway.pid').write_text(str(os.getpid()))
    server.serve_forever()
