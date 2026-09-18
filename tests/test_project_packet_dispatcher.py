from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from terminal_mcp.project_task_feeder import ProjectFeedConfig, ProjectTaskFeeder
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


def _registry(tmp_path, tasks):
    path = tmp_path / "TASKS.json"
    path.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    return path


def _feeder(tmp_path, tasks, *, lane="linux-codex-work", owner=None, second=None):
    store = QueueStore(tmp_path / "queue.db")
    queue = QueueService(store)
    path = _registry(tmp_path, tasks)
    configs = [ProjectFeedConfig("p", lane, str(path), owner=owner or lane)]
    if second:
        configs.append(ProjectFeedConfig("p", second, str(path), owner=second))
    return store, queue, ProjectTaskFeeder(queue, configs), path


def _done(store, task_id):
    row = store.get_task(task_id)
    for state in ("PRECHECK", "READY", "DISPATCHING", "RUNNING", "VERIFYING"):
        store.transition_task(row.id, state, event_type="TEST")
    store.mark_completed_with_evidence(row.id, evidence={"test": True})


def test_idle_worker_gets_next_packet_and_restart_cycle_is_idempotent(tmp_path):
    tasks = [{"id": "A", "title": "A", "status": "READY", "dependencies": [],
              "estimated_minutes": 45, "scope": ["src/a.py"]}]
    store, _, feeder, _ = _feeder(tmp_path, tasks)
    first = feeder.feed_if_idle("linux-codex-work")
    assert first["action"] == "ENQUEUED" and first["packet_id"]
    assert feeder.feed_if_idle("linux-codex-work")["action"] == "PACKET_ACTIVE"
    _done(store, first["queue_task_id"])
    assert feeder.feed_if_idle("linux-codex-work")["action"] == "REGISTRY_STALE"


def test_new_feeder_process_resumes_durable_packet_without_duplicate(tmp_path):
    tasks = [{"id": "A", "title": "A", "status": "READY", "dependencies": [],
              "estimated_minutes": 45, "scope": ["src/a.py"]}]
    store, queue, feeder, path = _feeder(tmp_path, tasks)
    first = feeder.feed_if_idle("linux-codex-work")
    # A fresh process has no in-memory feeder state, only the same queue DB.
    resumed = ProjectTaskFeeder(queue, [ProjectFeedConfig("p", "linux-codex-work", str(path), owner="linux-codex-work")])
    result = resumed.feed_if_idle("linux-codex-work")
    assert result["action"] == "PACKET_ACTIVE"
    assert result["packet_id"] == first["packet_id"]


def test_duplicate_cycle_does_not_create_second_packet_or_queue_row(tmp_path):
    tasks = [{"id": "A", "title": "A", "status": "READY", "dependencies": [], "scope": ["a.py"]}]
    store, queue, feeder, _ = _feeder(tmp_path, tasks)
    first = feeder.feed_if_idle("linux-codex-work")
    again = feeder.feed_if_idle("linux-codex-work")
    assert again["action"] == "PACKET_ACTIVE"
    assert len(store.tasks_with_statuses(["QUEUED", "RUNNING"])) == 1
    assert store.active_project_packet("linux-codex-work")["packet_id"] == first["packet_id"]


def test_small_tasks_bundle_only_when_scoped_and_sized(tmp_path):
    tasks = [{"id": f"T{i}", "title": f"T{i}", "status": "READY", "dependencies": [],
              "estimated_minutes": 15, "scope": [f"src/{i}.py"]} for i in range(1, 5)]
    _, _, feeder, _ = _feeder(tmp_path, tasks)
    result = feeder.feed_if_idle("linux-codex-work")
    assert result["action"] == "ENQUEUED"
    assert 2 <= len(result["task_ids"]) <= 5
    assert 45 <= result["packet_minutes"] <= 90


def test_dependency_chain_continuation_is_allowed_inside_packet(tmp_path):
    tasks = [
        {"id": "A", "title": "A", "status": "READY", "dependencies": [],
         "estimated_minutes": 30, "scope": ["src/a.py"]},
        {"id": "B", "title": "B", "status": "READY", "dependencies": ["A"],
         "estimated_minutes": 30, "scope": ["src/b.py"]},
    ]
    _, _, feeder, _ = _feeder(tmp_path, tasks)
    result = feeder.feed_if_idle("linux-codex-work")
    assert result["task_ids"] == ["A", "B"]


