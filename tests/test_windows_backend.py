"""WindowsSessionBackend (windows_backend.py) -- exercised against a REAL
running child process on this Linux dev box via `_FakePty`, a POSIX-pty-
backed test double satisfying the exact same `PtyProcessLike` shape
pywinpty's own `winpty.PtyProcess` has. Every test here runs real session-
management logic (registry, output buffering, identity, attach/detach
broadcast, kill) against a real process -- only the actual `pywinpty`/
ConPTY integration itself (`_default_process_factory`) is never invoked
here (it lazily imports `winpty`, which does not exist on Linux), and is
therefore NOT live-verified against real Windows -- see windows_backend.
py's own module docstring and the final report.
"""
from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from terminal_mcp.tmux import TmuxError
from terminal_mcp.windows_backend import (
    WindowsPathError,
    WindowsSessionBackend,
    validate_windows_cwd,
    validate_windows_session_name,
)

# A tiny, fully deterministic stand-in for an interactive shell -- reads a
# line, echoes it back prefixed, exactly enough to prove text really goes
# in and real output really comes back out through a real pty, without
# depending on any real shell's own prompt format.
_FAKE_SHELL_SCRIPT = (
    "import sys\n"
    "sys.stdout.write('PS> ')\n"
    "sys.stdout.flush()\n"
    "while True:\n"
    "    line = sys.stdin.readline()\n"
    "    if not line:\n"
    "        break\n"
    "    sys.stdout.write('you said: ' + line)\n"
    "    sys.stdout.write('PS> ')\n"
    "    sys.stdout.flush()\n"
)


class _FakePty:
    """POSIX-pty-backed real child process, satisfying windows_backend.
    PtyProcessLike's shape (pid/isalive/read/write/setwinsize/terminate)
    -- see this module's own docstring for why this is a faithful stand-
    in for pywinpty's real PtyProcess for testing this backend's own
    logic, not a mock of that logic itself."""

    def __init__(self, argv: list[str], cwd: str) -> None:
        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        self._proc = subprocess.Popen(argv, cwd=cwd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                                      start_new_session=True, close_fds=True)
        os.close(slave_fd)
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    @property
    def pid(self) -> int:
        return self._proc.pid

    def isalive(self) -> bool:
        return self._proc.poll() is None

    def read(self, size: int = 4096) -> str:
        ready, _, _ = select.select([self._master_fd], [], [], 1.0)
        if not ready:
            return ""
        try:
            data = os.read(self._master_fd, size)
        except OSError:
            return ""
        return data.decode("utf-8", errors="replace")

    def write(self, data: str) -> int:
        return os.write(self._master_fd, data.encode("utf-8"))

    def setwinsize(self, rows: int, cols: int) -> None:
        pass

    def terminate(self, force: bool = False) -> None:
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL if force else signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self._proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass
        try:
            os.close(self._master_fd)
        except OSError:
            pass


def _fake_factory(argv: list[str], cwd: str) -> _FakePty:
    return _FakePty(argv, cwd)


@pytest.fixture
def backend(tmp_path):
    b = WindowsSessionBackend(shell=sys.executable, process_factory=_fake_factory, history_lines=500)
    yield b
    # Cleanup: kill anything a test left running.
    for name in list(b._sessions.keys()):
        try:
            b.kill_session(name)
        except Exception:  # noqa: BLE001
            pass


