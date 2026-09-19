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
    # Living-requirements convention: the reminder is appended too, also
    # never touching the verbatim prompt (which appears FIRST, unmodified).
    assert "docs/REQUIREMENTS.md" in ops.sent[0]["text"]
    assert ops.sent[0]["text"].startswith("please do the real work carefully")

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
    assert result.action == "DISPATCH_UNCERTAIN"
    assert store.get_task(task_id).status == "DISPATCH_UNCERTAIN"
    assert len(unknown_ops.sent) == 1  # exactly one send attempt, no automatic resend within this tick

    # A later tick with the session still not showing real activity --
    # stays DISPATCH_UNCERTAIN (within its grace period), never resent.
    still_uncertain = engine.tick("lane-a")
    assert still_uncertain.action == "NO_OP"
    assert store.get_task(task_id).status == "DISPATCH_UNCERTAIN"
    assert len(unknown_ops.sent) == 1

    # After the grace period elapses, it safely falls back to QUEUED.
    with store._connection() as connection:
        connection.execute("UPDATE queue_tasks SET uncertain_or_waiting_since = '2000-01-01T00:00:00Z' "
                          "WHERE id = ?", (task_id,))
    reconciled = store.reconcile_uncertain_and_waiting("lane-a")
    assert task_id in reconciled
    assert store.get_task(task_id).status == QUEUED


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


# ---------------------------------------------------------------------------
# TMCP-RETRY-CONTEXT-002: a retry continues; it does not replay the prompt.
#
# The production failure: build_dispatch_text embedded task.prompt verbatim on
# EVERY attempt, so a retried long task looked exactly like a brand-new one to
# the agent, which re-planned and discarded an hour of reasoning.
# ---------------------------------------------------------------------------

PROMPT = "please do the real work carefully"


def _fail_then_retry(store, task_id):
    """A retry is only reachable from a real failure, so get there honestly
    rather than forcing an invalid RUNNING -> QUEUED transition."""
    store.transition_task(task_id, BLOCKED, event_type="BLOCKED", reason="simulated interruption")
    store.retry_task(task_id)

def _dispatch_once(store, ops, session="lane-a"):
    """Claim -> review -> dispatch, returning the text actually sent."""
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())
    for _ in range(4):
        result = engine.tick(session)
        if result.action == "DISPATCHED":
            return ops.sent[-1]["text"]
        if result.action.startswith("BLOCKED") or result.action == "IDLE":
            raise AssertionError(f"did not reach dispatch: {result.action} {result.detail}")
    raise AssertionError("dispatch never happened")


def test_first_attempt_still_sends_the_prompt_verbatim(store, ops):
    # Unchanged behaviour for a brand-new task -- the fix must not touch it.
    _make_task(store, prompt=PROMPT)
    ops.set_status("lane-a", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})
    text = _dispatch_once(store, ops)
    assert PROMPT in text


def test_retry_into_a_live_session_continues_instead_of_replaying_the_prompt(store, ops):
    task_id = _make_task(store, prompt=PROMPT)
    ops.set_status("lane-a", {"state": "IDLE", "exists": True, "node_id": "local", "cwd": "/repo/a"})
    _dispatch_once(store, ops)              # attempt 1 -- the real prompt
    _fail_then_retry(store, task_id)        # operator retry; attempt_count is now 1
    ops.sent.clear()

    text = _dispatch_once(store, ops)

    assert PROMPT not in text                               # (g) no replay
    assert "continue the SAME task, do not start over" in text
    assert f"task_id={task_id}" in text                     # same logical task
    assert "/clear" not in text                             # (b) no history destruction
    # The completion protocol still travels with a continued attempt.
    assert "###TERMINAL_MCP_COMPLETION" in text


