"""Agent-side adapter: a detached session host, made to look like a local PTY.

`WindowsSessionBackend` already talks to a session's process through exactly one
narrow shape -- `PtyProcessLike` (pid / isalive / read / write / setwinsize /
terminate). That is the whole seam this module needs. `HostProcessProxy`
implements that shape against the file spool of a detached host
(windows_session_host.py), so the backend's reader thread, VT parser, history
buffer, resize path and kill path all keep working untouched while the real
ConPTY lives in a process the agent does not own.

That is why this is an adapter and not a rewrite: the alternative -- teaching
the backend about hosts, spools and offsets -- would have meant changing the
reader loop, the liveness logic and the desktop-viewer path, every one of which
is load-bearing on a live node today.

    backend  --PtyProcessLike-->  HostProcessProxy  --files-->  session host
                                                                   |
                                                                ConPTY
                                                                   |
                                                    powershell.exe / claude.exe

WHAT ADOPTION MEANS. After a node-agent restart the registry is empty but the
state directory is not. `adopt_sessions` reads it, builds a proxy per host that
is confirmed ALIVE, and hands the backend an entry with the same logical name.
The proxy starts reading its spool at offset 0, so the restarted agent replays
the history it never saw being produced -- the scrollback is restored rather
than starting blank. Hosts that are GONE or whose PID looks reused are reported,
never adopted and never deleted; deletion is a separate explicit step.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from . import windows_session_host as wsh
from .windows_session_host import (
    HOST_ALIVE, SessionMeta, SessionPaths, HostError,
)

DEFAULT_STATE_DIRNAME = "win-sessions"
SPAWN_META_TIMEOUT_SECONDS = 15.0
READ_POLL_SECONDS = 0.05
TERMINATE_GRACE_SECONDS = 3.0
TERMINATE_REAP_SECONDS = 2.0

ADOPTED = "ADOPTED"
ALREADY_PRESENT = "ALREADY_PRESENT"
ORPHAN_HOST_GONE = "ORPHAN_HOST_GONE"
ORPHAN_PID_REUSED = "ORPHAN_PID_REUSED"
ADOPT_FAILED = "ADOPT_FAILED"


class HostProcessProxy:
    """A `PtyProcessLike` whose process lives in another, detached process.

    `pid` is the CHILD's pid, not the host's: the backend feeds this pid to
    `_win32_foreground_command`, which walks the descendant tree to answer "what
    is this session actually running right now". Reporting the host's pid there
    would make every session's foreground command read as the host wrapper
    instead of claude.exe. Host liveness is reported through `isalive()`
    instead, so `_is_alive`'s child-pid-then-isalive composition still ends up
    checking both."""

    def __init__(self, paths: SessionPaths, meta: SessionMeta, *,
                 alive: Callable[[int | None], bool] | None = None,
                 poll_seconds: float = READ_POLL_SECONDS,
                 offset: int = 0) -> None:
        self.paths = paths
        self.meta = meta
        self._alive = alive
        self._poll_seconds = poll_seconds
        self._offset = offset

    # -- PtyProcessLike --------------------------------------------------

    @property
    def pid(self) -> int:
        return int(self.meta.child_pid or self.meta.host_pid or 0)

    @property
    def host_pid(self) -> int:
        return int(self.meta.host_pid or 0)

    @property
    def read_offset(self) -> int:
        return self._offset

    def isalive(self) -> bool:
        meta = wsh.read_meta(self.paths) or self.meta
        return wsh.host_state(self.paths, meta, alive=self._alive) == HOST_ALIVE

    def read(self, size: int = 64 * 1024) -> str:
        """Return whatever the host has spooled since the last read.

        Sleeps briefly on an empty spool rather than returning instantly.
        `_reader_loop` treats an empty read from a live process as "nothing to
        say yet, poll again", so a proxy that returned "" with no delay would
        spin that thread at 100% CPU -- pywinpty's own read blocks with a
        timeout, and this keeps the same shape."""
        text, new_offset = wsh.read_spool(self.paths, offset=self._offset, max_bytes=size)
        self._offset = new_offset
        if not text:
            time.sleep(self._poll_seconds)
        return text

    def write(self, data: str) -> int:
        """Queue input for the host to feed to the child.

        Liveness is checked FIRST and explicitly. The input channel is an
        append-only spool, so a write to a dead session's directory would
        otherwise succeed and be read by nobody -- the caller would be told the
        keystrokes were delivered. Checking here is also why the spool's own
        write_input does not try to infer liveness itself."""
        if not data:
            return 0
        if not self.isalive():
            raise OSError(f"session {self.paths.name!r}: host is not alive, input refused")
        if not wsh.write_input(self.paths, data):
            raise OSError(f"session {self.paths.name!r}: input spool is not writable")
        return len(data)

    def setwinsize(self, rows: int, cols: int) -> None:
        wsh.request_control(self.paths, {"op": "resize", "rows": rows, "cols": cols})

    def terminate(self, force: bool = False, *,
                  grace_seconds: float = TERMINATE_GRACE_SECONDS) -> None:
        """Stop the session for good: ask the host to shut down, signal it if it
        will not go, and only then remove its state directory.

        The ORDER matters in two ways that a simpler implementation gets wrong.
        Deleting the directory while the host is still running would delete the
        shutdown request in `ctl.json` before the host ever read it -- leaving a
        live host with no state directory, which is an orphan nothing can find
        again. And on Windows `rmtree` cannot remove a file the host still has
        open, so cleanup would simply fail and leave the state behind. The host
        goes first, the directory second.

        The directory removal lives here, on the explicit kill path, and nowhere
        else -- requirement 4 asks that orphan cleanup be a deliberate action."""
        wsh.request_control(self.paths, {"op": "shutdown"})
        meta = wsh.read_meta(self.paths) or self.meta
        deadline = time.monotonic() + (0.0 if force else max(0.0, grace_seconds))
        while time.monotonic() < deadline and wsh.pid_alive(meta.host_pid):
            time.sleep(0.05)
        if wsh.pid_alive(meta.host_pid):
            wsh.terminate_host(self.paths, meta, alive=self._alive)
            # Give the signal a moment to land, so cleanup is not racing a host
            # that is still writing to its own spool.
            reap = time.monotonic() + TERMINATE_REAP_SECONDS
            while time.monotonic() < reap and wsh.pid_alive(meta.host_pid):
                time.sleep(0.05)
        wsh.cleanup_session_dir(self.paths)


# -- creating a session as a detached host --------------------------------

def session_process_factory(state_root: str | Path, *,
                            spawn: Callable[..., int] | None = None,
                            alive: Callable[[int | None], bool] | None = None,
                            timeout: float = SPAWN_META_TIMEOUT_SECONDS,
                            poll_seconds: float = READ_POLL_SECONDS) -> Callable[..., HostProcessProxy]:
    """Build the `(name, argv, cwd) -> PtyProcessLike` the backend can spawn with.

    Waits for the host to publish its metadata before returning, so a caller
    never receives a proxy with no child pid and no way to tell whether the
    session actually came up. A host that never publishes is a failure, not a
    session in an unknown state."""
    root = Path(state_root)
    spawner = spawn or wsh.spawn_host

    def _factory(name: str, argv: list[str], cwd: str) -> HostProcessProxy:
        paths = SessionPaths(root, name)
        paths.ensure()
        host_pid = spawner(paths, list(argv), cwd)
        meta = _await_meta(paths, host_pid, timeout=timeout)
        return HostProcessProxy(paths, meta, alive=alive, poll_seconds=poll_seconds)

    return _factory


def _await_meta(paths: SessionPaths, host_pid: int, *, timeout: float) -> SessionMeta:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = wsh.read_meta(paths)
        if meta is not None and meta.host_pid:
            return meta
        time.sleep(0.02)
    raise HostError(
        f"session host for {paths.name!r} (pid {host_pid}) did not publish metadata "
        f"within {timeout:g}s -- see {paths.host_log}")


# -- adoption after an agent restart --------------------------------------

def adopt_sessions(state_root: str | Path, *,
                   existing: Any = (),
                   alive: Callable[[int | None], bool] | None = None,
                   poll_seconds: float = READ_POLL_SECONDS,
                   discover: Callable[..., dict] | None = None) -> dict[str, dict[str, Any]]:
    """Inspect the state directory and decide, per session, what to do.

    Pure: returns a verdict per session name and creates no backend state, so it
    is safe to call for a dry-run report as well as for real adoption. Names in
    `existing` are reported ALREADY_PRESENT and never given a second proxy --
    which is what makes a repeated or concurrent adoption pass unable to create
    a duplicate session (requirement 5)."""
    root = Path(state_root)
    finder = discover or wsh.discover
    present = set(existing or ())
    report: dict[str, dict[str, Any]] = {}
    for name, entry in finder(root, alive=alive).items():
        if name in present:
            report[name] = {"verdict": ALREADY_PRESENT, "proxy": None, "meta": entry.get("meta")}
            continue
        state = entry.get("host_state")
        if state != HOST_ALIVE:
            verdict = (ORPHAN_PID_REUSED if state == wsh.HOST_PID_REUSED else ORPHAN_HOST_GONE)
            report[name] = {"verdict": verdict, "proxy": None, "meta": entry.get("meta"),
                            "reason": entry.get("reason")}
            continue
        paths = SessionPaths(root, name)
        meta = wsh.read_meta(paths)
        if meta is None:  # vanished between discovery and here
            report[name] = {"verdict": ADOPT_FAILED, "proxy": None, "meta": None,
                            "reason": "META_DISAPPEARED"}
            continue
        report[name] = {
            "verdict": ADOPTED, "meta": meta.to_dict(),
            # Offset 0 on purpose: replay the spool so the restarted agent's
            # scrollback is the session's real history, not blank from now on.
            "proxy": HostProcessProxy(paths, meta, alive=alive, poll_seconds=poll_seconds),
        }
    return report


def orphan_names(report: dict[str, dict[str, Any]]) -> list[str]:
    """The sessions an operator could clean up. Reporting only -- nothing here
    removes them, because a PID_REUSED verdict can also mean a live host that
    was merely slow to heartbeat, and deleting that destroys real history."""
    return sorted(name for name, entry in report.items()
                  if entry["verdict"] in (ORPHAN_HOST_GONE, ORPHAN_PID_REUSED))
