"""Process-scoped AISBench adapters: warmup length and large dataset storage."""
import copy
import importlib.machinery
import importlib.abc
import mmap
import os
from pathlib import Path
import pickle
import re
import secrets
import shutil
import sys
import types


def install_shared_memory_storage():
    """Also cover AISBench tasks executed as a script under __main__.

    Their class definitions bypass the import hook. The process-local stdlib
    factory is shared by both launch paths and spawned readers; only large
    allocations use private disk-backed storage. Message/semaphore storage and
    every process outside this job retain their native implementation.
    """
    from multiprocessing import shared_memory
    original = shared_memory.SharedMemory
    if getattr(original, '_bench_dataset_storage', False):
        return

    def memory(name=None, create=False, size=0):
        if str(name).startswith('fleetfile:'):
            return DatasetMemory(name=name)
        available = shutil.disk_usage('/dev/shm').free if os.path.isdir('/dev/shm') else 0
        if create and size > max(0, available // 2):
            print(f'[Runner] Dataset memory uses a private mmap file: {size} bytes', flush=True)
            return DatasetMemory(create=True, size=size)
        return original(name=name, create=create, size=size)

    memory._bench_dataset_storage = True
    shared_memory.SharedMemory = memory


class DatasetMemory:
    def __init__(self, name=None, create=False, size=0):
        job = os.environ.get('ASCEND_BENCH_JOB_ID', '')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', job):
            raise ValueError('Large dataset storage requires a valid job identity')
        root = Path(os.environ['ASCEND_BENCH_HOME']).resolve()
        folder = root / 'tmp' / 'aisbench-memory' / job
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if folder.resolve() != folder:
            raise ValueError('Dataset memory directory contains symbolic links')
        if create:
            name = 'fleetfile:' + job + ':' + secrets.token_hex(16)
        parts = str(name).split(':')
        if len(parts) != 3 or parts[:2] != ['fleetfile', job] or not re.fullmatch(r'[a-f0-9]{32}', parts[2]):
            raise ValueError('Dataset memory identity mismatch')
        self.name, self.path = name, folder / parts[2]
        flags = os.O_RDWR | (os.O_CREAT | os.O_EXCL if create else 0) | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            if create:
                if shutil.disk_usage(folder).free < size + 100 * 1024**2:
                    raise ValueError('Insufficient disk space for dataset memory')
                os.ftruncate(fd, size)
            self.size = os.fstat(fd).st_size
            self.mapping = mmap.mmap(fd, self.size)
            self.buf = memoryview(self.mapping)
        except BaseException:
            if create:
                self.path.unlink(missing_ok=True)
            raise
        finally:
            os.close(fd)

    def close(self):
        self.buf.release()
        self.mapping.close()

    def unlink(self):
        self.path.unlink(missing_ok=True)


def patch_dataset_storage(module):
    original = module.shared_memory.SharedMemory

    def memory(name=None, create=False, size=0):
        if str(name).startswith('fleetfile:'):
            return DatasetMemory(name=name)
        available = shutil.disk_usage('/dev/shm').free if os.path.isdir('/dev/shm') else 0
        if create and size > available // 2:
            print(f'[Runner] Dataset memory uses a private mmap file: {size} bytes', flush=True)
            return DatasetMemory(create=True, size=size)
        return original(name=name, create=create, size=size)

    module.shared_memory = types.SimpleNamespace(SharedMemory=memory)

    def dump(self, data_list, global_indexes):
        # Two passes avoid retaining a second complete pickled dataset in RAM.
        lengths = [len(pickle.dumps(data)) for data in data_list]
        size = sum(lengths)
        module.check_virtual_memory_usage(dataset_bytes=size, threshold_percent=80)
        storage = memory(create=True, size=size)
        try:
            indexes, offset = {}, 0
            for index, (data, length) in enumerate(zip(data_list, lengths)):
                storage.buf[offset:offset + length] = pickle.dumps(data)
                indexes[index] = (index, offset, length)
                offset += length
            padding = {i: indexes.get(k) for i, k in enumerate(global_indexes)}
            if not self.pressure:
                padding[len(global_indexes)] = None
            return len(data_list), storage, padding
        except BaseException:
            storage.close()
            storage.unlink()
            raise

    module.OpenICLApiInferTask._dump_dataset_to_share_memory = dump


def patch_warmup(module):
    original_memory = module.shared_memory.SharedMemory

    def open_memory(name=None, create=False, size=0):
        if str(name).startswith('fleetfile:'):
            return DatasetMemory(name=name)
        return original_memory(name=name, create=create, size=size)

    module.shared_memory = types.SimpleNamespace(SharedMemory=open_memory)
    cls = module.BaseApiInferencer
    original = cls.warmup

    async def warmup(self, data_list, warmup_times=1, concurrency=1):
        maximum = os.environ.get('AIS_BENCH_WARMUP_MAX_OUT_LEN')
        if maximum:
            maximum = int(maximum)
            data_list = copy.deepcopy(data_list[:min(len(data_list), warmup_times)])
            for data in data_list:
                data['max_out_len'] = maximum
            print(f'[Runner] Warmup max_out_len={maximum}; formal requests keep their configured output length', flush=True)
        result = await original(self, data_list, warmup_times, concurrency)
        print(f"Warmup finished Total Count: {warmup_times} Success Count: {result['success']} Failed Count: {result['failed']}", flush=True)
        return result

    cls.warmup = warmup


HOOKS = {
    'aiohttp.client_proto': lambda module: patch_http_disconnect(module),
    'ais_bench.benchmark.openicl.icl_inferencer.icl_base_api_inferencer': patch_warmup,
    'ais_bench.benchmark.tasks.openicl_api_infer': patch_dataset_storage,
}


def patch_http_disconnect(module):
    """Deliver protocol failures to an outstanding response body reader.

    aiohttp can close its transport after a chunk-parser error while leaving
    the already awaited payload Future pending. Wake that reader with the
    original exception so AISBench's own generate/retry loop handles it.
    No deadline, retry policy, request body or successful response is changed.
    This hook is installed only in the AISBench job subprocess.
    """
    cls = module.ResponseHandler
    if getattr(cls, '_aisbench_payload_errors', False):
        return
    original_error, original_lost = cls.set_exception, cls.connection_lost

    def notify(payload, error, cause=None):
        if payload is not None and not payload.is_eof() and payload.exception() is None:
            if cause is None:
                payload.set_exception(error)
            else:
                payload.set_exception(error, cause)

    def set_exception(self, error, *args, **kwargs):
        payload = self._payload
        try:
            return original_error(self, error, *args, **kwargs)
        finally:
            notify(payload, error, args[0] if args else kwargs.get('exc_cause'))

    def connection_lost(self, error):
        payload = self._payload
        try:
            return original_lost(self, error)
        finally:
            # Completed HTTP bodies already have EOF. Only a reader stranded
            # by a terminated connection needs an explicit failure here.
            if payload is not None and not payload.is_eof() and payload.exception() is None:
                if error is None:
                    from aiohttp.client_exceptions import ServerDisconnectedError
                    error = ServerDisconnectedError('Connection closed before response body completed')
                notify(payload, error)

    cls.set_exception, cls.connection_lost = set_exception, connection_lost
    cls._aisbench_payload_errors = True


class Loader:
    def __init__(self, loader, callback):
        self.loader, self.callback = loader, callback

    def create_module(self, spec):
        return self.loader.create_module(spec) if hasattr(self.loader, 'create_module') else None

    def exec_module(self, module):
        self.loader.exec_module(module)
        self.callback(module)

    def __getattr__(self, name):
        return getattr(self.loader, name)


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        callback = HOOKS.get(fullname)
        if callback:
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and spec.loader:
                spec.loader = Loader(spec.loader, callback)
                return spec


def install():
    install_shared_memory_storage()
    for name, callback in HOOKS.items():
        if name in sys.modules:
            callback(sys.modules[name])
    sys.meta_path.insert(0, Finder())
