"""One launcher for dependency setup, configuration and optional logon service."""
import subprocess
import sys


def main():
    for module, args in [('fleet.setup_admin', []), ('fleet.configure', []),
                         ('fleet.service', ['install']), ('fleet.service', ['start'])]:
        result = subprocess.run([sys.executable, '-X', 'utf8', '-m', module, *args])
        if result.returncode:
            print('当前步骤未完成，已停止后续安装。请查看上方提示后重试。')
            return result.returncode
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        raise SystemExit('\n安装已取消。')
