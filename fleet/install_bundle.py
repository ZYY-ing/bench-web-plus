"""Run on target Linux server: python3 install.py. No personal PC keys needed."""
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import fcntl


def host_public_keys():
    keys = []
    for name in ('ssh_host_ed25519_key.pub', 'ssh_host_ecdsa_key.pub', 'ssh_host_rsa_key.pub'):
        path = Path('/etc/ssh') / name
        if path.is_file():
            parts = path.read_text().strip().split()
            if len(parts) >= 2:
                keys.append(' '.join(parts[:2]))
    if not keys:
        raise ValueError('无法读取服务器 SSH 公钥；请管理员确认 sshd 的 HostKey 路径')
    return keys


def main():
    os.umask(0o077)
    here = Path(__file__).resolve().parent
    with (here / '.install.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        install(here)


def install(here):
    manifest = json.loads((here / 'manifest.json').read_text(encoding='utf-8'))
    spec = manifest['spec']
    if manifest['expires_at'] < time.time():
        raise ValueError('接入包已过期，请重新从网页生成')
    if getpass.getuser() != spec['user']:
        raise ValueError('请以网页填写的 SSH 用户 ' + spec['user'] + ' 执行；不要直接切换为其他用户')
    archive = here / 'runner-kit.tar.gz'
    if hashlib.sha256(archive.read_bytes()).hexdigest() != manifest['kit_sha256']:
        raise ValueError('工具包校验失败')
    public_key = manifest['public_key']
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/=]+', public_key):
        raise ValueError('接入公钥格式无效')
    keys = host_public_keys()
    home = Path.home()
    state = home / '.local/share/ascend-bench-access' / manifest['id']
    if (state / 'spec.json').exists() and json.loads((state / 'spec.json').read_text()) != spec:
        raise ValueError('已有接入目录的配置不一致，停止覆盖')
    # All server changes are visible before installation begins.
    print('将安装到已有容器:', spec['container'])
    print('容器内持久化目录:', spec['base'])
    print('授权范围: 固定 Runner 评测 API；禁止 shell / SCP / 端口转发')
    print('撤销方式: 安装后执行 python3 ' + str(state / 'revoke.py'))
    import remote_helper
    # Install first; grant SSH access only after Runner health is verified.
    remote_helper.main({'action': 'deploy', 'spec': spec, 'archive': str(archive), 'sha256': manifest['kit_sha256']})
    state.mkdir(parents=True, mode=0o700, exist_ok=True)
    shutil.copyfile(here / 'gateway.py', state / 'gateway.py')
    (state / 'spec.json').write_text(json.dumps(spec), encoding='utf-8')
    command = shlex.join([str(Path(sys.executable).resolve()), '-I', str(state / 'gateway.py')])
    escaped = command.replace('\\', '\\\\').replace('"', '\\"')
    marker = 'ascend-bench-' + manifest['id']
    entry = 'restrict,command="' + escaped + '" ' + public_key + ' ' + marker
    ssh_dir = home / '.ssh'
    authorized = ssh_dir / 'authorized_keys'
    if ssh_dir.is_symlink() or authorized.is_symlink():
        raise ValueError('SSH 授权路径为符号链接，停止授权，请管理员核对')
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    previous = authorized.read_text() if authorized.exists() else ''
    if authorized.exists() and not (state / 'authorized_keys.before').exists():
        shutil.copyfile(authorized, state / 'authorized_keys.before')
    # Append our own restricted entry; never replace existing authorizations.
    if any(line.endswith(' ' + marker) and line != entry for line in previous.splitlines()):
        raise ValueError('同名接入授权内容不一致，停止覆盖')
    if entry not in previous.splitlines():
        with authorized.open('a', encoding='utf-8') as stream:
            stream.write(('\n' if previous and not previous.endswith('\n') else '') + entry + '\n')
    authorized.chmod(0o600)
    revoke = '''from pathlib import Path
p = Path.home()/'.ssh/authorized_keys'
entry = ENTRY
lines = p.read_text().splitlines(keepends=True)
p.write_text(''.join(line for line in lines if line.strip() != entry))
p.chmod(0o600)
print('本工具接入授权已撤销；Runner 和结果保留。')
'''.replace('ENTRY', repr(entry))
    (state / 'revoke.py').write_text(revoke, encoding='utf-8')
    receipt = {'id': manifest['id'], 'host': spec['host'], 'user': spec['user'], 'host_keys': keys}
    receipt_path = here / ('receipt-' + manifest['id'] + '.json')
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print('\n安装成功。将以下回执文件传回网页并点「导入并检查接入」：')
    print(receipt_path)
    print('回执只含登记信息和服务器 SSH 公钥，不含密码、私钥或 Runner token。')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('安装停止：' + str(exc), file=sys.stderr)
        sys.exit(1)
