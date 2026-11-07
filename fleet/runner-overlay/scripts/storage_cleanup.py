"""Manual, signed cleanup plans scoped to expired cache and terminal job trees."""
from contextlib import nullcontext
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time

TERMINAL = {'completed', 'failed', 'stopped'}


def retention_days(value):
    if type(value) is not int or not 1 <= value <= 3650:
        raise ValueError('保留天数请输入1–3650之间的整数')
    return value


def _stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def _tree(path, cache=False):
    """Metadata only. Reject symlinks, devices and nested mounts, fail closed."""
    start = path.lstat()
    mounts=set()
    mountinfo=Path('/proc/self/mountinfo')
    if mountinfo.exists():
        for line in mountinfo.read_text().splitlines():
            mounts.add(Path(re.sub(r'\\([0-7]{3})',lambda m:chr(int(m[1],8)),line.split()[4])))
    if not stat.S_ISDIR(start.st_mode) or path.resolve() != path or os.path.ismount(path) or path in mounts:
        raise ValueError('目录包含链接或挂载点，已跳过')
    size = 0
    latest = 0 if cache else start.st_mtime
    digest = hashlib.sha256()
    stack = [path]
    while stack:
        for entry in sorted(stack.pop().iterdir()):
            if cache and (entry.name in ('.dataset-write.lock','.dataset-write.owner') or entry.name.startswith('.dataset-owner-')):
                continue
            s = entry.lstat()
            if s.st_dev != start.st_dev or entry.is_symlink() or os.path.ismount(entry) or entry in mounts:
                raise ValueError('目录包含链接或挂载点，已跳过')
            latest = max(latest, s.st_mtime)
            digest.update(json.dumps([str(entry.relative_to(path)),s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns]).encode())
            if stat.S_ISDIR(s.st_mode):
                stack.append(entry)
            elif stat.S_ISREG(s.st_mode):
                size += getattr(s, 'st_blocks', (s.st_size+511)//512) * 512
            else:
                raise ValueError('目录含特殊文件，已跳过')
    return size, latest, digest.hexdigest()


def _json(path):
    if path.stat().st_size > 4_000_000:
        raise ValueError('任务记录过大，已跳过')
    return json.loads(path.read_text(encoding='utf-8'))


def _candidate(base, kind, name, days, now, owned=False):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,220}', name):
        raise ValueError('目录名不符合清理范围')
    relative = ('results/jobs/' if kind == 'job' else 'datasets/perf/') + name
    path = base / relative
    # Parent links may otherwise redirect the entire allowed subtree.
    if path.resolve() != path:
        raise ValueError('路径包含符号链接，已跳过')
    size, latest, fingerprint = _tree(path,kind=='cache')
    if kind == 'job':
        if not re.fullmatch(r'real-[a-z0-9-]+', name):
            raise ValueError('非正式任务目录，已跳过')
        record = _json(path/'record.json')
        if record.get('id') != name or record.get('status') not in TERMINAL:
            raise ValueError('任务尚未确认结束，已跳过')
        when = _stamp(record['completed_at'])
        # A new reuse run or edits to results protect the source as well.
        when = max(when, latest)
    else:
        if not (path/'data.jsonl').is_file():
            raise ValueError('无完整性能缓存，已跳过')
        if ((path/'.dataset-write.owner').exists() and not owned) or list(path.glob('.dataset-reader-*')):
            raise ValueError('缓存正在使用或占用尚未确认释放，已跳过')
        when = latest
    if when > now-days*86400:
        return None
    s = path.stat()
    return {'kind':kind, 'name':name, 'relative':relative, 'path':str(path), 'bytes':size,
            'time':when, 'device':s.st_dev, 'inode':s.st_ino, 'mtime_ns':s.st_mtime_ns,'fingerprint':fingerprint}


def _sign(row, token):
    return hmac.new(token.encode(), json.dumps(row, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def scan(base, days, token, now=None):
    base = Path(base).resolve();days=retention_days(days);now=now or time.time()
    items=[];skipped=[]
    for kind, relative in [('cache','datasets/perf'),('job','results/jobs')]:
        root=base/relative
        if not root.exists():continue
        if root.resolve()!=root:raise ValueError('清理目录不能是符号链接')
        for path in sorted(root.iterdir()):
            try:
                if not path.is_dir() or path.is_symlink():continue
                if kind=='cache' and {p.name for p in path.iterdir()} <= {'.dataset-write.lock'}:
                    continue  # Already cleared; only a permanent lock remains.
                row=_candidate(base,kind,path.name,days,now)
                if row:
                    row.update(days=days, scanned_at=now)
                    row['signature']=_sign(row,token);items.append(row)
            except (OSError,ValueError,KeyError,TypeError) as exc:
                skipped.append({'path':str(path),'reason':str(exc)})
    return {'days':days,'items':items,'skipped':skipped,'bytes':sum(r['bytes'] for r in items)}


def delete(base, items, token, now=None):
    base=Path(base).resolve();now=now or time.time();results=[]
    if not isinstance(items,list) or not 1<=len(items)<=1000:
        raise ValueError('请选择1–1000项清理目录')
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError('当前平台不支持安全目录清理')
    from dataset_lock import cache_write_lock
    for original in items:
        row=dict(original);signature=row.pop('signature','')
        result={'path':row.get('path',''),'name':row.get('name',''),'bytes':0}
        try:
            if not hmac.compare_digest(str(signature),_sign(row,token)):
                raise ValueError('清理清单无效，请重新扫描')
            if not 0<=now-row['scanned_at']<=3600:
                raise ValueError('扫描清单超过1小时，请重新扫描')
            days=retention_days(row['days'])
            if row['kind'] not in ('job','cache'):raise ValueError('清理类型无效')
            fresh=_candidate(base,row['kind'],row['name'],days,now)
            if not fresh or any(fresh[k]!=row[k] for k in ('relative','path','device','inode','mtime_ns','bytes','time')):
                raise ValueError('文件已变化或近期被使用，请重新扫描')
            path=base/row['relative']
            context=cache_write_lock(path,{'service':'administrator cleanup'}) if row['kind']=='cache' else nullcontext()
            with context:
                if row['kind']=='cache':
                    checked=_candidate(base,'cache',row['name'],days,now,owned=True)
                    if not checked or any(checked[k]!=row[k] for k in ('device','inode','fingerprint','bytes','time')):
                        raise ValueError('缓存已变化或近期被使用，已跳过')
                    # Keep the permanent lock inode and ownership links until
                    # release. Removing the directory would split NFS locks.
                    if list(path.glob('.dataset-reader-*')):raise ValueError('缓存正在被测试使用')
                    for child in path.iterdir():
                        if child.name in ('.dataset-write.lock','.dataset-write.owner') or child.name.startswith('.dataset-owner-'):continue
                        if child.is_symlink():raise ValueError('文件已变化，已停止清理')
                        if child.is_dir():shutil.rmtree(child)
                        else:child.unlink()
                else:
                    shutil.rmtree(path)
            result.update(status='deleted',bytes=row['bytes'])
        except Exception as exc:
            result.update(status='skipped',reason=str(exc))
        results.append(result)
    return {'results':results,'deleted_bytes':sum(r['bytes'] for r in results if r['status']=='deleted')}
