"""WindowsSessionBackend -- a SessionBackend (session_backend.py)
implementation for a Windows node, backed by a ConPTY-attached persistent
PowerShell/cmd process per session, instead of tmux.

Runs INSIDE a Windows node agent process (windows_agent.py) only --
`TerminalService` (core.py) is constructed with an instance of this class
as its `tmux` object and otherwise runs completely unmodified: every
permission check, audit record, redaction, kill/reopen-metadata capture,
and the reliable-submission verification state machine (adapters.py) it
already has for Linux apply identically here, with zero duplicated logic
(see session_backend.py's own module docstring for why).

Real, honest architectural difference from tmux (documented, not hidden):
tmux is its OWN separate, persistent server process -- a session survives
even if every terminal-mcp process on the host is restarted, and
"attach"/"detach" are native tmux client operations. This backend has no
separate server: each session's child process is owned by THIS backend
instance, inside this ONE node-agent process. What IS built and real:

  - A session's process is NEVER tied to any one WebSocket viewer -- a
    background reader thread drains ConPTY output into a bounded ring
    buffer continuously, independent of whether anyone is watching, so
    "disconnect browser không kill process" (task's own explicit
    requirement) holds: closing the web terminal's WebSocket only stops
    that one viewer from receiving/sending bytes, never touches the
    underlying process, exactly like tmux `detach-client`.
  - `kill_session` genuinely terminates the process tree, freeing its
    RAM, exactly like tmux `kill-session` (never `taskkill /F` on the
    whole node -- see `_kill_process_tree` below for exactly what this
    signals).

What is NOT the same guarantee tmux gives (flagged here, in
docs/multi-node.md, and in the final report -- never silently assumed):
a session's process is normally a child of this node-agent's own
process. If the node-agent process itself is killed or crashes (not a
browser disconnect -- the AGENT process), its child sessions may be
terminated by the OS along with it, depending on how Windows job-object/
process-group semantics play out for however this was actually deployed
-- this backend does not attempt an OS-level "make this immune to my own
parent's death" trick (e.g. a detached process group) because that
specific behavior could not be verified against a real Windows host in
this environment; claiming it works would be exactly the kind of
unverified assertion this project's own standing rules forbid. A restart
of terminal-node-agent.service on a Windows node should be treated as
disruptive to that node's own sessions until this is verified live.

Testability without a real Windows machine: the actual `pywinpty`
import is LAZY (only inside `_default_process_factory`, called only when
actually spawning a real process) so this module imports cleanly on any
OS. Every test in tests/test_windows_backend.py instead injects a real,
functioning fake PTY process (`_FakePty` in that test module, built on
POSIX `pty.openpty()` + `subprocess.Popen`, since this dev environment is
Linux) satisfying the exact same `PtyProcessLike` Protocol pywinpty's own
`PtyProcess` already happens to satisfy -- so every test here exercises
this backend's REAL session-management logic (registry, buffering,
identity, attach/detach broadcast, kill, path/name validation) against a
REAL running child process, not a mock of this module's own behavior.
Only the actual `pywinpty`/ConPTY integration itself is unverified on
real Windows -- see this module's own report entry in the final summary.
"""
from __future__ import annotations

import queue
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

import pyte
from wcwidth import wcwidth

from .models import SessionInfo
from .redaction import strip_ansi
from .tmux import TmuxError

DEFAULT_HISTORY_LINES = 2000
DEFAULT_SHELL = "powershell.exe"
READ_CHUNK_SIZE = 4096
# Default ConPTY dimensions for a session's own canonical terminal-screen
# state (pyte.HistoryScreen) before any real resize has ever been applied
# -- matches the same 80x24 default winpty/ConPTY itself falls back to
# absent an explicit size (see _default_process_factory's own docstring
# on the dimension-mismatch bug that default caused before this
# project's earlier resize-sync fix). Kept in sync with the real ConPTY
# size on every _apply_resize call, below -- never allowed to drift.
DEFAULT_SCREEN_COLS = 80
DEFAULT_SCREEN_ROWS = 24


def _render_history_row(row: "pyte.screens.StaticDefaultDict", columns: int) -> str:
    """Same cell-joining logic pyte.Screen.display's own property uses
    internally (see its source) -- reused here because that property
    only ever renders the CURRENT visible screen (self.buffer), never a
    scrolled-off HISTORY row (self.screen.history.top/bottom), which has
    the identical per-column Char-dict shape but no built-in renderer of
    its own. Correctly skips the trailing stub cell of a wide (e.g. many
    CJK, some emoji) character rather than duplicating it -- the same
    correctness pyte's own renderer already gives the visible screen."""
    chars: list[str] = []
    is_wide_char = False
    for x in range(columns):
        if is_wide_char:
            is_wide_char = False
            continue
        char = row[x].data
        is_wide_char = bool(char) and wcwidth(char[0]) == 2
        chars.append(char)
    return "".join(chars)


# DEC private modes real terminals use for the "alternate screen buffer"
# (a full-screen TUI like Claude Code's own Ink renderer switches into
# this on startup and back out on exit) -- 1049 is the modern/xterm form
# (save cursor + switch + clear), 47/1047 are older, narrower variants
# real-world programs still sometimes emit. All three are treated the
# same way here: swap to/from a separate, blank buffer.
_ALTERNATE_SCREEN_MODES = (1049, 47, 1047)


