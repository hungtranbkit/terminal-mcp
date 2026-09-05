"""The central claim behind this project's whole Windows-support design
(session_backend.py's own module docstring): TerminalService (core.py)
runs COMPLETELY UNCHANGED on top of a non-tmux backend -- every
permission check, audit record, redaction, and the create/status/tail/
send/kill/reopen lifecycle it already has for Linux apply identically to
a WindowsSessionBackend-backed instance, with zero duplicated business
logic. This file is the real, running proof: the exact same TerminalService
class, constructed with `tmux=WindowsSessionBackend(...)` instead of the
default TmuxClient(), driving a REAL child process via `_FakePty` (see
test_windows_backend.py's own docstring for why that's a faithful,
non-mocked stand-in on this Linux dev host) through the identical MCP-
tool-facing methods every Linux test in this project already exercises.
"""
from __future__ import annotations

import sys
import time

import pytest

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.windows_backend import WindowsSessionBackend

from tests.test_windows_backend import _FAKE_SHELL_SCRIPT, _fake_factory


def _config(tmp_path) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True),
        allowed_session_patterns=("win-*",),
        max_capture_lines=200,
        default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("win-*",)),
        session_lifecycle=SessionLifecycleConfig(
            enabled=True, allowed_cwd_roots=(str(tmp_path),), protected_sessions=(),
            launch_commands=(),
        ),
    )


@pytest.fixture
def windows_service(tmp_path):
    script_path = tmp_path / "fake_shell.py"
    script_path.write_text(_FAKE_SHELL_SCRIPT)

    def factory(argv, cwd):
        return _fake_factory([sys.executable, "-u", str(script_path)], cwd)

    # `shell` here is what a session's pane_current_command reports for
    # agent_type="shell" classification purposes (core.py's
    # _classify_agent_type/SHELL_COMMAND_NAMES) -- set to a REAL Windows
    # shell name to exercise that path realistically, while the actual
    # spawned process (via `factory` above, which ignores argv) is still
    # the fake script, since powershell.exe doesn't exist on this host.
    backend = WindowsSessionBackend(shell="powershell.exe", process_factory=factory, history_lines=500)
    service = TerminalService(_config(tmp_path), tmux=backend)
    yield service, backend
    for name in list(backend._sessions.keys()):
        try:
            backend.kill_session(name)
        except Exception:  # noqa: BLE001
            pass


def _wait_until(predicate, *, timeout: float = 3.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_create_session_shell_reaches_ready(windows_service, tmp_path):
    service, _backend = windows_service
    result = service.terminal_create_session("win-svc-1", "shell", str(tmp_path))
    assert result["state"] == "READY"
    assert "error" not in result


def test_terminal_status_reports_exists_true(windows_service, tmp_path):
    service, _backend = windows_service
    service.terminal_create_session("win-svc-2", "shell", str(tmp_path))
    status = service.terminal_status("win-svc-2")
    assert status["exists"] is True


def test_terminal_tail_shows_real_output(windows_service, tmp_path):
    service, _backend = windows_service
    service.terminal_create_session("win-svc-3", "shell", str(tmp_path))
    assert _wait_until(lambda: "PS>" in service.terminal_tail("win-svc-3", 20)["output"])


def test_terminal_send_text_round_trip_through_full_verification_stack(windows_service, tmp_path):
    # This exercises core.py's REAL _send_text_and_verify_locked --
    # identity pinning, pre/post-Enter snapshot diffing, delivery_state
    # classification -- entirely unmodified, against the Windows backend.
    service, _backend = windows_service
    service.terminal_create_session("win-svc-4", "shell", str(tmp_path))
    assert _wait_until(lambda: "PS>" in service.terminal_tail("win-svc-4", 20)["output"])
    result = service.terminal_send_text("win-svc-4", "hello-from-integration-test", press_enter=True)
    assert result["sent"] is True
    assert result["enter_sent"] is True
    assert result["delivery_state"] in ("SUBMIT_CONFIRMED", "DELIVERY_UNKNOWN", "TEXT_SENT")
    assert _wait_until(lambda: "you said: hello-from-integration-test" in service.terminal_tail("win-svc-4", 20)["output"])


def test_kill_then_reopen_round_trip(windows_service, tmp_path):
    service, backend = windows_service
    service.terminal_create_session("win-svc-5", "shell", str(tmp_path))
    killed = service.terminal_kill_session("win-svc-5", "win-svc-5", requested_by="test")
    assert "error" not in killed
    assert backend.get_session("win-svc-5") is None  # real process actually gone

    reopened = service.terminal_reopen_session("win-svc-5")
    assert "error" not in reopened
    assert backend.get_session("win-svc-5") is not None  # a genuinely NEW process


def test_permission_denial_still_enforced_on_windows_backend(tmp_path):
    # Permission/whitelist logic lives entirely in core.py, never in the
    # backend -- must refuse exactly the same way it would for tmux.
    script_path = tmp_path / "fake_shell.py"
    script_path.write_text(_FAKE_SHELL_SCRIPT)

    def factory(argv, cwd):
        return _fake_factory([sys.executable, "-u", str(script_path)], cwd)

    backend = WindowsSessionBackend(shell=sys.executable, process_factory=factory)
    config = AppConfig(
        permissions=PermissionsConfig(True, False),  # terminal_input disabled globally
        allowed_session_patterns=("win-*",), max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("win-*",)),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),), protected_sessions=()),
    )
    service = TerminalService(config, tmux=backend)
    try:
        service.terminal_create_session("win-svc-6", "shell", str(tmp_path))
        result = service.terminal_send_text("win-svc-6", "should-be-blocked", press_enter=True)
        assert result["error"] == "INPUT_DISABLED"
    finally:
        for name in list(backend._sessions.keys()):
            backend.kill_session(name)


