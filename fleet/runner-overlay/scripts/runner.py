"""Loopback-only, same-origin real AISBench runner. Never accepts Python or shell source."""
import argparse
import csv
import hashlib
import io
import http.cookies
import json
import math
import os
from pathlib import Path
import re
import random
import secrets
import shlex
import signal
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from urllib.parse import urlparse

SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
BASE = Path(os.environ.get('ASCEND_BENCH_HOME', '/home/ascend_bench/runner')).resolve()
RUNS = BASE / 'results/jobs'
CONFIG_PATH = BASE / 'runner-config.json'
CONFIG = {}
CONNECTION = None
CONNECTION_REVISION = 0
LOCK = threading.Lock()
PROCESSES = {}
CANCELLED = set()
TOKEN = ''
PORT = int(os.environ.get('ASCEND_BENCH_PORT', '18787'))
INSTANCE_ID = secrets.token_hex(16)
ACTIVE_JOB = None
SAFE_IDLE = True
FLEET_REQUESTS = {}
STATE_LOCK = threading.Lock()
GENERATION = None
GENERATION_LOCK = threading.Lock()
OPERATION = None


def operation_info(kind, service=None, **details):
    service = service if isinstance(service, dict) and service else CONFIG.get('service', {})
    return {'kind': kind, 'service': f"{service.get('host', '?')}:{service.get('port', '?')}", 'model': service.get('model', ''), **details}


def reserve_operation(kind, service=None, **details):
    global OPERATION
    info = operation_info(kind, service, **details)
    if not LOCK.acquire(blocking=False):
        return False
    OPERATION = info
    return True


def release_operation():
    global OPERATION
    OPERATION = None
    LOCK.release()


def busy_message():
    info = OPERATION or {}
    if ACTIVE_JOB:
        c = ACTIVE_JOB['config']
        return f"当前 Runner 正在执行任务 {ACTIVE_JOB['id']}，目标服务 {c.get('host', '?')}:{c.get('port', '?')}；请等待完成"
    if info.get('kind') == 'generation':
        return f"当前 Runner 正在为服务 {info['service']} 生成性能数据集：输入 {info.get('input_tokens')} tokens、输出 {info.get('max_out_len')} tokens、{info.get('count')} 条、共享前缀 {info.get('prefix_ratio', 0):.0%}；请等待完成"
    if info:
        return f"当前 Runner 正在{info.get('label', '检查连接')}，服务 {info['service']}；请稍后重试"
    return '当前 Runner 有任务执行中，请等待完成'


class RunnerHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # This server is loopback-only. HTTPServer's reverse DNS lookup is not
        # needed and can stall server_activate() on isolated container networks.
        TCPServer.server_bind(self)
        self.server_name = 'localhost'
        self.server_port = self.server_address[1]


def detect_lan_ip():
    import socket
    for target in ('8.8.8.8', '114.114.114.114'):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((target, 80))
                ip = sock.getsockname()[0]
            if ip and not ip.startswith('127.'):
                return ip
        except OSError:
            continue
    return socket.gethostbyname(socket.gethostname())


def ais_launch_problem(path):
    executable = Path(path)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        return f'AISBench 可执行文件不存在或不可执行：{path}'
    try:
        with executable.open('rb') as stream:
            header = stream.readline(4096)
        if header.startswith(b'#!'):
            parts = shlex.split(header[2:].decode('utf-8').strip())
            if not parts or not Path(parts[0]).is_file() or not os.access(parts[0], os.X_OK):
                interpreter = parts[0] if parts else '(空)'
                return f'AISBench 启动解释器不可用：{interpreter}；请重新部署 Runner，自动修复旧虚拟环境'
    except (OSError, ValueError, UnicodeError):
        return f'无法读取 AISBench 启动入口：{path}'
    return ''


def check_cli_flags(ais_bin, argv, env):
    result = subprocess.run([ais_bin, '--help'], cwd=str(BASE), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
    if result.returncode:
        raise ValueError('AISBench 参数预检失败：' + result.stderr.decode(errors='replace')[-1000:])
    text = result.stdout.decode(errors='replace')
    missing = [arg for arg in argv if arg.startswith('--') and arg not in text]
    if missing:
        raise ValueError('所选 AISBench 环境不支持这些参数：' + ', '.join(missing) + '；请更新该 Runner 的 AISBench 环境')


def cleanup_dataset_memory(job_id):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', job_id):
        return
    root = BASE / 'tmp' / 'aisbench-memory' / job_id
    if not root.is_dir() or root.resolve() != root:
        return
    for path in root.iterdir():
        if re.fullmatch(r'[a-f0-9]{32}', path.name) and path.is_file() and not path.is_symlink():
            path.unlink(missing_ok=True)
    try:
        root.rmdir()
    except OSError:
        pass


def detect_ais_bench():
    """Prefer an ais_bench installed in the container itself, fall back to the project venv."""
    import shutil
    candidates = [
        shutil.which('ais_bench'),
        '/usr/local/python3.12.13/bin/ais_bench',
        '/usr/local/bin/ais_bench',
        str(BASE / 'bench-venv/bin/ais_bench'),
    ]
    for candidate in candidates:
        if candidate and not ais_launch_problem(candidate):
            return candidate
    return str(BASE / 'bench-venv/bin/ais_bench')


def default_config():
    # 服务信息留空：部署工具包不再要求命令行传参，
    # 用户在网站的「① 连接推理服务」面板填写后点「检查连接」保存。
    return {
        'service': {
            'host': detect_lan_ip(),
            'port': 8000,
            'model': '',
            'tokenizer_path': '',
        },
        'ais_bench_path': detect_ais_bench(),
        'gsm8k_path': str(BASE / 'datasets/gsm8k'),
        'container': os.environ.get('ASCEND_BENCH_CONTAINER', ''),
    }


def validate_config(cfg, require_service=False):
    if not isinstance(cfg, dict):
        raise ValueError('配置格式错误')
    service = cfg.get('service') or {}
    host = str(service.get('host', '')).strip()
    port = service.get('port')
    model = str(service.get('model', '')).strip()
    tokenizer_path = str(service.get('tokenizer_path', '')).strip()
    if not host or '/' in host or ':' in host or host.startswith('.'):
        raise ValueError('服务 IP / Host 不合法（纯 IP 或主机名，不带协议和端口）')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('端口范围 1–65535')
    if model and re.search(r'[\s/]', model):
        raise ValueError('模型名（served-model-name）不合法')
    if require_service and not model:
        raise ValueError('请填写模型名称（served-model-name，可用 curl <IP>:<端口>/v1/models 查看）')
    if tokenizer_path and not tokenizer_path.startswith('/'):
        raise ValueError('tokenizer 路径必须是 Linux 绝对路径')
    if require_service and not tokenizer_path:
        raise ValueError('请填写 Runner 所在环境可见的 tokenizer / 权重路径')
    if tokenizer_path and not Path(tokenizer_path).is_dir():
        raise ValueError(f'tokenizer 路径不存在：{tokenizer_path}')
    ais_path = str(cfg.get('ais_bench_path', '')).strip() or detect_ais_bench()
    if not ais_path.startswith('/'):
        raise ValueError('AISBench 路径必须是 Linux 绝对路径')
    problem = ais_launch_problem(ais_path)
    if problem:
        raise ValueError(problem)
    gsm8k_path = str(cfg.get('gsm8k_path', '')).strip()
    if not gsm8k_path.startswith('/'):
        raise ValueError('GSM8K 数据路径必须是 Linux 绝对路径')
    container = str(os.environ.get('ASCEND_BENCH_CONTAINER') or cfg.get('container', '')).strip()
    if container and not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', container):
        raise ValueError('容器名不合法')
    return {
        'service': {'host': host, 'port': port, 'model': model, 'tokenizer_path': tokenizer_path},
        'ais_bench_path': ais_path,
        'gsm8k_path': gsm8k_path,
        'container': container,
    }


def set_config(cfg):
    global CONFIG, CONNECTION, CONNECTION_REVISION
    CONFIG = cfg
    CONNECTION = None
    CONNECTION_REVISION += 1
    save(CONFIG_PATH, cfg)
    return cfg


def load_config():
    global CONFIG
    cfg = default_config()
    if CONFIG_PATH.is_file():
        try:
            saved = json.loads(CONFIG_PATH.read_text())
            if isinstance(saved.get('service'), dict):
                cfg['service'].update({k: v for k, v in saved['service'].items() if v})
            for key in ('ais_bench_path', 'gsm8k_path', 'container'):
                if saved.get(key):
                    cfg[key] = saved[key]
        except (ValueError, OSError):
            pass
    # Deployment identity belongs to the current process, not a previous
    # container that happened to mount this persistent project directory.
    if os.environ.get('ASCEND_BENCH_CONTAINER'):
        cfg['container'] = os.environ['ASCEND_BENCH_CONTAINER']
    CONFIG = cfg
    save(CONFIG_PATH, cfg)
    return cfg

def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)

def read_log_tail(path, max_chars=200000):
    """Keep the existing character tail without reading an unbounded log.

    UTF-8 needs at most four bytes per character. Four spare bytes keep a
    partial leading code point outside the returned tail. Normalize newlines
    just as Path.read_text() did. Each read is bounded even during truncation.
    """
    if max_chars <= 0:
        return ''
    limit = 4 * max_chars + 4
    try:
        with path.open('rb') as stream:
            offset = max(0, os.fstat(stream.fileno()).st_size - limit)
            stream.seek(offset)
            raw = stream.read(limit)
            if offset and not raw:  # The writer truncated after our size check.
                stream.seek(max(0, os.fstat(stream.fileno()).st_size - limit))
                raw = stream.read(limit)
    except FileNotFoundError:
        return ''
    return raw.decode('utf-8', errors='replace').replace('\r\n', '\n').replace('\r', '\n')[-max_chars:]