class _AltScreenHistoryScreen(pyte.HistoryScreen):
    """pyte's own Screen/HistoryScreen already tracks DEC private modes
    1049/47/1047 being SET in `self.mode` (see set_mode's own shifted-
    value storage), but does not itself swap to a separate buffer for
    them -- real xterm/VT100 alternate-screen semantics needs exactly
    that: entering it saves the current (primary) screen content and
    cursor and starts from a blank screen; leaving it restores exactly
    what was there. Confirmed live testing this fix: without this, a
    full-screen TUI's own content could show interleaved with whatever
    was on screen right before it started. A minimal, targeted addition
    on top of pyte's own otherwise-correct VT100 emulation -- not a
    second parser, just the one real behavior this library's own base
    classes don't implement (checked directly against pyte 0.8's own
    source: Screen.set_mode/reset_mode never reference these modes at
    all beyond the generic mode-set bookkeeping every mode gets)."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._alt_screen_active = False
        self._primary_buffer: Any = None
        self._primary_cursor: Any = None

    def set_mode(self, *modes: int, **kwargs: Any) -> None:
        super().set_mode(*modes, **kwargs)
        if kwargs.get("private") and not self._alt_screen_active and any(m in _ALTERNATE_SCREEN_MODES for m in modes):
            self._alt_screen_active = True
            self._primary_buffer = self.buffer
            self._primary_cursor = self.cursor
            self.buffer = type(self.buffer)(self.buffer.default_factory)
            self.dirty.update(range(self.lines))
            self.cursor_position()

    def reset_mode(self, *modes: int, **kwargs: Any) -> None:
        was_active = self._alt_screen_active
        super().reset_mode(*modes, **kwargs)
        if kwargs.get("private") and was_active and any(m in _ALTERNATE_SCREEN_MODES for m in modes):
            self._alt_screen_active = False
            if self._primary_buffer is not None:
                self.buffer = self._primary_buffer
                self.cursor = self._primary_cursor
                self.dirty.update(range(self.lines))
            self._primary_buffer = None
            self._primary_cursor = None


# Same rationale as tmux.py's SEND_TEXT_ENTER_SETTLE_SECONDS -- a small
# gap between the literal text write and the Enter byte so an
# interactive program's own async input handling has time to consume the
# text as one write before Enter arrives as a separate one. Kept as its
# own constant (not imported from tmux.py) since the two backends' write
# paths are otherwise fully independent -- this is a deliberate, small
# duplication of ONE float, not the behavior itself.
SEND_TEXT_ENTER_SETTLE_SECONDS = 0.08

# VT100/ANSI sequences ConPTY (and every real Windows Terminal/console
# host since ConPTY's introduction) already interprets as INPUT the same
# way any other real terminal does -- send_keys writes these bytes
# directly into the pty, exactly like tmux.py's send_keys asks tmux to
# synthesize the same logical keys. Only ever reached for a key already
# validated against config.input_policy.allow_keys (core.py) -- this map
# itself is not a security boundary, just a translation table.
KEY_BYTES: dict[str, bytes] = {
    "Enter": b"\r",
    "Escape": b"\x1b",
    "Tab": b"\t",
    "Up": b"\x1b[A",
    "Down": b"\x1b[B",
    "Right": b"\x1b[C",
    "Left": b"\x1b[D",
    "C-c": b"\x03",
    "C-d": b"\x04",
}


class WindowsPathError(TmuxError):
    """A `cwd`/session-name value failed Windows-specific validation
    before ever reaching a spawn call -- see `validate_windows_cwd`."""


def validate_windows_cwd(raw: str) -> Path:
    """Defense-in-depth shape/injection check for a Windows working
    directory, BEFORE it is ever handed to resolve_cwd's real filesystem
    containment check (lifecycle.py -- unchanged, reused as-is: `Path`
    becomes a real `WindowsPathError → WindowsPath` automatically on an
    actual Windows interpreter, so allowed_cwd_roots/symlink-escape
    protection already applies correctly there with zero extra code).
    This function only rejects shapes that are never legitimate here --
    never a substitute for that real check.

    Rejects: empty/whitespace-only, embedded NUL, a UNC path (`\\\\...`,
    out of scope -- a node's own local drives only, not a network share
    another identity might control), and anything that is not an
    absolute, drive-rooted Windows path (`C:\\...`) -- a relative path
    would resolve against whatever the node-agent process's own CWD
    happens to be, which is never an intentional choice here. `..`
    traversal is deliberately NOT hand-checked here (`..\\..\\Windows\\
    System32` is syntactically a valid absolute-rooted path) -- exactly
    the case the real `resolve_cwd` containment check (comparing the
    fully `.resolve()`d path against allowed_cwd_roots) exists to catch,
    the same way it already does for a Linux `../`. Never touches the
    filesystem itself (no dependency on this actually running on
    Windows) -- purely string/shape validation, real existence and
    containment are `resolve_cwd`'s job."""
    if not raw or not raw.strip():
        raise WindowsPathError("cwd must not be empty")
    if "\x00" in raw:
        raise WindowsPathError("cwd must not contain a NUL byte")
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise WindowsPathError("UNC paths are not allowed for a session cwd")
    if not re.match(r"^[A-Za-z]:[\\/]", raw):
        raise WindowsPathError(f"cwd must be an absolute, drive-rooted Windows path (e.g. C:\\Users\\me), got {raw!r}")
    return Path(raw)


def validate_windows_session_name(name: str) -> None:
    """A session name becomes part of an internal dict key only (never
    concatenated into a shell command or a filesystem path on its own --
    see `new_session` below, which always passes argv lists to the
    process factory, never a formatted command string) -- this exists as
    a second, independent guard anyway: `valid_new_session_name`
    (permissions.py) already runs before this in every real call path
    (core.py/lifecycle.py), so this is defense-in-depth against this
    class ever being used directly, bypassing that check."""
    if not name or not name.strip():
        raise WindowsPathError("session name must not be empty")
    if "\x00" in name:
        raise WindowsPathError("session name must not contain a NUL byte")


@runtime_checkable
class PtyProcessLike(Protocol):
    """The exact shape this backend needs from a spawned pty process --
    pywinpty's own `winpty.PtyProcess` (real Windows) already has every
    one of these; tests/test_windows_backend.py's `_FakePty` (a real,
    running POSIX-pty-backed process, for testing on this Linux dev
    environment) implements the same shape."""

    pid: int

    def isalive(self) -> bool: ...
    def read(self, size: int = READ_CHUNK_SIZE) -> str: ...
    def write(self, data: str) -> int: ...
    def setwinsize(self, rows: int, cols: int) -> None: ...
    def terminate(self, force: bool = False) -> None: ...


ProcessFactory = Callable[[list[str], str], PtyProcessLike]
"""`(argv, cwd) -> PtyProcessLike` -- how a session's process actually
gets spawned. Swappable so tests never need a real Windows host or a
real `pywinpty` install; production uses `_default_process_factory`
(lazy `import winpty`, only reached if this ever actually runs on
Windows)."""


