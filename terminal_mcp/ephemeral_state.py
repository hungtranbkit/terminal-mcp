"""One temp root per process for stores that have no persistent home.

THE LEAK THIS REPLACES

`register_dashboard` (and `build_default_controller`) fall back to a private
temp directory whenever a caller does not pass a store, so a test or an ad-hoc
caller can never write into the real `~/.local/state/terminal-mcp/*.db`. That
discipline is right and is kept. What was missing is the other half: nothing
ever removed those directories.

Each fallback called `tempfile.mkdtemp()` separately, so one
`register_dashboard()` without stores left six or seven directories directly
in `/tmp`, forever. Measured on m910 on 2026-09-14: **153,073** such
directories, 0.01 GB of content but one inode each, 34% of the tmpfs inode
table. `/tmp` there is a tmpfs with `usrquota`, and exhausting the per-uid
quota takes down the shell for every process of that uid on the machine --
which is what it did, repeatedly, for several sessions at once.

WHY PROCESS-SCOPED AND NOT A CONTEXT MANAGER

A `TemporaryDirectory` context manager is the better tool when the directory's
life fits a block. It does not fit here: `register_dashboard` returns while the
SQLite connections it created stay open for the lifetime of the app. The
directory has to outlive the function that made it, so the honest lifetime is
the process, and the honest cleanup hook is `atexit`.

The bound this gives: **one** `/tmp` entry per process rather than six or seven
per call. Everything else is nested inside it, so a long-lived process still
accumulates subdirectories, but they are removed together at exit and they
never touch the `/tmp` top level where the inode pressure was.

WHAT atexit DOES NOT COVER

`atexit` runs on a normal exit and on an unhandled exception. It does NOT run
on `SIGKILL`, on `os._exit`, or on a hard crash. A process killed that way
leaves exactly one directory behind, which is the point: one is recoverable,
six per call is what filled the table. `cleanup_ephemeral_state()` is exposed
so a caller that knows it is finished can clean up without waiting for exit.
"""
from __future__ import annotations

import atexit
import shutil
import tempfile
import threading
from pathlib import Path

_PREFIX = "terminal-mcp-ephemeral-"
_lock = threading.Lock()
_root: Path | None = None
_atexit_registered = False


def ephemeral_state_dir(purpose: str) -> Path:
    """A private, writable directory for one ephemeral store.

    `purpose` names the store it is for ("queue", "planner", ...) and becomes
    the subdirectory prefix, so the contents of the root stay diagnosable
    rather than being a wall of random names.
    """
    root = _ensure_root()
    return Path(tempfile.mkdtemp(prefix=f"{purpose}-", dir=root))


def ephemeral_db_path(purpose: str, filename: str) -> Path:
    """`ephemeral_state_dir` plus a filename, for the common one-db case."""
    return ephemeral_state_dir(purpose) / filename


def _ensure_root() -> Path:
    global _root, _atexit_registered
    with _lock:
        # Re-create if something removed it underneath us (a test calling
        # cleanup, an external tmp reaper). Checking is cheaper than the class
        # of bug where every later call fails because the root went away.
        if _root is None or not _root.exists():
            _root = Path(tempfile.mkdtemp(prefix=_PREFIX))
        if not _atexit_registered:
            atexit.register(cleanup_ephemeral_state)
            _atexit_registered = True
        return _root


def cleanup_ephemeral_state() -> None:
    """Remove this process's ephemeral root, if any.

    Never raises: it runs from `atexit`, where an exception would be reported
    on the way out of an otherwise successful process, and it runs in tests,
    where a failure to clean must not mask the assertion that actually
    matters. `ignore_errors` covers the real cases -- a file still open on
    Windows, a directory already reaped.
    """
    global _root
    with _lock:
        root, _root = _root, None
    if root is not None:
        shutil.rmtree(root, ignore_errors=True)


def current_root() -> Path | None:
    """The active root, or None if nothing ephemeral has been asked for yet.
    Exposed for tests and diagnostics; callers should not write here directly.
    """
    with _lock:
        return _root
