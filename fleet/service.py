"""Optional Windows logon task, limited to this tool and current user."""
import argparse
import contextlib
import os
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
TASK = 'BenchWebPlusCenter'
BOOT_TASK = TASK + 'Boot'


def task_user_sid(user):
    if user and user.startswith('S-1-'):
        return user
    if not user or sys.platform != 'win32':
        return user
    # Task Scheduler may normalize a principal SID into DOMAIN\username.
    # Resolve the account through Windows, rather than weakening ownership checks.
    environment = dict(os.environ, BENCH_TASK_OWNER=user)
    try:
        return subprocess.check_output([
            'powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
            '[System.Security.Principal.NTAccount]::new($env:BENCH_TASK_OWNER).Translate([System.Security.Principal.SecurityIdentifier]).Value'
        ], env=environment, text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        raise ValueError('无法确认后台任务的 Windows 用户身份，未执行修改') from None


def verify_owner(raw, sid, directory=None):
    # schtasks /Query can emit console-encoded bytes while declaring UTF-16.
    # Genuine UTF-16 task files still go directly to the XML parser.
    if isinstance(raw, bytes) and not (raw.startswith((b'\xff\xfe', b'\xfe\xff', b'<\x00', b'\x00<'))):
        if raw.startswith(b'\xef\xbb\xbf'):
            raw = raw.decode('utf-8-sig')
        elif sys.platform == 'win32':
            import ctypes
            kernel = ctypes.windll.kernel32
            page = kernel.GetConsoleOutputCP() or kernel.GetOEMCP()
            raw = raw.decode('cp' + str(page))
        else:
            raw = raw.decode('utf-8')
    tree = ET.fromstring(raw)
    directories = [e.text for e in tree.iter() if e.tag.endswith('WorkingDirectory')]
    users = [e.text for e in tree.iter() if e.tag.endswith('UserId')]
    arguments = [e.text for e in tree.iter() if e.tag.endswith('Arguments')]
    resolved = {user: task_user_sid(user) for user in set(users)}
    if directories != [str(directory or ROOT)] or not users or any(resolved[user] != sid for user in users) or arguments != ['-X utf8 -m fleet.service run']:
        raise ValueError('同名后台任务不属于当前安装目录/用户，未修改；请核对旧安装')


def task_xml(sid, executable, startup=False, directory=None):
    ns = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('', ns)
    def add(parent, tag, text=None):
        e = ET.SubElement(parent, '{'+ns+'}'+tag)
        e.text = text
        return e
    root = ET.Element('{'+ns+'}Task', version='1.2')
    trigger = add(add(root,'Triggers'),'BootTrigger' if startup else 'LogonTrigger')
    add(trigger,'Enabled','true')
    if not startup:add(trigger,'UserId',sid)
    principal = add(add(root,'Principals'),'Principal');principal.set('id','Owner')
    add(principal,'UserId','S-1-5-18' if startup else sid)
    # ServiceAccount is a COM/API enum, not a valid XML LogonType value.
    # SYSTEM is identified by its SID; omit the optional XML element.
    if not startup:
        add(principal,'LogonType','InteractiveToken')
    add(principal,'RunLevel','LeastPrivilege')
    settings = add(root,'Settings')
    add(settings,'MultipleInstancesPolicy','IgnoreNew')
    add(settings,'DisallowStartIfOnBatteries','false');add(settings,'StopIfGoingOnBatteries','false')
    add(settings,'ExecutionTimeLimit','PT0S');add(settings,'StartWhenAvailable','true')
    if startup:
        restart=add(settings,'RestartOnFailure');add(restart,'Interval','PT1M');add(restart,'Count','999')
    actions = add(root,'Actions');actions.set('Context','Owner')
    execute = add(actions,'Exec')
    add(execute,'Command',str(executable));add(execute,'Arguments','-X utf8 -m fleet.service run');add(execute,'WorkingDirectory',str(directory or ROOT))
    return ET.tostring(root,encoding='utf-16',xml_declaration=True)


def boot_directory():
    import ctypes
    buffer=ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None,35,None,0,buffer)!=0:
        raise ValueError('无法定位系统 ProgramData 目录')
    return Path(buffer.value).resolve()/'BenchWebPlus'


def validate_boot_xml(raw):
    """Use Windows' real schema validator without registering or running a task."""
    environment = dict(os.environ, BENCH_TASK_XML=raw.decode('utf-16'))
    script = r'''$ErrorActionPreference='Stop'
$service=New-Object -ComObject Schedule.Service
$service.Connect()
$definition=$service.NewTask(0)
$definition.XmlText=$env:BENCH_TASK_XML
$service.GetFolder('\').RegisterTask('BenchWebPlusXmlValidationOnly',$definition.XmlText,1,'SYSTEM',$null,5,$null) | Out-Null
'''
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
                            env=environment, text=True, capture_output=True)
    if result.returncode:
        raise ValueError('Windows 开机任务 XML 校验失败，未复制安装或创建任务：' + result.stderr.strip()[-1000:])


