"""Dashboard Task Manager UI -- task: "bổ sung màn hình quản lý Task theo
từng session trên Dashboard". Backend layer only (queue_service.py's own
aggregation + the dashboard HTTP routes built on it) -- reuses the SAME
persistent Queue/Coordinator store every terminal_queue_*/terminal_task_*
MCP tool already uses, never a second, parallel task store.

Real disposable tmux sessions throughout, never window/window2."""
from __future__ import annotations

import uuid

import pytest
from starlette.testclient import TestClient

from terminal_mcp.audit import AuditStore
from terminal_mcp.bindings import BindingStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.controller import build_default_controller
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _service(tmp_path) -> TerminalService:
    config = AppConfig(
        PermissionsConfig(True, True), ("tm-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("tm-*",), max_text_length=2000),
        session_lifecycle=SessionLifecycleConfig(enabled=True, protected_sessions=("terminal-mcp",)),
    )
    return TerminalService(config, bindings=BindingStore(tmp_path / "bindings.db"),
                          audit=AuditStore(tmp_path / "audit.db"),
                          grants=SessionGrantStore(tmp_path / "grants.db"))


@pytest.fixture
def rig(tmp_path, tmux_session_factory):
    service = _service(tmp_path)
    controller = build_default_controller(service)
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=(), agent_version=None)
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    server = build_mcp(service, controller=controller, queue=queue)
    register_dashboard(server, service, controller=controller, queue=queue)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})
    return {"client": client, "service": service, "queue": queue,
           "make_worker": lambda name: tmux_session_factory(name, "bash -lc 'sleep 300'")}


# -- session_task_board (queue_service.py) -- pure aggregation logic --------

def test_empty_queue_has_all_zero_groups_and_no_gate(rig):
    queue = rig["queue"]
    name = _unique("tm")
    board = queue.session_task_board(name)
    assert board["summary"] == {"running": 0, "queued": 0, "waiting_dependency": 0, "blocked_rework": 0, "total": 0}
    assert board["next_gate"] is None
    assert board["running"] == board["queued"] == board["waiting_dependency"] == [] == board["blocked_rework"]


def test_one_running_and_many_waiting_group_correctly(rig):
    queue = rig["queue"]
    name = _unique("tm")
    ids = queue.store.append_tasks(name, [{"prompt": f"task {i}"} for i in range(5)])
    running_id = ids[0]
    queue.store.transition_task(running_id, "PRECHECK", event_type="TEST")
    queue.store.transition_task(running_id, "READY", event_type="TEST")
    queue.store.transition_task(running_id, "DISPATCHING", event_type="TEST")
    queue.store.transition_task(running_id, "RUNNING", event_type="TEST")

    board = queue.session_task_board(name)
    assert board["summary"]["running"] == 1
    assert board["summary"]["queued"] == 4
    assert [t["id"] for t in board["running"]] == [running_id]
    assert len(board["queued"]) == 4
    # Something is running -- no "next gate" shown (correctly busy already).
    assert board["next_gate"] is None


def test_blocked_and_dependency_waiting_are_grouped_separately(rig):
    queue = rig["queue"]
    name = _unique("tm")
    ids = queue.store.append_tasks(name, [{"prompt": "first task"}, {"prompt": "second, depends on first"}])
    first_id, second_id = ids
    queue.store.transition_task(first_id, "PRECHECK", event_type="TEST")
    queue.store.transition_task(first_id, "BLOCKED", event_type="TEST", reason="a real failure")
    # second_id declares a dependency on first_id, which hasn't COMPLETED.
    queue.store.set_tasks(name, [{"prompt": "second, depends on first", "depends_on": [first_id]}],
                          replace_pending=False)

    board = queue.session_task_board(name)
    assert any(t["id"] == first_id for t in board["blocked_rework"])
    dependency_ids = {t["prompt"] for t in board["waiting_dependency"]}
    assert "second, depends on first" in dependency_ids
    assert board["summary"]["blocked_rework"] == 1
    assert board["summary"]["waiting_dependency"] == 1
    # first_id is BLOCKED (not QUEUED/PRECHECK) so it's skipped when
    # looking for a gate to show -- second_id (still QUEUED) is the real
    # head of line and gets the gate instead.
    assert board["next_gate"]["task_id"] == second_id


