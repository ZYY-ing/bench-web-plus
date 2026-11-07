"""Keep old Docker's clone3 denial compatible with glibc's clone fallback.

This stacks a process-local seccomp filter. Existing container restrictions stay
in force; clone3 remains denied, with ENOSYS instead of EPERM. No daemon changes,
privileged container, or unconfined policy are required. Descendants inherit it.
"""
import ctypes
import errno
import os
import platform
import sys
import threading


def ensure_threads():
    try:
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join()
        return False
    except RuntimeError as original:
        if sys.platform != 'linux' or platform.machine() not in ('aarch64', 'x86_64'):
            raise RuntimeError('容器无法创建线程，请检查进程限制和运行时兼容性') from original
        libc = ctypes.CDLL(None, use_errno=True)
        # Both supported Linux ABIs define __NR_clone3 as 435.
        if libc.syscall(435, ctypes.c_void_p(), 0) != -1 or ctypes.get_errno() != errno.EPERM:
            raise RuntimeError('容器无法创建线程，请检查进程限制和运行时兼容性') from original

        class Filter(ctypes.Structure):
            _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                        ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint)]

        class Program(ctypes.Structure):
            _fields_ = [('len', ctypes.c_ushort), ('filter', ctypes.POINTER(Filter))]

        # seccomp_data.nr; if clone3, return ENOSYS; otherwise defer to the
        # container's existing filters. No syscall is newly allowed.
        rules = (Filter * 4)(Filter(0x20, 0, 0, 0), Filter(0x15, 0, 1, 435),
                             Filter(0x06, 0, 0, 0x00050000 | errno.ENOSYS),
                             Filter(0x06, 0, 0, 0x7fff0000))
        program = Program(4, rules)
        if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.prctl(22, 2, ctypes.byref(program), 0, 0) != 0:
            raise RuntimeError('无法启用受限线程兼容模式，请使用兼容的容器镜像') from original
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join()
        return True


if __name__ == '__main__':
    applied = ensure_threads()
    if applied:
        print('已启用进程级线程兼容模式（保留容器安全限制）', file=sys.stderr, flush=True)
    if len(sys.argv) > 1:
        os.execv(sys.argv[1], sys.argv[1:])
