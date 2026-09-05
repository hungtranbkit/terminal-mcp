"""QueueStore -- Supervisor Queue v2 persistence + state machine (task:
"Supervisor Queue v2 cho Terminal MCP"). Pure in-process tests, no real
tmux/ConPTY session involved -- see queue_store.py's own module
docstring for why that split is deliberate.

SAFETY: every session name used below is a disposable test fixture
(tmp_path-scoped db, fake session names like "lane-a") -- never
`window`/`window2`. This whole feature must not be used to control
those real production sessions until the acceptance demo passes and
the user/ChatGPT explicitly confirms."""
from __future__ import annotations

import pytest

from terminal_mcp.queue_store import (
    ALL_STATUSES, BLOCKED, CANCELLED, COMPLETED, DISPATCHING, PAUSED, QUEUED, RUNNING, SKIPPED, VERIFYING,
    InvalidTransitionError, QueueStore, is_valid_transition,
)


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


# ---------------------------------------------------------------------------
# Test matrix item A: state-machine transitions, every valid/invalid edge.
# ---------------------------------------------------------------------------

def _make_one_task(store, session="lane-a", **overrides):
    task = {"prompt": "do the thing", "title": "Task"}
    task.update(overrides)
    (task_id,) = store.set_tasks(session, [task])
    return task_id


@pytest.mark.parametrize("from_status,to_status", [
    (QUEUED, DISPATCHING), (QUEUED, SKIPPED), (QUEUED, CANCELLED), (QUEUED, PAUSED),
    (DISPATCHING, RUNNING), (DISPATCHING, QUEUED), (DISPATCHING, BLOCKED), (DISPATCHING, CANCELLED), (DISPATCHING, PAUSED),
    (RUNNING, VERIFYING), (RUNNING, BLOCKED), (RUNNING, CANCELLED), (RUNNING, PAUSED),
    (VERIFYING, COMPLETED), (VERIFYING, RUNNING), (VERIFYING, BLOCKED), (VERIFYING, CANCELLED), (VERIFYING, PAUSED),
    (BLOCKED, QUEUED), (BLOCKED, SKIPPED), (BLOCKED, CANCELLED),
    (PAUSED, QUEUED), (PAUSED, DISPATCHING), (PAUSED, RUNNING), (PAUSED, VERIFYING), (PAUSED, CANCELLED),
])
def test_every_documented_valid_transition_is_accepted(store, from_status, to_status):
    assert is_valid_transition(from_status, to_status) is True
    task_id = _make_one_task(store)
    # Walk QUEUED to from_status first via whatever path is shortest/valid,
    # then apply the transition under test.
    _drive_to_status(store, task_id, from_status)
    updated = store.transition_task(task_id, to_status, event_type="TEST")
    assert updated.status == to_status


def _drive_to_status(store, task_id, status):
    if status == QUEUED:
        return
    store.transition_task(task_id, DISPATCHING, event_type="TEST")
    if status == DISPATCHING:
        return
    store.transition_task(task_id, RUNNING, event_type="TEST")
    if status == RUNNING:
        return
    if status == VERIFYING:
        store.transition_task(task_id, VERIFYING, event_type="TEST")
        return
    if status == BLOCKED:
        store.transition_task(task_id, BLOCKED, event_type="TEST")
        return
    if status == PAUSED:
        store.transition_task(task_id, PAUSED, event_type="TEST")
        return
    raise AssertionError(f"no drive path coded for {status}")