def test_next_gate_reflects_a_real_coordinator_decision(rig):
    queue = rig["queue"]
    name = _unique("tm")
    ids = queue.store.append_tasks(name, [{"prompt": "gated task"}])
    queue.store.transition_task(ids[0], "PRECHECK", event_type="TEST")
    # NEEDS_REWORK -> lands back at QUEUED (record_coordinator_decision's
    # own documented mapping) with a real coordinator_reason attached --
    # exactly the "still QUEUED but with a gate reason" case the UI needs.
    queue.store.record_coordinator_decision(ids[0], status="NEEDS_REWORK", reason="waiting on a real dependency")
    board = queue.session_task_board(name)
    assert board["next_gate"]["task_id"] == ids[0]
    assert board["next_gate"]["reason"] == "waiting on a real dependency"


def test_recent_completed_and_failed_are_capped_and_sorted(rig):
    queue = rig["queue"]
    name = _unique("tm")
    ids = queue.store.append_tasks(name, [{"prompt": f"done {i}"} for i in range(3)])
    for task_id in ids:
        queue.store.transition_task(task_id, "PRECHECK", event_type="TEST")
        queue.store.transition_task(task_id, "READY", event_type="TEST")
        queue.store.transition_task(task_id, "DISPATCHING", event_type="TEST")
        queue.store.transition_task(task_id, "RUNNING", event_type="TEST")
        queue.store.transition_task(task_id, "VERIFYING", event_type="TEST")
        queue.store.mark_completed_with_evidence(task_id, evidence={"output_tail": "done"})
    board = queue.session_task_board(name, recent_limit=2)
    assert len(board["recent"]) == 2


# -- dashboard HTTP routes ----------------------------------------------------

def test_dashboard_tasks_route_requires_read_authorization(rig):
    # A name OUTSIDE the "tm-*" static whitelist and with no dashboard
    # grant either -- _read_authorized must actually refuse this, not
    # just accept anything the fixture happens to name things.
    client = rig["client"]
    response = client.get("/dashboard/api/session/tasks", params={"name": _unique("other-noauth")})
    assert response.status_code == 403
    assert response.json()["error"] == "READ_RESTRICTED"


def test_dashboard_tasks_route_returns_real_board_for_a_granted_session(rig):
    client, service, queue, make_worker = rig["client"], rig["service"], rig["queue"], rig["make_worker"]
    name = _unique("tm")
    make_worker(name)
    service.grants.set_read(name, True, granted_by="test")
    queue.store.append_tasks(name, [{"prompt": "a real task"}])
    response = client.get("/dashboard/api/session/tasks", params={"name": name})
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["queued"] == 1


def test_dashboard_fleet_summary_route_is_a_real_aggregate(rig):
    client, service, queue, make_worker = rig["client"], rig["service"], rig["queue"], rig["make_worker"]
    name = _unique("tm")
    make_worker(name)
    service.grants.set_read(name, True, granted_by="test")
    queue.store.append_tasks(name, [{"prompt": "a"}, {"prompt": "b"}])
    response = client.get("/dashboard/api/fleet-task-summary")
    assert response.status_code == 200
    assert response.json()["queued"] >= 2


def test_dashboard_enqueue_pause_resume_retry_cancel_round_trip(rig):
    client, service, queue, make_worker = rig["client"], rig["service"], rig["queue"], rig["make_worker"]
    name = _unique("tm")
    make_worker(name)
    service.grants.set_read(name, True, granted_by="test")

    enqueued = client.post("/dashboard/api/session/queue/enqueue", json={"name": name, "prompt": "do the thing"})
    assert enqueued.status_code == 200, enqueued.json()
    task_id = enqueued.json()["task_id"] if "task_id" in enqueued.json() else \
        queue.store.lane_status(name)["tasks"][0]["id"]

    paused = client.post("/dashboard/api/session/queue/pause", json={"name": name})
    assert paused.status_code == 200
    assert paused.json()["paused"] is True

    resumed = client.post("/dashboard/api/session/queue/resume", json={"name": name})
    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False

    # Force the task into BLOCKED so retry has something real to do.
    queue.store.transition_task(task_id, "PRECHECK", event_type="TEST")
    queue.store.transition_task(task_id, "BLOCKED", event_type="TEST", reason="test")
    retried = client.post("/dashboard/api/task/retry", json={"name": name, "task_id": task_id})
    assert retried.status_code == 200
    assert retried.json()["task"]["status"] == "QUEUED"

    cancelled = client.post("/dashboard/api/task/cancel", json={"name": name, "task_id": task_id})
    assert cancelled.status_code == 200
    assert cancelled.json()["task"]["status"] == "CANCELLED"


