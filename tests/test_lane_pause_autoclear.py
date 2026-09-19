"""TMCP-BLOCKED-REVIEW-AUTOCLEAR-001: a coordinator lane pause must not outlive
the task it was guarding.

THE REAL INCIDENT THIS IS WRITTEN AGAINST (live, 2026-09-19, lane
terminal-mcp-session-health):

  09:40:18  ENQUEUED              "Compact legacy-call compatibility"
  09:40:18  CLAIMED               QUEUED   -> PRECHECK
  09:40:33  COORDINATOR_DECISION  PRECHECK -> PAUSED   ('merge into main' pattern)
  09:40:33  LANE_PAUSED
  09:41:05  CANCELLED             PAUSED   -> CANCELLED
  ... 3.5 hours, three more tasks enqueued, none ever claimed ...

The guard outlived its task by three and a half hours. Nothing revisited it:
resume_lane is only ever called by a human, engine.tick() returns PAUSED before
it looks at a task, and the loop only walks auto-dispatch-enabled lanes anyway.

The tests below pin both halves of the fix: the pause clears itself once
nothing is left to guard, and it is NEVER cleared for a pause a human set.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from terminal_mcp.queue_store import (
    PAUSE_ORIGIN_COORDINATOR, PAUSE_ORIGIN_PROJECT, PAUSE_ORIGIN_USER,
    QueueStore, pause_origin)
from terminal_mcp.queue_service import QueueService

SESSION = "test-pause-lane"


def _store(tmp_path: Path) -> QueueStore:
    return QueueStore(tmp_path / "queue.db")


def _enqueue(store: QueueStore, title: str, **kwargs) -> str:
    return store.set_tasks(SESSION, [{"title": title, "prompt": f"do {title}", **kwargs}],
                           replace_pending=False)[0]


def _events(store: QueueStore, event_type: str) -> list[dict]:
    return [e for e in store.list_events(session=SESSION) if e["event_type"] == event_type]


def _needs_human(store: QueueStore, task_id: str) -> None:
    """Drive the real coordinator path that pauses a lane, not a hand-written
    pause -- the bug lived in the difference between those two."""
    store.claim_next_task(SESSION, claimed_by="test", lease_seconds=60)
    store.record_coordinator_decision(
        task_id, status="NEEDS_HUMAN",
        reason="task prompt matches a sensitive/destructive pattern ('merge (to |into )?main\\b')")


# ---------------------------------------------------------------------------
# Origin classification -- the distinction the whole fix rests on
# ---------------------------------------------------------------------------

def test_pause_origin_prefers_the_stored_column():
    assert pause_origin(PAUSE_ORIGIN_USER, "coordinator: looks like a coordinator pause") == PAUSE_ORIGIN_USER


@pytest.mark.parametrize("reason,expected", [
    ("coordinator: sensitive pattern", PAUSE_ORIGIN_COORDINATOR),
    ("project-pause[proj-1]: paused at project level", PAUSE_ORIGIN_PROJECT),
    ("operator stopped this lane by hand", None),
    ("", None),
    (None, None),
])
def test_pause_origin_infers_legacy_rows_from_their_prefix(reason, expected):
    """Rows written before the column existed must still be classifiable --
    a backfill would have had to guess, and guessing wrong here means
    auto-clearing a pause a human set."""
    assert pause_origin(None, reason) == expected


def test_an_unknown_origin_is_never_treated_as_coordinator():
    assert pause_origin(None, "someone paused this for a reason we cannot parse") is None


# ---------------------------------------------------------------------------
# The fix: a coordinator pause clears once nothing is left to guard
# ---------------------------------------------------------------------------

def test_the_live_incident_replayed_end_to_end(tmp_path):
    store = _store(tmp_path)
    guarded = _enqueue(store, "Compact legacy-call compatibility")
    _needs_human(store, guarded)

    lane = store.lane_status(SESSION)
    assert lane["paused"] is True
    assert lane["paused_origin"] == PAUSE_ORIGIN_COORDINATOR
    assert store.get_task(guarded).status == "PAUSED"

    # Three later tasks land in a lane that is already closed.
    later = [_enqueue(store, f"later-{n}") for n in range(3)]
    assert store.next_dispatchable_task(SESSION) is None, "paused lane dispatches nothing"

    # The operator cancels the offending task -- and, before this fix, stopped
    # there, leaving the guard in place.
    store.cancel_task(guarded)
    assert store.lane_status(SESSION)["paused"] is True, "cancel alone must not lift it"

    assert store.reconcile_stale_lane_pause() == [SESSION]

    lane = store.lane_status(SESSION)
    assert lane["paused"] is False
    assert lane["paused_reason"] is None and lane["paused_origin"] is None
    # The stranded work is reachable again.
    assert store.next_dispatchable_task(SESSION).id in later


@pytest.mark.parametrize("resolve", ["cancel", "reconciled_complete"])
def test_any_resolution_of_the_guarded_task_releases_the_lane(tmp_path, resolve):
    """PAUSED's only real exits are CANCELLED and the reconciliation-only
    COMPLETED edge (PAUSED -> SKIPPED is not a valid transition at all, so it
    is deliberately not asserted here). Either way, once the task is no longer
    PAUSED the guard is spent."""
    store = _store(tmp_path)
    guarded = _enqueue(store, "guarded")
    _needs_human(store, guarded)
    if resolve == "cancel":
        store.cancel_task(guarded)
    else:
        store.transition_task(guarded, "COMPLETED", event_type="RECONCILED",
                              reason="work shipped out of band")
    assert store.get_task(guarded).status != "PAUSED"
    assert store.reconcile_stale_lane_pause() == [SESSION]
    assert store.lane_status(SESSION)["paused"] is False


def test_a_pause_whose_task_is_still_awaiting_a_human_is_left_alone(tmp_path):
    """The guard is doing its job -- age is irrelevant, and there is no timer
    here for exactly that reason."""
    store = _store(tmp_path)
    guarded = _enqueue(store, "guarded")
    _needs_human(store, guarded)
    assert store.get_task(guarded).status == "PAUSED"

    assert store.reconcile_stale_lane_pause() == []
    assert store.lane_status(SESSION)["paused"] is True
    assert store.get_task(guarded).status == "PAUSED"


def test_reconcile_is_idempotent_and_silent_when_there_is_nothing_to_do(tmp_path):
    store = _store(tmp_path)
    guarded = _enqueue(store, "guarded")
    _needs_human(store, guarded)
    store.cancel_task(guarded)

    assert store.reconcile_stale_lane_pause() == [SESSION]
    for _ in range(3):
        assert store.reconcile_stale_lane_pause() == []
    # Exactly ONE audit event, however many sweeps ran.
    assert len(_events(store, "LANE_PAUSE_RECONCILED")) == 1


def test_the_audit_event_records_the_reason_that_was_cleared(tmp_path):
    store = _store(tmp_path)
    guarded = _enqueue(store, "guarded")
    _needs_human(store, guarded)
    store.cancel_task(guarded)
    store.reconcile_stale_lane_pause()

    event = _events(store, "LANE_PAUSE_RECONCILED")[0]
    assert "no longer guards any task" in event["reason"]
    assert "sensitive/destructive pattern" in event["reason"], "the cleared pause must be recoverable from the log"
    assert event["task_id"] is None, "a lane-level event, not a task event"


def test_a_never_paused_lane_is_untouched(tmp_path):
    store = _store(tmp_path)
    _enqueue(store, "ordinary")
    assert store.reconcile_stale_lane_pause() == []
    assert _events(store, "LANE_PAUSE_RECONCILED") == []


def test_one_session_scope_never_reconciles_another_lane(tmp_path):
    store = _store(tmp_path)
    other = "test-pause-other"
    for session in (SESSION, other):
        task = store.set_tasks(session, [{"title": "g", "prompt": "p"}], replace_pending=False)[0]
        store.claim_next_task(session, claimed_by="test", lease_seconds=60)
        store.record_coordinator_decision(task, status="NEEDS_HUMAN", reason="held")
        store.cancel_task(task)

    assert store.reconcile_stale_lane_pause(SESSION) == [SESSION]
    assert store.lane_status(other)["paused"] is True, "scoped sweep must not reach another lane"
    assert store.reconcile_stale_lane_pause() == [other]


# ---------------------------------------------------------------------------
# PAUSED_BY_USER stays separate -- the safety property
# ---------------------------------------------------------------------------

def test_an_explicit_operator_pause_is_never_auto_cleared(tmp_path):
    store = _store(tmp_path)
    service = QueueService(store)
    _enqueue(store, "ordinary")

    lane = service.pause(SESSION, reason="hands off, I am debugging this pane")
    assert lane["paused"] is True
    assert lane["paused_origin"] == PAUSE_ORIGIN_USER

    for _ in range(3):
        assert store.reconcile_stale_lane_pause() == []
    assert store.lane_status(SESSION)["paused"] is True
    assert _events(store, "LANE_PAUSE_RECONCILED") == []

    # Only an explicit resume lifts it.
    assert service.resume(SESSION)["paused"] is False


def test_a_user_pause_survives_even_with_no_tasks_at_all(tmp_path):
    """"Nothing left to guard" is a coordinator concept. A standing operator
    instruction has nothing to do with what is in the lane."""
    store = _store(tmp_path)
    QueueService(store).pause(SESSION, reason="standing instruction")
    assert store.reconcile_stale_lane_pause() == []
    assert store.lane_status(SESSION)["paused"] is True


def test_a_project_pause_is_never_auto_cleared(tmp_path):
    store = _store(tmp_path)
    store.pause_lane(SESSION, reason="project-pause[proj-1]: paused at project level")
    assert store.reconcile_stale_lane_pause() == []
    assert store.lane_status(SESSION)["paused"] is True


def test_a_legacy_unknown_pause_is_never_auto_cleared(tmp_path):
    """No stored origin and no recognizable prefix -- refuse, because the one
    failure mode worth designing against is clearing somebody's deliberate
    pause."""
    store = _store(tmp_path)
    store.pause_lane(SESSION, reason="paused by hand, 2026-01-01")
    assert store.reconcile_stale_lane_pause() == []
    assert store.lane_status(SESSION)["paused"] is True


def test_a_legacy_coordinator_pause_with_no_stored_origin_is_still_reconciled(tmp_path):
    """Rows already on disk when this shipped -- exactly the live lane -- carry
    paused_origin=NULL and must still heal, via the reason prefix."""
    store = _store(tmp_path)
    _enqueue(store, "stranded")
    store.pause_lane(SESSION, reason="coordinator: sensitive pattern")  # no origin, as before
    assert store.lane_status(SESSION)["paused_origin"] == PAUSE_ORIGIN_COORDINATOR
    assert store.reconcile_stale_lane_pause() == [SESSION]
    assert store.lane_status(SESSION)["paused"] is False


# ---------------------------------------------------------------------------
# The sweep runs for lanes that never dispatch
# ---------------------------------------------------------------------------

def test_clearing_the_pause_does_not_dispatch_or_enable_dispatch(tmp_path):
    """The reconciler clears a LANE FLAG. It must not submit work, and must
    not turn an opted-out lane into one that does."""
    store = _store(tmp_path)
    guarded = _enqueue(store, "guarded")
    _needs_human(store, guarded)
    store.cancel_task(guarded)
    stranded = _enqueue(store, "stranded")

    store.reconcile_stale_lane_pause()

    lane = store.lane_status(SESSION)
    assert lane["auto_dispatch_enabled"] is False, "must never opt a lane into auto-dispatch"
    # Claimable by an explicit tick, but nothing has been claimed or sent.
    assert store.get_task(stranded).status == "QUEUED"
    assert store.get_task(stranded).attempt_count == 0


def test_the_loop_sweeps_every_lane_including_auto_dispatch_disabled_ones(tmp_path):
    """The gap that let the live incident persist: the loop only walks
    auto-dispatch-enabled lanes, and every reconciler lived inside tick()."""
    from terminal_mcp.queue_loop import QueueLoop

    store = _store(tmp_path)
    guarded = _enqueue(store, "guarded")
    _needs_human(store, guarded)
    store.cancel_task(guarded)
    _enqueue(store, "stranded")
    assert store.lane_status(SESSION)["auto_dispatch_enabled"] is False

    ticked: list[str] = []

    class Engine:
        def __init__(self, store):
            self.store = store

        def tick(self, session):  # pragma: no cover -- must never be reached here
            ticked.append(session)
            raise AssertionError("an auto-dispatch-disabled lane must not be ticked")

    results = QueueLoop(Engine(store)).run_one_cycle()

    assert results == [], "no lane was dispatch-eligible"
    assert ticked == [], "the sweep must not dispatch"
    assert store.lane_status(SESSION)["paused"] is False, "the stale pause was still reconciled"
    assert len(_events(store, "LANE_PAUSE_RECONCILED")) == 1


def test_the_loop_sweep_is_idempotent_across_cycles(tmp_path):
    from terminal_mcp.queue_loop import QueueLoop

    store = _store(tmp_path)
    guarded = _enqueue(store, "guarded")
    _needs_human(store, guarded)
    store.cancel_task(guarded)

    class Engine:
        def __init__(self, store):
            self.store = store

        def tick(self, session):  # pragma: no cover
            raise AssertionError("not dispatch-eligible")

    loop = QueueLoop(Engine(store))
    for _ in range(3):
        loop.run_one_cycle()
    assert len(_events(store, "LANE_PAUSE_RECONCILED")) == 1, "one real change, one event"


def test_the_loop_sweep_also_revisits_waiting_session_on_an_opted_out_lane(tmp_path):
    """The same gap applied to WAITING_SESSION, whose clear is documented as
    automatic but only ran inside tick()."""
    from terminal_mcp.queue_loop import QueueLoop

    store = _store(tmp_path)
    task = _enqueue(store, "waiting")
    store.claim_next_task(SESSION, claimed_by="test", lease_seconds=60)
    store.mark_waiting_session(task, reason="SESSION_NOT_FOUND")
    assert store.get_task(task).status == "WAITING_SESSION"

    class Engine:
        def __init__(self, store):
            self.store = store

        def tick(self, session):  # pragma: no cover
            raise AssertionError("not dispatch-eligible")

    # grace_seconds defaults to 60, so force the clock back to make it due.
    store.reconcile_uncertain_and_waiting(now="2099-01-01T00:00:00Z")
    QueueLoop(Engine(store)).run_one_cycle()
    assert store.get_task(task).status == "QUEUED"
