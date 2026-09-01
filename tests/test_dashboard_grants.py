"""Dashboard per-session read/input grants: an operator can explicitly
authorize a newly-discovered, non-whitelisted tmux session from the
dashboard itself -- read first, then input, both revocable, both
identity-pinned the same way bindings already are, with the raw MCP tool
surface (terminal_tail/terminal_status/terminal_send_text/terminal_bind)
completely unaffected throughout.
"""
from __future__ import annotations

import time

from starlette.testclient import TestClient

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.mcp_app import build_mcp


def _config(*, terminal_input=True, allowed_sensitive_commands=()) -> AppConfig:
    return AppConfig(
        PermissionsConfig(True, terminal_input), ("test-*", "agent-*"), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",),
                          allowed_sensitive_commands=allowed_sensitive_commands),
    )


def _client(config: AppConfig, *, grants_path=None, audit_path=None) -> tuple[TestClient, TerminalService]:
    from terminal_mcp.audit import AuditStore
    from terminal_mcp.grants import SessionGrantStore

    grants = SessionGrantStore(grants_path) if grants_path is not None else None
    audit = AuditStore(audit_path) if audit_path is not None else None
    service = TerminalService(config, grants=grants, audit=audit)
    server = build_mcp(service)
    register_dashboard(server, service)
    return TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"}), service


# ---------------------------------------------------------------------------
# Full workflow: discovery -> grant read -> view output -> grant input ->
# confirmed send -> revoke input -> revoke read
# ---------------------------------------------------------------------------


def test_full_grant_workflow(tmp_path, tmux_session_factory):
    name = "newsession-grant-demo"
    session = tmux_session_factory(name, "bash -lc 'read x; echo GOT:$x; sleep 20'")
    time.sleep(0.3)
    client, service = _client(_config(), grants_path=tmp_path / "grants.db", audit_path=tmp_path / "audit.db")

    # 1. Discovery: the session appears in the list, restricted.
    rows = client.get("/dashboard/api/sessions").json()["sessions"]
    row = next(r for r in rows if r["name"] == session)
    assert row["allowed"] is False
    assert row["effective_read"] is False
    assert row["state"] == "RESTRICTED"

    # Before any grant, detail is a clean, contentless 403.
    denied = client.get(f"/dashboard/api/session?name={session}")
    assert denied.status_code == 403
    assert denied.json()["error"] == "READ_RESTRICTED"

    # 2. Grant read.
    granted_read = client.post("/dashboard/api/session/grant-read", json={"name": session, "enabled": True})
    assert granted_read.status_code == 200
    assert granted_read.json() == {"session": session, "read_enabled": True, "input_enabled": False}

    # 3. View output: now succeeds, input composer not yet warranted.
    detail = client.get(f"/dashboard/api/session?name={session}")
    assert detail.status_code == 200
    body = detail.json()
    assert "error" not in body["tail"]
    assert body["input_allowed"] is False
    assert body["grant"] == {"read_enabled": True, "input_enabled": False}

    # Sessions listing now reflects the grant too.
    rows = client.get("/dashboard/api/sessions").json()["sessions"]
    row = next(r for r in rows if r["name"] == session)
    assert row["effective_read"] is True
    assert row["effective_input"] is False

    # 4. Grant input.
    granted_input = client.post("/dashboard/api/session/grant-input", json={"name": session, "enabled": True})
    assert granted_input.status_code == 200
    assert granted_input.json() == {"session": session, "read_enabled": True, "input_enabled": True}

    detail2 = client.get(f"/dashboard/api/session?name={session}")
    assert detail2.json()["input_allowed"] is True

    # 5. Confirmed send, through the dashboard's own input route -- the
    # exact same guarded primitive (idempotency/pane-lock/submit
    # verification) every other send in this project uses.
    sent = client.post("/dashboard/api/session/input", json={"name": session, "text": "y", "press_enter": True})
    assert sent.status_code == 200
    body = sent.json()
    assert body["sent"] is True
    assert body["submit_status"] == "SUBMIT_CONFIRMED"
    time.sleep(0.3)
    tail = client.get(f"/dashboard/api/session?name={session}").json()["tail"]["output"]
    assert "GOT:y" in tail

    # 6. Revoke input: send is refused again, read still works.
    revoked_input = client.post("/dashboard/api/session/grant-input", json={"name": session, "enabled": False})
    assert revoked_input.status_code == 200
    assert revoked_input.json() == {"session": session, "read_enabled": True, "input_enabled": False}
    refused = client.post("/dashboard/api/session/input", json={"name": session, "text": "z"})
    assert refused.status_code == 403
    assert refused.json()["error"] == "GRANT_REQUIRED"
    still_readable = client.get(f"/dashboard/api/session?name={session}")
    assert still_readable.status_code == 200

    # 7. Revoke read: back to fully restricted.
    revoked_read = client.post("/dashboard/api/session/grant-read", json={"name": session, "enabled": False})
    assert revoked_read.status_code == 200
    assert revoked_read.json() == {"session": session, "read_enabled": False, "input_enabled": False}
    final = client.get(f"/dashboard/api/session?name={session}")
    assert final.status_code == 403
    assert final.json()["error"] == "READ_RESTRICTED"

    # Audit trail recorded every grant/revoke action.
    events = service.audit.list(limit=50, session=session)
    actions = [e["action"] for e in events]
    assert actions.count("grant_read") == 2
    assert actions.count("grant_input") == 2


