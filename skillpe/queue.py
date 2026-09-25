"""Shared-filesystem, per-model queue with OS locks and bounded retries.

The filesystem must implement cross-host flock correctly. A worker crash releases
its lock; a stale running state alone does not block recovery. Never unlink locks.
"""
from concurrent.futures import ThreadPoolExecutor
import time
from pathlib import Path

from .io import digest, freeze, lock, read, write


class Queue:
    def __init__(self, root, model):
        self.root = Path(root) / 'queues' / model
        for name in ('jobs', 'state', 'locks', 'results'):
            (self.root/name).mkdir(parents=True, exist_ok=True)

    def publish(self, job):
        key = digest(job)[:32]
        freeze(self.root/'jobs'/f'{key}.json', job)
        return key

    def result(self, key):
        path = self.root/'results'/f'{key}.json'
        return read(path) if path.exists() else None

    def process(self, path, handler, attempts):
        key = path.stem
        with lock(self.root/'locks'/f'{key}.lock') as acquired:
            if not acquired:
                return False
            state_path = self.root/'state'/f'{key}.json'
            state = read(state_path) if state_path.exists() else {}
            if self.result(key) is not None or state.get('attempts', 0) >= attempts:
                return False
            job = read(path)
            n = state.get('attempts', 0)+1
            write(state_path, {'status': 'running', 'attempts': n, 'updated_at': time.time()})
            try:
                value = handler(job)
                write(self.root/'results'/f'{key}.json', value)
                write(state_path, {'status': 'done', 'attempts': n, 'updated_at': time.time()})
            except Exception as exc:
                write(state_path, {'status': 'failed' if n >= attempts else 'pending',
                                  'attempts': n, 'error_type': type(exc).__name__, 'updated_at': time.time()})
            return True

    def worker(self, handler, concurrency=1, attempts=3, watch=False):
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            while True:
                jobs = sorted((self.root/'jobs').glob('*.json'))
                progressed = sum(pool.map(lambda p: self.process(p, handler, attempts), jobs))
                if not watch and not progressed:
                    return
                if not progressed:
                    time.sleep(5)

    def status(self):
        from collections import Counter
        values = Counter()
        for path in (self.root/'jobs').glob('*.json'):
            p = self.root/'state'/path.name
            values[(read(p) if p.exists() else {}).get('status', 'pending')] += 1
        return dict(values)
