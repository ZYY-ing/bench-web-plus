"""Overlay locally reviewed sources on the original release without pip/downloads."""
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent


def overlay_bytes(path):
    raw = path.read_bytes()
    if path.suffix.lower() in {'.py', '.js', '.css', '.html', '.sh', '.json', '.txt', '.md', '.toml', '.cfg', '.ini'}:
        raw = raw.replace(b'\r\n', b'\n')
    return raw


def validate_wheel(relative, raw):
    """Reject broken embedded packages before publishing a release archive."""
    if not relative.endswith('.whl'):
        return
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as wheel:
            if not wheel.infolist():
                raise ValueError('empty wheel')
            bad = wheel.testzip()
            if bad:
                raise ValueError('CRC mismatch: ' + bad)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ValueError('Invalid release wheel ' + relative + ': ' + str(exc)) from exc


def build():
    output = ROOT / 'dist/ascend-bench-fleet-kit.tar.gz'
    original = ROOT / 'dist/ascend-bench-kit.tar.gz'
    if not original.exists():
        original = output  # Published source tree carries only one runtime kit.
    temporary = output.with_suffix('.tar.gz.tmp')
    overlay = ROOT / 'fleet/runner-overlay'
    runtime = {}
    benchmark = {}
    included = set()
    with tarfile.open(original) as source, tarfile.open(temporary, 'w:gz') as target:
        for member in source.getmembers():
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != 'ascend-bench-kit' or '..' in parts or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe release archive')
            if member.name == 'ascend-bench-kit/fleet-release.json':
                continue
            replacement = overlay.joinpath(*parts[1:])
            if member.isfile():
                raw = overlay_bytes(replacement) if replacement.is_file() else source.extractfile(member).read()
                member.size = len(raw)
                relative = '/'.join(parts[1:])
                validate_wheel(relative, raw)
                included.add(relative)
                if relative.startswith(('scripts/', 'website/')):
                    runtime[relative] = hashlib.sha256(raw).hexdigest()
                if relative.startswith('benchmark/') and relative.endswith(('.py', '.txt', '.toml', '.cfg')):
                    benchmark[relative] = hashlib.sha256(raw).hexdigest()
                target.addfile(member, io.BytesIO(raw))
            else:
                target.addfile(member)
        for replacement in sorted(overlay.rglob('*')):
            if not replacement.is_file():
                continue
            relative = replacement.relative_to(overlay).as_posix()
            if relative in included or '__pycache__' in replacement.parts:
                continue
            raw = overlay_bytes(replacement)
            validate_wheel(relative, raw)
            member = tarfile.TarInfo('ascend-bench-kit/' + relative)
            member.size, member.mode, member.mtime = len(raw), 0o644, 0
            target.addfile(member, io.BytesIO(raw))
            if relative.startswith(('scripts/', 'website/')):
                runtime[relative] = hashlib.sha256(raw).hexdigest()
            if relative.startswith('benchmark/') and relative.endswith(('.py', '.txt', '.toml', '.cfg')):
                benchmark[relative] = hashlib.sha256(raw).hexdigest()
        release = {'files': runtime, 'id': hashlib.sha256(json.dumps(runtime, sort_keys=True).encode()).hexdigest(), 'benchmark_files': benchmark}
        release['benchmark_id'] = hashlib.sha256(json.dumps(benchmark, sort_keys=True).encode()).hexdigest()
        raw = json.dumps(release, sort_keys=True).encode()
        member = tarfile.TarInfo('ascend-bench-kit/fleet-release.json')
        member.size, member.mode, member.mtime = len(raw), 0o644, 0
        target.addfile(member, io.BytesIO(raw))
    temporary.replace(output)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_name(output.name + '.sha256').write_text(digest + '  ' + output.name + '\n', encoding='ascii')
    print(f'{output.name}: {output.stat().st_size} bytes')
    print('SHA256: ' + digest)


if __name__ == '__main__':
    build()
