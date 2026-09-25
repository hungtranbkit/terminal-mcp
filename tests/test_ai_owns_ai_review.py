"""TMCP-AI-OWNS-AI-REVIEW-001: ownership, not status.

Two failure modes this pins down, and they pull in opposite directions:

  * escalating work the AI could have recovered -- which trains an operator to
    ignore the column, so the real decisions wait too;
  * auto-clearing something only a human may authorize.

Every test below is about the line between those. The coordinator reasons used
here are REAL strings taken off the live fleet database, not invented ones.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from terminal_mcp import ai_review as ar
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import PAUSE_ORIGIN_COORDINATOR, QueueStore, iso_now

SESSION = "test-ai-review"

# Real reasons, observed on the live fleet 2026-09-19.
REASON_MERGE_MAIN = ("coordinator: task prompt matches a sensitive/destructive pattern "
                     "('merge (to |into )?main\\\\b')")
REASON_CREDENTIAL = ("coordinator: task prompt matches a sensitive/destructive pattern "
                     "('credential')")
REASON_TOKEN = "coordinator: task prompt matches a sensitive/destructive pattern ('\\\\btoken\\\\b')"
REASON_RM_RF = "coordinator: task prompt matches a sensitive/destructive pattern ('\\\\brm -rf\\\\b')"
REASON_CONTENTION = ("coordinator: session 'terminal-mcp-session-health' is already actively "
                     "working in the same repo/worktree (/home/kimex/workspace/terminal-mcp)")
REASON_GIT_UNREADABLE = "coordinator: could not read git/repo status for '/tmp/some/path'"
REASON_REPEATED = ('coordinator: the exact same coordinator decision reason repeated 3 times '
                   'in a row ("uncommitted changes present")')
REASON_EXHAUSTED = ("coordinator: exceeded max coordinator review attempts (5) -- needs a human "
                    "decision instead of another automatic retry")


def _store(tmp_path: Path) -> QueueStore:
    return QueueStore(tmp_path / "queue.db")


def _task(**over) -> dict:
    base = {"id": "t1", "session": SESSION, "status": "BLOCKED", "attempt_count": 0,
            "coordinator_attempts": 0, "updated_at": iso_now(),
            "coordinator_checked_at": iso_now(), "metadata": "{}"}
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# (3) BLOCKED is AI-owned by DEFAULT
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason", [REASON_CONTENTION, REASON_GIT_UNREADABLE, REASON_REPEATED,
                                    None, "", "some refusal nobody wrote a pattern for"])
def test_blocked_is_ai_owned_by_default(reason):
    entry = ar.classify_task(_task(coordinator_reason=reason))
    assert entry.owner == ar.OWNER_AI
    assert entry.bucket == ar.BUCKET_AI_REVIEW
    assert entry.approval_class is None
    assert entry.next_action == ar.ACTION_RETRY


def test_retry_exhaustion_never_escalates_to_a_human():
    """THE rule most likely to be broken by accident. The live fleet's own
    reason even says "needs a human decision" -- and the policy overrides that:
    exhaustion changes the AI's next action, never the owner."""
    entry = ar.classify_task(_task(coordinator_reason=REASON_EXHAUSTED, coordinator_attempts=9))
    assert entry.owner == ar.OWNER_AI, "exhaustion must not hand work to a human"
    assert entry.bucket == ar.BUCKET_AI_REVIEW
    assert entry.approval_class is None
    assert entry.retries_exhausted is True
    # It stops retrying and starts diagnosing -- loop prevention without escalation.
    assert entry.next_action == ar.ACTION_DIAGNOSE


def test_exhaustion_flips_next_action_exactly_at_the_cap():
    below = ar.classify_task(_task(coordinator_attempts=ar.AI_MAX_AUTOMATIC_ATTEMPTS - 1))
    at_cap = ar.classify_task(_task(coordinator_attempts=ar.AI_MAX_AUTOMATIC_ATTEMPTS))
    assert below.next_action == ar.ACTION_RETRY and below.retries_exhausted is False
    assert at_cap.next_action == ar.ACTION_DIAGNOSE and at_cap.retries_exhausted is True
    assert below.owner == at_cap.owner == ar.OWNER_AI


