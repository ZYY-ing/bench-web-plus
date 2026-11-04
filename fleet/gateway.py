"""Server-side SSH forced command: only forwards approved Runner HTTP requests.

Invoked by a dedicated restricted authorized_keys entry. Never interprets
SSH_ORIGINAL_COMMAND; no shell, port forwarding, filesystem API or Docker API.
"""
import base64
import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def validate_request(request, spec):
    method, path, body = request.get('method'), request.get('path'), request.get('body')
    allowed = (method == 'GET' and (path in ('/api/status', '/api/info', '/api/config', '/api/jobs') or isinstance(path, str) and re.fullmatch(r'/api/jobs/real-[a-z0-9-]+', path))) or (
        method == 'POST' and (path in ('/api/check-service', '/api/jobs', '/api/datasets/check', '/api/datasets/prepare', '/api/datasets/generate') or isinstance(path, str) and re.fullmatch(r'/api/jobs/real-[a-z0-9-]+/stop', path)))
    if not allowed:
        raise ValueError('该专用密钥只允许评测 API，不允许执行命令或修改程序配置')
    if method == 'POST' and not isinstance(body, dict):
        raise ValueError('需要 JSON 对象')
    if method == 'GET' and body is not None:
        raise ValueError('GET 请求不能包含 body')
    if method == 'POST' and path in ('/api/check-service', '/api/jobs'):
        service = body.get('service', {}) if path == '/api/check-service' else body
        allowed_ports = spec.get('service_ports')
        if not isinstance(service, dict) or service.get('host') not in spec['service_hosts'] or type(service.get('port')) is not int or not 1 <= service['port'] <= 65535 or allowed_ports is not None and service['port'] not in allowed_ports:
            raise ValueError('服务端点不在安装时批准的清单中')
        tokenizer = service.get('tokenizer_path', '')
        if not isinstance(tokenizer, str) or '..' in tokenizer.split('/') or not any(tokenizer.startswith(root.rstrip('/') + '/') or tokenizer == root for root in spec['tokenizer_roots']):
            raise ValueError('tokenizer 不在批准目录中')
        if body.get('execution', {}).get('target', {}).get('aisbench_path'):
            raise ValueError('禁止覆盖执行文件')
    return method, path, body


def forward(request, spec):
    method, path, body = validate_request(request, spec)
    # Token stays on the server; not present in the enrollment receipt or on the PC.
    token = (Path(spec['token_file']).read_text().strip() if spec.get('token_file') else
             subprocess.check_output(['docker', 'exec', spec['container'], 'cat', spec['base'] + '/runner-token'], stderr=subprocess.DEVNULL, timeout=15).decode().strip())
    headers = {'Host': '127.0.0.1:' + str(spec['port']), 'Cookie': 'runner_session_' + str(spec['port']) + '=' + token}
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers['Content-Type'] = 'application/json'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    req = urllib.request.Request('http://127.0.0.1:' + str(spec['port']) + path, data=data, headers=headers, method=method)
    try:
        response = opener.open(req, timeout=300)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read(4_000_001)
        if len(raw) > 4_000_000:
            raise ValueError('响应过大')
        return {'status': response.status, 'content_type': response.headers.get('Content-Type', 'application/json'), 'body': base64.b64encode(raw).decode()}


def main():
    try:
        raw = sys.stdin.buffer.read(100001)
        if len(raw) > 100000:
            raise ValueError('请求过大')
        spec = json.loads(Path(__file__).with_name('spec.json').read_text())
        result = forward(json.loads(raw), spec)
    except Exception:
        # Keep paths, process arguments and credentials out of SSH error responses.
        result = {'error': '受限接入请求失败，请检查允许的服务参数、容器和 Runner 状态'}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
