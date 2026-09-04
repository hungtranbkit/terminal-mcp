"""Prompt-submission reliability upgrade -- coverage for the additive
surface this upgrade actually adds on top of the pre-existing, already
extensively covered send pipeline (adapters.py + core.py's
_send_text_and_verify_locked -- see test_send_reliability.py,
test_adapters.py, test_adapters_real_cli.py, test_p0_delivery.py,
test_p0_hardening.py for that pipeline's own regression suite, all
unchanged by this upgrade and still passing).

This file covers, specifically: the new receipt-enrichment fields
(submission_id/agent_type/evidence/activation_attempts/stage -- P6/P8),
the new permission-model split (permissions.allow_send_keys -- P9), the
new loop-protection metadata schema and its one enforced rule
(max_agent_bridge_depth -- P11), and the new PromptTransport extension
point (prompt_transport.py -- P10). See docs/prompt-submission.md for the
architecture this maps to.
"""
from __future__ import annotations

import time

from terminal_mcp.adapters import DELIVERY_SUBMIT_CONFIRMED, DELIVERY_TEXT_SENT
from terminal_mcp.audit import AuditStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, load_config
from terminal_mcp.core import TerminalService


def _service(tmp_path, *, allow_send_keys: bool = True, max_agent_bridge_depth: int = 2) -> TerminalService:
    config = AppConfig(
        PermissionsConfig(True, True, allow_send_keys=allow_send_keys), ("test-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("test-*",), max_text_length=2000),
        max_agent_bridge_depth=max_agent_bridge_depth,
    )
    return TerminalService(config, audit=AuditStore(tmp_path / "audit.db"))


# ---------------------------------------------------------------------------
# P6/P8: receipt enrichment
# ---------------------------------------------------------------------------

def test_receipt_has_submission_id_alias_of_correlation_id(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-receipt-alias", "bash")
    time.sleep(0.2)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "hello", press_enter=False)
    assert result["submission_id"] == result["correlation_id"]


def test_receipt_confirmed_send_has_agent_type_and_evidence(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-receipt-confirmed", "bash")
    time.sleep(0.2)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "echo hi", press_enter=True)
    assert result["delivery_state"] == DELIVERY_SUBMIT_CONFIRMED
    assert result["agent_type"] == "generic"
    assert result["evidence"] == ["OUTPUT_CHANGED"]
    assert result["activation_attempts"] == 1
    assert "stage" not in result  # nothing to diagnose on a confirmed send


def test_receipt_text_only_send_has_text_sent_evidence_and_zero_attempts(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-receipt-textonly", "bash")
    time.sleep(0.2)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "hello", press_enter=False)
    assert result["delivery_state"] == DELIVERY_TEXT_SENT
    assert result["evidence"] == ["TEXT_SENT"]
    assert result["activation_attempts"] == 0


def test_receipt_pane_busy_has_write_stage_and_no_evidence(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-receipt-busy", "bash")
    time.sleep(0.2)
    service = _service(tmp_path)
    identity = service.resolve_identity(session)
    lock_key = f"{identity.session_id}:{identity.pane_id}"
    assert service.leases.acquire(lock_key, "someone-else", ttl_seconds=30)
    try:
        result = service.terminal_send_text(session, "hello", press_enter=True)
        assert result["error"] == "PANE_BUSY"
        assert result["stage"] == "WRITE"
        assert result["evidence"] == []
        assert result["submission_id"] == result["correlation_id"]
    finally:
        service.leases.release(lock_key, "someone-else")


def test_receipt_session_not_found_mid_send_reports_write_stage(tmp_path):
    # _input_guard's own pre-check would normally catch a missing session
    # before _send_text_and_verify is ever reached (see
    # test_permissions.py) -- this exercises _send_text_and_verify_locked's
    # OWN, narrower SESSION_NOT_FOUND branch directly, the "vanished
    # between the outer guard and the actual send" race this upgrade's
    # stage field is meant to help diagnose.
    service = _service(tmp_path)
    result = service._send_text_and_verify_locked(
        "test-receipt-vanished-xyz", "hello", True, correlation_id="deadbeef",
    )
    result = service._enrich_receipt(result)
    assert result["error"] == "SESSION_NOT_FOUND"
    assert result["stage"] == "WRITE"


def test_receipt_idempotent_replay_keeps_the_same_enriched_fields(tmux_session_factory, tmp_path):
    # The replayed (stored) result already carries the enrichment from the
    # original attempt -- a repeat call must not silently lose it.
    session = tmux_session_factory("test-receipt-idem-replay", "bash")
    time.sleep(0.2)
    service = _service(tmp_path)
    first = service.terminal_send_text(session, "echo hi", press_enter=True, idempotency_key="receipt-replay-key")
    second = service.terminal_send_text(session, "echo hi", press_enter=True, idempotency_key="receipt-replay-key")
    assert second == first
    assert second["submission_id"] == first["correlation_id"]
    assert "evidence" in second and "activation_attempts" in second


# ---------------------------------------------------------------------------
# P9: permission-model split -- allow_send_keys, backward compatible
# ---------------------------------------------------------------------------

def test_allow_send_keys_defaults_to_true_backward_compatible():
    config = load_config()  # the repo's own config.yaml, no allow_send_keys key set
    assert config.permissions.allow_send_keys is True


def test_send_keys_disabled_blocks_raw_keys_but_not_send_text(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-send-keys-disabled", "bash")
    time.sleep(0.2)
    service = _service(tmp_path, allow_send_keys=False)
    keys_result = service.terminal_send_keys(session, ["Enter"])
    assert keys_result["error"] == "SEND_KEYS_DISABLED"
    text_result = service.terminal_send_text(session, "hello", press_enter=False)
    assert "error" not in text_result
    assert text_result["sent"] is True


def test_send_keys_enabled_by_default_unchanged(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-send-keys-default", "bash")
    time.sleep(0.2)
    service = _service(tmp_path)  # allow_send_keys defaults True
    result = service.terminal_send_keys(session, ["Enter"])
    assert result.get("sent") is True


# ---------------------------------------------------------------------------
# P11: loop-protection metadata -- schema present, depth enforced
# ---------------------------------------------------------------------------

def test_max_agent_bridge_depth_defaults_to_two():
    config = load_config()
    assert config.max_agent_bridge_depth == 2


def test_depth_exceeding_the_configured_max_is_refused_fail_closed(tmp_path):
    service = _service(tmp_path, max_agent_bridge_depth=2)
    result = service.terminal_send_text("test-depth-guard", "hello", press_enter=True, depth=3)
    assert result["error"] == "AGENT_BRIDGE_DEPTH_EXCEEDED"
    assert result["depth"] == 3
    assert result["max_agent_bridge_depth"] == 2


def test_depth_within_the_configured_max_is_unaffected(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-depth-ok", "bash")
    time.sleep(0.2)
    service = _service(tmp_path, max_agent_bridge_depth=2)
    result = service.terminal_send_text(session, "hello", press_enter=False, depth=2)
    assert "error" not in result


def test_depth_guard_also_applies_to_the_granted_send_path(tmp_path):
    from terminal_mcp.grants import SessionGrantStore

    service = _service(tmp_path)
    service.grants = SessionGrantStore(tmp_path / "grants.db")
    result = service.terminal_send_text_granted("some-granted-session", "hello", press_enter=True, depth=99)
    assert result["error"] == "AGENT_BRIDGE_DEPTH_EXCEEDED"


def test_origin_and_trace_id_are_recorded_to_the_audit_log(tmux_session_factory, tmp_path):
    import sqlite3

    session = tmux_session_factory("test-origin-audit", "bash")
    time.sleep(0.2)
    audit_path = tmp_path / "audit.db"
    config = AppConfig(PermissionsConfig(True, True), ("test-*",), 200, 100,
                       InputPolicyConfig(allowed_session_patterns=("test-*",)))
    service = TerminalService(config, audit=AuditStore(audit_path))
    service.terminal_send_text(session, "hello", press_enter=False,
                               origin="dashboard", trace_id="trace-abc", depth=1)
    conn = sqlite3.connect(audit_path)
    row = conn.execute(
        "SELECT origin, trace_id, depth FROM input_audit WHERE session=? ORDER BY id DESC LIMIT 1",
        (session,),
    ).fetchone()
    assert row == ("dashboard", "trace-abc", 1)


def test_loop_protection_metadata_omitted_by_default_is_backward_compatible(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-no-metadata", "bash")
    time.sleep(0.2)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "hello", press_enter=False)
    assert "error" not in result  # every existing caller (no origin/trace_id/depth) is unaffected


# ---------------------------------------------------------------------------
# P10: PromptTransport extension point -- no browser automation dependency
# ---------------------------------------------------------------------------

def test_chatgpt_web_transport_is_an_unimplemented_stub():
    from terminal_mcp.prompt_transport import ChatGptWebTransport

    try:
        ChatGptWebTransport()
        assert False, "expected NotImplementedError"
    except NotImplementedError:
        pass


def test_tmux_prompt_transport_satisfies_the_protocol_shape():
    from terminal_mcp.prompt_transport import PromptTransport, TmuxPromptTransport

    transport = TmuxPromptTransport(terminal=None)
    assert isinstance(transport, PromptTransport)


def test_submission_origin_child_increments_depth_and_keeps_trace_id():
    from terminal_mcp.prompt_transport import SubmissionOrigin

    root = SubmissionOrigin(origin="codex", trace_id="trace-1")
    child = root.child(origin="chatgpt", turn_id="turn-1")
    assert child.depth == 1
    assert child.trace_id == "trace-1"
    assert child.parent_turn_id == "turn-1"


# ---------------------------------------------------------------------------
# ask_chatgpt bridge Phase A (docs/ask-chatgpt-bridge.md §4): the
# ChatGptBridgeTransport Protocol addition on top of PromptTransport above.
# The rest of Phase A's own behavior (state machine, capability store,
# permission/loop-protection gates) is covered by
# tests/test_ask_chatgpt_bridge.py; this file only proves the Protocol
# shape itself, matching test_tmux_prompt_transport_satisfies_the_protocol_
# shape's own pattern above.
# ---------------------------------------------------------------------------

def test_mock_bridge_transport_satisfies_the_chatgpt_bridge_transport_protocol_shape():
    from terminal_mcp.bridge import MockBridgeTransport
    from terminal_mcp.prompt_transport import ChatGptBridgeTransport

    assert isinstance(MockBridgeTransport(), ChatGptBridgeTransport)


def test_chatgpt_web_transport_still_unimplemented_after_bridge_protocol_addition():
    # ChatGptBridgeTransport is an ADDITION alongside ChatGptWebTransport,
    # not a change to it -- the stub still raises exactly as before.
    from terminal_mcp.prompt_transport import ChatGptWebTransport

    try:
        ChatGptWebTransport()
        assert False, "expected NotImplementedError"
    except NotImplementedError:
        pass


def test_no_playwright_or_browser_automation_dependency_declared():
    # P10/P15: this phase must not introduce a browser-automation
    # dependency anywhere in the production dependency list.
    import pathlib
    import tomllib

    pyproject = tomllib.loads((pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    deps = " ".join(pyproject["project"]["dependencies"]).lower()
    for forbidden in ("playwright", "selenium", "puppeteer", "electron"):
        assert forbidden not in deps
