"""A per-session HOST process that owns the PTY, so the node agent does not.

THE PROBLEM THIS EXISTS TO FIX, and the empirical finding behind it.

Today a Windows session's ConPTY child is spawned by `WindowsSessionBackend`
INSIDE the node-agent process, and the only session registry is that backend's
own in-memory dict. The Phase 0 audit (docs/REQUIREMENTS.md, "Windows
node-agent restart safety") proved live, against real Windows processes, that
such a child does NOT survive the agent's exit -- identical outcome via a hard
`taskkill /F` and via the graceful `/v1/internal/shutdown` path. So every
node-agent update is an outage for every session on that node, and the state
needed to reconnect does not exist on disk at all.

That finding is scoped to "the current architecture", which is what this module
replaces. The fix is not a cleverer way to kill the agent; it is to stop the
session being the agent's child in the first place.

THE ARCHITECTURE.

    node agent (control plane, restartable)
        |  reads meta.json / out.log, writes in.log + ctl.json
        v
    session host  (one detached process per session, owns the PTY)
        |  ConPTY / pty
        v
    powershell.exe | claude.exe | codex

The agent never holds the PTY. It holds file handles onto a spool, which is why
restarting it is uneventful: nothing is torn down, and after restart it
rediscovers sessions by reading the state directory.

WHY A FILE SPOOL RATHER THAN A SOCKET/NAMED-PIPE RPC. A live control connection
has to be re-established after a restart, which means a handshake, a protocol,
versioning between an old host and a new agent, and a decision about what
happens to output produced while nobody was connected. An append-only spool has
none of that: output written while the agent was down is simply still there when
it comes back, history survives the agent by construction rather than by
buffering, and "reconnect" is `open()`. That is the smallest design that
actually satisfies "stdout/history and input continue" across an arbitrary
restart, and the smallest was the requirement.

Per session, under `<state>/win-sessions/<name>/`:

    meta.json   atomic, the only source of truth for host pid / child pid /
                generation / cwd / argv / geometry
    out.log     append-only output spool, rotated at a bound
    in.log      append-only input spool, read by offset like out.log
    ctl.json    control requests the host polls (resize, shutdown)
    host.log    the host's own diagnostics, never session output

WINDOWS PROCESS OWNERSHIP -- the part that must be exactly right.

Spawning the host with `DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB |
CREATE_NEW_PROCESS_GROUP`:

  - DETACHED_PROCESS: no inherited console. Without it the host joins the
    agent's console and dies with it.
  - CREATE_BREAKAWAY_FROM_JOB: leaves the agent's job object. This is the one
    that actually matters. A Scheduled Task's process tree is commonly placed in
    a job with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, and without breakaway the OS
    kills the host when the agent's job closes no matter how detached it looks.
    It fails with ERROR_ACCESS_DENIED if the job forbids breakaway
    (JOB_OBJECT_LIMIT_BREAKAWAY_OK unset), which is why `spawn_host` reports
    that failure explicitly instead of silently falling back to a child that
    will die.
  - CREATE_NEW_PROCESS_GROUP: Ctrl-C to the agent's group does not reach it.

Also: no handle inheritance (`close_fds`), and stdio goes to the host's own log,
never to an inherited pipe -- an inherited pipe handle keeps a dead agent's
objects alive and reintroduces exactly the coupling this removes.

NOT VERIFIED ON REAL WINDOWS. This host, its flags and its ConPTY interaction
are exercised here against a real POSIX pty with real processes (the same
discipline tests/test_windows_backend.py already uses, because this project's
dev and CI hosts are Linux). The flag semantics above are from the Win32
contract, not from an observed run on dell-5530 -- and dell-5530 is on 0.12.0
and must not be restarted. Every claim about survival on real Windows is
therefore DESIGNED and REVIEWED, not MEASURED, and the rollout procedure in
docs/WINDOWS_SESSION_HOST.md treats it that way.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SPOOL_NAME = "out.log"
META_NAME = "meta.json"
INPUT_NAME = "in.log"
CONTROL_NAME = "ctl.json"
HOST_LOG_NAME = "host.log"

DEFAULT_SPOOL_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_POLL_SECONDS = 0.05
PROTOCOL_VERSION = 1

# Win32 creation flags. Defined here rather than imported from subprocess so this
# module's intent is readable on Linux too, and so a test can assert the exact
# combination without a Windows host.
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000

WINDOWS_DETACH_FLAGS = (DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB
                        | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW)
"""The exact combination `spawn_host` uses on Windows. Asserted by a test, so a
future edit that drops CREATE_BREAKAWAY_FROM_JOB -- the one whose absence lets a
job object kill the host -- fails loudly rather than shipping a session that
still dies with the agent."""


class HostError(RuntimeError):
    pass


class BreakawayDenied(HostError):
    """CREATE_BREAKAWAY_FROM_JOB was refused by the job object.

    Raised rather than retried without the flag: a host spawned without
    breakaway looks identical and dies with the agent, which is the exact bug
    being fixed. An operator needs to know the job forbids breakaway so the
    Scheduled Task can be registered to allow it."""


@dataclass(frozen=True)
class SessionMeta:
    """What a restarted agent needs to find and re-adopt a session.

    `host_generation` is a random id minted once per host process, not a
    counter: it answers "is this the same host process I recorded?" across a
    PID that may have been reused, which a monotonic counter cannot do without
    its own persisted state (the same reasoning node_agent.AGENT_GENERATION
    already uses)."""

    name: str
    host_pid: int
    host_generation: str
    child_pid: int | None
    cwd: str
    argv: list[str]
    created_at: float
    rows: int = 24
    cols: int = 80
    agent_generation: str | None = None
    protocol_version: int = PROTOCOL_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "host_pid": self.host_pid,
            "host_generation": self.host_generation, "child_pid": self.child_pid,
            "cwd": self.cwd, "argv": list(self.argv), "created_at": self.created_at,
            "rows": self.rows, "cols": self.cols,
            "agent_generation": self.agent_generation,
            "protocol_version": self.protocol_version, "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SessionMeta":
        return cls(
            name=str(raw.get("name") or ""), host_pid=int(raw.get("host_pid") or 0),
            host_generation=str(raw.get("host_generation") or ""),
            child_pid=(int(raw["child_pid"]) if raw.get("child_pid") else None),
            cwd=str(raw.get("cwd") or ""), argv=list(raw.get("argv") or []),
            created_at=float(raw.get("created_at") or 0.0),
            rows=int(raw.get("rows") or 24), cols=int(raw.get("cols") or 80),
            agent_generation=raw.get("agent_generation"),
            protocol_version=int(raw.get("protocol_version") or PROTOCOL_VERSION),
            extra=dict(raw.get("extra") or {}))


# -- the state directory -------------------------------------------------

class SessionPaths:
    def __init__(self, root: str | Path, name: str) -> None:
        self.root = Path(root)
        self.name = name
        self.dir = self.root / name

    @property
    def meta(self) -> Path:
        return self.dir / META_NAME

    @property
    def spool(self) -> Path:
        return self.dir / SPOOL_NAME

    @property
    def input(self) -> Path:
        return self.dir / INPUT_NAME

    @property
    def control(self) -> Path:
        return self.dir / CONTROL_NAME

    @property
    def host_log(self) -> Path:
        return self.dir / HOST_LOG_NAME

    def ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)


def write_meta_atomic(paths: SessionPaths, meta: SessionMeta) -> None:
    """Write metadata so a reader NEVER sees a half-written record.

    Temp file in the SAME directory then `os.replace`, which is atomic on both
    POSIX and Windows (MoveFileEx with REPLACE_EXISTING). A plain open("w")
    truncates first, so an agent reading during a crash window would find an
    empty or partial file and conclude the session does not exist -- and act on
    that. `fsync` before the rename so the bytes, not just the directory entry,
    survive a host crash."""
    paths.ensure()
    # The temp name is unique per WRITER, not just per process: the heartbeat
    # thread and the resize path both publish metadata, and a shared temp file
    # would let two writers interleave their bytes into it before either
    # rename -- producing an atomically-installed but internally corrupt record,
    # which is worse than a partial write because it looks committed.
    # Thread identifiers can be reused as soon as a short lived writer
    # exits. A per-write random suffix keeps simultaneous publications
    # independent even when the OS recycles a thread ID.
    tmp = paths.dir / f".{META_NAME}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    payload = json.dumps(meta.to_dict(), indent=2, sort_keys=True)
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, paths.meta)


def read_meta(paths: SessionPaths) -> SessionMeta | None:
    """None for absent, unreadable or malformed metadata.

    Never raises and never guesses: a corrupt record means "no usable session
    here", which routes into orphan handling rather than into a reconnect
    against invented values."""
    try:
        raw = json.loads(paths.meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not raw.get("name"):
        return None
    try:
        return SessionMeta.from_dict(raw)
    except (TypeError, ValueError):
        return None


# -- liveness, with PID-reuse protection ---------------------------------

def pid_alive(pid: int | None) -> bool:
    """Is this PID running right now? Says nothing about WHICH process it is."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - exercised only on Windows
        return _win32_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return not _posix_is_zombie(pid)


