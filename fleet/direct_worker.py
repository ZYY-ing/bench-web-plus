"""Appended to remote_helper.py, executed only over an authenticated SSH session."""
def direct_main(payload):
    with remote_deployment_lock(payload['spec']) as lock_fd:
        if payload['spec'].get('_replace'):
            with replace_runner_transaction(payload['spec']):
                return direct_main_locked({**payload, '_deployment_lock_fd':lock_fd})
        return direct_main_locked({**payload, '_deployment_lock_fd':lock_fd})


def direct_main_locked(payload):
    spec = payload['spec']
    if spec.get('_peers'):
        verify_independent_projects(spec)
    print('检查所选容器与安装目录', flush=True)
    if spec.get('container_mode') == 'new':
        # Use a local image only: no pulling, privileged mode, devices or model service.
        image = spec['image']
        command('docker', 'image', 'inspect', image)
        existing = subprocess.run(['docker', 'inspect', spec['container']], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if existing:
            raise ValueError('同名容器已存在；请选择已有容器模式，不自动覆盖')
        for source in ('/mnt', '/home'):
            if not Path(source).is_dir():
                raise ValueError('宿主机默认挂载目录不存在：' + source)
        if port_listening(spec['port']):
            raise ValueError('Runner 端口 '+str(spec['port'])+' 已有进程监听；未创建新容器，请使用空闲端口，或先手动停止原专用 Runner 容器')
        host_path = Path(spec['host_path'])
        if host_path.resolve() != host_path:
            raise ValueError('挂载目录包含符号链接，停止创建')
        host_path.mkdir(parents=True, exist_ok=True)
        command('docker', 'run', '-d', '--name', spec['container'], '--network', 'host', '--restart', 'unless-stopped',
                '--label', 'ascend-bench.managed=true',
                '--mount', 'type=bind,src=/mnt,dst=/mnt,readonly',
                '--mount', 'type=bind,src=/home,dst=/home,readonly',
                '-v', str(host_path) + ':' + spec['base'] + ':rw',
                '--entrypoint', '/bin/bash', image, '-c', 'exec sleep infinity')
        print('已用本地镜像创建独立测试容器，不挂 NPU、不启动 vLLM', flush=True)
        print('宿主机 /mnt 和 /home 已同路径只读挂载；Runner 安装目录单独保持可写', flush=True)
    else:
        info = json.loads(command('docker', 'inspect', spec['container']))[0]
        if info['HostConfig']['NetworkMode'] != 'host':
            raise ValueError('所选容器不是 host 网络；请使用 host 网络测试容器')
        if not any(m.get('RW') and (spec['base'] == m['Destination'] or spec['base'].startswith(m['Destination'].rstrip('/') + '/')) for m in info['Mounts']):
            raise ValueError('安装目录缺少持久化 RW 挂载，请核对后重试')
        if info['State'].get('Paused'):
            print('所选容器已暂停，正在恢复容器', flush=True)
            command('docker', 'unpause', spec['container'])
        if not info['State']['Running']:
            print('所选容器为 ' + info['State']['Status'] + '，自动启动容器（执行其原有启动命令）', flush=True)
            command('docker', 'start', spec['container'])
            print('已启动选定的已有容器', flush=True)
        else:
            print('容器已运行，无需重复启动', flush=True)
    container_info(spec)
    if payload.get('pip_trust'):
        certificate=prepare_pip_trust(spec,payload['pip_trust'])
        payload={**payload,'network':{**payload.get('network',{}),'pip_cert':certificate}}
        print('已同步中心系统信任的 CA，本次 pip 保持 HTTPS 证书校验',flush=True)
    release = payload['release']
    import base64,zlib
    benchmark_probe=base64.b64encode(zlib.compress(json.dumps(release.get('benchmark_files',{})).encode())).decode()
    print('目标 Runner 文件版本：' + release['id'][:16], flush=True)
    probe_code = '''import hashlib,json,os,sys,subprocess,shutil,base64,zlib
from pathlib import Path
base=Path(sys.argv[1]);expected=json.loads(sys.argv[2]);actual={}
for name in expected:
 p=base/name
 actual[name]=hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
identity=sys.argv[3]
expected_benchmark=sys.argv[5] if len(sys.argv)>5 else ''
expected_sources=json.loads(zlib.decompress(base64.b64decode(sys.argv[6]))) if len(sys.argv)>6 else {}
ais=str(base/'environments'/identity/'bench-venv/bin/ais_bench')
cfg={};config_valid=False
try:
 cfg=json.loads((base/'runner-config.json').read_text());candidate=cfg.get('ais_bench_path')
 config_valid=isinstance(cfg,dict)
 changed=cfg.get('container') not in (None,'',sys.argv[4]) or cfg.get('installation_container_id',identity)!=identity
 foreign=candidate and candidate.startswith(str(base)+'/') and (changed or '/environments/' in candidate) and not candidate.startswith(str(base/'environments'/identity)+'/')
 if candidate and not foreign:ais=candidate
except (OSError,ValueError,TypeError,AttributeError):cfg={}
own=str(base/'environments'/identity)+'/'
candidates=[ais]+[p for p in (shutil.which('ais_bench'),'/usr/local/python3.12.13/bin/ais_bench','/usr/local/bin/ais_bench') if p and (not p.startswith(str(base)+'/') or p.startswith(own))]
ais=''
for candidate in dict.fromkeys(candidates):
 if not candidate or not os.path.isabs(candidate) or not os.path.isfile(candidate) or not os.access(candidate,os.X_OK):continue
 if expected_benchmark:
  if candidate!=str(base/'environments'/identity/'bench-venv/bin/ais_bench'):continue
  try:
   marker=json.loads((base/'environments'/identity/'benchmark-release.json').read_text())
   matched=marker.get('benchmark_id')==expected_benchmark
  except (OSError,ValueError):matched=False
  if not matched:
   source=base/'environments'/identity/'benchmark'
   matched=bool(expected_sources)
   for name,digest in expected_sources.items():
    relative=Path(name)
    if not name.startswith('benchmark/') or '..' in relative.parts:matched=False;break
    path=source.joinpath(*relative.parts[1:])
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:matched=False;break
   if not matched:continue
 try:
  compatibility=base/'scripts/runtime_compat.py'
  argv=[candidate,'--help']
  if expected.get('scripts/runtime_compat.py') and actual.get('scripts/runtime_compat.py')==expected['scripts/runtime_compat.py']:
   argv=[sys.executable,'-I','-S',str(compatibility)]+argv
  result=subprocess.run(argv,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30,env={**os.environ,'TORCH_DEVICE_BACKEND_AUTOLOAD':'0','ASCEND_BENCH_PROCESS_ROLE':'aisbench','ASCEND_BENCH_API_ONLY':'1','PYTHONPATH':str(base/'scripts')})
  if result.returncode==0:ais=candidate;break
 except (OSError,subprocess.TimeoutExpired):pass
running=False
try:
 pid=int((base/'runner.pid').read_text());proc=Path('/proc')/str(pid)
 running=(proc/'cwd').resolve()==base and any(a.endswith(b'runner.py') for a in (proc/'cmdline').read_bytes().split(bytes([0])))
except (OSError,ValueError):pass
print(json.dumps({'installed':(base/'scripts/runner.py').is_file(),'latest':actual==expected,'version':hashlib.sha256(json.dumps(actual,sort_keys=True).encode()).hexdigest(),'different':[k for k in expected if actual[k]!=expected[k]],'ais_path':ais,'running':running,'configured_container':cfg.get('container'),'configured_ais_path':cfg.get('ais_bench_path'),'config_valid':config_valid}))
'''
    container_id = json.loads(command('docker','inspect',spec['container']))[0].get('Id','')
    installed = json.loads(command('docker', 'exec', spec['container'], 'python3', '-c', probe_code, spec['base'], json.dumps(release['files']),container_id,spec['container'],release.get('benchmark_id',''),benchmark_probe))
    print(('检测到已安装 Runner，当前文件版本：' + installed['version'][:16]) if installed['installed'] else '尚未安装 Runner，将安装当前工具包中的最新版', flush=True)
    if installed['installed']:
        print('版本判断：' + ('已是最新版' if installed['latest'] else '需要更新；差异文件：' + ', '.join(installed['different'][:8])), flush=True)
    if installed['ais_path']:
        print('AISBench 可执行文件已存在，将复用当前依赖环境', flush=True)
    else:
        print('AISBench 不存在或不可执行，需要安装依赖环境', flush=True)
    occupied = command('docker', 'exec', spec['container'], 'python3', '-c',
        'import socket,sys;s=socket.socket();print(s.connect_ex(("127.0.0.1",int(sys.argv[1])))==0)', str(spec['port'])) == 'True'
    if occupied:
        try:
            checked = read_status(spec, allow_legacy_container=True)
        except ValueError as exc:
            raise ValueError('端口 '+str(spec['port'])+' 已被监听，但无法核验属于所选 Runner：'+str(exc)+'；未停止或改写监听进程。保留原服务时请使用独立端口和目录新增') from None
        current = checked['status']
        if checked.get('legacy_container'):
            print('原目录保留旧容器名；已核实当前容器的 Runner PID、启动环境和监听端口归属，将迁移容器身份并保留原配置与结果', flush=True)
        if not checked.get('legacy_container') and installed['latest'] and installed['ais_path'] and installed.get('configured_ais_path',installed['ais_path']) == installed['ais_path'] and current.get('source_sha256') == payload['runner_sha256']:
            print('已安装最新版 Runner，复用当前进程', flush=True)
            return
        if current.get('activity') != 'idle' or not current.get('process_tree_confirmed_stopped'):
            raise ValueError('已有 Runner 非空闲，停止升级')
        # Stop only this installation's verified Runner PID, never use pkill.
        stop_code = '''import os,signal,sys,time
from pathlib import Path
base=Path(sys.argv[1]);pid=int((base/'runner.pid').read_text())
cmd=Path('/proc')/str(pid)/'cmdline';cwd=Path('/proc')/str(pid)/'cwd'
args=cmd.read_bytes().split(bytes([0]))
assert any(a.endswith(b'runner.py') for a in args) and cwd.resolve()==base, 'Runner PID 身份不匹配'
os.kill(pid,signal.SIGTERM)
for _ in range(50):
 if not cmd.exists() or not cmd.read_bytes() or (cmd.parent/'stat').read_text().split()[2]=='Z':break
 time.sleep(.1)
else:raise RuntimeError('旧 Runner 未退出，停止升级')
pidfile=base/'runner.pid'
if pidfile.exists() and pidfile.read_text().strip()==str(pid):pidfile.unlink()
'''
        command('docker', 'exec', spec['container'], 'python3', '-c', stop_code, spec['base'])
        print('旧版空闲 Runner 已核对身份并停止，将升级项目文件', flush=True)
    else:
        if installed.get('configured_container') not in (None,'',spec['container']) and not spec.get('_replace'):
            raise ValueError('该安装目录属于另一个容器；请先手动停止原专用容器后复用，或选择独立目录和端口新增，未覆盖原文件')
        if installed['running']:
            raise ValueError('该目录已有 Runner 进程，但所选端口未监听；请核对原端口，不重复启动')
        if installed['latest'] and installed['ais_path'] and installed.get('config_valid',True):
            print('Runner 已是最新版但未运行，直接启动', flush=True)
            update_config = '''import json,sys
from pathlib import Path
base=Path(sys.argv[1]);path=base/'runner-config.json';cfg=json.loads(path.read_text());cfg.update(container=sys.argv[2],installation_container_id=sys.argv[3],ais_bench_path=sys.argv[4]);temporary=path.with_suffix('.starting-tmp');temporary.write_text(json.dumps(cfg));temporary.replace(path)
'''
            command('docker','exec',spec['container'],'python3','-c',update_config,spec['base'],spec['container'],container_id,installed['ais_path'])
            command('docker', 'exec', '-d', '-e', 'ASCEND_BENCH_MANAGED=1', '-e', 'ASCEND_BENCH_HOME=' + spec['base'], '-e', 'ASCEND_BENCH_CONTAINER=' + spec['container'],
                    '-e', 'ASCEND_BENCH_PORT=' + str(spec['port']), spec['container'], 'bash', spec['base'] + '/scripts/start-runner.sh')
            last_error = ''
            print('等待 Runner 启动并检查身份；启动期间连接暂不可用会自动重试', flush=True)
            for _ in range(10):
                try:
                    read_status(spec)
                    print('已安装最新版，启动与身份检查通过', flush=True)
                    return
                except Exception as exc:
                    last_error = str(exc).strip().splitlines()[-1]
                    time.sleep(1)
            raise ValueError('Runner 启动未通过健康检查：' + last_error)
    setup_spec = {**spec, 'ais_path': installed['ais_path']}
    print('安装/更新最新版文件，随后自动启动 Runner', flush=True)
    try:
        main({'action':'deploy','spec':setup_spec,'archive':payload['archive'],'sha256':payload['sha256'], 'pip_index':payload.get('pip_index'), 'network':payload.get('network', {}), '_deployment_lock_fd':payload.get('_deployment_lock_fd')})
    except Exception:
        # If setup failed before changing files, restore only the verified original
        # installation. Never start a partially replaced or unknown process.
        if occupied and installed['installed'] and installed['ais_path']:
            try:
                unchanged = json.loads(command('docker', 'exec', spec['container'], 'python3', '-c', probe_code, spec['base'], json.dumps(release['files']),container_id,spec['container'],release.get('benchmark_id',''),benchmark_probe))
                listening = command('docker', 'exec', spec['container'], 'python3', '-c',
                    'import socket,sys;s=socket.socket();print(s.connect_ex(("127.0.0.1",int(sys.argv[1])))==0)', str(spec['port'])) == 'True'
                if unchanged['version'] == installed['version'] and not listening:
                    command('docker', 'exec', '-d', '-e', 'ASCEND_BENCH_MANAGED=1', '-e', 'ASCEND_BENCH_HOME=' + spec['base'], '-e', 'ASCEND_BENCH_CONTAINER=' + spec['container'],
                            '-e', 'ASCEND_BENCH_PORT=' + str(spec['port']), spec['container'], 'bash', spec['base'] + '/scripts/start-runner.sh')
                    for _ in range(10):
                        try:
                            read_status(spec)
                            print('安装未完成，原文件未改变，已恢复原 Runner；本次部署仍记为失败', flush=True)
                            break
                        except Exception:time.sleep(1)
            except Exception:
                print('原 Runner 未能自动恢复，请检查日志；未操作其他进程', flush=True)
        raise
    verified = json.loads(command('docker', 'exec', spec['container'], 'python3', '-c', probe_code, spec['base'], json.dumps(release['files']),container_id,spec['container'],release.get('benchmark_id',''),benchmark_probe))
    if not verified['latest'] or not verified['ais_path']:
        raise ValueError('安装后版本或 AISBench 可执行检查未通过')
    print('安装后文件版本与目标完全一致，Runner 已启动', flush=True)
    print('网页已自动接入：打开用户评测入口，刷新并选择 Runner 后开始配置测试', flush=True)