def read_job(job_id):
    if not job_id.startswith('real-') or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in job_id):
        raise ValueError('Invalid job id')
    return json.loads((RUNS / job_id / 'record.json').read_text())

DATASETS = {
    'GSM8K': dict(
        subdir='gsm8k', required='test.jsonl', licensed=False,
        download={
            'test.jsonl': 'https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/test.jsonl',
            'train.jsonl': 'https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/train.jsonl',
        },
        config_import='from ais_bench.benchmark.configs.datasets.gsm8k.gsm8k_gen_0_shot_cot_chat_prompt import gsm8k_datasets as datasets'),
    'GPQA': dict(
        subdir='gpqa', required='gpqa_diamond.csv', licensed=True, download={},
        zip='http://opencompass.oss-cn-shanghai.aliyuncs.com/datasets/data/gpqa.zip',
        config_import='from ais_bench.benchmark.configs.datasets.gpqa.gpqa_gen_0_shot_cot_chat_prompt import gpqa_datasets as datasets'),
    'AIME2026': dict(
        subdir='aime2026', required='aime2026.jsonl', licensed=False, download={},
        config_import='from ais_bench.benchmark.configs.datasets.aime2026.aime2026_gen_0_shot_chat_prompt import aime2026_datasets as datasets'),
}

DATASET_ALIASES = {'GPQA Diamond': 'GPQA', 'GPQA_diamond': 'GPQA'}

def canonical_dataset(name):
    return DATASET_ALIASES.get(name, name)

def service_fingerprint(service):
    return json.dumps(service, sort_keys=True, ensure_ascii=False)

def require_connection(connection_id):
    if not CONNECTION or connection_id != CONNECTION['id'] or CONNECTION['fingerprint'] != service_fingerprint(CONFIG['service']):
        raise ValueError('服务连接未验证或配置已变化，请先重新检查连接；任务未启动')

def probe_service(service):
    root = f"http://{service['host']}:{service['port']}"
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    # /health 404 表示服务没有健康检查端点（如 P/D 负载均衡代理只暴露 /v1/models），视为可接受；
    # 其他错误（连接拒绝/超时/500）仍按失败处理，不静默放过。
    try:
        with opener.open(root + '/health', timeout=5) as response:
            if response.status != 200:
                raise ValueError(f'服务健康检查未通过：HTTP {response.status}')
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise ValueError(f'服务健康检查失败：HTTP {exc.code}')
    with opener.open(root + '/v1/models', timeout=5) as response:
        models = json.load(response)
    names = [row.get('id') for row in models.get('data', [])]
    if service['model'] not in names:
        raise ValueError('模型名称不匹配；服务实际提供：' + ', '.join(map(str, names)))
    return models


def dataset_dir(name):
    name = canonical_dataset(name)
    return BASE / 'datasets' / DATASETS[name]['subdir']


