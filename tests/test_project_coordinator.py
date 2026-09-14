"""P1.1 per-project event-driven coordinator (blg_324738a9eaf7).

The eight scenarios the implementation contract names, each as its own
class so a failure says which guarantee broke:

  1. two projects in isolation          TestProjectIsolation
  2. duplicate / replay idempotency     TestReplayIdempotency
  3. out-of-order events                TestOutOfOrder
  4. restart recovery                   TestRestartRecovery
  5. blocked dependency                 TestBlockedDependency
  6. missing project / scope            TestMissingProjectAndScope
  7. concurrent events                  TestConcurrency
  8. backward compatibility             TestBackwardCompatibility
"""
from __future__ import annotations

import threading

import pytest

from terminal_mcp.event_bus import EventBus
from terminal_mcp.project_coordinator import (
    CLARIFY,
    DECOMPOSE,
    DEPENDENCY_BLOCKED,
    HOLD,
    NOOP,
    PRIORITISE,
    REASON_BLOCKED_DEPENDENCY,
    REASON_FOREIGN_EVENT,
    REASON_MALFORMED_PAYLOAD,
    REASON_MISSING_ENTITY,
    REASON_OWN_OUTPUT,
    REASON_PROJECT_PAUSED,
    REASON_PROJECT_UNREADABLE,
    REASON_STALE_EVENT,
    REASON_UNCLEAR_SCOPE,
    REASON_UNKNOWN_PROJECT,
    CoordinationAction,
    ProjectCoordinator,
    action_idempotency_key,
    consumer_name,
    decide,
)

P1 = "git:github.com/acme/one"
P2 = "git:github.com/acme/two"


@pytest.fixture
def bus(tmp_path) -> EventBus:
    return EventBus(tmp_path / "events.db")


def _resolver(**per_project):
    """A project source-of-truth stub. Unknown project -> None, which is
    the fail-closed case, not an empty dict."""
    def resolve(project_id: str):
        return per_project.get(project_id)
    return resolve


def _coordinator(bus, *, resolver=None, enabled=True, **kwargs) -> ProjectCoordinator:
    return ProjectCoordinator(
        bus=bus, enabled=enabled,
        project_resolver=resolver or _resolver(**{P1: {"project_id": P1}, P2: {"project_id": P2}}),
        **kwargs)


def _actions(result) -> list[str]:
    return [a["action"] for a in result["actions"]]


class TestProjectIsolation:
    def test_a_drain_only_sees_its_own_project(self, bus):
        bus.publish("TASK_CREATED", project_id=P1, entity_id="t1", payload={"prompt": "a" * 80})
        bus.publish("TASK_CREATED", project_id=P2, entity_id="t2", payload={"prompt": "b" * 80})
        coordinator = _coordinator(bus)

        first = coordinator.drain(P1)
        assert first["processed"] == 1
        assert first["actions"][0]["entity_id"] == "t1"

        second = coordinator.drain(P2)
        assert second["processed"] == 1
        assert second["actions"][0]["entity_id"] == "t2"

    def test_one_project_draining_does_not_advance_the_others_cursor(self, bus):
        bus.publish("TASK_CREATED", project_id=P1, entity_id="t1", payload={"prompt": "a" * 80})
        bus.publish("TASK_CREATED", project_id=P2, entity_id="t2", payload={"prompt": "b" * 80})
        coordinator = _coordinator(bus)

        coordinator.drain(P1)
        assert coordinator.cursor(P2) == 0, "P2's position moved because P1 was drained"
        assert coordinator.drain(P2)["processed"] == 1

    def test_the_consumer_name_carries_the_project(self, bus):
        """Isolation is structural: the cursor is keyed on (consumer,
        project), and the project is in the consumer name too."""
        assert consumer_name(P1) != consumer_name(P2)
        assert P1 in consumer_name(P1)

    def test_an_event_from_another_project_is_refused_even_if_handed_over(self):
        """Defence in depth -- read_since already filters, so this is the
        branch that catches a caller passing the wrong scope."""
        event = {"id": "e1", "seq": 1, "project_id": P2, "type": "TASK_CREATED", "payload": {}}
        action = decide(event, project_id=P1, project_state={"project_id": P1})
        assert action.action == HOLD
        assert action.reason == REASON_FOREIGN_EVENT

    def test_actions_are_published_into_their_own_project(self, bus):
        bus.publish("TASK_CREATED", project_id=P1, entity_id="t1", payload={"prompt": "a" * 80})
        _coordinator(bus).drain(P1)
        assert [e["project_id"] for e in bus.list_events(project_id=P2, limit=50)] == []