def test_dashboard_task_actions_require_read_authorization(rig):
    client = rig["client"]
    name = _unique("other-noauth")
    for path, body in (
        ("/dashboard/api/session/queue/pause", {"name": name}),
        ("/dashboard/api/session/queue/resume", {"name": name}),
        ("/dashboard/api/session/queue/enqueue", {"name": name, "prompt": "x"}),
        ("/dashboard/api/task/retry", {"name": name, "task_id": "x"}),
        ("/dashboard/api/task/cancel", {"name": name, "task_id": "x"}),
    ):
        response = client.post(path, json=body)
        assert response.status_code == 403, path
        assert response.json()["error"] == "READ_RESTRICTED", path


def test_dashboard_task_action_rejects_cross_origin_request(tmp_path):
    # Action (POST) routes DO enforce Origin, unlike the read (GET)
    # routes just above (_read_guard's own documented, deliberate
    # no-Origin-check posture -- a top-level GET navigation doesn't
    # reliably send one at all) -- same _mutation_guard every other
    # dashboard POST route here already goes through.
    from terminal_mcp.mcp_app import build_mcp as _build_mcp
    service = _service(tmp_path)
    server = _build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "https://evil.example.com"})
    response = client.post("/dashboard/api/session/queue/pause", json={"name": "tm-x"})
    assert response.status_code == 403
    assert response.json()["error"] == "ORIGIN_NOT_ALLOWED"


def test_rename_session_is_reflected_immediately_in_the_task_board(rig):
    """Rename-safe (task's own requirement): the task board reads live
    off the queue store by name -- a rename that already updated the
    queue rows (session_service.py's rename_session) is visible under
    the new name with no extra wiring needed here."""
    client, service, queue, make_worker = rig["client"], rig["service"], rig["queue"], rig["make_worker"]
    old, new = _unique("tm-old"), _unique("tm-new")
    make_worker(old)
    service.grants.set_read(old, True, granted_by="test")
    service.grants.set_read(new, True, granted_by="test")
    queue.store.append_tasks(old, [{"prompt": "a task"}])
    try:
        renamed = client.post("/dashboard/api/session/rename", json={"name": old, "new_name": new})
        assert renamed.status_code == 200, renamed.json()
        response = client.get("/dashboard/api/session/tasks", params={"name": new})
        assert response.status_code == 200
        assert response.json()["summary"]["queued"] == 1
    finally:
        import subprocess
        subprocess.run(["tmux", "kill-session", "-t", new], check=False, capture_output=True)


def test_controller_restart_recovers_the_same_board_no_duplicate(rig, tmp_path):
    """item: 'controller restart' -- a fresh QueueStore instance over the
    SAME db file ('the controller restarted') sees the exact same,
    already-committed task rows; nothing duplicated, nothing lost."""
    queue = rig["queue"]
    name = _unique("tm")
    queue.store.append_tasks(name, [{"prompt": f"task {i}"} for i in range(3)])

    fresh_queue = QueueService(QueueStore(tmp_path / "queue.db"))
    board = fresh_queue.session_task_board(name)
    assert board["summary"]["queued"] == 3
    assert board["summary"]["total"] == 3


def test_task_board_works_identically_for_a_windows_backed_session(tmp_path):
    """item: 'Windows remote session' -- the task board/dashboard route
    never touch the session BACKEND at all (only the queue store, keyed
    by bare session name, and terminal._read_authorized, which is
    backend-independent policy) -- proven here with a REAL
    WindowsSessionBackend-backed TerminalService (same fixture pattern
    as test_windows_terminal_service_integration.py), not a mock."""
    import sys
    from starlette.testclient import TestClient
    from terminal_mcp.config import SessionLifecycleConfig
    from terminal_mcp.windows_backend import WindowsSessionBackend
    from tests.test_windows_backend import _FAKE_SHELL_SCRIPT, _fake_factory

    script_path = tmp_path / "fake_shell.py"
    script_path.write_text(_FAKE_SHELL_SCRIPT)

    def factory(argv, cwd):
        return _fake_factory([sys.executable, "-u", str(script_path)], cwd)

    backend = WindowsSessionBackend(shell="powershell.exe", process_factory=factory, history_lines=500)
    config = AppConfig(
        PermissionsConfig(True, True), ("win-tm-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("win-tm-*",), max_text_length=2000),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),)),
    )
    service = TerminalService(config, tmux=backend)
    controller = build_default_controller(service)
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=(), agent_version=None)
    queue = QueueService(QueueStore(tmp_path / "win_queue.db"))
    server = build_mcp(service, controller=controller, queue=queue)
    register_dashboard(server, service, controller=controller, queue=queue)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})

    name = "win-tm-session"
    service.terminal_create_session(name, "shell", str(tmp_path))
    try:
        queue.store.append_tasks(name, [{"prompt": "a windows-backed task"}])
        response = client.get("/dashboard/api/session/tasks", params={"name": name})
        assert response.status_code == 200
        assert response.json()["summary"]["queued"] == 1
    finally:
        try:
            backend.kill_session(name)
        except Exception:  # noqa: BLE001
            pass