def _plan_for(store, ops, task_id, status_response, session="lane-a"):
    """The retry plan the engine would use for THIS task's next dispatch.

    Exercised directly rather than through tick() because a task whose session
    is gone correctly never reaches _dispatch at all -- the coordinator parks it
    as WAITING_SESSION first, which is existing behaviour worth keeping. The
    escalation itself is still the engine's own, not a re-implementation.
    """
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())
    ops.set_status(session, status_response)
    task = store.get_task(task_id)
    return engine._retry_plan_for(task, session)


def _retryable_task(store, metadata=None, session="lane-a"):
    """A task that has already had one attempt, so the next dispatch is a retry."""
    (task_id,) = store.set_tasks(session, [{"prompt": PROMPT, "metadata": metadata or {}}])
    store.transition_task(task_id, "PRECHECK", event_type="CLAIMED")
    store.transition_task(task_id, "READY", event_type="COORDINATOR_APPROVED")
    store.transition_task(task_id, "DISPATCHING", event_type="DISPATCHED")
    store.transition_task(task_id, BLOCKED, event_type="BLOCKED", reason="simulated interruption")
    store.retry_task(task_id)
    return task_id


def test_retry_with_no_recoverable_context_plans_recovery_restart(store, ops):
    # RECOVERY_RESTART is reachable, and it is the only mode that replays --
    # _retry_plan_for reports None for it so the caller falls back to the prompt.
    task_id = _retryable_task(store)
    assert _plan_for(store, ops, task_id, {"error": "SESSION_NOT_FOUND"}) is None


def test_retry_uses_the_durable_capsule_when_the_conversation_is_gone(store, ops):
    task_id = _retryable_task(store, metadata={"recovery": {
        "agent_type": "shell",
        "capsule": {"completed_steps": ["step one done"], "next_step": "do step two",
                    "branch": "fix/lane", "tests_run": ["tests/test_x.py"]},
    }})
    plan = _plan_for(store, ops, task_id, {"error": "SESSION_NOT_FOUND"})
    assert plan is not None
    assert plan.mode == "RESUME_FROM_CHECKPOINT"
    assert PROMPT not in plan.continuation_text
    assert "step one done" in plan.continuation_text     # completed work preserved
    assert "do step two" in plan.continuation_text        # next step preserved
    assert "fix/lane" in plan.continuation_text
    assert plan.preserved["task_id"] == task_id


def test_retry_resumes_the_native_conversation_when_the_agent_died(store, ops):
    task_id = _retryable_task(store, metadata={"recovery": {
        "agent_type": "claude", "conversation_id": "conv-xyz"}})
    # The session exists but its agent is gone, so status cannot classify it.
    plan = _plan_for(store, ops, task_id, {"state": "UNKNOWN", "exists": True, "node_id": "local"})
    assert plan is not None
    assert plan.mode == "RESUME_NATIVE_CONVERSATION"
    assert plan.resume_conversation_id == "conv-xyz"
    assert plan.relaunch_agent is True
    assert plan.recreate_session is False       # the session survived; leave it alone
    assert PROMPT not in plan.continuation_text


def test_first_attempt_has_no_retry_plan_at_all(store, ops):
    (task_id,) = store.set_tasks("lane-a", [{"prompt": PROMPT}])
    assert _plan_for(store, ops, task_id, {"state": "IDLE", "exists": True}) is None


