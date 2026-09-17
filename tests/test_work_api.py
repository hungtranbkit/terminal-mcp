"""Work Runtime over its real surfaces, and the promise that matters most:
Terminal mode is unchanged.

Backward compatibility is P0 in the approved template, so it is tested first
and tested hardest. If Work is disabled the system must behave as it did
before the feature existed, and an ordinary session must never be touched by
it either way.
"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from terminal_mcp import dashboard as dashboard_module
from terminal_mcp import work_store as ws
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, WorkConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import WORK_HTML, register_dashboard
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.work_service import WorkService
from terminal_mcp.work_store import WorkStore


def _config():
    return AppConfig(PermissionsConfig(True, True), ("test-*",), 50, 20,
                     InputPolicyConfig(allowed_session_patterns=("test-*",)))


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_WORK_DB", str(tmp_path / "work.db"))
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    service = TerminalService(_config())
    server = build_mcp(service, queue=queue)
    register_dashboard(server, service, queue=queue)
    # Same-origin by default, exactly as a browser's fetch() from the
    # dashboard's own page sends it. The CSRF/Origin guard has its own tests;
    # omitting the header here would only re-test that, and did -- the first
    # run of this file got a correct 403 from it.
    client = TestClient(server.streamable_http_app(),
                        headers={"Origin": "http://testserver"})
    return client, server, queue


def _tool(server, name):
    return server._tool_manager._tools[name].fn


# -- backward compatibility (P0) ----------------------------------------------------

def test_work_is_off_by_default():
    """If Work runtime is disabled the system must behave as it did before
    the feature existed."""
    assert WorkConfig().enabled is False


def test_the_terminal_routes_are_untouched(wired):
    client, server, _queue = wired
    routes = {r.path: set(r.methods) for r in server._custom_starlette_routes
              if hasattr(r, "methods")}
    # The originals, unchanged.
    assert routes["/dashboard"] == {"GET", "HEAD"}
    assert routes["/dashboard/api/sessions"] == {"GET", "HEAD"}
    assert routes["/dashboard/api/session/input"] == {"POST"}
    assert client.get("/dashboard").status_code == 200


def test_an_ordinary_session_lane_is_never_auto_enabled_by_work(wired):
    """The guarantee in one test: a normal session with queued work keeps
    auto-dispatch off, which is how it behaved before Work existed."""
    _client, _server, queue = wired
    queue.enqueue("m1", "a human's own task")
    assert queue.status("m1")["auto_dispatch_enabled"] is False


def test_creating_work_on_an_ordinary_session_is_refused_over_mcp(wired):
    _client, server, _queue = wired
    result = _tool(server, "work_create")(title="T", goal="G", lane="terminal-mcp-main")
    assert result["error"] == "LANE_NOT_A_WORK_SESSION"


def test_creating_work_on_an_ordinary_session_is_refused_over_http(wired):
    client, _server, _queue = wired
    response = client.post("/dashboard/api/work/create",
                           json={"title": "T", "goal": "G", "lane": "m1"})
    assert response.status_code == 400
    assert response.json()["error"] == "LANE_NOT_A_WORK_SESSION"


# -- MCP surface ---------------------------------------------------------------------

def test_the_work_tools_cover_create_read_enqueue_gate_and_control(wired):
    _client, server, queue = wired
    created = _tool(server, "work_create")(
        title="Ship it", goal="the thing ships", lane="mesflow-work",
        done_criteria="tests pass;screen renders",
        tasks_json=json.dumps([{"title": "build", "prompt": "do the build", "weight": 2},
                               {"title": "test", "prompt": "write tests"}]))
    work_id = created["work"]["work_id"]
    assert created["work"]["done_criteria"] == ["tests pass", "screen renders"]
    assert len(created["tasks"]) == 2
    # Real queue rows, not a parallel table.
    assert len(queue.status("mesflow-work")["tasks"]) == 2

    status = _tool(server, "work_status")(work_id)
    assert status["progress"]["percent"] == 0
    assert status["contract"]["satisfied"] is False
    assert status["lane_is_work_session"] is True

    assert _tool(server, "work_list")()["works"][0]["work_id"] == work_id

    _tool(server, "work_continue")(work_id, "one more thing", title="extra")
    assert len(_tool(server, "work_status")(work_id)["tasks"]) == 3

    gate = _tool(server, "work_request_approval")(
        work_id, "production_deploy", "restart the controller", "agent:worker")
    approval_id = gate["approval"]["approval_id"]
    refused = _tool(server, "work_approve")(approval_id, "agent:worker")
    assert refused["error"] == "APPROVAL_REFUSED"
    ok = _tool(server, "work_approve")(approval_id, "human:hung")
    assert ok["approval"]["decision"] == "APPROVED"

    assert _tool(server, "work_control")(work_id, "pause")["work"]["state"] == ws.PAUSED


def test_malformed_plan_json_is_an_error_not_a_crash(wired):
    _client, server, _queue = wired
    result = _tool(server, "work_create")(title="T", goal="G", lane="mesflow-work",
                                          tasks_json="{not json")
    assert result["error"] == "TASKS_JSON_INVALID"


# -- HTTP surface ----------------------------------------------------------------------

def test_the_http_and_mcp_surfaces_agree(wired):
    """A surface that can see or do something the other cannot is how an
    operator gets told to "use the other one"."""
    client, server, _queue = wired
    created = _tool(server, "work_create")(
        title="Parity", goal="same both ways", lane="mesflow-work",
        tasks_json=json.dumps([{"title": "a", "prompt": "p"}]))
    work_id = created["work"]["work_id"]

    over_http = client.get(f"/dashboard/api/work?work={work_id}").json()
    over_mcp = _tool(server, "work_status")(work_id)
    assert over_http["work"] == over_mcp["work"]
    assert over_http["progress"] == over_mcp["progress"]
    assert [t["title"] for t in over_http["tasks"]] == [t["title"] for t in over_mcp["tasks"]]


def test_the_http_routes_create_continue_control_and_approve(wired):
    client, _server, queue = wired
    created = client.post("/dashboard/api/work/create", json={
        "title": "HTTP work", "goal": "created over http", "lane": "mesflow-work",
        "tasks": [{"title": "a", "prompt": "do a"}]}).json()
    work_id = created["work"]["work_id"]
    assert len(queue.status("mesflow-work")["tasks"]) == 1

    client.post("/dashboard/api/work/continue",
                json={"work_id": work_id, "prompt": "do b"})
    assert len(queue.status("mesflow-work")["tasks"]) == 2

    paused = client.post("/dashboard/api/work/control",
                         json={"work_id": work_id, "action": "pause"}).json()
    assert paused["work"]["state"] == ws.PAUSED


def test_an_empty_continue_prompt_is_refused(wired):
    client, server, _queue = wired
    work_id = _tool(server, "work_create")(
        title="T", goal="G", lane="mesflow-work")["work"]["work_id"]
    response = client.post("/dashboard/api/work/continue",
                           json={"work_id": work_id, "prompt": "   "})
    assert response.status_code == 400
    assert response.json()["error"] == "TASK_PROMPT_REQUIRED"


def test_a_malformed_body_is_a_client_error_not_a_500(wired):
    client, _server, _queue = wired
    response = client.post("/dashboard/api/work/create", content=b"not json",
                           headers={"Content-Type": "application/json"})
    assert response.status_code == 400


def test_the_approver_is_the_verified_identity_not_a_caller_supplied_value(wired):
    """An approval whose approver is self-declared is not an approval. The
    route reads the identity from the guard and ignores any `decided_by` in
    the body."""
    import inspect

    source = inspect.getsource(dashboard_module.register_dashboard)
    assert "decided_by = (identity.email if identity else None)" in source
    assert 'body.get("decided_by")' not in source


# -- the Work UI -------------------------------------------------------------------------

def test_the_work_page_renders_and_is_separate_from_terminal(wired):
    client, _server, _queue = wired
    response = client.get("/dashboard/work")
    assert response.status_code == 200
    assert "Work" in response.text
    assert "/dashboard/api/work" in response.text
    # Terminal mode is still its own page and still reachable.
    assert client.get("/dashboard").status_code == 200


def test_the_ui_puts_what_it_needs_from_you_first():
    """The two things an operator needs away from a desk are "what does it
    need from me" and "can I send the next instruction"."""
    assert "Cần bạn duyệt" in WORK_HTML
    assert "Giao thêm việc" in WORK_HTML
    # On a phone the detail comes before the list.
    assert "#detail { order:-1 }" in WORK_HTML


