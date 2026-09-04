"""ask_chatgpt bridge -- Phase A (docs/ask-chatgpt-bridge.md, §13's Phase
A/B test list). Every test here runs against MockBridgeTransport -- no
network, no browser, fully deterministic -- proving BridgeService's state
machine, capability/idempotency store, permission gate, and loop
protection independent of Phase C/D ever existing.
"""
from __future__ import annotations

import time

from terminal_mcp.bridge import (
    BRIDGE_ACTIVATING,
    BRIDGE_CANCELLED,
    BRIDGE_COMPLETED,
    BRIDGE_FAILED,
    BRIDGE_UNKNOWN,
    BridgeService,
    BridgeTurnStore,
    MockBridgeTransport,
    SCENARIO_AMBIGUOUS,
    SCENARIO_NEVER_COMPLETES,
    SCENARIO_PREPARE_FAILS,
    SCENARIO_SEND_NEVER_READY,
    SCENARIO_SERVER_ERROR,
    SCENARIO_SLOW,
    SCENARIO_SUCCESS,
    SCENARIO_VERIFY_MISMATCH,
)
from terminal_mcp.config import AppConfig, AskChatGptConfig, PermissionsConfig


def _config(*, ask_chatgpt: bool = True, depth: int = 2, **ask_chatgpt_kwargs) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True, True, ask_chatgpt=ask_chatgpt),
        allowed_session_patterns=("test-*",),
        max_agent_bridge_depth=depth,
        ask_chatgpt=AskChatGptConfig(**ask_chatgpt_kwargs),
    )


def _service(tmp_path, *, scenario: str = SCENARIO_SUCCESS, config: AppConfig | None = None,
            **transport_kwargs) -> BridgeService:
    store = BridgeTurnStore(tmp_path / "bridge.db")
    transport = MockBridgeTransport(scenario, **transport_kwargs)
    return BridgeService(config or _config(), store=store, transport=transport)


# -- happy path -----------------------------------------------------------

def test_success_reaches_completed_with_full_evidence_trail(tmp_path):
    service = _service(tmp_path)
    receipt = service.ask_chatgpt(source_session="test-1", prompt="hello", timeout_seconds=5,
                                  idempotency_key="k-success")
    assert receipt["state"] == BRIDGE_COMPLETED
    assert receipt["acceptance_evidence"] is True
    assert receipt["activation_attempts"] == 1
    assert receipt["response_text"] == "mock response"
    assert receipt["response_length"] == len("mock response")
    assert receipt["error_stage"] is None
    for stamp in ("prepared_at", "written_at", "verified_at", "activated_at", "accepted_at", "completed_at"):
        assert receipt[stamp] is not None
    assert service.transport.close_calls == [receipt["bridge_turn_id"]]


def test_slow_response_is_polled_until_completed(tmp_path):
    service = _service(tmp_path, scenario=SCENARIO_SLOW, responding_ticks=3)
    receipt = service.ask_chatgpt(source_session="test-1", prompt="hello", timeout_seconds=5,
                                  idempotency_key="k-slow")
    assert receipt["state"] == BRIDGE_COMPLETED


# -- idempotent retry -------------------------------------------------------

def test_idempotent_retry_same_key_never_resubmits(tmp_path):
    service = _service(tmp_path)
    first = service.ask_chatgpt(source_session="test-1", prompt="hello", timeout_seconds=5,
                                idempotency_key="k-retry")
    second = service.ask_chatgpt(source_session="test-1", prompt="a completely different prompt",
                                 timeout_seconds=5, idempotency_key="k-retry")
    assert second["bridge_turn_id"] == first["bridge_turn_id"]
    assert second["state"] == BRIDGE_COMPLETED
    assert service.transport.submit_calls == 1  # never invoked twice for the same key


def test_activation_ambiguous_is_unknown_and_retry_never_duplicates(tmp_path):
    service = _service(tmp_path, scenario=SCENARIO_AMBIGUOUS)
    first = service.ask_chatgpt(source_session="test-1", prompt="hello", timeout_seconds=5,
                                idempotency_key="k-ambiguous")
    assert first["state"] == BRIDGE_UNKNOWN
    assert first["error_stage"] == "ACTIVATION_UNCONFIRMED"
    assert first["activation_attempts"] == 1
    # A retry under the SAME idempotency_key must return the stored UNKNOWN
    # receipt, never attempt a second submit -- the golden rule.
    second = service.ask_chatgpt(source_session="test-1", prompt="hello", timeout_seconds=5,
                                 idempotency_key="k-ambiguous")
    assert second == first
    assert service.transport.submit_calls == 1


