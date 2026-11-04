"""Optional Windows logon task, limited to this tool and current user."""
import argparse
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
TASK = 'BenchWebPlusCenter'


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


def verify_owner(raw, sid):
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
    if directories != [str(ROOT)] or not users or any(resolved[user] != sid for user in users) or arguments != ['-X utf8 -m fleet.service run']:
        raise ValueError('同名后台任务不属于当前安装目录/用户，未修改；请核对旧安装')


def task_xml(sid, executable):
    ns = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('', ns)
    def add(parent, tag, text=None):
        e = ET.SubElement(parent, '{'+ns+'}'+tag)
        e.text = text
        return e
    root = ET.Element('{'+ns+'}Task', version='1.2')
    trigger = add(add(root,'Triggers'),'LogonTrigger')
    add(trigger,'Enabled','true');add(trigger,'UserId',sid)
    principal = add(add(root,'Principals'),'Principal');principal.set('id','Owner')
    add(principal,'UserId',sid);add(principal,'LogonType','InteractiveToken');add(principal,'RunLevel','LeastPrivilege')
    settings = add(root,'Settings')
    add(settings,'MultipleInstancesPolicy','IgnoreNew')
    add(settings,'DisallowStartIfOnBatteries','false');add(settings,'StopIfGoingOnBatteries','false')
    add(settings,'ExecutionTimeLimit','PT0S');add(settings,'StartWhenAvailable','true')
    actions = add(root,'Actions');actions.set('Context','Owner')
    execute = add(actions,'Exec')
    add(execute,'Command',str(executable));add(execute,'Arguments','-X utf8 -m fleet.service run');add(execute,'WorkingDirectory',str(ROOT))
    return ET.tostring(root,encoding='utf-16',xml_declaration=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action',choices=['install','start','status','stop','remove','run'])
    args = parser.parse_args()
    if args.action == 'run':
        from .app import main as center
        directory = ROOT / '.fleet-data';directory.mkdir(exist_ok=True)
        log = directory / 'center.log'
        if log.exists() and log.stat().st_size > 5000000:
            log.replace(directory / 'center.previous.log')
        sys.argv = [sys.argv[0]]
        with log.open('a',encoding='utf-8',buffering=1) as output, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            for _ in range(5):
                try:
                    center()
                    return
                except Exception:
                    traceback.print_exc()
                    time.sleep(5)
        return
    if sys.platform != 'win32':
        raise SystemExit('自动常驻安装仅适用于 Windows；其他系统可由系统服务管理器运行 start-fleet.sh')
    sid = subprocess.check_output(['powershell.exe','-NoProfile','-Command','[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],text=True).strip()
    existing = subprocess.run(['schtasks.exe','/Query','/TN',TASK,'/XML'],capture_output=True)
    if existing.returncode == 0:
        verify_owner(existing.stdout, sid)
    if args.action == 'install':
        if not (ROOT/'.fleet-data/listen.json').is_file():
            raise ValueError('请先运行 python -m fleet.configure')
        executable = Path(sys.executable).with_name('pythonw.exe')
        if not executable.exists():
            executable = Path(sys.executable)
        xml = ROOT/'.fleet-data/center-task.xml'
        xml.write_bytes(task_xml(sid,executable))
        command = ['schtasks.exe','/Create','/TN',TASK,'/XML',str(xml)]
        if existing.returncode == 0:
            command.append('/F')
        subprocess.run(command,input='N\n',text=True,check=True)
    else:
        if existing.returncode != 0:
            raise ValueError('后台任务不存在或无法核实，未执行修改')
        verb = {'start':'/Run','status':'/Query','stop':'/End','remove':'/Delete'}[args.action]
        command = ['schtasks.exe',verb,'/TN',TASK]
        if args.action == 'remove':command.append('/F')
        subprocess.run(command,check=True)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, ET.ParseError) as exc:
        raise SystemExit('后台任务操作未完成：' + str(exc))
