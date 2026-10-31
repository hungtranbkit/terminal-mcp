"""Control Tower routes on the EXISTING /dashboard/api/projects surface (#106).

Guards are exercised, not bypassed: CSRF (same-origin) on every POST, CF
Access identity when configured, and owner-only resume.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import terminal_mcp.dashboard as dashboard_module
from terminal_mcp.cf_access import AccessIdentity
from terminal_mcp.config import (AppConfig, DashboardConfig, InputPolicyConfig,
                                 PermissionsConfig, SessionAccessConfig)
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.mcp_app import build_mcp

from .test_autopilot_view import P1, P2, _add, tower  # noqa: F401 -- fixture

SAME_ORIGIN = {"Origin": "http://testserver"}
ROUTES = ("/dashboard/api/projects/autopilot", "/dashboard/api/projects/autopilot/notifications",
          "/dashboard/api/projects/autopilot/pause", "/dashboard/api/projects/autopilot/resume")


def _config(*, cf=False, mutations=True):
    dashboard = DashboardConfig(mutations_enabled=mutations, **(
        {"cloudflare_access_team_domain": "t.cloudflareaccess.com",
         "cloudflare_access_audience": "aud"} if cf else {}))
    return AppConfig(PermissionsConfig(True, True), ("test-*",), 50, 20,
                     InputPolicyConfig(allowed_session_patterns=("test-*",)),
                     dashboard=dashboard,
                     session_access=SessionAccessConfig(default_read=True, default_input=True))


def _client(config, autopilot):
    terminal = TerminalService(config)
    server = build_mcp(terminal)
    register_dashboard(server, terminal, autopilot=autopilot)
    return server, TestClient(server.streamable_http_app())


def test_routes_registered_and_unavailable_without_runtime():
    server, client = _client(_config(), None)
    paths = {r.path for r in server._custom_starlette_routes if hasattr(r, "methods")}
    assert set(ROUTES) <= paths
    assert client.get("/dashboard/api/projects/autopilot").status_code == 404


def test_overview_and_snapshot_reads(tower):  # noqa: F811
    _add(tower, "p1-work", "A")
    _add(tower, "p2-work", "Z", project=P2)
    _, client = _client(_config(), tower)
    overview = client.get("/dashboard/api/projects/autopilot").json()
    assert {p["project_id"] for p in overview["projects"]} == {P1, P2}
    snap = client.get("/dashboard/api/projects/autopilot", params={"project_id": P1}).json()
    assert snap["schema"] == "autopilot_status/v1" and len(snap["tasks"]) == 1
    notes = client.get("/dashboard/api/projects/autopilot/notifications",
                       params={"project_id": P1, "since_seq": "nope"})
    assert notes.status_code == 400


def test_pause_requires_same_origin(tower):  # noqa: F811
    _add(tower, "p1-work", "A")
    _, client = _client(_config(), tower)
    refused = client.post("/dashboard/api/projects/autopilot/pause", json={"project_id": P1})
    assert refused.status_code == 403
    assert tower.test["store"].lane_status("p1-work")["paused"] is False
    ok = client.post("/dashboard/api/projects/autopilot/pause",
                     json={"project_id": P1, "reason": "review"}, headers=SAME_ORIGIN)
    assert ok.status_code == 200 and ok.json()["paused"] == ["p1-work"]


def test_mutations_disabled_blocks_controls(tower):  # noqa: F811
    _, client = _client(_config(mutations=False), tower)
    response = client.post("/dashboard/api/projects/autopilot/pause", json={"project_id": P1},
                           headers=SAME_ORIGIN)
    assert response.status_code == 403


def test_resume_without_verified_owner_is_forbidden(tower):  # noqa: F811
    _add(tower, "p1-work", "A")
    _, client = _client(_config(), tower)
    client.post("/dashboard/api/projects/autopilot/pause", json={"project_id": P1}, headers=SAME_ORIGIN)
    response = client.post("/dashboard/api/projects/autopilot/resume", json={"project_id": P1},
                           headers=SAME_ORIGIN)
    assert response.status_code == 403 and response.json()["error"] == "OWNER_APPROVAL_REQUIRED"
    assert tower.test["store"].lane_status("p1-work")["paused"] is True


@pytest.mark.parametrize("email,status", [("owner@test", 200), ("intruder@test", 403)])
def test_resume_with_cf_access_identity(tower, monkeypatch, email, status):  # noqa: F811
    _add(tower, "p1-work", "A")
    identity = AccessIdentity(email=email, subject="u", raw_claims={})
    monkeypatch.setattr(dashboard_module, "verify_access_assertion", lambda token, **kw: identity)
    _, client = _client(_config(cf=True), tower)
    headers = {**SAME_ORIGIN, "Cf-Access-Jwt-Assertion": "verified-in-test"}
    assert client.post("/dashboard/api/projects/autopilot/pause", json={"project_id": P1},
                       headers=headers).status_code == 200
    response = client.post("/dashboard/api/projects/autopilot/resume",
                           json={"project_id": P1, "idempotency_key": "r1"}, headers=headers)
    assert response.status_code == status
    assert tower.test["store"].lane_status("p1-work")["paused"] is (status != 200)


def test_cf_access_configured_blocks_anonymous_reads(tower, monkeypatch):  # noqa: F811
    _, client = _client(_config(cf=True), tower)
    assert client.get("/dashboard/api/projects/autopilot").status_code in (401, 403)