def test_verify_mismatch_fails_before_any_activation_attempt(tmp_path):
    service = _service(tmp_path, scenario=SCENARIO_VERIFY_MISMATCH)
    receipt = service.ask_chatgpt(source_session="test-1", prompt="hello", timeout_seconds=5,
                                  idempotency_key="k-verify")
    assert receipt["state"] == BRIDGE_FAILED
    assert receipt["error_stage"] == "VERIFY_MISMATCH"
    assert receipt["activation_attempts"] == 0  # never reached ACTIVATING
    assert receipt["activated_at"] is None


def test_send_control_never_ready_fails_before_activation(tmp_path):
    service = _service(tmp_path, scenario=SCENARIO_SEND_NEVER_READY)
    receipt = service.ask_chatgpt(source_session="test-1", prompt="hello", timeout_seconds=5,
                                  idempotency_key="k-send")
    assert receipt["state"] == BRIDGE_FAILED
    assert receipt["error_stage"] == "SEND_CONTROL_NEVER_ENABLED"


def test_prepare_failure_never_touches_submit(tmp_path):
    service = _service(tmp_path, scenario=SCENARIO_PREPARE_FAILS)
    receipt = service.ask_chatgpt(source_session="test-1", prompt="hello", timeout_seconds=5,
                                  idempotency_key="k-prepare")
    assert receipt["state"] == BRIDGE_FAILED
    assert receipt["error_stage"] == "PREPARE_FAILED"
    assert service.transport.submit_calls == 0
    assert service.transport.close_calls == []  # no handle was ever opened


def test_server_side_response_failure_is_failed_not_unknown(tmp_path):
    service = _service(tmp_path, scenario=SCENARIO_SERVER_ERROR)
    receipt = service.ask_chatgpt(source_session="test-1", prompt="hello", timeout_seconds=5,
                                  idempotency_key="k-server-error")
    assert receipt["state"] == BRIDGE_FAILED
    assert receipt["error_stage"] == "RESPONSE_FAILED"
    assert receipt["acceptance_evidence"] is True  # activation WAS accepted -- the failure is later


def test_response_timeout_is_unknown_not_failed(tmp_path):
    service = _service(tmp_path, config=_config(min_timeout_seconds=0.1), scenario=SCENARIO_NEVER_COMPLETES)
    receipt = service.ask_chatgpt(source_session="test-1", prompt="hello", timeout_seconds=0.5,
                                  idempotency_key="k-timeout")
    assert receipt["state"] == BRIDGE_UNKNOWN
    assert receipt["error_stage"] == "RESPONSE_TIMEOUT"


# -- permission / validation ------------------------------------------------

def test_permission_denied_creates_no_row_at_all(tmp_path):
    service = _service(tmp_path, config=_config(ask_chatgpt=False))
    receipt = service.ask_chatgpt(source_session="test-1", prompt="hello", timeout_seconds=5,
                                  idempotency_key="k-denied")
    assert receipt == {"error": "ASK_CHATGPT_DISABLED"}
    assert service.store.get_by_idempotency_key("k-denied") is None
    assert service.transport.submit_calls == 0


def test_invalid_input_requires_exactly_one_of_session_or_binding(tmp_path):
    service = _service(tmp_path)
    both = service.ask_chatgpt(source_session="test-1", binding="b1", prompt="hi",
                               timeout_seconds=5, idempotency_key="k-both")
    neither = service.ask_chatgpt(prompt="hi", timeout_seconds=5, idempotency_key="k-neither")
    assert both["error"] == "INVALID_INPUT"
    assert neither["error"] == "INVALID_INPUT"


# -- depth / cycle ------------------------------------------------------------

