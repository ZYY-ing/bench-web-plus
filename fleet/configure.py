"""Interactive, one-time setup on the chosen center PC."""
import getpass
import json
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit
from .app import Store
from .accounts import Accounts
from .certificates import issue


def prompt_first_administrator(accounts):
    print('密码输入时不会显示字符或星号；输入完成后按回车。')
    while True:
        name = input('首个管理员账号: ').strip()
        while True:
            password = getpass.getpass('管理员密码（12–256字符）: ')
            confirmation = getpass.getpass('再次输入相同密码: ')
            if password != confirmation:
                print('两次密码不一致，请重新输入两次密码；账号和前面的网络设置无需重填。')
                continue
            if not 12 <= len(password) <= 256:
                print('密码长度需要12–256个字符，请重新输入。')
                continue
            try:
                accounts.create(name, password)
            except ValueError as exc:
                print(str(exc) + '，请重新填写管理员账号和密码。')
                break
            finally:
                password = confirmation = None
            return


def main():
    root = Path(__file__).resolve().parent.parent
    data = root / '.fleet-data'
    public_url = input('用户访问中心的网址（例如 https://管理电脑IP:19881）: ').strip().rstrip('/')
    parsed = urlsplit(public_url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('请输入纯 HTTPS 地址，不含路径、账号密码或查询参数')
    port = parsed.port or 443
    if not 1 <= port <= 65535:
        raise ValueError('端口范围无效')
    allowed = [str(ipaddress.ip_network(v.strip(),strict=False)) for v in input('允许访问的用户网段 CIDR（例如192.168.1.0/24；多个用逗号分隔）: ').split(',') if v.strip()]
    if not allowed or any(ipaddress.ip_network(v).prefixlen == 0 for v in allowed):
        raise ValueError('请明确指定用户网段，不允许向整个互联网开放匿名用户入口')
    data.mkdir(exist_ok=True)
    store = Store(data, recover=False)
    try:
        accounts = Accounts(store)
        if not accounts.list():
            prompt_first_administrator(accounts)
        custom = input('已有可信证书？填证书 PEM 路径；留空生成本中心测试证书: ').strip()
        if custom:
            cert = Path(custom).resolve()
            key = Path(input('对应私钥 PEM 路径（仅留在中心电脑）: ').strip()).resolve()
            import ssl
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
        else:
            directory = data / 'tls'
            material = issue(parsed.hostname, directory)
            cert, key = directory / 'server-cert.pem', directory / 'server-key.pem'
            cert.write_text(material['cert'], encoding='utf-8')
            key.write_text(material['key'], encoding='utf-8');key.chmod(0o600)
            print('测试证书：用户首次需信任 .fleet-data/tls/ca.pem；不要分发任何私钥。')
        settings = {'bind':'0.0.0.0', 'public_url':public_url, 'port':port, 'cert':str(cert), 'key':str(key), 'allowed_networks':allowed}
        path = data / 'listen.json'
        path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding='utf-8')
        print('配置完成。启动中心后用户打开 ' + public_url + '；管理员在页面点击管理员登录。')
        print('请确认 Windows 防火墙仅向需要使用的内网开放此端口。配置器不会自动修改防火墙或信任库。')
    finally:
        store.db.close()


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        raise SystemExit('\n配置已取消，尚未完成的步骤不会继续执行。')
    except (ValueError, OSError) as exc:
        raise SystemExit('配置未完成：' + str(exc))