# ---------------------------------------------------------------------------
# The four -- and only four -- escalation classes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason,expected", [
    (REASON_CREDENTIAL, ar.APPROVAL_CREDENTIALS),
    (REASON_TOKEN, ar.APPROVAL_CREDENTIALS),
    (REASON_RM_RF, ar.APPROVAL_DESTRUCTIVE),
    (REASON_MERGE_MAIN, ar.APPROVAL_PROTECTED_DEPLOY),
])
def test_only_true_authorization_classes_leave_ai_review(reason, expected):
    entry = ar.classify_task(_task(coordinator_reason=reason))
    assert entry.owner == ar.OWNER_USER
    assert entry.bucket == ar.BUCKET_NEEDS_APPROVAL
    assert entry.approval_class == expected
    assert entry.next_action == ar.ACTION_AWAIT_APPROVAL


def test_every_coordinator_sensitive_pattern_is_classified_deliberately():
    """The escalation table must stay aligned with the gate that actually
    refuses. A pattern the coordinator screens on that is NOT in the table
    lands in AI Review -- fine, and the safe direction -- but it must be a
    deliberate choice, so this test names the whole set."""
    from terminal_mcp.coordinator import SENSITIVE_PROMPT_PATTERNS

    for pattern in SENSITIVE_PROMPT_PATTERNS:
        reason = f"coordinator: task prompt matches a sensitive/destructive pattern ({pattern.pattern!r})"
        assert ar.approval_class_for_reason(reason) is not None, (
            f"{pattern.pattern!r} screens a sensitive action but maps to no approval class")


def test_a_business_decision_needs_an_explicit_marker_never_an_inference():
    prose = _task(coordinator_reason="this needs a product decision about pricing, probably")
    assert ar.classify_task(prose).bucket == ar.BUCKET_AI_REVIEW, "prose must never escalate"

    marked = _task(metadata=json.dumps({"needs_human_decision": True}))
    entry = ar.classify_task(marked)
    assert entry.bucket == ar.BUCKET_NEEDS_APPROVAL
    assert entry.approval_class == ar.APPROVAL_BUSINESS_DECISION


def test_a_business_decision_can_also_come_from_a_coordinator_blocker():
    task = _task(coordinator_decision=json.dumps(
        {"status": "NEEDS_HUMAN", "reason": "pick one", "blockers": ["needs_business_decision"]}))
    assert ar.classify_task(task).approval_class == ar.APPROVAL_BUSINESS_DECISION


# ---------------------------------------------------------------------------
# (1) VERIFYING / evidence wait is AI-owned, and never completed without evidence
# ---------------------------------------------------------------------------

def test_verifying_is_ai_owned_and_asks_for_reverification():
    entry = ar.classify_task(_task(status="VERIFYING"))
    assert entry.owner == ar.OWNER_AI
    assert entry.bucket == ar.BUCKET_AI_REVIEW
    assert entry.next_action == ar.ACTION_REVERIFY
    assert "no completion without evidence" in entry.diagnosis


def test_a_long_verifying_wait_still_never_becomes_a_human_ticket():
    entry = ar.classify_task(_task(status="VERIFYING", attempt_count=50))
    assert entry.owner == ar.OWNER_AI
    assert entry.next_action == ar.ACTION_DIAGNOSE  # reroute the verifier, do not escalate