def test_depth_exceeded_refuses_before_claim(tmp_path):
    service = _service(tmp_path, config=_config(depth=1))
    receipt = service.ask_chatgpt(source_session="test-1", prompt="hello", timeout_seconds=5,
                                  idempotency_key="k-depth", depth=2)
    assert receipt == {"error": "AGENT_BRIDGE_DEPTH_EXCEEDED", "depth": 2, "max_agent_bridge_depth": 1}
    assert service.store.get_by_idempotency_key("k-depth") is None


def test_cycle_detected_for_same_trace_and_session_while_non_terminal(tmp_path):
    service = _service(tmp_path, scenario=SCENARIO_NEVER_COMPLETES)
    # First call never reaches a terminal state within its own short
    # timeout window (SCENARIO_NEVER_COMPLETES) -- runs synchronously
    # inside ask_chatgpt() itself, so by the time it returns (UNKNOWN,
    # RESPONSE_TIMEOUT) it's already terminal. To exercise the cycle
    # check against a genuinely non-terminal row, claim one directly via
    # the store (simulating "still in flight" without needing a second
    # thread).
    service.store.claim(
        idempotency_key="k-inflight", source_session="test-1", binding=None, trace_id="trace-1",
        parent_turn_id=None, depth=0, allowed_tools=(), mode=None, model=None, effort=None,
        prompt="in flight", ttl_seconds=300,
    )
    receipt = service.ask_chatgpt(source_session="test-1", prompt="second ask", trace_id="trace-1",
                                  timeout_seconds=5, idempotency_key="k-new-different-key")
    assert receipt == {"error": "CYCLE_DETECTED", "trace_id": "trace-1"}
    assert service.transport.submit_calls == 0


def test_no_cycle_for_different_source_session_same_trace(tmp_path):
    # max_concurrent_turns raised above the default (1) -- this test is
    # about cycle detection, not the concurrency bound, and the directly-
    # claimed row below would otherwise occupy the only slot.
    service = _service(tmp_path, config=_config(max_concurrent_turns=2))
    service.store.claim(
        idempotency_key="k-inflight-2", source_session="test-1", binding=None, trace_id="trace-2",
        parent_turn_id=None, depth=0, allowed_tools=(), mode=None, model=None, effort=None,
        prompt="in flight", ttl_seconds=300,
    )
    receipt = service.ask_chatgpt(source_session="test-2", prompt="different session", trace_id="trace-2",
                                  timeout_seconds=5, idempotency_key="k-different-session")
    assert receipt["state"] == BRIDGE_COMPLETED  # not a cycle -- different source_session


# -- mode/model/effort: explicit, no silent fallback -------------------------

def test_omitted_mode_resolves_to_configured_default(tmp_path):
    service = _service(tmp_path, config=_config(default_mode="gpt-default"))
    receipt = service.ask_chatgpt(source_session="test-1", prompt="hi", timeout_seconds=5,
                                  idempotency_key="k-mode-default")
    assert receipt["mode"] == "gpt-default"


def test_unavailable_explicit_mode_fails_named_not_silently_substituted(tmp_path):
    service = _service(tmp_path, config=_config(allowed_modes=("a", "b")))
    receipt = service.ask_chatgpt(source_session="test-1", prompt="hi", mode="not-allowed",
                                  timeout_seconds=5, idempotency_key="k-mode-bad")
    assert receipt == {"error": "MODE_NOT_AVAILABLE", "mode": "not-allowed", "allowed": ["a", "b"]}
    assert service.store.get_by_idempotency_key("k-mode-bad") is None
    assert service.transport.submit_calls == 0


def test_explicit_allowed_mode_is_used_and_recorded(tmp_path):
    service = _service(tmp_path, config=_config(allowed_modes=("a", "b"), default_mode="a"))
    receipt = service.ask_chatgpt(source_session="test-1", prompt="hi", mode="b",
                                  timeout_seconds=5, idempotency_key="k-mode-explicit")
    assert receipt["mode"] == "b"


# -- bounded concurrency / queue ----------------------------------------------

