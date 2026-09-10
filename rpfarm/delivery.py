"""One local delivery path shared by Download TOP and legacy auto-download.

Receipts identify a render item, remote file and validated local stat. Locks
prevent two processes from transferring the same output concurrently.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

from . import config, pods, sync


def root():
    from .context import _snapshot_root
    path = _snapshot_root() / 'delivery'
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _key(item, cfg, entry):
    raw = (getattr(cfg, 'volume_id', ''), item.get('delivery_key', ''), entry[0], entry[1])
    return hashlib.sha256(json.dumps(raw).encode()).hexdigest()


def valid(item, cfg, entry):
    if not item.get('delivery_key'):
        return False  # Custom paths have no immutable render identity.
    try:
        receipt = json.loads((root() / (_key(item, cfg, entry) + '.json')).read_text())
        stat = os.stat(entry[0])
        return receipt == {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
    except (OSError, ValueError):
        return False


def transfer(item, cfg, perform, cancel=lambda: False):
    entries = item['files']
    lock_paths = sorted({hashlib.sha256(os.path.abspath(e[0]).encode()).hexdigest() for e in entries})
    def pause(seconds):
        if cancel():
            raise InterruptedError('Download cancelled')
        import time
        time.sleep(min(seconds, 0.1))
    with contextlib.ExitStack() as stack:
        for key in lock_paths:
            stack.enter_context(pods._file_lock(root() / (key + '.lock'), timeout=3600, sleep=pause))
        if cancel():
            raise InterruptedError('Download cancelled')
        pending = [e for e in entries if not valid(item, cfg, e)]
        stats = perform(dict(item, files=pending, bytes=sum(e[2] for e in pending))) if pending else {
            'files': 0, 'bytes': 0, 'seconds': 0.0}
        missing = [e[0] for e in entries if not os.path.isfile(e[0])]
        if missing:
            raise sync.SyncError('Download did not produce local output(s): ' + ', '.join(missing[:5]))
        if cancel():
            raise InterruptedError('Download cancelled')
        verified = stats.pop('verified', {})
        for entry in pending:
            stamp = verified.get(entry[0])
            if stamp is None:
                continue
            path = root() / (_key(item, cfg, entry) + '.json')
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps(stamp))
            os.replace(tmp, path)
        return dict(stats, delivered=sum(valid(item, cfg, e) for e in entries))


class Queue:
    """Non-blocking legacy scheduler adapter. Its worker owns only immutable args."""
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='rpfarm-download')
        self.cancelled = threading.Event()
        self.futures = []

    def submit(self, item, cfg, sftp, client):
        from .packages import run_download_item
        future = self.executor.submit(run_download_item, item, cfg, sftp, client, 'newer',
                                      cancel=self.cancelled.is_set)
        self.futures.append((item, future))

    def poll(self):
        ready = [(i, f) for i, f in self.futures if f.done()]
        self.futures = [(i, f) for i, f in self.futures if not f.done()]
        results = []
        for item, future in ready:
            try:
                results.append((item, future.result(), None))
            except Exception as exc:
                results.append((item, None, exc))
        return results

    def cancel(self):
        self.cancelled.set()
        for _item, future in self.futures:
            future.cancel()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def close(self):
        self.executor.shutdown(wait=False)
