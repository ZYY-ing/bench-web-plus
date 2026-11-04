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
import subprocess
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

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
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
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
    if not Path(ais_path).is_file() or not os.access(ais_path, os.X_OK):
        raise ValueError(f'ais_bench 不存在或不可执行：{ais_path}')
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
            raise ValueError(result['error'])
        if spec['licensed']:
            raise ValueError(f"需要授权文件 {spec['required']}：请将其放置到服务器 {d} 后点重新检查")
        raise ValueError(f"缺少 {spec['required']}：工具包应携带该文件，也可手动放置到 {d}")
    digest = hashlib.sha256((d / spec['required']).read_bytes()).hexdigest()
    result['sha256'] = digest
    return result


def perf_dataset_dir(length, count, max_out, seed=20261027, prefix_ratio=0):
    tokenizer = Path(CONFIG['service']['tokenizer_path'])
    stamp = str(tokenizer)
    for name in ('tokenizer.json', 'tokenizer_config.json'):
        path = tokenizer / name
        if path.is_file():
            stamp += f'/{name}:{path.stat().st_size}:{path.stat().st_mtime_ns}'
    tag = hashlib.sha256(stamp.encode()).hexdigest()[:12]
    pfx = f'_pfx{int(round(prefix_ratio * 100))}' if prefix_ratio else ''
    return BASE / 'datasets' / 'perf' / f'v2_in{length}_req{count}_out{max_out}_seed{seed}{pfx}_{tag}'


def get_perf_dataset(c, count):
    workload = c.get('workload') or {}
    folder = perf_dataset_dir(workload['input_tokens'], count, c['max_out_len'], workload.get('seed', 20261027), workload.get('prefix_ratio', 0))
    if (folder / 'data.jsonl').is_file():
        return folder / 'data.jsonl', True
    folder.mkdir(parents=True, exist_ok=True)
    return generate_data(c, folder, count), False

def validate(c):
    if c.get('engine') != 'aisbench':
        raise ValueError('当前仅启用 AISBench，请切换该引擎')
    supported_extra = {'host', 'port', 'model', 'tokenizer_path', 'batch_size', 'max_out_len', 'stream', 'retry', 'timeout', 'sample_limit', 'api.request_rate', 'execution.mode'}
    for entry in c.get('extra_parameters_resolved', []):
        target = str(entry.get('target', ''))
        if not target.startswith('generation_kwargs.') and target not in supported_extra:
            raise ValueError('额外参数尚未接入执行：' + str(entry.get('key')))
    if c.get('mode') not in ('accuracy', 'performance'):
        raise ValueError('Unknown mode')
    service = CONFIG['service']
    if c.get('host') not in (service['host'], '127.0.0.1', 'localhost') or c.get('port') != service['port']:
        raise ValueError(f"Runner 当前配置只允许测试 {service['host']}:{service['port']}，如需更换请在「Runner 配置」中修改")
    if c.get('model') != service['model'] or c.get('tokenizer_path') != service['tokenizer_path']:
        raise ValueError('模型名或 tokenizer 路径与 Runner 配置不匹配')
    target = c.get('execution', {}).get('target', {})
    if target.get('kind') != 'direct':
        raise ValueError('任务在 Runner 当前环境直接执行；不支持通过容器名切换执行环境')
    for key, maximum in [('batch_size', 256), ('max_out_len', 204800)]:
        value = c.get(key)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(key + ' 超出当前接入范围')
    execution = c['execution']
    if execution.get('mode') not in ('all', 'perf'):
        raise ValueError('当前仅启用 all / perf')
    if execution['mode'] != ('all' if c['mode'] == 'accuracy' else 'perf'):
        raise ValueError('AISBench mode 不匹配')
    if c['mode'] == 'accuracy' and canonical_dataset(c.get('dataset', {}).get('name')) not in DATASETS:
        raise ValueError('仅支持 GSM8K / GPQA / AIME2026，其他数据集尚未适配')
    if c['mode'] == 'accuracy':
        count = c.get('sample_limit')
    else:
        # 性能模式：样本数（>0）优先于请求数量
        count = c.get('sample_limit') or (c.get('workload') or {}).get('count')
    if type(count) is not int or not 1 <= count <= 10000:
        raise ValueError('样本数超出范围（1–10000）')
    if c.get('warmup', {}).get('enabled') and not c.get('warmup', {}).get('count'):
        raise ValueError('预热请求数需大于 0')
    if execution.get('pressure'):
        raise ValueError('稳态压测尚未接入结果校验，请先跑定条数测试')
    if (c.get('workload') or {}).get('prefix_ratio'):
        if c['mode'] != 'performance':
            raise ValueError('共享前缀仅适用于性能测试')
    if c.get('api', {}).get('endpoint') != 'chat':
        raise ValueError('本次先验证 Chat Completions 接口')
    if any(c.get('api', {}).get(k) for k in ('url', 'use_timestamp', 'traffic_cfg')):
        raise ValueError('自定义 URL / 流量调度尚未接入')
    generation = c.get('generation_kwargs', {})
    known = {'temperature', 'ignore_eos', 'top_p', 'top_k', 'min_p', 'seed', 'repetition_penalty', 'frequency_penalty', 'presence_penalty', 'stop', 'stop_token_ids', 'min_tokens'}
    if set(generation) - known:
        raise ValueError('暂未接入生成参数：' + str(set(generation) - known))
    if c['mode'] == 'performance':
        length = c['workload'].get('input_tokens')
        if type(length) is not int or not 64 <= length <= 204800:
            raise ValueError('输入长度范围 64–204800 tokens')
        if not c.get('stream'):
            raise ValueError('性能测量必须使用流式响应')
    return count

