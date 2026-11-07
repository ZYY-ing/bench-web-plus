"""Appended to remote_helper.py. Reuse only a stopped/deleted container's project.

Old containers and inference processes are never stopped here. Private snapshots
cover shared program/config files, not results, datasets or dependency trees.
"""
import shutil
import signal
import secrets


def host_project(spec):
    if spec.get('container_mode') == 'new':
        base = Path(spec['host_path'])
    else:
        result = subprocess.run(['docker','inspect',spec['container']],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        if result.returncode:
            if not spec.get('host_path'):
                raise ValueError('旧容器已删除且缺少宿主机挂载记录，无法安全确认原目录')
            base = Path(spec['host_path'])
        else:
            info = json.loads(result.stdout)[0]
            matches = [m for m in info['Mounts'] if m.get('RW') and (spec['base']==m['Destination'] or spec['base'].startswith(m['Destination'].rstrip('/')+'/'))]
            if not matches:
                raise ValueError('项目目录缺少可确认的持久化 RW 挂载')
            mount = max(matches,key=lambda m:len(m['Destination']))
            base = Path(mount['Source'])/spec['base'][len(mount['Destination']):].lstrip('/')
    if base.resolve()!=base or not str(base).startswith(('/home/','/mnt/')) or len(base.parts)<4:
        raise ValueError('项目宿主目录含符号链接或不是专用子目录')
    return base


def verify_independent_projects(spec):
    base = host_project(spec)
    for peer in spec.get('_peers',[]):
        other = host_project(peer)
        if base==other or base in other.parents or other in base.parents:
            raise ValueError('两个 Runner 实际映射到相同或嵌套的宿主机目录；请使用独立挂载目录')


def tcp_port_listening(port, directory=Path('/proc/net')):
    # A connect() handshake can succeed while the listener is closing. Inspect
    # the kernel LISTEN state instead; established/TIME_WAIT sockets are not a
    # listening Runner and must not trigger an attempt to stop an absent B.
    found=False
    for name in ('tcp','tcp6'):
        path=directory/name
        try:lines=path.read_text().splitlines()[1:]
        except FileNotFoundError:continue
        found=True
        for line in lines:
            fields=line.split()
            if len(fields)>=4 and fields[3]=='0A' and int(fields[1].rsplit(':',1)[1],16)==port:return True
    if not found:raise ValueError('无法读取内核监听端口信息，未假定端口空闲')
    return False


def port_listening(port):
    return tcp_port_listening(port)


def wait_runner_port_free(port, timeout=5):
    deadline=time.monotonic()+timeout;free=0
    while time.monotonic()<deadline:
        free=free+1 if not port_listening(port) else 0
        if free>=2:return
        time.sleep(.1)
    raise ValueError('Runner 端口持续监听，未操作未知进程')


def stop_owned_idle_runner(spec):
    if not port_listening(spec['port']):
        wait_runner_port_free(spec['port'])
        return
    try:current = read_status(spec)['status']
    except Exception:
        # The process can exit between the LISTEN snapshot and HTTP request.
        # Waiting for an empty port does not authorize stopping another owner.
        wait_runner_port_free(spec['port'])
        return
    if current.get('activity')!='idle' or current.get('process_tree_confirmed_stopped') is not True:
        raise ValueError('旧 Runner 正在执行任务或状态未知，不能释放')
    verify_runner_owner(spec)
    # The centre reserves this Runner for deployment before dispatch. Verify
    # ownership and idle status again immediately before terminating its PID.
    code=r'''import json,os,signal,sys,time
from pathlib import Path
base=Path(sys.argv[1]);pid=int((base/'runner.pid').read_text());proc=Path('/proc')/str(pid)
assert pid>1 and (proc/'cwd').resolve()==base
assert any(a.endswith(b'runner.py') for a in (proc/'cmdline').read_bytes().split(bytes([0])))
env=dict(e.split(b'=',1) for e in (proc/'environ').read_bytes().split(bytes([0])) if b'=' in e)
assert env.get(b'ASCEND_BENCH_CONTAINER',b'').decode()==sys.argv[2]
assert env.get(b'ASCEND_BENCH_PORT',b'').decode()==sys.argv[3]
os.kill(pid,signal.SIGTERM)
for _ in range(50):
 if not proc.exists() or not (proc/'cmdline').read_bytes() or (proc/'stat').read_text().split()[2]=='Z':break
 time.sleep(.1)
else:raise ValueError('旧 Runner 进程未退出，停止替换')
'''
    command('docker','exec',spec['container'],'python3','-c',code,spec['base'],spec['container'],str(spec['port']))
    wait_runner_port_free(spec['port'])


def start_owned_runner(spec):
    if port_listening(spec['port']):
        read_status(spec)
        return
    command('docker','exec','-d','-w','/','-e','ASCEND_BENCH_MANAGED=1','-e','ASCEND_BENCH_HOME='+spec['base'],
            '-e','ASCEND_BENCH_CONTAINER='+spec['container'],'-e','ASCEND_BENCH_PORT='+str(spec['port']),
            spec['container'],'bash',spec['base']+'/scripts/start-runner.sh')
    for _ in range(20):
        try:
            row=read_status(spec)['status']
            if row.get('activity')=='idle' and row.get('process_tree_confirmed_stopped') is True:return
        except Exception:pass
        time.sleep(1)
    raise ValueError('原 Runner 恢复未通过核验')


@contextmanager
def replace_runner_transaction(spec):
    old=spec['_replace']
    assert old['profile_id']==spec['profile_id'] and old['port']==spec['port'] and old['base']==spec['base']
    base=host_project(old)
    if host_project(spec)!=base:
        raise ValueError('复用安装目录时须沿用同一个宿主机挂载目录，未释放旧 Runner')
    retired=retired_container(old)['retired']
    if not retired:
        raise ValueError('旧容器仍在运行，请先手动停止旧容器；不会自动释放或停止旧 Runner')
    was_running=False
    wait_runner_port_free(old['port'])
    names=('scripts','website','runner-config.json','runner-token','pip.conf')
    for name in names:
        path=base/name
        if path.is_symlink() or path.is_dir() and any(p.is_symlink() for p in path.rglob('*')):
            raise ValueError('旧 Runner 程序或配置含符号链接，停止替换')
    folder=base/'backups'/('replacement-'+secrets.token_hex(12))
    if folder.parent.is_symlink() or folder.parent.resolve()!=folder.parent:
        raise ValueError('原项目备份目录包含符号链接，停止替换')
    folder.mkdir(mode=0o700,parents=True)
    manifest={}
    for name in names:
        path=base/name;manifest[name]=path.exists()
        if path.is_dir():shutil.copytree(path,folder/name)
        elif path.is_file():shutil.copy2(path,folder/name)
    (folder/'transaction.json').write_text(json.dumps({'old':old,'new':{k:v for k,v in spec.items() if not k.startswith('_')},'state':'prepared','was_running':was_running}))
    # Retire only this authorization's matching recovery row. A manually stopped
    # old container stays stopped throughout installation and rollback.
    recovery=[]
    root=os.environ.get('BENCHWEB_MANAGEMENT_ROOT')
    if root:
        for path in (Path(root)/'runtimes').glob('*.json'):
            row=json.loads(path.read_text())
            if row.get('active') and all(row['spec'].get(k)==old[k] for k in ('profile_id','container','base','port')):
                recovery.append((path,row));path.write_text(json.dumps({**row,'active':False}))
    try:
        print('已确认旧容器停止/删除且原端口空闲；备份后复用目录，不启动旧容器',flush=True)
        yield
        read_status(spec)
        (folder/'transaction.json').write_text(json.dumps({'state':'committed','old':old,'new':{k:v for k,v in spec.items() if not k.startswith('_')}}))
    except BaseException as deployment_error:
        try:
            if port_listening(spec['port']):
                stop_owned_idle_runner(spec)
            wait_runner_port_free(spec['port'])
            for name in names:
                path=base/name
                if path.is_symlink() or path.resolve()!=path:raise ValueError('回退目标目录身份发生变化')
                if path.is_dir():shutil.rmtree(path)
                elif path.exists():path.unlink()
                if manifest[name]:
                    if (folder/name).is_dir():shutil.copytree(folder/name,path)
                    else:shutil.copy2(folder/name,path)
            for path,row in recovery:path.write_text(json.dumps(row))
            (folder/'transaction.json').write_text(json.dumps({'state':'rolled_back','old':old,'error':str(deployment_error)[-2000:]}))
            print('新 Runner 部署失败，已恢复旧程序和配置，旧容器保持停止；原始原因：'+str(deployment_error).strip().splitlines()[-1],flush=True)
        except Exception as rollback_error:
            raise ValueError('替换失败且回退未确认：'+str(rollback_error)) from rollback_error
        raise
