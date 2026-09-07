"""PM/Orchestrator -- real I/O glue (pm_service.py): CapabilityProfile
CRUD, candidate assembly, routing (SUGGEST/AUTO), approval, and the
append-only decision audit trail. Direct QueueService/PMStore tests (no
MCP layer, no real tmux) -- test_pm_mcp_tools.py covers the tool
surface.

SAFETY: every session name below is a disposable fixture string --
never `window`/`window2`/`wtest`."""
from __future__ import annotations

import pytest

from terminal_mcp.pm_service import MODE_AUTO, MODE_SUGGEST, PMService
from terminal_mcp.pm_store import PMStore
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


@pytest.fixture
def pm(tmp_path):
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    pm_store = PMStore(tmp_path / "pm.db")
    return PMService(pm_store, queue)


# -- capability profile CRUD --------------------------------------------------

def test_upsert_and_list_capability(pm):
    result = pm.upsert_capability("local", "worker-a", os="linux", role="developer", skills=[{"name": "docker"}])
    assert "error" not in result
    assert result["capability"]["session"] == "worker-a"
    listed = pm.list_capabilities()["capabilities"]
    assert len(listed) == 1
    assert listed[0]["role"] == "developer"


def test_upsert_capability_rejects_invalid_session_name(pm):
    result = pm.upsert_capability("local", "../not valid")
    assert result["error"] == "INVALID_SESSION_NAME"


def test_delete_capability(pm):
    pm.upsert_capability("local", "worker-a")
    result = pm.delete_capability("local", "worker-a")
    assert result["deleted"] is True
    assert pm.list_capabilities()["capabilities"] == []


# -- eligible_workers ----------------------------------------------------------

def test_eligible_workers_reports_hard_gate_reasons(pm):
    pm.upsert_capability("local", "linux-a", os="linux")
    pm.upsert_capability("windows-node", "win-a", os="windows")
    task_id = pm.queue.create_task("t", "p", session=None, metadata={"required_os": "windows"})["task_id"]
    result = pm.eligible_workers(task_id)
    assert result["eligible"] == ["windows-node/win-a"]
    assert len(result["ineligible"]) == 1
    assert "OS mismatch" in result["ineligible"][0]["reason"]


def test_eligible_workers_unknown_task_reports_error(pm):
    result = pm.eligible_workers("no-such-task")
    assert result["error"] == "TASK_NOT_FOUND"


# -- route_task: SUGGEST mode --------------------------------------------------

def test_route_task_suggest_mode_never_assigns(pm):
    pm.upsert_capability("local", "worker-a", os="linux")
    task_id = pm.queue.create_task("t", "p", session=None)["task_id"]
    result = pm.route_task(task_id, mode=MODE_SUGGEST)
    assert result["decision"]["status"] == "SUGGESTED"
    assert result["decision"]["chosen_session"] == "worker-a"
    # Not actually assigned -- still in the backlog.
    board = pm.queue.board()
    assert board["counts"]["backlog"] == 1
    assert board["counts"]["queued"] == 0


def test_route_task_suggest_mode_persists_decision_for_explain(pm):
    pm.upsert_capability("local", "worker-a")
    task_id = pm.queue.create_task("t", "p", session=None)["task_id"]
    pm.route_task(task_id, mode=MODE_SUGGEST)
    history = pm.explain(task_id)["decisions"]
    assert len(history) == 1
    assert history[0]["status"] == "SUGGESTED"


def test_route_task_no_eligible_worker_stays_unassigned(pm):
    task_id = pm.queue.create_task("t", "p", session=None, metadata={"required_os": "windows"})["task_id"]
    result = pm.route_task(task_id, mode=MODE_SUGGEST)
    assert result["decision"]["status"] == "NO_ELIGIBLE_WORKER"
    board = pm.queue.board()
    assert board["counts"]["backlog"] == 1  # never dropped


def test_route_task_unknown_task_reports_error(pm):
    result = pm.route_task("no-such-task")
    assert result["error"] == "TASK_NOT_FOUND"


def test_route_task_invalid_mode_rejected(pm):
    task_id = pm.queue.create_task("t", "p", session=None)["task_id"]
    result = pm.route_task(task_id, mode="BOGUS")
    assert result["error"] == "INVALID_MODE"


# -- route_task: AUTO mode -----------------------------------------------------