def test_bounded_concurrency_queues_then_times_out(tmp_path):
    service = _service(tmp_path, config=_config(max_concurrent_turns=1, min_timeout_seconds=0.1))
    # Occupy the one slot with a still-non-terminal row (direct store
    # claim -- simulates a genuinely in-flight turn without needing a
    # second thread/process).
    service.store.claim(
        idempotency_key="k-occupies-slot", source_session="test-1", binding=None, trace_id=None,
        parent_turn_id=None, depth=0, allowed_tools=(), mode=None, model=None, effort=None,
        prompt="occupying", ttl_seconds=300,
    )
    started = time.monotonic()
    receipt = service.ask_chatgpt(source_session="test-2", prompt="queued", timeout_seconds=0.6,
                                  idempotency_key="k-queued")
    elapsed = time.monotonic() - started
    assert receipt == {"error": "QUEUE_TIMEOUT", "max_concurrent_turns": 1}
    assert elapsed >= 0.5  # actually waited, not an instant refusal
    assert service.transport.submit_calls == 0


def test_bounded_concurrency_proceeds_once_slot_frees(tmp_path):
    service = _service(tmp_path, config=_config(max_concurrent_turns=1))
    row, _ = service.store.claim(
        idempotency_key="k-frees", source_session="test-1", binding=None, trace_id=None,
        parent_turn_id=None, depth=0, allowed_tools=(), mode=None, model=None, effort=None,
        prompt="will free", ttl_seconds=300,
    )
    service.store.cas_update(row["bridge_turn_id"], expected_state=row["state"], state=BRIDGE_CANCELLED)
    service.store.revoke(row["bridge_turn_id"])
    receipt = service.ask_chatgpt(source_session="test-2", prompt="now fits", timeout_seconds=5,
                                  idempotency_key="k-fits-now")
    assert receipt["state"] == BRIDGE_COMPLETED


# -- capability expiry / explicit cancel --------------------------------------

def test_sweep_expired_cancels_and_revokes_stale_claim(tmp_path):
    service = _service(tmp_path)
    row, _ = service.store.claim(
        idempotency_key="k-stale", source_session="test-1", binding=None, trace_id=None,
        parent_turn_id=None, depth=0, allowed_tools=(), mode=None, model=None, effort=None,
        prompt="stale", ttl_seconds=-1,  # already expired at claim time
    )
    swept = service.sweep_expired()
    assert swept == [row["bridge_turn_id"]]
    final = service.store.get(row["bridge_turn_id"])
    assert final["state"] == BRIDGE_CANCELLED
    assert final["error_stage"] == "CAPABILITY_EXPIRED"
    assert final["revoked_at"] is not None
    # Idempotent: a second sweep finds nothing left to do.
    assert service.sweep_expired() == []


def test_explicit_cancel_is_idempotent_and_calls_transport_cancel_once(tmp_path):
    service = _service(tmp_path)
    row, _ = service.store.claim(
        idempotency_key="k-cancel-me", source_session="test-1", binding=None, trace_id=None,
        parent_turn_id=None, depth=0, allowed_tools=(), mode=None, model=None, effort=None,
        prompt="cancel me", ttl_seconds=300,
    )
    service.store.cas_update(row["bridge_turn_id"], expected_state=row["state"], state=BRIDGE_ACTIVATING)
    first = service.cancel_turn(row["bridge_turn_id"])
    assert first["state"] == BRIDGE_CANCELLED
    second = service.cancel_turn(row["bridge_turn_id"])
    assert second == first  # idempotent -- same terminal receipt, not an error
    assert service.transport.cancel_calls == [row["bridge_turn_id"]]  # exactly once


def test_cancel_unknown_turn_id_is_not_found(tmp_path):
    service = _service(tmp_path)
    assert service.cancel_turn("does-not-exist") == {"error": "BRIDGE_TURN_NOT_FOUND"}


# -- ownership / response binding ---------------------------------------------

def test_response_only_readable_by_the_owning_source_session(tmp_path):
    service = _service(tmp_path)
    receipt = service.ask_chatgpt(source_session="test-owner", prompt="hi", timeout_seconds=5,
                                  idempotency_key="k-owned")
    turn_id = receipt["bridge_turn_id"]
    owner_read = service.get_turn(turn_id, source_session="test-owner")
    assert owner_read["bridge_turn_id"] == turn_id
    stranger_read = service.get_turn(turn_id, source_session="test-someone-else")
    assert stranger_read == {"error": "FORBIDDEN"}


# -- tool round-trip allowlist -------------------------------------------------

