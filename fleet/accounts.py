"""Small local administrator database; passwords are salted scrypt hashes."""
import hashlib
import re
import secrets
import time


class Accounts:
    def __init__(self, store):
        self.store = store

    def list(self):
        return [{'id': a['name'], 'disabled': a['disabled'], 'created_at': a['created_at']} for a in self.store.list('administrator')]

    def create(self, name, password):
        if name == 'bootstrap':
            raise ValueError('该账号名为系统保留，请使用其他名称')
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,39}', name):
            raise ValueError('管理员账号需为1–40位字母、数字、点、横线或下划线')
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ValueError('管理员密码需要12–256个字符')
        salt = secrets.token_hex(16)
        hashed = self.hash(password, salt)
        with self.store.lock:
            if any(a['id'] == name for a in self.list()):
                raise ValueError('管理员账号已存在')
            self.store.save('administrator', {'id': 'administrator:' + name, 'name': name, 'disabled': False, 'created_at': time.time(),
                                             'salt': salt, 'hash': hashed, 'revision': 1})
        return {'ok': True}

    @staticmethod
    def hash(password, salt):
        return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()

    def authenticate(self, name, password):
        if not isinstance(name, str) or not isinstance(password, str) or len(password) > 256:
            return False
        account = next((a for a in self.store.list('administrator') if a['name'] == name), None)
        salt = account['salt'] if account else '00' * 16
        actual = self.hash(password, salt)
        return bool(account and not account['disabled'] and secrets.compare_digest(actual, account['hash']))

    def disable(self, name):
        with self.store.lock:
            account = self.store.get('administrator', 'administrator:' + name)
            if not account['disabled'] and sum(not a['disabled'] for a in self.list()) <= 1:
                raise ValueError('至少保留一个可用管理员')
            account['disabled'] = True
            account['revision'] = account.get('revision', 0) + 1
            self.store.save('administrator', account)

    def enable(self, name):
        with self.store.lock:
            account = self.store.get('administrator', 'administrator:' + name)
            account.update(disabled=False, revision=account.get('revision', 0)+1)
            self.store.save('administrator', account)

    def password(self, name, password):
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ValueError('管理员密码需要12–256个字符')
        salt = secrets.token_hex(16)
        hashed = self.hash(password, salt)
        with self.store.lock:
            account = self.store.get('administrator', 'administrator:' + name)
            account.update(salt=salt, hash=hashed, revision=account.get('revision', 0)+1)
            self.store.save('administrator', account)

    def revision(self, name):
        return self.store.get('administrator', 'administrator:' + name).get('revision', 0)

    def enabled(self, name):
        return any(a['id'] == name and not a['disabled'] for a in self.list())
