"""Serialize local session admission across HTTP/sidecar transports."""
from contextlib import contextmanager
from functools import wraps
import os
from pathlib import Path
import threading

_LOCK = threading.RLock()


@contextmanager
def admission_lock():
    with _LOCK:
        if os.name != 'posix':
            yield
            return
        import fcntl
        root = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'terminal-mcp'
        root.mkdir(parents=True, exist_ok=True)
        with (root / 'session-admission.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def serialized_admission(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        with admission_lock():
            return function(*args, **kwargs)
    return guarded