@pytest.mark.parametrize("from_status,to_status", [
    (QUEUED, RUNNING), (QUEUED, COMPLETED), (QUEUED, VERIFYING), (QUEUED, BLOCKED),
    (COMPLETED, RUNNING), (COMPLETED, QUEUED), (COMPLETED, CANCELLED),
    (SKIPPED, QUEUED), (SKIPPED, RUNNING),
    (CANCELLED, QUEUED), (CANCELLED, RUNNING),
    (BLOCKED, RUNNING), (BLOCKED, COMPLETED), (BLOCKED, VERIFYING), (BLOCKED, DISPATCHING),
    (RUNNING, QUEUED), (RUNNING, COMPLETED), (RUNNING, DISPATCHING),
    (DISPATCHING, VERIFYING), (DISPATCHING, COMPLETED),
    (PAUSED, COMPLETED), (PAUSED, BLOCKED), (PAUSED, SKIPPED),
])
def test_every_undocumented_transition_is_rejected(from_status, to_status):
    assert is_valid_transition(from_status, to_status) is False


def test_transition_task_raises_on_invalid_transition_and_never_mutates(store):
    task_id = _make_one_task(store)
    with pytest.raises(InvalidTransitionError):
        store.transition_task(task_id, RUNNING, event_type="TEST")  # QUEUED -> RUNNING skips DISPATCHING
    # Refused transition must not have partially applied.
    assert store.get_task(task_id).status == QUEUED


def test_terminal_statuses_have_no_outgoing_transitions_at_all():
    for status in (COMPLETED, SKIPPED, CANCELLED):
        for other in ALL_STATUSES:
            assert is_valid_transition(status, other) is False


# ---------------------------------------------------------------------------
# Test matrix item B: persistence + restart recovery.
# ---------------------------------------------------------------------------

def test_tasks_and_events_survive_reopening_the_store(tmp_path):
    db_path = tmp_path / "queue.db"
    store1 = QueueStore(db_path)
    task_id = _make_one_task(store1, title="Survives restart")
    store1.transition_task(task_id, DISPATCHING, event_type="DISPATCHED")

    # Simulate a Terminal MCP process restart: a brand new QueueStore
    # instance pointed at the SAME db file, no shared Python state at all.
    store2 = QueueStore(db_path)
    task = store2.get_task(task_id)
    assert task is not None
    assert task.status == DISPATCHING
    assert task.title == "Survives restart"
    events = store2.list_events("lane-a")
    assert any(e["event_type"] == "DISPATCHED" for e in events)


def test_migration_is_idempotent_across_repeated_opens(tmp_path):
    db_path = tmp_path / "queue.db"
    for _ in range(3):
        QueueStore(db_path)  # must not raise (duplicate CREATE TABLE, etc.)


# ---------------------------------------------------------------------------
# set_tasks / append_tasks semantics.
# ---------------------------------------------------------------------------

def test_set_tasks_replaces_pending_but_never_touches_in_flight(store):
    old_ids = store.set_tasks("lane-a", [{"prompt": "old-1"}, {"prompt": "old-2"}])
    store.transition_task(old_ids[0], DISPATCHING, event_type="TEST")
    store.transition_task(old_ids[0], RUNNING, event_type="TEST")

    new_ids = store.set_tasks("lane-a", [{"prompt": "new-1"}])

    # old-1 was RUNNING -- untouched.
    assert store.get_task(old_ids[0]).status == RUNNING
    # old-2 was QUEUED (pending) -- superseded/cancelled by the replace.
    assert store.get_task(old_ids[1]).status == CANCELLED
    # the new task is queued.
    assert store.get_task(new_ids[0]).status == QUEUED


def test_append_tasks_never_touches_existing_pending_tasks(store):
    old_ids = store.set_tasks("lane-a", [{"prompt": "old-1"}])
    new_ids = store.append_tasks("lane-a", [{"prompt": "new-1"}])
    assert store.get_task(old_ids[0]).status == QUEUED
    assert store.get_task(new_ids[0]).status == QUEUED
    positions = {t["id"]: t["position"] for t in store.lane_status("lane-a")["tasks"]}
    assert positions[old_ids[0]] < positions[new_ids[0]]


def test_set_tasks_preserves_given_order_via_position(store):
    ids = store.set_tasks("lane-a", [{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}])
    tasks = store.lane_status("lane-a")["tasks"]
    assert [t["id"] for t in tasks] == ids


