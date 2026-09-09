"""Orchestration V1, P0-A: regression tests for defects the baseline audit
found in the deterministic foundations.

Each test here corresponds to a real bug that existed on main, not a
hypothetical. Where a bug was silent (it corrupted or hid data rather than
raising), the test asserts the observable consequence, because that is what
made it survive so long.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from terminal_mcp import queue_store as qs
from terminal_mcp.event_bus import FAILED, MAX_ATTEMPTS, EventBus
from terminal_mcp.lease import ResourceLockStore
from terminal_mcp.queue_store import QueueStore


@pytest.fixture
def store(tmp_path) -> QueueStore:
    return QueueStore(tmp_path / "queue.db")


# -- B1: handoff destroyed a task's provenance ---------------------------

def test_handoff_after_reassign_keeps_both_entries(store):
    (task_id,) = store.set_tasks("lane-a", [{"title": "t", "prompt": "p"}])
    store.reassign_task(task_id, "lane-b", reason="rebalance", actor="operator")
    claimed = store.claim_next_task("lane-b", claimed_by="worker-A")
    store.handoff_task(claimed.id, claimed.claim_token, to_worker="verifier-B",
                       reason="hand to verifier")

    history = store.get_task(task_id).migration_history
    events = [e.get("event") for e in history]
    assert len(history) == 2, f"expected reassign + handoff, got {history}"
    assert "handoff" in events
    assert any(e.get("to_session") == "lane-b" for e in history)


# -- B2: dependency cycles deadlocked a lane silently --------------------

def test_a_dependency_cycle_is_refused_at_creation(store):
    """The bug: no cycle check existed anywhere. The dispatch check is
    fail-closed, so a cycle produced a lane that was silently, permanently
    idle -- no event, no error, no alarm."""
    a, b = store.set_tasks("lane-a", [{"title": "a", "prompt": "p"},
                                      {"title": "b", "prompt": "p"}])
    connection = sqlite3.connect(store.path)
    connection.execute("UPDATE queue_tasks SET depends_on = ? WHERE id = ?", (json.dumps([a]), b))
    connection.commit()
    connection.close()

    with pytest.raises(QueueStore.DependencyError, match="cycle"):
        store.validate_dependencies(a, [b])


def test_self_dependency_is_refused(store):
    (a,) = store.set_tasks("lane-a", [{"title": "a", "prompt": "p"}])
    with pytest.raises(QueueStore.DependencyError, match="itself"):
        store.validate_dependencies(a, [a])


def test_a_dependency_on_a_nonexistent_task_is_refused(store):
    """Fail-closed at dispatch means a typo'd id is unmet FOREVER. Refusing
    it at creation is the only place it can still be reported."""
    (a,) = store.set_tasks("lane-a", [{"title": "a", "prompt": "p"}])
    with pytest.raises(QueueStore.DependencyError, match="do not exist"):
        store.validate_dependencies(a, ["no-such-task"])


def test_a_long_valid_chain_is_accepted(store):
    ids = store.set_tasks("lane-a", [{"title": f"t{i}", "prompt": "p"} for i in range(30)])
    connection = sqlite3.connect(store.path)
    for earlier, later in zip(ids, ids[1:]):
        connection.execute("UPDATE queue_tasks SET depends_on = ? WHERE id = ?",
                           (json.dumps([earlier]), later))
    connection.commit()
    connection.close()
    store.validate_dependencies(ids[0], [])          # no edges at all
    (fresh,) = store.set_tasks("lane-b", [{"title": "new", "prompt": "p"}])
    store.validate_dependencies(fresh, [ids[-1]])    # deep chain, no cycle


def test_dependency_deadlocks_reports_existing_damage(store):
    """A diagnostic for rows that are ALREADY broken -- validation at
    creation cannot help a database that already contains a cycle."""
    a, b = store.set_tasks("lane-a", [{"title": "a", "prompt": "p"},
                                      {"title": "b", "prompt": "p"}])
    connection = sqlite3.connect(store.path)
    connection.execute("UPDATE queue_tasks SET depends_on = ? WHERE id = ?", (json.dumps([b]), a))
    connection.execute("UPDATE queue_tasks SET depends_on = ? WHERE id = ?", (json.dumps([a]), b))
    connection.commit()
    connection.close()

    stuck = store.dependency_deadlocks()
    assert {row["task_id"] for row in stuck} == {a, b}
    assert all(row["cycle"] for row in stuck)


def test_dependency_deadlocks_ignores_terminal_tasks(store):
    a, b = store.set_tasks("lane-a", [{"title": "a", "prompt": "p"},
                                      {"title": "b", "prompt": "p"}])
    connection = sqlite3.connect(store.path)
    connection.execute("UPDATE queue_tasks SET depends_on = ? WHERE id = ?", (json.dumps(["gone"]), a))
    connection.commit()
    connection.close()
    store.cancel_task(a)
    assert store.dependency_deadlocks() == []


# -- B3: exhausted events vanished instead of dead-lettering -------------

def test_an_event_that_exhausts_its_budget_is_dead_lettered(tmp_path):
    """The bug: MAX_ATTEMPTS was only a claim FILTER. An event that burned
    its budget stayed PENDING forever -- never FAILED, absent from stats(),
    invisible to every operator view. It stopped moving AND stopped being
    reportable, which is the worst combination."""
    bus = EventBus(tmp_path / "events.db")
    published = bus.publish("TASK_CREATED", project_id="p1")

    for _ in range(MAX_ATTEMPTS):
        claimed = bus.claim_next(consumer="c", project_id="p1", lease_seconds=-1)
        assert claimed is not None
    assert bus.claim_next(consumer="c", project_id="p1") is None, "budget should be spent"

    assert bus.get(published["id"])["status"] == FAILED
    assert bus.stats(project_id="p1").get(FAILED) == 1
    letters = bus.dead_letters(project_id="p1")
    assert [row["id"] for row in letters] == [published["id"]]
    assert "delivery attempts" in letters[0]["last_error"]


def test_dead_lettering_is_idempotent_and_leaves_healthy_events_alone(tmp_path):
    bus = EventBus(tmp_path / "events.db")
    doomed = bus.publish("TASK_CREATED", project_id="p1", idempotency_key="doomed")
    healthy = bus.publish("TASK_READY", project_id="p1", idempotency_key="healthy")
    for _ in range(MAX_ATTEMPTS):
        bus.claim_next(consumer="c", project_id="p1", types=["TASK_CREATED"], lease_seconds=-1)

    bus.claim_next(consumer="c", project_id="p1")
    bus.claim_next(consumer="c", project_id="p1")
    assert bus.get(doomed["id"])["status"] == FAILED
    assert bus.get(healthy["id"])["status"] != FAILED
    assert len(bus.dead_letters(project_id="p1")) == 1


def test_an_acked_event_is_never_dead_lettered(tmp_path):
    bus = EventBus(tmp_path / "events.db")
    published = bus.publish("TASK_CREATED", project_id="p1")
    claimed = bus.claim_next(consumer="c", project_id="p1")
    assert bus.ack(claimed["id"], claimed["claim_token"]) is True
    for _ in range(MAX_ATTEMPTS + 2):
        bus.claim_next(consumer="c", project_id="p1")
    assert bus.get(published["id"])["status"] == "ACKED"


# -- B4: events.db was never maintained ----------------------------------

def test_events_db_is_covered_by_maintenance(tmp_path):
    """The bug: events.db was absent from MaintenanceLoop._db_paths, so the
    bus -- a durable append-only log that only grows -- was never
    WAL-checkpointed by any code path."""
    from terminal_mcp.audit import AuditStore
    from terminal_mcp.config import MaintenanceConfig
    from terminal_mcp.lease import PaneLeaseStore
    from terminal_mcp.maintenance import MaintenanceLoop

    events = EventBus(tmp_path / "events.db")
    loop = MaintenanceLoop(audit=AuditStore(tmp_path / "audit.db"), supervisor2_store=None,
                           bindings_path=None, config=MaintenanceConfig(),
                           leases=PaneLeaseStore(tmp_path / "leases.db"), events=events)
    assert events.path in loop._db_paths()


# -- B5: forced lock breaks left no record -------------------------------

def test_force_release_persists_who_broke_whose_lock(tmp_path):
    """The bug: force_release accepted actor+reason, RETURNED them, and
    stored nothing. Breaking another worker's lock is exactly the action
    that must never be possible without a durable record."""
    locks = ResourceLockStore(tmp_path / "leases.db")
    locks.acquire("proj", "branch:main", "worker-A", reason="rebasing")
    locks.force_release("proj", "branch:main", actor="operator", reason="node was rebuilt")

    history = locks.override_history(project_id="proj")
    assert len(history) == 1
    entry = history[0]
    assert entry["actor"] == "operator" and entry["reason"] == "node was rebuilt"
    assert entry["previous_owner"] == "worker-A"
    assert entry["previous_reason"] == "rebasing"
    assert entry["at"]


def test_a_no_op_force_release_records_nothing(tmp_path):
    locks = ResourceLockStore(tmp_path / "leases.db")
    result = locks.force_release("proj", "nothing", actor="op", reason="tidy")
    assert result["released"] is False
    assert locks.override_history() == []


def test_override_history_is_project_scoped(tmp_path):
    locks = ResourceLockStore(tmp_path / "leases.db")
    for project in ("a", "b"):
        locks.acquire(project, "f", "w")
        locks.force_release(project, "f", actor="op", reason="r")
    assert len(locks.override_history(project_id="a")) == 1
    assert len(locks.override_history()) == 2


# -- B13: the stdio tool surface silently lagged the HTTP one ------------

@pytest.mark.anyio
async def test_stdio_and_http_expose_the_same_tool_surface():
    """The bug: build_mcp() left `backlog` and `events` at None, while
    server.py calls build_mcp() bare -- so the stdio server exposed 19 fewer
    tools than the HTTP one, and the contract test, which builds the stdio
    server, could not see them at all."""
    from terminal_mcp.mcp_app import build_mcp

    bare = {tool.name for tool in await build_mcp().list_tools()}
    assert any(name.startswith("terminal_backlog_") for name in bare)
    assert any(name.startswith("terminal_event_") for name in bare)

    narrow = {tool.name for tool in await build_mcp(default_optional_services=False).list_tools()}
    assert narrow < bare, "opting out must genuinely narrow the surface"


@pytest.fixture
def anyio_backend():
    return "asyncio"
