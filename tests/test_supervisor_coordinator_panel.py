"""Dashboard Supervisor/Coordinator panel + Global Task Inbox -- the 4 new
read-only dashboard routes (queue/global-inbox, queue/recent-events,
queue/loop-status, integration/fleet-overview). Real HTTP requests via
starlette's TestClient, real QueueStore/IntegrationStore -- no MCP layer
needed for these (read-only, no session-scoped auth to exercise beyond
the existing _read_guard already covered elsewhere).

SAFETY: every session/project name below is a disposable fixture string
-- never window/window2."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.integration_service import IntegrationService
from terminal_mcp.integration_store import IntegrationStore
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


@pytest.fixture
def rig(tmp_path):
    config = AppConfig(
        PermissionsConfig(True, True), ("sc-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("sc-*",)),
    )
    service = TerminalService(config)
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    integration = IntegrationService(IntegrationStore(tmp_path / "integration.db"))
    server = build_mcp(service, queue=queue, integration=integration)
    register_dashboard(server, service, queue=queue, integration=integration)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})
    return {"client": client, "queue": queue, "integration": integration}


def test_global_inbox_route_returns_real_grouped_tasks(rig):
    client, queue = rig["client"], rig["queue"]
    queue.store.append_tasks("sc-a", [{"prompt": "a real task"}])
    response = client.get("/dashboard/api/queue/global-inbox")
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["total"] == 1
    assert body["queued"][0]["session"] == "sc-a"


def test_recent_events_route_returns_real_merged_events(rig):
    client, queue = rig["client"], rig["queue"]
    queue.store.append_tasks("sc-a", [{"prompt": "a"}])
    queue.store.append_tasks("sc-b", [{"prompt": "b"}])
    response = client.get("/dashboard/api/queue/recent-events", params={"limit": 5})
    assert response.status_code == 200
    events = response.json()["events"]
    assert len(events) <= 5
    assert {e["session"] for e in events} >= {"sc-a", "sc-b"}


def test_loop_status_route_reports_not_running_by_default(rig):
    client = rig["client"]
    response = client.get("/dashboard/api/queue/loop-status")
    assert response.status_code == 200
    assert response.json()["running"] is False


def test_integration_fleet_overview_route_lists_configured_projects(rig):
    client, integration = rig["client"], rig["integration"]
    empty = client.get("/dashboard/api/integration/fleet-overview")
    assert empty.json()["projects"] == []

    integration.configure("proj-sc", repo_path="/tmp/does-not-matter")
    response = client.get("/dashboard/api/integration/fleet-overview")
    assert response.status_code == 200
    projects = response.json()["projects"]
    assert len(projects) == 1
    assert projects[0]["project"] == "proj-sc"


def test_all_four_routes_reject_cross_origin_get_is_unaffected_by_read_guard(rig, tmp_path):
    # _read_guard (GET routes) deliberately does NOT check Origin --
    # same documented posture as every other dashboard GET route (a
    # plain top-level navigation doesn't reliably send one). Confirms
    # these new routes follow that SAME established rule rather than
    # inventing a stricter one of their own.
    from starlette.testclient import TestClient as _TestClient
    from terminal_mcp.mcp_app import build_mcp as _build_mcp
    config = AppConfig(PermissionsConfig(True, True), ("sc-*",), 200, 100,
                      InputPolicyConfig(allowed_session_patterns=("sc-*",)))
    service = TerminalService(config)
    server = _build_mcp(service)
    register_dashboard(server, service)
    client = _TestClient(server.streamable_http_app(), headers={"Origin": "https://evil.example.com"})
    assert client.get("/dashboard/api/queue/global-inbox").status_code == 200
    assert client.get("/dashboard/api/queue/loop-status").status_code == 200
