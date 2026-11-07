"""Cross-process cache publication lock; tokenizer files are never locked."""
from contextlib import contextmanager
import errno
import json
import os
import re
from pathlib import Path
import socket
import secrets
import stat
import threading
import time

_local_guard = threading.Lock()
_local_locks = {}


class CacheBusyError(RuntimeError):
    status = 409


def _busy(folder, info=None):
    info = info or {}
    return CacheBusyError(f"数据集缓存目录正在被其他生成任务使用：{folder}；占用机器 {info.get('host', '?')}，服务 {info.get('service', '?')}，输入 {info.get('input_tokens', '?')} tokens、{info.get('count', '?')} 条；请稍后重试")


def _read_owner(gate):
    try:
        value = json.loads(gate.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _release_gate(folder, owner):
    if not re.fullmatch(r'[0-9a-f]{32}', str(owner.get('nonce', ''))):
        return
    gate = folder / '.dataset-write.owner'
    # Only the owning process or its parent with exit proof calls this. Other
    # contenders never delete a foreign/stale gate or take it over by time.
    if _read_owner(gate).get('nonce') == owner.get('nonce'):
        gate.unlink()
    own = folder / ('.dataset-owner-' + owner['nonce'] + '.json')
    own.unlink(missing_ok=True)


def cleanup_exited_child(folder, request_id, child):
    """Parent-only recovery after wait/poll proves this exact child exited."""
    if child.poll() is None:
        return False
    folder = Path(folder).resolve()
    owner = _read_owner(folder / '.dataset-write.owner')
    if owner.get('request_id') != request_id or owner.get('pid') != child.pid or owner.get('host') != socket.gethostname():
        return False
    _release_gate(folder, owner)
    return True


@contextmanager
def cache_write_lock(folder, details=None):
    folder = Path(folder).resolve()
    with _local_guard:
        local = _local_locks.setdefault(str(folder), threading.Lock())
    if not local.acquire(blocking=False):
        raise CacheBusyError(f'数据集缓存目录正在被当前进程的其他生成任务使用：{folder}；请稍后重试')
    try:
        with _file_cache_lock(folder, details):
            yield
    finally:
        local.release()


@contextmanager
def _file_cache_lock(folder, details=None):
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / '.dataset-write.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    acquired = False
    owner = None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('缓存锁必须是普通文件')
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b' ')
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                # POSIX record locks are sent to an NFS lock server even on
                # mounts where flock is explicitly local to each client.
                fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0, os.SEEK_SET)
            acquired = True
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise RuntimeError('无法建立缓存文件锁，已停止写入：' + str(exc)) from exc
            owner_desc = ''
            try:
                os.lseek(fd, 1, os.SEEK_SET)
                info = json.loads(os.read(fd, 8192).decode('utf-8'))
                owner_desc = f"；占用机器 {info.get('host', '?')}，服务 {info.get('service', '?')}，输入 {info.get('input_tokens', '?')} tokens、{info.get('count', '?')} 条"
            except (OSError, ValueError, UnicodeError):
                pass
            raise CacheBusyError(f'数据集缓存目录正在被其他生成任务使用：{folder}{owner_desc}；请稍后重试') from exc
        info = {**(details or {}), 'host': socket.gethostname(), 'pid': os.getpid(), 'started_at': time.time(), 'nonce': secrets.token_hex(16)}
        # NFS mounts may use nolock/local_lock=all, making both flock and
        # lockf local to a client. Atomic hard-link creation supplies the
        # cross-client exclusion. Publish complete immutable ownership data.
        own = folder / ('.dataset-owner-' + info['nonce'] + '.json')
        with own.open('x', encoding='utf-8') as stream:
            json.dump(info, stream, ensure_ascii=False)
            stream.flush();os.fsync(stream.fileno())
        gate = folder / '.dataset-write.owner'
        try:
            os.link(own, gate)
            owner = info
        except OSError:
            # A server may commit a LINK and lose its response. Confirm the
            # nonce instead of assuming failure and leaving an unknown owner.
            existing = _read_owner(gate)
            if existing.get('nonce') == info['nonce']:
                owner = info
            else:
                own.unlink(missing_ok=True)
                if gate.exists():
                    raise _busy(folder, existing)
                raise RuntimeError('共享缓存无法建立原子写入占位，已停止写入')
        os.lseek(fd, 1, os.SEEK_SET)
        os.ftruncate(fd, 1)
        os.write(fd, json.dumps(info, ensure_ascii=False).encode('utf-8'))
        os.fsync(fd)
        yield
    finally:
        try:
            if owner:
                _release_gate(folder, owner)
        finally:
            try:
                if acquired:
                    if os.name == 'nt':
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.lockf(fd, fcntl.LOCK_UN, 1, 0, os.SEEK_SET)
            finally:
                # Never unlink the OS lock: a waiter may hold an FD to it.
                os.close(fd)
