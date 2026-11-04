"""Executed over SSH stdin, never as a daemon. All Docker arguments use argv."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import time
import tempfile
import urllib.request


def command(*args):
    return subprocess.check_output(args, stderr=subprocess.STDOUT, timeout=30).decode().strip()


def container_info(spec):
    info = json.loads(command('docker', 'inspect', spec['container']))[0]
    if info['HostConfig']['NetworkMode'] != 'host':
        raise ValueError('容器必须使用 host 网络')
    if not info['State']['Running']:
        raise ValueError('容器未运行，请先启动已有测试容器')
    base = spec['base']
    if not any(m.get('RW') and (base == m['Destination'] or base.startswith(m['Destination'].rstrip('/') + '/')) for m in info['Mounts']):
        raise ValueError('安装目录不在容器的持久化 RW 挂载内')
    command('docker', 'exec', spec['container'], 'python3', '-c',
            'import os,sys; assert os.path.realpath(sys.argv[1]) == sys.argv[1], "安装目录包含符号链接，停止写入"', base)
    # Avoid writing into an existing installation belonging to another purpose.
    return info


def read_status(spec, allow_legacy_container=False):
    container_info(spec)
    token = command('docker', 'exec', spec['container'], 'cat', spec['base'] + '/runner-token')
    code = '''import json,sys,urllib.request
port,token=sys.argv[1:]
req=urllib.request.Request('http://127.0.0.1:'+port+'/api/status',headers={'Cookie':'runner_session_'+port+'='+token})
with urllib.request.urlopen(req,timeout=8) as r: print(r.read().decode())
'''
    # Token is passed to Python stdin, rather than command-line arguments.
    code = code.replace('port,token=sys.argv[1:]', 'port,token=' + repr((str(spec['port']), token)))
    raw = subprocess.check_output(['docker', 'exec', '-i', spec['container'], 'python3', '-'], input=code.encode(), timeout=20)
    status = json.loads(raw)
    if status.get('root') != spec['base']:
        raise ValueError('端口上的 Runner 安装目录与所选目录不匹配，未操作该服务')
    legacy = status.get('container') != spec['container']
    if legacy:
        # A shared directory can retain the previous container name. Permit
        # migration only if this container's verified Runner PID owns the socket.
        proof = '''import os,sys
from pathlib import Path
base=Path(sys.argv[1]);port=int(sys.argv[2]);container=sys.argv[3]
pid=int((base/'runner.pid').read_text());proc=Path('/proc')/str(pid)
assert (proc/'cwd').resolve()==base
assert any(a.endswith(b'runner.py') for a in (proc/'cmdline').read_bytes().split(bytes([0])))
env=dict(e.split(b'=',1) for e in (proc/'environ').read_bytes().split(bytes([0])) if b'=' in e)
assert env.get(b'ASCEND_BENCH_CONTAINER',b'').decode()==container
inodes=set()
for line in (proc/'net/tcp').read_text().splitlines()[1:]:
 fields=line.split()
 if fields[1]=='0100007F:'+format(port,'04X') and fields[3]=='0A':inodes.add(fields[9])
assert inodes
assert any(os.readlink(fd) in {'socket:['+i+']' for i in inodes} for fd in (proc/'fd').iterdir())
print('verified')
'''
        if not allow_legacy_container:
            raise ValueError('Runner 容器身份不匹配：所选 ' + spec['container'] + '，接口返回 ' + str(status.get('container')) + '；请通过管理部署核对迁移')
        try:
            command('docker', 'exec', spec['container'], 'python3', '-c', proof, spec['base'], str(spec['port']), spec['container'])
        except Exception:
            raise ValueError('端口上的 Runner 不属于所选容器的已核实进程；未停止或修改该服务') from None
    return {'status': status, 'token': token, 'legacy_container': legacy}


def main(payload):
    spec, action = payload['spec'], payload['action']
    if action == 'status':
        print(json.dumps(read_status(spec)))
        return
    container_info(spec)
    occupied = command('docker', 'exec', spec['container'], 'python3', '-c',
        'import socket,sys;s=socket.socket();print(s.connect_ex(("127.0.0.1",int(sys.argv[1])))==0)', str(spec['port'])) == 'True'
    if occupied:
        # Healthy known installation is imported, never stopped or overwritten.
        read_status(spec)
        print('已有同目录 Runner 健康，跳过安装；将重新连接', flush=True)
        return
    archive = Path(payload['archive'])
    if hashlib.sha256(archive.read_bytes()).hexdigest() != payload['sha256']:
        raise ValueError('上传包 SHA256 不一致')
    destination = Path(tempfile.mkdtemp(prefix='unpacked-', dir=archive.parent))
    with tarfile.open(archive) as kit:
        for member in kit.getmembers():
            p = PurePosixPath(member.name)
            if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0] != 'ascend-bench-kit' or not (member.isfile() or member.isdir()):
                raise ValueError('压缩包包含不安全路径或链接')
        # Manual regular-file extraction works with older remote Python as well.
        for member in kit.getmembers():
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with kit.extractfile(member) as stream:
                    target.write_bytes(stream.read())
                target.chmod(member.mode & 0o755)
    env = os.environ.copy()
    for key in ('ASCEND_BENCH_PROXY', 'ASCEND_BENCH_PIP_INDEX', 'ASCEND_BENCH_EXTRA_HOSTS'):
        env.pop(key, None)
    if payload.get('pip_index'):
        env['ASCEND_BENCH_PIP_INDEX'] = payload['pip_index']
    for key, value in payload.get('network', {}).items():
        variable = {'proxy': 'ASCEND_BENCH_PROXY', 'pip_index': 'ASCEND_BENCH_PIP_INDEX', 'extra_hosts': 'ASCEND_BENCH_EXTRA_HOSTS'}[key]
        env[variable] = value
    env['ASCEND_BENCH_MANAGED'] = '1'
    print('调用原有 setup.sh：环境检查 → 拷贝 → AISBench → 启动', flush=True)
    subprocess.run(['bash', str(destination / 'ascend-bench-kit/setup.sh'), spec['container'], spec['base'], spec.get('ais_path', ''), str(spec['port'])], env=env, check=True)
    last_error = ''
    for _ in range(5):
        try:
            read_status(spec)
            print('Runner 身份与状态接口验证通过', flush=True)
            return
        except Exception as exc:
            last_error = str(exc).strip().splitlines()[-1]
            time.sleep(1)
    raise ValueError('安装已执行，但 Runner 身份/健康校验失败：' + last_error)
