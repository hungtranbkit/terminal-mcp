"""Orchestration V1 -- the deterministic runtime feeding the event bus.

The baseline audit's central finding was that the bus DEFINED exactly the
vocabulary the queue and verify queue produce (TASK_CREATED, VERIFY_PENDING,
WORKER_DONE, MERGE_CONFLICT) and none of them ever called publish(). Six
subsystems, zero coupling, one event in production. These tests pin the
wiring that closes that, and -- just as importantly -- pin that it changed
nothing about how the runtime behaves.
"""
from __future__ import annotations

import sqlite3

import pytest

from terminal_mcp import queue_store as qs
from terminal_mcp.event_bus import EventBus
from terminal_mcp.event_wiring import (
    build_queue_event_sink,
    build_verify_event_sink,
    publish_resource_conflict,
    queue_event_to_bus,
)
from terminal_mcp.lease import ResourceLockStore
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.verify_queue import VerifyQueue

PROJECT = "git:github.com/acme/widget"


@pytest.fixture
def bus(tmp_path) -> EventBus:
    return EventBus(tmp_path / "events.db")


@pytest.fixture
def store(tmp_path, bus) -> QueueStore:
    return QueueStore(tmp_path / "queue.db", event_sink=build_queue_event_sink(bus))


def scoped_task(store: QueueStore, *, session="lane-a") -> str:
    (task_id,) = store.set_tasks(session, [{"title": "t", "prompt": "p"}], replace_pending=False)
    store.set_task_project(task_id, PROJECT)
    return task_id


# -- 1. The end-to-end stream -------------------------------------------

def test_a_full_task_lifecycle_produces_the_expected_stream(store, bus, tmp_path):
    verify = VerifyQueue(store, event_sink=build_verify_event_sink(bus))
    task_id = scoped_task(store)
    store.transition_task(task_id, qs.DISPATCHING, event_type="D")
    task = store.transition_task(task_id, qs.RUNNING, event_type="R")
    job = verify.ensure_verify_job(task, required_capabilities=["python"])
    claimed = verify.claim_next(verifier="v1", capabilities=["python"])
    verify.complete(job.id, claimed.claim_token, evidence={"command": "pytest", "exit_code": 0})

    assert [e["type"] for e in bus.list_events()] == [
        "TASK_CREATED", "TASK_STARTED", "WORKER_DONE",
        "VERIFY_PENDING", "VERIFY_CLAIMED", "TASK_COMPLETED", "VERIFY_PASS",
    ]


def test_events_are_attributed_and_project_scoped(store, bus):
    verify = VerifyQueue(store, event_sink=build_verify_event_sink(bus))
    task_id = scoped_task(store)
    store.transition_task(task_id, qs.DISPATCHING, event_type="D")
    task = store.transition_task(task_id, qs.RUNNING, event_type="R")
    verify.ensure_verify_job(task, required_capabilities=["python"])

    by_type = {e["type"]: e for e in bus.list_events()}
    assert by_type["TASK_STARTED"]["actor"] == "queue-store"
    assert by_type["VERIFY_PENDING"]["actor"] == "verify-queue"
    assert by_type["TASK_STARTED"]["project_id"] == PROJECT
    assert by_type["TASK_STARTED"]["entity_type"] == "task"
    assert by_type["VERIFY_PENDING"]["entity_type"] == "verify_job"
    # The routing information that makes VERIFY_PENDING actionable.
    assert by_type["VERIFY_PENDING"]["payload"]["required_capabilities"] == ["python"]


def test_dispatching_does_not_double_emit_a_start(store, bus):
    """DISPATCHING is a delivery mechanic, not a coordination signal.
    Mapping it too would emit TASK_STARTED twice per task and double every
    number a consumer counted."""
    task_id = scoped_task(store)
    store.transition_task(task_id, qs.DISPATCHING, event_type="D")
    store.transition_task(task_id, qs.RUNNING, event_type="R")
    assert [e["type"] for e in bus.list_events()].count("TASK_STARTED") == 1