class TestReplayIdempotency:
    def test_draining_twice_publishes_one_action(self, bus):
        bus.publish("TASK_CREATED", project_id=P1, entity_id="t1", payload={"prompt": "a" * 80})
        coordinator = _coordinator(bus)
        coordinator.drain(P1)
        before = len(bus.list_events(project_id=P1, limit=100))

        # Rewind is impossible through the API, so replay the event by
        # constructing the same decision again -- exactly what a crash
        # between publish and commit_cursor would cause.
        source = bus.list_events(project_id=P1, types=["TASK_CREATED"], limit=1)[0]
        action = decide(source, project_id=P1, project_state={"project_id": P1})
        republished = coordinator._publish(action)

        assert republished["duplicate"] is True
        assert len(bus.list_events(project_id=P1, limit=100)) == before

    def test_the_idempotency_key_is_derived_from_the_source_event(self):
        key = action_idempotency_key(P1, "evt-1", PRIORITISE)
        assert key == action_idempotency_key(P1, "evt-1", PRIORITISE)
        assert key != action_idempotency_key(P1, "evt-2", PRIORITISE)
        assert key != action_idempotency_key(P2, "evt-1", PRIORITISE)
        assert key != action_idempotency_key(P1, "evt-1", HOLD)

    def test_a_duplicate_does_not_re_run_the_sink(self, bus):
        """An idempotent RECORD is not the same as an idempotent EFFECT.
        The sink must fire once, not once per delivery."""
        fired: list[str] = []
        coordinator = _coordinator(bus, action_sink=lambda a: fired.append(a.action))
        bus.publish("TASK_CREATED", project_id=P1, entity_id="t1", payload={"prompt": "a" * 80})
        coordinator.drain(P1)
        assert fired == [PRIORITISE]

        source = bus.list_events(project_id=P1, types=["TASK_CREATED"], limit=1)[0]
        coordinator._publish(decide(source, project_id=P1, project_state={"project_id": P1}))
        assert fired == [PRIORITISE], "the sink re-fired for a duplicate action"

    def test_the_coordinator_does_not_react_to_its_own_output(self, bus):
        """Without this the stream grows on every drain: each action is
        published onto the project stream the coordinator reads."""
        bus.publish("TASK_CREATED", project_id=P1, entity_id="t1", payload={"prompt": "a" * 80})
        coordinator = _coordinator(bus)
        for _ in range(5):
            coordinator.drain(P1)
        # 1 source event + 1 action, and nothing compounding.
        assert len(bus.list_events(project_id=P1, limit=100)) == 2

    def test_an_own_action_event_decides_noop(self):
        event = {"id": "e1", "seq": 5, "project_id": P1, "type": PRIORITISE,
                 "actor": "project-coordinator", "payload": {}}
        action = decide(event, project_id=P1, project_state={"project_id": P1})
        assert action.action == NOOP
        assert action.reason == REASON_OWN_OUTPUT

    def test_a_noop_is_never_published(self, bus):
        bus.publish("SOMETHING_UNHANDLED", project_id=P1, payload={})
        result = _coordinator(bus).drain(P1)
        assert _actions(result) == [NOOP]
        assert result["actions"][0]["published"] is False
        assert len(bus.list_events(project_id=P1, limit=50)) == 1