def copy_app_sources(app, preserve_data=False):
    """Updates keep the protected centre's live accounts and SSH credentials."""
    ignore=shutil.ignore_patterns('__pycache__','*.pyc','.git','outputs')
    for name in ('fleet','dist','.fleet-deps','.fleet-data'):
        if preserve_data and name=='.fleet-data':
            continue
        source=ROOT/name
        if source.is_dir():shutil.copytree(source,app/name,dirs_exist_ok=True,ignore=ignore)
    for name in ('fleet-config.json','requirements-admin.txt'):
        if preserve_data and name=='fleet-config.json' and (app/name).exists():
            continue
        if (ROOT/name).is_file():shutil.copy2(ROOT/name,app/name)


def wait_port_free(listen, timeout=20):
    """Task Scheduler /End returns before the process releases its sockets."""
    deadline=time.monotonic()+timeout
    announced=False
    while True:
        try:
            with socket.socket(socket.AF_INET6 if ':' in listen.get('bind','') else socket.AF_INET) as probe:
                probe.bind((listen.get('bind','127.0.0.1'),listen['port']))
            return
        except OSError as exc:
            if time.monotonic()>=deadline:
                raise ValueError(f"中心端口 {listen['port']} 在等待 {timeout} 秒后仍不可用：{exc}；未结束未知进程，请确认旧中心已停止") from None
            if not announced:
                print(f"等待旧中心释放 {listen['port']} 端口…",flush=True)
                announced=True
            time.sleep(.5)


