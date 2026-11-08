"""Keep deployment packages free of local metadata and development leftovers."""
from pathlib import PurePosixPath
import re


def excluded(relative):
    parts = PurePosixPath(relative).parts
    if any(part in {'.git', '__pycache__', '.pytest_cache', '.mypy_cache'} for part in parts):
        return True
    if parts and (parts[-1].endswith(('.pyc', '.pyo', '.bak', '.tmp')) or parts[-1] in {'.DS_Store', 'Thumbs.db'}):
        return True
    return relative.startswith(('benchmark/tests/', 'benchmark/smoke_tests/')) or relative in {
        'benchmark/upload_pypi.sh', 'benchmark/.coveragerc', 'benchmark/.gitignore',
    } or (relative.startswith('benchmark/plugins/') and 'tests' in parts)


def published_bytes(relative, raw):
    # Documentation/example-only replacements in the bundled upstream tree.
    if relative == 'benchmark/ais_bench/benchmark/configs/models/lmm_models/qwen_image_edit.py':
        raw = re.sub(rb"path='[^']*'", b"path='/models/Qwen-Image-Edit-2509/'", raw)
    if relative in {'benchmark/ais_bench/benchmark/spec_decode/reporter.py',
                    'benchmark/ais_bench/benchmark/spec_decode/urls.py'}:
        raw = re.sub(rb'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])', b'192.0.2.10', raw)
    return raw


def public_metadata(member):
    member.uid = member.gid = 0
    member.uname = member.gname = ''
    member.mtime = 0
    member.pax_headers = {}
    return member