def test_reclaiming_a_stale_verifier_lease_never_completes_the_task(tmp_path):
    store = _store(tmp_path)
    task_id = store.set_tasks(SESSION, [{"title": "v", "prompt": "p",
                                         "completion_policy": {"verify": {"capabilities": []}}}],
                              replace_pending=False)[0]
    store.claim_next_task(SESSION, claimed_by="test", lease_seconds=60)
    store.record_coordinator_decision(task_id, status="READY", reason="ok")
    store.transition_task(task_id, "DISPATCHING", event_type="DISPATCHING", reason=None)
    store.mark_running_with_evidence(
        task_id, evidence={"accepted": True, "signal": "explicit_running_signal"})
    store.transition_task(task_id, "VERIFYING", event_type="VERIFYING", reason=None)

    from terminal_mcp.verify_queue import VerifyQueue

    verify = VerifyQueue(store)
    job = verify.ensure_verify_job(store.get_task(task_id), required_capabilities=(),
                                  require_independent=False)
    claimed = verify.claim_next(verifier="worker-1", capabilities=(), lease_seconds=600.0)
    assert claimed is not None and claimed.status in ("VERIFY_CLAIMED", "VERIFY_RUNNING")

    reclaimed = store.reclaim_stale_verify_leases(now="2099-01-01T00:00:00Z")
    assert reclaimed == [job.id]
    assert verify.get(job.id).status == "VERIFY_PENDING"
    # The task is untouched: still awaiting evidence, never completed.
    assert store.get_task(task_id).status == "VERIFYING"

    events = [e for e in store.list_events(session=SESSION)
              if e["event_type"] == "AI_VERIFY_LEASE_RECLAIMED"]
    assert len(events) == 1
    assert json.loads(events[0]["metadata"])["actor"] == ar.ACTOR_AI_RECONCILER
    # Idempotent: no lease left to expire.
    assert store.reclaim_stale_verify_leases(now="2099-01-01T00:00:00Z") == []


# ---------------------------------------------------------------------------
# (2) Transient infrastructure is AI-owned
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,action", [
    ("WAITING_SESSION", ar.ACTION_RESUME_SESSION),
    ("DISPATCH_UNCERTAIN", ar.ACTION_RESUME_SESSION),
])
def test_transient_infrastructure_is_ai_owned(status, action):
    entry = ar.classify_task(_task(status=status))
    assert entry.owner == ar.OWNER_AI
    assert entry.bucket == ar.BUCKET_AI_REVIEW
    assert entry.next_action == action


# ---------------------------------------------------------------------------
# (4) PAUSED_BY_USER is separate and never auto-resumed
# ---------------------------------------------------------------------------

def test_an_explicit_user_pause_is_its_own_bucket(tmp_path):
    store = _store(tmp_path)
    store.set_tasks(SESSION, [{"title": "t", "prompt": "p"}], replace_pending=False)
    QueueService(store).pause(SESSION, reason="hands off, I am debugging")

    board = QueueService(store).attention()
    assert board["paused_by_user"], "an explicit pause must be visible as its own bucket"
    assert board["ai_review"] == []
    entry = board["paused_by_user"][0]
    assert entry["owner"] == ar.OWNER_USER
    assert entry["next_action"] == ar.ACTION_AWAIT_USER_RESUME


def test_a_coordinator_lane_pause_is_ai_owned(tmp_path):
    store = _store(tmp_path)
    task = store.set_tasks(SESSION, [{"title": "t", "prompt": "p"}], replace_pending=False)[0]
    store.claim_next_task(SESSION, claimed_by="test", lease_seconds=60)
    store.record_coordinator_decision(task, status="NEEDS_HUMAN", reason=REASON_CONTENTION)
    assert store.lane_status(SESSION)["paused_origin"] == PAUSE_ORIGIN_COORDINATOR

    board = QueueService(store).attention()
    assert board["paused_by_user"] == [], "a system pause is not a user pause"
    assert len(board["ai_review"]) == 1
    assert board["ai_review"][0]["owner"] == ar.OWNER_AI


def test_a_coordinator_pause_encoding_a_true_approval_class_escalates(tmp_path):
    """(5): a coordinator refusal is AI-owned UNLESS it encodes one of the true
    external-authorization classes."""
    store = _store(tmp_path)
    task = store.set_tasks(SESSION, [{"title": "t", "prompt": "merge into main"}],
                           replace_pending=False)[0]
    store.claim_next_task(SESSION, claimed_by="test", lease_seconds=60)
    store.record_coordinator_decision(task, status="NEEDS_HUMAN", reason=REASON_MERGE_MAIN)

    board = QueueService(store).attention()
    assert len(board["needs_approval"]) == 1
    assert board["needs_approval"][0]["approval_class"] == ar.APPROVAL_PROTECTED_DEPLOY
    assert board["ai_review"] == []


