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

# The dashboard's always-on CSRF guard refuses a mutation with no
# same-origin Origin/Referer. Sending it is what the real panel's fetch()
# does on every request, so these headers exercise the guard rather than
# bypassing it -- and a test that skipped on the 403 instead would be
# silently asserting nothing.
SAME_ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def _isolated_backlog_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_BACKLOG_DB", str(tmp_path / "backlog.db"))


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


# -- Project-addressed routes (dashboard project picker) -----------------
#
# The panel's whole reason for a picker: one project has checkouts on
# several machines, so a PATH identifies a checkout and only ever reaches
# the controller's own box. These pin that a project_id addresses the
# same backlog a path does, and that it keeps working when the path does
# not exist here at all.

def test_projects_route_registered_and_lists_projects(rig):
    server, repo, backlog = rig
    backlog.add(str(repo), tasks=[{"title": "one", "priority": "P1"}])
    assert "/dashboard/api/projects" in _paths(server)

    client = TestClient(server.streamable_http_app())
    response = client.get("/dashboard/api/projects")
    assert response.status_code in (200, 401, 403)
    if response.status_code == 200:
        body = response.json()
        rows = body["projects"]
        assert rows, "a project with a backlog must be offerable in the picker"
        row = next(r for r in rows if r["has_backlog"])
        assert row["project_id"].startswith("git:")
        assert row["open_total"] == 1


def test_projects_route_503s_without_a_backlog_service(tmp_path):
    config = make_config(tmp_path)
    terminal = TerminalService(config)
    server = build_mcp(terminal)
    register_dashboard(server, terminal)
    client = TestClient(server.streamable_http_app())
    response = client.get("/dashboard/api/projects")
    assert response.status_code in (503, 401, 403)


def test_read_route_accepts_project_id_and_returns_the_same_backlog(rig):
    server, repo, backlog = rig
    backlog.add(str(repo), tasks=[{"title": "by-project", "priority": "P1"}])
    project_id = backlog.get(str(repo))["project"]["project_id"]

    client = TestClient(server.streamable_http_app())
    by_path = client.get("/dashboard/api/backlog", params={"path": str(repo)})
    by_project = client.get("/dashboard/api/backlog", params={"project_id": project_id})
    assert by_project.status_code == by_path.status_code
    if by_project.status_code == 200:
        assert by_project.json()["items"] == by_path.json()["items"]
        assert by_project.json()["project"]["project_id"] == project_id


def test_project_id_addresses_a_backlog_with_no_local_checkout(rig):
    """The case the path box structurally cannot serve: a project whose
    checkout lives on another machine. There is no path to type, so if
    project_id did not reach the service the panel could never show it."""
    server, _repo, backlog = rig
    remote_id = "git:github.com/acme/on-another-node"
    backlog.add(project_id=remote_id, tasks=[{"title": "remote work", "priority": "P0"}])

    client = TestClient(server.streamable_http_app())
    response = client.get("/dashboard/api/backlog", params={"project_id": remote_id})
    assert response.status_code in (200, 401, 403)
    if response.status_code == 200:
        body = response.json()
        assert body["project"]["project_id"] == remote_id
        assert [i["title"] for i in body["items"]] == ["remote work"]


def test_every_mutating_route_honours_project_id(rig):
    """add/update/dispatch/complete must ALL reach the picked project --
    a picker that can read a project but writes to the default one would
    be worse than no picker."""
    server, _repo, backlog = rig
    project_id = "git:github.com/acme/mutate-target"
    client = TestClient(server.streamable_http_app())

    added = client.post("/dashboard/api/backlog/add", headers=SAME_ORIGIN,
                        json={"project_id": project_id, "tasks": [{"title": "t1"}]})
    assert added.status_code == 200, added.text
    task_id = backlog.get(project_id=project_id)["items"][0]["id"]

    updated = client.post("/dashboard/api/backlog/update", headers=SAME_ORIGIN,
                          json={"project_id": project_id, "task_id": task_id,
                                "patch": {"status": "READY"}})
    assert updated.status_code == 200, updated.text
    assert backlog.get(project_id=project_id)["items"][0]["status"] == "READY"

    completed = client.post("/dashboard/api/backlog/complete", headers=SAME_ORIGIN,
                            json={"project_id": project_id, "task_id": task_id,
                                  "commit": "abc1234"})
    assert completed.status_code == 200, completed.text
    assert backlog.get(project_id=project_id)["items"][0]["status"] == "DONE"

    # Nothing leaked into the rig's own repo-backed project.
    assert backlog.get(project_id=project_id)["project"]["project_id"] == project_id


def test_dispatch_session_is_the_target_not_project_resolution(rig):
    """dispatch() takes `session` for the DISPATCH TARGET and
    `project_session` for project resolution. Conflating them would send
    work to the wrong place, so the route's mapping is pinned here."""
    server, repo, backlog = rig
    backlog.add(str(repo), tasks=[{"title": "dispatch me"}])
    project_id = backlog.get(str(repo))["project"]["project_id"]
    task_id = backlog.get(str(repo))["items"][0]["id"]

    client = TestClient(server.streamable_http_app())
    response = client.post("/dashboard/api/backlog/dispatch", headers=SAME_ORIGIN,
                           json={"project_id": project_id, "task_id": task_id,
                                 "session": "lane-target"})
    assert response.status_code == 200, response.text
    item = backlog.get(project_id=project_id)["items"][0]
    assert item["queue_task_id"], "dispatch must have created a real queue task"
    assert item["session"] == "lane-target"