def _default_process_factory(argv: list[str], cwd: str) -> PtyProcessLike:
    # Deliberately lazy: importing winpty at module import time would
    # make this whole module fail to import on any non-Windows host
    # (including every test run in this project's own CI/dev
    # environment, which is Linux) -- see this module's own docstring.
    import winpty  # type: ignore[import-not-found]

    # argv is ALWAYS a plain list (never a formatted string, never
    # shell=True-equivalent) -- winpty.PtyProcess.spawn, like
    # subprocess.Popen on Windows, builds ONE correctly-quoted command
    # line from a list via the same discipline TmuxClient already uses
    # for every tmux invocation (tmux.py's `self._run([self.binary,
    # *args])`, never a shell string) -- matching that project-wide
    # convention, not a new one introduced here.
    return winpty.PtyProcess.spawn(argv, cwd=cwd)


ForegroundCommandResolver = Callable[[int, str], str]
"""`(root_pid, fallback_name) -> command_name` -- swappable exactly like
ProcessFactory above, for the same reason: the real implementation
(_win32_foreground_command) only works on an actual Windows host, so
tests on this Linux dev environment inject a fake one to verify the
WIRING (get_session actually calls it, actually uses its result,
correctly falls back on failure) without needing real Windows or a
real child-process tree."""


def _win32_foreground_command(root_pid: int, fallback_name: str) -> str:
    """P0 HOTFIX (task: 'P0 WINDOWS SESSION STATE-LOSS / STALE STREAM',
    item 5's own 'current_command should reflect foreground process/
    agent when available'): REAL BUG this fixes -- get_session() used to
    report the session's ORIGINAL spawn command forever (e.g.
    'powershell.exe'), never updated, because it was a bare Path(...)
    .name of the value captured once at create_session time. Confirmed
    live on the real 'window' session on dell-5530: `claude.exe` had
    been running for hours as a real, live, actively-CPU-burning CHILD
    of the session's own tracked powershell.exe, yet terminal_status
    kept reporting current_command='powershell.exe' throughout, feeding
    classify_status a permanently-wrong signal.

    Unlike tmux's own #{pane_current_command} (a native, O(1) query --
    the pty's own foreground process GROUP leader is a well-defined
    POSIX concept), Windows/ConPTY has no equivalent native "foreground
    process for this console" query. This walks the process tree
    instead, starting at the session's own root pid, descending through
    single-child chains (the same heuristic real terminal emulators use
    to approximate "what's actually running right now"): at each step,
    if the current process has exactly one still-alive child, descend
    into it; stop at a leaf (no children) or a branch (more than one
    live child, where "which one is foreground" is genuinely
    ambiguous -- reports the last unambiguous process rather than
    guessing). Best-effort, matches get_session's own existing
    "Best-effort approximation, not a native OS query" framing -- falls
    back to `fallback_name` on ANY failure (missing pywin32, a
    permission error enumerating another user's process, the root pid
    itself already gone), never raises past this function."""
    if sys.platform != "win32":
        return fallback_name
    try:
        import ctypes
        import ctypes.wintypes as wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == INVALID_HANDLE_VALUE:
            return fallback_name
        try:
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            children_by_parent: dict[int, list[tuple[int, str]]] = {}
            if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
                return fallback_name
            while True:
                children_by_parent.setdefault(entry.th32ParentProcessID, []).append(
                    (entry.th32ProcessID, entry.szExeFile.decode("mbcs", errors="replace")))
                if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snapshot)

        current_pid = root_pid
        current_name = fallback_name
        seen = {root_pid}  # cycle guard -- a snapshot race could in theory misreport a parent link
        while True:
            live_children = [
                (pid, name) for pid, name in children_by_parent.get(current_pid, [])
                if pid not in seen
            ]
            if len(live_children) != 1:
                break
            current_pid, exe = live_children[0]
            current_name = Path(exe).stem
            seen.add(current_pid)
        return current_name
    except Exception:  # noqa: BLE001 -- best-effort only, must never break get_session over this
        return fallback_name


def _kill_process_tree(proc: PtyProcessLike) -> None:
    """Terminate exactly this session's own process (and whatever it
    spawned under it, if the pty implementation's own terminate() covers
    that -- pywinpty's does, via the ConPTY's own process group) --
    never anything belonging to another session or to the node-agent
    process itself. The Windows-side analogue of tmux.py's kill_session
    docstring ("never kill-server, which would tear down every session
    on the host")."""
    try:
        proc.terminate(force=True)
    except Exception:  # noqa: BLE001 -- a process already gone/misbehaving must not crash kill_session
        pass