def check_dataset(name):
    name = canonical_dataset(name)
    spec = DATASETS.get(name)
    if spec is None:
        return {'name': name, 'state': 'unadapted'}
    d = dataset_dir(name)
    target = d / spec['required']
    if target.is_file():
        if name == 'GPQA':
            with target.open(encoding='utf-8-sig', newline='') as stream:
                reader = csv.DictReader(stream)
                required = {'Question', 'Correct Answer', 'Incorrect Answer 1', 'Incorrect Answer 2', 'Incorrect Answer 3'}
                if not required.issubset(reader.fieldnames or []):
                    return {'name': name, 'state': 'invalid', 'path': str(d), 'file': spec['required'], 'error': 'GPQA CSV 缺少题目／正确答案／三个错误答案列'}
                rows = list(reader)
                if not rows or any(not str(row.get(key, '')).strip() for row in rows for key in required):
                    return {'name': name, 'state': 'invalid', 'path': str(d), 'file': spec['required'], 'error': 'GPQA CSV 为空或存在缺失答案的样本'}
                count = len(rows)
        else:
            count = sum(1 for _ in target.open()) if target.suffix == '.jsonl' else None
        return {'name': name, 'state': 'ready', 'path': str(d), 'file': spec['required'], 'size': count, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
    return {'name': name, 'state': 'missing', 'path': str(d), 'file': spec['required'], 'downloadable': bool(spec['download'])}


def prepare_dataset(name):
    name = canonical_dataset(name)
    if name not in DATASETS:
        raise ValueError('尚未适配的数据集：' + str(name))
    spec = DATASETS[name]
    d = dataset_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    for fname, url in spec['download'].items():
        target = d / fname
        if target.is_file():
            continue
        request = urllib.request.Request(url, headers={'User-Agent': 'Ascend-Bench-Runner'})
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read(60 * 1024 * 1024)
        if fname.endswith('.jsonl'):
            rows = [json.loads(line) for line in raw.decode().splitlines() if line]
            if not rows or 'question' not in rows[0] or 'answer' not in rows[0]:
                raise ValueError('Invalid dataset file: ' + fname)
        tmp = target.with_suffix('.download')
        tmp.write_bytes(raw)
        tmp.replace(target)
    result = check_dataset(name)
    if result['state'] != 'ready' and spec.get('zip'):
        # 授权数据：尝试从公开镜像下载 zip 并解出所需 csv
        import zipfile
        request = urllib.request.Request(spec['zip'], headers={'User-Agent': 'Ascend-Bench-Runner'})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read(64 * 1024 * 1024)
        except Exception as exc:
            raise ValueError(f"自动下载 {name} 失败（源暂不可用，可稍后重试或手动放置 {spec['required']} 到 {d}）：{exc}")
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for member in zf.namelist():
                if member.endswith('/' + spec['required']) or member == spec['required']:
                    (d / spec['required']).write_bytes(zf.read(member))
                    break
            else:
                raise ValueError(f"zip 中未找到 {spec['required']}")
        result = check_dataset(name)
    if result['state'] != 'ready':
        if result.get('error'):
            if result.get('status') == 409:
                from dataset_lock import CacheBusyError
                raise CacheBusyError(result['error'])
            raise ValueError(result['error'])
        if spec['licensed']:
            raise ValueError(f"需要授权文件 {spec['required']}：请将其放置到服务器 {d} 后点重新检查")
        raise ValueError(f"缺少 {spec['required']}：工具包应携带该文件，也可手动放置到 {d}")
    digest = hashlib.sha256((d / spec['required']).read_bytes()).hexdigest()
    result['sha256'] = digest
    return result


def perf_dataset_dir(length, count, max_out, seed=20261027, prefix_ratio=0, endpoint='chat'):
    tokenizer = Path(CONFIG['service']['tokenizer_path'])
    stamp = str(tokenizer) + ':' + endpoint + ':prefix=' + repr(float(prefix_ratio))
    for name in ('tokenizer.json', 'tokenizer_config.json'):
        path = tokenizer / name
        if path.is_file():
            stamp += f'/{name}:{path.stat().st_size}:{path.stat().st_mtime_ns}'
    tag = hashlib.sha256(stamp.encode()).hexdigest()[:12]
    pfx = f'_pfx{int(round(prefix_ratio * 100))}' if prefix_ratio else ''
    return BASE / 'datasets' / 'perf' / f'v4_in{length}_req{count}_out{max_out}_seed{seed}{pfx}_{tag}'


def get_perf_dataset(c, count):
    """Keep tokenization/cache scans out of the HTTP management process."""
    workload = c.get('workload') or {}
    count = max(count, workload.get('count') or count)
    folder = perf_dataset_dir(workload['input_tokens'], count, c['max_out_len'], workload.get('seed', 20261027), workload.get('prefix_ratio', 0), c.get('api', {}).get('endpoint', 'chat'))
    return dataset_worker(c, folder, count)


def dataset_worker(c, folder, count):
    stage = BASE / 'tmp' / ('dataset-' + secrets.token_hex(12))
    stage.mkdir(parents=True)
    request = {'config': {key: c[key] for key in ('workload', 'max_out_len', 'api')}, 'service': CONFIG['service'].copy(), 'folder': str(folder), 'count': count, 'base': str(BASE)}
    request['config']['api'] = {key: c.get('api', {}).get(key) for key in ('endpoint', 'trust_remote_code')}
    save(stage / 'request.json', request)
    env = os.environ.copy()
    env.update(ASCEND_BENCH_PROCESS_ROLE='runner', ASCEND_BENCH_API_ONLY='1', TORCH_DEVICE_BACKEND_AUTOLOAD='0')
    try:
        with (stage / 'worker.log').open('w') as log:
            process = subprocess.Popen([sys.executable, '-u', str(BASE / 'scripts/dataset_worker.py'), str(stage)], env=env,
                                       stdout=log, stderr=subprocess.STDOUT)
            while process.poll() is None:
                if GENERATION and (stage / 'progress.json').is_file():
                    try:
                        GENERATION['progress'] = json.loads((stage / 'progress.json').read_text())
                    except (OSError, ValueError):
                        pass
                time.sleep(.2)
        result_path = stage / 'result.json'
        if not result_path.is_file():
            raise RuntimeError('数据集生成子进程退出：' + read_log_tail(stage / 'worker.log', 1500))
        result = json.loads(result_path.read_text())
        if result.get('error'):
            if result.get('status') == 409:
                from dataset_lock import CacheBusyError
                raise CacheBusyError(result['error'])
            raise ValueError(result['error'])
        return Path(result['path']), result['reused']
    finally:
        # This directory belongs solely to the child just waited above.
        if 'process' not in locals() or process.poll() is not None:
            if 'process' in locals():
                from dataset_lock import cleanup_exited_child
                cleanup_exited_child(folder, stage.name, process)
            shutil.rmtree(stage)


def prepare_perf_dataset(c, folder, count, progress=None):
    from dataset_lock import cache_write_lock
    workload = c['workload']
    service = CONFIG.get('service', {})
    with cache_write_lock(folder, {'service': f"{service.get('host', '?')}:{service.get('port', '?')}", 'input_tokens': workload['input_tokens'], 'count': count, 'request_id': c.get('_cache_owner')}):
        if (folder / 'data.jsonl').is_file():
            verify_dataset_cache(folder, count, workload['input_tokens'], c['max_out_len'])
            (folder / '.last-used').touch()
            return folder / 'data.jsonl', True
        path = generate_data(c, folder, count, progress=progress)
        (folder / '.last-used').touch()
        return path, False


def generation_response(body):
    """Idempotent short requests; one generator holds the Runner reservation."""
    global GENERATION
    key = hashlib.sha256(json.dumps({'service': CONFIG['service'], 'request': body}, sort_keys=True).encode()).hexdigest()
    with GENERATION_LOCK:
        if GENERATION and GENERATION['key'] == key:
            if GENERATION['state'] == 'failed':
                return {'error': GENERATION['error']}, GENERATION.get('status', 400)
            if GENERATION['state'] == 'completed':
                return GENERATION['result'], 200
            return {'state': 'preparing', 'progress': GENERATION.get('progress', {})}, 202
        if not reserve_operation('generation', input_tokens=body['input_tokens'], max_out_len=body['max_out_len'], count=body['count'], prefix_ratio=body.get('prefix_ratio', 0)):
            return {'error': busy_message()}, 409
        try:
            require_connection(body.get('connection_id'))
        except Exception:
            release_operation()
            raise
        c = {'workload': {'input_tokens': body['input_tokens'], 'seed': body.get('seed', 20261027), 'prefix_ratio': body.get('prefix_ratio', 0)},
             'max_out_len': body['max_out_len'], 'api': {'endpoint': body.get('endpoint', 'chat'), 'trust_remote_code': bool(body.get('trust_remote_code'))}}
        state = GENERATION = {'key': key, 'state': 'preparing', 'progress': {'phase': '启动生成进程', 'completed': 0, 'total': body['count']}}
        def work():
            try:
                folder = perf_dataset_dir(body['input_tokens'], body['count'], body['max_out_len'], body.get('seed', 20261027), body.get('prefix_ratio', 0), body.get('endpoint', 'chat'))
                path, reused = dataset_worker(c, folder, body['count'])
                state['result'] = dict(path=str(path), dir=str(folder), reused=reused, input_tokens=body['input_tokens'], count=body['count'], max_out_len=body['max_out_len'], prefix_ratio=body.get('prefix_ratio', 0))
                state['state'] = 'completed'
            except Exception as exc:
                state.update(state='failed', error=str(exc), status=getattr(exc, 'status', 400))
            finally:
                release_operation()
        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception:
            state.update(state='failed', error='数据集生成线程未能启动', status=400)
            release_operation()
            raise
        return {'state': 'preparing', 'progress': state['progress']}, 202


def verify_dataset_cache(folder, count, input_tokens, max_out):
    with (folder / 'dataset-manifest.json').open(encoding='utf-8') as stream:
        manifest = json.load(stream)
    if len(manifest) != count:
        raise ValueError('已有数据集条目数与配置不符，请重新生成')
    previous, digest = None, None
    with (folder / 'data.jsonl').open(encoding='utf-8') as stream:
        for entry in manifest:
            line = stream.readline()
            if not line:
                raise ValueError('已有数据集被截断，请重新生成')
            row = json.loads(line)
            text = row['question']
            if text != previous:
                digest = hashlib.sha256(text.encode()).hexdigest()
            if digest != entry['sha256'] or entry['input_tokens'] != input_tokens or row['max_out_len'] != max_out:
                raise ValueError('已有数据集内容与校验信息不符，请重新生成')
            previous = text
        if stream.readline():
            raise ValueError('已有数据集包含额外条目，请重新生成')

def validate_execution_mode(c):
    expected = {'accuracy': 'all', 'performance': 'perf'}.get(c.get('mode'))
    execution = c.get('execution', {})
    if not expected or execution.get('mode') != expected:
        raise ValueError('精度仅支持 all，性能仅支持 perf')
    return expected


def validate(c):
    if c.get('engine') != 'aisbench':
        raise ValueError('当前仅启用 AISBench，请切换该引擎')
    if type(c.get('retry',2)) is not int or not 0 <= c.get('retry',2) <= 1000:
        raise ValueError('重试次数范围为0–1000')
    if c.get('timeout') is not None and (type(c['timeout']) not in (int,float) or not 0 < c['timeout'] <= 86400):
        raise ValueError('请求超时必须大于0且不超过86400秒')
    supported_extra = {'host', 'port', 'model', 'tokenizer_path', 'batch_size', 'max_out_len', 'stream', 'retry', 'timeout', 'sample_limit',
                       'api.request_rate', 'api.trust_remote_code', 'api.use_timestamp', 'api.traffic_cfg', 'api.url', 'api.abbr', 'warmup.count', 'warmup.max_out_len',
                       'execution.mode', 'execution.work_dir', 'execution.reuse', 'execution.max_num_workers', 'execution.debug',
                       'execution.dump_eval_details', 'execution.dump_extract_rate', 'execution.merge_ds', 'execution.response_anomaly',
                       'execution.response_anomaly_payload_retention', 'execution.spec_decode', 'execution.pressure', 'execution.pressure_time', 'execution.steady_state',
                       'execution.max_workers_per_gpu', 'api.enable_ssl', 'api.verbose', 'api.meta_template', 'api.api_key'}
    for entry in c.get('extra_parameters_resolved', []):
        target = str(entry.get('target', ''))
        if not target.startswith('generation_kwargs.') and target not in supported_extra:
            raise ValueError('额外参数尚未接入执行：' + str(entry.get('key')))
    if c.get('mode') not in ('accuracy', 'performance'):
        raise ValueError('Unknown mode')
    service = CONFIG['service']
    same_host = c.get('host') == service['host'] or (c.get('host') in ('127.0.0.1', 'localhost') and service['host'] in ('127.0.0.1', 'localhost'))
    if not same_host or c.get('port') != service['port']:
        raise ValueError(f"Runner 当前配置只允许测试 {service['host']}:{service['port']}，如需更换请在「Runner 配置」中修改")
    if (c.get('model') != service['model'] or c.get('tokenizer_path') != service['tokenizer_path']):
        raise ValueError('模型名或 tokenizer 路径与 Runner 配置不匹配')
    target = c.get('execution', {}).get('target', {})
    if target.get('kind') != 'direct':
        raise ValueError('任务在 Runner 当前环境直接执行；不支持通过容器名切换执行环境')
    for key, maximum in [('batch_size', 64000), ('max_out_len', 225280)]:
        value = c.get(key)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(key + ' 超出当前接入范围')
    execution = c['execution']
    validate_execution_mode(c)
    if c['mode'] == 'accuracy' and canonical_dataset(c.get('dataset', {}).get('name')) not in DATASETS:
        raise ValueError('仅支持 GSM8K / GPQA / AIME2026，其他数据集尚未适配')
    if c['mode'] == 'accuracy':
        count = c.get('sample_limit')
    else:
        # 性能模式：样本数（>0）优先于请求数量
        count = c.get('sample_limit') or (c.get('workload') or {}).get('count')
    if type(count) is not int or not 1 <= count <= 10000:
        raise ValueError('样本数超出范围（1–10000）')
    if c.get('warmup', {}).get('enabled'):
        warmups = c['warmup'].get('count')
        if type(warmups) is not int or not 0 <= warmups <= count:
            raise ValueError('预热请求数范围为0–正式样本总数')
        warmout = c['warmup'].get('max_out_len', c['max_out_len'])
        if type(warmout) is not int or not 1 <= warmout <= 225280:
            raise ValueError('预热输出长度范围为1–225280 tokens（220k）')
    if execution.get('pressure'):
        duration = execution.get('pressure_time')
        if c['mode'] != 'performance' or type(duration) is not int or not 1 <= duration <= 86400:
            raise ValueError('稳态压测仅支持性能模式，持续时间范围为1–86400秒')
    if (c.get('workload') or {}).get('prefix_ratio'):
        if c['mode'] != 'performance':
            raise ValueError('共享前缀仅适用于性能测试')
    api = c.get('api', {})
    if api.get('endpoint') not in ('chat', 'completions'):
        raise ValueError('接口仅支持 chat / completions')
    if api.get('url'):
        address = urlparse(api['url'])
        if address.scheme not in ('http', 'https') or address.username or address.password or address.hostname != service['host'] or (address.port or (443 if address.scheme == 'https' else 80)) != service['port']:
            raise ValueError('自定义 URL 必须属于已验证服务的 IP 和端口，不得包含用户名或密码')
    abbr = api.get('abbr')
    if abbr and (not isinstance(abbr, str) or any(ch in abbr for ch in ('/', '\\', '..', '\n', '\r', '\0'))):
        raise ValueError('模型显示简称不能包含路径或控制字符')
    generation = c.get('generation_kwargs', {})
    known = {'temperature', 'ignore_eos', 'top_p', 'top_k', 'min_p', 'seed', 'repetition_penalty', 'frequency_penalty', 'presence_penalty',
             'stop', 'stop_token_ids', 'min_tokens', 'bad_words', 'allowed_token_ids', 'logprobs', 'top_logprobs', 'skip_special_tokens',
             'spaces_between_special_tokens', 'detokenize', 'include_stop_str_in_output', 'chat_template_kwargs', 'num_return_sequences'}
    if not isinstance(generation, dict) or any(not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', key) for key in generation):
        raise ValueError('generation_kwargs 必须使用有效的参数名')
    temperature = generation.get('temperature', 0)
    if type(temperature) not in (int, float) or not math.isfinite(temperature) or temperature < 0:
        raise ValueError('temperature 必须为大于或等于 0 的有限数字')
    if c['mode'] == 'performance':
        length = c['workload'].get('input_tokens')
        if type(length) is not int or not 64 <= length <= 225280:
            raise ValueError('输入长度范围 64–225280 tokens')
    return count

def _chat_len(tokenizer, text, endpoint='chat'):
    if endpoint == 'completions':
        return len(tokenizer.encode(text, add_special_tokens=True))
    rendered = tokenizer.apply_chat_template([{'role': 'user', 'content': text}], tokenize=False, add_generation_prompt=True)
    return len(tokenizer.encode(rendered, add_special_tokens=False))


def bounded_map(function, items, workers=4):
    """Keep only a small window of large prompts resident while preserving order."""
    iterator = iter(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = deque()
        for _ in range(workers * 2):
            try:
                pending.append(pool.submit(function, next(iterator)))
            except StopIteration:
                break
        while pending:
            yield pending.popleft().result()
            try:
                pending.append(pool.submit(function, next(iterator)))
            except StopIteration:
                pass


def generate_data(c, folder, count, progress=None):
    # Also cover standalone starts that do not use start-runner.sh.
    os.environ['TORCH_DEVICE_BACKEND_AUTOLOAD'] = '0'
    from transformers import AutoTokenizer
    if progress:
        progress('加载 tokenizer', 0, count)
    folder.mkdir(parents=True, exist_ok=True)
    tokenizer = AutoTokenizer.from_pretrained(CONFIG['service']['tokenizer_path'], trust_remote_code=bool(c.get('api', {}).get('trust_remote_code')), local_files_only=True)
    workload = c.get('workload') or {}
    target = workload['input_tokens']
    prefix_ratio = workload.get('prefix_ratio', 0) or 0
    seed = workload.get('seed', 20261027)
    endpoint = c.get('api', {}).get('endpoint', 'chat')
    length = lambda text: _chat_len(tokenizer, text, endpoint)
    if shutil.disk_usage(folder).free < count * target * 8 + 200 * 1024**2:
        raise ValueError('生成数据集所需磁盘空间不足；未删除任何已有数据')
    unit = None
    for cand in (' x', ' .', ' 1', ' -', ' a'):
        base = ' benchmark' * 8
        if length(base + cand * 4) - length(base) == 4:
            unit = cand
            break
    if unit is None:
        raise ValueError('tokenizer 粒度过粗，找不到 1-token 填充单元')
    pool = (' apple', ' river', ' cloud', ' green', ' stone', ' music', ' paper', ' world')

    def build(budget, tail, rng, measure=None):
        measure = measure or length
        wl = [rng.choice(pool) for _ in range(budget)]
        words = max(1, budget // 2 - 32)
        for _ in range(60):
            n = measure(''.join(wl[:words]) + tail)
            if n <= budget:
                break
            words -= max(1, (n - budget + 1) // 2)
        else:
            raise ValueError('粗调失败：无法降到目标长度以下')
        head = ''.join(wl[:words])
        pad = budget - n
        text = head + unit * pad + tail
        for _ in range(8):
            n = measure(text)
            if n == budget:
                break
            pad += budget - n
            if pad < 0:
                raise ValueError('填充校准失败：pad 为负')
            text = head + unit * pad + tail
        if measure(text) != budget:
            raise ValueError('无法精确校准 tokenizer 长度')
        return text

    # Only one prompt is held at a time, including independent verification.
    # A 2560 x 128k dataset must not become several in-memory JSON copies.
    path = folder / 'data.jsonl'
    nonce = secrets.token_hex(8)
    temporary = folder / ('data.' + nonce + '.part')
    audit = folder / ('manifest.' + nonce + '.part')
    pfx_tokens = round(target * prefix_ratio)
    # Shared prefix is raw prompt text; account for the chat wrapper only once
    # when calibrating the final request. Full-prefix prompts include that
    # wrapper in their total and are reused byte-for-byte.
    raw_length = lambda text: len(tokenizer.encode(text, add_special_tokens=False))
    common = build(pfx_tokens, '', random.Random(seed), None if prefix_ratio == 1 else raw_length) if prefix_ratio else ''
    def make_row(i):
        if prefix_ratio == 1:
            text = common
        elif prefix_ratio:
            suffix = build(target - pfx_tokens, f'\nRequest {i}: return a short numbered list.', random.Random(seed + 100000 + i))
            text = common + suffix
            difference = target - length(text)
            if difference >= 0:
                text += unit * difference
            if length(text) != target:
                raise ValueError('前缀拼接后长度校准失败')
        else:
            text = build(target, f'\nRequest {i}: return a short numbered list.', random.Random(seed + i))
        row = {'question': text, 'answer': '', 'max_out_len': c['max_out_len']}
        entry = {'id': i, 'input_tokens': target, 'prefix_tokens': pfx_tokens, 'sha256': hashlib.sha256(text.encode()).hexdigest()}
        return row, entry
    try:
        with temporary.open('w', encoding='utf-8') as data_file, audit.open('w', encoding='utf-8') as manifest_file:
            manifest_file.write('[')
            rows = map(make_row, range(count)) if prefix_ratio == 1 else bounded_map(make_row, range(count))
            for row, entry in rows:
                data_file.write(json.dumps(row, ensure_ascii=False) + '\n')
                manifest_file.write((',' if entry['id'] else '') + json.dumps(entry))
                if progress:
                    progress('生成', entry['id'] + 1, count)
            manifest_file.write(']')
        previous = None
        seen = 0
        with temporary.open(encoding='utf-8') as data_file:
            def verify_line(line):
                text = json.loads(line)['question']
                if length(text) != target:
                    raise ValueError('数据集重读长度校验失败')
                return True
            if prefix_ratio != 1:
                for valid in bounded_map(verify_line, data_file):
                    seen += valid
                    if progress:
                        progress('校验', seen, count)
            for line in data_file if prefix_ratio == 1 else ():
                text = json.loads(line)['question']
                # Identical full-prefix rows are checked once; every other row
                # is independently retokenized after reading from disk.
                if text != previous and length(text) != target:
                    raise ValueError('数据集重读长度校验失败')
                previous = text
                seen += 1
                if progress:
                    progress('校验', seen, count)
        if seen != count:
            raise ValueError('数据集条目数校验失败')
        audit.replace(folder / 'dataset-manifest.json')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
        audit.unlink(missing_ok=True)
    return path


def request_timeout_env(c):
    """Leave AISBench's native timeout untouched unless seconds were selected."""
    return 'native' if c.get('timeout') is None else str(c['timeout'])


def make_config(c, folder, dataset_path):
    # All Python is generated from validated primitive values, never supplied by the client.
    service = CONFIG['service']
    # Pass the native AISBench retry value through unchanged.
    api = c.get('api', {})
    params = dict(attr='service', abbr=api.get('abbr') or service['model'], path=service['tokenizer_path'], model=service['model'], stream=c.get('stream', False), host_ip=service['host'], host_port=service['port'], max_out_len=c['max_out_len'], batch_size=c['batch_size'], request_rate=api.get('request_rate', 0), retry=c.get('retry', 2), trust_remote_code=bool(api.get('trust_remote_code')), generation_kwargs=c['generation_kwargs'])
    for name in ('url', 'use_timestamp', 'traffic_cfg', 'enable_ssl', 'verbose', 'meta_template', 'api_key'):
        if api.get(name):
            params[name] = api[name]
    # Leading empty _base_ forces mmengine eager parsing (lazy mode forbids list
    # iteration and constructor calls on imported objects).
    # Class objects break ConfigManager._dump_and_reload_config serialization; the dotted
    # string is the canonical form ais_bench itself converts model types into.
    params['type'] = ('ais_bench.benchmark.models.api_models.vllm_custom_api.VLLMCustomAPI'
                      if api.get('endpoint') == 'completions' else 'ais_bench.benchmark.models.api_models.vllm_custom_api_chat.VLLMCustomAPIChat')
    text = '_base_ = []\nmodels = ' + repr([params]) + '\n'
    # Request timeout is applied by bench-venv sitecustomize via AIS_BENCH_TOTAL_TIMEOUT;
    # mmengine Config.fromfile strips imports, so patching base_api inside the config
    # file cannot reach the real module.
    if c['mode'] == 'accuracy':
        dataset_name = canonical_dataset(c.get('dataset', {}).get('name'))
        if dataset_name not in DATASETS:
            raise ValueError('尚未适配的数据集：' + str(dataset_name))
        text += DATASETS[dataset_name]['config_import'] + '\n'
        # Eager parse leaves real classes in the official dataset cfg; stringify
        # them (dotted names) so ConfigManager dump/reload can serialize.
        text += 'def _strcls(o):\n    if isinstance(o, type) or callable(o):\n        return o.__module__ + "." + o.__name__\n    if isinstance(o, dict):\n        return dict((k, _strcls(v)) for k, v in o.items())\n    if isinstance(o, (list, tuple)):\n        return [_strcls(v) for v in o]\n    return o\n'
        data_location = dataset_dir(dataset_name)
        if dataset_name == 'AIME2026':
            data_location /= DATASETS[dataset_name]['required']
        text += 'for _d in datasets:\n    _d["path"] = ' + repr(str(data_location)) + '\n'
        text += 'datasets = [_strcls(d) for d in datasets]\ndel _d, _strcls\n'
    else:
        _sum = dict(type='ais_bench.benchmark.summarizers.default_perf.DefaultPerfSummarizer',
                    attr='performance', calculator=dict(type='ais_bench.benchmark.calculators.DefaultPerfMetricCalculator', stats_list=['Average', 'Min', 'Max', 'Median', 'P90', 'P99']))
        if c.get('execution', {}).get('steady_state'):
            _sum.update(attr='performance', calculator=dict(type='ais_bench.benchmark.calculators.StablePerfMetricCalculator', stats_list=['Average', 'Min', 'Max', 'Median', 'P90', 'P99']))
        _dsd = dict(abbr='synthetic', type='ais_bench.benchmark.datasets.custom.CustomDataset', path=str(dataset_path),
            reader_cfg=dict(input_columns=['question', 'max_out_len'], output_column='answer'),
            infer_cfg=dict(prompt_template=dict(type='ais_bench.benchmark.openicl.icl_prompt_template.icl_prompt_template.PromptTemplate', template='{question}'),
                retriever=dict(type='ais_bench.benchmark.openicl.icl_retriever.icl_zero_retriever.ZeroRetriever'),
                inferencer=dict(type='ais_bench.benchmark.openicl.icl_inferencer.icl_gen_inferencer.GenInferencer')),
            eval_cfg=dict(evaluator=dict(type='ais_bench.benchmark.openicl.icl_evaluator.icl_hf_evaluator.AccEvaluator')))
        text += 'summarizer = ' + repr(_sum) + '\n'
        text += 'datasets = [' + repr(_dsd) + ']\n'
        del _sum, _dsd
    (folder / 'config.py').write_text(text)
    return text

def missing_score_problem(folder):
    log = folder / 'execution.log'
    if log.is_file():
        with log.open('rb') as stream:
            stream.seek(max(0, log.stat().st_size - 200000))
            text = stream.read().decode('utf-8', errors='replace')
        text = re.sub(r'\x1b\[[0-9;]*m', '', text)
        for line in text.splitlines():
            if ('Inconsistency detected by ld.so' in line or
                    'Fatal Python error:' in line or 'Segmentation fault' in line):
                return '精度评测子进程崩溃，未生成有效评分：' + line.strip()[:500]
        for line in text.splitlines():
            if 'warmup failed' in line.lower() or ('warmup' in line.lower() and 'Success Count: 0' in line):
                return '预热请求全部失败，未生成有效评分：' + line.strip()[:500]
        if 'No predictions found' in text:
            return '推理未生成预测结果，无法评分；请查看执行日志中推理阶段的首次错误'
    return '未找到可核验的精度评分文件；AISBench 退出码 0 不代表推理及评分子任务成功，请检查完整执行日志'


def collect_warmup(output, record, folder):
    warmup = record['config'].get('warmup') or {}
    requested = warmup.get('count', 0) if warmup.get('enabled') else 0
    if not requested or record['config'].get('execution', {}).get('mode') in ('eval', 'viz', 'perf_viz'):
        record['warmup_result'] = {'requested': 0, 'performed': False}
        return
    summaries = []
    # Native --debug sends task output directly to the CLI stream; ordinary
    # mode sends it to per-task files. Inspect the stream actually used.
    debug = record['config'].get('execution', {}).get('debug', False)
    paths = [folder / 'execution.log'] if debug else [p for p in output.rglob('*')
        if p.is_file() and p.suffix in ('.log', '.out') and 'logs' in p.parts]
    for path in paths:
        if not path.is_file():
            continue
        maximum, counts = [], []
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                if 'Warmup' not in line:
                    continue
                maximum.extend(re.findall(r'\[Runner\] Warmup max_out_len=(\d+)', line))
                counts.extend(re.findall(r'Warmup finished Total Count:\s*(\d+) Success Count:\s*(\d+) Failed Count:\s*(\d+)', line))
        # Debug mode also prints the official summary; keep one per task log.
        if counts:
            total, success, failed = map(int, counts[-1])
            summaries.append({'total': total, 'success': success, 'failed': failed, 'max_out_len': int(maximum[-1]) if maximum else None})
    record['warmup_result'] = {'requested': requested, 'performed': bool(summaries), 'tasks': summaries}
    if not summaries:
        record.setdefault('notices', []).append('未找到预热摘要；以 AISBench 原生日志和退出结果为准')
        return
    expected_maximum = warmup.get('max_out_len') or record['config']['max_out_len']
    if any(row['total'] != requested or row['success'] != requested or row['failed'] or row['max_out_len'] != expected_maximum for row in summaries):
        record.setdefault('notices', []).append('预热摘要与配置不完全一致，详见 warmup_result；以 AISBench 原生退出结果为准')
    # Keep the verified result in metadata. Replaying warmup after the final
    # result would duplicate native output and scramble the log's chronology.


def verify_performance_lengths(tables, config):
    """Report requested/measured differences without overriding native results."""
    expected_input = (config.get('workload') or {}).get('input_tokens')
    expected_output = config.get('max_out_len') if config.get('generation_kwargs', {}).get('ignore_eos') else None
    expected = {'InputTokens': expected_input, 'OutputTokens': expected_output}
    notices = []
    for table in tables:
        columns = [i for i, name in enumerate(table.get('headers', [])) if name in ('Average', 'Min', 'Max', 'Value')]
        for row in table.get('rows', []):
            if len(row) < 3 or row[1] not in ('total', 'stable') or expected.get(row[0]) is None:
                continue
            differences = []
            for index in columns:
                if index >= len(row):
                    continue
                try:
                    actual = float(str(row[index]).replace(',', ''))
                except (ValueError, TypeError):
                    continue
                if not math.isfinite(actual) or actual != expected[row[0]]:
                    differences.append((table['headers'][index], str(row[index])))
            if differences:
                values = list(dict.fromkeys(value for _, value in differences))
                observed = values[0] if len(values) == 1 else ', '.join(name + '=' + value for name, value in differences)
                label = '输入' if row[0] == 'InputTokens' else '输出'
                notices.append(f'{label}长度提示（{row[1]}）：配置目标 {expected[row[0]]} tokens，AISBench 实际统计 {observed} tokens；保留原生结果，不因此判失败')
    return list(dict.fromkeys(notices))


def resolve_output_dir(folder, record):
    if not record.get('output_root'):
        return folder / 'output'
    root = Path(record['output_root']).resolve()
    if record.get('reuse_directory'):
        return Path(record['reuse_directory'])
    previous = set(record.get('previous_output_directories', []))
    candidates = [p for p in root.iterdir() if p.is_dir() and p.name not in previous]
    if len(candidates) != 1:
        raise ValueError('无法唯一确认本次 AISBench 结果目录，不读取旧任务的结果')
    return candidates[0]


def output_options(c, folder):
    execution = c.get('execution', {})
    supplied = execution.get('work_dir')
    explicit = any(e.get('target') == 'execution.work_dir' for e in c.get('extra_parameters_resolved', []))
    if not supplied or supplied == '/data/benchmarks/outputs' and not explicit:
        root = folder / 'output'
    else:
        root = Path(supplied)
    validate_execution_mode(c)
    reuse = execution.get('reuse') or ''
    if reuse and Path(reuse).is_absolute():
        root, reuse = Path(reuse).parent, Path(reuse).name
    elif re.fullmatch(r'\d{8}_\d{6}', reuse) and not explicit and supplied in (None, '', '/data/benchmarks/outputs'):
        matches = sorted(p.resolve() for p in RUNS.glob('*/output/' + reuse) if p.is_dir())
        if len(matches) > 1:
            raise ValueError('多个任务包含此时间戳，请通过 reuse 指定完整结果目录')
        if matches:
            root = matches[0].parent
    root = root.resolve()
    if not root.is_relative_to(BASE) or root == BASE:
        raise ValueError('结果目录必须位于当前 Runner 安装目录内，不能写入其他项目')
    root.mkdir(parents=True, exist_ok=True)
    if reuse == 'latest':
        choices = sorted(p.name for p in root.iterdir() if p.is_dir() and re.fullmatch(r'\d{8}_\d{6}', p.name))
        if not choices:
            raise ValueError('没有可复用的 AISBench 结果')
        reuse = choices[-1]
    if reuse and (not re.fullmatch(r'\d{8}_\d{6}', reuse) or not (root / reuse).is_dir()):
        raise ValueError('复用参数必须指向已有的 AISBench 时间戳结果目录')
    return root, reuse



def collect(folder, record):
    output = resolve_output_dir(folder, record)
    tables, result_files, accuracy = [], [], None
    for path in sorted(output.rglob('*')):
        if not path.is_file():
            continue
        relative = str(path.relative_to(folder)) if path.is_relative_to(folder) else str(path)
        if path.suffix in ('.csv', '.json', '.jsonl'):
            result_files.append(relative)
        if path.suffix == '.csv' and any(x in path.parts for x in ('summary', 'performance', 'performances')):
            try:
                with path.open() as stream:
                    rows = list(csv.reader(stream))
                if rows:
                    tables.append({'file': relative, 'headers': rows[0], 'rows': rows[1:1000]})
            except (UnicodeError, csv.Error):
                pass
        if path.suffix == '.json' and 'performances' in path.parts:
            try:
                data = json.loads(path.read_text())
                rows = [[key, stage, value] for key, stages in data.items() if isinstance(stages, dict) for stage, value in stages.items() if isinstance(value, (str, int, float))]
                if rows:
                    tables.append({'file': relative, 'headers': ['Performance Parameters', 'Stage', 'Value'], 'rows': rows})
            except (ValueError, AttributeError):
                pass
        if path.suffix == '.json' and 'results' in path.parts:
            try:
                data = json.loads(path.read_text())
                if isinstance(data, dict) and isinstance(data.get('accuracy'), (int, float)):
                    flags = []
                    for detail in data.get('details', {}).values():
                        correct = detail.get('correct', [])
                        if isinstance(correct, list) and correct:
                            flags.append(bool(correct[0]))
                    accuracy = dict(score_percent=data['accuracy'], correct=sum(flags) if flags else None, total=len(flags) if flags else record['count'])
            except (ValueError, TypeError, AttributeError):
                pass
    record['raw_tables'] = tables
    record['result_files'] = result_files
    record['metrics'] = {'accuracy': accuracy} if record['config']['mode'] == 'accuracy' else {}
    failed_count = 0
    for path in output.rglob('*failed*.jsonl'):
        if path.is_file():
            # Match AISBench _summary_failed_results: parse each JSONL record.
            # str.splitlines() also splits U+2028 inside a valid JSON string.
            with path.open(encoding='utf-8') as stream:
                for line in stream:
                    json.loads(line)
                    failed_count += 1
    record['failed_requests'] = failed_count
    if failed_count:
        record.setdefault('notices', []).append(f'AISBench 记录了 {failed_count} 条失败请求；任务状态遵循原生退出结果，请查看失败请求文件')
    if record['config']['mode'] == 'accuracy' and record['config'].get('execution', {}).get('mode', 'all') in ('all', 'eval') and accuracy is None:
        record.setdefault('notices', []).append(missing_score_problem(folder))
    if record['config']['mode'] == 'performance' and not tables:
        record.setdefault('notices', []).append('未找到性能指标表；以 AISBench 原生日志和退出结果为准')
    if record['config']['mode'] == 'performance':
        record.setdefault('notices', []).extend(verify_performance_lengths(tables, record['config']))
    collect_warmup(output, record, folder)

def process_group_running(pgid, proc_root=Path('/proc')):
    """True for live members, False for absent/zombie-only groups, None if unknown."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return None
    seen, uncertain = False, False
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            # comm may contain spaces or parentheses. Fields after the final
            # closing parenthesis are state, ppid, pgrp, session, ... .
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) != pgid:
                continue
            seen = True
            if fields[0] not in ('Z', 'X', 'x'):
                return True
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError):
            uncertain = True
    if uncertain:
        return None
    if seen:
        return False
    # A group could have exited during the scan. Do not infer absence merely
    # from a /proc view which may hide members in another PID namespace.
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass
    return None


def wait_process_group_stopped(pgid):
    for _ in range(20):
        if process_group_running(pgid) is False:
            return True
        time.sleep(.1)
    return False


def reconcile_finished_task():
    """A status refresh can finish deferred cleanup without administrator action."""
    global ACTIVE_JOB, SAFE_IDLE
    with STATE_LOCK:
        record = ACTIVE_JOB
        if not record or record.get('status') != 'unknown' or record['id'] in PROCESSES:
            return
        terminal = record.get('terminal_status')
        if terminal not in ('completed', 'failed', 'stopped') or not record.get('completed_at') or not record.get('pid'):
            return
        if process_group_running(record['pid']) is not False:
            return
        restored = {**record, 'status': terminal, 'error': record.get('terminal_error')}
        save(RUNS / record['id'] / 'record.json', restored)
        if record.get('fleet_request_id'):
            FLEET_REQUESTS[record['fleet_request_id']] = {'state': 'finished', 'job_id': record['id']}
        ACTIVE_JOB = None
        SAFE_IDLE = True
        cleanup_dataset_memory(record['id'])
        release_operation()


def restore_finished_requests():
    """Retain terminal request receipts across an idle Runner upgrade/restart."""
    for path in RUNS.glob('*/record.json'):
        data = json.loads(path.read_text())
        request_id = data.get('fleet_request_id')
        if not request_id or data.get('status') not in ('completed', 'failed', 'stopped') or not data.get('completed_at'):
            continue
        # These terminal states are written only after group cleanup. Unknown
        # or interrupted tasks are deliberately excluded from this receipt.
        if data.get('terminal_status') != data['status']:
            continue
        FLEET_REQUESTS[request_id] = {'state': 'finished', 'job_id': data['id'], 'restored_after_restart': True,
                                      'terminal_status': data['status'], 'completed_at': data['completed_at'],
                                      'process_tree_confirmed_stopped': True}


def run_job(record):
    global ACTIVE_JOB, SAFE_IDLE
    folder = RUNS / record['id']
    log_path = folder / 'execution.log'
    try:
        c = record['config']
        with log_path.open('a', buffering=1) as log:
            log.write('[REAL] 结果归档目录: ' + str(folder) + '\n')
            log.write('[REAL] 完整日志: ' + str(log_path) + '\n')
            output_root, reuse = output_options(c, folder)
            if c['mode'] == 'performance' and reuse:
                dataset_path = folder / 'unused-reused-performance.jsonl'
                log.write('[REAL] perf + reuse：由 AISBench 原生复用已有性能结果，不生成数据集\n')
            elif c['mode'] == 'accuracy':
                log.write('[REAL] Preparing dataset\n')
                dataset_path = prepare_dataset(c['dataset']['name'])['path']
            else:
                log.write('[REAL] Preparing dataset\n')
                dataset_path, reused = get_perf_dataset(c, record['count'])
                cache_pin = dataset_path.parent / ('.dataset-reader-' + secrets.token_hex(16))
                cache_pin.write_text(record['id'], encoding='utf-8')
                log.write(f"[REAL] {'复用已有性能数据集' if reused else '生成新性能数据集'}: {dataset_path}\n")
            content = make_config(c, folder, dataset_path)
            # Per-run absolute ais_bench path wins when it exists; otherwise the saved default.
            requested_ais = str((c.get('execution', {}).get('target') or {}).get('aisbench_path') or '')
            ais_bin = requested_ais if requested_ais.startswith('/') and Path(requested_ais).is_file() and os.access(requested_ais, os.X_OK) else CONFIG['ais_bench_path']
            problem = ais_launch_problem(ais_bin)
            if problem:
                raise ValueError(problem)
            warmup = c.get('warmup') or {}
            num_warmups = warmup.get('count', 0) if warmup.get('enabled') else 0
            record['output_root'] = str(output_root)
            record['previous_output_directories'] = [p.name for p in output_root.iterdir() if p.is_dir()]
            if reuse:
                record['reuse_directory'] = str(output_root / reuse)
            # make_config writes models[0].batch_size. Older AISBench versions
            # do not expose --batch-size, so keep concurrency in the config.
            argv = [ais_bin, str(folder / 'config.py'), '--mode', c['execution']['mode'], '--num-prompts', str(record['count']), '--num-warmups', str(num_warmups), '--work-dir', str(output_root), '--max-num-workers', str(c['execution'].get('max_num_workers', 1))]
            for field, flag in (('debug', '--debug'), ('dump_eval_details', '--dump-eval-details'), ('dump_extract_rate', '--dump-extract-rate'), ('merge_ds', '--merge-ds'), ('response_anomaly', '--response-anomaly'), ('spec_decode', '--spec-decode')):
                if c['execution'].get(field, field == 'dump_eval_details'):
                    argv.append(flag)
            if c['execution'].get('response_anomaly') and c['execution'].get('response_anomaly_payload_retention'):
                argv += ['--response-anomaly-payload-retention', c['execution']['response_anomaly_payload_retention']]
            if reuse:
                argv += ['--reuse', reuse]
            if c['execution'].get('max_workers_per_gpu'):
                argv += ['--max-workers-per-gpu', str(c['execution']['max_workers_per_gpu'])]
            if c['execution'].get('pressure'):
                argv += ['--pressure', '--pressure-time', str(c['execution']['pressure_time'])]
            record['command'] = shlex.join(argv)
            record['artifacts'] = {'files': [{'name': 'config.py', 'content': content}], 'aisbench_commands': [record['command']], 'execution_command': record['command'], 'note': '已由 Runner 执行；Runner 位于本项目容器内。'}
            (folder / 'command.txt').write_text(record['command'] + '\n')
            save(folder / 'record.json', record)
            log.write('[REAL] ' + record['command'] + '\n')
            env = os.environ.copy()
            env['ASCEND_BENCH_PROCESS_ROLE'] = 'aisbench'
            env['ASCEND_BENCH_API_ONLY'] = '1'
            env['ASCEND_BENCH_JOB_ID'] = record['id']
            env['ASCEND_BENCH_HOME'] = str(BASE)
            env['AIS_BENCH_WARMUP_MAX_OUT_LEN'] = str(warmup.get('max_out_len') or c['max_out_len'])
            no_proxy = '127.0.0.1,localhost,' + CONFIG['service']['host']
            # 清空代理变量：内网环境容器常带 http_proxy（enterprise proxy 类中间人），
            # 测试请求打本机/内网服务必须直连，否则 504（proxy error page）。
            env.update(TORCH_DEVICE_BACKEND_AUTOLOAD='0', PYTHONDONTWRITEBYTECODE='1', HOME=str(BASE), TMPDIR=str(BASE / 'tmp'), HF_HOME=str(BASE / 'cache/huggingface'), XDG_CACHE_HOME=str(BASE / 'cache'), NO_PROXY=no_proxy, no_proxy=no_proxy, http_proxy='', https_proxy='', HTTP_PROXY='', HTTPS_PROXY='', AIS_BENCH_TOTAL_TIMEOUT=request_timeout_env(c))
            # Load this release's timeout/error patch even when reusing an AISBench
            # executable installed by an older release or elsewhere in the container.
            env['PYTHONPATH'] = str(BASE / 'scripts') + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
            # Export the same process-scoped settings used below. The command
            # alone cannot reproduce timeout, warmup length or runtime adapters.
            exported = {key: env[key] for key in ('ASCEND_BENCH_PROCESS_ROLE', 'ASCEND_BENCH_API_ONLY', 'ASCEND_BENCH_JOB_ID', 'ASCEND_BENCH_HOME', 'AIS_BENCH_WARMUP_MAX_OUT_LEN', 'AIS_BENCH_TOTAL_TIMEOUT', 'TORCH_DEVICE_BACKEND_AUTOLOAD', 'PYTHONPATH', 'HOME', 'TMPDIR', 'HF_HOME', 'XDG_CACHE_HOME', 'NO_PROXY', 'no_proxy', 'http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY')}
            replay = '#!/usr/bin/env bash\nset -euo pipefail\ncd ' + shlex.quote(str(BASE)) + '\n' + '\n'.join('export ' + key + '=' + shlex.quote(value) for key, value in exported.items()) + '\nexec ' + record['command'] + '\n'
            (folder / 'run.sh').write_text(replay)
            record['artifacts']['files'].append({'name': 'run.sh', 'content': replay})
            save(folder / 'record.json', record)
            # Editable installs resolve their own source via the venv. The old
            # shared BASE/benchmark may not exist with per-container environments.
            run_cwd = str(BASE)
            check_cli_flags(ais_bin, argv, env)
            if record['id'] in CANCELLED:
                raise RuntimeError('任务在准备阶段已取消')
            process = subprocess.Popen(argv, cwd=run_cwd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            PROCESSES[record['id']] = process
            record['pid'] = process.pid
            save(folder / 'record.json', record)
            code = process.wait()
            record['exit_code'] = code
            (folder / 'exitcode').write_text(str(code) + '\n')
            if code:
                raise RuntimeError(f'AISBench exited {code}')
        try:
            collect(folder, record)
        except Exception as exc:
            record.setdefault('notices', []).append('结果读取提示：' + str(exc))
        record['status'] = 'completed'
    except BaseException as exc:
        process = PROCESSES.get(record['id'])
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        record['status'] = 'stopped' if record['id'] in CANCELLED else 'failed'
        record['error'] = str(exc)
        with log_path.open('a') as log:
            log.write('[ERROR] ' + str(exc) + '\n')
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=log)
    finally:
        # Completion is not published until the task process group has stopped.
        record['terminal_status'] = record['status']
        record['terminal_error'] = record.get('error')
        process = PROCESSES.get(record['id'])
        SAFE_IDLE = True
        if process:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            if not wait_process_group_stopped(process.pid):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                SAFE_IDLE = wait_process_group_stopped(process.pid)
            if not SAFE_IDLE:
                record['status'] = 'unknown'
                record['error'] = '任务进程组未确认退出，请管理员核对'
        if SAFE_IDLE:
            cleanup_dataset_memory(record['id'])
            if 'cache_pin' in locals():
                try:
                    cache_pin.unlink(missing_ok=True)
                except OSError as exc:
                    record.setdefault('notices', []).append('缓存使用标记未能释放，清理时将保守跳过：' + str(exc))
        record['log'] = read_log_tail(log_path)
        record['completed_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        save(folder / 'record.json', record)
        with STATE_LOCK:
            PROCESSES.pop(record['id'], None)
            ACTIVE_JOB = None if SAFE_IDLE else record
            if record.get('fleet_request_id'):
                FLEET_REQUESTS[record['fleet_request_id']] = {'state': 'finished' if SAFE_IDLE else 'unknown', 'job_id': record['id']}
            if SAFE_IDLE:
                release_operation()

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE / 'website'), **kwargs)

    def _host_port(self):
        # 浏览器看到的本地隧道端口（Host 头带的是本地端口），
        # 多机同时使用时每台映射一个不同的本地端口（18787/18788/…）。
        host = self.headers.get('Host', '')
        port = host.rsplit(':', 1)[-1] if ':' in host else ''
        return port if port.isdigit() else str(PORT)

    def allowed_origin(self):
        # 只接受回环访问（任意本地端口），拒绝任何对外 IP / 域名
        host = self.headers.get('Host', '')
        if not (host.startswith('127.0.0.1:') or host.startswith('localhost:') or host.startswith('[::1]:')):
            return False
        origin = self.headers.get('Origin')
        return (not origin or origin.startswith('http://127.0.0.1:')
                or origin.startswith('http://localhost:') or origin.startswith('http://[::1]:'))

    def authed(self):
        # cookie 按本地端口隔离：多机同时开（不同本地端口）互不串登录态
        cookie = http.cookies.SimpleCookie(self.headers.get('Cookie', ''))
        name = 'runner_session_' + self._host_port()
        return name in cookie and secrets.compare_digest(cookie[name].value.encode('utf-8'), TOKEN.encode('utf-8'))

    def json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.allowed_origin():
            return self.json_response({'error': 'Invalid host/origin'}, 403)
        path = urlparse(self.path).path
        if path.startswith('/api/'):
            if not self.authed():
                return self.json_response({'error': 'Runner 登录凭据未验证'}, 401)
            if path == '/api/info':
                info = dict(real=True, **CONFIG['service'], root=str(BASE), container=CONFIG['container'], aisbench_path=CONFIG['ais_bench_path'], ais_bench_detected=detect_ais_bench())
                info['execution_context'] = {'kind': 'current', 'container': os.environ.get('ASCEND_BENCH_CONTAINER') or CONFIG.get('container') or os.uname().nodename, 'external_container_execution': False}
                info['connection_verified'] = bool(CONNECTION)
                return self.json_response(info)
            if path == '/api/status':
                reconcile_finished_task()
                return self.json_response({
                    'protocol_version': 2, 'version': 'fleet-v3', 'instance_id': INSTANCE_ID,
                    'source_sha256': SOURCE_SHA256, 'health': 'healthy',
                    'root': str(BASE), 'container': CONFIG['container'],
                    'activity': ('running' if ACTIVE_JOB else 'preparing') if LOCK.locked() else 'idle',
                    'operation': OPERATION if LOCK.locked() else None,
                    'active_job': {'id': ACTIVE_JOB['id'], 'mode': ACTIVE_JOB['config'].get('mode'),
                                   'target': {k: ACTIVE_JOB['config'].get(k) for k in ('host', 'port', 'model')}} if ACTIVE_JOB else None,
                    'service': CONFIG['service'], 'process_tree_confirmed_stopped': SAFE_IDLE and not LOCK.locked(),
                    'fleet_requests': dict(FLEET_REQUESTS), 'observed_at': time.time()})
            if path == '/api/config':
                return self.json_response(dict(config=CONFIG, ais_bench_detected=detect_ais_bench()))
            if path == '/api/jobs':
                rows = []
                for p in sorted(RUNS.glob('*/record.json'), reverse=True):
                    try:
                        rows.append(json.loads(p.read_text()))
                    except FileNotFoundError:
                        # An administrator may concurrently remove an expired
                        # terminal job; omit it instead of breaking the list.
                        continue
                return self.json_response(rows)
            if path.startswith('/api/jobs/'):
                try:
                    row = read_job(path.rsplit('/', 1)[1])
                    log = RUNS / row['id'] / 'execution.log'
                    row['log'] = read_log_tail(log)
                    return self.json_response(row)
                except (ValueError, FileNotFoundError):
                    return self.json_response({'error': 'Unknown job'}, 404)
            return self.json_response({'error': 'Unknown endpoint'}, 404)
        if path == '/kit':
            if not self.authed():
                return self.json_response({'error': 'Runner 登录凭据未验证'}, 401)
            kit = BASE / 'dist' / 'ascend-bench-kit.tar.gz'
            if not kit.is_file():
                return self.json_response({'error': '工具包尚未生成；请在服务器执行 scripts/make-kit.sh'}, 404)
            data = kit.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'application/gzip')
            self.send_header('Content-Disposition', 'attachment; filename="ascend-bench-kit.tar.gz"')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if path == '/runner-client.js':
            raw = (BASE / 'scripts/runner-client.js').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/javascript')
            self.end_headers()
            return self.wfile.write(raw)
        if path == '/':
            raw = (BASE / 'website/index.html').read_text().replace('</body>', '<script src="/runner-client.js"></script></body>').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            return self.wfile.write(raw)
        return super().do_GET()

    def do_POST(self):
        global CONNECTION, CONNECTION_REVISION, ACTIVE_JOB, OPERATION
        if not self.allowed_origin() or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.json_response({'error': 'Invalid origin/content type'}, 403)
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length < 100000:
            return self.json_response({'error': 'Invalid body size'}, 400)
        fleet_request_id = None
        try:
            body = json.loads(self.rfile.read(length))
            path = urlparse(self.path).path
            if path == '/api/login':
                if not secrets.compare_digest(str(body.get('token', '')).encode('utf-8'), TOKEN.encode('utf-8')):
                    return self.json_response({'error': '无效 Runner 凭据'}, 403)
                self.send_response(200)
                self.send_header('Set-Cookie', 'runner_session_' + self._host_port() + '=' + TOKEN + '; HttpOnly; SameSite=Strict; Path=/')
                self.send_header('Content-Length', '2')
                self.end_headers()
                return self.wfile.write(b'{}')
            if not self.authed():
                return self.json_response({'error': 'Unauthorized'}, 401)
            if path in ('/api/maintenance/storage/scan', '/api/maintenance/storage/delete'):
                if not SAFE_IDLE or not reserve_operation('cleanup', label='管理员扫描 / 清理存储'):
                    return self.json_response({'error': busy_message()}, 409)
                try:
                    from storage_cleanup import scan, delete
                    if path.endswith('/scan'):
                        result = scan(BASE, body.get('days', 7), TOKEN)
                    else:
                        result = delete(BASE, body.get('items'), TOKEN)
                        # A completed generation response must not return a
                        # path whose cache was just removed by the administrator.
                        global GENERATION
                        if GENERATION and GENERATION.get('state') == 'completed':
                            GENERATION = None
                    return self.json_response(result)
                finally:
                    release_operation()
            if path == '/api/check-service':
                if not reserve_operation('checking', body.get('service'), label='检查服务连接'):
                    return self.json_response({'error': busy_message(), 'connection_verified': bool(CONNECTION)}, 409)
                try:
                    CONNECTION = None
                    CONNECTION_REVISION += 1
                    revision = CONNECTION_REVISION
                    service = body.get('service')
                    merged = dict(CONFIG)
                    if isinstance(service, dict) and service:
                        merged['service'] = {**CONFIG['service'], **service}
                    candidate = validate_config(merged, require_service=True)
                    models = probe_service(candidate['service'])
                    if revision != CONNECTION_REVISION:
                        raise ValueError('连接检查期间配置发生变化，请重新检查')
                    set_config(candidate)
                    CONNECTION = {'id': secrets.token_urlsafe(24), 'fingerprint': service_fingerprint(CONFIG['service']), 'verified_at': time.time()}
                    models['connection_id'] = CONNECTION['id']
                    return self.json_response(models)
                finally:
                    release_operation()
            if path == '/api/config':
                if not reserve_operation('configuration', label='保存配置'):
                    return self.json_response({'error': busy_message()}, 409)
                try:
                    return self.json_response(set_config(validate_config(body)))
                finally:
                    release_operation()
            if path == '/api/datasets/check':
                return self.json_response(check_dataset(str(body.get('name', ''))))
            if path == '/api/datasets/prepare':
                if not reserve_operation('dataset', label='准备精度数据集'):
                    return self.json_response({'error': busy_message()}, 409)
                try:
                    require_connection(body.get('connection_id'))
                    return self.json_response(prepare_dataset(str(body.get('name') or 'GSM8K')))
                finally:
                    release_operation()
            if path == '/api/datasets/generate':
                require_connection(body.get('connection_id'))
                length, count, max_out = body.get('input_tokens'), body.get('count'), body.get('max_out_len')
                seed = body.get('seed', 20261027)
                prefix_ratio = body.get('prefix_ratio', 0) or 0
                endpoint = body.get('endpoint', 'chat')
                if endpoint not in ('chat', 'completions'):
                    raise ValueError('接口仅支持 chat / completions')
                if type(seed) is not int or not -(2**53 - 1) <= seed <= 2**53 - 1:
                    raise ValueError('随机种子必须是 JavaScript 安全整数（可为负数）')
                if type(prefix_ratio) is not int and type(prefix_ratio) is not float or not 0 <= prefix_ratio <= 1:
                    raise ValueError('公共前缀比例范围 0–1')
                if type(length) is not int or not 64 <= length <= 225280:
                    return self.json_response({'error': '输入长度范围 64–225280 tokens'}, 400)
                if type(count) is not int or not 1 <= count <= 10000:
                    return self.json_response({'error': '请求数量范围 1–10000'}, 400)
                if type(max_out) is not int or not 1 <= max_out <= 225280:
                    return self.json_response({'error': 'max_out_len 范围 1–225280'}, 400)
                result, status = generation_response(body)
                return self.json_response(result, status)
            if path == '/api/jobs':
                fleet_request_id = body.get('fleet_request_id')
                validate_execution_mode(body)
                if fleet_request_id:
                    if not isinstance(fleet_request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', fleet_request_id):
                        raise ValueError('中心请求 ID 无效')
                    previous = FLEET_REQUESTS.get(fleet_request_id)
                    if previous:
                        if previous.get('job_id'):
                            return self.json_response(read_job(previous['job_id']), 200)
                        raise ValueError('该中心请求已拒绝，请重新提交')
                if not reserve_operation('checking', label='检查测试前置条件'):
                    if fleet_request_id:
                        FLEET_REQUESTS[fleet_request_id] = {'state': 'rejected'}
                    return self.json_response({'error': busy_message()}, 409)
                handed_off = False
                try:
                    require_connection(body.get('connection_id'))
                    try:
                        probe_service(CONFIG['service'])
                    except Exception:
                        CONNECTION = None
                        raise
                    count = validate(body)
                    job_id = 'real-' + str(int(time.time())) + '-' + secrets.token_hex(4)
                    record = dict(id=job_id, demo=False, status='running', created_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), config=body, count=count, metrics=None, exit_code=None, log='')
                    record['fleet_request_id'] = fleet_request_id
                    save(RUNS / job_id / 'record.json', record)
                    if fleet_request_id:
                        FLEET_REQUESTS[fleet_request_id] = {'state': 'accepted', 'job_id': job_id}
                    ACTIVE_JOB = record
                    OPERATION = operation_info('test', job_id=job_id)
                    try:
                        threading.Thread(target=run_job, args=(record,), daemon=True).start()
                    except Exception:
                        ACTIVE_JOB = None
                        if fleet_request_id:
                            FLEET_REQUESTS[fleet_request_id] = {'state': 'rejected'}
                        record.update(status='failed', error='任务线程未能启动；未执行 AISBench')
                        save(RUNS / job_id / 'record.json', record)
                        raise
                    handed_off = True
                    return self.json_response(record, 202)
                finally:
                    if not handed_off:
                        release_operation()
            if path.startswith('/api/jobs/') and path.endswith('/stop'):
                job_id = path.split('/')[-2]
                read_job(job_id)
                process = PROCESSES.get(job_id)
                if ACTIVE_JOB and ACTIVE_JOB['id'] == job_id:
                    CANCELLED.add(job_id)
                    if not process:
                        return self.json_response({'status': 'stopping'})
                if not process or process.poll() is not None:
                    raise ValueError('任务不在请求执行阶段，当前无需停止')
                CANCELLED.add(job_id)
                os.killpg(process.pid, signal.SIGTERM)
                return self.json_response({'status': 'stopping'})
            return self.json_response({'error': 'Unknown endpoint'}, 404)
        except Exception as exc:
            if isinstance(fleet_request_id, str) and re.fullmatch(r'[a-f0-9]{32}', fleet_request_id) and fleet_request_id not in FLEET_REQUESTS:
                FLEET_REQUESTS[fleet_request_id] = {'state': 'rejected'}
            return self.json_response({'error': str(exc), 'connection_verified': bool(CONNECTION)}, 400)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=int(os.environ.get('ASCEND_BENCH_PORT', '18787')))
    args = parser.parse_args()
    PORT = args.port
    RUNS.mkdir(parents=True, exist_ok=True)
    restore_finished_requests()
    for path in RUNS.glob('*/record.json'):
        data = json.loads(path.read_text())
        if data['status'] == 'running':
            data['status'] = 'interrupted'
            data['error'] = 'Runner restarted; inspect existing process before retrying'
            save(path, data)
    token_path = BASE / 'runner-token'
    if not token_path.is_file():
        token_path.write_text(secrets.token_urlsafe(32))
        token_path.chmod(0o600)
    TOKEN = token_path.read_text().strip()
    cfg = load_config()
    print(f'正在绑定 Runner 监听端口 127.0.0.1:{PORT}（PID={os.getpid()}）',flush=True)
    server = RunnerHTTPServer(('127.0.0.1', PORT), Handler)
    (BASE / 'runner.pid').write_text(str(os.getpid()) + '\n')
    print("=" * 64, flush=True)
    print(f"Runner 已启动：http://127.0.0.1:{PORT}", flush=True)
    print(f"  服务 {cfg['service']['host']}:{cfg['service']['port']} · 模型 {cfg['service']['model']}", flush=True)
    print(f"  ais_bench：{cfg['ais_bench_path']}", flush=True)
    if os.environ.get('ASCEND_BENCH_MANAGED') == '1':
        print('  中心管理模式；等待中心核验身份后加入评测列表', flush=True)
    else:
        print(f"  登录 token：{BASE}/runner-token", flush=True)
        print(f"  本机隧道：ssh -N -L <本地端口>:127.0.0.1:{PORT} <SSH用户>@<服务器IP>", flush=True)
        print("  打开：http://127.0.0.1:<本地端口>", flush=True)
    print("=" * 64, flush=True)
    server.serve_forever()
