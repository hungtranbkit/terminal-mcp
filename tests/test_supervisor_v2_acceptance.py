"""An autonomous chain may only advance on PROVED submission.

`execute_send` used to check `submit_status == "SUBMIT_UNCONFIRMED"` -- a
denylist. Two results slipped through it and were treated as success:

  TEXT_SENT      the text reached the composer and Enter's effect was never
                 established. `to_legacy_submit_status` deliberately preserves
                 this spelling instead of folding it into the unconfirmed
                 bucket, so a `!= "SUBMIT_UNCONFIRMED"` check reads it as a win.
  no field       a result shape the check had not met -- a short-circuit path, a
                 new transport, a refactor.

Both advanced the action to `observing` and incremented the auto-action count,
so the chain marched on after a prompt that may never have been submitted. This
consumes the transport's own verdict (adapters.is_submission_confirmed) rather
than reimplementing acceptance detection, which is m1's layer.
"""
from __future__ import annotations

import json

import pytest

from terminal_mcp.adapters import is_submission_confirmed


# == the predicate: a positive allowlist =================================

@pytest.mark.parametrize("result,expected", [
    ({"delivery_state": "SUBMIT_CONFIRMED"}, True),
    ({"submit_status": "SUBMIT_CONFIRMED"}, True),
    ({"delivery_state": "TEXT_SENT"}, False),
    ({"submit_status": "TEXT_SENT"}, False),
    ({"delivery_state": "DELIVERY_UNKNOWN"}, False),
    ({"delivery_state": "SUBMIT_STALLED"}, False),
    ({"delivery_state": "BLOCKED"}, False),
    ({"delivery_state": "ERROR"}, False),
    ({"submit_status": "SUBMIT_UNCONFIRMED"}, False),
    ({}, False),
    ({"sent": True}, False),
    ({"delivery_state": "SOME_FUTURE_STATE"}, False),
])
def test_only_a_confirmed_submission_counts_as_accepted(result, expected):
    assert is_submission_confirmed(result) is expected


def test_delivery_state_wins_over_the_legacy_field():
    """`submit_status` is derived from `delivery_state`, so when both are present
    the authoritative one decides. A result claiming a confirmed legacy status
    over a non-confirmed delivery state is contradictory and must not pass."""
    assert is_submission_confirmed(
        {"delivery_state": "DELIVERY_UNKNOWN", "submit_status": "SUBMIT_CONFIRMED"}) is False


def test_every_delivery_state_except_confirmed_is_unproven():
    """Pinned against the vocabulary itself, so a state added to adapters.py
    without thinking about autonomy defaults to 'not proved' rather than
    silently becoming an acceptance."""
    from terminal_mcp.adapters import DELIVERY_STATES, DELIVERY_SUBMIT_CONFIRMED

    for state in DELIVERY_STATES:
        confirmed = is_submission_confirmed({"delivery_state": state})
        assert confirmed is (state == DELIVERY_SUBMIT_CONFIRMED), state


# == the flow: what execute_send does with it =============================

class _Recorder:
    """Captures what execute_send did without running a real send."""

    def __init__(self, send_result):
        self.send_result = send_result
        self.updates: list[dict] = []
        self.blocked: list[tuple[str, str]] = []
        self.auto_action_increments = 0


def _wire(monkeypatch, v2, recorder, action, watch):
    """Drive execute_send's tail with a chosen send result.

    Patches the seams rather than building a real approved action end to end:
    what is under test is the branch that reads the send result, and a real
    send would need a live agent that can be made to answer TEXT_SENT on
    demand -- which is exactly the condition that has no reliable trigger."""
    monkeypatch.setattr(v2.config.__class__, "v2_enabled", property(lambda self: True), raising=False)
    monkeypatch.setattr(v2.store, "get_action", lambda action_id: dict(action))
    monkeypatch.setattr(v2, "_lease_valid", lambda a: True)
    monkeypatch.setattr(v2.v1.store, "get_watch", lambda key: dict(watch))

    def _cas(action_id, *, expected_state, **fields):
        recorder.updates.append({"expected_state": expected_state, **fields})
        return True

    monkeypatch.setattr(v2.store, "cas_update", _cas)
    monkeypatch.setattr(v2.store, "block_policy",
                        lambda key, reason: recorder.blocked.append((key, reason)))

    def _increment(key):
        recorder.auto_action_increments += 1
        return recorder.auto_action_increments

    monkeypatch.setattr(v2.store, "increment_auto_action_count", _increment)
    monkeypatch.setattr(v2.v1.terminal, "terminal_send_text",
                        lambda *a, **kw: dict(recorder.send_result))