def test_active_foreign_owner_is_reassign_required_not_stolen(tmp_path):
    tasks = [{"id": "FOREIGN", "title": "foreign", "status": "READY", "owner": "other-work",
              "dependencies": []}]
    _, _, feeder, _ = _feeder(tmp_path, tasks)
    result = feeder.feed_if_idle("linux-codex-work")
    assert result["action"] == "REASSIGN_REQUIRED"
    assert result["task_ids"] == ["FOREIGN"]


def test_overlapping_scopes_are_not_bundled(tmp_path):
    tasks = [{"id": "A", "title": "A", "status": "READY", "dependencies": [],
              "estimated_minutes": 30, "scope": ["src/shared"]},
             {"id": "B", "title": "B", "status": "READY", "dependencies": [],
              "estimated_minutes": 30, "scope": ["src/shared/file.py"]}]
    _, _, feeder, _ = _feeder(tmp_path, tasks)
    result = feeder.feed_if_idle("linux-codex-work")
    assert result["task_ids"] == ["A"]


def test_no_executable_task_has_explicit_reason(tmp_path):
    tasks = [{"id": "WAIT", "title": "wait", "status": "READY", "dependencies": ["MISSING"]}]
    _, _, feeder, _ = _feeder(tmp_path, tasks)
    result = feeder.feed_if_idle("linux-codex-work")
    assert result["action"] == "NO_EXECUTABLE_TASK"
    assert result["why_not_dispatched"]


def test_parallel_workers_get_disjoint_packets(tmp_path):
    tasks = [{"id": "A", "title": "A", "status": "READY", "dependencies": [], "scope": ["a.py"]},
             {"id": "B", "title": "B", "status": "READY", "dependencies": [], "scope": ["b.py"]}]
    store, queue, feeder, path = _feeder(tmp_path, tasks, second="other-work")
    store.set_auto_dispatch("other-work", True)
    a = feeder.feed_if_idle("linux-codex-work")
    b = feeder.feed_if_idle("other-work")
    assert a["action"] == b["action"] == "ENQUEUED"
    assert a["packet_id"] != b["packet_id"]


