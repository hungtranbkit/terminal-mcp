"""P0 Part A: delivery-state semantics, correlation ids, mid-send identity
revalidation, and idempotency-claim crash recovery -- see adapters.py and
TerminalService._send_text_and_verify_locked (core.py)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from terminal_mcp.adapters import DELIVERY_BLOCKED, DELIVERY_SUBMIT_CONFIRMED, DELIVERY_TEXT_SENT
from terminal_mcp.audit import AuditStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService


def _service(tmp_path) -> TerminalService:
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("test-*",), max_text_length=2000),
    )
    return TerminalService(config, audit=AuditStore(tmp_path / "audit.db"))


def test_delivery_state_and_legacy_submit_status_both_present_text_sent(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-delivery-textonly", "bash")
    time.sleep(0.2)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "hello", press_enter=False)
    assert result["delivery_state"] == DELIVERY_TEXT_SENT
    assert result["submit_status"] == "TEXT_SENT"
    assert "correlation_id" in result and len(result["correlation_id"]) == 32


def test_delivery_state_confirmed_on_plain_shell(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-delivery-confirmed", "bash -lc 'read value; echo GOT=$value; sleep 10'")
    time.sleep(0.2)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "hi-there", press_enter=True)
    assert result["delivery_state"] == DELIVERY_SUBMIT_CONFIRMED
    assert result["submit_status"] == "SUBMIT_CONFIRMED"
    assert result["enter_sent"] is True


def test_every_correlation_id_is_unique_per_attempt(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-delivery-corr-unique", "bash")
    time.sleep(0.2)
    service = _service(tmp_path)
    first = service.terminal_send_text(session, "a", press_enter=False)
    second = service.terminal_send_text(session, "b", press_enter=False)
    assert first["correlation_id"] != second["correlation_id"]


def test_idempotent_replay_returns_original_correlation_id_never_resends(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-delivery-corr-idem", "bash -lc 'read v; echo GOT=$v; sleep 10'")
    time.sleep(0.2)
    service = _service(tmp_path)
    first = service.terminal_send_text(session, "once", press_enter=True, idempotency_key="corr-replay-key")
    second = service.terminal_send_text(session, "once", press_enter=True, idempotency_key="corr-replay-key")
    assert first["correlation_id"] == second["correlation_id"]
    pane = service.terminal_tail(session, 20)["output"]
    assert pane.count("GOT=once") == 1  # never sent twice


def test_identity_changed_mid_send_blocks_enter_and_never_retargets(tmux_session_factory, tmp_path, monkeypatch):
    # P0 Part A.3: simulate the pinned identity moving between the
    # text-send revalidation point and the Enter-send revalidation point
    # (e.g. the session was destroyed and a same-named one recreated in
    # that window). Tied to the actual semantic checkpoint (has the real
    # tmux.send_text call for *this* attempt's text happened yet) rather
    # than a brittle raw call count, so this stays correct across any
    # future change to how many get_session calls happen around it. Enter
    # must never be sent to whatever now answers to that name.
    session = tmux_session_factory("test-delivery-identity-race", "bash -lc 'read v; echo GOT=$v; sleep 10'")
    time.sleep(0.2)
    service = _service(tmp_path)
    assert service.tmux.get_session(session) is not None
    state = {"text_sent": False}
    original_get_session = service.tmux.get_session
    original_send_text = service.tmux.send_text

    def flaky_get_session(target: str):
        info = original_get_session(target)
        if state["text_sent"] and info is not None:
            info = info.__class__(**{**info.__dict__, "session_id": "$999999", "pane_id": "%999999"})
        return info

    def marking_send_text(target: str, text: str, press_enter: bool):
        original_send_text(target, text, press_enter)
        state["text_sent"] = True

    monkeypatch.setattr(service.tmux, "get_session", flaky_get_session)
    monkeypatch.setattr(service.tmux, "send_text", marking_send_text)
    result = service.terminal_send_text(session, "should-not-submit", press_enter=True)
    monkeypatch.setattr(service.tmux, "get_session", original_get_session)
    monkeypatch.setattr(service.tmux, "send_text", original_send_text)
    assert result["delivery_state"] == DELIVERY_BLOCKED
    assert result["submit_status"] == "SUBMIT_UNCONFIRMED"
    assert result["error"] == "IDENTITY_CHANGED_MID_SEND"
    assert result["enter_sent"] is False
    assert result["sent"] is True  # the text itself really was delivered before the abort
    pane = service.terminal_tail(session, 20)["output"]
    assert "GOT=" not in pane  # Enter was withheld -- the read never completed
    assert "should-not-submit" in pane  # but the typed text really is sitting there


def test_pane_current_command_change_mid_send_also_blocks(tmux_session_factory, tmp_path, monkeypatch):
    # Same revalidation, the pane_current_command half: identity (session_id/
    # pane_id/created_epoch) stays pinned, but the foreground command
    # changed between the pre-text-send and pre-Enter checkpoints -- also
    # an abort, never a send to a target whose state has moved on since
    # text landed. See the identity test above for the checkpoint-tied
    # (not raw-call-count) fake design.
    session = tmux_session_factory("test-delivery-command-race", "bash -lc 'read v; echo GOT=$v; sleep 10'")
    time.sleep(0.2)
    service = _service(tmp_path)
    assert service.tmux.get_session(session) is not None
    state = {"text_sent": False}
    original_get_session = service.tmux.get_session
    original_send_text = service.tmux.send_text

    def flaky_get_session(target: str):
        info = original_get_session(target)
        if state["text_sent"] and info is not None:
            info = info.__class__(**{**info.__dict__, "pane_current_command": "vim"})
        return info

    def marking_send_text(target: str, text: str, press_enter: bool):
        original_send_text(target, text, press_enter)
        state["text_sent"] = True

    monkeypatch.setattr(service.tmux, "get_session", flaky_get_session)
    monkeypatch.setattr(service.tmux, "send_text", marking_send_text)
    result = service.terminal_send_text(session, "should-not-submit", press_enter=True)
    monkeypatch.setattr(service.tmux, "get_session", original_get_session)
    monkeypatch.setattr(service.tmux, "send_text", original_send_text)
    assert result["delivery_state"] == DELIVERY_BLOCKED
    assert result["error"] == "IDENTITY_CHANGED_MID_SEND"
    assert result["enter_sent"] is False


# ---------------------------------------------------------------------------
# The mid-send revalidation must tell "the same shell, one command later" apart
# from "a different process wearing the same name".
#
# Reproduced live through the real ChatGPT connector (2026-09-19): a freshly
# created generic shell `tmcp-surface-shell` on hp-linux accepted one multiline
# command, and the NEXT send came back BLOCKED/IDENTITY_CHANGED_MID_SEND,
# agent_type=generic, enter_sent=false, enter_count=0, attempts=0. Nothing had
# been replaced -- the pane was still running the previous command when the
# text landed and was back at its own prompt 80ms later, so the raw
# `command_at_enter != command_before` comparison read a shell finishing its
# work as the target disappearing, and left the typed text unexecutable.
# ---------------------------------------------------------------------------

def _mid_send_mutation(service, monkeypatch, mutate_before=None, mutate_after=None):
    """Rewrite what get_session reports before/after THIS attempt's text send.

    Tied to the real semantic checkpoint (has tmux.send_text run yet) rather
    than a call count, the same design the two identity tests above use and for
    the same reason.
    """
    state = {"text_sent": False}
    original_get_session = service.tmux.get_session
    original_send_text = service.tmux.send_text

    def faked_get_session(target: str):
        info = original_get_session(target)
        mutate = mutate_after if state["text_sent"] else mutate_before
        if info is not None and mutate is not None:
            info = info.__class__(**{**info.__dict__, **mutate})
        return info

    def marking_send_text(target: str, text: str, press_enter: bool):
        original_send_text(target, text, press_enter)
        state["text_sent"] = True

    monkeypatch.setattr(service.tmux, "get_session", faked_get_session)
    monkeypatch.setattr(service.tmux, "send_text", marking_send_text)


def test_shell_returning_to_its_prompt_mid_send_does_not_false_block(tmux_session_factory, tmp_path,
                                                                    monkeypatch):
    # THE live repro, at the same checkpoints: the pane reports the previous
    # command ("python3") when the text is sent and its own shell ("bash") when
    # Enter is about to go out. That is a shell finishing a command, not a
    # replaced target, so Enter must be sent and the submission confirmed.
    session = tmux_session_factory("test-delivery-shell-settles", "bash -lc 'read v; echo GOT=$v; sleep 10'")
    time.sleep(0.2)
    service = _service(tmp_path)
    _mid_send_mutation(service, monkeypatch, mutate_before={"pane_current_command": "python3"})

    result = service.terminal_send_text(session, "hi-there", press_enter=True)

    assert result.get("error") is None, result
    assert result["agent_type"] == "generic"  # same adapter as the live report
    assert result["enter_sent"] is True
    assert result["enter_count"] == 1  # exactly one Enter, never a retry storm
    assert result["delivery_state"] == DELIVERY_SUBMIT_CONFIRMED
    time.sleep(0.3)
    assert "GOT=hi-there" in service.terminal_tail(session, 20)["output"]


def test_pane_process_replaced_mid_send_still_blocks_enter(tmux_session_factory, tmp_path, monkeypatch):
    # The safety this keeps, and strengthens: a pane whose own process was
    # replaced reports the SAME command name at both ends (bash -> bash), so
    # the command comparison never could catch it. tmux's #{pane_pid} can.
    session = tmux_session_factory("test-delivery-pid-replaced", "bash -lc 'read v; echo GOT=$v; sleep 10'")
    time.sleep(0.2)
    service = _service(tmp_path)
    before = service.tmux.get_session(session)
    assert before is not None
    _mid_send_mutation(service, monkeypatch, mutate_after={"pane_pid": before.pane_pid + 424242})

    result = service.terminal_send_text(session, "should-not-submit", press_enter=True)

    assert result["delivery_state"] == DELIVERY_BLOCKED
    assert result["error"] == "IDENTITY_CHANGED_MID_SEND"
    assert result["enter_sent"] is False
    assert "process was replaced" in result["submit_reason"]
    pane = service.terminal_tail(session, 20)["output"]
    assert "GOT=" not in pane  # Enter was withheld -- the read never completed


def test_shell_taken_over_by_another_program_mid_send_still_blocks_enter(tmux_session_factory, tmp_path,
                                                                        monkeypatch):
    # Not relaxed for a shell: a change INTO something that is not the pane's
    # own shell means another program owns the pty now, and a stray Enter would
    # land in it. (test_pane_current_command_change_mid_send_also_blocks above
    # covers the same rule from the other direction; this one states it
    # explicitly as the boundary of the fix.)
    session = tmux_session_factory("test-delivery-shell-taken-over", "bash -lc 'read v; echo GOT=$v; sleep 10'")
    time.sleep(0.2)
    service = _service(tmp_path)
    _mid_send_mutation(service, monkeypatch, mutate_after={"pane_current_command": "vim"})

    result = service.terminal_send_text(session, "should-not-submit", press_enter=True)

    assert result["delivery_state"] == DELIVERY_BLOCKED
    assert result["error"] == "IDENTITY_CHANGED_MID_SEND"
    assert result["enter_sent"] is False
    assert "not this pane's own shell" in result["submit_reason"]


def test_enter_safety_rule_by_adapter():
    # The rule itself, without a pty: an agent CLI's command name IS the
    # target's identity and any change blocks; a shell's is transient metadata
    # and only a change away from a shell blocks.
    from terminal_mcp.adapters import (ClaudeAdapter, CodexAdapter, GenericShellAdapter,
                                       enter_is_safe_after_command_change)

    generic, claude, codex = GenericShellAdapter(), ClaudeAdapter(), CodexAdapter()

    # Unchanged is always fine, for every adapter.
    for adapter, command in ((generic, "bash"), (claude, "claude"), (codex, "codex")):
        assert enter_is_safe_after_command_change(adapter, command, command) is True

    # Shell: back to a prompt (any real shell, and a Windows-suffixed name) is
    # safe; anything else is not.
    assert enter_is_safe_after_command_change(generic, "python3", "bash") is True
    assert enter_is_safe_after_command_change(generic, "git", "zsh") is True
    assert enter_is_safe_after_command_change(generic, "pytest", "bash.exe") is True
    assert enter_is_safe_after_command_change(generic, "bash", "vim") is False
    assert enter_is_safe_after_command_change(generic, "bash", "ssh") is False

    # Agent CLI: the process this send was aimed at is gone. Never safe --
    # including a change to a shell, which means the agent itself exited.
    assert enter_is_safe_after_command_change(claude, "claude", "bash") is False
    assert enter_is_safe_after_command_change(codex, "codex", "bash") is False
    assert enter_is_safe_after_command_change(claude, "claude", "node") is False

    # A future adapter is strict until it deliberately opts out.
    assert GenericShellAdapter.foreground_command_is_identity is False
    assert ClaudeAdapter.foreground_command_is_identity is True
    assert CodexAdapter.foreground_command_is_identity is True


def test_stale_idempotency_claim_is_reclaimed_not_stuck_forever(tmp_path):
    # A crashed claimant (process killed after claiming, before storing a
    # result) must not leave the key permanently reporting
    # DUPLICATE_IN_PROGRESS -- a caller retrying it later must eventually
    # be able to actually perform the action.
    audit = AuditStore(tmp_path / "audit.db")
    assert audit.claim_idempotency_key("stale-key", stale_after_seconds=9999) is True
    # A concurrent, still-legitimately-in-flight claim within the window
    # must NOT be reclaimed.
    assert audit.claim_idempotency_key("stale-key", stale_after_seconds=9999) is False
    # Backdate the claim to simulate real elapsed time without sleeping.
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    with audit._connection() as connection:
        connection.execute("UPDATE idempotent_sends SET created_at = ? WHERE idempotency_key = ?",
                           (old, "stale-key"))
    assert audit.claim_idempotency_key("stale-key", stale_after_seconds=30) is True
    # And a genuinely completed claim (result stored) is never "stale" --
    # get_idempotent_result is what a caller checks first in practice, but
    # reclaiming a *finished* claim would risk a real duplicate send if a
    # caller ever called claim_idempotency_key directly after completion.
    audit.store_idempotent_result("stale-key", {"ok": True})
    with audit._connection() as connection:
        connection.execute("UPDATE idempotent_sends SET created_at = ? WHERE idempotency_key = ?",
                           (old, "stale-key"))
    assert audit.claim_idempotency_key("stale-key", stale_after_seconds=30) is False


def test_generic_shell_enter_is_confirmation_even_without_redraw(tmux_session_factory, tmp_path, monkeypatch):
    """A silent shell command must not become false DELIVERY_UNKNOWN.

    Generic shells do not have a raw-mode composer that can swallow Enter.
    Once the existing mid-send identity/command guard has proved the same
    shell still owns the pane, delivery of Enter is the acceptance boundary.
    """
    session = tmux_session_factory("test-delivery-silent-shell", "bash")
    time.sleep(0.2)
    service = _service(tmp_path)
    original_capture = service.tmux.capture_lines

    # Preserve the real pre-send capture until text has been written, then
    # simulate the live failure shape: no visible redraw during verification.
    state = {"text_sent": False}
    original_send_text = service.tmux.send_text

    def marking_send_text(target: str, text: str, press_enter: bool):
        original_send_text(target, text, press_enter)
        state["text_sent"] = True

    frozen = original_capture(session, 200)

    def frozen_capture(target: str, lines: int):
        if state["text_sent"]:
            return list(frozen)
        return original_capture(target, lines)

    monkeypatch.setattr(service.tmux, "send_text", marking_send_text)
    monkeypatch.setattr(service.tmux, "capture_lines", frozen_capture)

    result = service.terminal_send_text(session, "sleep 0.1", press_enter=True)

    assert result["delivery_state"] == "SUBMIT_CONFIRMED"
    assert result["enter_sent"] is True
    assert "SHELL_ENTER_DELIVERED" in result["evidence"]
