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
from terminal_mcp.lease import PaneLeaseStore


def _service(tmp_path) -> TerminalService:
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("test-*",), max_text_length=2000, allow_keys=["q", "Enter"]),
    )
    return TerminalService(
        config, audit=AuditStore(tmp_path / "audit.db"),
        bindings=BindingStore(tmp_path / "bindings.db"), grants=SessionGrantStore(tmp_path / "grants.db"),
        leases=PaneLeaseStore(tmp_path / "leases.db"),
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


def test_explicit_exit_copy_mode_then_real_send_reaches_program(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-copymode-explicit", "bash -lc 'read v; echo GOT=$v; sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path)
    _enter_copy_mode(session)

    blocked = service.terminal_send_text(session, "explicit-flow", press_enter=True)
    assert blocked["error"] == "PANE_IN_COPY_MODE"
    exited = service.terminal_exit_copy_mode(session=session)
    assert exited["status"] == "COPY_MODE_EXITED"
    assert exited["copy_mode_exited"] is True
    assert service.tmux.get_session(session).pane_in_mode is False

    sent = service.terminal_send_text(session, "explicit-flow", press_enter=True)
    assert sent["delivery_state"] == "SUBMIT_CONFIRMED"
    time.sleep(0.2)
    assert "GOT=explicit-flow" in service.terminal_tail(session, 20)["output"]
    events = service.terminal_list_input_audit(session=session)["events"]
    exit_event = next(e for e in events if e["action"] == "exit_copy_mode")
    assert exit_event["result"] == "SUCCEEDED"
    assert exit_event["preview"] is None


def test_exit_copy_mode_by_pinned_binding(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-copymode-bound-exit", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path)
    assert "error" not in service.terminal_bind("copy-exit", session, input_enabled=True)
    _enter_copy_mode(session)
    result = service.terminal_exit_copy_mode(binding="copy-exit")
    assert result["binding"] == "copy-exit"
    assert result["status"] == "COPY_MODE_EXITED"
    assert service.tmux.get_session(session).pane_in_mode is False


def test_exit_copy_mode_not_in_mode_is_audited_noop(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-copymode-noop", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path)
    result = service.terminal_exit_copy_mode(session=session)
    assert result == {"session": session, "copy_mode_exited": False, "status": "NOT_IN_COPY_MODE"}
    event = service.terminal_list_input_audit(session=session)["events"][0]
    assert event["action"] == "exit_copy_mode"
    assert event["result"] == "NOOP"


def test_exit_copy_mode_denies_forbidden_session_and_binding(tmux_session_factory, tmp_path):
    service = _service(tmp_path)
    forbidden = tmux_session_factory("private-copy-mode", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    _enter_copy_mode(forbidden)
    assert service.terminal_exit_copy_mode(session=forbidden)["error"] == "ACCESS_DENIED"
    assert service.terminal_exit_copy_mode(binding="missing-binding")["error"] == "BINDING_NOT_FOUND"

    allowed = tmux_session_factory("test-copy-disabled-binding", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    assert "error" not in service.terminal_bind("copy-disabled", allowed, input_enabled=False)
    _enter_copy_mode(allowed)
    assert service.terminal_exit_copy_mode(binding="copy-disabled")["error"] == "BINDING_INPUT_DISABLED"


def test_exit_copy_mode_stale_binding_and_missing_session_fail_safe(tmux_session_factory, tmp_path):
    service = _service(tmp_path)
    session = tmux_session_factory("test-copy-stale", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    assert "error" not in service.terminal_bind("copy-stale", session, input_enabled=True)
    subprocess.run(["tmux", "kill-session", "-t", session], check=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "bash -lc 'sleep 20'"], check=True)
    time.sleep(0.2)
    _enter_copy_mode(session)
    assert service.terminal_exit_copy_mode(binding="copy-stale")["error"] == "IDENTITY_MISMATCH"
    subprocess.run(["tmux", "kill-session", "-t", session], check=True)
    assert service.terminal_exit_copy_mode(session=session)["error"] == "SESSION_NOT_FOUND"