def test_stale_worker_lease_is_recovered(tmp_path):
    tasks = [{"id": "A", "title": "A", "status": "READY", "dependencies": [], "estimated_minutes": 45}]
    store, _, feeder, _ = _feeder(tmp_path, tasks)
    first = feeder.feed_if_idle("linux-codex-work")
    store.update_project_packet(first["packet_id"], lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    second = feeder.feed_if_idle("linux-codex-work")
    assert second["action"] in {"EXISTING_TASK", "ENQUEUED"}


def test_explicit_codex_names_and_remote_affinity_are_supported(tmp_path):
    path = _registry(tmp_path, [])
    cfg = ProjectFeedConfig("p", "codex1", str(path), target_session="codex1",
                            target_node_id="dell-5530", allowed_agent_types=("codex",))
    _, _, feeder, _ = _feeder(tmp_path, [])
    feeder = ProjectTaskFeeder(feeder.queue, [cfg])
    rows = feeder.fleet_status([{"session": "codex1", "node_id": "dell-5530",
                                 "agent_type": "codex", "state": "IDLE",
                                 "input_allowed": True, "background_work": False}])
    assert rows[0]["classification"] == "IDLE_ELIGIBLE"


def test_non_codex_usage_limited_and_unknown_workers_fail_closed(tmp_path):
    path = _registry(tmp_path, [])
    cfg = ProjectFeedConfig("p", "wcodex-work", str(path), target_session="wcodex-work",
                            allowed_agent_types=("codex",))
    _, _, feeder, _ = _feeder(tmp_path, [])
    feeder = ProjectTaskFeeder(feeder.queue, [cfg])
    assert feeder.fleet_status([{"session": "wcodex-work", "agent_type": "shell", "state": "IDLE", "input_allowed": True}])[0]["classification"] == "BLOCKED_AGENT_TYPE"
    assert feeder.fleet_status([{"session": "wcodex-work", "agent_type": "codex", "state": "UNKNOWN", "usage_limited": True}])[0]["classification"] == "NO_COMPATIBLE_TASK"
    assert feeder.fleet_status([{"session": "wcodex-work", "agent_type": "codex", "state": "UNKNOWN", "background_work": True}])[0]["classification"] == "BLOCKED_UNKNOWN_ACTIVITY"


def test_cross_node_affinity_and_offline_node_are_visible(tmp_path):
    path = _registry(tmp_path, [])
    cfg = ProjectFeedConfig("p", "wcodex-work2", str(path), target_session="wcodex-work2",
                            target_node_id="dell-5530")
    _, _, feeder, _ = _feeder(tmp_path, [])
    feeder = ProjectTaskFeeder(feeder.queue, [cfg])
    assert feeder.fleet_status([{"session": "wcodex-work2", "node_id": "local", "state": "IDLE", "input_allowed": True}])[0]["classification"] == "NO_COMPATIBLE_TASK"
    assert feeder.fleet_status([], unreachable_nodes=[{"node_id": "dell-5530", "status": "offline"}])[0]["reason"] == "node_unavailable"


def test_missing_duration_is_inferred_and_forms_target_packet(tmp_path):
    tasks = [{"id": f"I{i}", "title": f"I{i}", "status": "READY", "dependencies": [],
              "acceptance": ["a", "b"], "scope": [f"src/{i}.py", f"tests/{i}.py"]}
             for i in range(1, 5)]
    store = QueueStore(tmp_path / "queue.db")
    queue = QueueService(store)
    path = _registry(tmp_path, tasks)
    cfg = ProjectFeedConfig("p", "codex1", str(path), target_session="codex1", infer_task_size=True)
    result = ProjectTaskFeeder(queue, [cfg]).feed_if_idle("codex1")
    assert 2 <= len(result["task_ids"]) <= 5
    assert 45 <= result["packet_minutes"] <= 90
    assert all(source in {"inferred", "explicit"} for _, (source, _) in result["estimations"].items())


def test_authoritative_remote_inventory_discovers_two_windows_sessions(tmp_path):
    path = _registry(tmp_path, [])
    store = QueueStore(tmp_path / "queue.db")
    queue = QueueService(store)
    feeds = [ProjectFeedConfig("p", name, str(path), target_session=name,
                               target_node_id="dell-5530", allowed_agent_types=("codex",))
             for name in ("wcodex-work", "wcodex-work2")]
    feeder = ProjectTaskFeeder(queue, feeds, inventory_provider=lambda: {
        "sessions": [
            {"name": "wcodex-work", "node_id": "dell-5530", "agent_type": "codex",
             "state": "IDLE", "effective_input": True, "effective_read": True, "background_work": False},
            {"name": "wcodex-work2", "node_id": "dell-5530", "agent_type": "codex",
             "state": "WAITING_INPUT", "effective_input": True, "effective_read": True},
        ], "unreachable_nodes": []})
    rows = feeder.status()["fleet"]
    assert {row["session"] for row in rows} == {"wcodex-work", "wcodex-work2"}
    assert rows[0]["classification"] == "IDLE_ELIGIBLE"
    assert rows[1]["classification"] == "ACTIVE"


def test_authoritative_inventory_marks_remote_node_offline_and_stale_pin(tmp_path):
    path = _registry(tmp_path, [])
    store = QueueStore(tmp_path / "queue.db")
    queue = QueueService(store)
    feeder = ProjectTaskFeeder(queue, [
        ProjectFeedConfig("p", "wcodex-work", str(path), target_session="wcodex-work",
                          target_node_id="dell-5530", allowed_agent_types=("codex",)),
        ProjectFeedConfig("p", "wcodex-work2", str(path), target_session="wcodex-work2",
                          target_node_id="dell-5530", allowed_agent_types=("codex",)),
    ], inventory_provider=lambda: {
        "sessions": [{"name": "wcodex-work", "node_id": "other-node", "agent_type": "codex",
                       "state": "IDLE", "effective_input": True}],
        "unreachable_nodes": [{"node_id": "dell-5530", "status": "offline"}],
    })
    rows = {row["session"]: row for row in feeder.status()["fleet"]}
    assert rows["wcodex-work"]["reason"] == "node_unavailable"
    assert rows["wcodex-work2"]["reason"] == "node_unavailable"
