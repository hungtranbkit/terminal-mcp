"""P0 audit finding #14: a tmux pane left in copy-mode (a human manually
scrolled it, or an errant key sequence entered it) intercepts every
keystroke for its own scrollback/search/selection UI -- none of it ever
reaches the underlying program, regardless of what pane_current_command
reports. Before this fix, every input path silently reported generic
DELIVERY_UNKNOWN with no indication of why, indefinitely, until something
exited copy-mode out of band -- reproduced live against a real tmux pane.
Every input path must now refuse with a specific, actionable
PANE_IN_COPY_MODE error instead, and never auto-exit copy-mode itself."""
from __future__ import annotations

import subprocess
import time

from terminal_mcp.audit import AuditStore
from terminal_mcp.bindings import BindingStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.grants import SessionGrantStore


def _service(tmp_path) -> TerminalService:
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("test-*",), max_text_length=2000, allow_keys=["q", "Enter"]),
    )
    return TerminalService(
        config, audit=AuditStore(tmp_path / "audit.db"),
        bindings=BindingStore(tmp_path / "bindings.db"), grants=SessionGrantStore(tmp_path / "grants.db"),
    )


def _enter_copy_mode(session: str) -> None:
    subprocess.run(["tmux", "copy-mode", "-t", session], check=True, capture_output=True, text=True, timeout=10)


def _exit_copy_mode(session: str) -> None:
    # The out-of-band "operator" action the error message itself points
    # to -- never performed by terminal-mcp's own guarded pipeline.
    subprocess.run(["tmux", "send-keys", "-t", session, "-X", "cancel"],
                   check=True, capture_output=True, text=True, timeout=10)


def test_terminal_send_text_refused_in_copy_mode(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-copymode-text", "bash -lc 'read v; echo GOT=$v; sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path)
    _enter_copy_mode(session)
    result = service.terminal_send_text(session, "should-not-land", press_enter=True)
    assert result["error"] == "PANE_IN_COPY_MODE"
    assert "copy-mode" in result["reason"]
    _exit_copy_mode(session)
    time.sleep(0.1)
    pane = service.terminal_tail(session, 20)["output"]
    assert "should-not-land" not in pane  # never actually sent


def test_terminal_send_keys_refused_in_copy_mode(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-copymode-keys", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path)
    _enter_copy_mode(session)
    result = service.terminal_send_keys(session, ["Enter"])
    assert result["error"] == "PANE_IN_COPY_MODE"
    _exit_copy_mode(session)


def test_terminal_send_bound_refused_in_copy_mode(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-copymode-bound", "bash -lc 'read v; echo GOT=$v; sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path)
    bind = service.terminal_bind("copymode-binding", session, input_enabled=True)
    assert "error" not in bind
    _enter_copy_mode(session)
    result = service.terminal_send_bound("copymode-binding", "should-not-land", press_enter=True)
    assert result["error"] == "PANE_IN_COPY_MODE"
    _exit_copy_mode(session)


def test_terminal_send_text_granted_refused_in_copy_mode(tmux_session_factory, tmp_path):
    session = tmux_session_factory("newsession-copymode-granted", "bash -lc 'read v; echo GOT=$v; sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path)
    assert service.grant_session_read(session, True, granted_by="t").get("read_enabled") is True
    assert service.grant_session_input(session, True, granted_by="t").get("input_enabled") is True
    _enter_copy_mode(session)
    result = service.terminal_send_text_granted(session, "should-not-land", press_enter=True)
    assert result["error"] == "PANE_IN_COPY_MODE"
    _exit_copy_mode(session)


def test_terminal_input_context_reports_pane_in_mode(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-copymode-context", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path)
    before = service.terminal_input_context(session=session)
    assert before["pane_in_mode"] is False
    assert before["effective_input"] is True

    _enter_copy_mode(session)
    during = service.terminal_input_context(session=session)
    assert during["pane_in_mode"] is True
    assert during["effective_input"] is False

    _exit_copy_mode(session)
    time.sleep(0.1)
    after = service.terminal_input_context(session=session)
    assert after["pane_in_mode"] is False
    assert after["effective_input"] is True


def test_read_paths_unaffected_by_copy_mode(tmux_session_factory, tmp_path):
    # Copy-mode only ever gates INPUT -- reading (tail/capture/status) a
    # pane that's currently in copy-mode must keep working exactly as
    # before (an operator legitimately scrolling back to look at
    # something is not itself a problem; only input delivery is unsafe).
    session = tmux_session_factory("test-copymode-read", "bash -lc 'echo hello; sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path)
    _enter_copy_mode(session)
    assert "error" not in service.terminal_tail(session, 10)
    assert "error" not in service.terminal_capture(session)
    assert "error" not in service.terminal_status(session)
    _exit_copy_mode(session)


def test_send_recovers_normally_once_copy_mode_is_exited(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-copymode-recover", "bash -lc 'read v; echo GOT=$v; sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path)
    _enter_copy_mode(session)
    blocked = service.terminal_send_text(session, "first-try", press_enter=True)
    assert blocked["error"] == "PANE_IN_COPY_MODE"

    _exit_copy_mode(session)
    time.sleep(0.2)
    result = service.terminal_send_text(session, "second-try", press_enter=True)
    assert result["delivery_state"] == "SUBMIT_CONFIRMED"
    time.sleep(0.2)
    assert "GOT=second-try" in service.terminal_tail(session, 20)["output"]


def test_not_in_copy_mode_is_completely_unaffected(tmux_session_factory, tmp_path):
    # Plain regression: a normal, never-in-copy-mode session behaves
    # exactly as before this fix.
    session = tmux_session_factory("test-copymode-normal", "bash -lc 'read v; echo GOT=$v; sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "normal-send", press_enter=True)
    assert result["delivery_state"] == "SUBMIT_CONFIRMED"
    assert "error" not in result