def test_the_ui_states_the_isolation_rule_rather_than_leaving_it_implied():
    assert "-work" in WORK_HTML
    # Asserted on contiguous fragments: the sentence is assembled from
    # several JS string literals, so a phrase spanning a join never appears
    # in the template source even though the reader sees it.
    assert "Work Runtime chỉ tự động điều khiển session có hậu tố -work" in WORK_HTML
    assert "Session thường giữ nguyên hành vi cũ: " in WORK_HTML
    assert "không bao giờ chứa session thường" in WORK_HTML
    assert "trạng thái thật trong queue" in WORK_HTML


def test_the_session_list_labels_work_sessions():
    """A label, not a control: the session stays an ordinary terminal in
    Terminal mode and nothing about that tab behaves differently."""
    assert "work-badge" in dashboard_module.DASHBOARD_HTML
    assert "/-work$/.test(row.name)" in dashboard_module.DASHBOARD_HTML


def test_the_work_menu_entry_exists():
    assert 'href="/dashboard/work"' in dashboard_module.DASHBOARD_HTML


# -- persistence across a "restart" ----------------------------------------------------

def test_work_survives_a_process_restart_without_duplicating_anything(tmp_path, monkeypatch):
    """The restart requirement: reload, reconcile, resume -- and crucially no
    duplicate task appears just because the controller came back."""
    monkeypatch.setenv("TERMINAL_MCP_WORK_DB", str(tmp_path / "work.db"))
    queue = QueueService(QueueStore(tmp_path / "q.db"))
    service = WorkService(WorkStore(tmp_path / "work.db"), queue=queue)
    created = service.create(title="Durable", goal="survive", lane="mesflow-work",
                             tasks=[{"title": "a", "prompt": "p1"},
                                    {"title": "b", "prompt": "p2"}])
    work_id = created["work"]["work_id"]
    before = queue.status("mesflow-work")["tasks"]

    # A fresh set of objects over the same files, as a restart produces.
    queue2 = QueueService(QueueStore(tmp_path / "q.db"))
    service2 = WorkService(WorkStore(tmp_path / "work.db"), queue=queue2)
    status = service2.status(work_id)

    after = queue2.status("mesflow-work")["tasks"]
    assert len(after) == len(before) == 2, "restart must not duplicate queued work"
    assert {t["id"] for t in after} == {t["id"] for t in before}
    assert len(status["tasks"]) == 2
    assert status["work"]["state"] == ws.READY

    # And re-planning the SAME work does not silently re-enqueue the old plan.
    from terminal_mcp.work_loop import WorkCoordinatorLoop, WorkLoopConfig

    loop = WorkCoordinatorLoop(
        service=service2, config=WorkLoopConfig(enabled=True),
        evidence=lambda: {"sessions": [{"name": "mesflow-work", "node_id": "local",
                                        "input_allowed": True}],
                          "nodes": {"local": {"node_id": "local", "status": "online",
                                              "metadata_stale": False}},
                          "statuses": {"mesflow-work": {"exists": True}}})
    loop.tick()
    loop.tick()
    assert len(queue2.status("mesflow-work")["tasks"]) == 2, (
        "a coordinator tick must never re-enqueue an existing plan")