@dataclass
class _WindowsSession:
    name: str
    proc: PtyProcessLike
    cwd: str
    command: str | None
    created_epoch: int
    activity_epoch: int
    # Desktop-visibility metadata (task: "user nhìn tại máy Windows cũng
    # thấy đúng terminal session đang chạy") -- see windows_visible_
    # console.py's own module docstring for the full rationale/mechanism.
    # `visible` is only ever True when a DesktopViewerHandle was
    # actually, successfully spawned AND is still alive -- never assumed
    # from a caller's own request alone (task item 4's own explicit
    # "Không được giả là visible").
    visible: bool = False
    desktop_session_id: int | None = None
    visible_reason: str | None = None
    # The viewer VIEWING this session's real process (windows_visible_
    # console.DesktopViewerHandle) -- never the process itself. None
    # until show_on_desktop is requested (at creation or retroactively);
    # closing its window sets its own isalive() to False without this
    # entry, or entry.proc, ever being touched.
    desktop_viewer: object | None = None
    buffer: deque[str] = field(default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_LINES))
    _partial_line: str = ""
    # Canonical terminal-screen state (task: "Terminal MCP dashboard/
    # session renderer trên mobile/Windows" -- item 4's own "bổ sung
    # canonical terminal-screen state/VT parser"). `buffer`/_partial_line
    # above stay exactly as they were -- Session Knowledge Store's own
    # read_new_output() still reads from them, untouched by this fix --
    # `screen`/`vt_stream` are a SEPARATE, additional real VT100/xterm
    # emulator fed the exact same raw chunks in _reader_loop, and are
    # what capture_lines() (the dashboard's own read-only tail/preview
    # path) reads from instead: a simple \r-overwrite model (this
    # project's own earlier, smaller fix) cannot correctly resolve a
    # modern Ink-based TUI's real redraw mechanism -- full cursor
    # repositioning (CSI CUP), erase-line/screen, alternate-screen --
    # confirmed live: repeated "Beaming…Beaming…Beaming…" spinner text in
    # the real dell-5530 'window' session's own read-only preview, which
    # the \r-only fix alone could not fix. pyte.HistoryScreen bounds its
    # own scrollback the same way `buffer` above already does (maxlen-
    # style, via its own `history=` param) -- see get_session/_apply_
    # resize for how `screen`'s own dimensions stay synced to the real
    # ConPTY size, and capture_lines for how history+current-visible-
    # screen are combined into one flat snapshot.
    screen: "pyte.HistoryScreen" = field(
        default_factory=lambda: _AltScreenHistoryScreen(DEFAULT_SCREEN_COLS, DEFAULT_SCREEN_ROWS,
                                                    history=DEFAULT_HISTORY_LINES))
    # Bound to `screen` in __post_init__ below -- never constructed
    # independently, so a caller can never pass a vt_stream wrapping a
    # DIFFERENT screen instance by mistake (a real, easy-to-make error
    # this dataclass shape makes structurally impossible instead).
    vt_stream: "pyte.Stream" = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.vt_stream = pyte.Stream(self.screen)
    lock: threading.Lock = field(default_factory=threading.Lock)
    attached_viewers: int = 0
    detach_callbacks: list[Callable[[], None]] = field(default_factory=list)
    # One Queue per currently-attached live viewer (windows_webterm.py) --
    # the reader thread pushes every raw chunk into each of these too, in
    # addition to the shared buffer above, so a viewer sees NEW output as
    # it happens (a live terminal feed) rather than re-polling the whole
    # accumulated buffer -- capture_lines (MCP tools/tail) and this live
    # stream are two independent consumers of the exact same reader loop,
    # never two separate reads of the underlying pty.
    viewer_queues: list["queue.Queue[str]"] = field(default_factory=list)
    reader_thread: threading.Thread | None = None
    stop_reading: threading.Event = field(default_factory=threading.Event)
    # Monotonic count of every COMPLETE line ever appended to `buffer`
    # (never reset, never decremented) -- Session Knowledge Store's own
    # capture cursor for this backend (see read_new_output below):
    # `buffer` itself is bounded (history_lines), so this is what lets a
    # caller detect "some of what I haven't captured yet has already been
    # evicted" rather than silently re-reading old content or missing a
    # gap.
    total_lines_seen: int = 0
    # (rows, cols) most recently actually applied via resize()/
    # resize_from_desktop_viewer() -- an idempotency guard, never a time-
    # based debounce (see resize()'s own docstring: a genuinely new size
    # must never be silently dropped, only a REPEAT of the size already
    # in effect).
    last_resize_dims: tuple[int, int] | None = None
    # Cursor column within `_partial_line` -- see _append_chunk's own
    # docstring (P0 HOTFIX: "\r overwrite fix for last_output"). Always
    # 0 <= _partial_line_col <= len(_partial_line); reset to 0 by '\r'
    # or by committing the line on '\n', never negative.
    _partial_line_col: int = 0
    # P0 HOTFIX (task: "P0 WINDOWS SESSION STATE-LOSS / STALE STREAM"):
    # reader_thread's own liveness was previously never checked again
    # after create_session -- a died thread (entry.proc.read() raised
    # once) meant this session's output stream silently froze forever,
    # indistinguishable from "genuinely idle" to any caller. Counted/
    # timestamped (not just a bool) so health metadata can show an
    # operator "this session's stream recovered from a stall N times",
    # never silently hiding that a real, if self-healed, problem
    # happened. See _ensure_reader_alive.
    reader_restarts: int = 0
    last_reader_restart_epoch: int | None = None


