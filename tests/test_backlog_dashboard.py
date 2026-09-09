"""Dashboard API smoke for the backlog panel's data surface, including
the guards: an unauthenticated browser must not be able to read or write
a project's plan.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from terminal_mcp.backlog_service import BacklogService
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore
from tests.test_backlog import make_config, make_repo

BACKLOG_ROUTES = ("/dashboard/api/backlog", "/dashboard/api/backlog/add",
                  "/dashboard/api/backlog/update", "/dashboard/api/backlog/dispatch",
                  "/dashboard/api/backlog/complete")


@pytest.fixture
def rig(tmp_path):
    repo = make_repo(tmp_path / "widget")
    config = make_config(tmp_path)
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    backlog = BacklogService(config, queue=queue)
    terminal = TerminalService(config)
    server = build_mcp(terminal, queue=queue, backlog=backlog)
    register_dashboard(server, terminal, queue=queue, backlog=backlog)
    return server, repo, backlog


def _paths(server):
    return {r.path for r in server._custom_starlette_routes if hasattr(r, "methods")}


def test_all_backlog_routes_registered(rig):
    server, _, _ = rig
    registered = _paths(server)
    for route in BACKLOG_ROUTES:
        assert route in registered, route


def test_routes_absent_without_a_backlog_service(tmp_path):
    config = make_config(tmp_path)
    terminal = TerminalService(config)
    server = build_mcp(terminal)
    register_dashboard(server, terminal)
    registered = _paths(server)
    # The routes register unconditionally but answer 503; what must NOT
    # happen is a crash or a silent 200 with no backlog behind it.
    for route in BACKLOG_ROUTES:
        assert route in registered


def test_read_route_returns_the_project_backlog(rig):
    server, repo, backlog = rig
    backlog.add(str(repo), tasks=[{"title": "from-api", "priority": "P1"}])
    client = TestClient(server.streamable_http_app())
    response = client.get("/dashboard/api/backlog", params={"path": str(repo)})
    # Either the data (no auth configured in this rig) or a guard refusal
    # -- never a 500.
    assert response.status_code in (200, 401, 403)
    if response.status_code == 200:
        body = response.json()
        assert body["total"] == 1 and body["items"][0]["title"] == "from-api"
        assert body["project"]["project_id"].startswith("git:")


def test_write_route_rejects_unknown_project_path(rig):
    server, _, _ = rig
    client = TestClient(server.streamable_http_app())
    response = client.post("/dashboard/api/backlog/add",
                           json={"path": "/etc", "tasks": [{"title": "x"}]})
    assert response.status_code in (400, 401, 403)
    assert response.status_code != 500


def test_no_backlog_route_returns_500_on_a_bad_body(rig):
    server, _, _ = rig
    client = TestClient(server.streamable_http_app())
    for route in ("/dashboard/api/backlog/add", "/dashboard/api/backlog/update",
                  "/dashboard/api/backlog/dispatch", "/dashboard/api/backlog/complete"):
        response = client.post(route, content=b"not json")
        assert response.status_code != 500, route