def test_retry_fact_gathering_failure_never_strands_the_task(store, ops):
    # A broken status lookup must degrade to today's behaviour, not refuse.
    task_id = _make_task(store, prompt=PROMPT)
    ops.set_status("lane-a", {"state": "IDLE", "exists": True, "node_id": "local", "cwd": "/repo/a"})
    _dispatch_once(store, ops)
    _fail_then_retry(store, task_id)

    class ExplodingStatus(type(ops)):
        def terminal_status(self, session):
            if self.sent:  # let the coordinator's own pre-dispatch read succeed
                raise RuntimeError("status backend down")
            return {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"}

    broken = ExplodingStatus()
    broken.status_by_session = dict(ops.status_by_session)
    text = _dispatch_once(store, broken)
    assert PROMPT in text  # fell back to replay rather than stranding the task


# ---------------------------------------------------------------------------
# Durable capsule WRITING, and native-resume EXECUTION.
# ---------------------------------------------------------------------------

def test_identity_is_persisted_before_the_send_not_after(store, ops):
    # After an agent dies the pane is gone, so anything not already written is
    # unrecoverable -- the snapshot has to happen before the dispatch.
    (task_id,) = store.set_tasks("lane-a", [{"prompt": PROMPT}])
    ops.set_status("lane-a", {"state": "IDLE", "exists": True, "node_id": "local",
                              "cwd": "/repo/a", "resume_conversation_id": "conv-live"})
    _dispatch_once(store, ops)
    state = store.get_recovery_state(task_id)
    assert state["conversation_id"] == "conv-live"
    assert state["worktree"] == "/repo/a"
    assert state["node_id"] == "local"


def test_a_capsule_is_written_when_a_dispatch_is_blocked(store, ops):
    (task_id,) = store.set_tasks("lane-a", [{"prompt": PROMPT}])
    ops.set_status("lane-a", {"state": "IDLE", "exists": True, "node_id": "local", "cwd": "/repo/a"})
    ops.set_capture("lane-a", {"output": "progress: finished step one\nabout to do step two"})

    class RefusingOps(type(ops)):
        def terminal_send_text(self, session, text, press_enter=False, dry_run=False, **kwargs):
            return {"error": "TARGET_AWAITING_APPROVAL"}

    refusing = RefusingOps()
    refusing.status_by_session = dict(ops.status_by_session)
    refusing.capture_by_session = dict(ops.capture_by_session)
    engine = QueueEngine(store, refusing, coordinator=_always_ready_gate())
    for _ in range(4):
        if engine.tick("lane-a").action == "BLOCKED":
            break
    capsule = store.get_recovery_state(task_id).get("capsule") or {}
    assert "finished step one" in (capsule.get("last_decision") or "")


def test_native_resume_relaunches_through_registry_reopen(store, ops):
    task_id = _retryable_task(store, metadata={"recovery": {
        "agent_type": "claude", "conversation_id": "conv-xyz"}})

    class ReopeningOps(type(ops)):
        def __init__(self):
            super().__init__()
            self.reopened = []

        def terminal_registry_reopen(self, session, resume_session_id=None):
            self.reopened.append((session, resume_session_id))
            return {"session": session, "state": "READY"}

    reopening = ReopeningOps()
    # Session exists, agent gone -> RESUME_NATIVE_CONVERSATION.
    reopening.set_status("lane-a", {"state": "UNKNOWN", "exists": True, "node_id": "local", "cwd": "/repo/a"})
    engine = QueueEngine(store, reopening, coordinator=_always_ready_gate())
    for _ in range(4):
        result = engine.tick("lane-a")
        if result.action == "DISPATCHED":
            break

    assert reopening.reopened == [("lane-a", "conv-xyz")]   # the agent's OWN resume path
    assert "retry_mode=RESUME_NATIVE_CONVERSATION" in (result.detail or "")
    assert "native_resume=ok" in (result.detail or "")
    assert PROMPT not in reopening.sent[-1]["text"]


def test_a_controller_without_registry_reopen_never_claims_a_resume_it_did_not_do(store, ops):
    # No reopen capability -> the continuation is still sent, but the result must
    # not assert native_resume=ok.
    _retryable_task(store, metadata={"recovery": {
        "agent_type": "claude", "conversation_id": "conv-xyz"}})
    ops.set_status("lane-a", {"state": "UNKNOWN", "exists": True, "node_id": "local", "cwd": "/repo/a"})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())
    for _ in range(4):
        result = engine.tick("lane-a")
        if result.action == "DISPATCHED":
            break
    assert "retry_mode=RESUME_NATIVE_CONVERSATION" in (result.detail or "")
    assert "native_resume=ok" not in (result.detail or "")
