"""QueueEngine.tick -- the Phase 2 dispatch loop (task: "Supervisor
Queue v2 Phase 2 -- Coordinator Agent"). Uses a FAKE SessionOps (no real
tmux/ConPTY session) to test the engine's own reconciliation logic in
isolation, deterministically and fast -- the real end-to-end proof
(actual tmux sessions, actual core.py TerminalService/idempotency store)
is test_queue_engine_smoke.py.

SAFETY: every session name below is a disposable fixture -- never
`window`/`window2`."""
from __future__ import annotations

import pytest

from terminal_mcp.coordinator import CoordinatorGate, RepoEvidence
from terminal_mcp.queue_engine import QueueEngine, idempotency_key_for
from terminal_mcp.queue_store import BLOCKED, COMPLETED, PAUSED, QUEUED, QueueStore, RUNNING, VERIFYING


class FakeOps:
    """A minimal, controllable stand-in for ControllerService. Each
    session name maps to a CURRENT terminal_status/terminal_capture
    response (set_status/set_capture overwrite it; terminal_status/
    terminal_capture just read whatever is current -- no queue/pop
    semantics to get subtly wrong) and a captured log of every
    terminal_send_text call -- tests assert against that log to prove
    dispatch/idempotency behavior, the same way a real core.py
    TerminalService's idempotent_sends store would."""

    def __init__(self):
        self.status_by_session: dict[str, dict] = {}
        self.capture_by_session: dict[str, dict] = {}
        self.sent: list[dict] = []
        self._sent_by_key: dict[str, dict] = {}

    def set_status(self, session, response):
        self.status_by_session[session] = response

    def set_capture(self, session, response):
        self.capture_by_session[session] = response

    def terminal_status(self, session):
        return self.status_by_session.get(session, {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})

    def terminal_tail(self, session, lines=None):
        return self.capture_by_session.get(session, {"output": ""})

    def terminal_send_text(self, session, text, press_enter=False, dry_run=False, **kwargs):
        idempotency_key = kwargs.get("idempotency_key")
        if idempotency_key and idempotency_key in self._sent_by_key:
            return self._sent_by_key[idempotency_key]  # real core.py behavior: return the ORIGINAL result
        self.sent.append({"session": session, "text": text, "idempotency_key": idempotency_key})
        response = {"sent": True, "delivery_state": "SUBMIT_CONFIRMED", "node_id": "local"}
        if idempotency_key:
            self._sent_by_key[idempotency_key] = response
        return response


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


@pytest.fixture
def ops():
    return FakeOps()


def _always_ready_gate():
    return CoordinatorGate(evidence_collector=lambda cwd: RepoEvidence(branch="main", head="x", clean=True,
                                                                       status_lines=()))


def _make_task(store, session="lane-a", prompt="please do the real work carefully"):
    (task_id,) = store.set_tasks(session, [{"prompt": prompt}])
    return task_id


# ---------------------------------------------------------------------------
# IDLE / PAUSED
# ---------------------------------------------------------------------------

def test_tick_on_an_empty_lane_is_idle(store, ops):
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())
    result = engine.tick("lane-a")
    assert result.action == "IDLE"


def test_tick_on_a_paused_lane_never_claims(store, ops):
    _make_task(store)
    store.pause_lane("lane-a", reason="test")
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())
    result = engine.tick("lane-a")
    assert result.action == "PAUSED"
    assert store.get_task(store.lane_status("lane-a")["tasks"][0]["id"]).status == QUEUED


# ---------------------------------------------------------------------------
# Claim -> review -> dispatch -> running -> completed, one tick at a time.
# ---------------------------------------------------------------------------

def test_full_happy_path_one_tick_at_a_time(store, ops):
    task_id = _make_task(store)
    ops.set_status("lane-a", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())

    claim = engine.tick("lane-a")
    assert claim.action == "CLAIMED"
    assert store.get_task(task_id).status == "PRECHECK"

    review = engine.tick("lane-a")
    assert review.action == "COORDINATOR_READY"
    assert store.get_task(task_id).status == "READY"

    dispatch = engine.tick("lane-a")
    assert dispatch.action == "DISPATCHED"
    assert store.get_task(task_id).status == RUNNING
    assert len(ops.sent) == 1
    assert "please do the real work carefully" in ops.sent[0]["text"]
    # The completion-marker wrapper is appended, never replacing the
    # verbatim prompt (item 7).
    assert "TERMINAL_MCP_COMPLETION" in ops.sent[0]["text"]

    # Still running.
    ops.set_status("lane-a", {"state": "RUNNING", "node_id": "local", "cwd": "/repo/a"})
    still_running = engine.tick("lane-a")
    assert still_running.action == "RUNNING"
    assert store.get_task(task_id).status == RUNNING

    # Goes quiet -> VERIFYING.
    ops.set_status("lane-a", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})
    verifying = engine.tick("lane-a")
    assert verifying.action == "VERIFYING"
    assert store.get_task(task_id).status == VERIFYING

    # No marker yet -- stays in VERIFYING, does NOT complete on a bare heuristic.
    ops.set_capture("lane-a", {"output": "still thinking, no marker here"})
    ops.set_status("lane-a", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})
    awaiting = engine.tick("lane-a")
    assert awaiting.action == "AWAITING_VERIFICATION"
    assert store.get_task(task_id).status == VERIFYING

    # Now the real completion marker appears, with the correct
    # task_id/attempt/nonce -- verified, COMPLETED.
    task = store.get_task(task_id)
    marker_line = (
        f"###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 task_id={task.id} "
        f"attempt={task.attempt_count} nonce={task.verification_nonce} status=completion_candidate "
        f"summary_sha256=deadbeef###"
    )
    ops.set_capture("lane-a", {"output": f"some progress\n{marker_line}\n"})
    ops.set_status("lane-a", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})
    completed = engine.tick("lane-a")
    assert completed.action == "COMPLETED"
    final = store.get_task(task_id)
    assert final.status == COMPLETED
    assert final.verification_evidence  # real evidence attached, not just the label