class TestOutOfOrder:
    def test_an_event_at_or_below_the_acted_seq_is_held_as_stale(self):
        event = {"id": "e1", "seq": 3, "project_id": P1, "type": "TASK_CREATED",
                 "payload": {"prompt": "a" * 80}}
        action = decide(event, project_id=P1, project_state={"project_id": P1}, last_acted_seq=7)
        assert action.action == HOLD
        assert action.reason == REASON_STALE_EVENT
        assert action.detail["last_acted_seq"] == 7

    def test_a_newer_event_is_still_acted_on(self):
        event = {"id": "e1", "seq": 9, "project_id": P1, "type": "TASK_CREATED",
                 "entity_id": "t1", "payload": {"prompt": "a" * 80}}
        action = decide(event, project_id=P1, project_state={"project_id": P1}, last_acted_seq=7)
        assert action.action == PRIORITISE

    def test_a_late_commit_cannot_rewind_the_cursor(self, bus):
        """The bus makes commit_cursor monotonic; this pins that the
        coordinator depends on that and does not defeat it."""
        consumer = consumer_name(P1)
        bus.commit_cursor(consumer, 10, project_id=P1)
        bus.commit_cursor(consumer, 4, project_id=P1)
        assert _coordinator(bus).cursor(P1) == 10

    def test_events_are_processed_in_seq_order(self, bus):
        for index in range(3):
            bus.publish("TASK_CREATED", project_id=P1, entity_id=f"t{index}",
                        payload={"prompt": "a" * 80})
        result = _coordinator(bus).drain(P1)
        assert [a["entity_id"] for a in result["actions"]] == ["t0", "t1", "t2"]


class TestRestartRecovery:
    def test_a_new_instance_resumes_from_the_stored_cursor(self, tmp_path):
        path = tmp_path / "events.db"
        bus = EventBus(path)
        bus.publish("TASK_CREATED", project_id=P1, entity_id="t1", payload={"prompt": "a" * 80})
        first = _coordinator(bus).drain(P1)
        assert first["processed"] == 1

        # A whole new process: new bus handle, new coordinator, same file.
        restarted = _coordinator(EventBus(path))
        assert restarted.cursor(P1) == first["cursor"]
        assert restarted.drain(P1)["processed"] == 1  # the action event, decided NOOP

    def test_no_double_action_across_restart(self, tmp_path):
        path = tmp_path / "events.db"
        bus = EventBus(path)
        bus.publish("TASK_CREATED", project_id=P1, entity_id="t1", payload={"prompt": "a" * 80})
        _coordinator(bus).drain(P1)

        for _ in range(3):
            _coordinator(EventBus(path)).drain(P1)

        prioritised = EventBus(path).list_events(project_id=P1, types=[PRIORITISE], limit=50)
        assert len(prioritised) == 1

    def test_a_crash_before_commit_replays_the_batch_without_duplicating(self, tmp_path):
        """The cursor advances only after the whole batch is decided, so a
        crash mid-batch re-reads it -- safe only because each action's key
        makes the republish a no-op."""
        path = tmp_path / "events.db"
        bus = EventBus(path)
        for index in range(3):
            bus.publish("TASK_CREATED", project_id=P1, entity_id=f"t{index}",
                        payload={"prompt": "a" * 80})

        crashing = _coordinator(bus)
        original_commit = crashing.bus.commit_cursor

        def boom(*args, **kwargs):
            raise RuntimeError("process killed before the cursor was committed")

        crashing.bus.commit_cursor = boom
        with pytest.raises(RuntimeError):
            crashing.drain(P1)

        crashing.bus.commit_cursor = original_commit
        recovered = _coordinator(EventBus(path))
        assert recovered.cursor(P1) == 0  # the crash never committed
        recovered.drain(P1)

        prioritised = EventBus(path).list_events(project_id=P1, types=[PRIORITISE], limit=50)
        assert len(prioritised) == 3, "replay after the crash duplicated actions"


class TestBlockedDependency:
    def test_a_blocked_task_is_reported_blocked_not_coordinated_around(self):
        event = {"id": "e1", "seq": 1, "project_id": P1, "type": "TASK_CREATED",
                 "entity_id": "t9", "payload": {"prompt": "a" * 80, "blocked_by": ["t1", "t2"]}}
        action = decide(event, project_id=P1, project_state={"project_id": P1})
        assert action.action == DEPENDENCY_BLOCKED
        assert action.reason == REASON_BLOCKED_DEPENDENCY
        assert action.detail["blocked_by"] == ["t1", "t2"]

    def test_a_dependency_trigger_event_blocks(self):
        for event_type in ("TASK_BLOCKED", "MERGE_CONFLICT", "TEST_FAILED"):
            event = {"id": "e1", "seq": 1, "project_id": P1, "type": event_type,
                     "entity_id": "t9", "payload": {}}
            action = decide(event, project_id=P1, project_state={"project_id": P1})
            assert action.action == DEPENDENCY_BLOCKED, event_type

    def test_a_blocked_event_without_an_entity_holds(self):
        event = {"id": "e1", "seq": 1, "project_id": P1, "type": "MERGE_CONFLICT", "payload": {}}
        action = decide(event, project_id=P1, project_state={"project_id": P1})
        assert action.action == HOLD
        assert action.reason == REASON_MISSING_ENTITY

    def test_the_coordinator_never_dispatches(self, bus):
        """Dispatch stays CoordinatorGate's and the queue's job. The
        coordinator's whole output is events."""
        bus.publish("TASK_CREATED", project_id=P1, entity_id="t1", payload={"prompt": "a" * 80})
        published = _coordinator(bus).drain(P1)["actions"]
        assert all(a["action"].startswith("COORDINATION_") for a in published)


