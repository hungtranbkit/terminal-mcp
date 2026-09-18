from __future__ import annotations

import json

from terminal_mcp.project_task_feeder import ProjectFeedConfig, ProjectTaskFeeder
from terminal_mcp.queue_engine import QueueEngine
from terminal_mcp.queue_loop import QueueLoop
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore

from tests.test_queue_engine import FakeOps, _always_ready_gate


def _write_registry(path, tasks):
    path.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")


def _feed(tmp_path, store, *, preferred=()):
    registry = tmp_path / "TASKS.json"
    queue = QueueService(store)
    cfg = ProjectFeedConfig(
        project_id="novaretail",
        lane="linux-codex-work",
        owner="linux-codex-work",
        registry_path=str(registry),
        preferred_task_ids=tuple(preferred),
    )
    return registry, queue, ProjectTaskFeeder(queue, [cfg])


def test_feeder_prefers_configured_p0_task(tmp_path):
    store = QueueStore(tmp_path / "queue.db")
    registry, queue, feeder = _feed(
        tmp_path, store, preferred=("NWR-UI-V3-POLISH-001",)
    )
    _write_registry(registry, [
        {"id": "OTHER-P0", "title": "other", "status": "READY",
         "priority": "P0", "dependencies": [], "acceptance": ["x"]},
        {"id": "NWR-UI-V3-POLISH-001", "title": "polish", "status": "READY",
         "priority": "P0", "dependencies": [], "acceptance": ["visual polish"]},
    ])

    result = feeder.feed_if_idle("linux-codex-work")

    assert result["action"] == "ENQUEUED"
    assert result["task_id"] == "NWR-UI-V3-POLISH-001"
    row = store.get_task(result["queue_task_id"])
    assert row.metadata["canonical_task_id"] == "NWR-UI-V3-POLISH-001"
    assert row.request_key == "project-feed:novaretail:NWR-UI-V3-POLISH-001"


def test_feeder_rechecks_dependencies_fail_closed(tmp_path):
    store = QueueStore(tmp_path / "queue.db")
    registry, queue, feeder = _feed(tmp_path, store)
    _write_registry(registry, [
        {"id": "DEP", "title": "dep", "status": "READY",
         "priority": "P0", "dependencies": []},
        {"id": "NEXT", "title": "next", "status": "READY",
         "priority": "P0", "dependencies": ["DEP"]},
    ])

    first = feeder.feed_if_idle("linux-codex-work")
    assert first["task_id"] == "DEP"

    # NEXT must not be selected merely because its own status says READY.
    task = store.get_task(first["queue_task_id"])
    store.transition_task(task.id, "PRECHECK", event_type="TEST")
    store.transition_task(task.id, "READY", event_type="TEST")
    store.transition_task(task.id, "DISPATCHING", event_type="TEST")
    store.transition_task(task.id, "RUNNING", event_type="TEST")
    store.transition_task(task.id, "VERIFYING", event_type="TEST")
    store.mark_completed_with_evidence(task.id, evidence={"test": True})

    second = feeder.feed_if_idle("linux-codex-work")
    assert second["action"] == "REGISTRY_STALE"
    assert "NEXT" not in second.get("skipped_terminal", [])


def test_terminal_dedup_does_not_reenqueue_stale_registry_task(tmp_path):
    store = QueueStore(tmp_path / "queue.db")
    registry, queue, feeder = _feed(tmp_path, store)
    _write_registry(registry, [
        {"id": "ONE", "title": "one", "status": "READY",
         "priority": "P0", "dependencies": []},
        {"id": "TWO", "title": "two", "status": "READY",
         "priority": "P1", "dependencies": []},
    ])

    one = feeder.feed_if_idle("linux-codex-work")
    row = store.get_task(one["queue_task_id"])
    store.transition_task(row.id, "PRECHECK", event_type="TEST")
    store.transition_task(row.id, "READY", event_type="TEST")
    store.transition_task(row.id, "DISPATCHING", event_type="TEST")
    store.transition_task(row.id, "RUNNING", event_type="TEST")
    store.transition_task(row.id, "VERIFYING", event_type="TEST")
    store.mark_completed_with_evidence(row.id, evidence={"test": True})

    result = feeder.feed_if_idle("linux-codex-work")
    assert result["action"] == "ENQUEUED"
    assert result["task_id"] == "TWO"


def test_queue_loop_persists_and_claims_project_task_in_same_cycle(tmp_path):
    store = QueueStore(tmp_path / "queue.db")
    registry, queue, feeder = _feed(
        tmp_path, store, preferred=("NWR-UI-V3-POLISH-001",)
    )
    _write_registry(registry, [
        {"id": "NWR-UI-V3-POLISH-001", "title": "Polish V3", "status": "READY",
         "priority": "P0", "dependencies": [], "acceptance": ["finish visual polish"]},
    ])
    store.set_auto_dispatch("linux-codex-work", True)

    ops = FakeOps()
    ops.set_status("linux-codex-work", {
        "state": "IDLE", "node_id": "local", "cwd": "/repo/novaretail"
    })
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())
    loop = QueueLoop(engine, poll_interval_seconds=60, project_feeder=feeder)

    results = loop.run_one_cycle()

    assert results[0]["action"] == "IDLE"
    assert results[1]["action"] == "CLAIMED"
    assert results[1]["project_feed"]["task_id"] == "NWR-UI-V3-POLISH-001"
    lane = store.lane_status("linux-codex-work")
    assert lane["current_task"]["metadata"]["canonical_task_id"] == "NWR-UI-V3-POLISH-001"
