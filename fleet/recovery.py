"""Install and register a tool-owned systemd recovery service over signed SSH."""
import json
from pathlib import Path


INSTALL_SERVICE = r'''
import json,os,subprocess,sys,time
from pathlib import Path
p=json.loads(PAYLOAD);root=Path(os.environ['BENCHWEB_MANAGEMENT_ROOT'])
assert root.resolve()==root and root.stat().st_uid==0 and len(root.name)==32 and all(c in '0123456789abcdef' for c in root.name)
source=root/'recovery_host.py';assert not source.is_symlink();source.write_text(p['source']);source.chmod(0o600)
unit='bench-web-'+root.name+'.service';path=Path('/etc/systemd/system')/unit
python='/usr/bin/python3'
clean={'PATH':'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'}
probe=subprocess.run([python,'-I','-S','-c','import sys,fcntl,ssl,http.server;assert sys.version_info>=(3,8)'],env=clean,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
assert probe.returncode==0, '服务器系统 Python 无法运行恢复服务；未依赖交互式 Conda 环境启动'
content='[Unit]\nDescription=BenchWeb tool-owned Runner recovery\nAfter=network-online.target docker.service\nWants=network-online.target\n\n[Service]\nType=simple\nExecStart='+python+' -I -S '+str(source)+'\nRestart=always\nRestartSec=5\nUMask=0077\nEnvironment="PATH='+clean['PATH']+'"\n\n[Install]\nWantedBy=multi-user.target\n'
assert not path.is_symlink()
if path.exists():assert (' '+str(source)+'\n') in path.read_text(), 'Systemd unit belongs to another install'
path.write_text(content);path.chmod(0o644)
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable','--now',unit],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
subprocess.run(['systemctl','restart',unit],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
for _ in range(20):
 if subprocess.run(['systemctl','is-active','--quiet',unit]).returncode==0:break
 time.sleep(.2)
else:raise ValueError('本工具恢复服务未启动，请查看 '+unit+' 日志')
print(json.dumps({'unit':unit,'enabled':True}))
'''


REGISTER = r'''
import hashlib,json,os,subprocess
from pathlib import Path
p=json.loads(PAYLOAD);spec=p['spec'];root=Path(os.environ['BENCHWEB_MANAGEMENT_ROOT'])
info=json.loads(subprocess.check_output(['docker','inspect',spec['container']]))[0]
matches=[m for m in info['Mounts'] if m.get('RW') and (spec['base']==m['Destination'] or spec['base'].startswith(m['Destination'].rstrip('/')+'/'))]
assert matches and info['State']['Running']
mount=max(matches,key=lambda m:len(m['Destination']));base=Path(mount['Source'])/spec['base'][len(mount['Destination']):].lstrip('/')
assert base.resolve()==base
gateway=base/'center-access'/p['access_id'];assert gateway.resolve()==gateway
config=json.loads((gateway/'access.json').read_text());assert config['spec']['base']==spec['base'] and config['spec']['container']==spec['container'] and config['spec']['port']==spec['port']
hashes=lambda folder,names:{name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in names}
row={'spec':spec,'host_base':str(base),'container_id':info['Id'],'active':True,'gateway_dir':str(gateway),'bind':config['bind'],'access_port':config['port'],
 'runner_hashes':hashes(base,['scripts/runner.py','scripts/start-runner.sh']),
 'gateway_hashes':hashes(gateway,['persistent_gateway.py','gateway.py','http_server.py','access.json','cert.pem','key.pem'])}
directory=root/'runtimes';directory.mkdir(mode=0o700,exist_ok=True)
key=hashlib.sha256(json.dumps([spec['profile_id'],spec['port']]).encode()).hexdigest()
for old in directory.glob('*.json'):
 data=json.loads(old.read_text())
 if data['spec']['profile_id']==spec['profile_id'] and (data['spec']['port']==spec['port'] or data['spec']['base']==spec['base']):
  data['active']=False;old.write_text(json.dumps(data))
path=directory/(key+'.json');temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(row));temporary.chmod(0o600);temporary.replace(path)
print(json.dumps({'recovery_registered':True,'container':spec['container']}))
'''


def install_service(session):
    payload = {'source': Path(__file__).with_name('recovery_host.py').read_text(encoding='utf-8')}
    return json.loads(session.execute(INSTALL_SERVICE.replace('PAYLOAD', repr(json.dumps(payload))), timeout=45))


def register(session, spec, access_id):
    from .managed_ssh import ManagedSession
    if not isinstance(session, ManagedSession):
        return False
    payload = {'spec': spec, 'access_id': access_id}
    session.execute(REGISTER.replace('PAYLOAD', repr(json.dumps(payload))), timeout=45)
    return True