def test_worker_done_and_verify_pending_are_distinct_signals(store, bus):
    """They used to both map to VERIFY_PENDING, publishing the same signal
    twice -- once without the job id or capabilities that make it routable."""
    verify = VerifyQueue(store, event_sink=build_verify_event_sink(bus))
    task_id = scoped_task(store)
    store.transition_task(task_id, qs.DISPATCHING, event_type="D")
    task = store.transition_task(task_id, qs.RUNNING, event_type="R")
    verify.ensure_verify_job(task, required_capabilities=["dotnet"])
    types = [e["type"] for e in bus.list_events()]
    assert types.count("VERIFY_PENDING") == 1
    assert types.count("WORKER_DONE") == 1


# -- 2. Delivery semantics ----------------------------------------------

def test_publishing_is_idempotent_per_queue_transition(store, bus):
    """Delivery is at-least-once by construction (the bus is a different
    database, so there is no cross-store transaction). The queue_events row
    id is an autoincrement PK and therefore already the perfect natural
    key, so a republish creates nothing."""
    task_id = scoped_task(store)
    store.transition_task(task_id, qs.DISPATCHING, event_type="D")
    before = len(bus.list_events())

    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    row = connection.execute("SELECT * FROM queue_events ORDER BY id DESC LIMIT 1").fetchone()
    connection.close()
    sink = build_queue_event_sink(bus)
    entry = {"queue_event_id": row["id"], "session": row["session"], "task_id": row["task_id"],
             "event_type": row["event_type"], "from_status": row["from_status"],
             "to_status": row["to_status"], "reason": row["reason"],
             "project_id": PROJECT, "outcome_id": None}
    sink(entry)
    sink(entry)
    assert len(bus.list_events()) == before


def test_a_rolled_back_transition_publishes_nothing(store, bus):
    """An event describing a transition that rolled back would be a lie."""
    task_id = scoped_task(store)
    before = len(bus.list_events())
    with pytest.raises(qs.InvalidTransitionError):
        store.transition_task(task_id, qs.COMPLETED, event_type="bogus")
    assert len(bus.list_events()) == before


def test_a_failing_sink_never_breaks_a_transition(tmp_path):
    """A publishing glitch must not un-commit real state."""
    def _explode(_entry):
        raise RuntimeError("bus is down")

    store = QueueStore(tmp_path / "queue.db", event_sink=_explode)
    (task_id,) = store.set_tasks("lane-a", [{"title": "t", "prompt": "p"}])
    store.transition_task(task_id, qs.DISPATCHING, event_type="D")
    assert store.get_task(task_id).status == qs.DISPATCHING


def test_verify_transaction_drains_its_own_task_events(store, bus):
    """ensure_verify_job moves a TASK through the store's chokepoint but
    commits on its OWN connection, so the store's context manager never
    runs. Without an explicit drain the queued event leaked into whatever
    unrelated queue operation committed next -- attributing it to the wrong
    moment, or losing it entirely."""
    verify = VerifyQueue(store, event_sink=build_verify_event_sink(bus))
    task_id = scoped_task(store)
    store.transition_task(task_id, qs.DISPATCHING, event_type="D")
    task = store.transition_task(task_id, qs.RUNNING, event_type="R")

    verify.ensure_verify_job(task, required_capabilities=["python"])
    # Immediately after the verify transaction -- before any further queue
    # operation could have drained on its behalf.
    assert "WORKER_DONE" in [e["type"] for e in bus.list_events()]


# -- 3. Behaviour is unchanged ------------------------------------------

def test_a_store_without_a_sink_behaves_identically(tmp_path):
    """The sink is optional and additive: no sink, no bus, no change."""
    plain = QueueStore(tmp_path / "plain.db")
    (task_id,) = plain.set_tasks("lane-a", [{"title": "t", "prompt": "p"}])
    plain.transition_task(task_id, qs.DISPATCHING, event_type="D")
    assert plain.get_task(task_id).status == qs.DISPATCHING
    assert plain._event_sink is None


def test_bookkeeping_events_are_not_broadcast():
    """The queue records events a coordinator has no reason to wake for.
    Forwarding everything would make the stream noise; returning None is a
    real answer, not a failure."""
    for event_type in ("LANE_PAUSED", "LANE_RESUMED", "AUTO_DISPATCH_DISABLED", "PROJECT_SET"):
        assert queue_event_to_bus({"queue_event_id": 1, "event_type": event_type,
                                   "to_status": None}) is None


def test_an_entry_without_a_queue_event_id_is_ignored():
    assert queue_event_to_bus({"event_type": "ENQUEUED"}) is None


# -- 4. Resource conflict -----------------------------------------------