# ---------------------------------------------------------------------------
# Only one task in flight per session; lane pause blocks dispatch.
# ---------------------------------------------------------------------------

def test_next_dispatchable_task_returns_none_when_a_task_is_already_in_flight(store):
    ids = store.set_tasks("lane-a", [{"prompt": "a"}, {"prompt": "b"}])
    assert store.next_dispatchable_task("lane-a").id == ids[0]
    store.transition_task(ids[0], DISPATCHING, event_type="TEST")
    assert store.next_dispatchable_task("lane-a") is None  # b must wait
    store.transition_task(ids[0], RUNNING, event_type="TEST")
    assert store.next_dispatchable_task("lane-a") is None
    store.transition_task(ids[0], VERIFYING, event_type="TEST")
    store.transition_task(ids[0], COMPLETED, event_type="TEST")
    assert store.next_dispatchable_task("lane-a").id == ids[1]  # now b is next


def test_next_dispatchable_task_is_none_when_lane_paused(store):
    ids = store.set_tasks("lane-a", [{"prompt": "a"}])
    store.pause_lane("lane-a", reason="manual test pause")
    assert store.next_dispatchable_task("lane-a") is None
    store.resume_lane("lane-a")
    assert store.next_dispatchable_task("lane-a").id == ids[0]


def test_two_sessions_are_completely_independent_lanes(store):
    a_ids = store.set_tasks("lane-a", [{"prompt": "a1"}])
    b_ids = store.set_tasks("lane-b", [{"prompt": "b1"}])
    store.pause_lane("lane-a", reason="test")
    assert store.next_dispatchable_task("lane-a") is None
    assert store.next_dispatchable_task("lane-b").id == b_ids[0]  # lane-b unaffected by lane-a's pause


# ---------------------------------------------------------------------------
# Pause/resume roundtrip for an in-flight task (item 10's own mechanism).
# ---------------------------------------------------------------------------

def test_pause_lane_moves_an_in_flight_task_to_paused_and_remembers_its_status(store):
    task_id = _make_one_task(store)
    store.transition_task(task_id, DISPATCHING, event_type="TEST")
    store.transition_task(task_id, RUNNING, event_type="TEST")
    store.pause_lane("lane-a", reason="manual intervention detected")
    task = store.get_task(task_id)
    assert task.status == PAUSED
    assert task.paused_from_status == RUNNING


def test_resume_lane_restores_a_paused_task_to_its_prior_status(store):
    task_id = _make_one_task(store)
    store.transition_task(task_id, DISPATCHING, event_type="TEST")
    store.transition_task(task_id, RUNNING, event_type="TEST")
    store.pause_lane("lane-a", reason="test")
    store.resume_lane("lane-a")
    task = store.get_task(task_id)
    assert task.status == RUNNING
    assert task.paused_from_status is None


def test_pause_with_no_in_flight_task_just_blocks_future_dispatch(store):
    task_id = _make_one_task(store)  # stays QUEUED
    store.pause_lane("lane-a", reason="test")
    assert store.get_task(task_id).status == QUEUED  # untouched, still just queued
    assert store.next_dispatchable_task("lane-a") is None  # but won't dispatch


# ---------------------------------------------------------------------------
# Failure policy: BLOCKED stops the lane; only explicit retry/skip/cancel proceed.
# ---------------------------------------------------------------------------

def test_blocked_task_is_never_auto_retried_or_auto_skipped(store):
    ids = store.set_tasks("lane-a", [{"prompt": "a"}, {"prompt": "b"}])
    store.transition_task(ids[0], DISPATCHING, event_type="TEST")
    store.transition_task(ids[0], RUNNING, event_type="TEST")
    store.transition_task(ids[0], BLOCKED, event_type="TEST", reason="simulated failure")
    # b must NOT become dispatchable just because a failed.
    assert store.next_dispatchable_task("lane-a") is None
    assert store.get_task(ids[1]).status == QUEUED