def test_completion_marker_with_wrong_nonce_is_never_verified(store, ops):
    task_id = _make_task(store)
    ops.set_status("lane-a", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())
    engine.tick("lane-a")  # CLAIMED
    engine.tick("lane-a")  # COORDINATOR_READY
    engine.tick("lane-a")  # DISPATCHED -> RUNNING
    ops.set_status("lane-a", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})
    engine.tick("lane-a")  # -> VERIFYING

    task = store.get_task(task_id)
    forged_marker = (
        f"###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 task_id={task.id} "
        f"attempt={task.attempt_count} nonce=WRONG-NONCE status=completion_candidate summary_sha256=x###"
    )
    ops.set_capture("lane-a", {"output": forged_marker})
    ops.set_status("lane-a", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})
    result = engine.tick("lane-a")
    assert result.action == "AWAITING_VERIFICATION"  # never trusted
    assert store.get_task(task_id).status == VERIFYING


# ---------------------------------------------------------------------------
# Coordinator NEEDS_REWORK / NEEDS_HUMAN / BLOCKED wiring.
# ---------------------------------------------------------------------------

def test_coordinator_needs_human_pauses_the_lane_and_stops_dispatch(store, ops):
    task_id = _make_task(store, prompt="fix it")  # too short -- default scope reasoner flags it
    ops.set_status("lane-a", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})
    engine = QueueEngine(store, ops)  # default CoordinatorGate, default scope reasoner
    engine.tick("lane-a")  # CLAIMED
    result = engine.tick("lane-a")  # review
    assert result.action == "COORDINATOR_NEEDS_HUMAN"
    task = store.get_task(task_id)
    assert task.status == PAUSED
    assert store.lane_status("lane-a")["paused"] is True
    assert len(ops.sent) == 0  # never dispatched


def test_a_blocked_task_stops_only_its_own_lane(store, ops):
    task_a = _make_task(store, session="lane-a", prompt="rm -rf /tmp/whatever now please")
    task_b = _make_task(store, session="lane-b", prompt="a perfectly normal harmless task here")
    ops.set_status("lane-a", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})
    ops.set_status("lane-b", {"state": "IDLE", "node_id": "local", "cwd": "/repo/b"})
    # Real (default) sensitive-pattern/scope checks, but a fake git
    # evidence collector -- /repo/a and /repo/b aren't real git repos on
    # this test host, and that's not what this test is about.
    engine = QueueEngine(store, ops, coordinator=CoordinatorGate(
        evidence_collector=lambda cwd: RepoEvidence(branch="main", head="x", clean=True, status_lines=())))

    engine.tick("lane-a")
    engine.tick("lane-a")  # -> NEEDS_HUMAN, lane-a paused
    engine.tick("lane-b")
    review_b = engine.tick("lane-b")
    assert review_b.action == "COORDINATOR_READY"
    assert store.lane_status("lane-a")["paused"] is True
    assert store.lane_status("lane-b")["paused"] is False


# ---------------------------------------------------------------------------
# Send failure -> BLOCKED.
# ---------------------------------------------------------------------------

def test_send_error_response_blocks_the_task(store, ops):
    task_id = _make_task(store)
    ops.set_status("lane-a", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})

    class ErrorOps(FakeOps):
        def terminal_send_text(self, session, text, press_enter=False, dry_run=False, **kwargs):
            return {"error": "ACCESS_DENIED"}

    error_ops = ErrorOps()
    error_ops.status_by_session = ops.status_by_session
    engine = QueueEngine(store, error_ops, coordinator=_always_ready_gate())
    engine.tick("lane-a")  # CLAIMED
    engine.tick("lane-a")  # READY
    result = engine.tick("lane-a")  # dispatch attempt fails
    assert result.action == "BLOCKED"
    assert store.get_task(task_id).status == BLOCKED


