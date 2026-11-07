"""Capture the real AISBench curses terminal, without changing its renderer.

The original process/session/group remains intact. A reader thread records the
PTY bytes and decodes their screen using pyte, rather than making progress bars.
"""
import atexit
import codecs
import os
from pathlib import Path
import re
import select
import signal
import sys
import threading
import time


def terminal_screen(emit, reply, columns=320, lines=50):
    vendor = str(Path(__file__).parent / 'terminal_vendor')
    if vendor not in sys.path:
        sys.path.append(vendor)
    import pyte

    class Screen(pyte.Screen):
        def __init__(self):
            self.alternate = False
            self.last_frame = None
            self.last_snapshot = 0
            self.drawing = False
            super().__init__(columns, lines)

        def draw(self, data):
            if not self.alternate:
                emit(data)
            self.drawing = True
            try:
                super().draw(data)
            finally:
                self.drawing = False

        def linefeed(self):
            super().linefeed()
            if not self.alternate and not self.drawing:
                emit('\n')

        def carriage_return(self):
            super().carriage_return()
            if not self.alternate and not self.drawing:
                emit('\r')

        def set_mode(self, *modes, **kwargs):
            if kwargs.get('private') and any(mode in (47, 1047, 1049) for mode in modes):
                self.alternate = True
            super().set_mode(*modes, **kwargs)

        def reset_mode(self, *modes, **kwargs):
            if kwargs.get('private') and any(mode in (47, 1047, 1049) for mode in modes):
                self.snapshot(force=True)
                self.alternate = False
            super().reset_mode(*modes, **kwargs)

        def write_process_input(self, data):
            # Answer terminal protocol queries, not application input/keys.
            reply(data)

        def snapshot(self, force=False):
            now = time.monotonic()
            if not self.alternate or not force and now-self.last_snapshot < 1:
                return
            frame = '\n'.join(line.rstrip() for line in self.display).rstrip()
            # Initial curses capability checks enter/leave an empty screen.
            if frame and frame != self.last_frame and 'Task Progress Table' in frame:
                emit('\n[Runner terminal frame begin]\n' + frame + '\n[Runner terminal frame end]\n')
                self.last_frame = frame
                self.last_snapshot = now

    screen = Screen()
    return screen, pyte.Stream(screen)


def enable(folder):
    import fcntl
    import pty
    import struct
    import termios

    if os.isatty(0) and os.isatty(1):
        return False  # An actual interactive CLI already has its native board.
    if os.getsid(0) != os.getpid():
        raise RuntimeError('native terminal capture requires the existing job session leader')
    folder = Path(folder)
    master, slave = pty.openpty()
    original = {fd: os.dup(fd) for fd in (0, 1, 2)}
    stopped = threading.Event()
    raw = None
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 50, 320, 0, 0))
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        raw = (folder / 'native-terminal.ansi').open('ab', buffering=0)
        def emit(text):
            if text:
                os.write(original[1], text.encode('utf-8'))
        def reply(text):
            try:
                os.write(master, text.encode('utf-8'))
            except OSError:
                pass
        screen, stream = terminal_screen(emit, reply)
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        def read():
            try:
                while True:
                    ready, _, _ = select.select([master], [], [], .1)
                    if ready:
                        try:
                            chunk = os.read(master, 65536)
                        except OSError:
                            break
                        if not chunk:
                            break
                        raw.write(chunk)
                        stream.feed(decoder.decode(chunk))
                    elif stopped.is_set():
                        break
                    screen.snapshot()
            except (OSError, ValueError):
                pass
            finally:
                try:
                    screen.snapshot(force=True)
                except OSError:
                    pass
        os.environ['TERM'] = 'xterm-256color'
        for fd in (0, 1, 2):
            os.dup2(slave, fd)
        os.close(slave)
        slave = -1
        sys.stdout.reconfigure(line_buffering=True, write_through=True)
        sys.stderr.reconfigure(line_buffering=True, write_through=True)
        emit('[Runner terminal recording: ' + str(folder/'native-terminal.ansi') + ']\n')
        thread = threading.Thread(target=read, name='aisbench-native-terminal', daemon=True)
        thread.start()
        def finish():
            sys.stdout.flush()
            sys.stderr.flush()
            # Closing our controlling PTY must not turn a successful CLI exit
            # into SIGHUP. This runs only during process exit.
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            for fd in (0, 1, 2):
                os.dup2(original[fd], fd)
            stopped.set()
            thread.join(timeout=3)
            raw.close()
            os.close(master)
            for fd in original.values():
                os.close(fd)
        atexit.register(finish)
        return True
    except Exception:
        previous_hup = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        for fd in (0, 1, 2):
            os.dup2(original[fd], fd)
        for fd in original.values():
            os.close(fd)
        if slave >= 0:
            os.close(slave)
        os.close(master)
        if raw:
            raw.close()
        signal.signal(signal.SIGHUP, previous_hup)
        raise


def start_for_cli():
    base = os.environ.get('ASCEND_BENCH_HOME')
    job = os.environ.get('ASCEND_BENCH_JOB_ID', '')
    if not base or not re.fullmatch(r'real-[a-z0-9-]+', job):
        return
    folder = Path(base) / 'results/jobs' / job
    if str(folder/'config.py') not in sys.argv or '--mode' not in sys.argv:
        return
    enable(folder)
