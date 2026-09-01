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
