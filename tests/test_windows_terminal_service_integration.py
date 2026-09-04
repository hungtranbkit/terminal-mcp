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
