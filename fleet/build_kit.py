"""Overlay locally reviewed sources on the original release without pip/downloads."""
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile

ROOT = Path(__file__).resolve().parent.parent


def build():
    output = ROOT / 'dist/ascend-bench-fleet-kit.tar.gz'
    original = ROOT / 'dist/ascend-bench-kit.tar.gz'
    if not original.exists():
        original = output  # Published source tree carries only one runtime kit.
    temporary = output.with_suffix('.tar.gz.tmp')
    overlay = ROOT / 'fleet/runner-overlay'
    runtime = {}
    with tarfile.open(original) as source, tarfile.open(temporary, 'w:gz') as target:
        for member in source.getmembers():
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != 'ascend-bench-kit' or '..' in parts or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe release archive')
            if member.name == 'ascend-bench-kit/fleet-release.json':
                continue
            replacement = overlay.joinpath(*parts[1:])
            if member.isfile():
                raw = replacement.read_bytes().replace(b'\r\n', b'\n') if replacement.is_file() else source.extractfile(member).read()
                member.size = len(raw)
                relative = '/'.join(parts[1:])
                if relative.startswith(('scripts/', 'website/')):
                    runtime[relative] = hashlib.sha256(raw).hexdigest()
                target.addfile(member, io.BytesIO(raw))
            else:
                target.addfile(member)
        release = {'files': runtime, 'id': hashlib.sha256(json.dumps(runtime, sort_keys=True).encode()).hexdigest()}
        raw = json.dumps(release, sort_keys=True).encode()
        member = tarfile.TarInfo('ascend-bench-kit/fleet-release.json')
        member.size, member.mode, member.mtime = len(raw), 0o644, 0
        target.addfile(member, io.BytesIO(raw))
    temporary.replace(output)
    print(f'{output.name}: {output.stat().st_size} bytes')
    print('SHA256: ' + hashlib.sha256(output.read_bytes()).hexdigest())


if __name__ == '__main__':
    build()