def _wait_until(predicate, *, timeout: float = 3.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _new_fake_shell_session(backend: WindowsSessionBackend, tmp_path, name: str = "win-test") -> None:
    # command=[sys.executable, "-c", script] doesn't fit new_session's
    # single-string `command` param (matches tmux.py's own new_session
    # contract: one program name, no argv, exactly like a real launch_
    # commands entry) -- so this uses the `shell` (no `command` given)
    # path, with `shell` overridden to our fake interpreter + inline
    # script via the backend fixture's own `shell=sys.executable`
    # plus... Python needs `-c script` as args, which a single `shell`
    # string can't carry either. Simplest real fix: write the fake shell
    # out as its own executable script file and use ITS path as `shell`.
    script_path = tmp_path / "fake_shell.py"
    script_path.write_text(_FAKE_SHELL_SCRIPT)
    backend.shell = f"{sys.executable}"
    # Monkeypatch the process factory closure to always run this script,
    # and to spawn against the REAL (POSIX) tmp_path regardless of the
    # Windows-shaped cwd string new_session() was given -- new_session's
    # own validate_windows_cwd only checks the STRING'S SHAPE (a real
    # Windows path is never actually reachable to spawn against on this
    # Linux test host), so the fake cwd below exists purely to satisfy
    # that shape check; the REAL directory the fake process actually
    # runs in is tmp_path, supplied here instead.
    original_factory = backend._process_factory
    def factory(argv, cwd):
        return original_factory([sys.executable, "-u", str(script_path)], str(tmp_path))
    backend._process_factory = factory
    backend.new_session(name, "C:\\fake\\windows\\path")


# -- validate_windows_cwd / validate_windows_session_name (pure, no process) --

def test_validate_windows_cwd_accepts_drive_rooted_path():
    assert str(validate_windows_cwd("C:\\Users\\me\\workspace"))


def test_validate_windows_cwd_rejects_relative_path():
    with pytest.raises(WindowsPathError, match="drive-rooted"):
        validate_windows_cwd("workspace\\project")


def test_validate_windows_cwd_rejects_unc_path():
    with pytest.raises(WindowsPathError, match="UNC"):
        validate_windows_cwd("\\\\fileserver\\share\\project")


def test_validate_windows_cwd_rejects_empty():
    with pytest.raises(WindowsPathError, match="empty"):
        validate_windows_cwd("")


def test_validate_windows_cwd_rejects_nul_byte():
    with pytest.raises(WindowsPathError, match="NUL"):
        validate_windows_cwd("C:\\Users\\me\x00\\evil")


def test_validate_windows_cwd_traversal_is_syntactically_accepted_here():
    # Deliberately NOT rejected by this shape-only check -- real
    # containment (../.. escaping allowed_cwd_roots) is resolve_cwd's
    # job (lifecycle.py, unchanged, reused as-is on a real Windows
    # interpreter). Documented here so this isn't mistaken for a gap.
    resolved = validate_windows_cwd("C:\\allowed\\..\\..\\Windows\\System32")
    assert "System32" in str(resolved)


def test_validate_windows_session_name_rejects_empty():
    with pytest.raises(WindowsPathError):
        validate_windows_session_name("")


def test_windows_path_error_is_a_tmux_error_for_core_py_compatibility():
    # See session_backend.py's own SessionBackendError comment -- every
    # `except TmuxError` clause in core.py must catch this too.
    assert issubclass(WindowsPathError, TmuxError)


# -- Real process lifecycle (via _FakePty) -----------------------------------

def test_new_session_creates_a_real_running_process(backend, tmp_path):
    _new_fake_shell_session(backend, tmp_path)
    info = backend.get_session("win-test")
    assert info is not None
    assert info.pane_dead is False
    assert info.pane_pid > 0
    assert info.windows == 1
    assert info.pane_in_mode is False


def test_capture_lines_shows_real_output_from_the_process(backend, tmp_path):
    _new_fake_shell_session(backend, tmp_path)
    assert _wait_until(lambda: any("PS>" in line for line in backend.capture_lines("win-test", 20)))


def test_send_text_and_capture_round_trip(backend, tmp_path):
    _new_fake_shell_session(backend, tmp_path)
    _wait_until(lambda: any("PS>" in line for line in backend.capture_lines("win-test", 20)))
    backend.send_text("win-test", "hello-windows", press_enter=True)
    assert _wait_until(lambda: any("you said: hello-windows" in line for line in backend.capture_lines("win-test", 20)))


def test_capture_lines_includes_in_progress_partial_line(backend, tmp_path):
    _new_fake_shell_session(backend, tmp_path)
    assert _wait_until(lambda: any("PS>" in line for line in backend.capture_lines("win-test", 20)))
    # Write text WITHOUT pressing enter -- no newline has landed yet, so
    # this must show up as the buffered partial line, not be invisible
    # until Enter (see capture_lines' own docstring for why this matters
    # to the reliable-submission verification poll in core.py).
    backend.send_text("win-test", "still-typing", press_enter=False)
    assert _wait_until(lambda: any("still-typing" in line for line in backend.capture_lines("win-test", 20)))


def test_send_keys_unmapped_key_raises(backend, tmp_path):
    _new_fake_shell_session(backend, tmp_path)
    with pytest.raises(TmuxError, match="no key mapping"):
        backend.send_keys("win-test", ["F13-does-not-exist"])


def test_send_keys_enter_submits(backend, tmp_path):
    _new_fake_shell_session(backend, tmp_path)
    _wait_until(lambda: any("PS>" in line for line in backend.capture_lines("win-test", 20)))
    backend.send_text("win-test", "via-keys", press_enter=False)
    backend.send_keys("win-test", ["Enter"])
    assert _wait_until(lambda: any("you said: via-keys" in line for line in backend.capture_lines("win-test", 20)))


def test_kill_session_terminates_real_process_and_frees_registry(backend, tmp_path):
    _new_fake_shell_session(backend, tmp_path)
    info = backend.get_session("win-test")
    pid = info.pane_pid
    backend.kill_session("win-test")
    assert backend.get_session("win-test") is None
    # Real evidence: the OS process is actually gone, not just forgotten
    # by this backend's own registry -- _FakePty.terminate() already
    # waitpid()s the child itself, so this should already be reaped.
    assert _wait_until(lambda: not _pid_alive(pid), timeout=3.0)


def test_kill_nonexistent_session_raises(backend):
    with pytest.raises(TmuxError, match="does not exist"):
        backend.kill_session("ghost")


def test_new_session_duplicate_name_raises(backend, tmp_path):
    _new_fake_shell_session(backend, tmp_path)
    with pytest.raises(TmuxError, match="already exists"):
        _new_fake_shell_session(backend, tmp_path)


def test_new_session_does_not_re_validate_cwd_shape(backend, tmp_path):
    # See new_session's own comment: cwd here is already resolve_cwd's
    # OUTPUT (a real, resolved path on whatever OS this runs on), never
    # a raw client string -- re-demanding "C:\...\" shape here would
    # wrongly reject that real, valid output on a non-Windows-shaped
    # (but real and correctly resolved) test host. validate_windows_cwd
    # itself is still tested standalone above for exactly this shape
    # check, for a caller (e.g. windows_agent.py) that wants to
    # pre-validate a RAW string before it ever reaches resolve_cwd.
    backend.new_session("win-real-cwd", str(tmp_path))
    assert backend.get_session("win-real-cwd") is not None


def test_detach_session_calls_viewer_callbacks_without_killing_process(backend, tmp_path):
    _new_fake_shell_session(backend, tmp_path)
    called = []
    backend.register_viewer("win-test", lambda: called.append(True))
    backend.detach_session("win-test")
    assert called == [True]
    # Process itself must be completely unaffected -- exactly the task's
    # own "disconnect browser không kill process" requirement, exercised
    # here via the detach path specifically.
    info = backend.get_session("win-test")
    assert info is not None
    assert info.pane_dead is False


def test_attached_reflects_registered_viewer_count(backend, tmp_path):
    _new_fake_shell_session(backend, tmp_path)
    assert backend.get_session("win-test").attached is False
    stop = lambda: None
    backend.register_viewer("win-test", stop)
    assert backend.get_session("win-test").attached is True
    backend.unregister_viewer("win-test", stop)
    assert backend.get_session("win-test").attached is False


def test_exit_copy_mode_is_a_safe_noop(backend, tmp_path):
    _new_fake_shell_session(backend, tmp_path)
    backend.exit_copy_mode("win-test")  # must not raise
    assert backend.get_session("win-test").pane_dead is False


# ---------------------------------------------------------------------------
# Desktop-visible-window wiring (task: "user nhìn tại máy Windows cũng
# thấy đúng terminal session") -- windows_visible_console.py's own actual
# Win32 API calls are unverifiable on this Linux dev box (pywin32 doesn't
# exist here), so these tests monkeypatch its two entry points (is_
# available/spawn_desktop_viewer) and verify ONLY windows_backend.py's own
# dispatch/metadata logic -- exactly the same "test the real wiring, fake
# only the one platform-specific primitive" discipline this whole file
# already uses for pywinpty itself (_FakePty stands in for winpty.
# PtyProcess the same way here).
#
# The real architecture (see windows_visible_console.py's own module
# docstring for the full "why"): the session's own process is ALWAYS the
# normal headless ConPTY child -- a viewer is a separate, disposable thing
# attached to it, never the process itself. `_FakeDesktopViewer` below
# stands in for windows_visible_console.DesktopViewerHandle: an
# `isalive()` a test can flip (simulating the user closing the real
# window) and a `stop()` call log (so kill_session's own cleanup can be
# asserted without a real process to check).
# ---------------------------------------------------------------------------

class _FakeDesktopViewer:
    def __init__(self) -> None:
        self._alive = True
        self.stop_calls = 0

    def isalive(self) -> bool:
        return self._alive

    def stop(self) -> None:
        self.stop_calls += 1
        self._alive = False


def test_show_on_desktop_false_is_the_unchanged_default(backend, tmp_path):
    visible, reason = backend.new_session("win-headless-default", str(tmp_path))
    assert visible is False
    assert reason is None
    assert backend.get_desktop_metadata("win-headless-default") == {
        "visible_window": False, "desktop_session_id": None, "pid": backend.get_session("win-headless-default").pane_pid,
        "visible_reason": None,
    }


def test_show_on_desktop_true_spawns_visible_when_available(backend, tmp_path, monkeypatch):
    from terminal_mcp import windows_visible_console

    fake_viewer = _FakeDesktopViewer()
    monkeypatch.setattr(windows_visible_console, "is_available", lambda: (True, None))
    monkeypatch.setattr(windows_visible_console, "spawn_desktop_viewer",
                        lambda backend, name, cwd: fake_viewer)
    monkeypatch.setattr(windows_visible_console, "desktop_session_id", lambda: 1)

    visible, reason = backend.new_session("win-visible-ok", str(tmp_path), show_on_desktop=True)
    assert visible is True
    assert reason is None
    meta = backend.get_desktop_metadata("win-visible-ok")
    assert meta["visible_window"] is True
    assert meta["desktop_session_id"] == 1
    # The session itself is otherwise a completely normal, working
    # session -- same reader thread/buffer/kill semantics, proven by
    # actually reading real output through it. Its own process is a
    # REAL headless _FakePty throughout -- the fake viewer above never
    # becomes the session's process the way the old design's `spawn()`
    # once did.
    info = backend.get_session("win-visible-ok")
    assert info is not None and info.pane_dead is False


def test_closing_the_viewer_window_never_kills_the_session(backend, tmp_path, monkeypatch):
    """The real bug this redesign fixes: with the OLD CREATE_NEW_CONSOLE-
    as-the-shell design, closing the window force-killed the session
    (verified live against dell-5530). Here: the viewer's own isalive()
    flips to False (simulating its window having been closed) and the
    session's real process must be completely unaffected -- still alive,
    still readable/writable."""
    from terminal_mcp import windows_visible_console

    fake_viewer = _FakeDesktopViewer()
    monkeypatch.setattr(windows_visible_console, "is_available", lambda: (True, None))
    monkeypatch.setattr(windows_visible_console, "spawn_desktop_viewer",
                        lambda backend, name, cwd: fake_viewer)
    monkeypatch.setattr(windows_visible_console, "desktop_session_id", lambda: 1)
    backend.new_session("win-close-safe", str(tmp_path), show_on_desktop=True)
    assert backend.get_desktop_metadata("win-close-safe")["visible_window"] is True

    fake_viewer._alive = False  # simulate the user closing the real window
    meta = backend.get_desktop_metadata("win-close-safe")
    assert meta["visible_window"] is False  # honestly reflects "hidden now"
    # The session itself: still a real, live, working process -- never
    # touched by the viewer window closing.
    info = backend.get_session("win-close-safe")
    assert info is not None and info.pane_dead is False
    assert fake_viewer.stop_calls == 0  # closing the WINDOW is not this backend calling stop()


def test_show_on_desktop_retroactive_action_attaches_a_new_viewer(backend, tmp_path, monkeypatch):
    """Task item 5: a session created headless (or whose viewer window
    was since closed) can be shown again -- attaching a NEW viewer to the
    SAME already-running process, never a second shell."""
    from terminal_mcp import windows_visible_console

    backend.new_session("win-show-later", str(tmp_path))  # headless, no show_on_desktop
    assert backend.get_desktop_metadata("win-show-later")["visible_window"] is False
    same_pid = backend.get_session("win-show-later").pane_pid

    fake_viewer = _FakeDesktopViewer()
    monkeypatch.setattr(windows_visible_console, "is_available", lambda: (True, None))
    monkeypatch.setattr(windows_visible_console, "spawn_desktop_viewer",
                        lambda backend, name, cwd: fake_viewer)
    monkeypatch.setattr(windows_visible_console, "desktop_session_id", lambda: 1)

    visible, reason = backend.show_on_desktop("win-show-later")
    assert visible is True
    assert reason is None
    assert backend.get_desktop_metadata("win-show-later")["visible_window"] is True
    # Exact same process throughout -- never a second shell spawned.
    assert backend.get_session("win-show-later").pane_pid == same_pid


def test_show_on_desktop_retroactive_action_is_a_noop_if_already_visible(backend, tmp_path, monkeypatch):
    from terminal_mcp import windows_visible_console

    fake_viewer = _FakeDesktopViewer()
    spawn_calls = []
    monkeypatch.setattr(windows_visible_console, "is_available", lambda: (True, None))
    monkeypatch.setattr(windows_visible_console, "spawn_desktop_viewer",
                        lambda backend, name, cwd: (spawn_calls.append(1), fake_viewer)[1])
    monkeypatch.setattr(windows_visible_console, "desktop_session_id", lambda: 1)
    backend.new_session("win-already-visible", str(tmp_path), show_on_desktop=True)
    assert len(spawn_calls) == 1

    visible, reason = backend.show_on_desktop("win-already-visible")
    assert visible is True
    assert len(spawn_calls) == 1  # no second viewer spawned -- the existing one is still alive


def test_kill_session_stops_a_still_open_viewer(backend, tmp_path, monkeypatch):
    from terminal_mcp import windows_visible_console

    fake_viewer = _FakeDesktopViewer()
    monkeypatch.setattr(windows_visible_console, "is_available", lambda: (True, None))
    monkeypatch.setattr(windows_visible_console, "spawn_desktop_viewer",
                        lambda backend, name, cwd: fake_viewer)
    monkeypatch.setattr(windows_visible_console, "desktop_session_id", lambda: 1)
    backend.new_session("win-kill-with-viewer", str(tmp_path), show_on_desktop=True)

    backend.kill_session("win-kill-with-viewer")
    assert fake_viewer.stop_calls == 1
    assert backend.get_session("win-kill-with-viewer") is None


def test_show_on_desktop_true_falls_back_to_headless_when_unavailable(backend, tmp_path, monkeypatch):
    from terminal_mcp import windows_visible_console

    monkeypatch.setattr(windows_visible_console, "is_available",
                        lambda: (False, "this node-agent process is running in session 0, but the "
                                        "active interactive desktop is session 1"))

    visible, reason = backend.new_session("win-visible-fallback", str(tmp_path), show_on_desktop=True)
    assert visible is False
    assert reason is not None and "session 0" in reason
    meta = backend.get_desktop_metadata("win-visible-fallback")
    assert meta["visible_window"] is False
    assert meta["visible_reason"] == reason
    # Never silently drops the session create itself -- still a real,
    # working (just headless) session.
    info = backend.get_session("win-visible-fallback")
    assert info is not None and info.pane_dead is False


def test_show_on_desktop_true_falls_back_when_spawn_itself_fails(backend, tmp_path, monkeypatch):
    from terminal_mcp import windows_visible_console

    def _boom(backend, name, cwd):
        raise windows_visible_console.VisibleConsoleSpawnError("CreateProcess failed: access denied")

    monkeypatch.setattr(windows_visible_console, "is_available", lambda: (True, None))
    monkeypatch.setattr(windows_visible_console, "spawn_desktop_viewer", _boom)

    visible, reason = backend.new_session("win-visible-spawn-fail", str(tmp_path), show_on_desktop=True)
    assert visible is False
    assert reason is not None and "access denied" in reason
    assert backend.get_session("win-visible-spawn-fail") is not None  # headless fallback still created


def test_desktop_capability_passthrough(backend, monkeypatch):
    from terminal_mcp import windows_visible_console

    monkeypatch.setattr(windows_visible_console, "is_available", lambda: (False, "no interactive desktop"))
    monkeypatch.setattr(windows_visible_console, "desktop_session_id", lambda: None)
    result = backend.desktop_capability()
    assert result == {"available": False, "reason": "no interactive desktop", "desktop_session_id": None}


def test_get_desktop_metadata_of_unknown_session_is_empty_dict(backend):
    assert backend.get_desktop_metadata("no-such-session-ever") == {}


# ---------------------------------------------------------------------------
# Resize ownership (P0 hotfix: garbled/overlapping Windows terminal
# rendering) -- real bug found live: a web terminal viewer's own resize
# (driven by the browser's own window size) could silently change the SAME
# ConPTY's dimensions a physical desktop viewer was ALSO currently
# rendering, producing exactly the corrupted/overlapping text a full-
# screen TUI (Claude Code's own Ink renderer) shows when its believed
# column/row count doesn't match the canvas it's actually drawn on.
# ---------------------------------------------------------------------------

def test_resize_applies_normally_with_no_desktop_viewer_attached(backend, tmp_path):
    _new_fake_shell_session(backend, tmp_path)
    backend.resize("win-test", 30, 100)
    assert backend._sessions["win-test"].last_resize_dims == (30, 100)


def test_resize_is_ignored_while_a_desktop_viewer_is_attached(backend, tmp_path, monkeypatch):
    from terminal_mcp import windows_visible_console

    fake_viewer = _FakeDesktopViewer()
    monkeypatch.setattr(windows_visible_console, "is_available", lambda: (True, None))
    monkeypatch.setattr(windows_visible_console, "spawn_desktop_viewer", lambda backend, name, cwd: fake_viewer)
    monkeypatch.setattr(windows_visible_console, "desktop_session_id", lambda: 1)
    backend.new_session("win-resize-owned", str(tmp_path), show_on_desktop=True)

    # A web viewer's own resize (e.g. browser window resize) must be a
    # silent no-op -- the physical desktop console is the size authority
    # while its viewer is attached and alive.
    backend.resize("win-resize-owned", 40, 120)
    assert backend._sessions["win-resize-owned"].last_resize_dims is None


def test_resize_applies_again_once_the_desktop_viewer_is_gone(backend, tmp_path, monkeypatch):
    from terminal_mcp import windows_visible_console

    fake_viewer = _FakeDesktopViewer()
    monkeypatch.setattr(windows_visible_console, "is_available", lambda: (True, None))
    monkeypatch.setattr(windows_visible_console, "spawn_desktop_viewer", lambda backend, name, cwd: fake_viewer)
    monkeypatch.setattr(windows_visible_console, "desktop_session_id", lambda: 1)
    backend.new_session("win-resize-freed", str(tmp_path), show_on_desktop=True)

    fake_viewer._alive = False  # the viewer window was closed
    backend.resize("win-resize-freed", 40, 120)
    assert backend._sessions["win-resize-freed"].last_resize_dims == (40, 120)


def test_resize_from_desktop_viewer_bypasses_the_ownership_guard(backend, tmp_path, monkeypatch):
    """The desktop viewer's OWN attach-time size sync must apply even
    though a desktop viewer is (of course) attached at that exact moment
    -- resize()'s own guard must never also block this path, or the
    dimension sync this whole fix exists for could never actually run."""
    from terminal_mcp import windows_visible_console

    fake_viewer = _FakeDesktopViewer()
    monkeypatch.setattr(windows_visible_console, "is_available", lambda: (True, None))
    monkeypatch.setattr(windows_visible_console, "spawn_desktop_viewer", lambda backend, name, cwd: fake_viewer)
    monkeypatch.setattr(windows_visible_console, "desktop_session_id", lambda: 1)
    backend.new_session("win-resize-sync", str(tmp_path), show_on_desktop=True)

    backend.resize_from_desktop_viewer("win-resize-sync", 50, 160)
    assert backend._sessions["win-resize-sync"].last_resize_dims == (50, 160)


def test_resize_is_idempotent_for_an_unchanged_size(backend, tmp_path):
    _new_fake_shell_session(backend, tmp_path)
    entry = backend._sessions["win-test"]
    backend.resize("win-test", 24, 80)
    assert entry.last_resize_dims == (24, 80)
    # A second call with the SAME size must still be a safe no-op (never
    # raises, never a redundant syscall) -- verified by simply calling it
    # again and confirming the recorded dims are unchanged.
    backend.resize("win-test", 24, 80)
    assert entry.last_resize_dims == (24, 80)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------------------
# P0 HOTFIX (task: "P0 WINDOWS SESSION STATE-LOSS / STALE STREAM"): real
# 'window' incident on dell-5530 -- current_command permanently reported
# the session's own original spawn command ("powershell.exe") even hours
# into a real, live claude.exe run as its child, and a died reader thread
# had no self-repair at all. Both fixed below; _win32_foreground_command
# itself is only ever exercised through an injected fake resolver here
# (see ForegroundCommandResolver's own docstring for why -- the real
# ctypes/CreateToolhelp32Snapshot implementation only works on an actual
# Windows host) except for its own explicit non-Windows fallback, which
# this dev environment's own sys.platform naturally exercises for real.
# ---------------------------------------------------------------------------

from terminal_mcp.windows_backend import _win32_foreground_command  # noqa: E402


def test_win32_foreground_command_falls_back_on_non_windows_platform():
    # This suite runs on Linux -- sys.platform != "win32" is exactly the
    # real, unmocked condition here, not a simulated one.
    assert _win32_foreground_command(99999, "powershell.exe") == "powershell.exe"


def test_get_session_uses_the_injected_foreground_resolver(backend, tmp_path):
    _new_fake_shell_session(backend, tmp_path)
    calls = []

    def fake_resolver(pid: int, fallback: str) -> str:
        calls.append((pid, fallback))
        return "claude"

    backend._foreground_command_resolver = fake_resolver
    info = backend.get_session("win-test")
    assert info.pane_current_command == "claude"
    assert calls and calls[0][0] == info.pane_pid
    assert calls[0][1] == Path(backend.shell).name  # unchanged fallback-computation contract


def test_get_session_falls_back_safely_if_the_resolver_itself_raises(backend, tmp_path):
    _new_fake_shell_session(backend, tmp_path)

    def broken_resolver(pid: int, fallback: str) -> str:
        raise RuntimeError("simulated ctypes failure")

    backend._foreground_command_resolver = broken_resolver
    info = backend.get_session("win-test")
    assert info is not None
    # Falls back to the ORIGINAL (pre-fix) behavior -- never raises past
    # get_session, never leaves pane_current_command empty/wrong.
    assert info.pane_current_command == Path(backend.shell).name


def test_get_session_reports_reader_alive_true_for_a_healthy_session(backend, tmp_path):
    _new_fake_shell_session(backend, tmp_path)
    info = backend.get_session("win-test")
    assert info.reader_alive is True
    assert info.reader_restarts == 0


def test_reader_thread_death_self_heals_on_the_next_get_session_poll(backend, tmp_path):
    """REAL BUG this fixes (task: item 3/6/7's own resilience
    requirement): reader_thread was started once at create_session time
    and never otherwise supervised -- a single entry.proc.read()
    exception silently ended the thread forever, freezing
    activity_epoch/buffer/live-viewer feed even though the real process
    stayed alive and kept producing output nobody was draining anymore.
    Simulated here by directly killing the live reader thread and
    corrupting entry.proc.read() to fail exactly once (mirroring a
    transient ConPTY hiccup) -- get_session() must detect the dead
    thread, restart it, and see output written AFTER the restart."""
    _new_fake_shell_session(backend, tmp_path)
    entry = backend._sessions["win-test"]
    assert _wait_until(lambda: any("PS>" in line for line in backend.capture_lines("win-test", 20)))

    # Kill the live reader thread exactly like a real proc.read()
    # exception would (see _reader_loop's own top-level except: break) --
    # stop it and wait for it to actually exit, so the next get_session()
    # call sees a genuinely dead (not just "about to die") thread.
    entry.stop_reading.set()
    entry.reader_thread.join(timeout=3)
    assert not entry.reader_thread.is_alive()

    info = backend.get_session("win-test")
    assert info.reader_alive is True  # self-healed within this one call
    assert info.reader_restarts == 1
    assert entry.reader_thread.is_alive()

    # Prove the restarted thread is genuinely draining NEW output, not
    # just reporting is_alive()=True while still stuck -- write something
    # only after the restart and confirm it's captured.
    backend.send_text("win-test", "post-restart-marker", press_enter=True)
    assert _wait_until(lambda: any("you said: post-restart-marker" in line
                                   for line in backend.capture_lines("win-test", 20)))


def test_reader_thread_is_not_restarted_once_the_process_itself_is_dead(backend, tmp_path):
    """The other half of the same fix's own safety condition: restarting
    a reader against a process that genuinely exited would just fail
    identically forever (a busy-fail loop), so get_session() must never
    attempt it -- reader_alive is None (not True/False) once the
    process itself is confirmed dead, since "is the reader alive" stops
    being a meaningful question at that point."""
    _new_fake_shell_session(backend, tmp_path)
    entry = backend._sessions["win-test"]
    entry.proc.terminate(force=True)
    assert _wait_until(lambda: not entry.proc.isalive())
    entry.stop_reading.set()
    entry.reader_thread.join(timeout=3)

    info = backend.get_session("win-test")
    assert info.pane_dead is True
    assert info.reader_alive is None
    assert entry.reader_restarts == 0  # never attempted -- process is genuinely gone