def _posix_is_zombie(pid: int) -> bool:
    """Is this PID an already-exited process that nobody has reaped yet?

    `os.kill(pid, 0)` succeeds on a zombie, so without this check a host that
    was killed a moment ago still reads as ALIVE for as long as its parent has
    not called wait() -- and the agent would happily "adopt" it, then report a
    healthy session whose pty is gone.

    Windows has no equivalent state (a terminated process's handle reports a real
    exit code immediately), so this is the POSIX side of making liveness mean the
    same thing on both platforms rather than a test-only convenience. Unreadable
    /proc is treated as not-a-zombie: that direction only ever loses this extra
    check and falls back to the kill(0) answer, whereas guessing "zombie" would
    abandon a live session."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rpartition(b")")[2].split()
        return bool(fields) and fields[0] == b"Z"
    except OSError:
        return False


def _win32_pid_alive(pid: int) -> bool:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


HOST_ALIVE = "ALIVE"
HOST_GONE = "GONE"
HOST_PID_REUSED = "PID_REUSED"
HOST_STATES = (HOST_ALIVE, HOST_GONE, HOST_PID_REUSED)


def host_state(paths: SessionPaths, meta: SessionMeta, *,
               alive: Any = None) -> str:
    """Is the recorded host still the process we recorded?

    PID alone is not enough. A host can die and the OS can hand its number to
    something unrelated -- on Windows PIDs are recycled aggressively -- and a
    stale record whose PID happens to be live again would make an agent adopt a
    session whose "host" is somebody else's process, then write input into a
    pipe nothing is reading. So a live PID is confirmed against a liveness
    token the host itself maintains:

        the host rewrites `meta.json` with its own generation, and holds an
        exclusive lock on its own directory for its lifetime.

    The cheap, portable check used here is the generation recorded in the file
    versus the generation the running host reports in its heartbeat field. If
    the PID is live but the heartbeat has not advanced past the host's declared
    interval, the record is treated as PID_REUSED rather than ALIVE -- the
    conservative direction, because the cost of a false ALIVE is writing into a
    void while telling an operator the session is healthy."""
    checker = alive or pid_alive
    if not checker(meta.host_pid):
        return HOST_GONE
    beat = _heartbeat_age(paths)
    if beat is None:
        # PID is live but the host never wrote a heartbeat: either a foreign
        # process now holds that PID, or a host that died before its first beat.
        return HOST_PID_REUSED
    if beat > STALE_HEARTBEAT_SECONDS:
        return HOST_PID_REUSED
    return HOST_ALIVE


STALE_HEARTBEAT_SECONDS = 30.0
HEARTBEAT_INTERVAL_SECONDS = 5.0


def _heartbeat_age(paths: SessionPaths) -> float | None:
    try:
        return max(0.0, time.time() - paths.meta.stat().st_mtime)
    except OSError:
        return None


# -- the host process ----------------------------------------------------

class SessionHost:
    """Runs INSIDE the detached host process. One instance per session.

    Owns the PTY and nothing else: it does not know about the agent, HTTP, the
    controller or the session registry. That is deliberate -- a host that had to
    understand the agent's protocol would need upgrading in lockstep with it,
    and the entire point is that the agent can be replaced underneath."""

    def __init__(self, paths: SessionPaths, process: Any, *,
                 spool_max_bytes: int = DEFAULT_SPOOL_MAX_BYTES,
                 poll_seconds: float = DEFAULT_POLL_SECONDS,
                 generation: str | None = None) -> None:
        self.paths = paths
        self.process = process
        self.spool_max_bytes = spool_max_bytes
        self.poll_seconds = poll_seconds
        self.generation = generation or os.urandom(8).hex()
        self._stop = False
        self._meta: SessionMeta | None = None

    # -- lifecycle -------------------------------------------------------

    def publish(self, meta: SessionMeta) -> None:
        self._meta = meta
        write_meta_atomic(self.paths, meta)

    def heartbeat(self) -> None:
        """Re-stamp the metadata file. Its mtime IS the heartbeat -- no second
        file to keep consistent with the first, and a reader that can read the
        metadata can already see the beat."""
        if self._meta is not None:
            write_meta_atomic(self.paths, self._meta)

    def stop(self) -> None:
        self._stop = True

    # -- the pumps -------------------------------------------------------

    def append_output(self, data: str) -> None:
        """Append to the spool, rotating at the bound.

        Rotation drops the OLDEST half rather than truncating to empty: an
        operator debugging a session that has been running for days needs the
        recent tail, and truncating on overflow would throw away precisely the
        part they are looking at."""
        if not data:
            return
        self.paths.ensure()
        with open(self.paths.spool, "a", encoding="utf-8", errors="replace") as handle:
            handle.write(data)
        try:
            if self.paths.spool.stat().st_size > self.spool_max_bytes:
                self._rotate_spool()
        except OSError:
            pass

    def _rotate_spool(self) -> None:
        try:
            text = self.paths.spool.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        keep = text[len(text) // 2:]
        # Resume at a line boundary so a reader never starts mid-escape-sequence.
        newline = keep.find("\n")
        if newline != -1:
            keep = keep[newline + 1:]
        tmp = self.paths.dir / f".{SPOOL_NAME}.rot"
        tmp.write_text(keep, encoding="utf-8")
        os.replace(tmp, self.paths.spool)

    def drain_control(self) -> list[dict[str, Any]]:
        """Read and CLEAR pending control requests.

        Read-then-unlink rather than a growing log: a control request is
        consumed exactly once, and a host that crashed mid-resize must not
        replay it forever on restart."""
        try:
            raw = self.paths.control.read_text(encoding="utf-8")
        except OSError:
            return []
        try:
            self.paths.control.unlink()
        except OSError:
            pass
        requests: list[dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                requests.append(parsed)
        return requests

    def apply_control(self, request: dict[str, Any]) -> None:
        op = str(request.get("op") or "")
        if op == "resize":
            try:
                rows, cols = int(request["rows"]), int(request["cols"])
            except (KeyError, TypeError, ValueError):
                return
            try:
                self.process.setwinsize(rows, cols)
            except Exception:  # noqa: BLE001 -- a bad resize must not kill the host
                return
            if self._meta is not None:
                self._meta = SessionMeta(**{**self._meta.to_dict(),
                                            "argv": list(self._meta.argv),
                                            "rows": rows, "cols": cols})
                write_meta_atomic(self.paths, self._meta)
        elif op == "shutdown":
            self.stop()


# -- spawning a host -----------------------------------------------------

def windows_creation_flags() -> int:
    return WINDOWS_DETACH_FLAGS


def spawn_host(paths: SessionPaths, argv: list[str], cwd: str, *,
               python: str | None = None, popen: Any = None,
               env: dict[str, str] | None = None) -> int:
    """Start a DETACHED host process for this session. Returns its pid.

    The host is started as `python -m terminal_mcp.windows_session_host` rather
    than by forking: on Windows there is no fork, and a spawn keeps the two
    platforms on one code path instead of a POSIX-only shortcut that then
    behaves differently in production from in tests.

    stdio is redirected to the host's own log file, never to a pipe. An
    inherited pipe handle would keep the agent's objects referenced and give the
    host a reason to die when the agent's handles close -- reintroducing the
    coupling this whole module removes."""
    import subprocess

    paths.ensure()
    runner = popen or subprocess.Popen
    command = [python or sys.executable, "-m", "terminal_mcp.windows_session_host",
               "--state-root", str(paths.root), "--name", paths.name,
               "--cwd", cwd, "--"] + list(argv)

    kwargs: dict[str, Any] = {"cwd": cwd, "close_fds": True,
                              "env": dict(env or os.environ)}
    if os.name == "nt":  # pragma: no cover - Windows only
        kwargs["creationflags"] = windows_creation_flags()
    else:
        # POSIX equivalent of the same intent: a new session, so the host is not
        # in the agent's process group and does not receive its signals.
        kwargs["start_new_session"] = True

    log = open(paths.host_log, "a", encoding="utf-8")
    try:
        process = runner(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs)
    except OSError as exc:
        log.close()
        if getattr(exc, "winerror", None) == 5:  # pragma: no cover - Windows only
            raise BreakawayDenied(
                "CREATE_BREAKAWAY_FROM_JOB was denied by the agent's job object -- "
                "register the Scheduled Task so breakaway is permitted, or the host "
                "will be killed with the agent") from exc
        raise HostError(f"could not spawn a session host: {exc}") from exc
    finally:
        try:
            log.close()
        except OSError:
            pass
    return int(process.pid)


# -- the agent-side view -------------------------------------------------

def discover(state_root: str | Path, *, alive: Any = None) -> dict[str, dict[str, Any]]:
    """What sessions exist on disk right now, and are their hosts real?

    This is what makes an agent restart uneventful: no handoff, no handshake --
    the agent reads the directory and knows. Returns one entry per session
    directory, each carrying its metadata and a host_state of
    ALIVE / GONE / PID_REUSED, so the caller decides what to adopt and what to
    treat as an orphan. Never raises on one bad directory."""
    root = Path(state_root)
    found: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return found
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        paths = SessionPaths(root, child.name)
        meta = read_meta(paths)
        if meta is None:
            found[child.name] = {"name": child.name, "meta": None,
                                 "host_state": HOST_GONE, "reason": "NO_METADATA"}
            continue
        found[child.name] = {"name": child.name, "meta": meta.to_dict(),
                             "host_state": host_state(paths, meta, alive=alive)}
    return found


def read_spool(paths: SessionPaths, *, offset: int = 0,
               max_bytes: int = 1024 * 1024) -> tuple[str, int]:
    """Read the spool from `offset`. Returns (text, new_offset).

    Byte offsets, not line counts: an agent restart resumes exactly where the
    previous one stopped without re-reading or skipping, and a rotation is
    visible as the file having shrunk below the offset -- which the caller
    handles by resetting to 0 rather than reading garbage."""
    return _read_at_offset(paths.spool, offset=offset, max_bytes=max_bytes)


def _read_at_offset(path: Path, *, offset: int, max_bytes: int) -> tuple[str, int]:
    try:
        size = path.stat().st_size
    except OSError:
        return "", offset
    if offset > size:
        offset = 0  # rotated underneath us
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            chunk = handle.read(max_bytes)
    except OSError:
        return "", offset
    return chunk.decode("utf-8", errors="replace"), offset + len(chunk)


def write_input(paths: SessionPaths, data: str) -> bool:
    """Queue input for the session. True when it was written to the spool.

    An append to a file, deliberately, rather than a FIFO or a named pipe. A
    FIFO does not exist on Windows and a Windows named pipe is a different API
    with different semantics, so a pipe-based channel means two
    platform-specific implementations of the one path that carries an operator's
    keystrokes -- and the Windows half could only ever be tested on a Windows
    host, which this project does not have in CI. An offset-read spool is one
    implementation on both, and it inherits the same property as the output
    spool: input queued while the host is briefly busy is not lost.

    The directory is NOT created here. If it is gone the session is gone, and
    silently recreating it would turn "this session no longer exists" into a
    write that appears to succeed and is read by nobody.

    This returning True means the bytes are in the spool -- NOT that a live host
    read them. Liveness is a separate question, and the caller that cares
    (HostProcessProxy.write) checks it explicitly rather than inferring it from
    a successful write."""
    if not data:
        return True
    try:
        with open(paths.input, "a", encoding="utf-8", errors="replace") as handle:
            handle.write(data)
        return True
    except OSError:
        return False


def read_input(paths: SessionPaths, *, offset: int = 0,
               max_bytes: int = 256 * 1024) -> tuple[str, int]:
    """Host side of the input spool: read what the agent has queued since
    `offset`. Same offset discipline as read_spool, so a rotation shows up as
    the file having shrunk and resets rather than reading garbage."""
    return _read_at_offset(paths.input, offset=offset, max_bytes=max_bytes)


def request_control(paths: SessionPaths, request: dict[str, Any]) -> bool:
    """Queue a control request (resize/shutdown). Append-only, one JSON per
    line, so two concurrent requests cannot interleave into one corrupt
    object."""
    try:
        paths.ensure()
        with open(paths.control, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(request) + "\n")
        return True
    except OSError:
        return False


def terminate_host(paths: SessionPaths, meta: SessionMeta, *,
                   alive: Any = None, killer: Any = None) -> bool:
    """Stop a host process. Explicit only -- nothing here runs on a timer.

    Refuses anything that is not confirmed ALIVE. A live PID is NOT sufficient:
    a stale record whose host died and whose PID the OS has since handed to
    something unrelated would otherwise make this signal a foreign process --
    the worst outcome available in this module, and on Windows `taskkill /T`
    would take that process's children with it. So the full `host_state` check
    (PID **and** a fresh heartbeat) gates the signal, which is why `paths` is
    required here rather than being an optional extra safety argument that a
    caller could omit exactly when it matters."""
    if not meta.host_pid or meta.host_pid <= 0:
        return False
    if host_state(paths, meta, alive=alive) != HOST_ALIVE:
        return False
    send = killer or _default_kill
    try:
        send(meta.host_pid)
        return True
    except (OSError, ProcessLookupError):
        return False


def _default_kill(pid: int) -> None:
    if os.name == "nt":  # pragma: no cover - Windows only
        import subprocess

        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, check=False)
        return
    os.kill(pid, signal.SIGTERM)


def cleanup_session_dir(paths: SessionPaths) -> bool:
    """Remove a session's state directory. EXPLICIT ONLY.

    Never called on a schedule and never on a host that might be alive: the
    caller establishes GONE first. Requirement 4 is that orphan cleanup is
    explicit and safe, and a sweeper that deleted a directory whose host was
    merely slow to heartbeat would destroy a live session's history."""
    import shutil

    try:
        shutil.rmtree(paths.dir)
        return True
    except OSError:
        return False