def _chat_len(tokenizer, text):
    rendered = tokenizer.apply_chat_template([{'role': 'user', 'content': text}], tokenize=False, add_generation_prompt=True)
    return len(tokenizer.encode(rendered, add_special_tokens=False))


def generate_data(c, folder, count):
    from transformers import AutoTokenizer
    folder.mkdir(parents=True, exist_ok=True)
    tokenizer = AutoTokenizer.from_pretrained(CONFIG['service']['tokenizer_path'], trust_remote_code=False, local_files_only=True)
    workload = c.get('workload') or {}
    target = workload['input_tokens']
    prefix_ratio = workload.get('prefix_ratio', 0) or 0
    seed = workload.get('seed', 20261027)
    unit = None
    for cand in (' x', ' .', ' 1', ' -', ' a'):
        base = ' benchmark' * 8
        if _chat_len(tokenizer, base + cand * 4) - _chat_len(tokenizer, base) == 4:
            unit = cand
            break
    if unit is None:
        raise ValueError('tokenizer 粒度过粗，找不到 1-token 填充单元')
    pool = (' apple', ' river', ' cloud', ' green', ' stone', ' music', ' paper', ' world')

    def build(budget, tail, rng):
        wl = [rng.choice(pool) for _ in range(budget)]
        words = max(1, budget // 2 - 32)
        for _ in range(60):
            n = _chat_len(tokenizer, ''.join(wl[:words]) + tail)
            if n <= budget:
                break
            words -= max(1, (n - budget + 1) // 2)
        else:
            raise ValueError('粗调失败：无法降到目标长度以下')
        head = ''.join(wl[:words])
        pad = budget - n
        text = head + unit * pad + tail
        for _ in range(8):
            n = _chat_len(tokenizer, text)
            if n == budget:
                break
            pad += budget - n
            if pad < 0:
                raise ValueError('填充校准失败：pad 为负')
            text = head + unit * pad + tail
        if _chat_len(tokenizer, text) != budget:
            raise ValueError('无法精确校准 tokenizer 长度')
        return text

    rows, manifest = [], []
    if prefix_ratio > 0:
        pfx_tokens = max(1, round(target * prefix_ratio))
        common = build(pfx_tokens, '\nShared prefix.', random.Random(seed))
        for i in range(count):
            rng = random.Random(seed + 100000 + i)
            tail = f'\nRequest {i}: return a short numbered list.'
            suffix = build(target - pfx_tokens, tail, rng)
            text = common + suffix
            n = _chat_len(tokenizer, text)
            if n != target:
                text = common + suffix + unit * max(0, target - n)
                n = _chat_len(tokenizer, text)
            if n != target:
                raise ValueError('前缀拼接后长度校准失败')
            rows.append({'question': text, 'answer': '', 'max_out_len': c['max_out_len']})
            manifest.append({'id': i, 'input_tokens': n, 'prefix_tokens': pfx_tokens, 'sha256': hashlib.sha256(text.encode()).hexdigest()})
    else:
        for i in range(count):
            rng = random.Random(seed + i)
            tail = f'\nRequest {i}: return a short numbered list.'
            text = build(target, tail, rng)
            rows.append({'question': text, 'answer': '', 'max_out_len': c['max_out_len']})
            manifest.append({'id': i, 'input_tokens': target, 'sha256': hashlib.sha256(text.encode()).hexdigest()})
    path = folder / 'data.jsonl'
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    # Independent reread and retokenization, including chat template.
    for row in [json.loads(line) for line in path.read_text().splitlines()]:
        n = _chat_len(tokenizer, row['question'])
        if n != target:
            raise ValueError('数据集重读长度校验失败')
    save(folder / 'dataset-manifest.json', manifest)
    return path


def make_config(c, folder, dataset_path):
    # All Python is generated from validated primitive values, never supplied by the client.
    service = CONFIG['service']
    params = dict(attr='service', abbr=service['model'], path=service['tokenizer_path'], model=service['model'], stream=c.get('stream', False), host_ip=service['host'], host_port=service['port'], max_out_len=c['max_out_len'], batch_size=c['batch_size'], request_rate=c.get('api', {}).get('request_rate', 0), retry=c.get('retry', 2), trust_remote_code=False, generation_kwargs=c['generation_kwargs'])
    # Leading empty _base_ forces mmengine eager parsing (lazy mode forbids list
    # iteration and constructor calls on imported objects).
    # Class objects break ConfigManager._dump_and_reload_config serialization; the dotted
    # string is the canonical form ais_bench itself converts model types into.
    params['type'] = 'ais_bench.benchmark.models.api_models.vllm_custom_api_chat.VLLMCustomAPIChat'
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
        _sum = dict(type='ais_bench.benchmark.summarizers.default_perf.DefaultPerfSummarizer')
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

def collect(folder, record):
    output = folder / 'output'
    tables, result_files, accuracy = [], [], None
    for path in sorted(output.rglob('*')):
        if not path.is_file():
            continue
        relative = str(path.relative_to(folder))
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
    failed = []
    for path in output.rglob('*failed*.jsonl'):
        if path.is_file():
            failed.extend(line for line in path.read_text().splitlines() if line.strip())
    record['failed_requests'] = len(failed)
    if failed:
        raise RuntimeError('AISBench 留下失败请求，不能将退出码 0 视为测试成功')
    if record['config']['mode'] == 'accuracy' and accuracy is None:
        raise RuntimeError('未找到可核验的精度评分文件')
    if record['config']['mode'] == 'performance' and not tables:
        raise RuntimeError('未找到性能指标表')

def run_job(record):
    global ACTIVE_JOB, SAFE_IDLE
    folder = RUNS / record['id']
    log_path = folder / 'execution.log'
    try:
        c = record['config']
        with log_path.open('a', buffering=1) as log:
            log.write('[REAL] 结果归档目录: ' + str(folder) + '\n')
            log.write('[REAL] 完整日志: ' + str(log_path) + '\n')
            log.write('[REAL] Preparing dataset\n')
            if c['mode'] == 'accuracy':
                dataset_path = prepare_dataset(c['dataset']['name'])['path']
            else:
                dataset_path, reused = get_perf_dataset(c, record['count'])
                log.write(f"[REAL] {'复用已有性能数据集' if reused else '生成新性能数据集'}: {dataset_path}\n")
            content = make_config(c, folder, dataset_path)
            # Per-run absolute ais_bench path wins when it exists; otherwise the saved default.
            requested_ais = str((c.get('execution', {}).get('target') or {}).get('aisbench_path') or '')
            ais_bin = requested_ais if requested_ais.startswith('/') and Path(requested_ais).is_file() and os.access(requested_ais, os.X_OK) else CONFIG['ais_bench_path']
            warmup = c.get('warmup') or {}
            num_warmups = warmup.get('count', 0) if warmup.get('enabled') else 0
            argv = [ais_bin, str(folder / 'config.py'), '--mode', c['execution']['mode'], '--num-prompts', str(record['count']), '--num-warmups', str(num_warmups), '--batch-size', str(c['batch_size']), '--dump-eval-details', '--debug', '--work-dir', str(folder / 'output')]
            record['command'] = shlex.join(argv)
            record['artifacts'] = {'files': [{'name': 'config.py', 'content': content}], 'aisbench_commands': [record['command']], 'execution_command': record['command'], 'note': '已由 Runner 执行；Runner 位于本项目容器内。'}
            (folder / 'command.txt').write_text(record['command'] + '\n')
            save(folder / 'record.json', record)
            log.write('[REAL] ' + record['command'] + '\n')
            env = os.environ.copy()
            no_proxy = '127.0.0.1,localhost,' + CONFIG['service']['host']
            # 清空代理变量：内网环境容器常带 http_proxy（NetentSec 类中间人），
            # 测试请求打本机/内网服务必须直连，否则 504（HIS Proxy Notification）。
            env.update(TORCH_DEVICE_BACKEND_AUTOLOAD='0', PYTHONDONTWRITEBYTECODE='1', HOME=str(BASE), TMPDIR=str(BASE / 'tmp'), HF_HOME=str(BASE / 'cache/huggingface'), XDG_CACHE_HOME=str(BASE / 'cache'), NO_PROXY=no_proxy, no_proxy=no_proxy, http_proxy='', https_proxy='', HTTP_PROXY='', HTTPS_PROXY='', AIS_BENCH_TOTAL_TIMEOUT=str(c.get('timeout', 600)))
            # Keep cwd inside the project only when the chosen ais_bench is the project venv
            # (editable install); a container-installed ais_bench must not pick up the
            # benchmark source tree from cwd with mismatched dependencies.
            run_cwd = str(BASE / 'benchmark') if ais_bin.startswith(str(BASE)) else str(BASE)
            if record['id'] in CANCELLED:
                raise RuntimeError('任务在准备阶段已取消')
            process = subprocess.Popen(argv, cwd=run_cwd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            PROCESSES[record['id']] = process
            record['pid'] = process.pid
            save(folder / 'record.json', record)
            code = process.wait(timeout=7200)
            record['exit_code'] = code
            (folder / 'exitcode').write_text(str(code) + '\n')
            if code:
                raise RuntimeError(f'AISBench exited {code}')
        collect(folder, record)
        record['status'] = 'completed'
    except BaseException as exc:
        process = PROCESSES.get(record['id'])
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        record['status'] = 'stopped' if record['id'] in CANCELLED else 'failed'
        record['error'] = str(exc)
        with log_path.open('a') as log:
            log.write('[ERROR] ' + str(exc) + '\n')
    finally:
        # Completion is not published until the task process group has stopped.
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
            for _ in range(20):
                try:
                    os.killpg(process.pid, 0)
                    time.sleep(.1)
                except ProcessLookupError:
                    break
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                SAFE_IDLE = False
                record['status'] = 'unknown'
                record['error'] = '任务进程组未确认退出，请管理员核对'
        record['log'] = log_path.read_text(errors='replace')[-200000:]
        record['completed_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        save(folder / 'record.json', record)
        PROCESSES.pop(record['id'], None)
        ACTIVE_JOB = None if SAFE_IDLE else record
        if record.get('fleet_request_id'):
            FLEET_REQUESTS[record['fleet_request_id']] = {'state': 'finished' if SAFE_IDLE else 'unknown', 'job_id': record['id']}
        if SAFE_IDLE:
            LOCK.release()

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
                return self.json_response({
                    'protocol_version': 2, 'version': 'fleet-v3', 'instance_id': INSTANCE_ID,
                    'source_sha256': hashlib.sha256(Path(__file__).read_bytes().replace(b'\r\n', b'\n')).hexdigest(), 'health': 'healthy',
                    'root': str(BASE), 'container': CONFIG['container'],
                    'activity': ('running' if ACTIVE_JOB else 'preparing') if LOCK.locked() else 'idle',
                    'active_job': {'id': ACTIVE_JOB['id'], 'mode': ACTIVE_JOB['config'].get('mode'),
                                   'target': {k: ACTIVE_JOB['config'].get(k) for k in ('host', 'port', 'model')}} if ACTIVE_JOB else None,
                    'service': CONFIG['service'], 'process_tree_confirmed_stopped': SAFE_IDLE and not LOCK.locked(),
                    'fleet_requests': dict(FLEET_REQUESTS), 'observed_at': time.time()})
            if path == '/api/config':
                return self.json_response(dict(config=CONFIG, ais_bench_detected=detect_ais_bench()))
            if path == '/api/jobs':
                rows = [json.loads(p.read_text()) for p in sorted(RUNS.glob('*/record.json'), reverse=True)]
                return self.json_response(rows)
            if path.startswith('/api/jobs/'):
                try:
                    row = read_job(path.rsplit('/', 1)[1])
                    log = RUNS / row['id'] / 'execution.log'
                    row['log'] = log.read_text(errors='replace')[-200000:] if log.is_file() else ''
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
        global CONNECTION, CONNECTION_REVISION, ACTIVE_JOB
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
            if path == '/api/check-service':
                if LOCK.locked():
                    raise ValueError('任务执行中，暂不能切换服务')
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
            if path == '/api/config':
                if LOCK.locked():
                    raise ValueError('任务执行中，暂不能修改 Runner 配置')
                return self.json_response(set_config(validate_config(body)))
            if path == '/api/datasets/check':
                return self.json_response(check_dataset(str(body.get('name', ''))))
            if path == '/api/datasets/prepare':
                require_connection(body.get('connection_id'))
                return self.json_response(prepare_dataset(str(body.get('name') or 'GSM8K')))
            if path == '/api/datasets/generate':
                require_connection(body.get('connection_id'))
                length, count, max_out = body.get('input_tokens'), body.get('count'), body.get('max_out_len')
                seed = body.get('seed', 20261027)
                prefix_ratio = body.get('prefix_ratio', 0) or 0
                if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
                    raise ValueError('随机种子必须是 0–4294967295 的整数')
                if type(prefix_ratio) is not int and type(prefix_ratio) is not float or not 0 <= prefix_ratio < 1:
                    raise ValueError('公共前缀比例范围 0–0.99')
                if type(length) is not int or not 64 <= length <= 204800:
                    return self.json_response({'error': '输入长度范围 64–204800 tokens'}, 400)
                if type(count) is not int or not 1 <= count <= 10000:
                    return self.json_response({'error': '请求数量范围 1–10000'}, 400)
                if type(max_out) is not int or not 1 <= max_out <= 204800:
                    return self.json_response({'error': 'max_out_len 范围 1–204800'}, 400)
                if not LOCK.acquire(blocking=False):
                    return self.json_response({'error': '有任务执行中，请稍后再生成'}, 409)
                try:
                    fake = {'workload': {'input_tokens': length, 'seed': seed, 'prefix_ratio': prefix_ratio}, 'max_out_len': max_out}
                    folder = perf_dataset_dir(length, count, max_out, seed, prefix_ratio)
                    reused = (folder / 'data.jsonl').is_file()
                    generate_data(fake, folder, count)
                    return self.json_response({'path': str(folder / 'data.jsonl'), 'dir': str(folder), 'reused': reused, 'input_tokens': length, 'count': count, 'max_out_len': max_out, 'prefix_ratio': prefix_ratio})
                finally:
                    LOCK.release()
            if path == '/api/jobs':
                fleet_request_id = body.get('fleet_request_id')
                if fleet_request_id:
                    if not isinstance(fleet_request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', fleet_request_id):
                        raise ValueError('中心请求 ID 无效')
                    previous = FLEET_REQUESTS.get(fleet_request_id)
                    if previous:
                        if previous.get('job_id'):
                            return self.json_response(read_job(previous['job_id']), 200)
                        raise ValueError('该中心请求已拒绝，请重新提交')
                require_connection(body.get('connection_id'))
                try:
                    probe_service(CONFIG['service'])
                except Exception:
                    CONNECTION = None
                    raise
                count = validate(body)
                if not LOCK.acquire(blocking=False):
                    if fleet_request_id:
                        FLEET_REQUESTS[fleet_request_id] = {'state': 'rejected'}
                    return self.json_response({'error': '已有任务执行中，请等待完成'}, 409)
                job_id = 'real-' + str(int(time.time())) + '-' + secrets.token_hex(4)
                record = dict(id=job_id, demo=False, status='running', created_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), config=body, count=count, metrics=None, exit_code=None, log='')
                record['fleet_request_id'] = fleet_request_id
                save(RUNS / job_id / 'record.json', record)
                if fleet_request_id:
                    FLEET_REQUESTS[fleet_request_id] = {'state': 'accepted', 'job_id': job_id}
                ACTIVE_JOB = record
                threading.Thread(target=run_job, args=(record,), daemon=True).start()
                return self.json_response(record, 202)
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
    print("=" * 64, flush=True)
    print(f"Runner 已启动：http://127.0.0.1:{PORT}", flush=True)
    print(f"  服务 {cfg['service']['host']}:{cfg['service']['port']} · 模型 {cfg['service']['model']}", flush=True)
    print(f"  ais_bench：{cfg['ais_bench_path']}", flush=True)
    if os.environ.get('ASCEND_BENCH_MANAGED') == '1':
        print('  已由中心接入；请从用户评测入口刷新并选择此 Runner', flush=True)
    else:
        print(f"  登录 token：{BASE}/runner-token", flush=True)
        print(f"  本机隧道：ssh -N -L <本地端口>:127.0.0.1:{PORT} <SSH用户>@<服务器IP>", flush=True)
        print("  打开：http://127.0.0.1:<本地端口>", flush=True)
    print("=" * 64, flush=True)
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    (BASE / 'runner.pid').write_text(str(os.getpid()) + '\n')
    server.serve_forever()
