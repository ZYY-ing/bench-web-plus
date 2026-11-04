"""Install the optional small SSH admin dependency into this tool only."""
from pathlib import Path
import subprocess
import sys

if __name__ == '__main__':
    if sys.version_info < (3, 12):
        raise SystemExit('需要 Python 3.12 或更新版本，请更新 Python 后重试。')
    if '--demo' in sys.argv:
        sys.exit(0)
    target = Path(__file__).resolve().parent.parent / '.fleet-deps'
    sys.path.insert(0, str(target))
    try:
        import paramiko
        sys.exit(0)
    except ImportError:
        pass
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--target', str(target), '--upgrade',
                    'paramiko==5.1.0', '--disable-pip-version-check'], check=True)