def test_a_paused_lane_with_no_paused_task_is_still_visible(tmp_path):
    """The 3.5-hour invisible pause: a lane flag with nothing to speak for it."""
    store = _store(tmp_path)
    store.pause_lane(SESSION, reason="coordinator: something stale")
    board = QueueService(store).attention()
    assert len(board["ai_review"]) == 1
    assert board["ai_review"][0]["status"] == "LANE_PAUSED"
    assert board["ai_review"][0]["next_action"] == ar.ACTION_RECONCILE_PAUSE


# ---------------------------------------------------------------------------
# (5) A stale coordinator refusal is re-evaluated, not left forever
# ---------------------------------------------------------------------------

def _blocked_task(store: QueueStore, reason: str, *, attempts: int = 0) -> str:
    task = store.set_tasks(SESSION, [{"title": "t", "prompt": "p"}], replace_pending=False)[0]
    store.claim_next_task(SESSION, claimed_by="test", lease_seconds=60)
    store.record_coordinator_decision(task, status="BLOCKED", reason=reason)
    for _ in range(attempts):
        store.transition_task(task, "QUEUED", event_type="AI_BLOCKED_REEVALUATED", reason=None)
        store.claim_next_task(SESSION, claimed_by="test", lease_seconds=60)
        store.record_coordinator_decision(task, status="BLOCKED", reason=reason)
    return task


def test_a_stale_ai_owned_refusal_is_requeued_with_an_actor_stamped_event(tmp_path):
    store = _store(tmp_path)
    task = _blocked_task(store, REASON_CONTENTION)
    assert store.get_task(task).status == "BLOCKED"

    # Far future: the backoff window has certainly elapsed.
    assert store.reevaluate_ai_owned_blocked(now="2099-01-01T00:00:00Z") == [task]
    assert store.get_task(task).status == "QUEUED"

    events = [e for e in store.list_events(session=SESSION) if e["event_type"] == "AI_RECONCILED"]
    assert len(events) == 1
    assert json.loads(events[0]["metadata"])["actor"] == ar.ACTOR_AI_RECONCILER
    # Idempotent: nothing is BLOCKED any more.
    assert store.reevaluate_ai_owned_blocked(now="2099-01-01T00:00:00Z") == []


def test_a_refusal_needing_human_authorization_is_never_requeued(tmp_path):
    """Re-queueing it would just get it refused again, forever -- and it is not
    the AI's to decide."""
    store = _store(tmp_path)
    task = _blocked_task(store, REASON_MERGE_MAIN)
    assert store.reevaluate_ai_owned_blocked(now="2099-01-01T00:00:00Z") == []
    assert store.get_task(task).status == "BLOCKED"


def test_backoff_holds_a_fresh_refusal_for_its_window(tmp_path):
    store = _store(tmp_path)
    task = _blocked_task(store, REASON_CONTENTION)
    # "now" == the refusal instant: inside the first backoff window.
    assert store.reevaluate_ai_owned_blocked(now=store.get_task(task).coordinator_checked_at) == []
    assert store.get_task(task).status == "BLOCKED"


def test_automatic_retry_stops_at_the_cap_without_escalating(tmp_path):
    store = _store(tmp_path)
    task = _blocked_task(store, REASON_CONTENTION, attempts=ar.AI_MAX_AUTOMATIC_ATTEMPTS)
    assert store.reevaluate_ai_owned_blocked(now="2099-01-01T00:00:00Z") == [], "must stop retrying"

    board = QueueService(store).attention()
    entry = next(e for e in board["ai_review"] if e["task_id"] == task)
    assert entry["owner"] == ar.OWNER_AI, "still AI-owned -- exhaustion is not an escalation"
    assert entry["next_action"] == ar.ACTION_DIAGNOSE
    assert board["needs_approval"] == []


# ---------------------------------------------------------------------------
# (6) API shape, and (8) backward compatibility
# ---------------------------------------------------------------------------

def test_every_row_carries_what_the_dashboard_has_to_show(tmp_path):
    store = _store(tmp_path)
    _blocked_task(store, REASON_CONTENTION)
    row = QueueService(store).attention()["ai_review"][0]
    for field in ("task_id", "session", "status", "owner", "bucket", "diagnosis",
                  "next_action", "attempts", "age_seconds", "last_check", "next_check_at"):
        assert field in row, field
    assert row["owner"] == "ai"
    assert isinstance(row["age_seconds"], float)
    assert row["next_check_at"], "a UI must be able to say WHEN the next attempt happens"


