"""Local operator commands; no password on command line or in shell history."""
import argparse
import getpass
from pathlib import Path
from .app import Store
from .accounts import Accounts
from .certificates import issue


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['create', 'reset', 'disable', 'enable', 'certificate'])
    parser.add_argument('name', help='管理员账号；certificate 时填写中心域名或 IP')
    parser.add_argument('--data', type=Path, default=Path(__file__).resolve().parent.parent / '.fleet-data')
    args = parser.parse_args()
    if args.action == 'certificate':
        directory = args.data / 'tls'
        material = issue(args.name, directory)
        for name in ('cert', 'key'):
            p = directory / ('server-' + name + '.pem')
            p.write_text(material[name], encoding='utf-8')
            p.chmod(0o600)
        print('证书已生成。仅分发 tls/ca.pem 供用户信任，勿分发私钥。')
        return
    store = Store(args.data, recover=False)
    try:
        accounts = Accounts(store)
        if args.action in ('create', 'reset'):
            password = getpass.getpass('管理员密码（至少12字符）: ')
            if password != getpass.getpass('再次输入密码: '):
                raise ValueError('两次密码不一致')
            (accounts.create if args.action == 'create' else accounts.password)(args.name, password)
        else:
            (accounts.disable if args.action == 'disable' else accounts.enable)(args.name)
        print('操作完成')
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