def test_route_task_auto_mode_actually_assigns(pm):
    pm.upsert_capability("local", "worker-a", os="linux")
    task_id = pm.queue.create_task("t", "p", session=None)["task_id"]
    result = pm.route_task(task_id, mode=MODE_AUTO)
    assert result["decision"]["status"] == "ROUTED"
    assert result["assign_result"]["task"]["session"] == "worker-a"
    board = pm.queue.board()
    assert board["counts"]["backlog"] == 0
    assert board["counts"]["queued"] == 1


# -- approve_routing ------------------------------------------------------------

def test_approve_routing_assigns_a_suggested_decision(pm):
    pm.upsert_capability("local", "worker-a")
    task_id = pm.queue.create_task("t", "p", session=None)["task_id"]
    pm.route_task(task_id, mode=MODE_SUGGEST)
    result = pm.approve_routing(task_id)
    assert "error" not in result
    assert result["decision"]["status"] == "APPROVED_AND_ASSIGNED"
    board = pm.queue.board()
    assert board["counts"]["queued"] == 1


def test_approve_routing_refuses_without_a_pending_suggestion(pm):
    task_id = pm.queue.create_task("t", "p", session=None)["task_id"]
    result = pm.approve_routing(task_id)
    assert result["error"] == "NO_SUGGESTED_DECISION"


def test_approve_routing_refuses_a_second_time_after_already_approved(pm):
    pm.upsert_capability("local", "worker-a")
    task_id = pm.queue.create_task("t", "p", session=None)["task_id"]
    pm.route_task(task_id, mode=MODE_SUGGEST)
    pm.approve_routing(task_id)
    second = pm.approve_routing(task_id)
    assert second["error"] == "NO_SUGGESTED_DECISION"
    assert second["latest_status"] == "APPROVED_AND_ASSIGNED"


# -- route_all_unassigned -------------------------------------------------------

def test_route_all_unassigned_routes_every_backlog_task(pm):
    pm.upsert_capability("local", "worker-a")
    pm.queue.create_task("t1", "p1", session=None)
    pm.queue.create_task("t2", "p2", session=None)
    result = pm.route_all_unassigned(mode=MODE_SUGGEST)
    assert result["routed_count"] == 2
    assert all(r["decision"]["status"] == "SUGGESTED" for r in result["results"])


def test_route_all_unassigned_never_touches_already_assigned_tasks(pm):
    pm.upsert_capability("local", "worker-a")
    pm.queue.create_task("assigned", "p", session="worker-b")
    result = pm.route_all_unassigned(mode=MODE_SUGGEST)
    assert result["routed_count"] == 0


# -- permission_checker injection -----------------------------------------------

def test_permission_checker_hard_gates_a_session_without_input(tmp_path):
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    pm_store = PMStore(tmp_path / "pm.db")
    pm = PMService(pm_store, queue, permission_checker=lambda node_id, session: session != "no-input-worker")
    pm.upsert_capability("local", "no-input-worker")
    pm.upsert_capability("local", "ok-worker")
    task_id = pm.queue.create_task("t", "p", session=None)["task_id"]
    result = pm.route_task(task_id, mode=MODE_SUGGEST)
    assert result["decision"]["chosen_session"] == "ok-worker"


def test_node_online_check_via_controller_list_nodes(tmp_path):
    class _FakeNode:
        def __init__(self, node_id, status):
            self.id = node_id
            self.status = status

    class _FakeController:
        def list_nodes(self):
            return [_FakeNode("offline-node", "offline"), _FakeNode("online-node", "online")]

    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    pm_store = PMStore(tmp_path / "pm.db")
    pm = PMService(pm_store, queue, controller=_FakeController())
    pm.upsert_capability("offline-node", "worker-a")
    pm.upsert_capability("online-node", "worker-b")
    task_id = pm.queue.create_task("t", "p", session=None)["task_id"]
    result = pm.route_task(task_id, mode=MODE_SUGGEST)
    assert result["decision"]["chosen_session"] == "worker-b"


def test_wip_limit_hard_gates_a_session_at_capacity(pm):
    pm.upsert_capability("local", "worker-a", max_queued=1)
    pm.upsert_capability("local", "worker-b")  # unbounded
    # Fill worker-a to its own cap.
    pm.queue.create_task("existing", "p", session="worker-a")
    task_id = pm.queue.create_task("t", "p", session=None)["task_id"]
    result = pm.route_task(task_id, mode=MODE_SUGGEST)
    assert result["decision"]["chosen_session"] == "worker-b"