class WindowsSessionBackend:
    """Implements session_backend.SessionBackend (structural -- no
    inheritance declared, matching TmuxClient's own posture) over a
    registry of `_WindowsSession` entries, each with its own background
    reader thread continuously draining ConPTY output into a bounded
    buffer -- see this module's own docstring for exactly what
    "persistent" does and does not mean here."""

    def __init__(self, *, shell: str = DEFAULT_SHELL, history_lines: int = DEFAULT_HISTORY_LINES,
                process_factory: ProcessFactory | None = None,
                foreground_command_resolver: ForegroundCommandResolver | None = None) -> None:
        self.shell = shell
        self.history_lines = history_lines
        self._process_factory = process_factory or _default_process_factory
        self._foreground_command_resolver = foreground_command_resolver or _win32_foreground_command
        self._sessions: dict[str, _WindowsSession] = {}
        self._registry_lock = threading.Lock()

    # -- SessionBackend surface ------------------------------------------

    def list_sessions(self) -> list[SessionInfo]:
        with self._registry_lock:
            names = list(self._sessions.keys())
        return [info for name in names if (info := self.get_session(name)) is not None]

    def get_session(self, name: str) -> SessionInfo | None:
        with self._registry_lock:
            session = self._sessions.get(name)
        if session is None:
            return None
        alive = _is_alive(session.proc)
        if alive:
            self._ensure_reader_alive(session)
        fallback_command = Path(session.command or self.shell).name
        try:
            # P0 HOTFIX (task: "P0 WINDOWS SESSION STATE-LOSS / STALE
            # STREAM"): re-derived on EVERY call, never cached/computed
            # once -- see _win32_foreground_command's own docstring for
            # the real bug this fixes (current_command used to report
            # this session's ORIGINAL spawn command forever, e.g.
            # "powershell.exe", even hours into a real claude.exe run as
            # its child). Best-effort: any failure here must never break
            # get_session itself, so it's wrapped even though the
            # resolver already catches its own errors internally.
            current_command = self._foreground_command_resolver(session.proc.pid, fallback_command)
        except Exception:  # noqa: BLE001
            current_command = fallback_command
        return SessionInfo(
            name=session.name,
            attached=session.attached_viewers > 0,
            windows=1,  # no multi-window concept on this backend -- always exactly one
            created_epoch=session.created_epoch,
            activity_epoch=session.activity_epoch,
            pane_pid=session.proc.pid,
            # Best-effort approximation, not a native OS query (unlike
            # tmux's own #{pane_current_command}) -- see module docstring.
            # Basename only (never a full path) -- matches tmux's own
            # #{pane_current_command} semantic exactly (always a bare
            # process name, e.g. "bash", never "/bin/bash"), which is
            # what core.py's _classify_agent_type/SHELL_COMMAND_NAMES and
            # config.session_lifecycle.launch_commands matching both
            # already assume. Now walks to the real foreground child
            # (e.g. "claude" running inside a powershell wrapper) rather
            # than reporting the session's own original spawn command
            # forever -- see _win32_foreground_command.
            pane_current_command=current_command,
            pane_dead=not alive,
            session_id=f"win:{session.name}:{session.created_epoch}",
            pane_id=f"pid:{session.proc.pid}",
            pane_in_mode=False,  # no copy-mode concept -- input is never blocked by one here
            pane_current_path=session.cwd,
            # None (not False) when the process itself is already dead --
            # "is the reader alive" stops being a meaningful question at
            # that point, distinct from "the reader specifically died
            # while the process kept running" (reader_alive=False, the
            # actual failure mode this whole fix targets).
            reader_alive=(session.reader_thread is not None and session.reader_thread.is_alive()) if alive else None,
            reader_restarts=session.reader_restarts,
        )

    def capture_lines(self, session: str, lines: int, *, ansi: bool = False) -> list[str]:
        """P0 HOTFIX (task: "Terminal MCP dashboard/session renderer trên
        mobile/Windows" -- item 4): now reads from the canonical, real-VT-
        emulated screen state (`entry.screen`, a pyte.HistoryScreen fed
        every raw chunk in _reader_loop) instead of the old naive line
        buffer -- see _WindowsSession's own "screen"/"vt_stream" field
        comments for the full ROOT_CAUSE this replaces (a bare \\r-
        overwrite model cannot correctly resolve full cursor-
        repositioning redraws, confirmed live as repeated "Beaming…"
        spinner text). `ansi` is accepted for call-site parity with the
        tmux backend but is a deliberate no-op HERE: pyte has already
        fully resolved every control sequence (SGR included) into a
        plain, correct text snapshot by this point -- there is no raw
        ANSI left to preserve or strip either way, matching item 2's own
        framing of the read-only path as "sanitized/rendered text
        snapshot; không raw ANSI" (color fidelity in the read-only
        preview is a real, deliberate simplification given up for
        correctness -- the LIVE interactive terminal, windows_webterm.py,
        is unaffected by any of this: it still gets the true raw byte
        stream straight from the reader loop, for a real xterm.js to
        render with full colour, exactly as before)."""
        entry = self._require(session)
        lines = max(1, lines)
        with entry.lock:
            history_rows = [_render_history_row(row, entry.screen.columns) for row in entry.screen.history.top]
            current_rows = [row.rstrip() for row in entry.screen.display]
        snapshot = history_rows + current_rows
        # pyte.Screen.display always returns exactly `screen.lines` rows
        # (padded with blanks down to the full screen height, regardless
        # of how much real content has actually been written) -- trim
        # only the TRAILING run of not-yet-drawn blank rows, never an
        # interior blank line that's genuinely part of the real content
        # (a paragraph break, a blank line in a diff, ...).
        while snapshot and not snapshot[-1]:
            snapshot.pop()
        return snapshot[-lines:]

    def read_new_output(self, session: str, cursor: int | None) -> tuple[str, int, bool]:
        """Session Knowledge Store's real capture mechanism for this
        backend (core.py wires this in, never called directly by a
        client) -- `cursor` is an opaque total-lines-seen count from a
        PREVIOUS call (None for the very first capture of this session
        instance). Returns (new_text, new_cursor, gap_occurred): `buffer`
        is bounded (history_lines) so old lines get evicted over time --
        `gap_occurred=True` means some of what hasn't been captured yet
        was ALREADY evicted before this call ever got to it (capture
        polling fell behind faster than the ring buffer's own retention),
        an honest signal rather than silently skipping or re-reading the
        wrong lines. A gap only ever loses what genuinely couldn't be
        captured in time -- it never duplicates content."""
        entry = self._require(session)
        with entry.lock:
            total = entry.total_lines_seen
            start_total = total - len(entry.buffer)
            effective_cursor = start_total if cursor is None else max(cursor, start_total)
            gap = cursor is not None and cursor < start_total
            skip = effective_cursor - start_total
            new_lines = list(entry.buffer)[skip:]
        text = ("\n".join(strip_ansi(line) for line in new_lines) + "\n") if new_lines else ""
        return text, total, gap

    def send_text(self, session: str, text: str, press_enter: bool) -> None:
        entry = self._require(session)
        try:
            entry.proc.write(text)
            if press_enter:
                time.sleep(SEND_TEXT_ENTER_SETTLE_SECONDS)
                entry.proc.write("\r")
        except Exception as exc:  # noqa: BLE001 -- any pty-write failure is a real backend error
            raise TmuxError(f"failed to write to session {session!r}: {exc}") from exc
        entry.activity_epoch = int(time.time())

    def send_keys(self, session: str, keys: list[str]) -> None:
        entry = self._require(session)
        for key in keys:
            data = KEY_BYTES.get(key)
            if data is None:
                # Never reached in practice -- core.py only calls this
                # with keys already validated against config.input_
                # policy.allow_keys, itself restricted to this same set
                # project-wide. Fails loudly rather than silently
                # dropping an unrecognized key, so a future allow_keys
                # addition that this map hasn't caught up with is
                # immediately visible instead of a silent no-op.
                raise TmuxError(f"WindowsSessionBackend has no key mapping for {key!r}")
            try:
                entry.proc.write(data.decode("latin-1"))
            except Exception as exc:  # noqa: BLE001
                raise TmuxError(f"failed to write key {key!r} to session {session!r}: {exc}") from exc
        entry.activity_epoch = int(time.time())

    def new_session(self, name: str, cwd: str, command: str | None = None, *,
                    show_on_desktop: bool = False) -> tuple[bool, str | None]:
        """The session's own process is ALWAYS the normal headless ConPTY
        child (`_spawn_headless`) -- exactly the same as every other
        session, visible-requested or not. `show_on_desktop=True` does
        NOT change what gets spawned as the session; it additionally
        attaches a real, visible desktop VIEWER onto it afterward (see
        `_attach_desktop_viewer`/windows_visible_console.py's own module
        docstring for the full rationale: this is the fix for a real,
        verified-live bug the previous design had, where the session's
        own shell owned the visible console directly and got force-
        killed by Windows the instant that window closed).

        Returns (visible, reason) -- `visible` is only True if a viewer
        window was ACTUALLY spawned; a request that can't be honored (no
        interactive desktop attached to this node-agent's own session,
        pywin32 missing, ...) never fails session creation itself, but
        reports exactly why, for the caller (core.py's terminal_create_
        session, surfaced to the dashboard) to show honestly -- never
        silently claims visible when it isn't.

        `cwd` is NOT re-validated with validate_windows_cwd here on
        purpose: by the time TerminalService/SessionLifecycleService
        calls this (lifecycle.py's create()), `cwd` is already
        resolve_cwd's own OUTPUT -- a real, existing, already-
        containment-checked path on THIS machine, produced by pathlib
        running natively on whatever OS this process is actually on.
        Re-demanding a literal "C:\\...\\" shape here would be not just
        redundant but actively WRONG: it would reject resolve_cwd's own
        valid output whenever this backend is exercised against a non-
        Windows-shaped (but real, resolved, and correctly contained) path
        -- exactly what this project's own test suite does on this Linux
        dev host (see tests/test_windows_terminal_service_integration.
        py). validate_windows_cwd stays available as a standalone,
        independently-tested utility for a caller that wants to pre-
        validate a RAW, not-yet-resolved client-supplied string before it
        ever reaches resolve_cwd (e.g. windows_agent.py could use it for
        an early, friendlier rejection) -- it is simply not appropriate
        to call from inside new_session, whose `cwd` argument is never
        that raw value."""
        validate_windows_session_name(name)
        with self._registry_lock:
            if name in self._sessions:
                raise TmuxError(f"session {name!r} already exists")
        argv = [command] if command else [self.shell]
        proc = self._spawn_headless(argv, cwd)
        now = int(time.time())
        entry = _WindowsSession(name=name, proc=proc, cwd=cwd, command=command,
                                created_epoch=now, activity_epoch=now,
                                buffer=deque(maxlen=self.history_lines),
                                screen=_AltScreenHistoryScreen(DEFAULT_SCREEN_COLS, DEFAULT_SCREEN_ROWS,
                                                         history=self.history_lines))
        with self._registry_lock:
            self._sessions[name] = entry
        entry.reader_thread = threading.Thread(target=self._reader_loop, args=(entry,), daemon=True)
        entry.reader_thread.start()
        if not show_on_desktop:
            return False, None
        return self._attach_desktop_viewer(entry)

    def _spawn_headless(self, argv: list[str], cwd: str) -> PtyProcessLike:
        try:
            return self._process_factory(argv, cwd)
        except Exception as exc:  # noqa: BLE001 -- any spawn failure is a real backend error
            raise TmuxError(f"failed to spawn session in {cwd!r}: {exc}") from exc

    def _attach_desktop_viewer(self, entry: "_WindowsSession") -> tuple[bool, str | None]:
        """Spawns a real, visible desktop viewer for an ALREADY-running
        session's entry -- used both right after creation (show_on_
        desktop=True) and retroactively (`show_on_desktop()` below). The
        session's own entry.proc is never touched or re-spawned here."""
        from . import windows_visible_console
        ok, reason = windows_visible_console.is_available()
        if not ok:
            entry.visible = False
            entry.visible_reason = reason
            return False, reason
        try:
            viewer = windows_visible_console.spawn_desktop_viewer(self, entry.name, entry.cwd)
        except windows_visible_console.VisibleConsoleSpawnError as exc:
            reason = f"viewer spawn failed: {exc}"
            entry.visible = False
            entry.visible_reason = reason
            return False, reason
        with entry.lock:
            entry.desktop_viewer = viewer
        entry.visible = True
        entry.visible_reason = None
        entry.desktop_session_id = windows_visible_console.desktop_session_id()
        return True, None

    def show_on_desktop(self, name: str) -> tuple[bool, str | None]:
        """Retroactive "Show on desktop" action (task item 5: a session
        created headless, or whose viewer window was since closed, can
        be shown again) -- attaches a NEW viewer to the SAME already-
        running entry.proc; never spawns a second shell. A no-op success
        if a viewer is already alive and attached."""
        entry = self._require(name)
        with entry.lock:
            existing = entry.desktop_viewer
        if existing is not None and existing.isalive():
            return True, None
        return self._attach_desktop_viewer(entry)

    def desktop_capability(self) -> dict:
        """Coarse, session-independent capability probe (task item 4's
        "No interactive desktop" reporting, and show_on_desktop()'s own
        pre-check) -- reused by both the create-session path above and a
        dashboard status query, never a second, possibly-divergent copy
        of this decision."""
        from . import windows_visible_console
        ok, reason = windows_visible_console.is_available()
        return {"available": ok, "reason": reason, "desktop_session_id": windows_visible_console.desktop_session_id()}

    def get_desktop_metadata(self, name: str) -> dict:
        """Per-session visibility metadata (task item 3: `visible_window`/
        `desktop_session_id`/pid) -- read-only, never mutates anything
        except entry.visible itself, kept honest against the viewer's
        OWN currently-alive state (never stuck on the create-time answer:
        a viewer window the user closed flips this back to False on the
        very next read, exactly reflecting "hidden now, Show on desktop
        again if you want it back"). SESSION_NOT_FOUND-shaped (empty
        dict) for an unknown name rather than raising, since dashboard
        listing calls this opportunistically for every row and must
        never let one missing/racing session break the whole listing."""
        with self._registry_lock:
            entry = self._sessions.get(name)
        if entry is None:
            return {}
        with entry.lock:
            viewer = entry.desktop_viewer
        visible_now = bool(viewer is not None and viewer.isalive())
        entry.visible = visible_now
        return {"visible_window": visible_now, "desktop_session_id": entry.desktop_session_id,
                "pid": entry.proc.pid, "visible_reason": entry.visible_reason}

    def detach_session(self, name: str) -> None:
        entry = self._require(name)
        # No native "kick the attached client" operation the way tmux
        # has one -- signals every currently-registered viewer (the
        # WebSocket bridge, windows_webterm.py) to close its own
        # connection. Never touches entry.proc.
        with entry.lock:
            callbacks = list(entry.detach_callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001 -- one misbehaving viewer callback must not block the rest
                pass

    def kill_session(self, name: str) -> None:
        with self._registry_lock:
            entry = self._sessions.pop(name, None)
        if entry is None:
            raise TmuxError(f"session {name!r} does not exist")
        entry.stop_reading.set()
        with entry.lock:
            viewer = entry.desktop_viewer
        if viewer is not None:
            # The session itself is going away -- the viewer window (if
            # any is still open) has nothing left to view, so this is the
            # one place a viewer window IS force-closed. Never the other
            # way around (closing the viewer window on its own never
            # reaches here, never touches entry.proc below).
            try:
                viewer.stop()
            except Exception:  # noqa: BLE001 -- best-effort cleanup only
                pass
        _kill_process_tree(entry.proc)

    def exit_copy_mode(self, session: str) -> None:
        # No copy-mode concept on this backend at all (pane_in_mode is
        # always False -- see get_session above), so core.py's own guard
        # never actually routes a real call here; kept as a safe no-op
        # purely to satisfy the SessionBackend Protocol's shape.
        self._require(session)

    # -- Attach/detach bookkeeping for windows_webterm.py's WS bridge -----

    def register_viewer(self, name: str, on_detach: Callable[[], None]) -> "queue.Queue[str]":
        """Returns a fresh, per-viewer Queue the reader thread pushes
        every new raw chunk into from this point on (never past history
        -- see windows_webterm.py's own use of this for the live
        WebSocket feed; capture_lines/tail is the separate, poll-based
        way to see accumulated history)."""
        entry = self._require(name)
        viewer_queue: "queue.Queue[str]" = queue.Queue()
        with entry.lock:
            entry.attached_viewers += 1
            entry.detach_callbacks.append(on_detach)
            entry.viewer_queues.append(viewer_queue)
        return viewer_queue

    def unregister_viewer(self, name: str, on_detach: Callable[[], None],
                          viewer_queue: "queue.Queue[str] | None" = None) -> None:
        entry = self._sessions.get(name)
        if entry is None:
            return
        with entry.lock:
            entry.attached_viewers = max(0, entry.attached_viewers - 1)
            if on_detach in entry.detach_callbacks:
                entry.detach_callbacks.remove(on_detach)
            if viewer_queue is not None and viewer_queue in entry.viewer_queues:
                entry.viewer_queues.remove(viewer_queue)

    def write_raw(self, name: str, data: str) -> None:
        """Used by windows_webterm.py for live keystroke passthrough from
        an attached, input-authorized WebSocket viewer -- bypasses the
        Enter-settle delay send_text applies (a live interactive terminal
        should feel immediate), but is otherwise the exact same
        entry.proc.write call."""
        entry = self._require(name)
        entry.proc.write(data)
        entry.activity_epoch = int(time.time())

    def resize(self, name: str, rows: int, cols: int) -> None:
        """The WEB terminal's own resize path (windows_webterm.py's
        WindowsTerminalViewer.resize, driven by the browser's xterm.js
        fitting to its own window size) -- real bug found live (P0
        hotfix: garbled/overlapping Windows terminal rendering): with
        NO resize-ownership concept at all, a browser tab's own resize
        could silently change the SAME ConPTY's dimensions a physical
        desktop viewer was ALSO currently rendering, out from under it --
        a full-screen TUI (Claude Code's own Ink renderer) computes every
        redraw against ITS OWN believed column/row count, so a mismatch
        between that and the console it's actually being displayed in
        produces exactly this kind of corrupted overlapping text. While a
        desktop viewer is attached and alive, the physical console is the
        size authority -- a web resize request while one is attached is a
        deliberate, silent no-op (never an error to the browser; its own
        xterm.js still renders at ITS size regardless, just letterboxed/
        cropped relative to what the real PTY is actually producing) --
        see resize_from_desktop_viewer below for the one path that DOES
        get to set the authoritative size."""
        entry = self._require(name)
        with entry.lock:
            viewer = entry.desktop_viewer
        if viewer is not None and viewer.isalive():
            return
        self._apply_resize(entry, rows, cols)

    def resize_from_desktop_viewer(self, name: str, rows: int, cols: int) -> None:
        """Called exactly once, right when a desktop viewer's relay
        process attaches (DesktopBridgeServer._handle_connection,
        windows_visible_console.py) -- syncs the ConPTY's own dimensions
        to the REAL physical console's actual size, the fix for the
        dimension-mismatch root cause above. Bypasses resize()'s own
        guard entirely (that guard exists specifically to protect an
        ALREADY-attached desktop viewer from a web resize -- it must
        never also block the desktop viewer's own initial sync)."""
        entry = self._require(name)
        self._apply_resize(entry, rows, cols)

    def _apply_resize(self, entry: "_WindowsSession", rows: int, cols: int) -> None:
        with entry.lock:
            if entry.last_resize_dims == (rows, cols):
                return  # idempotent no-op -- never a redundant syscall for a size that hasn't actually changed
            entry.last_resize_dims = (rows, cols)
            # Keeps the canonical screen-state's own dimensions synced to
            # the real ConPTY size -- exactly the same dimension-mismatch
            # bug this project's earlier resize-sync fix addressed for the
            # visible desktop console, applied here too: a full-screen
            # TUI's own redraws (cursor positioning, erase regions) are
            # only correctly resolved if this emulator believes the same
            # column/row count the real console does. pyte.Screen.resize
            # itself handles reflow/history push safely.
            entry.screen.resize(rows, cols)
        try:
            entry.proc.setwinsize(rows, cols)
        except Exception:  # noqa: BLE001 -- a resize failure is never fatal to the session
            pass

    # -- internals ----------------------------------------------------------

    def _require(self, name: str) -> _WindowsSession:
        with self._registry_lock:
            entry = self._sessions.get(name)
        if entry is None:
            raise TmuxError(f"session {name!r} does not exist")
        return entry

    def _ensure_reader_alive(self, entry: _WindowsSession) -> None:
        """P0 HOTFIX (task: "P0 WINDOWS SESSION STATE-LOSS / STALE
        STREAM", items 3/6/7): reader_thread was started exactly once,
        at create_session time, and never otherwise supervised -- if
        entry.proc.read() ever raised (a transient ConPTY hiccup, not
        necessarily the underlying process dying), _reader_loop's own
        top-level `except: break` quietly ends the thread forever,
        freezing this session's activity_epoch/buffer/live-viewer feed
        even though the real process is still alive and producing
        output nobody is draining anymore -- exactly the silent
        STREAM_STALLED failure mode this task exists to catch and
        self-heal. Called from get_session() (so a single status poll
        both detects AND repairs it, no separate watchdog loop needed),
        ONLY while the underlying process is confirmed still alive -- a
        session whose process genuinely exited needs no reader at all;
        restarting one against a dead proc.read() would just fail
        identically on its very first iteration.

        Reconnects to the SAME already-open entry.proc handle -- never
        respawns the process, never touches or clears the accumulated
        buffer, only ever loses whatever output arrived during the gap
        between the old thread dying and this restart (unavoidable; the
        alternative -- never restarting -- loses everything from that
        point on forever, which is the real bug this fixes)."""
        if entry.reader_thread is not None and entry.reader_thread.is_alive():
            return
        with entry.lock:
            if entry.reader_thread is not None and entry.reader_thread.is_alive():
                return  # lost the race with another caller already restarting it
            entry.reader_restarts += 1
            entry.last_reader_restart_epoch = int(time.time())
            entry.stop_reading.clear()
            entry.reader_thread = threading.Thread(target=self._reader_loop, args=(entry,), daemon=True)
            entry.reader_thread.start()

    def _reader_loop(self, entry: _WindowsSession) -> None:
        while not entry.stop_reading.is_set():
            try:
                chunk = entry.proc.read(READ_CHUNK_SIZE)
            except Exception:  # noqa: BLE001 -- pty read failing means the process is gone/broken
                break
            if not chunk:
                # An empty read is ambiguous by itself: it means EITHER
                # "the process actually exited" OR "nothing to read
                # within this read()'s own poll timeout, try again" --
                # a real, live-process-still-idling read (e.g. a shell
                # sitting at its prompt between two commands) returns
                # empty just as often as a genuine EOF does. Only a
                # confirmed-dead process ends this loop; a live one that
                # just has nothing to say right now keeps being polled.
                # Getting this backwards (treating every empty read as
                # EOF) was a real bug caught by this backend's own tests:
                # a session's live output stream would go permanently
                # silent after its first idle gap, well before the
                # process itself ever exited.
                if not _is_alive(entry.proc):
                    break
                continue
            entry.activity_epoch = int(time.time())
            with entry.lock:
                _append_chunk(entry, chunk)
                # Canonical terminal-screen state -- fed the exact same
                # raw chunk, completely independent of _append_chunk's
                # own line buffer above (that one still backs Session
                # Knowledge Store's read_new_output, untouched by this
                # fix). pyte.Stream.feed itself never raises on malformed
                # input (unknown/invalid sequences are simply ignored,
                # same tolerance strip_ansi/redaction.py already has for
                # this project's other sanitizers) -- still wrapped
                # defensively since a third-party parser must never be
                # able to kill this reader thread over a byte sequence it
                # doesn't understand.
                try:
                    entry.vt_stream.feed(chunk)
                except Exception:  # noqa: BLE001 -- see comment above
                    pass
                viewer_queues = list(entry.viewer_queues)
            for viewer_queue in viewer_queues:
                viewer_queue.put(chunk)
        # Process gone/reader stopped -- wake any still-attached viewers
        # with a sentinel (empty string) so their own read() loop notices
        # EOF instead of blocking forever on an empty queue.
        with entry.lock:
            viewer_queues = list(entry.viewer_queues)
        for viewer_queue in viewer_queues:
            viewer_queue.put("")

    def snapshot_for_test(self, name: str) -> list[str]:
        """Test-only convenience -- not part of SessionBackend."""
        entry = self._require(name)
        with entry.lock:
            return list(entry.buffer)


def _is_alive(proc: PtyProcessLike) -> bool:
    try:
        return bool(proc.isalive())
    except Exception:  # noqa: BLE001 -- treat an unqueryable process as dead, never crash a caller over it
        return False


def _append_chunk(entry: _WindowsSession, chunk: str) -> None:
    """Splits a raw read chunk into complete lines, buffering an
    incomplete trailing line until the next read completes it -- mirrors
    how a real terminal only ever shows a finished line in scrollback,
    keeping `capture_lines` output stable/comparable across polls (the
    same property tmux's own capture-pane already gives every existing
    caller, including the reliable-submission verification poll loop in
    core.py, which diffs successive captures).

    P0 HOTFIX ("\r overwrite fix for last_output" -- task: "P0 WINDOWS
    SESSION STATE-LOSS / STALE STREAM", the garbled/overlapping-text
    finding deliberately deferred out of that task's own item 10, "do
    not work on renderer cosmetics"). REAL BUG this fixes: the OLD
    version only ever split on '\\n' and .rstrip("\\r") a finished
    line's own TRAILING carriage returns -- a '\\r' in the MIDDLE of a
    line (used by any in-place redraw: a spinner, a live-updating
    status/progress line, exactly what Claude Code's own Ink renderer
    emits constantly) was kept as an ordinary character and every
    successive redraw got naively CONCATENATED onto the last one
    instead of overwriting it. Confirmed live against the real dell-
    5530 'window' session: last_output showed the same spinner verb
    ("Newspapering…") repeated dozens of times back-to-back, and whole
    multi-line code-diff blocks appearing twice in a row.

    Interprets exactly the two control characters real terminal '\r'
    overwrite semantics need -- '\\r' resets the write cursor to the
    START of the current (still in-progress) line, so subsequent
    characters overwrite what was already there (extending the line
    only once the cursor reaches its current end) rather than being
    appended after it; '\\n' commits the current line to `buffer` and
    starts a genuinely new one. Deliberately does NOT interpret ANSI
    cursor-positioning/clear-line/clear-screen/alternate-screen escape
    sequences (moving the cursor to an arbitrary row, or up/down
    between lines) -- those still pass through as literal bytes (later
    stripped for display by strip_ansi, same as before this fix) and a
    full-screen redraw spanning multiple lines can still show as
    repeated/interleaved content. That is a real, separate, larger gap
    (a genuine terminal-state emulator, not a one-line patch) --
    explicitly out of scope here, same as the task's own instruction
    that scoped this fix to '\r' specifically."""
    line = list(entry._partial_line)
    col = entry._partial_line_col
    committed = 0
    for ch in chunk:
        if ch == "\n":
            entry.buffer.append("".join(line))
            committed += 1
            line = []
            col = 0
        elif ch == "\r":
            col = 0
        else:
            if col < len(line):
                line[col] = ch
            else:
                line.append(ch)
            col += 1
    entry._partial_line = "".join(line)
    entry._partial_line_col = col
    entry.total_lines_seen += committed
