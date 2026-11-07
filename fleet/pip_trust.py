"""Export the centre OS trust store for a deployment, never disable HTTPS."""
import hashlib
import ssl


def system_ca_bundle():
    certificates=ssl.create_default_context().get_ca_certs(binary_form=True)
    if not certificates:
        raise ValueError('中心系统未提供可信 CA，请明确配置包源 CA 文件；不会关闭证书校验')
    return ''.join(ssl.DER_cert_to_PEM_cert(cert) for cert in sorted(set(certificates)))


def trust_payload(network):
    if network.get('pip_cert'):
        return None
    pem=system_ca_bundle()
    return {'pem':pem,'sha256':hashlib.sha256(pem.encode()).hexdigest()}


REMOTE = r'''
import hashlib,json,os,subprocess
from pathlib import Path
def prepare_pip_trust(spec,trust):
 if not trust:return None
 assert hashlib.sha256(trust['pem'].encode()).hexdigest()==trust['sha256']
 path=spec['base']+'/.pip-trust/center-'+trust['sha256'][:16]+'.pem'
 code="""import hashlib,json,sys,ssl
from pathlib import Path
p=json.loads(sys.stdin.read());path=Path(p['path'])
assert path.parent.resolve()==path.parent and not path.is_symlink()
raw=p['pem'].encode();assert hashlib.sha256(raw).hexdigest()==p['sha256']
ssl.create_default_context(cadata=p['pem'])
path.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
temporary=path.with_suffix('.writing');assert not temporary.is_symlink()
temporary.write_bytes(raw);temporary.chmod(0o644);temporary.replace(path)
print('trusted')
"""
 data=json.dumps({**trust,'path':path}).encode()
 subprocess.run(['docker','exec','-i','-w','/',spec['container'],'python3','-c',code],input=data,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=20,check=True)
 return path
'''
