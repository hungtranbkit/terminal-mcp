"""Windows-only: a REAL, visible desktop window that VIEWS an existing
WindowsSessionBackend session -- never a second shell, never the session's
own owning process.

Architecture (rewritten -- see the final report for why the previous
approach was replaced): the session's real, persistent process is ALWAYS
the normal headless ConPTY child windows_backend.py already spawns for
every session (`_spawn_headless`) -- the one guarantee this whole backend
is built around ("disconnect/close a viewer never kills the process").
What `spawn_desktop_viewer` below adds is a thin, fully disposable VIEWER:

  1. `DesktopBridgeServer` opens a loopback-only TCP socket and, on each
     connection, attaches windows_webterm.py's own `WindowsTerminalViewer`
     -- the EXACT SAME attach/detach primitive the web terminal's
     WebSocket already uses (register_viewer/unregister_viewer/write_raw),
     never a second, parallel way of reading/writing the session.
  2. A brand-new `CREATE_NEW_CONSOLE` process (a real, visible OS window,
     for the reason documented below) runs a small relay script that
     connects to that socket and pumps bytes between it and its own
     console's stdin/stdout -- nothing more. It has no idea what shell is
     actually running; it is a dumb terminal, the Windows-desktop
     equivalent of `tmux attach`.

Closing that window (or killing the relay process any other way) only
drops the loopback socket -- windows_backend.py's `_handle_connection`
unregisters that one viewer and returns, exactly like a browser tab
closing the web terminal's WebSocket. The session's own `entry.proc`
(the ConPTY child) is never touched, never even referenced by the viewer
process's own code. This is the fix for the real, verified-live bug the
PREVIOUS design had: giving the session's own shell a real console via
CREATE_NEW_CONSOLE meant Windows destroyed that console -- and force-
killed every process still attached to it, including the shell -- the
moment its window closed (CTRL_CLOSE_EVENT), with no way for an unmodified
powershell.exe to opt out. A session can now be shown, hidden (viewer
window closed), and shown again (`show_on_desktop` below) any number of
times with the exact same underlying process throughout.

Real, load-bearing precondition (verified live against dell-5530, see the
final report): a viewer window only appears on a physical/RDP desktop a
user is actually looking at if the SPAWNING process (this node-agent)
itself runs in that user's own interactive session -- Session 0 (a
background service/scheduled task not configured to run interactively)
has no desktop a window could appear on at all, by Windows' own session-
isolation design. `is_available()` reports the concrete facts it can
verify (this process's own session id vs the host's active console
session id) rather than ever assuming success.
"""
from __future__ import annotations

import secrets
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .windows_backend import WindowsSessionBackend

BRIDGE_HOST = "127.0.0.1"  # loopback only -- never reachable off this machine
ACCEPT_POLL_SECONDS = 1.0
TOKEN_READ_TIMEOUT_SECONDS = 5.0