def test_tool_allowlist_enforced_and_send_keys_never_eligible(tmp_path):
    config = _config(round_trip_allowed_tools=("terminal_tail", "terminal_send_keys"))
    # AskChatGptConfig.__post_init__ already strips terminal_send_keys at
    # load time -- confirm that landed, then confirm check_tool_allowed
    # enforces the same exclusion independently too (belt-and-suspenders).
    assert "terminal_send_keys" not in config.ask_chatgpt.round_trip_allowed_tools
    service = _service(tmp_path, config=config)
    receipt = service.ask_chatgpt(source_session="test-1", prompt="hi", timeout_seconds=5,
                                  idempotency_key="k-tools")
    turn_id = receipt["bridge_turn_id"]
    assert service.check_tool_allowed(turn_id, "terminal_tail") is True
    assert service.check_tool_allowed(turn_id, "terminal_send_keys") is False
    assert service.check_tool_allowed(turn_id, "terminal_delete_session") is False  # not in the allowlist at all


def test_empty_allowlist_by_default_permits_no_tool(tmp_path):
    service = _service(tmp_path)
    receipt = service.ask_chatgpt(source_session="test-1", prompt="hi", timeout_seconds=5,
                                  idempotency_key="k-no-tools")
    assert service.check_tool_allowed(receipt["bridge_turn_id"], "terminal_tail") is False


# -- secrets never logged ------------------------------------------------------

def test_secret_shaped_prompt_never_persisted_only_hash_and_redacted_preview(tmp_path):
    service = _service(tmp_path)
    secret_prompt = "please use OPENAI_API_KEY=sk-super-secret-value-do-not-leak for this"
    service.ask_chatgpt(source_session="test-1", prompt=secret_prompt, timeout_seconds=5,
                        idempotency_key="k-secret")
    row = service.store.get_by_idempotency_key("k-secret")
    assert "sk-super-secret-value-do-not-leak" not in row["prompt_preview"]
    assert "sk-super-secret-value-do-not-leak" not in str(row.values())
    assert row["prompt_sha256"] and row["prompt_sha256"] != secret_prompt


def test_response_text_never_persisted_only_hash_length_preview(tmp_path):
    service = _service(tmp_path, response_text="the actual secret-shaped response OPENAI_API_KEY=sk-abc123")
    receipt = service.ask_chatgpt(source_session="test-1", prompt="hi", timeout_seconds=5,
                                  idempotency_key="k-resp-secret")
    row = service.store.get(receipt["bridge_turn_id"])
    assert "sk-abc123" not in str(row.values())
    assert row["response_sha256"] is not None
    assert row["response_length"] == len("the actual secret-shaped response OPENAI_API_KEY=sk-abc123")


# -- delivery (deliver_to) -----------------------------------------------------

def test_deliver_to_without_configured_callback_fails_closed(tmp_path):
    service = _service(tmp_path)
    receipt = service.ask_chatgpt(source_session="test-1", prompt="hi", timeout_seconds=5,
                                  idempotency_key="k-deliver-none", deliver_to={"session": "target-1"})
    assert receipt["state"] == BRIDGE_COMPLETED  # the ChatGPT-side call itself still succeeded
    assert receipt["delivery"] == {"error": "DELIVERY_NOT_CONFIGURED"}
    assert receipt["response_text"]  # response is never lost even though delivery failed closed


def test_deliver_to_with_callback_receives_incremented_depth_and_trace(tmp_path):
    calls = []

    def fake_deliver(target, text, origin):
        calls.append((target, text, origin))
        return {"sent": True}

    service = _service(tmp_path)
    service.deliver_callback = fake_deliver
    receipt = service.ask_chatgpt(source_session="test-1", prompt="hi", trace_id="trace-9", depth=0,
                                  timeout_seconds=5, idempotency_key="k-deliver-yes",
                                  deliver_to={"session": "target-1"})
    assert receipt["delivery"] == {"sent": True}
    assert len(calls) == 1
    target, text, origin = calls[0]
    assert target == {"session": "target-1"}
    assert text == "mock response"
    assert origin.origin == "ask_chatgpt"
    assert origin.trace_id == "trace-9"
    assert origin.depth == 1  # incremented, never reset -- SubmissionOrigin.child()'s own contract