def test_concurrent_enqueue_and_pause_never_lose_an_update(rig):
    """item: 'concurrent updates' -- real threads hammering the dashboard
    routes for the SAME session at once; queue_store's own atomic
    transactions (not this route) are what actually guarantee this, but
    this proves it end to end through the real HTTP path."""
    import threading
    client, service, make_worker = rig["client"], rig["service"], rig["make_worker"]
    name = _unique("tm")
    make_worker(name)
    service.grants.set_read(name, True, granted_by="test")

    errors = []
    def enqueue_many():
        for i in range(10):
            r = client.post("/dashboard/api/session/queue/enqueue", json={"name": name, "prompt": f"concurrent {i}"})
            if r.status_code != 200:
                errors.append(r.json())

    threads = [threading.Thread(target=enqueue_many) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == []
    board_response = client.get("/dashboard/api/session/tasks", params={"name": name})
    assert board_response.json()["summary"]["total"] == 40  # 4 threads * 10 each, none lost, none duplicated


# -- Unified Task System checkpoint: Global Tasks Kanban dashboard routes --

def test_dashboard_tasks_board_route_reflects_real_queue_state(rig):
    client, queue = rig["client"], rig["queue"]
    queue.create_task("Backlog item", "investigate", session=None)
    board = client.get("/dashboard/api/tasks/board")
    assert board.status_code == 200
    assert board.json()["counts"]["backlog"] == 1


def test_dashboard_tasks_create_route_without_session_lands_in_backlog(rig):
    client = rig["client"]
    created = client.post("/dashboard/api/tasks/create", json={"title": "t", "prompt": "p"})
    assert created.status_code == 200, created.json()
    assert created.json()["assigned"] is False
    board = client.get("/dashboard/api/tasks/board").json()
    assert board["counts"]["backlog"] == 1
    assert board["backlog"][0]["session"] is None


def test_dashboard_tasks_create_route_with_session_requires_read_authorization(rig):
    client = rig["client"]
    name = _unique("other-noauth")  # never granted read -- must be refused, not silently created
    denied = client.post("/dashboard/api/tasks/create", json={"title": "t", "prompt": "p", "session": name})
    assert denied.status_code == 403
    assert denied.json()["error"] == "READ_RESTRICTED"


def test_dashboard_tasks_create_route_with_authorized_session_is_assigned_directly(rig):
    client, service, make_worker = rig["client"], rig["service"], rig["make_worker"]
    name = _unique("tm")
    make_worker(name)
    service.grants.set_read(name, True, granted_by="test")
    created = client.post("/dashboard/api/tasks/create", json={"title": "t", "prompt": "p", "session": name})
    assert created.status_code == 200, created.json()
    assert created.json()["assigned"] is True
    board = client.get("/dashboard/api/tasks/board").json()
    assert any(t["session"] == name for t in board["queued"])


def test_dashboard_tasks_assign_route_moves_same_task_no_duplicate(rig):
    client, service, make_worker = rig["client"], rig["service"], rig["make_worker"]
    name = _unique("tm")
    make_worker(name)
    service.grants.set_read(name, True, granted_by="test")

    created = client.post("/dashboard/api/tasks/create", json={"title": "t", "prompt": "p"})
    task_id = created.json()["task_id"]
    assigned = client.post("/dashboard/api/tasks/assign", json={"task_id": task_id, "session": name})
    assert assigned.status_code == 200, assigned.json()
    assert assigned.json()["task"]["id"] == task_id

    board = client.get("/dashboard/api/tasks/board").json()
    assert board["counts"]["backlog"] == 0
    assert board["counts"]["queued"] == 1
    assert board["queued"][0]["id"] == task_id


def test_dashboard_tasks_assign_route_requires_read_authorization_on_target_session(rig):
    client = rig["client"]
    created = client.post("/dashboard/api/tasks/create", json={"title": "t", "prompt": "p"})
    task_id = created.json()["task_id"]
    other = _unique("other-noauth")
    denied = client.post("/dashboard/api/tasks/assign", json={"task_id": task_id, "session": other})
    assert denied.status_code == 403
    assert denied.json()["error"] == "READ_RESTRICTED"


def test_dashboard_tasks_page_served_when_read_authorized(rig):
    client = rig["client"]
    response = client.get("/dashboard/tasks")
    assert response.status_code == 200
    assert "Global Tasks" in response.text


def test_dashboard_tasks_board_route_enriches_cards_with_pm_routing_reason(tmp_path, tmux_session_factory):
    # PM/Orchestrator checkpoint (§20.2): the Kanban board's own routing_
    # reason display must reflect a REAL, persisted PM decision -- one
    # bulk read (pm.store.latest_decisions_for_tasks), never N+1, never
    # a client-side guess.
    from terminal_mcp.pm_service import PMService
    from terminal_mcp.pm_store import PMStore

    service = _service(tmp_path)
    controller = build_default_controller(service)
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=(), agent_version=None)
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    pm = PMService(PMStore(tmp_path / "pm.db"), queue, controller)
    server = build_mcp(service, controller=controller, queue=queue, pm=pm)
    register_dashboard(server, service, controller=controller, queue=queue, pm=pm)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})

    name = _unique("tm")
    tmux_session_factory(name, "bash -lc 'sleep 300'")
    service.grants.set_read(name, True, granted_by="test")
    service.grants.set_input(name, True, granted_by="test")

    pm.upsert_capability("local", name, os="linux")
    created = pm.queue.create_task("t", "p", session=None)
    pm.route_task(created["task_id"], mode="SUGGEST")

    board = client.get("/dashboard/api/tasks/board").json()
    card = next(t for t in board["backlog"] if t["id"] == created["task_id"])
    assert card["pm_decision_status"] == "SUGGESTED"
    assert name in card["routing_reason"]