# ---------------------------------------------------------------------------
# Unauthorized caller
# ---------------------------------------------------------------------------


def test_grant_routes_require_same_origin(tmp_path, tmux_session_factory):
    name = "newsession-noorigin"
    session = tmux_session_factory(name, "bash -lc 'sleep 20'")
    time.sleep(0.2)
    config = _config()
    from terminal_mcp.grants import SessionGrantStore

    service = TerminalService(config, grants=SessionGrantStore(tmp_path / "grants.db"))
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app())  # no Origin header

    response = client.post("/dashboard/api/session/grant-read", json={"name": session, "enabled": True})
    assert response.status_code == 403
    assert response.json()["error"] == "ORIGIN_NOT_ALLOWED"
    assert service.grants.get(session) is None  # never reached the store


def test_grant_routes_respect_mutations_disabled(tmp_path, tmux_session_factory):
    name = "newsession-mutdisabled"
    session = tmux_session_factory(name, "bash -lc 'sleep 20'")
    time.sleep(0.2)
    from terminal_mcp.config import DashboardConfig
    from terminal_mcp.grants import SessionGrantStore

    config = AppConfig(
        PermissionsConfig(True, True), ("test-*", "agent-*"), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
        dashboard=DashboardConfig(mutations_enabled=False),
    )
    service = TerminalService(config, grants=SessionGrantStore(tmp_path / "grants.db"))
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})

    response = client.post("/dashboard/api/session/grant-read", json={"name": session, "enabled": True})
    assert response.status_code == 403
    assert response.json()["error"] == "DASHBOARD_MUTATIONS_DISABLED"


# ---------------------------------------------------------------------------
# Stale / reused session identity
# ---------------------------------------------------------------------------


def test_input_grant_refuses_a_session_recreated_under_the_same_name(tmp_path, tmux_session_factory):
    name = "newsession-recreated"
    session = tmux_session_factory(name, "bash -lc 'sleep 30'")
    time.sleep(0.2)
    client, service = _client(_config(), grants_path=tmp_path / "grants.db")

    client.post("/dashboard/api/session/grant-read", json={"name": session, "enabled": True})
    granted = client.post("/dashboard/api/session/grant-input", json={"name": session, "enabled": True})
    assert granted.json()["input_enabled"] is True

    # The exact same session NAME, but a genuinely different tmux
    # session/pane underneath (kill + recreate) -- the pin captured at
    # grant time no longer matches.
    tmux_session_factory(name, "bash -lc 'sleep 30'")
    time.sleep(0.2)

    refused = client.post("/dashboard/api/session/input", json={"name": session, "text": "y"})
    assert refused.status_code == 403
    assert refused.json()["error"] == "IDENTITY_MISMATCH"

    # Read (not identity-pinned) still works -- only input is refused.
    assert client.get(f"/dashboard/api/session?name={session}").status_code == 200

    # An explicit re-grant accepts the new identity, exactly like
    # terminal_bind(replace=true) already does for bindings.
    regranted = client.post("/dashboard/api/session/grant-input", json={"name": session, "enabled": True})
    assert regranted.json()["input_enabled"] is True
    sent = client.post("/dashboard/api/session/input", json={"name": session, "text": "y"})
    assert sent.json().get("sent") is True


# ---------------------------------------------------------------------------
# Global input disabled
# ---------------------------------------------------------------------------


