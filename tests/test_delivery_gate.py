"""PROMPT DELIVERY / ACCEPTANCE GATE -- delivery_gate.py and its enforcement
in queue_engine._dispatch.

These are the regression tests for the six failure modes the rule exists to
prevent. Each one is written against a REAL QueueStore (real sqlite, real
transitions) with a scripted SessionOps double for the send receipt, because
the thing under test is the DECISION made from a receipt -- not tmux.

The central defect being locked down: both audited send paths used a
DENYLIST ("advance unless I recognise a specific failure"), so any receipt
state they did not enumerate meant "delivered". The gate inverts that to a
positive allowlist.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from terminal_mcp import delivery_gate
from terminal_mcp.adapters import (DELIVERY_BLOCKED, DELIVERY_ERROR, DELIVERY_SUBMIT_CONFIRMED,
                                   DELIVERY_TEXT_SENT, DELIVERY_UNKNOWN, TARGET_COMPOSER,
                                   TARGET_RUNNING, TARGET_WAITING)
from terminal_mcp.config import PromptDeliveryConfig
from terminal_mcp.queue_engine import QueueEngine
from terminal_mcp.queue_store import (BLOCKED, DISPATCH_UNCERTAIN, QUEUED, READY, RUNNING,
                                      WAITING_SESSION, InvalidTransitionError, QueueStore)


# -- gate 1: activation, as a positive allowlist -------------------------

def test_only_submit_confirmed_is_an_activation_pass():
    kind, reason, _ = delivery_gate.classify_activation(
        {"delivery_state": DELIVERY_SUBMIT_CONFIRMED, "press_enter": True})
    assert (kind, reason) == (delivery_gate.DELIVERED, delivery_gate.ACTIVATION_CONFIRMED)


@pytest.mark.parametrize("state,expected_kind", [
    (DELIVERY_UNKNOWN, delivery_gate.UNCERTAIN),
    (DELIVERY_TEXT_SENT, delivery_gate.REFUSED),
    (DELIVERY_BLOCKED, delivery_gate.REFUSED),
    (DELIVERY_ERROR, delivery_gate.REFUSED),
])
def test_no_other_known_state_passes_activation(state, expected_kind):
    kind, _, _ = delivery_gate.classify_activation({"delivery_state": state, "press_enter": True})
    assert kind == expected_kind
    assert kind != delivery_gate.DELIVERED


def test_an_unrecognised_delivery_state_is_never_delivered():
    """The whole point of the allowlist. adapters.py documents
    DELIVERY_STATES as extensible; under the old denylists a new state fell
    through to RUNNING."""
    kind, reason, _ = delivery_gate.classify_activation(
        {"delivery_state": "SOME_FUTURE_STATE", "press_enter": True})
    assert kind == delivery_gate.UNCERTAIN
    assert reason == delivery_gate.ACTIVATION_STATE_UNRECOGNISED


def test_a_receipt_with_no_delivery_state_is_never_delivered():
    kind, reason, _ = delivery_gate.classify_activation({"sent": True})
    assert kind == delivery_gate.UNCERTAIN
    assert reason == delivery_gate.ACTIVATION_MISSING
    assert delivery_gate.classify_activation({})[0] != delivery_gate.DELIVERED
    assert delivery_gate.classify_activation(None)[0] == delivery_gate.REFUSED



def test_submit_confirmed_with_enter_sent_false_is_refused():
    kind, reason, detail = delivery_gate.classify_activation(
        {"delivery_state": DELIVERY_SUBMIT_CONFIRMED, "press_enter": True,
         "enter_sent": False})
    assert kind == delivery_gate.REFUSED
    assert reason == delivery_gate.ACTIVATION_REFUSED
    assert "enter_sent=False" in detail

def test_confirmed_alongside_an_error_fails_closed():
    kind, reason, _ = delivery_gate.classify_activation(
        {"delivery_state": DELIVERY_SUBMIT_CONFIRMED, "error": "PANE_BUSY"})
    assert kind == delivery_gate.UNCERTAIN
    assert reason == delivery_gate.SEND_ERROR


# -- scenario 1: the prompt is sitting in the input box, unsubmitted -----

def test_prompt_left_in_the_input_box_is_refused_not_reported_delivered():
    """press_enter was requested but Enter never went out: the text is in
    the composer. This must never read as delivered, and it is the one case
    that IS safe to re-attempt under the same idempotency key."""
    verdict = delivery_gate.evaluate(
        {"delivery_state": DELIVERY_TEXT_SENT, "press_enter": True, "sent": True,
         "correlation_id": "abc"})
    assert verdict.kind == delivery_gate.REFUSED
    assert verdict.activation == delivery_gate.ACTIVATION_TEXT_ONLY
    assert verdict.may_advance is False
    assert verdict.safe_to_retry is True
    assert "input box" in verdict.detail


def test_submit_confirmed_but_prompt_still_visible_in_composer_is_not_accepted():
    """Gate 2's headline case: Enter was processed and the pane redrew, yet
    the target is still showing a composer and nothing advanced."""
    lines = ["> my prompt text", ""]
    verdict = delivery_gate.evaluate(
        {"delivery_state": DELIVERY_SUBMIT_CONFIRMED, "press_enter": True},
        before_lines=lines, after_lines=lines, target_state=TARGET_COMPOSER)
    assert verdict.kind == delivery_gate.NOT_ACCEPTED
    assert verdict.acceptance == delivery_gate.ACCEPTANCE_PROMPT_STILL_IN_COMPOSER
    assert verdict.may_advance is False
    assert verdict.safe_to_retry is False  # a resend would duplicate a confirmed submit


# -- scenario 2: DELIVERY_UNKNOWN ---------------------------------------

def test_delivery_unknown_is_uncertain_and_not_retry_safe():
    verdict = delivery_gate.evaluate({"delivery_state": DELIVERY_UNKNOWN, "press_enter": True})
    assert verdict.kind == delivery_gate.UNCERTAIN
    assert verdict.may_advance is False
    assert verdict.safe_to_retry is False
    assert "terminal_input_context" in verdict.detail


def test_unknown_skips_the_acceptance_check_entirely():
    """There is nothing to accept when activation is unproven -- and
    checking would invite reading a half-submitted state as progress."""
    verdict = delivery_gate.evaluate(
        {"delivery_state": DELIVERY_UNKNOWN}, after_lines=["working..."],
        target_state=TARGET_RUNNING)
    assert verdict.kind == delivery_gate.UNCERTAIN
    assert verdict.acceptance == delivery_gate.ACCEPTANCE_NOT_CHECKED


# -- scenario 3: acceptance evidence ------------------------------------

def test_acceptance_passes_when_the_target_is_working():
    verdict = delivery_gate.evaluate(
        {"delivery_state": DELIVERY_SUBMIT_CONFIRMED}, before_lines=["> x"],
        after_lines=["thinking..."], target_state=TARGET_RUNNING)
    assert verdict.kind == delivery_gate.DELIVERED
    assert delivery_gate.ACCEPTANCE_TARGET_WORKING in verdict.evidence
    assert verdict.may_advance is True


def test_acceptance_passes_on_genuine_output_advance():
    verdict = delivery_gate.evaluate(
        {"delivery_state": DELIVERY_SUBMIT_CONFIRMED},
        before_lines=["> x"], after_lines=["> x", "reading files"])
    assert verdict.kind == delivery_gate.DELIVERED
    assert delivery_gate.ACCEPTANCE_OUTPUT_ADVANCED in verdict.evidence


def test_a_target_waiting_on_a_human_is_not_acceptance():
    """The target is blocked on a person, not working on our prompt."""
    verdict = delivery_gate.evaluate(
        {"delivery_state": DELIVERY_SUBMIT_CONFIRMED},
        before_lines=["a"], after_lines=["Do you want to proceed? (y/n)"],
        target_state=TARGET_WAITING)
    assert verdict.kind == delivery_gate.NOT_ACCEPTED
    assert verdict.acceptance == delivery_gate.ACCEPTANCE_TARGET_AWAITING_HUMAN


def test_an_unobservable_target_is_never_an_acceptance_pass():
    verdict = delivery_gate.evaluate({"delivery_state": DELIVERY_SUBMIT_CONFIRMED},
                                     after_lines=None)
    assert verdict.kind == delivery_gate.NOT_ACCEPTED
    assert verdict.acceptance == delivery_gate.ACCEPTANCE_UNOBSERVABLE


def test_adapter_ack_counts_as_acceptance():
    class _Ack:
        def submit_ack_evidence(self, before, after, sent_text):
            return True

    verdict = delivery_gate.evaluate({"delivery_state": DELIVERY_SUBMIT_CONFIRMED},
                                     before_lines=["a"], after_lines=["a"], adapter=_Ack())
    assert verdict.kind == delivery_gate.DELIVERED
    assert delivery_gate.ACCEPTANCE_ADAPTER_ACK in verdict.evidence


def test_a_raising_adapter_cannot_break_the_gate():
    class _Boom:
        def submit_ack_evidence(self, before, after, sent_text):
            raise RuntimeError("adapter bug")

    verdict = delivery_gate.evaluate({"delivery_state": DELIVERY_SUBMIT_CONFIRMED},
                                     before_lines=["a"], after_lines=["a"], adapter=_Boom())
    assert verdict.kind == delivery_gate.NOT_ACCEPTED  # degrades, never crashes


def test_require_acceptance_false_reproduces_pre_gate_behaviour():
    verdict = delivery_gate.evaluate({"delivery_state": DELIVERY_SUBMIT_CONFIRMED},
                                     require_acceptance=False)
    assert verdict.kind == delivery_gate.DELIVERED
    assert verdict.acceptance == delivery_gate.ACCEPTANCE_NOT_CHECKED


# -- queue enforcement ---------------------------------------------------

class _Ops:
    """Scripted SessionOps: one queued send receipt, one status reply."""

    def __init__(self, receipt, status=None):
        self.receipt = receipt
        self.status = status or {"state": "IDLE", "last_output": "same"}
        self.sends = []

    def terminal_send_text(self, session, text, press_enter=False, dry_run=False,
                           idempotency_key=None, **kwargs):
        self.sends.append({"text": text, "press_enter": press_enter,
                           "idempotency_key": idempotency_key})
        return dict(self.receipt)

    def terminal_status(self, session):
        return dict(self.status)

    def terminal_tail(self, session, lines=None, **kwargs):
        return {"output": self.status.get("last_output", "")}


def _store():
    return QueueStore(Path(tempfile.mkdtemp()) / "queue.db")


def _ready_task(store, session="agent-gate"):
    created = store.append_tasks(session, [{"prompt": "do the thing", "title": "t"}])
    task_id = created[0]  # append_tasks returns ids, not task objects
    store.transition_task(task_id, "PRECHECK", event_type="CLAIMED")
    store.transition_task(task_id, READY, event_type="READY")
    return task_id


def _engine(store, ops, *, mode="advisory", require_acceptance=True):
    return QueueEngine(store, ops, delivery_policy=PromptDeliveryConfig(
        mode=mode, require_acceptance=require_acceptance))


def test_queue_does_not_reach_running_when_text_was_never_submitted():
    """Scenario 6 + the audited latent fail-open: a TEXT_SENT receipt with
    press_enter=True used to fall through the DELIVERY_UNKNOWN check
    straight into transition_task(RUNNING)."""
    store = _store()
    task_id = _ready_task(store)
    ops = _Ops({"delivery_state": DELIVERY_TEXT_SENT, "press_enter": True, "sent": True})
    result = _engine(store, ops)._dispatch("agent-gate", task_id)
    assert store.get_task(task_id).status == BLOCKED
    assert store.get_task(task_id).status != RUNNING
    assert result.action == "BLOCKED"


def test_queue_does_not_reach_running_on_an_unrecognised_delivery_state():
    store = _store()
    task_id = _ready_task(store)
    ops = _Ops({"delivery_state": "A_STATE_FROM_THE_FUTURE", "press_enter": True})
    _engine(store, ops)._dispatch("agent-gate", task_id)
    assert store.get_task(task_id).status == DISPATCH_UNCERTAIN


def test_delivery_unknown_still_becomes_dispatch_uncertain():
    """Pre-existing behaviour must be preserved exactly."""
    store = _store()
    task_id = _ready_task(store)
    ops = _Ops({"delivery_state": DELIVERY_UNKNOWN, "press_enter": True})
    _engine(store, ops)._dispatch("agent-gate", task_id)
    assert store.get_task(task_id).status == DISPATCH_UNCERTAIN


def test_advisory_mode_still_enforces_lifecycle_acceptance_gate():
    """Policy may be advisory, but task lifecycle truth is never advisory."""
    store = _store()
    task_id = _ready_task(store)
    ops = _Ops({"delivery_state": DELIVERY_SUBMIT_CONFIRMED, "press_enter": True},
               status={"state": "IDLE", "last_output": "unchanged"})
    _engine(store, ops, mode="advisory")._dispatch("agent-gate", task_id)
    task = store.get_task(task_id)
    assert task.status == DISPATCH_UNCERTAIN
    verdict = task.metadata["delivery_verdict"]
    assert verdict["kind"] == delivery_gate.NOT_ACCEPTED
    assert verdict["may_advance"] is False


def test_enforce_mode_holds_a_confirmed_submit_with_no_acceptance_evidence():
    """Scenario 5. The submit is real, so this is NOT a failure to send --
    it is held as uncertain for re-observation, never resent."""
    store = _store()
    task_id = _ready_task(store)
    ops = _Ops({"delivery_state": DELIVERY_SUBMIT_CONFIRMED, "press_enter": True},
               status={"state": "IDLE", "last_output": "unchanged"})
    _engine(store, ops, mode="enforce")._dispatch("agent-gate", task_id)
    task = store.get_task(task_id)
    assert task.status == DISPATCH_UNCERTAIN
    assert task.status != RUNNING
    assert task.metadata["delivery_verdict"]["acceptance"] in (
        delivery_gate.ACCEPTANCE_NOT_OBSERVED, delivery_gate.ACCEPTANCE_UNOBSERVABLE)


def test_codex_idle_composer_overrides_stale_running_status():
    store = _store()
    task_id = _ready_task(store)
    ops = _Ops({"delivery_state": DELIVERY_SUBMIT_CONFIRMED, "press_enter": True},
               status={"state": "RUNNING", "current_command": "codex",
                       "last_output": "› Ask Codex to do anything"})
    result = _engine(store, ops)._dispatch("agent-gate", task_id)
    assert result.action == "DISPATCH_UNCERTAIN"
    assert store.get_task(task_id).status == DISPATCH_UNCERTAIN


def test_enforce_mode_reaches_running_with_real_acceptance_evidence():
    store = _store()
    task_id = _ready_task(store)
    ops = _Ops({"delivery_state": DELIVERY_SUBMIT_CONFIRMED, "press_enter": True},
               status={"state": "RUNNING", "last_output": "working on it"})
    _engine(store, ops, mode="enforce")._dispatch("agent-gate", task_id)
    task = store.get_task(task_id)
    assert task.status == RUNNING
    assert task.metadata["delivery_verdict"]["kind"] == delivery_gate.DELIVERED


def test_exactly_one_enter_per_dispatch_no_duplicate_submit():
    """Scenario 3: duplicate Enter. The engine must send once, with
    press_enter=True, and never issue a second raw-Enter/send for the same
    dispatch -- spamming Enter is the specific thing forbidden for Claude."""
    store = _store()
    task_id = _ready_task(store)
    ops = _Ops({"delivery_state": DELIVERY_SUBMIT_CONFIRMED, "press_enter": True},
               status={"state": "RUNNING", "last_output": "go"})
    _engine(store, ops, mode="enforce")._dispatch("agent-gate", task_id)
    assert len(ops.sends) == 1
    assert ops.sends[0]["press_enter"] is True


def test_uncertain_dispatch_cannot_be_requeued_by_timeout():
    """Elapsed time cannot authorize a retry of a possibly accepted prompt."""
    store = _store()
    task_id = _ready_task(store)
    ops = _Ops({"delivery_state": DELIVERY_UNKNOWN, "press_enter": True})
    engine = _engine(store, ops)
    engine._dispatch("agent-gate", task_id)
    first_key = ops.sends[0]["idempotency_key"]
    assert store.get_task(task_id).status == DISPATCH_UNCERTAIN
    assert store.get_task(task_id).dispatch_idempotency_key == first_key

    with pytest.raises(InvalidTransitionError):
        store.transition_task(task_id, QUEUED, event_type="REQUEUED", reason="grace elapsed")


def test_the_verdict_is_recorded_on_every_dispatch_including_refusals():
    store = _store()
    task_id = _ready_task(store)
    ops = _Ops({"delivery_state": DELIVERY_TEXT_SENT, "press_enter": True})
    _engine(store, ops)._dispatch("agent-gate", task_id)
    metadata = store.get_task(task_id).metadata
    assert metadata["delivery_verdict"]["kind"] == delivery_gate.REFUSED
    assert metadata["delivery_verdict"]["activation"] == delivery_gate.ACTIVATION_TEXT_ONLY
    assert len(metadata["delivery_verdict_history"]) == 1


def test_verdict_history_is_bounded():
    store = _store()
    task_id = _ready_task(store)
    for _ in range(8):
        store.record_delivery_verdict(task_id, {"kind": delivery_gate.REFUSED})
    assert len(store.get_task(task_id).metadata["delivery_verdict_history"]) == 5


def test_an_unobservable_status_call_cannot_crash_dispatch():
    """A status probe that raises must degrade to UNOBSERVABLE, not take
    the dispatch down with it."""
    class _Boom(_Ops):
        def terminal_status(self, session):
            raise RuntimeError("tmux gone")

    store = _store()
    task_id = _ready_task(store)
    ops = _Boom({"delivery_state": DELIVERY_SUBMIT_CONFIRMED, "press_enter": True})
    result = _engine(store, ops, mode="enforce")._dispatch("agent-gate", task_id)
    assert result.action == "WAITING_SESSION"
    assert store.get_task(task_id).status == WAITING_SESSION


def test_send_errors_still_block_exactly_as_before():
    store = _store()
    task_id = _ready_task(store)
    ops = _Ops({"error": "PANE_BUSY", "delivery_state": DELIVERY_BLOCKED})
    _engine(store, ops)._dispatch("agent-gate", task_id)
    assert store.get_task(task_id).status == BLOCKED


# -- the contract itself -------------------------------------------------

def test_only_delivered_may_advance_a_task():
    for kind in delivery_gate.VERDICT_KINDS:
        verdict = delivery_gate.DeliveryVerdict(kind=kind, activation="x", acceptance="y")
        assert verdict.may_advance == (kind == delivery_gate.DELIVERED)


def test_only_refused_is_safe_to_retry():
    for kind in delivery_gate.VERDICT_KINDS:
        verdict = delivery_gate.DeliveryVerdict(kind=kind, activation="x", acceptance="y")
        assert verdict.safe_to_retry == (kind == delivery_gate.REFUSED)


def test_the_gate_module_never_sends_anything():
    """Structural: the decision layer must have no send/Enter primitive, so
    it can never become a retry loop."""
    import ast

    tree = ast.parse(Path(delivery_gate.__file__).read_text())
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    # Checked against real CALLS, not prose: the module's own docstrings
    # legitimately discuss press_enter/tmux, and a substring scan would
    # forbid explaining the very thing it enforces.
    for forbidden in ("send_text", "send_keys", "send", "run", "Popen", "check_output"):
        assert forbidden not in called, f"delivery_gate must not call {forbidden}()"
    assert "subprocess" not in {
        n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}