def test_retry_task_moves_blocked_back_to_queued_without_resetting_attempt_count(store):
    task_id = _make_one_task(store)
    store.transition_task(task_id, DISPATCHING, event_type="TEST")  # attempt_count -> 1
    store.transition_task(task_id, RUNNING, event_type="TEST")
    store.transition_task(task_id, BLOCKED, event_type="TEST", reason="fail")
    store.retry_task(task_id)
    task = store.get_task(task_id)
    assert task.status == QUEUED
    assert task.attempt_count == 1  # NOT reset -- max_attempts is a lifetime cap


def test_skip_and_cancel_from_blocked(store):
    id_a = _make_one_task(store, session="lane-a", prompt="a")
    store.transition_task(id_a, DISPATCHING, event_type="TEST")
    store.transition_task(id_a, RUNNING, event_type="TEST")
    store.transition_task(id_a, BLOCKED, event_type="TEST", reason="fail")
    store.skip_task(id_a)
    assert store.get_task(id_a).status == SKIPPED

    id_b = _make_one_task(store, session="lane-a", prompt="b")
    store.transition_task(id_b, DISPATCHING, event_type="TEST")
    store.transition_task(id_b, RUNNING, event_type="TEST")
    store.transition_task(id_b, BLOCKED, event_type="TEST", reason="fail")
    store.cancel_task(id_b)
    assert store.get_task(id_b).status == CANCELLED


# ---------------------------------------------------------------------------
# reorder / clear
# ---------------------------------------------------------------------------

def test_reorder_tasks_only_affects_still_queued_tasks(store):
    ids = store.set_tasks("lane-a", [{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}])
    store.transition_task(ids[0], DISPATCHING, event_type="TEST")  # a is now in flight, not reorderable
    store.reorder_tasks("lane-a", [ids[2], ids[1]])  # only b, c are QUEUED
    tasks = store.lane_status("lane-a")["tasks"]
    by_id = {t["id"]: t["position"] for t in tasks}
    assert by_id[ids[2]] < by_id[ids[1]]


def test_clear_tasks_only_pending_never_touches_in_flight(store):
    ids = store.set_tasks("lane-a", [{"prompt": "a"}, {"prompt": "b"}])
    store.transition_task(ids[0], DISPATCHING, event_type="TEST")
    cleared = store.clear_tasks("lane-a", only_pending=True)
    assert cleared == 1  # only b (still QUEUED)
    assert store.get_task(ids[0]).status == DISPATCHING
    assert store.get_task(ids[1]).status == CANCELLED


def test_clear_tasks_not_only_pending_also_clears_blocked_but_never_in_flight(store):
    ids = store.set_tasks("lane-a", [{"prompt": "a"}, {"prompt": "b"}])
    store.transition_task(ids[0], DISPATCHING, event_type="TEST")
    store.transition_task(ids[0], RUNNING, event_type="TEST")
    store.transition_task(ids[0], BLOCKED, event_type="TEST", reason="fail")
    cleared = store.clear_tasks("lane-a", only_pending=False)
    assert store.get_task(ids[0]).status == CANCELLED
    assert store.get_task(ids[1]).status == CANCELLED
    assert cleared == 2


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------

def test_record_event_and_list_events_roundtrip(store):
    task_id = _make_one_task(store)
    store.record_event(session="lane-a", task_id=task_id, event_type="MANUAL_INTERVENTION",
                       reason="human typed into the pane")
    events = store.list_events("lane-a")
    assert any(e["event_type"] == "MANUAL_INTERVENTION" and e["reason"] == "human typed into the pane"
              for e in events)


def test_list_all_lanes_reports_every_session_independently(store):
    store.set_tasks("lane-a", [{"prompt": "a"}])
    store.set_tasks("lane-b", [{"prompt": "b1"}, {"prompt": "b2"}])
    lanes = {lane["session"]: lane for lane in store.list_all_lanes()}
    assert lanes["lane-a"]["total_count"] == 1
    assert lanes["lane-b"]["total_count"] == 2
