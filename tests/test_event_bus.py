"""P0.2 Event Bus -- claim/lease/idempotency/recovery/isolation.

Concurrency is tested with real PROCESSES, not threads: the guarantee is
`BEGIN IMMEDIATE` serialising two independent SQLite connections, which
threads in one interpreter would not exercise honestly.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from terminal_mcp.event_bus import (ACKED, CLAIMED, FAILED, PENDING, TASK_CREATED,
                                    VERIFY_PENDING, WORKER_DONE, EventBus)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bus(tmp_path):
    return EventBus(tmp_path / "events.db")


# ------------------------------------------------------------ publish/read
def test_publish_returns_a_sequenced_pending_event(bus):
    e = bus.publish(TASK_CREATED, project_id="p1", entity_type="task", entity_id="t1")
    assert e["status"] == PENDING and e["seq"] == 1 and e["duplicate"] is False
    assert e["project_id"] == "p1" and e["entity_id"] == "t1"


def test_payload_round_trips_as_an_object(bus):
    e = bus.publish(TASK_CREATED, project_id="p1", payload={"a": 1, "nested": {"b": [1, 2]}})
    assert bus.get(e["id"])["payload"] == {"a": 1, "nested": {"b": [1, 2]}}


def test_oversized_payload_is_refused(bus):
    with pytest.raises(ValueError, match="too large"):
        bus.publish(TASK_CREATED, payload={"blob": "x" * 70_000})


def test_per_project_ordering_is_by_seq(bus):
    for n in range(5):
        bus.publish(TASK_CREATED, project_id="p1", entity_id=f"t{n}")
        bus.publish(TASK_CREATED, project_id="p2", entity_id=f"o{n}")
    p1 = bus.list_events(project_id="p1")
    assert [e["entity_id"] for e in p1] == [f"t{n}" for n in range(5)]
    assert [e["seq"] for e in p1] == sorted(e["seq"] for e in p1)


def test_since_seq_pagination(bus):
    ids = [bus.publish(TASK_CREATED, project_id="p1")["seq"] for _ in range(4)]
    later = bus.list_events(project_id="p1", since_seq=ids[1])
    assert [e["seq"] for e in later] == ids[2:]


# ------------------------------------------------------------ idempotency
def test_same_idempotency_key_returns_the_original(bus):
    first = bus.publish(TASK_CREATED, project_id="p1", idempotency_key="k")
    second = bus.publish(TASK_CREATED, project_id="p1", idempotency_key="k")
    assert second["id"] == first["id"] and second["duplicate"] is True
    assert len(bus.list_events(project_id="p1")) == 1


def test_different_keys_are_different_events(bus):
    a = bus.publish(TASK_CREATED, project_id="p1", idempotency_key="a")
    b = bus.publish(TASK_CREATED, project_id="p1", idempotency_key="b")
    assert a["id"] != b["id"]


# ------------------------------------------------------------------ claim
def test_claim_is_fifo_and_marks_claimed(bus):
    bus.publish(TASK_CREATED, project_id="p1", entity_id="first")
    bus.publish(TASK_CREATED, project_id="p1", entity_id="second")
    c = bus.claim_next(consumer="w1", project_id="p1")
    assert c["entity_id"] == "first" and c["status"] == CLAIMED and c["claimed_by"] == "w1"


def test_claim_filters_by_type(bus):
    bus.publish(TASK_CREATED, project_id="p1")
    bus.publish(VERIFY_PENDING, project_id="p1")
    c = bus.claim_next(consumer="verifier", project_id="p1", types=[VERIFY_PENDING])
    assert c["type"] == VERIFY_PENDING


def test_claim_returns_none_when_nothing_matches(bus):
    bus.publish(TASK_CREATED, project_id="p1")
    assert bus.claim_next(consumer="w", project_id="other") is None
    assert bus.claim_next(consumer="w", project_id="p1", types=[WORKER_DONE]) is None


def test_a_claimed_event_is_not_reclaimed_while_leased(bus):
    bus.publish(TASK_CREATED, project_id="p1")
    assert bus.claim_next(consumer="w1", project_id="p1") is not None
    assert bus.claim_next(consumer="w2", project_id="p1") is None


# ------------------------------------------------- ack / release / recovery
def test_ack_requires_the_current_token(bus):
    bus.publish(TASK_CREATED, project_id="p1")
    c = bus.claim_next(consumer="w1", project_id="p1")
    assert bus.ack(c["id"], "wrong-token") is False
    assert bus.ack(c["id"], c["claim_token"]) is True
    assert bus.get(c["id"])["status"] == ACKED


def test_release_puts_it_back_for_another_consumer(bus):
    bus.publish(TASK_CREATED, project_id="p1")
    c = bus.claim_next(consumer="w1", project_id="p1")
    assert bus.release(c["id"], c["claim_token"], error="busy") is True
    again = bus.claim_next(consumer="w2", project_id="p1")
    assert again["id"] == c["id"] and again["claimed_by"] == "w2"


def test_expired_lease_is_reclaimable_crash_recovery(bus):
    """A consumer that dies mid-handling must not strand the event."""
    bus.publish(TASK_CREATED, project_id="p1")
    c = bus.claim_next(consumer="crashed", project_id="p1", lease_seconds=-1)  # already expired
    assert bus.claim_next(consumer="w2", project_id="p1")["id"] == c["id"]


def test_fail_stops_redelivery_and_retry_resumes_it(bus):
    bus.publish(TASK_CREATED, project_id="p1")
    c = bus.claim_next(consumer="w1", project_id="p1")
    assert bus.fail(c["id"], c["claim_token"], error="poison") is True
    assert bus.get(c["id"])["status"] == FAILED
    assert bus.claim_next(consumer="w2", project_id="p1") is None      # not redelivered
    assert bus.retry(c["id"]) is True
    assert bus.claim_next(consumer="w2", project_id="p1")["id"] == c["id"]


def test_attempt_budget_stops_an_infinite_redelivery_loop(bus):
    bus.publish(TASK_CREATED, project_id="p1")
    for _ in range(10):
        c = bus.claim_next(consumer="w", project_id="p1", lease_seconds=-1)
        if c is None:
            break
    assert bus.claim_next(consumer="w", project_id="p1", lease_seconds=-1) is None


# ------------------------------------------------------------- isolation
def test_projects_are_isolated(bus):
    bus.publish(TASK_CREATED, project_id="p1", entity_id="a")
    bus.publish(TASK_CREATED, project_id="p2", entity_id="b")
    assert [e["entity_id"] for e in bus.list_events(project_id="p1")] == ["a"]
    assert bus.claim_next(consumer="w", project_id="p1")["entity_id"] == "a"
    assert bus.claim_next(consumer="w", project_id="p1") is None       # p2's event untouched
    assert bus.claim_next(consumer="w", project_id="p2")["entity_id"] == "b"


def test_unscoped_events_are_not_returned_by_a_project_claim(bus):
    bus.publish(TASK_CREATED, project_id=None, entity_id="global")
    assert bus.claim_next(consumer="w", project_id="p1") is None
    assert bus.claim_next(consumer="w")["entity_id"] == "global"       # unscoped claim sees it


# ------------------------------------------------------------- durability
def test_survives_a_fresh_instance(tmp_path):
    EventBus(tmp_path / "e.db").publish(TASK_CREATED, project_id="p1", entity_id="kept")
    assert [e["entity_id"] for e in EventBus(tmp_path / "e.db").list_events()] == ["kept"]


CLAIMER = textwrap.dedent("""
    import sys, json
    sys.path.insert(0, {root!r})
    from terminal_mcp.event_bus import EventBus
    bus = EventBus({db!r})
    got = []
    while True:
        e = bus.claim_next(consumer={tag!r}, project_id="p1")
        if e is None:
            break
        got.append(e["entity_id"])
        bus.ack(e["id"], e["claim_token"])
    print(json.dumps(got))
""")


def test_two_processes_never_claim_the_same_event(tmp_path):
    """The real guarantee: BEGIN IMMEDIATE serialises two independent
    connections, so no event is delivered twice and none is lost."""
    db = tmp_path / "events.db"
    bus = EventBus(db)
    total = 30
    for n in range(total):
        bus.publish(TASK_CREATED, project_id="p1", entity_id=f"e{n}")

    procs = [subprocess.Popen(
        [sys.executable, "-c", CLAIMER.format(root=str(REPO_ROOT), db=str(db), tag=tag)],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for tag in ("A", "B")]
    claimed: list[str] = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err or out
        claimed.extend(__import__("json").loads(out.strip().splitlines()[-1]))

    assert sorted(claimed) == sorted(f"e{n}" for n in range(total)), "lost or duplicated"
    assert len(claimed) == len(set(claimed)), "an event was delivered twice"
    assert bus.stats() == {ACKED: total}