def test_the_summary_reports_every_bucket_and_class_including_zeros(tmp_path):
    store = _store(tmp_path)
    _blocked_task(store, REASON_MERGE_MAIN)
    summary = QueueService(store).attention()["summary"]
    assert set(summary["counts"]) == set(ar.BUCKETS)
    assert set(summary["approval_classes"]) == set(ar.APPROVAL_CLASSES)
    assert summary["approval_classes"][ar.APPROVAL_PROTECTED_DEPLOY] == 1
    assert summary["human_owned"] == 1 and summary["ai_owned"] == 0
    assert summary["policy"]["retry_exhaustion_escalates"] is False
    assert summary["policy"]["max_automatic_attempts"] == ar.AI_MAX_AUTOMATIC_ATTEMPTS


def test_the_board_keeps_blocked_review_for_existing_callers(tmp_path):
    store = _store(tmp_path)
    _blocked_task(store, REASON_CONTENTION)
    board = QueueService(store).board()
    # Compatibility (item 8): the old key and its count still mean what they did.
    assert len(board["blocked_review"]) == 1
    assert board["counts"]["blocked_review"] == 1
    # And the ownership split is additive, alongside it.
    assert board["counts"]["ai_review"] == 1
    assert board["counts"]["needs_approval"] == 0
    assert board["counts"]["paused_by_user"] == 0
    for key in ("backlog", "queued", "running", "done"):
        assert key in board


def test_a_quiet_fleet_reports_empty_buckets_not_missing_ones(tmp_path):
    board = QueueService(_store(tmp_path)).attention()
    assert board["ai_review"] == [] and board["needs_approval"] == [] and board["paused_by_user"] == []
    assert board["summary"]["counts"] == {b: 0 for b in ar.BUCKETS}
    assert board["summary"]["oldest_age_seconds"] is None


def test_a_running_task_is_in_no_attention_bucket():
    assert ar.classify_task(_task(status="RUNNING")) is None
    assert ar.classify_task(_task(status="QUEUED")) is None
    assert ar.classify_task(_task(status="COMPLETED")) is None


# ---------------------------------------------------------------------------
# (7) Backoff / loop prevention
# ---------------------------------------------------------------------------

def test_backoff_is_bounded_and_monotonic():
    values = [ar.backoff_seconds(n) for n in range(0, 12)]
    assert values[0] == ar.AI_BACKOFF_BASE_SECONDS
    assert all(b >= a for a, b in zip(values, values[1:])), "monotonic"
    assert max(values) == ar.AI_BACKOFF_CAP_SECONDS, "capped"
    assert ar.backoff_seconds(10_000) == ar.AI_BACKOFF_CAP_SECONDS, "no overflow"


def test_the_loop_runs_the_ai_sweeps_without_dispatching(tmp_path):
    from terminal_mcp.queue_loop import QueueLoop

    store = _store(tmp_path)
    task = _blocked_task(store, REASON_CONTENTION)
    # Age the refusal past its backoff window.
    store.transition_task(task, "QUEUED", event_type="X", reason=None)
    store.claim_next_task(SESSION, claimed_by="test", lease_seconds=60)
    store.record_coordinator_decision(task, status="BLOCKED", reason=REASON_CONTENTION)
    with store._connection() as connection:  # noqa: SLF001 -- pinning the clock, not behaviour
        connection.execute("UPDATE queue_tasks SET coordinator_checked_at = '2000-01-01T00:00:00Z', "
                           "updated_at = '2000-01-01T00:00:00Z' WHERE id = ?", (task,))

    class Engine:
        def __init__(self, store):
            self.store = store

        def tick(self, session):  # pragma: no cover -- lane is not dispatch-eligible
            raise AssertionError("the sweep must not dispatch")

    assert QueueLoop(Engine(store)).run_one_cycle() == []
    assert store.get_task(task).status == "QUEUED", "the AI sweep re-evaluated it"
    assert store.lane_status(SESSION)["auto_dispatch_enabled"] is False