@pytest.fixture
def v2(tmp_path):
    from terminal_mcp.config import (AppConfig, PermissionsConfig, SessionAccessConfig,
                                     SupervisorConfig)
    from terminal_mcp.core import TerminalService
    from terminal_mcp.supervisor import SupervisorService, SupervisorStore
    from terminal_mcp.supervisor2 import build_supervisor_v2

    config = AppConfig(PermissionsConfig(True, True), ("test-*",), 50, 20,
                       supervisor=SupervisorConfig(v2_enabled=True),
                       session_access=SessionAccessConfig(default_read=True, default_input=True))
    v1 = SupervisorService(TerminalService(config), SupervisorStore(tmp_path / "supervisor.db"))
    return build_supervisor_v2(v1)


ACTION = {"id": 1, "state": "approved", "watch_key": "session:test-x", "created_at": "2026-01-01T00:00:00",
          "proposed_prompt": "do the thing", "expected_output_hash": None}
WATCH = {"watch_key": "session:test-x", "kind": "session", "target": "test-x",
         "last_output_hash": "h1", "pinned_session_id": None, "pinned_pane_id": None,
         "pinned_created_epoch": None, "state": "IDLE"}


@pytest.mark.parametrize("send_result", [
    {"sent": True, "delivery_state": "TEXT_SENT", "submit_status": "TEXT_SENT"},
    {"sent": True, "delivery_state": "DELIVERY_UNKNOWN", "submit_status": "SUBMIT_UNCONFIRMED"},
    {"sent": True, "delivery_state": "SUBMIT_STALLED", "submit_status": "SUBMIT_UNCONFIRMED"},
    {"sent": True},
])
def test_an_unproven_submission_blocks_and_never_counts_as_an_auto_action(
        monkeypatch, v2, send_result):
    recorder = _Recorder(send_result)
    _wire(monkeypatch, v2, recorder, ACTION, WATCH)

    result = v2.execute_send(1)

    assert result["sent"] is True, "the text really was sent; that stays accurate"
    states = [u.get("state") for u in recorder.updates]
    assert "blocked" in states, f"an unproven submission advanced: {states}"
    assert "observing" not in states, "the chain advanced on unproven submission"
    assert recorder.auto_action_increments == 0, \
        "an unproven submission was counted as a successful auto-action"
    assert recorder.blocked and recorder.blocked[0][1] == "submit_unconfirmed"


def test_a_confirmed_submission_advances_the_chain_exactly_as_before(monkeypatch, v2):
    recorder = _Recorder({"sent": True, "delivery_state": "SUBMIT_CONFIRMED",
                          "submit_status": "SUBMIT_CONFIRMED"})
    _wire(monkeypatch, v2, recorder, ACTION, WATCH)

    result = v2.execute_send(1)

    assert result["sent"] is True
    states = [u.get("state") for u in recorder.updates]
    assert "observing" in states, f"a confirmed submission failed to advance: {states}"
    assert "blocked" not in states
    assert recorder.auto_action_increments == 1
    assert recorder.blocked == []


def test_the_failing_send_result_is_kept_for_diagnosis(monkeypatch, v2):
    """The stop_reason keeps its existing spelling, so the specific state that
    failed to confirm has to be recoverable from the stored result."""
    recorder = _Recorder({"sent": True, "delivery_state": "SUBMIT_STALLED",
                          "submit_status": "SUBMIT_UNCONFIRMED"})
    _wire(monkeypatch, v2, recorder, ACTION, WATCH)
    v2.execute_send(1)

    blocked_update = next(u for u in recorder.updates if u.get("state") == "blocked")
    stored = json.loads(blocked_update["send_result"])
    assert stored["delivery_state"] == "SUBMIT_STALLED"
