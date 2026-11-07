"""Create a small trial bundle; never include local config, tokens, databases."""
import hashlib
import argparse
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', default='bench-web-plus-shared-current.zip')
    args = parser.parse_args()
    if Path(args.name).name != args.name or not args.name.endswith('.zip'):
        parser.error('输出名称必须是单个 ZIP 文件名')
    output = ROOT.parent / 'outputs' / args.name
    output.parent.mkdir(exist_ok=True)
    files = [ROOT / name for name in ('README.md', 'start-fleet.cmd', 'setup-shared-center.cmd', 'install-center-autostart.cmd', 'start-fleet.sh', 'requirements-admin.txt', 'fleet/README.md', 'dist/ascend-bench-fleet-kit.tar.gz')]
    files += [p for p in (ROOT / 'docs').rglob('*') if p.is_file()]
    (ROOT / 'dist/ascend-bench-fleet-kit.tar.gz.sha256').write_text(hashlib.sha256((ROOT / 'dist/ascend-bench-fleet-kit.tar.gz').read_bytes()).hexdigest() + '  ascend-bench-fleet-kit.tar.gz\n', encoding='utf-8')
    files.append(ROOT / 'dist/ascend-bench-fleet-kit.tar.gz.sha256')
    files += [p for p in (ROOT / 'fleet').rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc' and p.name != 'README.md']
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for file in files:
            archive.write(file, file.relative_to(ROOT).as_posix())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(digest + '  ' + output.name + '\n', encoding='utf-8')
    print(str(output))
    print(f'{output.stat().st_size} bytes; SHA256 {digest}')


if __name__ == '__main__':
    main()
