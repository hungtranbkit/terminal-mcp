"""Orchestration V1 -- connect the deterministic runtime to the event bus.

THE GAP THIS CLOSES. The baseline audit's central finding was that the bus's
own vocabulary (TASK_CREATED, VERIFY_PENDING, WORKER_DONE, MERGE_CONFLICT)
names exactly the signals the queue, verify queue and resource locks
produce -- and none of them ever called publish(). Six subsystems, zero code
coupling, one event in production. Everything above them (a coordinator that
reacts, a report that explains) was waiting on a stream that nobody fed.

WHY A SEPARATE MODULE. The stores stay ignorant of the bus: each takes an
optional `event_sink` callable and knows nothing about event types, project
scoping or idempotency. That keeps the mapping -- which is policy, and will
change -- out of the persistence layer, and keeps every store testable with
a plain list as its sink.

DELIVERY IS AT-LEAST-ONCE, AND THAT IS DELIBERATE. The bus is a different
database from the queue, so there is no cross-store transaction to be had.
Publishing INSIDE the queue's transaction would allow an event describing a
transition that then rolled back -- strictly worse than the reverse. So
publishing happens after commit, and the gap between them is covered by
idempotency rather than by locking: every event derived from a queue
transition is keyed on the `queue_events` row id, which is an autoincrement
primary key and therefore already the perfect natural key. A republish of
the same transition returns the original event and creates nothing.
"""
from __future__ import annotations

from typing import Any, Callable

from .queue_store import (
    BLOCKED, COMPLETED, FAILED, PRECHECK, READY, RUNNING, VERIFYING,
    WAITING_SESSION,
)

# queue transition -> bus vocabulary. Keyed on the DESTINATION status where
# one exists, because that is what a consumer reacts to; a few queue event
# types carry meaning the status alone does not, and are matched first.
_EVENT_TYPE_BY_QUEUE_EVENT: dict[str, str] = {
    "ENQUEUED": "TASK_CREATED",
    "CLAIMED": "TASK_CLAIMED",
    "CLAIM_RELEASED": "LEASE_RELEASED",
    "CLAIM_HANDOFF": "TASK_HANDOFF",
    "RECOVERED_AFTER_RESTART": "LEASE_EXPIRED",
    # WORKER_DONE, not VERIFY_PENDING: this queue transition says the
    # IMPLEMENTER finished. The verify queue emits VERIFY_PENDING itself,
    # carrying the job id and required capabilities a consumer actually
    # needs to route it -- mapping both to VERIFY_PENDING published the same
    # signal twice, once without the information that makes it actionable.
    "VERIFY_REQUESTED": "WORKER_DONE",
}

_EVENT_TYPE_BY_STATUS: dict[str, str] = {
    READY: "TASK_READY",
    # DISPATCHING is deliberately absent: it means "a send is in flight",
    # which is a delivery mechanic, not a coordination signal. Mapping it
    # too would emit TASK_STARTED twice for every task -- once when the
    # send begins and again when the agent actually starts -- and a
    # consumer counting starts would double every number.
    RUNNING: "TASK_STARTED",
    VERIFYING: "WORKER_DONE",
    COMPLETED: "TASK_COMPLETED",
    FAILED: "TASK_FAILED",
    BLOCKED: "TASK_BLOCKED",
    WAITING_SESSION: "TASK_BLOCKED",
    PRECHECK: "TASK_CLAIMED",
}

DEFAULT_ACTOR = "queue-store"


def queue_event_to_bus(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Translate one recorded queue event into a bus publish, or None when
    it carries no signal worth broadcasting.

    Returning None is a real answer, not a failure: the queue records
    bookkeeping events (lane pause/resume, project scoping) that a
    coordinator has no reason to wake for, and forwarding everything would
    make the stream noise."""
    queue_event_id = entry.get("queue_event_id")
    if queue_event_id is None:
        return None
    event_type = _EVENT_TYPE_BY_QUEUE_EVENT.get(str(entry.get("event_type") or ""))
    if event_type is None:
        event_type = _EVENT_TYPE_BY_STATUS.get(str(entry.get("to_status") or ""))
    if event_type is None:
        return None
    return {
        "type": event_type,
        "project_id": entry.get("project_id"),
        "entity_type": "task",
        "entity_id": entry.get("task_id"),
        # The queue_events row id is an autoincrement PK, so it is already
        # the perfect natural idempotency key for the transition it records.
        "idempotency_key": f"queue_event:{queue_event_id}",
        "payload": {
            "session": entry.get("session"),
            "from_status": entry.get("from_status"),
            "to_status": entry.get("to_status"),
            "reason": entry.get("reason"),
            "outcome_id": entry.get("outcome_id"),
            "queue_event_id": queue_event_id,
        },
    }


def build_queue_event_sink(bus: Any, *, actor: str = DEFAULT_ACTOR) -> Callable[[dict], None]:
    """The callable QueueStore(event_sink=...) takes.

    Never raises: QueueStore already swallows sink exceptions so a
    publishing glitch cannot un-commit a state transition, and this keeps
    that contract explicit rather than relying on the caller's rescue."""
    def _sink(entry: dict[str, Any]) -> None:
        publish = queue_event_to_bus(entry)
        if publish is None:
            return
        bus.publish(publish["type"], project_id=publish["project_id"],
                    entity_type=publish["entity_type"], entity_id=publish["entity_id"],
                    payload=publish["payload"], idempotency_key=publish["idempotency_key"],
                    actor=actor)
    return _sink


def build_verify_event_sink(bus: Any, *, actor: str = "verify-queue") -> Callable[[dict], None]:
    """Verify-job state changes onto the bus.

    VERIFY_PENDING is the one the audit specifically called out: the bus
    DEFINED that type and the verify queue never emitted it, so nothing
    could ever react to "this needs verifying"."""
    mapping = {
        "VERIFY_PENDING": "VERIFY_PENDING",
        "VERIFY_CLAIMED": "VERIFY_CLAIMED",
        "VERIFIED_PASS": "VERIFY_PASS",
        "VERIFIED_FAIL": "VERIFY_FAIL",
        "NEEDS_REWORK": "VERIFY_FAIL",
        "VERIFY_BLOCKED": "VERIFY_BLOCKED",
    }

    def _sink(entry: dict[str, Any]) -> None:
        event_type = mapping.get(str(entry.get("status") or ""))
        if event_type is None:
            return
        bus.publish(event_type, project_id=entry.get("project_id"),
                    entity_type="verify_job", entity_id=entry.get("job_id"),
                    payload={k: v for k, v in entry.items() if k != "claim_token"},
                    idempotency_key=f"verify:{entry.get('job_id')}:{entry.get('status')}"
                                    f":{entry.get('attempt')}",
                    actor=actor)
    return _sink


def publish_resource_conflict(bus: Any, *, project_id: str, resource_key: str,
                              requester: str, holder: dict[str, Any] | None,
                              actor: str = "resource-lock") -> dict[str, Any] | None:
    """RESOURCE_CONFLICT -- emitted when a worker is refused a lock.

    Not published from inside ResourceLockStore: a refusal is a completely
    ordinary, expected outcome that happens on a hot path, and turning every
    one into a durable row would flood the log. The CALLER decides that a
    particular refusal is worth escalating -- which is exactly the decision
    the coordinator needs to make."""
    if bus is None:
        return None
    return bus.publish("RESOURCE_CONFLICT", project_id=project_id,
                       entity_type="resource", entity_id=resource_key,
                       payload={"requester": requester, "resource_key": resource_key,
                                "holder": holder},
                       actor=actor)