class TestMissingProjectAndScope:
    def test_an_unknown_project_holds(self, bus):
        bus.publish("TASK_CREATED", project_id="ghost", entity_id="t1",
                    payload={"prompt": "a" * 80})
        result = _coordinator(bus).drain("ghost")
        assert _actions(result) == [HOLD]
        assert result["actions"][0]["reason"] == REASON_UNKNOWN_PROJECT

    def test_an_unreadable_project_holds(self):
        action = decide({"id": "e1", "seq": 1, "project_id": P1, "type": "TASK_CREATED",
                         "payload": {}},
                        project_id=P1, project_state={"error": "QUEUE_UNAVAILABLE"})
        assert action.action == HOLD
        assert action.reason == REASON_PROJECT_UNREADABLE

    def test_a_paused_project_holds(self):
        action = decide({"id": "e1", "seq": 1, "project_id": P1, "type": "TASK_CREATED",
                         "payload": {}},
                        project_id=P1, project_state={"project_id": P1, "paused": True})
        assert action.action == HOLD
        assert action.reason == REASON_PROJECT_PAUSED

    def test_a_resolver_that_raises_holds(self, bus):
        def explode(project_id):
            raise RuntimeError("registry down")

        bus.publish("TASK_CREATED", project_id=P1, entity_id="t1", payload={"prompt": "a" * 80})
        result = _coordinator(bus, resolver=explode).drain(P1)
        assert _actions(result) == [HOLD]
        assert result["actions"][0]["reason"] == REASON_PROJECT_UNREADABLE

    def test_a_malformed_payload_holds_rather_than_reading_as_empty(self):
        action = decide({"id": "e1", "seq": 1, "project_id": P1, "type": "TASK_CREATED",
                         "payload": "not json at all {"},
                        project_id=P1, project_state={"project_id": P1})
        assert action.action == HOLD
        assert action.reason == REASON_MALFORMED_PAYLOAD

    def test_an_empty_project_id_is_refused(self, bus):
        assert _coordinator(bus).drain("")["error"] == "INVALID_REQUEST"

    def test_an_unclear_scope_asks_for_clarification(self):
        action = decide({"id": "e1", "seq": 1, "project_id": P1, "type": "GOAL_SUBMITTED",
                         "payload": {"goal": "fix it"}},
                        project_id=P1, project_state={"project_id": P1},
                        scope_reasoner=lambda text: "too vague" if len(text) < 40 else None)
        assert action.action == CLARIFY
        assert action.reason == REASON_UNCLEAR_SCOPE
        assert action.detail["scope_reason"] == "too vague"

    def test_a_clear_scope_decomposes(self):
        action = decide({"id": "e1", "seq": 1, "project_id": P1, "type": "GOAL_SUBMITTED",
                         "entity_id": "g1", "payload": {"goal": "g" * 200}},
                        project_id=P1, project_state={"project_id": P1},
                        scope_reasoner=lambda text: None)
        assert action.action == DECOMPOSE

    def test_a_reasoner_that_raises_is_a_refusal_not_an_approval(self):
        """An exception is an absence of judgment, and an absence is
        never read as a yes."""
        def explode(text):
            raise ValueError("llm unreachable")

        action = decide({"id": "e1", "seq": 1, "project_id": P1, "type": "GOAL_SUBMITTED",
                         "payload": {"goal": "g" * 200}},
                        project_id=P1, project_state={"project_id": P1}, scope_reasoner=explode)
        assert action.action == HOLD
        assert action.reason == REASON_UNCLEAR_SCOPE
        assert "ValueError" in action.detail["reasoner_error"]

    def test_the_default_scope_reasoner_plugs_in_unchanged(self):
        """The seam the roadmap names: coordinator.py's own pluggable
        callable, same signature, no adapter."""
        from terminal_mcp.coordinator import _default_scope_reasoner

        action = decide({"id": "e1", "seq": 1, "project_id": P1, "type": "GOAL_SUBMITTED",
                         "payload": {"goal": "short"}},
                        project_id=P1, project_state={"project_id": P1},
                        scope_reasoner=_default_scope_reasoner)
        assert action.action == CLARIFY