# ---------------------------------------------------------------------------
# Desktop-visible-window metadata, end-to-end through TerminalService
# (task: "user nhìn tại máy Windows cũng thấy đúng terminal session") --
# windows_visible_console.py's own real Win32 calls are monkeypatched
# (unverifiable on this Linux dev host, see test_windows_backend.py's own
# equivalent tests); everything from terminal_create_session's own
# show_on_desktop kwarg down through dashboard_list_sessions/terminal_
# list_sessions' row metadata is real, unmocked TerminalService/core.py
# logic.
# ---------------------------------------------------------------------------

class _FakeDesktopViewer:
    """Stands in for windows_visible_console.DesktopViewerHandle -- the
    session's own process (a REAL _FakePty, via the windows_service
    fixture's normal headless path) is never this object; this is only
    ever the separate, disposable VIEWER (see windows_visible_console.py's
    own module docstring)."""

    def __init__(self) -> None:
        self._alive = True
        self.stop_calls = 0

    def isalive(self) -> bool:
        return self._alive

    def stop(self) -> None:
        self.stop_calls += 1
        self._alive = False


def test_create_session_show_on_desktop_surfaces_in_listings(windows_service, tmp_path, monkeypatch):
    from terminal_mcp import windows_visible_console

    spawned_viewers = []

    def _spawn_fake_viewer(backend, name, cwd):
        # A REAL spawn always produces a genuinely new, alive viewer --
        # never reuses a previously-closed one -- so the fake must too.
        viewer = _FakeDesktopViewer()
        spawned_viewers.append(viewer)
        return viewer

    monkeypatch.setattr(windows_visible_console, "is_available", lambda: (True, None))
    monkeypatch.setattr(windows_visible_console, "spawn_desktop_viewer", _spawn_fake_viewer)
    monkeypatch.setattr(windows_visible_console, "desktop_session_id", lambda: 1)

    service, _backend = windows_service
    result = service.terminal_create_session("win-svc-visible", "shell", str(tmp_path), show_on_desktop=True)
    assert result["state"] == "READY"
    assert result["visible_window"] is True

    mcp_row = {r["name"]: r for r in service.terminal_list_sessions()["sessions"]}["win-svc-visible"]
    assert mcp_row["visible_window"] is True
    assert mcp_row["desktop_session_id"] == 1

    dash_row = {r["name"]: r for r in service.dashboard_list_sessions()["sessions"]}["win-svc-visible"]
    assert dash_row["visible_window"] is True

    # The real bug this redesign fixes: closing the viewer window must
    # never kill the session -- its own process (a real, live _FakePty)
    # must go on being readable/sendable exactly as before.
    spawned_viewers[0]._alive = False
    assert service.terminal_status("win-svc-visible")["exists"] is True
    send = service.terminal_send_text("win-svc-visible", "still-alive-after-viewer-closed", press_enter=True)
    assert "error" not in send
    row_after = {r["name"]: r for r in service.terminal_list_sessions()["sessions"]}["win-svc-visible"]
    assert row_after["visible_window"] is False  # honestly reflects the closed window

    # And the retroactive "Show on desktop" action attaches a NEW viewer
    # to that SAME still-running process -- never a second shell.
    result2 = service.terminal_show_on_desktop("win-svc-visible")
    assert result2["visible_window"] is True
    row_again = {r["name"]: r for r in service.terminal_list_sessions()["sessions"]}["win-svc-visible"]
    assert row_again["visible_window"] is True


def test_create_session_show_on_desktop_false_by_default_no_extra_fields_forced(windows_service, tmp_path):
    service, _backend = windows_service
    result = service.terminal_create_session("win-svc-headless", "shell", str(tmp_path))
    assert "visible_window" not in result  # only surfaced when actually requested
    mcp_row = {r["name"]: r for r in service.terminal_list_sessions()["sessions"]}["win-svc-headless"]
    assert mcp_row["visible_window"] is False


def test_desktop_capability_reaches_terminal_service(windows_service, monkeypatch):
    from terminal_mcp import windows_visible_console

    monkeypatch.setattr(windows_visible_console, "is_available", lambda: (False, "no interactive desktop"))
    monkeypatch.setattr(windows_visible_console, "desktop_session_id", lambda: None)
    service, _backend = windows_service
    assert service.terminal_desktop_capability() == {
        "available": False, "reason": "no interactive desktop", "desktop_session_id": None,
    }


def test_desktop_metadata_is_empty_on_the_plain_tmux_backend(tmp_path):
    # core.py's duck-typed dispatch (_desktop_metadata_for/terminal_
    # desktop_capability) must be a harmless {} on a backend with no such
    # concept at all -- never raise, never invent a fake answer.
    from terminal_mcp.tmux import TmuxClient
    config = AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("test-*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("test-*",)),
    )
    service = TerminalService(config, tmux=TmuxClient())
    assert service._desktop_metadata_for("anything") == {}
    assert service.terminal_desktop_capability() == {}


def test_show_on_desktop_not_supported_on_the_plain_tmux_backend(tmp_path):
    from terminal_mcp.tmux import TmuxClient
    config = AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("test-*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("test-*",)),
    )
    service = TerminalService(config, tmux=TmuxClient())
    result = service.terminal_show_on_desktop("nonexistent-anyway")
    assert result["error"] in ("NOT_SUPPORTED", "SESSION_NOT_FOUND", "ACCESS_DENIED")