def is_available() -> tuple[bool, str | None]:
    """Best-effort, import-time-safe capability probe -- never raises,
    always returns a concrete (ok, reason) pair rather than assuming.
    Reused by windows_backend.py to decide whether `show_on_desktop=True`
    (or the retroactive `show_on_desktop()` action) can even be attempted."""
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
    if it cannot be determined. Purely informational (dashboard metadata),
    never used to gate anything by itself (`is_available()` is the real
    gate)."""
    try:
        import win32ts  # type: ignore[import-not-found]
        return win32ts.WTSGetActiveConsoleSessionId()
    except Exception:  # noqa: BLE001
        return None


class VisibleConsoleSpawnError(RuntimeError):
    """Raised by spawn_desktop_viewer() -- windows_backend.py wraps this
    into the same honest "fell back / stayed headless" reporting every
    other spawn failure already gets, never a new exception type callers
    need to special-case."""


def _build_commandline(argv: list[str]) -> str:
    # win32process.CreateProcess takes ONE already-quoted command line
    # string (the raw Win32 CreateProcess API shape) -- quote each
    # argument exactly like Python's own subprocess module does on
    # Windows (list2cmdline), so this never needs its own, possibly-
    # divergent quoting rules. Still never shell=True-equivalent: no
    # cmd.exe/powershell -Command string interpolation, no argv element
    # is ever anything other than an opaque, individually-quoted token.
    return subprocess.list2cmdline(argv)


def _read_line_locked(conn: socket.socket, *, max_bytes: int = 256) -> str | None:
    """One newline-terminated line, byte-by-byte (never a bulk recv that
    could swallow the START of the next protocol field/the actual pumped
    stream) -- shared by the token line and the size line in the same
    handshake. None on a closed connection before a newline ever arrives."""
    line = b""
    while not line.endswith(b"\n") and len(line) < max_bytes:
        chunk = conn.recv(1)
        if not chunk:
            return None
        line += chunk
    return line.decode("utf-8", errors="replace").strip()


def _parse_size(size_line: str) -> tuple[int | None, int | None]:
    """"cols,rows" -> (cols, rows), or (None, None) for anything that
    isn't exactly that shape -- a malformed/missing size line just means
    the initial sync is skipped (session stays at whatever size it
    already had), never a crash or a guessed fallback size."""
    parts = size_line.split(",")
    if len(parts) != 2:
        return None, None
    try:
        cols, rows = int(parts[0]), int(parts[1])
    except ValueError:
        return None, None
    if cols <= 0 or rows <= 0:
        return None, None
    return cols, rows


class DesktopBridgeServer:
    """A loopback-only TCP server bridging ONE viewer connection at a
    time to windows_backend.py's existing register_viewer/write_raw
    primitives (the exact same ones windows_webterm.py's WebSocket viewer
    uses) -- never a second way of reading/writing a session's bytes.
    Bound to an ephemeral port (`self.port`) and gated by a random,
    per-instance token the relay process must present first, so nothing
    else on this loopback-only surface can attach to a live session
    uninvited."""

    def __init__(self, backend: "WindowsSessionBackend", session_name: str) -> None:
        self.backend = backend
        self.session_name = session_name
        self.token = secrets.token_hex(16)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((BRIDGE_HOST, 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        self._sock.settimeout(ACCEPT_POLL_SECONDS)
        while not self._stopped.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle_connection(conn)

    def _handle_connection(self, conn: socket.socket) -> None:
        # Handshake: the relay's first line must be this server's own
        # token, read byte-by-byte with a bounded timeout -- a stray/
        # unauthorized local connection is dropped before it ever touches
        # the session's own viewer registration. Second line (P0 hotfix:
        # garbled Windows terminal rendering): the relay's own REAL
        # console dimensions ("cols,rows") -- see this module's own
        # relay-script docstring for why this is the actual fix for the
        # root cause (a ConPTY spawned at pywinpty's own default size,
        # completely disconnected from whatever size the real console
        # the human is looking at happens to be, guarantees a full-screen
        # TUI's own cursor-position math is wrong for the canvas it's
        # actually rendered on).
        conn.settimeout(TOKEN_READ_TIMEOUT_SECONDS)
        try:
            token_line = _read_line_locked(conn)
            if token_line is None or token_line.strip() != self.token:
                conn.close()
                return
            size_line = _read_line_locked(conn) or ""
        except OSError:
            conn.close()
            return
        conn.settimeout(None)

        from .windows_webterm import WindowsTerminalViewer
        viewer = WindowsTerminalViewer(self.backend, self.session_name, readonly=False)
        cols, rows = _parse_size(size_line)
        if cols and rows:
            try:
                self.backend.resize_from_desktop_viewer(self.session_name, rows, cols)
            except Exception:  # noqa: BLE001 -- an initial size sync failure must not block the viewer from attaching at all
                pass

        def _pump_session_to_socket() -> None:
            while True:
                chunk = viewer.read(timeout=1.0)
                if chunk is None:
                    continue
                if chunk == b"":
                    break
                try:
                    conn.sendall(chunk)
                except OSError:
                    break

        reader = threading.Thread(target=_pump_session_to_socket, daemon=True)
        reader.start()
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                viewer.write(data)
        except OSError:
            pass
        finally:
            # Only ever detaches THIS viewer (register_viewer/
            # unregister_viewer) -- entry.proc, the session's real
            # process, is never referenced here at all.
            viewer.close()
            try:
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stopped.set()
        try:
            self._sock.close()
        except OSError:
            pass


class DesktopViewerHandle:
    """Returned by spawn_desktop_viewer() -- windows_backend.py stores
    this on the session's own entry so get_desktop_metadata() can report
    whether a viewer window is CURRENTLY alive (not just "was requested
    once"), and kill_session() can clean up the bridge/relay process
    without ever touching the session's own proc."""

    def __init__(self, *, bridge: DesktopBridgeServer, process_handle, pid: int) -> None:
        self.bridge = bridge
        self.pid = pid
        self._process_handle = process_handle

    def isalive(self) -> bool:
        import win32event
        return win32event.WaitForSingleObject(self._process_handle, 0) != win32event.WAIT_OBJECT_0

    def stop(self) -> None:
        """Used only when the SESSION itself is being killed/reopened --
        tears down the bridge server and force-closes the still-open
        viewer window (if any), since there is nothing left for it to
        view. Closing the viewer window by itself (the normal case) never
        calls this -- that path is handled entirely by the relay
        process's own exit and DesktopBridgeServer noticing the dropped
        connection."""
        self.bridge.stop()
        try:
            import win32api
            if self.isalive():
                win32api.TerminateProcess(self._process_handle, 0)
        except Exception:  # noqa: BLE001 -- best-effort cleanup only
            pass


# The relay itself: deliberately tiny and dependency-light (stdlib only,
# `msvcrt` for raw single-keystroke input -- no line buffering, so Enter/
# Backspace/Ctrl-C reach the real session exactly as typed) -- it does not
# parse or understand terminal output at all, just forwards bytes in both
# directions, the same posture as `ssh`/`tmux attach` towards whatever
# program is actually running. Arrow/function keys (which arrive as a
# 2-byte sequence starting with \x00 or \xe0 from getwch()) are swallowed
# rather than forwarded -- a documented, minimal-scope limitation (no
# raw VT input encoding here), not a crash or a wrong byte sent.
_RELAY_SCRIPT_SOURCE = '''
import socket
import sys
import threading

try:
    import msvcrt
except ImportError:
    msvcrt = None

_cols = _rows = None
try:
    import win32console
    _h = win32console.GetStdHandle(win32console.STD_OUTPUT_HANDLE)
    _h.SetConsoleMode(_h.GetConsoleMode() | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    # The VISIBLE window rect, not the full scrollback buffer's own Size
    # -- a full-screen TUI needs to know how many rows/cols are actually
    # ON SCREEN right now, exactly what a real terminal emulator reports
    # via TIOCGWINSZ/GetConsoleScreenBufferInfo's own Window field.
    _info = _h.GetConsoleScreenBufferInfo()
    _win = _info["Window"]
    _cols = _win.Right - _win.Left + 1
    _rows = _win.Bottom - _win.Top + 1
except Exception:
    pass

host, port, token = sys.argv[1], int(sys.argv[2]), sys.argv[3]
sock = socket.create_connection((host, port), timeout=5)
sock.sendall((token + "\\n").encode("utf-8"))
# Real console size (P0 hotfix: garbled Windows terminal rendering) --
# the ConPTY this viewer is about to display was spawned at some
# unrelated default size; this line lets the server sync it to match
# what's ACTUALLY visible here, once, before any output/input flows.
# Empty values (both query steps above failed) are still sent -- the
# server's own _parse_size treats that as "skip the sync", never a
# guessed fallback.
sock.sendall(f"{_cols or ''},{_rows or ''}\\n".encode("utf-8"))
# create_connection's own `timeout` applies to every socket op after
# connect() too, not just the connect itself -- left at 5s, any quiet gap
# longer than that between server writes (the ordinary case: a shell
# just sitting idle at its prompt) raises socket.timeout on the next
# recv(), which looks exactly like the server having closed the
# connection. Real bug, caught live: the viewer would silently stop
# updating a few seconds after opening, well before anyone closed
# anything. Back to blocking (no timeout) once connected, matching the
# server side's own conn.settimeout(None) after its handshake.
sock.settimeout(None)


def pump_output():
    while True:
        try:
            data = sock.recv(4096)
        except OSError:
            break
        if not data:
            break
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    print("\\n[terminal-mcp: viewer disconnected -- the session itself keeps running]")


threading.Thread(target=pump_output, daemon=True).start()

if msvcrt is None:
    # No raw keystroke API available -- degrade to line-buffered input
    # rather than not accepting input at all.
    while True:
        try:
            line = sys.stdin.readline()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            break
        try:
            sock.sendall(line.encode("utf-8", errors="replace"))
        except OSError:
            break
else:
    while True:
        try:
            ch = msvcrt.getwch()
        except KeyboardInterrupt:
            ch = "\\x03"
        if ch in ("\\x00", "\\xe0"):
            msvcrt.getwch()  # swallow the 2nd byte of an arrow/function key -- not forwarded
            continue
        try:
            sock.sendall(ch.encode("utf-8", errors="replace"))
        except OSError:
            break
'''


def _ensure_relay_script() -> str:
    """Writes the relay script to a stable path once (idempotent -- a
    second call reuses the same file if its content already matches) so
    repeated spawns don't churn temp files."""
    import tempfile
    path = Path(tempfile.gettempdir()) / "terminal_mcp_desktop_viewer_relay.py"
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != _RELAY_SCRIPT_SOURCE:
            path.write_text(_RELAY_SCRIPT_SOURCE, encoding="utf-8")
    except OSError:
        # Fall back to a fresh temp file if the stable path is somehow
        # unwritable (e.g. concurrent access) -- never fail the whole
        # spawn over this.
        fd, tmp_name = tempfile.mkstemp(suffix=".py", prefix="terminal_mcp_desktop_viewer_relay_")
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write(_RELAY_SCRIPT_SOURCE)
        return tmp_name
    return str(path)


def spawn_desktop_viewer(backend: "WindowsSessionBackend", session_name: str, cwd: str) -> DesktopViewerHandle:
    """Starts the loopback bridge + a real, visible CREATE_NEW_CONSOLE
    relay process attached to it. The session's own process is never
    touched or referenced here -- `backend`/`session_name` are only used
    to attach a windows_webterm.py-style viewer (see DesktopBridgeServer
    above)."""
    try:
        import win32con
        import win32process
    except ImportError as exc:
        raise VisibleConsoleSpawnError(f"pywin32 not installed: {exc}") from exc

    bridge = DesktopBridgeServer(backend, session_name)
    try:
        relay_script = _ensure_relay_script()
        argv = [sys.executable, relay_script, BRIDGE_HOST, str(bridge.port), bridge.token]
        commandline = _build_commandline(argv)
        startup_info = win32process.STARTUPINFO()
        try:
            handle, _thread_handle, pid, _tid = win32process.CreateProcess(
                None, commandline, None, None, False, win32con.CREATE_NEW_CONSOLE, None, cwd, startup_info,
            )
        except Exception as exc:  # noqa: BLE001 -- any spawn failure becomes one clear error type
            raise VisibleConsoleSpawnError(f"viewer CreateProcess failed in {cwd!r}: {exc}") from exc
    except Exception:
        bridge.stop()
        raise
    return DesktopViewerHandle(bridge=bridge, process_handle=handle, pid=pid)