class TestConcurrency:
    def test_two_coordinators_draining_one_project_publish_one_action_each(self, tmp_path):
        path = tmp_path / "events.db"
        seeding = EventBus(path)
        for index in range(8):
            seeding.publish("TASK_CREATED", project_id=P1, entity_id=f"t{index}",
                            payload={"prompt": "a" * 80})

        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def run():
            try:
                coordinator = _coordinator(EventBus(path))
                barrier.wait(timeout=10)
                coordinator.drain(P1)
            except BaseException as exc:  # noqa: BLE001 -- surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, errors
        actions = EventBus(path).list_events(project_id=P1, types=[PRIORITISE], limit=100)
        assert len(actions) == 8, "concurrent drains duplicated actions"

    def test_a_concurrent_sink_fires_once_per_source_event(self, tmp_path):
        path = tmp_path / "events.db"
        EventBus(path).publish("TASK_CREATED", project_id=P1, entity_id="t1",
                               payload={"prompt": "a" * 80})
        fired: list[str] = []
        lock = threading.Lock()

        def sink(action: CoordinationAction) -> None:
            with lock:
                fired.append(action.source_event_id or "")

        barrier = threading.Barrier(3)

        def run():
            coordinator = _coordinator(EventBus(path), action_sink=sink)
            barrier.wait(timeout=10)
            coordinator.drain(P1)

        threads = [threading.Thread(target=run) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert len(fired) == 1, f"sink fired {len(fired)} times for one event"


class TestBackwardCompatibility:
    def test_disabled_is_a_true_noop(self, bus):
        bus.publish("TASK_CREATED", project_id=P1, entity_id="t1", payload={"prompt": "a" * 80})
        result = _coordinator(bus, enabled=False).drain(P1)
        assert result == {"project_id": P1, "enabled": False, "processed": 0,
                          "actions": [], "cursor": 0}
        assert len(bus.list_events(project_id=P1, limit=50)) == 1

    def test_disabled_does_not_advance_the_cursor(self, bus):
        """Enabling the feature later must resume where the stream
        actually is, not skip everything that happened while it was off."""
        bus.publish("TASK_CREATED", project_id=P1, entity_id="t1", payload={"prompt": "a" * 80})
        _coordinator(bus, enabled=False).drain(P1)
        assert _coordinator(bus, enabled=False).cursor(P1) == 0
        assert _coordinator(bus).drain(P1)["processed"] == 1

    def test_disabled_is_the_default(self, bus):
        assert ProjectCoordinator(bus=bus).enabled is False

    def test_it_adds_no_table_and_no_database(self, tmp_path):
        """Zero new state is the design constraint: the cursor and the
        actions are both ordinary bus rows. A coordinator keeping its own
        copy of what it had processed would be a second source of truth
        to drift from the bus."""
        import sqlite3

        path = tmp_path / "events.db"
        bus = EventBus(path)
        with sqlite3.connect(path) as connection:
            baseline = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}

        bus.publish("TASK_CREATED", project_id=P1, entity_id="t1", payload={"prompt": "a" * 80})
        _coordinator(bus).drain(P1)

        with sqlite3.connect(path) as connection:
            assert {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")} == baseline
        assert sorted(f.name for f in tmp_path.iterdir() if f.suffix == ".db") == ["events.db"]

    def test_an_unhandled_event_type_is_left_alone(self, bus):
        bus.publish("PREVIEW_FAILED", project_id=P1, entity_id="x", payload={})
        result = _coordinator(bus).drain(P1)
        assert _actions(result) == [NOOP]

    def test_the_existing_queue_and_gate_are_untouched(self):
        """This module imports neither -- dispatch safety is not something
        it can affect even by accident."""
        from pathlib import Path

        import terminal_mcp.project_coordinator as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "import queue_engine" not in source
        assert "from .queue_engine" not in source
        assert "from .queue_loop" not in source
