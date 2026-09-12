"""Application-layer authentication on the Notes HTTP surface.

The gap this closes: Cloudflare Access at the EDGE says nothing to this
process about a request that actually arrives, because cloudflared connects
over loopback -- tunnel traffic and local traffic are indistinguishable once
here. So notes hold whatever the operator kept, reachable by anything that
can reach the port (a tailnet peer included).

No new mechanism is introduced: these routes accept the repo's two existing
identities -- a webauth.py session cookie, or a verified cf_access.py
assertion -- and nothing else.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import (
    AppConfig, DashboardConfig, InputPolicyConfig, NotesConfig, PermissionsConfig,
)
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.notes_service import NotesService
from terminal_mcp.notes_store import NotesStore
from terminal_mcp.webauth import SESSION_COOKIE_NAME, WebAuthStore
from tests.fixtures.notes_images import png_bytes

SAME_ORIGIN = {"Origin": "http://testserver"}

READ_ROUTES = ("/dashboard/api/notes", "/dashboard/api/notes/facets",
               "/dashboard/api/notes/note?id=note_x",
               "/dashboard/api/notes/attachment?id=att_x")
WRITE_ROUTES = ("/dashboard/api/notes/create", "/dashboard/api/notes/update",
                "/dashboard/api/notes/mark-applied", "/dashboard/api/notes/delete",
                "/dashboard/api/notes/restore", "/dashboard/api/notes/attachment/remove")


def make_config(notes: NotesConfig | None = None, **dashboard_kwargs) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("*",)),
        dashboard=DashboardConfig(**dashboard_kwargs) if dashboard_kwargs else DashboardConfig(),
        notes=notes or NotesConfig(),
    )


def build(tmp_path, *, config=None, with_webauth=True):
    config = config or make_config()
    notes = NotesService(NotesStore(tmp_path / "notes.db"),
                         attachments_dir=tmp_path / "attachments")
    webauth = WebAuthStore(tmp_path / "webauth.db") if with_webauth else None
    terminal = TerminalService(config)
    server = build_mcp(terminal, notes=notes)
    register_dashboard(server, terminal, notes=notes, webauth=webauth)
    return TestClient(server.streamable_http_app()), notes, webauth


@pytest.fixture
def rig(tmp_path):
    client, notes, webauth = build(tmp_path)
    webauth.create_or_replace_user("operator", "correct horse battery staple")
    return client, notes, webauth


def login(client, webauth, username="operator"):
    token = webauth.create_session(username)
    client.cookies.set(SESSION_COOKIE_NAME, token)
    return token


# -- unauthenticated is refused everywhere --------------------------------

def test_the_page_redirects_a_browser_to_the_login_form(rig):
    client, _, _ = rig
    response = client.get("/dashboard/notes", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.parametrize("route", READ_ROUTES)
def test_every_read_route_refuses_an_unauthenticated_caller(rig, route):
    client, _, _ = rig
    response = client.get(route)
    assert response.status_code == 401
    assert response.json()["error"] == "LOGIN_REQUIRED"


@pytest.mark.parametrize("route", WRITE_ROUTES)
def test_every_write_route_refuses_an_unauthenticated_caller(rig, route):
    client, _, _ = rig
    response = client.post(route, json={"id": "note_x"}, headers=SAME_ORIGIN)
    assert response.status_code == 401
    assert response.json()["error"] == "LOGIN_REQUIRED"


def test_the_upload_route_refuses_an_unauthenticated_caller(rig):
    client, _, _ = rig
    response = client.post("/dashboard/api/notes/attachment/upload",
                           data={"note_id": "note_x"},
                           files={"file": ("a.png", png_bytes(), "image/png")},
                           headers=SAME_ORIGIN)
    assert response.status_code == 401


def test_attachment_bytes_are_not_served_to_an_unauthenticated_caller(rig):
    """The one that would leak content rather than just metadata: a real,
    existing attachment must 401, not 200, without a session."""
    client, notes, webauth = rig
    note = notes.create(title="Private idea", summary="chỉ mình tôi đọc")
    record = notes.add_attachment(note["id"], filename="secret.png", data=png_bytes(64, 48))
    unauthenticated = client.get(record["url"])
    assert unauthenticated.status_code == 401
    assert b"PNG" not in unauthenticated.content
    login(client, webauth)
    authenticated = client.get(record["url"])
    assert authenticated.status_code == 200
    assert authenticated.content.startswith(b"\x89PNG")


def test_note_content_does_not_leak_in_the_refusal_body(rig):
    client, notes, _ = rig
    notes.create(title="Secret project Fenrir", summary="zzsecret-payload")
    for route in ("/dashboard/api/notes", "/dashboard/api/notes?q=Fenrir"):
        response = client.get(route)
        assert response.status_code == 401
        assert b"Fenrir" not in response.content
        assert b"zzsecret-payload" not in response.content


# -- a webauth session is accepted ----------------------------------------

def test_a_webauth_session_unlocks_the_page_and_the_api(rig):
    client, _, webauth = rig
    login(client, webauth)
    assert client.get("/dashboard/notes").status_code == 200
    assert client.get("/dashboard/api/notes").status_code == 200
    created = client.post("/dashboard/api/notes/create",
                          json={"title": "Với session thì được"}, headers=SAME_ORIGIN)
    assert created.status_code == 200
    assert created.json()["title"] == "Với session thì được"


def test_the_full_flow_works_under_one_session(rig):
    client, _, webauth = rig
    login(client, webauth)
    note = client.post("/dashboard/api/notes/create",
                       json={"title": "Có ảnh", "summary": "zzflow"}, headers=SAME_ORIGIN).json()
    upload = client.post("/dashboard/api/notes/attachment/upload",
                         data={"note_id": note["id"]},
                         files={"file": ("a.png", png_bytes(120, 80), "image/png")},
                         headers=SAME_ORIGIN)
    assert upload.status_code == 200
    assert client.get(upload.json()["url"]).status_code == 200
    assert client.get("/dashboard/api/notes?q=zzflow").json()["total"] == 1
    assert client.post("/dashboard/api/notes/mark-applied", json={"id": note["id"]},
                       headers=SAME_ORIGIN).json()["status"] == "applied"


# -- session invalidation is real, not cosmetic ---------------------------

def test_a_forged_or_unknown_cookie_is_refused(rig):
    client, _, _ = rig
    client.cookies.set(SESSION_COOKIE_NAME, "not-a-real-token")
    assert client.get("/dashboard/api/notes").status_code == 401


def test_logout_style_session_destruction_locks_the_notes_surface_again(rig):
    client, _, webauth = rig
    token = login(client, webauth)
    assert client.get("/dashboard/api/notes").status_code == 200
    webauth.destroy_session(token)
    assert client.get("/dashboard/api/notes").status_code == 401


def test_destroying_every_session_for_a_user_also_locks_it(rig):
    client, _, webauth = rig
    login(client, webauth)
    assert client.get("/dashboard/api/notes").status_code == 200
    webauth.destroy_all_sessions_for("operator")
    assert client.get("/dashboard/api/notes").status_code == 401


def test_an_expired_session_is_refused(rig):
    from datetime import timedelta
    client, _, webauth = rig
    token = webauth.create_session("operator", ttl=timedelta(seconds=-1))
    client.cookies.set(SESSION_COOKIE_NAME, token)
    assert client.get("/dashboard/api/notes").status_code == 401


def test_a_user_forced_to_change_their_password_cannot_read_notes(tmp_path):
    client, _, webauth = build(tmp_path)
    webauth.create_or_replace_user("fresh", "temporary-one-time", must_change_password=True)
    client.cookies.set(SESSION_COOKIE_NAME, webauth.create_session("fresh"))
    response = client.get("/dashboard/api/notes")
    assert response.status_code == 403
    assert response.json()["error"] == "PASSWORD_CHANGE_REQUIRED"
    # And the page does NOT bounce them to /login (they are logged in) --
    # it tells them what is actually wrong.
    page = client.get("/dashboard/notes", follow_redirects=False)
    assert page.status_code == 403


# -- Cloudflare Access as the other accepted identity ---------------------

def test_configured_cloudflare_access_satisfies_the_guard(tmp_path, monkeypatch):
    """With Access configured app-side, a verified assertion is an identity in
    its own right -- an operator using the Access path needs no password."""
    import terminal_mcp.dashboard as dashboard_module
    from terminal_mcp.cf_access import AccessIdentity

    config = make_config(cloudflare_access_team_domain="team.cloudflareaccess.com",
                         cloudflare_access_audience="aud-123")
    client, _, _ = build(tmp_path, config=config, with_webauth=False)
    verified = AccessIdentity(email="operator@example.com", subject="sub-1", raw_claims={})
    monkeypatch.setattr(dashboard_module, "verify_access_assertion",
                        lambda token, **kwargs: verified if token == "good-jwt" else None)
    assert client.get("/dashboard/api/notes",
                      headers={"cf-access-jwt-assertion": "good-jwt"}).status_code == 200
    # An unverified assertion is stopped by the pre-existing _read_guard.
    assert client.get("/dashboard/api/notes",
                      headers={"cf-access-jwt-assertion": "bad-jwt"}).status_code == 403
    # And no assertion at all is refused too -- edge-only is not enough.
    assert client.get("/dashboard/api/notes").status_code == 403


def test_edge_only_access_is_not_enough(tmp_path):
    """The actual deployed shape: Access in front of the tunnel, nothing
    configured app-side. Traffic arrives over loopback looking local, so the
    app itself must still demand a session."""
    client, _, _ = build(tmp_path)
    assert client.get("/dashboard/api/notes").status_code == 401
    # Spoofing the header does nothing when Access is not configured.
    assert client.get("/dashboard/api/notes",
                      headers={"cf-access-jwt-assertion": "anything"}).status_code == 401


# -- the documented opt-out ----------------------------------------------

def test_require_auth_false_restores_the_previous_open_behaviour(tmp_path):
    client, _, _ = build(tmp_path, config=make_config(NotesConfig(require_auth=False)))
    assert client.get("/dashboard/notes").status_code == 200
    assert client.get("/dashboard/api/notes").status_code == 200
    assert client.post("/dashboard/api/notes/create", json={"title": "open"},
                       headers=SAME_ORIGIN).status_code == 200


def test_with_no_webauth_store_wired_the_surface_fails_closed(tmp_path):
    """A deployment that forgets to pass the store must LOCK, never open."""
    client, _, _ = build(tmp_path, with_webauth=False)
    assert client.get("/dashboard/api/notes").status_code == 401
    assert client.get("/dashboard/notes", follow_redirects=False).status_code == 303


# -- the guards still compose with the pre-existing ones -------------------

def test_csrf_is_still_enforced_for_a_logged_in_caller(rig):
    client, _, webauth = rig
    login(client, webauth)
    response = client.post("/dashboard/api/notes/create", json={"title": "x"},
                           headers={"Origin": "http://evil.test"})
    assert response.status_code == 403
    assert response.json()["error"] == "ORIGIN_NOT_ALLOWED"


def test_mutations_disabled_still_wins_over_a_valid_session(tmp_path):
    client, _, webauth = build(tmp_path, config=make_config(mutations_enabled=False))
    webauth.create_or_replace_user("operator", "pw")
    login(client, webauth)
    response = client.post("/dashboard/api/notes/create", json={"title": "x"},
                           headers=SAME_ORIGIN)
    assert response.status_code == 403
    assert response.json()["error"] == "DASHBOARD_MUTATIONS_DISABLED"


def test_notes_disabled_still_wins_over_a_valid_session(tmp_path):
    config = make_config(NotesConfig(enabled=False))
    terminal = TerminalService(config)
    server = build_mcp(terminal, notes=None)
    webauth = WebAuthStore(tmp_path / "webauth.db")
    webauth.create_or_replace_user("operator", "pw")
    register_dashboard(server, terminal, notes=None, webauth=webauth)
    client = TestClient(server.streamable_http_app())
    client.cookies.set(SESSION_COOKIE_NAME, webauth.create_session("operator"))
    assert client.get("/dashboard/api/notes").status_code == 503


def test_other_dashboard_routes_are_not_changed_by_this(rig):
    """This hardening must not silently start demanding a session on the
    pre-existing /dashboard/* routes -- it adds a boundary to the Notes
    surface only."""
    client, _, _ = rig
    assert client.get("/dashboard").status_code == 200
    assert client.get("/dashboard/api/sessions").status_code == 200
    assert client.get("/dashboard/backlog").status_code == 200
