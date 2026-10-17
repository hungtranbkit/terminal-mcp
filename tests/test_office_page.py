"""/dashboard/office (Văn phòng AI, issue #67): routes, guard, role isolation."""
from __future__ import annotations

import os

from starlette.testclient import TestClient

from terminal_mcp.audit import AuditStore
from terminal_mcp.office_page import OFFICE_HTML
from terminal_mcp.task_center_reader import HttpStatusError, TaskCenterReader

NOW_ROW = {"task_id": "tc_" + "c" * 32, "user_id": "owner", "project": "Terminal MCP",
           "title": "Văn phòng AI", "details": "AC:\n- đẹp", "source": "manual", "external_id": "",
           "external_url": "", "node": "", "state": "waiting_input", "state_source": "user",
           "evidence": [], "created_at": 1.0, "updated_at": 2.0}


class Gateway:
    def __init__(self):
        self.calls = []

    def __call__(self, base, path, params, token):
        self.calls.append(path)
        if path.endswith("/summary"):
            return {"total": 1, "counts": {"waiting_input": 1}, "sources": {"chatgpt_history": "not_connected"}}
        return {"data": [NOW_ROW], "total": 1, "next_offset": None}


def _server(tmp_path, *, reader=None, dashboard=None):
    from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
    from terminal_mcp.core import TerminalService
    from terminal_mcp.dashboard import register_dashboard
    from terminal_mcp.grants import SessionGrantStore
    from terminal_mcp.mcp_app import build_mcp

    kwargs = {"dashboard": dashboard} if dashboard is not None else {}
    config = AppConfig(PermissionsConfig(True, True), ("test-*",), 50, 20,
                       InputPolicyConfig(allowed_session_patterns=("test-*",)), **kwargs)
    service = TerminalService(config, audit=AuditStore(tmp_path / "audit.db"),
                              grants=SessionGrantStore(tmp_path / "grants.db"))
    server = build_mcp(service)
    if reader is not None:
        server.task_center_reader = reader
    register_dashboard(server, service)
    return server


def _reader(tmp_path, gateway):
    token = tmp_path / "tok"
    token.write_text("secret-session-value")
    os.chmod(token, 0o600)
    return TaskCenterReader("https://gw.example", str(token), fetch=gateway)


def test_office_page_is_served_with_nav_and_safe_rendering(tmp_path):
    client = TestClient(_server(tmp_path).streamable_http_app(), base_url="https://testserver")
    page = client.get("/dashboard/office")
    assert page.status_code == 200
    assert "data-global-nav" in page.text and 'href="/dashboard/office"' in page.text
    assert "/dashboard/api/office" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    # Server strings never reach innerHTML; motion and fallback list exist.
    assert "innerHTML" not in OFFICE_HTML
    assert "prefers-reduced-motion" in OFFICE_HTML and 'id="rosterList"' in OFFICE_HTML
    assert "Chưa kết nối" in OFFICE_HTML


def test_office_api_without_gateway_says_chua_ket_noi(tmp_path):
    client = TestClient(_server(tmp_path).streamable_http_app(), base_url="https://testserver")
    body = client.get("/dashboard/api/office").json()
    assert body["read_only"] is True and isinstance(body["sessions"], list)
    tc = body["task_center"]
    assert tc["connection"] == "not_configured" and tc["connection_label"] == "Chưa kết nối"
    assert tc["tasks"] == [] and tc["history_import"]["label"] == "Chưa kết nối"


def test_office_api_reads_task_center_for_operator(tmp_path):
    gateway = Gateway()
    server = _server(tmp_path, reader=_reader(tmp_path, gateway))
    client = TestClient(server.streamable_http_app(), base_url="https://testserver")
    response = client.get("/dashboard/api/office")
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["privileged"] is True
    tc = body["task_center"]
    assert tc["connection"] == "connected"
    assert [t["bucket"] for t in tc["tasks"]] == ["waiting_input"]
    assert tc["tasks"][0]["acceptance_criteria"] == ["đẹp"]
    assert "secret-session-value" not in response.text
    assert all(path.startswith("/api/workspace/task-center/") for path in gateway.calls)


def test_viewer_role_gets_no_task_rows(tmp_path, monkeypatch):
    gateway = Gateway()
    server = _server(tmp_path, reader=_reader(tmp_path, gateway))
    import terminal_mcp.dashboard as dashboard_module
    monkeypatch.setattr(dashboard_module, "role_for_identity", lambda *a, **k: "viewer")
    client = TestClient(server.streamable_http_app(), base_url="https://testserver")
    body = client.get("/dashboard/api/office").json()
    assert body["privileged"] is False
    assert body["task_center"]["connection"] == "forbidden" and body["task_center"]["tasks"] == []
    assert gateway.calls == []


def test_gateway_failure_never_breaks_the_office(tmp_path):
    def broken(*_args):
        raise HttpStatusError(503)

    client = TestClient(_server(tmp_path, reader=_reader(tmp_path, broken)).streamable_http_app(),
                        base_url="https://testserver")
    response = client.get("/dashboard/api/office")
    assert response.status_code == 200
    assert response.json()["task_center"]["connection"] == "error"


def test_office_routes_are_read_guarded_when_access_is_configured(tmp_path):
    from terminal_mcp.config import DashboardConfig

    server = _server(tmp_path, dashboard=DashboardConfig(
        cloudflare_access_team_domain="test-team.cloudflareaccess.com",
        cloudflare_access_audience="test-aud"))
    client = TestClient(server.streamable_http_app())
    for path in ("/dashboard/office", "/dashboard/api/office"):
        response = client.get(path)
        assert response.status_code == 403, path
        assert response.json()["error"] == "CLOUDFLARE_ACCESS_VERIFICATION_FAILED"


def test_office_api_has_no_write_methods(tmp_path):
    client = TestClient(_server(tmp_path).streamable_http_app(), base_url="https://testserver")
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/dashboard/api/office").status_code == 405
