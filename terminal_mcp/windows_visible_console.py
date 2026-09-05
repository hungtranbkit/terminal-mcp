"""Windows-only: a real, visible OS console window for a session, as an
ALTERNATIVE process backend plugged into the EXISTING WindowsSessionBackend
(windows_backend.py) -- not a parallel implementation. `VisibleConsoleProcess`
implements the exact same `PtyProcessLike` Protocol windows_backend.py
already defines for a ConPTY-backed (winpty.PtyProcess) session, so every
other piece of session management there (the reader thread, the bounded
history buffer, viewer queues, kill_session, resize, capture_lines) runs
completely unmodified for a visible-console session too -- the only thing
that differs is HOW bytes get in and out of the underlying process.

Why this exists (task: "user nhìn tại máy Windows cũng thấy đúng terminal
session đang chạy, đồng thời dashboard vẫn attach/stream cùng session
đó"): ConPTY (windows_backend.py's normal path) is architecturally
headless BY DESIGN -- CreatePseudoConsole never has an associated window,
on any Windows version, regardless of which session the calling process
runs in (this is not a hidden flag anyone can flip; it is what ConPTY
IS -- a byte-pipe interface meant for a terminal FRONT END like Windows
Terminal to render, never a window of its own). The only way to get a
REAL, native, visible window for a process is to let it have its own
real Windows console (CREATE_NEW_CONSOLE) instead of a ConPTY -- and for
this backend's own read()/write() to be the exact same process's real
console content (never a second, mirrored process), this class talks to
THAT SAME console via the Win32 Console API (AttachConsole + ReadConsole
Output + WriteConsoleInput), the way a handful of legitimate legacy
remote-console tools already do, instead of a pipe.

Real, load-bearing precondition (verified live, see docs/multi-node.md
and the final report this backs): a process only gets a window B
attached user actually sees if the SPAWNING process (this node-agent)
itself is running in that user's own interactive desktop session --
Session 0 (a background service/scheduled task not configured to run
interactively) has no desktop a window could appear on at all, by
Windows' own session-isolation design. `spawn()` below reports the
concrete facts it can verify (its own current session id vs the host's
active console session id) rather than ever assuming success.

AttachConsole/FreeConsole affect this ENTIRE PROCESS's console
association, not just one thread -- `_console_lock` below serializes
every attach/read-or-write/detach cycle across every visible-console
session this one node-agent process manages, so two concurrent
dashboard polls against two different visible sessions can never
interleave and read/write the wrong one's buffer.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

_console_lock = threading.Lock()

# A console screen buffer is a fixed-size grid (commonly 30 rows tall by
# default) -- most rows below the real, printed content are blank
# padding, exactly like tmux capture-pane's own trailing-blank-line
# behavior (tmux.py's own capture_lines docstring documents the
# identical concern for a real tmux pane). Stripped before ever being
# treated as "new output" to emit.
_TRAILING_BLANK_RE = re.compile(r"[ \t]+$")


def is_available() -> tuple[bool, str | None]:
    """Best-effort, import-time-safe capability probe -- never raises,
    always returns a concrete (ok, reason) pair rather than assuming.
    Reused by windows_backend.py to decide whether `show_on_desktop=True`
    can even be attempted before ever calling spawn()."""
    try:
        import win32console  # noqa: F401
        import win32process  # noqa: F401
    except ImportError as exc:
        return False, f"pywin32 not installed: {exc}"
    try:
        import win32ts  # type: ignore[import-not-found]
        active = win32ts.WTSGetActiveConsoleSessionId()
        mine = _current_session_id()
        if active != mine:
            return False, (f"this node-agent process is running in session {mine}, "
                           f"but the active interactive desktop is session {active} "
                           f"-- a spawned window would not be visible to that desktop")
    except Exception as exc:  # noqa: BLE001 -- a capability probe must never itself crash a caller
        return False, f"could not determine active console session: {exc}"
    return True, None


def _current_session_id() -> int:
    import win32process
    import win32ts  # type: ignore[import-not-found]
    return win32ts.ProcessIdToSessionId(win32process.GetCurrentProcessId())


def desktop_session_id() -> int | None:
    """The Windows session id of the CURRENTLY ACTIVE interactive desktop
    (whoever is physically/RDP logged into the console right now) -- None
    if it cannot be determined. Purely informational (dashboard metadata:
    task's own `desktop_session_id` field), never used to gate anything
    by itself (`is_available()` above is the real gate)."""
    try:
        import win32ts  # type: ignore[import-not-found]
        return win32ts.WTSGetActiveConsoleSessionId()
    except Exception:  # noqa: BLE001
        return None


class VisibleConsoleSpawnError(RuntimeError):
    """Raised by spawn() -- windows_backend.py wraps this into the exact
    same TmuxError every other spawn failure already becomes, never a
    new exception type callers need to special-case."""


def spawn(argv: list[str], cwd: str) -> "VisibleConsoleProcess":
    """Starts `argv` with a real, brand-new, visible OS console
    (CREATE_NEW_CONSOLE) -- lazy pywin32 imports, exactly like windows_
    backend.py's own _default_process_factory is lazy about `winpty`, so
    this module stays importable (and its capability PROBED) on any
    host, including this project's own Linux dev/test environment."""
    try:
        import win32con
        import win32process
    except ImportError as exc:
        raise VisibleConsoleSpawnError(f"pywin32 not installed: {exc}") from exc

    commandline = _build_commandline(argv)
    startup_info = win32process.STARTUPINFO()
    creation_flags = win32con.CREATE_NEW_CONSOLE
    try:
        handle, thread_handle, pid, _tid = win32process.CreateProcess(
            None, commandline, None, None, False, creation_flags, None, cwd, startup_info,
        )
    except Exception as exc:  # noqa: BLE001 -- any spawn failure becomes one clear error type
        raise VisibleConsoleSpawnError(f"CreateProcess failed for {argv!r} in {cwd!r}: {exc}") from exc
    return VisibleConsoleProcess(process_handle=handle, thread_handle=thread_handle, pid=pid)


def _build_commandline(argv: list[str]) -> str:
    # win32process.CreateProcess takes ONE already-quoted command line
    # string (the raw Win32 CreateProcess API shape), unlike winpty.
    # PtyProcess.spawn/subprocess's own argv-list conveniences -- quote
    # each argument exactly like Python's own subprocess module does on
    # Windows (list2cmdline), so this never needs its own, possibly-
    # divergent quoting rules. Still never shell=True-equivalent: no
    # cmd.exe/powershell -Command string interpolation, no argv element
    # is ever anything other than an opaque, individually-quoted token.
    import subprocess
    return subprocess.list2cmdline(argv)


class VisibleConsoleProcess:
    """Satisfies windows_backend.py's own PtyProcessLike Protocol --
    `pid`, `isalive()`, `read()`, `write()`, `setwinsize()`, `terminate()`
    -- backed by a REAL Win32 console (not a ConPTY pipe) via AttachConsole
    + ReadConsoleOutputCharacter/WriteConsoleInput. `read()` is a SNAPSHOT-
    diff, not an incremental stream (a console screen buffer has no
    "give me only the new bytes" primitive the way a ConPTY pipe does) --
    the class keeps its own last-seen-line count and only reports lines
    beyond that, exactly the same tail-diffing shape terminal-mcp already
    relies on elsewhere (tmux capture-pane-based reliable-submission
    verification, core.py) for a system with no native incremental-read
    primitive."""

    def __init__(self, *, process_handle, thread_handle, pid: int) -> None:
        self.pid = pid
        self._process_handle = process_handle
        self._thread_handle = thread_handle
        self._last_snapshot_text = ""

    def isalive(self) -> bool:
        import win32event
        return win32event.WaitForSingleObject(self._process_handle, 0) != win32event.WAIT_OBJECT_0

    def read(self, size: int = 4096) -> str:  # noqa: ARG002 -- size is a ConPTY-pipe concept, unused here
        with _console_lock:
            lines = self._read_snapshot_locked()
        if lines is None:
            return ""  # process gone or transiently unreadable -- caller's isalive() decides what that means
        # Plain string-prefix diff against the last full snapshot -- a
        # console screen buffer has no incremental "only the new bytes"
        # read primitive the way a ConPTY pipe does, so this treats the
        # whole current screen as one string and reports only what's new
        # since last time, INCLUDING a still-growing final line (e.g. a
        # prompt being typed character by character across several
        # polls) -- exactly the same incremental-append behavior windows_
        # backend.py's own _append_chunk/entry._partial_line already
        # assume for every OTHER backend, so this needs no special
        # handling on that side at all.
        new_text = "\n".join(lines)
        previous_text = self._last_snapshot_text
        if new_text == previous_text:
            return ""
        if new_text.startswith(previous_text):
            delta = new_text[len(previous_text):]
        else:
            # Screen changed non-append-only (cleared, scrolled past the
            # visible buffer, a full-screen TUI redraw) -- best-effort
            # fallback: report the whole current screen as freshly seen
            # rather than silently losing it. Same "best-effort
            # approximation, documented honestly" posture this module's
            # pane_current_command already has.
            delta = new_text
        self._last_snapshot_text = new_text
        return delta

    def write(self, data: str) -> int:
        with _console_lock:
            self._write_input_locked(data)
        return len(data)

    def setwinsize(self, rows: int, cols: int) -> None:
        with _console_lock:
            self._set_size_locked(rows, cols)

    def terminate(self, force: bool = False) -> None:  # noqa: ARG002 -- always a hard terminate here
        import win32api
        try:
            win32api.TerminateProcess(self._process_handle, 0)
        except Exception:  # noqa: BLE001 -- process already gone is not an error
            pass

    # -- Win32 console plumbing (always called under _console_lock) ------

    def _attach_locked(self):
        import win32console
        with contextlib_suppress():
            win32console.FreeConsole()
        win32console.AttachConsole(self.pid)

    def _detach_locked(self) -> None:
        import win32console
        with contextlib_suppress():
            win32console.FreeConsole()

    def _read_snapshot_locked(self) -> list[str] | None:
        import win32console
        import win32file
        import pywintypes  # type: ignore[import-not-found]
        try:
            self._attach_locked()
            handle = win32file.CreateFile(
                "CONOUT$", win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE, None,
                win32file.OPEN_EXISTING, 0, None,
            )
            console_out = win32console.PyConsoleScreenBufferType(handle)
            info = console_out.GetConsoleScreenBufferInfo()
            width = info["Size"].X
            cursor_row = info["CursorPosition"].Y
            lines: list[str] = []
            for row in range(cursor_row + 1):
                text = console_out.ReadConsoleOutputCharacter(width, win32console.PyCOORDType(0, row))
                lines.append(_TRAILING_BLANK_RE.sub("", text))
            return lines
        except pywintypes.error:
            return None
        finally:
            self._detach_locked()

    def _write_input_locked(self, data: str) -> None:
        import win32con
        import win32console
        import win32file
        import pywintypes  # type: ignore[import-not-found]
        try:
            self._attach_locked()
            handle = win32file.CreateFile(
                "CONIN$", win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE, None,
                win32file.OPEN_EXISTING, 0, None,
            )
            console_in = win32console.PyConsoleScreenBufferType(handle)
            records = []
            for ch in data:
                # Plain typed characters are recognized correctly by a
                # console's line editor from `Char` alone (verified live
                # against a real powershell.exe session), but Enter is
                # NOT -- PSReadLine (modern PowerShell's line editor)
                # decides "submit the line" from the record's virtual key
                # code, not from the Unicode char, so a Char='\r' with
                # VirtualKeyCode=0 is silently misread as a stray keypress
                # instead of Enter (caught live: it left a garbage
                # character appended to the unexecuted command line
                # rather than submitting it). VK_RETURN makes it a real,
                # recognized Enter regardless of which line editor is
                # reading it.
                is_enter = ch in ("\r", "\n")
                virtual_key_code = win32con.VK_RETURN if is_enter else 0
                for key_down in (True, False):
                    record = win32console.PyINPUT_RECORDType(win32console.KEY_EVENT)
                    record.KeyDown = key_down
                    record.RepeatCount = 1
                    record.Char = ch
                    record.VirtualKeyCode = virtual_key_code
                    record.VirtualScanCode = 0
                    record.ControlKeyState = 0
                    records.append(record)
            console_in.WriteConsoleInput(records)
        except pywintypes.error:
            pass
        finally:
            self._detach_locked()

    def _set_size_locked(self, rows: int, cols: int) -> None:
        import win32console
        import win32file
        import pywintypes  # type: ignore[import-not-found]
        try:
            self._attach_locked()
            handle = win32file.CreateFile(
                "CONOUT$", win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE, None,
                win32file.OPEN_EXISTING, 0, None,
            )
            console_out = win32console.PyConsoleScreenBufferType(handle)
            info = console_out.GetConsoleScreenBufferInfo()
            new_size = win32console.PyCOORDType(max(cols, info["Size"].X), max(rows, info["Size"].Y))
            console_out.SetConsoleScreenBufferSize(new_size)
        except pywintypes.error:
            pass
        finally:
            self._detach_locked()


class contextlib_suppress:
    """Tiny inline `contextlib.suppress(Exception)` -- avoids importing
    the real pywin32 exception type at module scope just to name it in a
    context manager used only for "FreeConsole may legitimately fail if
    nothing was attached" cleanup calls."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None
