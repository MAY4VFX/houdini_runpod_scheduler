"""Compose cook and infrastructure state without overwriting either channel."""
import datetime
import threading

_lock = threading.Lock()
_nodes = {}


def update(key, **parts):
    with _lock:
        state = _nodes.setdefault(key, {})
        state.update(parts)
        stamp = datetime.datetime.now().strftime('%H:%M:%S')
        return 'Updated {}\n{}'.format(stamp, '\n\n'.join(
            state[p] for p in ('cook', 'job', 'farm') if state.get(p)))