def install_boot():
    """One-time elevated install into SYSTEM-owned, non-user-writable paths.

    Never runs SYSTEM from the editable workspace or a user-writable Python.
    Server passwords and Windows account passwords are not requested/stored.
    """
    import ctypes
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise ValueError('开机自动启动需要一次 Windows 管理员权限安装；请以管理员身份运行 install-center-autostart.cmd。未修改已有启动任务')
    settings=ROOT/'.fleet-data/listen.json'
    if not settings.is_file():raise ValueError('请先完成中心配置')
    listen=json.loads(settings.read_text(encoding='utf-8'))
    wait_port_free(listen)
    target=boot_directory();app=target/'app';runtime=target/'runtime';marker=target/'install.json'
    if target.exists():
        if target.resolve()!=target or not marker.is_file():raise ValueError('系统安装目录已有未知内容，未覆盖')
        previous=json.loads(marker.read_text(encoding='utf-8'))
        if previous['source']!=str(ROOT) and ROOT!=app:raise ValueError('系统目录属于另一安装，未覆盖')
    preserve_data=(app/'.fleet-data/listen.json').is_file()
    if preserve_data:
        listen=json.loads((app/'.fleet-data/listen.json').read_text(encoding='utf-8'))
        wait_port_free(listen)
    source_runtime=Path(sys.base_prefix).resolve()
    if len(source_runtime.parts)<3 or not (source_runtime/'python.exe').is_file() or not (source_runtime/'Lib').is_dir():
        raise ValueError('需要可复制的完整 Windows Python 运行时，未注册系统启动任务')
    if ROOT==app:raise ValueError('请从待更新的原始安装目录运行开机安装器')
    # Validate before copying files or changing permissions, even on first install.
    validate_boot_xml(task_xml('S-1-5-18',runtime/'pythonw.exe',startup=True,directory=app))
    target.mkdir(parents=True,exist_ok=True)
    subprocess.run(['icacls.exe',str(target),'/inheritance:r','/grant:r','*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F'],check=True,stdout=subprocess.DEVNULL)
    app.mkdir(exist_ok=True)
    marker.write_text(json.dumps({'source':str(ROOT),'app':str(app),'runtime':str(runtime)}),encoding='utf-8')
    ignore=shutil.ignore_patterns('__pycache__','*.pyc','.git','outputs')
    copy_app_sources(app,preserve_data)
    shutil.copytree(source_runtime,runtime,dirs_exist_ok=True,ignore=ignore)
    # Rebind certificate paths to the protected copy, preserving existing identity.
    if not preserve_data:
        for field in ('cert','key'):
            original=Path(listen[field])
            copied=app/'.fleet-data'/('boot-'+field+'.pem')
            shutil.copy2(original,copied);listen[field]=str(copied)
    (app/'.fleet-data/listen.json').write_text(json.dumps(listen,ensure_ascii=False,indent=2),encoding='utf-8')
    executable=runtime/('pythonw.exe' if (runtime/'pythonw.exe').exists() else 'python.exe')
    paths=json.loads(subprocess.check_output([str(runtime/'python.exe'),'-I','-c',
        'import ssl,sqlite3,ctypes,sys,json;print(json.dumps([sys.prefix,sys.base_prefix,*sys.path]))'],text=True))
    if not all(Path(value).resolve().is_relative_to(runtime.resolve()) for value in paths):
        raise ValueError('复制的 Python 仍引用系统目录以外的运行时路径，未注册 SYSTEM 任务')
    # Explicitly remove user ownership as well as inherited write permissions.
    subprocess.run(['icacls.exe',str(target),'/inheritance:r','/grant:r','*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F'],check=True,stdout=subprocess.DEVNULL)
    subprocess.run(['icacls.exe',str(target),'/setowner','*S-1-5-18','/T','/C'],check=True,stdout=subprocess.DEVNULL)
    task=target/'task.xml';task.write_bytes(task_xml('S-1-5-18',executable,startup=True,directory=app))
    existing=subprocess.run(['schtasks.exe','/Query','/TN',BOOT_TASK,'/XML'],capture_output=True)
    if existing.returncode==0:verify_owner(existing.stdout,'S-1-5-18',app)
    command=['schtasks.exe','/Create','/TN',BOOT_TASK,'/XML',str(task)]
    if existing.returncode==0:command.append('/F')
    subprocess.run(command,input='N\n',text=True,check=True)
    subprocess.run(['schtasks.exe','/Run','/TN',BOOT_TASK],check=True)
    print('开机中心已安装到受保护的系统目录；无需登录 Windows，无需保存 Windows 或服务器密码。')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action',choices=['install','install-boot','start','status','stop','remove','run'])
    parser.add_argument('--boot',action='store_true',help='管理受保护系统目录中的开机任务')
    args = parser.parse_args()
    if args.action == 'run':
        from .app import main as center
        directory = ROOT / '.fleet-data';directory.mkdir(exist_ok=True)
        log = directory / 'center.log'
        if log.exists() and log.stat().st_size > 5000000:
            log.replace(directory / 'center.previous.log')
        sys.argv = [sys.argv[0]]
        with log.open('a',encoding='utf-8',buffering=1) as output, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            failures=0
            while True:
                try:
                    center()
                    return
                except Exception:
                    traceback.print_exc()
                    failures+=1
                    time.sleep(min(60,2**min(failures,6)))
        return
    if sys.platform != 'win32':
        raise SystemExit('自动常驻安装仅适用于 Windows；其他系统可由系统服务管理器运行 start-fleet.sh')
    if args.action=='install-boot':
        install_boot();return
    sid = subprocess.check_output(['powershell.exe','-NoProfile','-Command','[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],text=True).strip()
    task_name=BOOT_TASK if args.boot else TASK
    existing = subprocess.run(['schtasks.exe','/Query','/TN',task_name,'/XML'],capture_output=True)
    if existing.returncode == 0:
        verify_owner(existing.stdout, 'S-1-5-18' if args.boot else sid, boot_directory()/'app' if args.boot else None)
    if args.action == 'install':
        if not (ROOT/'.fleet-data/listen.json').is_file():
            raise ValueError('请先运行 python -m fleet.configure')
        executable = Path(sys.executable).with_name('pythonw.exe')
        if not executable.exists():
            executable = Path(sys.executable)
        xml = ROOT/'.fleet-data/center-task.xml'
        xml.write_bytes(task_xml(sid,executable))
        command = ['schtasks.exe','/Create','/TN',task_name,'/XML',str(xml)]
        if existing.returncode == 0:
            command.append('/F')
        subprocess.run(command,input='N\n',text=True,check=True)
    else:
        if existing.returncode != 0:
            raise ValueError('后台任务不存在或无法核实，未执行修改')
        verb = {'start':'/Run','status':'/Query','stop':'/End','remove':'/Delete'}[args.action]
        command = ['schtasks.exe',verb,'/TN',task_name]
        if args.action == 'remove':command.append('/F')
        subprocess.run(command,check=True)
        if args.action == 'stop':
            settings=(boot_directory()/'app' if args.boot else ROOT)/'.fleet-data/listen.json'
            if settings.is_file():wait_port_free(json.loads(settings.read_text(encoding='utf-8')))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, ET.ParseError) as exc:
        raise SystemExit('后台任务操作未完成：' + str(exc))
