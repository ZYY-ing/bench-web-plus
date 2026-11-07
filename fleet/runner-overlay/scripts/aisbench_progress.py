"""Read AISBench's original task output; never change its tasks or requests."""
import atexit
import codecs
import json
import os
from pathlib import Path
import re
import sys
import threading
import time


class Relay:
    def __init__(self, base, job_id):
        if not re.fullmatch(r'real-[a-z0-9-]+', job_id):
            raise ValueError('Invalid job id')
        self.base = Path(base).resolve()
        self.folder = self.base / 'results/jobs' / job_id
        self.offsets = {}
        self.decoders = {}
        self.announced = set()
        self.lock_file = None

    def acquire(self):
        import fcntl
        self.lock_file = (self.folder / '.progress-relay.lock').open('a')
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            self.lock_file = None
            return False
        return True

    def append(self, text):
        if text:
            with (self.folder / 'execution.log').open('ab', buffering=0) as stream:
                stream.write(text.encode('utf-8'))

    def poll(self):
        record = json.loads((self.folder / 'record.json').read_text())
        root = Path(record.get('output_root') or self.folder / 'output').resolve()
        if root == self.base or not root.is_relative_to(self.base):
            return
        previous = set(record.get('previous_output_directories', []))
        reuse = record.get('reuse_directory')
        directories = [p for p in root.glob('*') if p.is_dir() and re.fullmatch(r'\d{8}_\d{6}', p.name)
                       and (p.name not in previous or str(p) == reuse)]
        for directory in sorted(directories)[-4:]:
            for path in sorted((directory / 'logs').rglob('*.out'))[:64]:
                if not path.resolve().is_relative_to(root):
                    continue
                stat = path.stat()
                identity = (stat.st_dev, stat.st_ino)
                old = self.offsets.get(path)
                if old is None or old[0] != identity or stat.st_size < old[1]:
                    # Reused inference logs belong to the earlier task. Watch
                    # their future appends/rotation without replaying old work.
                    offset = stat.st_size if old is None and reuse else 0
                    self.decoders[path] = codecs.getincrementaldecoder('utf-8')('replace')
                else:
                    offset = old[1]
                with path.open('rb') as stream:
                    stream.seek(offset)
                    raw = stream.read(65536)
                self.offsets[path] = (identity, offset + len(raw))
                text = self.decoders[path].decode(raw)
                if text:
                    if path not in self.announced:
                        self.append('\n[Runner log source: ' + str(path.relative_to(root)) + ']\n')
                        self.announced.add(path)
                    self.append(text)

    def safe_poll(self):
        try:
            self.poll()
        except (OSError, ValueError):
            pass

    def close(self):
        self.safe_poll()
        if self.lock_file:
            self.lock_file.close()


def start_for_cli():
    """Only the top-level job CLI owns a relay, not its spawned workers/help."""
    base = os.environ.get('ASCEND_BENCH_HOME')
    job_id = os.environ.get('ASCEND_BENCH_JOB_ID', '')
    if not base or not re.fullmatch(r'real-[a-z0-9-]+', job_id):
        return
    config = str(Path(base) / 'results/jobs' / job_id / 'config.py')
    if config not in sys.argv or '--mode' not in sys.argv or '--debug' in sys.argv:
        return
    relay = Relay(base, job_id)
    if not relay.acquire():
        return
    stopped = threading.Event()
    def loop():
        while not stopped.is_set():
            relay.safe_poll()
            stopped.wait(.23)
    thread = threading.Thread(target=loop, name='aisbench-log-relay', daemon=True)
    try:
        thread.start()
    except Exception:
        relay.close()
        raise
    def finish():
        stopped.set()
        thread.join(timeout=2)
        relay.close()
    atexit.register(finish)


def follow_active(base, job_id):
    """Temporary attachment for an already-running job; reads logs only."""
    relay = Relay(base, job_id)
    if not relay.acquire():
        return
    try:
        record = json.loads((relay.folder / 'record.json').read_text())
        process = Path('/proc') / str(record['pid'])
        expected_start = (process / 'stat').read_text().rsplit(')', 1)[1].split()[19]
        while True:
            relay.safe_poll()
            record = json.loads((relay.folder / 'record.json').read_text())
            if record.get('status') not in ('running', 'preparing'):
                return
            try:
                if (process / 'stat').read_text().rsplit(')', 1)[1].split()[19] != expected_start:
                    return
            except FileNotFoundError:
                return
            time.sleep(.23)
    finally:
        relay.close()


if __name__ == '__main__':
    follow_active(sys.argv[1], sys.argv[2])
