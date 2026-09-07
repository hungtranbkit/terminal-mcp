"""dashboard.py's Move-Task UI backend (/dashboard/api/tasks/reassign)
and the Requirements/Feature Matrix doc link (/dashboard/requirements).
Real Starlette TestClient (in-process ASGI), real QueueService/
QueueStore over a tmp_path db -- no mocking of the actual reassign
mechanics (already thoroughly tested at the queue_service.py layer;
this file's own job is the dashboard route's own wiring/authorization)."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


def _config() -> AppConfig:
    return AppConfig(
        PermissionsConfig(True, True), ("test-*", "agent-*"), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
    )


@pytest.fixture
def client_and_queue(tmp_path):
    from terminal_mcp.grants import SessionGrantStore
    service = TerminalService(_config(), grants=SessionGrantStore(tmp_path / "grants.db"))
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    server = build_mcp(service, queue=queue)
    register_dashboard(server, service, queue=queue)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})
    return client, queue


# -- /dashboard/api/tasks/reassign -----------------------------------------

def test_reassign_moves_a_real_task_to_a_new_session(client_and_queue):
    client, queue = client_and_queue
    created = queue.create_task("t", "a real prompt here", session="test-a")
    response = client.post("/dashboard/api/tasks/reassign",
                           json={"task_id": created["task_id"], "to_session": "test-b", "reason": "rebalance"})
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert queue.store.get_task(created["task_id"]).session == "test-b"


def test_reassign_refuses_a_non_allowed_target_session(client_and_queue):
    client, queue = client_and_queue
    created = queue.create_task("t", "a real prompt here", session="test-a")
    response = client.post("/dashboard/api/tasks/reassign",
                           json={"task_id": created["task_id"], "to_session": "not-allowed-session", "reason": "x"})
    assert response.status_code == 403
    assert response.json()["error"] == "READ_RESTRICTED"
    assert queue.store.get_task(created["task_id"]).session == "test-a"  # never touched


def test_reassign_unknown_task_id(client_and_queue):
    client, _queue = client_and_queue
    response = client.post("/dashboard/api/tasks/reassign",
                           json={"task_id": "no-such-id", "to_session": "test-b", "reason": "x"})
    assert response.status_code == 404
    assert response.json()["error"] == "TASK_NOT_FOUND"


def test_reassign_invalid_request_body(client_and_queue):
    client, _queue = client_and_queue
    response = client.post("/dashboard/api/tasks/reassign", json={"task_id": ""})
    assert response.status_code == 400
    assert response.json()["error"] == "INVALID_REQUEST"


def test_reassign_never_moves_a_running_task(client_and_queue):
    client, queue = client_and_queue
    created = queue.create_task("t", "a real prompt here", session="test-a")
    with queue.store._connection() as connection:
        connection.execute("UPDATE queue_tasks SET status = 'RUNNING' WHERE id = ?", (created["task_id"],))
    response = client.post("/dashboard/api/tasks/reassign",
                           json={"task_id": created["task_id"], "to_session": "test-b", "reason": "x"})
    assert response.status_code == 400
    assert "error" in response.json()
    assert queue.store.get_task(created["task_id"]).session == "test-a"


# -- /dashboard/requirements ------------------------------------------------

def test_requirements_route_serves_the_real_doc(client_and_queue):
    client, _queue = client_and_queue
    response = client.get("/dashboard/requirements")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "Unified Task System" in response.text  # a real §20 heading from the real file


def test_requirements_link_present_in_dashboard_and_task_modal(client_and_queue):
    client, _queue = client_and_queue
    response = client.get("/dashboard")
    html = response.text
    assert 'href="/dashboard/requirements"' in html
    assert 'id="requirementsLink"' in html
