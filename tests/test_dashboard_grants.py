"""Dashboard per-session read/input grants: an operator can explicitly
authorize a newly-discovered, non-whitelisted tmux session from the
dashboard itself -- read first, then input, both revocable, both
identity-pinned the same way bindings already are.

GRANTING/REVOKING stays dashboard-only (no MCP tool wrapper anywhere).
DISCOVERY is shared: terminal_list_sessions (the raw MCP tool surface --
Claude Code, ChatGPT via the tunnel) shows the full tmux inventory plus
each session's grant/capability metadata too, same as
dashboard_list_sessions. The actual content/control tools (terminal_tail/
terminal_status/terminal_send_text/terminal_bind) share the exact same
canonical authorization decision this listing reports (core.py's
_read_authorized/_input_authorized -- see the P0 HOTFIX commit) -- a
grant that widens discovery's read_allowed/input_allowed also widens what
these tools actually do; a session with no grant and no static whitelist
entry is still refused everywhere. See test_dashboard_grant_widens_
discovery_and_the_plain_mcp_tools_consistently and the MCP-discovery
section below, plus test_p0_grant_authorization_hotfix.py for the full
regression suite for the live bug this fixed (discovery reporting
read_allowed/input_allowed=true while the plain tools still returned
ACCESS_DENIED for a grant-only session).
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
    # Routed through controller.terminal_grant_session_read (multi-node
    # grant-routing fix) -- additive node_id/node_name now included,
    # exactly like every other routed lifecycle response (create/kill/
    # reopen) already carries; never a breaking shape change.
    body = granted_read.json()
    assert body["session"] == session and body["read_enabled"] is True and body["input_enabled"] is False
    assert body["node_id"] == "local"

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
    body = granted_input.json()
    assert body["session"] == session and body["read_enabled"] is True and body["input_enabled"] is True

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
    body = revoked_input.json()
    assert body["session"] == session and body["read_enabled"] is True and body["input_enabled"] is False
    refused = client.post("/dashboard/api/session/input", json={"name": session, "text": "z"})
    assert refused.status_code == 403
    assert refused.json()["error"] == "GRANT_REQUIRED"
    still_readable = client.get(f"/dashboard/api/session?name={session}")
    assert still_readable.status_code == 200

    # 7. Revoke read: back to fully restricted.
    revoked_read = client.post("/dashboard/api/session/grant-read", json={"name": session, "enabled": False})
    assert revoked_read.status_code == 200
    body = revoked_read.json()
    assert body["session"] == session and body["read_enabled"] is False and body["input_enabled"] is False
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


def test_dashboard_grant_widens_discovery_and_the_plain_mcp_tools_consistently(tmp_path, tmux_session_factory):
    # P0 HOTFIX: terminal_list_sessions and the actual read/input tools
    # (terminal_tail/terminal_status/terminal_send_text) now share one
    # canonical authorization decision (_read_authorized/_input_authorized,
    # core.py) -- a grant that terminal_list_sessions reports as
    # read_allowed/input_allowed=true must ALSO make terminal_tail/
    # terminal_status/terminal_send_text actually succeed, not just the
    # dashboard-only *_granted methods. This is the exact live bug this
    # hotfix fixes: discovery previously reported true while the plain
    # tools still returned ACCESS_DENIED. Discovery alone (no grant yet)
    # is still never access -- that invariant is unchanged, and is what
    # the "before any grant" half of this test proves.
    name = "newsession-mcp-isolation"
    session = tmux_session_factory(name, "bash -lc 'sleep 20'")
    time.sleep(0.2)
    client, service = _client(_config(), grants_path=tmp_path / "grants.db")

    # Before any grant: discoverable (in both listings), but every
    # capability field says no, and every tool refuses it.
    row_before = next(s for s in service.terminal_list_sessions()["sessions"] if s["name"] == session)
    assert row_before == {
        "name": session, "allowed": False, "attached": False, "windows": 1,
        "created": row_before["created"], "activity": row_before["activity"],
        "read_allowed": False, "read_granted": False,
        "input_allowed": False, "input_granted": False,
        "effective_read": False, "effective_input": False,
        "input_denied_reason": None,  # no grant at all yet -- nothing specific to explain
    }
    assert service.terminal_tail(session)["error"] == "ACCESS_DENIED"
    assert service.terminal_status(session)["error"] == "ACCESS_DENIED"
    assert service.terminal_capture(session)["error"] == "ACCESS_DENIED"
    assert service.terminal_send_text(session, "y")["error"] == "ACCESS_DENIED"

    client.post("/dashboard/api/session/grant-read", json={"name": session, "enabled": True})
    client.post("/dashboard/api/session/grant-input", json={"name": session, "enabled": True})

    # After granting: terminal_list_sessions reflects the grant, AND the
    # plain tools now actually succeed -- consistent, not contradictory.
    assert "error" not in service.terminal_tail(session)
    assert "error" not in service.terminal_status(session)
    assert "error" not in service.terminal_capture(session)
    sent = service.terminal_send_text(session, "y", dry_run=True)
    assert sent.get("would_send") is True

    row_after = next(s for s in service.terminal_list_sessions()["sessions"] if s["name"] == session)
    assert row_after["allowed"] is False  # static whitelist result itself never changes
    assert row_after["read_allowed"] is True
    assert row_after["read_granted"] is True
    assert row_after["input_allowed"] is True
    assert row_after["input_granted"] is True
    assert row_after["effective_read"] is True
    assert row_after["effective_input"] is True
    # The dashboard-only *_granted methods still work too (same canonical
    # decision underneath, just a different entry point/error shape).
    assert "error" not in service.terminal_tail_granted(session)
    assert service.terminal_send_text_granted(session, "y").get("sent") is True

    assert any(s["name"] == session for s in service.dashboard_list_sessions()["sessions"])


# ---------------------------------------------------------------------------
# MCP discovery (terminal_list_sessions): ChatGPT/Claude Code and the
# dashboard now discover the same full tmux inventory + capability
# metadata; only content/control access stays gated as before.
# ---------------------------------------------------------------------------


def test_terminal_list_sessions_shows_full_inventory_not_just_whitelisted(tmp_path, tmux_session_factory):
    whitelisted = tmux_session_factory("test-full-inventory-a", "bash -lc 'sleep 20'")
    other = tmux_session_factory("newsession-full-inventory-b", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    from terminal_mcp.grants import SessionGrantStore

    service = TerminalService(_config(), grants=SessionGrantStore(tmp_path / "grants.db"))
    names = {row["name"] for row in service.terminal_list_sessions()["sessions"]}
    assert whitelisted in names
    assert other in names  # discoverable even though not whitelisted and never granted


def test_terminal_list_sessions_shows_a_promptflow_like_granted_session(tmp_path, tmux_session_factory):
    # Mirrors the real promptflow scenario: a session outside the static
    # whitelist, explicitly granted read+input from the dashboard, must
    # be fully visible -- as capability metadata, never content -- to an
    # MCP client's terminal_list_sessions call, with no service restart
    # anywhere in this test (SessionGrantStore reads live off sqlite).
    name = "promptflow-like-demo"
    session = tmux_session_factory(name, "bash -lc 'sleep 20'")
    time.sleep(0.2)
    from terminal_mcp.grants import SessionGrantStore

    service = TerminalService(_config(), grants=SessionGrantStore(tmp_path / "grants.db"))

    row = next(r for r in service.terminal_list_sessions()["sessions"] if r["name"] == session)
    assert row["allowed"] is False
    assert row["read_allowed"] is False and row["input_allowed"] is False

    service.grant_session_read(session, True, granted_by="test-operator")
    service.grant_session_input(session, True, granted_by="test-operator")

    row = next(r for r in service.terminal_list_sessions()["sessions"] if r["name"] == session)
    assert row["allowed"] is False  # still not statically whitelisted
    assert row["read_allowed"] is True and row["read_granted"] is True
    assert row["input_allowed"] is True and row["input_granted"] is True
    assert "output" not in row and "last_output" not in row  # discovery, never content


def test_ungranted_session_discoverable_but_tail_status_input_denied(tmp_path, tmux_session_factory):
    name = "newsession-ungranted-mcp"
    session = tmux_session_factory(name, "bash -lc 'sleep 20'")
    time.sleep(0.2)
    from terminal_mcp.grants import SessionGrantStore

    service = TerminalService(_config(), grants=SessionGrantStore(tmp_path / "grants.db"))

    assert any(r["name"] == session for r in service.terminal_list_sessions()["sessions"])
    assert service.terminal_tail(session)["error"] == "ACCESS_DENIED"
    assert service.terminal_status(session)["error"] == "ACCESS_DENIED"
    assert service.terminal_send_text(session, "y")["error"] == "ACCESS_DENIED"
    # The *_granted methods refuse it too -- there is no grant at all,
    # not even a read-only one.
    assert service.terminal_tail_granted(session)["error"] == "READ_RESTRICTED"
    assert service.terminal_status_granted(session)["error"] == "READ_RESTRICTED"
    assert service.terminal_send_text_granted(session, "y")["error"] == "GRANT_REQUIRED"


def test_grant_and_revoke_reflected_immediately_in_list_metadata_no_restart(tmp_path, tmux_session_factory):
    # "No restart needed" is a structural property, not something to
    # mock: grants.list()/get() are plain sqlite reads with no cache
    # layer anywhere, so this is exercised here simply by calling
    # terminal_list_sessions() -- a fresh, independent TerminalService
    # instance -- again after each grant/revoke.
    name = "newsession-immediate-reflect"
    session = tmux_session_factory(name, "bash -lc 'sleep 20'")
    time.sleep(0.2)
    from terminal_mcp.grants import SessionGrantStore

    grants_path = tmp_path / "grants.db"

    def row():
        service = TerminalService(_config(), grants=SessionGrantStore(grants_path))
        return next(r for r in service.terminal_list_sessions()["sessions"] if r["name"] == session)

    assert row()["read_allowed"] is False

    granter = TerminalService(_config(), grants=SessionGrantStore(grants_path))
    granter.grant_session_read(session, True, granted_by="test-operator")
    assert row()["read_allowed"] is True  # a DIFFERENT service instance sees it immediately

    granter.grant_session_input(session, True, granted_by="test-operator")
    assert row()["input_allowed"] is True

    granter.grant_session_input(session, False, granted_by="test-operator")
    assert row()["input_allowed"] is False
    assert row()["read_allowed"] is True  # revoking input alone leaves read intact

    granter.grant_session_read(session, False, granted_by="test-operator")
    assert row()["read_allowed"] is False
    assert row()["input_allowed"] is False  # revoking read also revoked input


def test_terminal_bind_accepts_a_granted_session_denies_an_ungranted_one(tmp_path, tmux_session_factory):
    # P0 HOTFIX requirement #8: terminal_bind must accept a session whose
    # required effective permissions are granted (same canonical
    # _bind_authorized decision terminal_status/etc. now use), while an
    # otherwise-identical session with NO grant and no static whitelist
    # entry is still refused -- discovery showing more sessions never
    # makes a truly unauthorized target bindable; only an actual grant
    # (or the static whitelist, unchanged) does.
    from terminal_mcp.grants import SessionGrantStore

    granted = "newsession-now-bindable"
    ungranted = "newsession-still-not-bindable"
    granted_session = tmux_session_factory(granted, "bash -lc 'sleep 20'")
    ungranted_session = tmux_session_factory(ungranted, "bash -lc 'sleep 20'")
    time.sleep(0.2)
    service = TerminalService(_config(), grants=SessionGrantStore(tmp_path / "grants.db"))
    service.grant_session_read(granted_session, True, granted_by="test-operator")
    service.grant_session_input(granted_session, True, granted_by="test-operator")

    assert service.terminal_bind("still-refused", ungranted_session)["error"] == "ACCESS_DENIED"

    bound = service.terminal_bind("now-works", granted_session, input_enabled=True)
    assert "error" not in bound
    assert bound["allowed"] is True  # bind-target authorization (canonical), not the raw static whitelist
    # Existing pinning/remap protections are untouched: a second bind to
    # the same name without replace=True is still refused.
    assert service.terminal_bind("now-works", granted_session)["error"] == "BINDING_EXISTS"
    service.terminal_unbind("now-works")

    whitelisted = tmux_session_factory("test-bindable", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    bound = service.terminal_bind("still-works", whitelisted)
    assert "error" not in bound
    service.terminal_unbind("still-works")
