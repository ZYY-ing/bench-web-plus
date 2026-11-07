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
from contextlib import contextmanager


def command(*args):
    return subprocess.check_output(args, stderr=subprocess.STDOUT, timeout=30).decode().strip()


@contextmanager
def remote_deployment_lock(spec):
    import fcntl, stat
    key = hashlib.sha256(json.dumps([spec['profile_id'],spec['port']]).encode()).hexdigest()
    fd = os.open('/tmp/bench-web-deploy-' + key + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as handle:
        info = os.fstat(handle.fileno())
        assert stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid(), '部署锁不属于当前用户'
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('该 Runner 的远端部署仍在执行，请稍后重新部署') from None
        yield handle.fileno()


def retired_container(spec):
    result = subprocess.run(['docker','inspect',spec['container']], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        names = command('docker','ps','-a','--format','{{.Names}}').splitlines()
        return {'retired':spec['container'] not in names, 'reason':'deleted'}
    info = json.loads(result.stdout)[0]
    state = info['State']
    stopped = state.get('Running') is False and state.get('Restarting') is False and state.get('Pid') == 0 and info.get('HostConfig',{}).get('PidMode') != 'host'
    return {'retired':stopped, 'reason':'stopped' if stopped else 'active'}


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
    try:
        token = command('docker', 'exec', spec['container'], 'cat', spec['base'] + '/runner-token')
    except subprocess.CalledProcessError as exc:
        detail=(exc.output or b'').decode(errors='replace').strip().splitlines()
        raise ValueError('所选容器的 Runner 凭据文件缺失或不可读：'+spec['base']+'/runner-token'+('；'+detail[-1][-300:] if detail else '')) from None
    code = '''import json,sys,urllib.request
port,token=sys.argv[1:]
req=urllib.request.Request('http://127.0.0.1:'+port+'/api/status',headers={'Cookie':'runner_session_'+port+'='+token})
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(req,timeout=8) as r: print(r.read().decode())
'''
    # Token is passed to Python stdin, rather than command-line arguments.
    code = code.replace('port,token=sys.argv[1:]', 'port,token=' + repr((str(spec['port']), token)))
    try:
        raw = subprocess.check_output(['docker', 'exec', '-i', spec['container'], 'python3', '-'], input=code.encode(), timeout=20, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        # Startup probes are retried by the caller. Keep child tracebacks out of
        # deployment progress, but preserve the final error if all retries fail.
        lines = (exc.stderr or b'').decode('utf-8', errors='replace').strip().splitlines()
        detail = lines[-1][-400:] if lines else '容器内状态查询未完成'
        raise ValueError('Runner 状态接口暂不可用：' + detail) from None
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


def verify_runner_owner(spec):
    proof = '''import os,sys
from pathlib import Path
base=Path(sys.argv[1]);port=int(sys.argv[2]);container=sys.argv[3]
pid=int((base/'runner.pid').read_text());proc=Path('/proc')/str(pid)
assert pid>1 and (proc/'cwd').resolve()==base
assert any(a.endswith(b'runner.py') for a in (proc/'cmdline').read_bytes().split(bytes([0])))
env=dict(e.split(b'=',1) for e in (proc/'environ').read_bytes().split(bytes([0])) if b'=' in e)
assert env.get(b'ASCEND_BENCH_CONTAINER',b'').decode()==container
inodes={line.split()[9] for line in (proc/'net/tcp').read_text().splitlines()[1:] if line.split()[1]=='0100007F:'+format(port,'04X') and line.split()[3]=='0A'}
assert inodes and any(os.readlink(fd) in {'socket:['+i+']' for i in inodes} for fd in (proc/'fd').iterdir())
'''
    command('docker', 'exec', spec['container'], 'python3', '-c', proof, spec['base'], str(spec['port']), spec['container'])


def recover_completed_job(spec, request_id, expected_instance, runner_source):
    """Repair only a finished successful job blocked by zombie-only children."""
    current = read_status(spec)['status']
    assert current['instance_id'] == expected_instance, 'Runner 实例已变化，请刷新后重新核对'
    job_id = (current.get('active_job') or {}).get('id')
    assert job_id and current.get('fleet_requests', {}).get(request_id, {}).get('state') == 'unknown', '没有可核对的未知任务'
    proof = '''import json,os,sys,signal,time,hashlib
from pathlib import Path
base=Path(sys.argv[1]);job_id=sys.argv[2];request_id=sys.argv[3];source_sha=sys.argv[4]
assert job_id.startswith('real-') and all(c.isalnum() or c=='-' for c in job_id)
record_path=base/'results/jobs'/job_id/'record.json'
record=json.loads(record_path.read_text())
assert record['id']==job_id and record['fleet_request_id']==request_id
assert record['status']=='unknown' and record['exit_code']==0 and record.get('metrics') and record.get('completed_at'), '任务结果未确认成功，拒绝恢复'
assert record.get('error')=='任务进程组未确认退出，请管理员核对', '不是可恢复的进程组核对状态'
pgid=int(record['pid']);assert pgid>1
for entry in Path('/proc').iterdir():
 if not entry.name.isdecimal():continue
 try:fields=(entry/'stat').read_text().rsplit(')',1)[1].split()
 except FileNotFoundError:continue
 assert int(fields[2])!=pgid or fields[0] in ('Z','X','x'), '原任务仍有活跃进程，未进行恢复'
pid=int((base/'runner.pid').read_text());proc=Path('/proc')/str(pid)
assert pid>1 and pid!=pgid and (proc/'cwd').resolve()==base
args=(proc/'cmdline').read_bytes().split(bytes([0]))
assert any(a.endswith(b'runner.py') for a in args)
env=dict(e.split(b'=',1) for e in (proc/'environ').read_bytes().split(bytes([0])) if b'=' in e)
assert env.get(b'ASCEND_BENCH_HOME',b'').decode()==str(base)
source=base/'scripts/runner.py'
assert not source.is_symlink() and hashlib.sha256(source.read_bytes()).hexdigest()==source_sha, 'Runner 源码归属未确认'
updated=sys.stdin.read();assert updated and 'def process_group_running(' in updated
# Preserve both the archived result and the installed source before stopping.
stamp=str(time.time_ns())
record_path.with_name('record.before-recovery-'+stamp+'.json').write_bytes(record_path.read_bytes())
source.with_name('runner.before-recovery-'+stamp+'.py').write_bytes(source.read_bytes())
os.kill(pid,signal.SIGTERM)
for _ in range(50):
 try:state=(proc/'stat').read_text().rsplit(')',1)[1].split()[0]
 except FileNotFoundError:break
 if state in ('Z','X','x'):break
 time.sleep(.1)
else:raise RuntimeError('已核实的 Runner 未退出，拒绝启动第二个实例')
temporary=source.with_name('runner.py.recovery-tmp');temporary.write_text(updated);temporary.chmod(0o644);temporary.replace(source)
record.update(status='completed',error=None,recovered_at=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
temporary=record_path.with_name('record.json.recovery-tmp');temporary.write_text(json.dumps(record,ensure_ascii=False));temporary.replace(record_path)
print('verified')
'''
    subprocess.check_output(['docker','exec','-i',spec['container'],'python3','-c',proof,spec['base'],job_id,request_id,current['source_sha256']], input=runner_source.encode(), stderr=subprocess.PIPE, timeout=30)
    command('docker','exec','-d','-e','ASCEND_BENCH_MANAGED=1','-e','ASCEND_BENCH_HOME='+spec['base'],
            '-e','ASCEND_BENCH_CONTAINER='+spec['container'],'-e','ASCEND_BENCH_PORT='+str(spec['port']),
            spec['container'],'bash',spec['base']+'/scripts/start-runner.sh')
    for _ in range(20):
        try:
            status=read_status(spec)['status']
            if status['instance_id']!=expected_instance and status['activity']=='idle' and status['process_tree_confirmed_stopped']:
                return {'status':status,'request_id':request_id,'job_id':job_id}
        except Exception:
            pass
        time.sleep(1)
    raise ValueError('任务结果已保留，但 Runner 恢复未确认；中心占用仍保留，请核对日志')


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
    for key in ('ASCEND_BENCH_PROXY', 'ASCEND_BENCH_PIP_INDEX', 'ASCEND_BENCH_EXTRA_HOSTS','ASCEND_BENCH_PIP_CERT'):
        env.pop(key, None)
    if payload.get('pip_index'):
        env['ASCEND_BENCH_PIP_INDEX'] = payload['pip_index']
    for key, value in payload.get('network', {}).items():
        variable = {'proxy': 'ASCEND_BENCH_PROXY', 'pip_index': 'ASCEND_BENCH_PIP_INDEX', 'extra_hosts': 'ASCEND_BENCH_EXTRA_HOSTS','pip_cert':'ASCEND_BENCH_PIP_CERT'}[key]
        env[variable] = value
    env['ASCEND_BENCH_MANAGED'] = '1'
    print('调用原有 setup.sh：环境检查 → 拷贝 → AISBench → 启动', flush=True)
    lock_fd = payload.get('_deployment_lock_fd')
    subprocess.run(['bash', str(destination / 'ascend-bench-kit/setup.sh'), spec['container'], spec['base'], spec.get('ais_path', ''), str(spec['port'])], env=env, check=True,
                   pass_fds=() if lock_fd is None else (lock_fd,))
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