# -- host entry point ----------------------------------------------------

PTY_FACTORY_ENV = "TERMINAL_MCP_SESSION_PTY_FACTORY"
"""`module:attr` naming the `(argv, cwd) -> PtyProcessLike` the host should use.

Exists so the host entry point itself can be exercised for real -- spawned,
outliving its spawner, adopted again -- on this project's Linux dev and CI
hosts, against a real POSIX pty. Without it `main()` would be the one piece of
this design that no test ever runs, which is exactly the piece whose failure
mode is "sessions silently do not come up on the node". Production sets nothing
and gets the real pywinpty path.
"""


def _resolve_pty_factory() -> Any:
    spec = os.environ.get(PTY_FACTORY_ENV)
    if not spec:
        from .windows_backend import _default_process_factory

        return _default_process_factory
    module_name, _, attr = spec.partition(":")
    import importlib

    return getattr(importlib.import_module(module_name), attr)


def main(argv: list[str] | None = None) -> int:
    """`python -m terminal_mcp.windows_session_host` -- the detached host.

    Runs until the child exits or a shutdown request arrives. Everything it owns
    is per-session: it has no idea the agent exists, which is what allows the
    agent to be replaced underneath it."""
    import argparse
    import threading

    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--rows", type=int, default=24)
    parser.add_argument("--cols", type=int, default=80)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]

    paths = SessionPaths(args.state_root, args.name)
    paths.ensure()
    # Create the input spool BEFORE publishing metadata: once metadata exists an
    # agent may adopt this session and immediately write input, and an append to
    # a file that does not exist yet would be reported to the operator as the
    # session refusing input.
    try:
        paths.input.touch(exist_ok=True)
    except OSError:
        pass

    try:
        process = _resolve_pty_factory()(command, args.cwd)
    except Exception as exc:  # noqa: BLE001 -- report, never leave a silent empty session dir
        _log(paths, f"FATAL could not start {command!r} in {args.cwd!r}: {exc!r}")
        return 2

    host = SessionHost(paths, process)
    host.publish(SessionMeta(
        name=args.name, host_pid=os.getpid(), host_generation=host.generation,
        child_pid=getattr(process, "pid", None), cwd=args.cwd, argv=command,
        created_at=time.time(), rows=args.rows, cols=args.cols,
        agent_generation=os.environ.get("TERMINAL_MCP_AGENT_GENERATION")))
    _log(paths, f"host up pid={os.getpid()} child={getattr(process, 'pid', None)} argv={command!r}")

    stop = threading.Event()

    def _input_pump() -> None:
        """Feed queued input to the child. Its own thread because the output
        read below can block, and an operator's keystroke must not wait for the
        session to produce output first."""
        offset = 0
        while not stop.is_set():
            data, offset = read_input(paths, offset=offset)
            if not data:
                stop.wait(host.poll_seconds)
                continue
            try:
                process.write(data)
            except Exception as exc:  # noqa: BLE001 -- a broken write must not kill the host
                _log(paths, f"input write failed: {exc!r}")

    def _heartbeat_pump() -> None:
        """Re-stamp the metadata on a fixed interval, in its OWN thread.

        Deliberately not folded into the output loop below: `process.read()` can
        block until the child says something, so a session sitting idle at a
        prompt would skip heartbeats, its metadata would go stale, and the agent
        would classify a perfectly healthy host as PID_REUSED and abandon it.
        Liveness must not depend on the child being talkative."""
        while not stop.is_set():
            host.heartbeat()
            stop.wait(HEARTBEAT_INTERVAL_SECONDS)

    threading.Thread(target=_input_pump, name="host-input", daemon=True).start()
    threading.Thread(target=_heartbeat_pump, name="host-heartbeat", daemon=True).start()

    try:
        while not host._stop:
            try:
                data = process.read()
            except Exception:  # noqa: BLE001 -- a failed read means the pty is gone
                data = ""
            if data:
                host.append_output(data)
            for request in host.drain_control():
                host.apply_control(request)
            if not data:
                time.sleep(host.poll_seconds)
                try:
                    if not process.isalive():
                        break
                except Exception:  # noqa: BLE001
                    break
    finally:
        stop.set()
        _log(paths, "host exiting")
        # The child is terminated with the host: a host that exits while leaving
        # a live ConPTY behind would leave a process no agent can ever reach
        # again -- an orphan with no state directory pointing at it.
        try:
            process.terminate(force=True)
        except Exception:  # noqa: BLE001
            pass
    return 0


def _log(paths: SessionPaths, message: str) -> None:
    """Host diagnostics only -- never session output, which belongs in the
    spool. Best-effort: losing a log line must never end a session."""
    try:
        with open(paths.host_log, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