def test_input_grant_refused_when_global_terminal_input_is_disabled(tmp_path, tmux_session_factory):
    name = "newsession-globaloff"
    session = tmux_session_factory(name, "bash -lc 'sleep 20'")
    time.sleep(0.2)
    client, service = _client(_config(terminal_input=False), grants_path=tmp_path / "grants.db")

    client.post("/dashboard/api/session/grant-read", json={"name": session, "enabled": True})
    response = client.post("/dashboard/api/session/grant-input", json={"name": session, "enabled": True})
    assert response.status_code == 403
    assert response.json()["error"] == "INPUT_DISABLED"
    assert service.grants.get(session).input_enabled is False


# ---------------------------------------------------------------------------
# No whitelist/permission bypass
# ---------------------------------------------------------------------------


def test_grant_never_affects_a_different_whitelisted_session(tmp_path, tmux_session_factory):
    granted_name = "newsession-isolated"
    whitelisted = tmux_session_factory("test-still-normal", "bash -lc 'sleep 20'")
    tmux_session_factory(granted_name, "bash -lc 'sleep 20'")
    time.sleep(0.2)
    client, service = _client(_config(), grants_path=tmp_path / "grants.db")

    client.post("/dashboard/api/session/grant-read", json={"name": granted_name, "enabled": True})
    client.post("/dashboard/api/session/grant-input", json={"name": granted_name, "enabled": True})

    # The already-whitelisted session's own behavior is completely
    # unaffected -- still reachable through its normal, unchanged path.
    detail = client.get(f"/dashboard/api/session?name={whitelisted}")
    assert detail.status_code == 200
    assert detail.json()["input_allowed"] is True
    assert service.grants.get(whitelisted) is None  # never touched


def test_sensitive_named_session_can_never_be_granted(tmp_path, tmux_session_factory):
    name = "root-shell-demo"
    session = tmux_session_factory(name, "bash -lc 'sleep 20'")
    time.sleep(0.2)
    client, service = _client(_config(), grants_path=tmp_path / "grants.db")

    response = client.post("/dashboard/api/session/grant-read", json={"name": session, "enabled": True})
    assert response.status_code == 403
    assert response.json()["error"] == "SENSITIVE_SESSION_NOT_GRANTABLE"
    assert service.grants.get(session) is None


def test_input_grant_still_respects_denied_session_patterns(tmp_path, tmux_session_factory):
    name = "prod-shell-demo"
    session = tmux_session_factory(name, "bash -lc 'sleep 20'")
    time.sleep(0.2)
    client, service = _client(_config(), grants_path=tmp_path / "grants.db")

    read = client.post("/dashboard/api/session/grant-read", json={"name": session, "enabled": True})
    assert read.status_code == 200  # read isn't gated by input_policy's deny list

    response = client.post("/dashboard/api/session/grant-input", json={"name": session, "enabled": True})
    assert response.status_code == 403
    assert response.json()["error"] == "ACCESS_DENIED"
    assert service.grants.get(session).input_enabled is False


def test_raw_mcp_tool_surface_never_sees_a_dashboard_grant(tmp_path, tmux_session_factory):
    # The core security invariant of this whole feature: terminal_tail/
    # terminal_status/terminal_send_text (the MCP tool surface any
    # connected client -- Claude Code, ChatGPT via the tunnel -- can call)
    # must stay exactly as restrictive as before, regardless of any
    # dashboard grant.
    name = "newsession-mcp-isolation"
    session = tmux_session_factory(name, "bash -lc 'sleep 20'")
    time.sleep(0.2)
    client, service = _client(_config(), grants_path=tmp_path / "grants.db")

    client.post("/dashboard/api/session/grant-read", json={"name": session, "enabled": True})
    client.post("/dashboard/api/session/grant-input", json={"name": session, "enabled": True})

    assert service.terminal_tail(session)["error"] == "ACCESS_DENIED"
    assert service.terminal_status(session)["error"] == "ACCESS_DENIED"
    assert service.terminal_send_text(session, "y")["error"] == "ACCESS_DENIED"
    # terminal_list_sessions (the raw MCP tool, still whitelist-filtered)
    # never surfaces a granted-but-not-whitelisted session at all --
    # unlike dashboard_list_sessions, which is the dashboard-only,
    # deliberately unfiltered listing this whole feature adds.
    assert all(s["name"] != session for s in service.terminal_list_sessions()["sessions"])
    assert any(s["name"] == session for s in service.dashboard_list_sessions()["sessions"])