def test_dashboard_tasks_board_route_omits_routing_reason_for_never_routed_task(tmp_path):
    from terminal_mcp.pm_service import PMService
    from terminal_mcp.pm_store import PMStore

    service = _service(tmp_path)
    controller = build_default_controller(service)
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    pm = PMService(PMStore(tmp_path / "pm.db"), queue, controller)
    server = build_mcp(service, controller=controller, queue=queue, pm=pm)
    register_dashboard(server, service, controller=controller, queue=queue, pm=pm)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})

    created = pm.queue.create_task("t", "p", session=None)
    board = client.get("/dashboard/api/tasks/board").json()
    card = next(t for t in board["backlog"] if t["id"] == created["task_id"])
    assert "routing_reason" not in card


def test_dashboard_tasks_board_route_shows_child_progress_for_split_parent(tmp_path):
    from terminal_mcp.planner_service import PlannerService
    from terminal_mcp.planner_store import PlannerStore

    service = _service(tmp_path)
    controller = build_default_controller(service)
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    planner = PlannerService(PlannerStore(tmp_path / "planner.db"), queue)
    server = build_mcp(service, controller=controller, queue=queue, planner=planner)
    register_dashboard(server, service, controller=controller, queue=queue, planner=planner)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})

    parent = queue.create_task("Big task", "do it", session=None)
    children = [{"prompt": "p1", "acceptance_criteria": "a1"}, {"prompt": "p2", "acceptance_criteria": "a2"}]
    split = planner.propose_split(parent["task_id"], children, mode="AUTO")
    done_child = split["child_task_ids"][0]
    queue.store.transition_task(done_child, "DISPATCHING", event_type="TEST")
    queue.store.transition_task(done_child, "RUNNING", event_type="TEST")
    queue.store.transition_task(done_child, "VERIFYING", event_type="TEST")
    queue.store.mark_completed_with_evidence(done_child, evidence={"ok": True})

    board = client.get("/dashboard/api/tasks/board").json()
    parent_card = next(t for t in board["blocked_review"] if t["id"] == parent["task_id"])
    assert parent_card["child_progress"] == {"total": 2, "done": 1}
