"""Generate per-machine restricted enrollment bundles, no personal key discovery."""
import base64
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import tarfile
import time
import uuid

HERE = Path(__file__).parent


def validate(body):
    fields = ('name', 'host', 'user', 'container', 'base')
    if any(not isinstance(body.get(k), str) for k in fields):
        raise ValueError('请填写机器名称、服务器、用户、容器和目录')
    spec = {k: body[k].strip() for k in fields}
    if not spec['name'] or len(spec['name']) > 80:
        raise ValueError('机器名称长度为 1–80 字符')
    for key, pattern in [('host', r'[a-zA-Z0-9][a-zA-Z0-9.-]*'), ('user', r'[a-zA-Z_][a-zA-Z0-9_-]*'), ('container', r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}')]:
        if not re.fullmatch(pattern, spec[key]):
            raise ValueError(key + ' 格式无效')
    if not re.fullmatch(r'/[a-zA-Z0-9_/-]+', spec['base']) or '//' in spec['base'] or spec['base'].endswith('/') or spec['base'] in ('/etc', '/usr', '/var', '/root'):
        raise ValueError('请使用专用 Linux 持久化目录，不带空格、点号或特殊字符')
    for key, default in [('port', 18787), ('ssh_port', 22), ('service_port', 8000)]:
        value = body.get(key, default)
        if type(value) is not int or not 1 <= value <= 65535:
            raise ValueError(key + ' 范围为 1–65535')
        spec[key] = value
    spec['service_hosts'] = [spec['host'], '127.0.0.1', 'localhost']
    spec['service_ports'] = [spec.pop('service_port')]
    tokenizer_root = body.get('tokenizer_root', '/models')
    if not isinstance(tokenizer_root, str) or not re.fullmatch(r'/[a-zA-Z0-9_/-]+', tokenizer_root) or '//' in tokenizer_root:
        raise ValueError('请填写专用 tokenizer 目录，不带空格或点号')
    spec['tokenizer_roots'] = [tokenizer_root]
    spec['ais_path'] = ''
    return spec


def generate(directory, archive, spec):
    bundle_id = uuid.uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    key_path = directory / 'identity'
    # Only creates a NEW tool-owned identity; never searches ~/.ssh or uses ssh-agent.
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'ascend-bench-' + bundle_id, '-f', str(key_path)], check=True, capture_output=True, timeout=15)
    key_path.chmod(0o600)
    public_key = ' '.join(key_path.with_suffix('.pub').read_text().split()[:2])
    manifest = {'id': bundle_id, 'spec': spec, 'public_key': public_key, 'expires_at': time.time() + 86400,
                'kit_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    output = directory / 'install-bundle.tar.gz'
    with tarfile.open(output, 'w:gz') as tar:
        def add(name, raw, mode=0o600):
            info = tarfile.TarInfo('ascend-bench-install/' + name)
            info.size, info.mode = len(raw), mode
            tar.addfile(info, io.BytesIO(raw))
        for name, source in [('install.py', 'install_bundle.py'), ('gateway.py', 'gateway.py'), ('remote_helper.py', 'remote_helper.py')]:
            add(name, (HERE / source).read_bytes().replace(b'\r\n', b'\n'))
        add('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        add('runner-kit.tar.gz', archive.read_bytes())
        add('install.sh', b'#!/usr/bin/env bash\nset -euo pipefail\ncd "$(dirname "$0")"\ncommand -v python3 >/dev/null || { echo "python3 is required"; exit 1; }\npython3 -u install.py 2>&1 | tee install.log\n', 0o700)
        add('README.txt', ('把此包传到网页填写的服务器，使用指定 SSH 用户执行：\n'
            'tar xzf install-bundle.tar.gz && bash ascend-bench-install/install.sh\n\n'
            '程序调用原 setup.sh 安装，并只添加专用受限公钥，不修改其他授权。\n'
            '安装后将 receipt-*.json 传回网页导入。私钥留在中心工具目录，不在本包中。\n'
            '接入包 24 小时内有效；最终 SSH 授权持续有效，直到执行输出的 revoke.py 撤销。\n').encode())
    return {'id': bundle_id, 'spec': spec, 'key_path': str(key_path.resolve()), 'archive': str(output.resolve()), 'expires_at': manifest['expires_at'],
            'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'state': 'generated'}


def known_hosts(receipt, spec):
    if receipt.get('host') != spec['host'] or receipt.get('user') != spec['user']:
        raise ValueError('回执的服务器/用户与生成接入包时不一致')
    keys = receipt.get('host_keys')
    if not isinstance(keys, list) or not 1 <= len(keys) <= 5:
        raise ValueError('服务器公钥缺失')
    host = spec['host'] if spec['ssh_port'] == 22 else '[' + spec['host'] + ']:' + str(spec['ssh_port'])
    lines = []
    for key in keys:
        if not isinstance(key, str) or not re.fullmatch(r'(ssh-ed25519|ecdsa-sha2-nistp(?:256|384|521)|ssh-rsa) [A-Za-z0-9+/=]{20,2000}', key):
            raise ValueError('服务器公钥格式无效')
        base64.b64decode(key.split()[1], validate=True)
        lines.append(host + ' ' + key)
    return '\n'.join(lines) + '\n'