def test_resource_conflict_is_published_by_the_caller_not_the_lock(tmp_path, bus):
    """A refused lock is an ordinary, expected outcome on a hot path;
    turning every one into a durable row would flood the log. The CALLER
    decides a particular refusal is worth escalating -- which is exactly
    the judgement the coordinator exists to make."""
    locks = ResourceLockStore(tmp_path / "leases.db")
    locks.acquire(PROJECT, "src/app.py", "worker-A", reason="editing")
    denied = locks.acquire(PROJECT, "src/app.py", "worker-B")
    assert denied["acquired"] is False
    assert bus.list_events() == [], "the lock store itself must not publish"

    published = publish_resource_conflict(bus, project_id=PROJECT, resource_key="src/app.py",
                                          requester="worker-B", holder=denied["holder"])
    assert published["type"] == "RESOURCE_CONFLICT"
    event = bus.list_events(types=["RESOURCE_CONFLICT"])[0]
    assert event["payload"]["requester"] == "worker-B"
    assert event["payload"]["holder"]["owner_id"] == "worker-A"
    assert event["project_id"] == PROJECT


# -- 5. Causation + cursors (what a coordinator needs) -------------------

def test_causation_chain_reconstructs_why_something_happened(bus):
    root = bus.publish("GOAL_SUBMITTED", project_id=PROJECT, actor="chatgpt")
    planned = bus.publish("TASK_CREATED", project_id=PROJECT, actor="coordinator",
                          causation_id=root["id"])
    started = bus.publish("TASK_STARTED", project_id=PROJECT, actor="queue-store",
                          causation_id=planned["id"])
    chain = bus.causation_chain(started["id"])
    assert [e["type"] for e in chain] == ["TASK_STARTED", "TASK_CREATED", "GOAL_SUBMITTED"]
    assert chain[-1]["actor"] == "chatgpt"


def test_a_cursor_is_a_durable_non_destructive_read_position(bus):
    """claim/ack is right for WORK (one event, one worker). A cursor is
    right for OBSERVATION: many independent readers, each with its own
    position, none consuming the event from the others."""
    for i in range(3):
        bus.publish("TASK_CREATED", project_id=PROJECT, idempotency_key=f"k{i}")

    first = bus.read_since("coordinator", project_id=PROJECT)
    assert len(first) == 3
    # Reading does not advance -- a consumer that crashes mid-handling
    # re-reads rather than silently skipping.
    assert len(bus.read_since("coordinator", project_id=PROJECT)) == 3

    bus.commit_cursor("coordinator", first[1]["seq"], project_id=PROJECT)
    assert [e["seq"] for e in bus.read_since("coordinator", project_id=PROJECT)] == [first[2]["seq"]]
    # A second consumer is completely unaffected by the first.
    assert len(bus.read_since("reporter", project_id=PROJECT)) == 3


def test_a_cursor_never_rewinds(bus):
    """An out-of-order or replayed commit must not cause reprocessing."""
    published = [bus.publish("TASK_CREATED", project_id=PROJECT, idempotency_key=f"k{i}")
                 for i in range(3)]
    bus.commit_cursor("c", published[2]["seq"], project_id=PROJECT)
    assert bus.commit_cursor("c", published[0]["seq"], project_id=PROJECT) == published[2]["seq"]
    assert bus.read_since("c", project_id=PROJECT) == []


def test_cursors_are_project_scoped(bus):
    bus.publish("TASK_CREATED", project_id="a", idempotency_key="a1")
    bus.publish("TASK_CREATED", project_id="b", idempotency_key="b1")
    events = bus.read_since("c", project_id="a")
    bus.commit_cursor("c", events[0]["seq"], project_id="a")
    assert bus.read_since("c", project_id="a") == []
    assert len(bus.read_since("c", project_id="b")) == 1


def test_migration_v2_is_additive_on_an_existing_bus(tmp_path):
    """events.db v1 exists in production with a real row."""
    path = tmp_path / "events.db"
    first = EventBus(path)
    published = first.publish("TASK_CREATED", project_id=PROJECT, idempotency_key="pre-existing")

    reopened = EventBus(path)
    row = reopened.get(published["id"])
    assert row["type"] == "TASK_CREATED"
    assert row["actor"] is None and row["causation_id"] is None
    assert reopened.cursor("anyone")["last_seq"] == 0