# ---------------------------------------------------------------------------
# DELIVERY_UNKNOWN -> reconcile, never resend blindly (item 9).
# ---------------------------------------------------------------------------

def test_delivery_unknown_reconciles_to_queued_not_a_resend(store, ops):
    task_id = _make_task(store)
    ops.set_status("lane-a", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})

    class UnknownOps(FakeOps):
        def terminal_send_text(self, session, text, press_enter=False, dry_run=False, **kwargs):
            self.sent.append({"session": session, "text": text})
            return {"sent": True, "delivery_state": "DELIVERY_UNKNOWN"}

    unknown_ops = UnknownOps()
    unknown_ops.status_by_session = ops.status_by_session
    engine = QueueEngine(store, unknown_ops, coordinator=_always_ready_gate())
    engine.tick("lane-a")
    engine.tick("lane-a")
    result = engine.tick("lane-a")
    assert result.action == "SUBMIT_UNKNOWN"
    assert store.get_task(task_id).status == QUEUED
    assert len(unknown_ops.sent) == 1  # exactly one send attempt, no automatic resend within this tick


# ---------------------------------------------------------------------------
# Idempotency across a simulated restart -- a NEW engine/store re-dispatching
# the same claimed task never sends twice for the same attempt.
# ---------------------------------------------------------------------------

def test_idempotency_key_is_stable_for_the_same_attempt(store, ops):
    task_id = _make_task(store)
    task = store.get_task(task_id)
    key1 = idempotency_key_for(task_id, 1)
    key2 = idempotency_key_for(task_id, 1)
    assert key1 == key2
    key_different_attempt = idempotency_key_for(task_id, 2)
    assert key_different_attempt != key1


def test_reconciled_stale_dispatch_reuses_the_same_idempotency_key_and_never_double_sends(store, ops):
    """The exact scenario item B's 'restart controller/queue worker giữa
    chừng không duplicate dispatch' is about: a task reaches DISPATCHING
    (send already attempted, outcome uncertain from the engine's own
    point of view) and then the process is simulated to have crashed --
    reconcile_stale_claims pushes it back to QUEUED. A brand NEW engine
    re-claims and re-dispatches it; the SAME idempotency_key must be
    used both times (a real core.py TerminalService would then dedupe
    the second one via its own idempotent_sends store)."""
    task_id = _make_task(store)
    ops.set_status("lane-a", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())
    engine.tick("lane-a")  # CLAIMED
    engine.tick("lane-a")  # COORDINATOR_READY
    engine.tick("lane-a")  # DISPATCHED -> RUNNING
    first_key = ops.sent[0]["idempotency_key"]
    task_after_first_send = store.get_task(task_id)
    assert task_after_first_send.dispatch_idempotency_key == first_key

    # Simulate a crash: force the task's row straight back to DISPATCHING
    # with an already-expired lease (as if the engine died between "sent"
    # and committing the RUNNING transition -- a state no *public* API
    # can reach directly, since RUNNING has no outgoing edge back to
    # DISPATCHING; a raw SQL update is the honest way to simulate an
    # external crash mid-transition here). reconcile_stale_claims is the
    # real recovery path for exactly this.
    with store._connection() as connection:
        connection.execute("UPDATE queue_tasks SET status = 'DISPATCHING', lease_expires_at = '2000-01-01T00:00:00Z' "
                          "WHERE id = ?", (task_id,))
    reconciled = store.reconcile_stale_claims("lane-a")
    assert task_id in reconciled
    assert store.get_task(task_id).status == QUEUED
    assert store.get_task(task_id).dispatch_idempotency_key == first_key  # STICKY -- not cleared

    # A brand new engine instance (simulating a fresh process) re-claims
    # and re-dispatches.
    engine2 = QueueEngine(store, ops, coordinator=_always_ready_gate())
    engine2.tick("lane-a")  # CLAIMED
    engine2.tick("lane-a")  # COORDINATOR_READY
    engine2.tick("lane-a")  # DISPATCHED again

    # FakeOps.terminal_send_text itself mimics core.py's real dedup
    # behavior (a repeat call with the same idempotency_key returns the
    # ORIGINALLY stored result instead of registering as a new send) --
    # so `sent` staying at length 1 here is the actual proof: the
    # engine presented the IDENTICAL key both times, so the real
    # send-layer never saw a second distinct attempt to act on. This is
    # exactly what a real core.py TerminalService's own idempotent_sends
    # store guarantees for the real MCP tool path (test_p0_hardening.py's
    # own idempotency-key regression tests prove that side; this test
    # proves the engine always presents the same key across a reconcile-
    # and-reclaim cycle, which is the precondition those guarantees rely
    # on).
    assert len(ops.sent) == 1
    assert store.get_task(task_id).status == RUNNING
    final_task = store.get_task(task_id)
    assert final_task.dispatch_idempotency_key == first_key
