"""CPU-only performance dataset preparation, isolated from Runner HTTP threads."""
import json
import os
from pathlib import Path
import sys
import time

import runner


def main():
    stage = Path(sys.argv[1])
    request = json.loads((stage / 'request.json').read_text())
    runner.BASE = Path(request['base'])
    runner.CONFIG = {'service': request['service']}
    request['config']['_cache_owner'] = stage.name
    previous = [0, None]
    def progress(phase, completed, total):
        now = time.monotonic()
        if now - previous[0] >= 1 or phase != previous[1] or completed == total:
            runner.save(stage / 'progress.json', dict(phase=phase, completed=completed, total=total))
            previous[:] = [now, phase]
    try:
        progress('检查缓存', 0, request['count'])
        path, reused = runner.prepare_perf_dataset(request['config'], Path(request['folder']), request['count'], progress)
        result = {'path': str(path), 'reused': reused}
    except Exception as exc:
        result = {'error': str(exc), 'status': getattr(exc, 'status', 400)}
    runner.save(stage / 'result.json', result)


if __name__ == '__main__':
    main()
