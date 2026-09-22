"""The public read-only observer surface and its OAuth authorization server.

Two things are being defended here, and they are different:

  1. WHAT is reachable from the internet -- the tool surface. The first
     three tests pin it exactly, including an explicit denylist, because a
     tool leaking onto this surface is the one bug in this feature that
     turns a monitoring endpoint into a remote shell.
  2. WHETHER an unauthenticated caller can reach it at all -- the OAuth
     flow, exercised end to end over a real ASGI client rather than by
     calling provider methods directly, because the parts most likely to
     be wrong (PKCE, redirect matching, the 401 on the MCP route itself)
     live in the SDK's handlers, not in ours.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import AppConfig, PermissionsConfig, SessionAccessConfig
from terminal_mcp.controller import build_default_controller
from terminal_mcp.core import TerminalService
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.observer_app import (
    OBSERVER_TOOL_NAMES,
    build_observer_mcp,
    observer_actor,
    observer_asgi_app,
)
from terminal_mcp.observer_auth import (
    OBSERVE_SCOPE,
    AuthorizationCode,
    ObserverOAuthProvider,
    ObserverOAuthStore,
    PendingAuthorization,
    observer_auth_settings,
    register_observer_login,
)
from terminal_mcp.repo_service import build_repo_service
from terminal_mcp.webauth import WebAuthStore

PUBLIC_URL = "https://watch.example.net"
PASSWORD = "correct-horse-battery-staple"
CLIENT_REDIRECT = "https://claude.ai/api/mcp/auth_callback"


# -- fixtures ----------------------------------------------------------------


@pytest.fixture
def observer_config() -> AppConfig:
    return AppConfig(PermissionsConfig(True, False), ("test-*",), 50, 20,
                     session_access=SessionAccessConfig(default_read=True, default_input=True))


@pytest.fixture
def observer_server(observer_config):
    """The tool surface only -- no OAuth, which is what the surface tests want."""
    terminal = TerminalService(observer_config)
    controller = build_default_controller(terminal)
    repo = build_repo_service(terminal, controller)
    return build_observer_mcp(terminal, controller, repo)


@pytest.fixture
def oauth_store(tmp_path) -> ObserverOAuthStore:
    return ObserverOAuthStore(tmp_path / "observer-oauth.db")


@pytest.fixture
def webauth(tmp_path) -> WebAuthStore:
    store = WebAuthStore(tmp_path / "webauth.db")
    store.create_or_replace_user("dell", PASSWORD)
    return store


@pytest.fixture
def client(observer_config, oauth_store, webauth) -> TestClient:
    """The whole thing: read-only tools + OAuth endpoints + login page,
    served as the real ASGI app."""
    terminal = TerminalService(observer_config)
    controller = build_default_controller(terminal)
    repo = build_repo_service(terminal, controller)
    provider = ObserverOAuthProvider(oauth_store, public_base_url=PUBLIC_URL)
    server = build_observer_mcp(terminal, controller, repo,
                                auth_settings=observer_auth_settings(PUBLIC_URL),
                                auth_provider=provider)
    register_observer_login(server, provider=provider, webauth=webauth)
    # The REAL production app builder, transport-security settings and all --
    # see observer_asgi_app's docstring for why the tests must not
    # assemble their own.
    app = observer_asgi_app(server, public_url=PUBLIC_URL, port=8767)
    # Entered as a context manager on purpose: the streamable-HTTP session
    # manager starts its task group in the app lifespan, and without it
    # every /mcp request fails with "Task group is not initialized".
    with TestClient(app, base_url=PUBLIC_URL) as test_client:
        yield test_client


# -- 1. what is reachable -----------------------------------------------------


@pytest.mark.anyio
async def test_observer_exposes_exactly_the_declared_tools(observer_server):
    tools = await observer_server.list_tools()
    assert {tool.name for tool in tools} == set(OBSERVER_TOOL_NAMES)


@pytest.mark.anyio
async def test_observer_exposes_no_tool_that_can_change_anything(observer_server):
    """An explicit denylist, not a prefix rule.

    Named tools rather than a heuristic because the failure this catches is
    someone adding a tool to observer_app.py that *looks* like a read (it
    is called _status, or _list) and is not. If one of these ever appears
    on this surface, a stolen bearer token becomes a shell."""
    forbidden = {
        "terminal_send_text", "terminal_send_keys", "terminal_send_task", "terminal_send_bound",
        "terminal_turn", "terminal_create_session", "terminal_delete_session",
        "terminal_kill_session", "terminal_detach_session", "terminal_reopen_session",
        "terminal_recover_session", "terminal_enqueue_task", "terminal_queue_set",
        "terminal_queue_cancel", "terminal_emergency_stop", "terminal_emergency_resume",
        "terminal_worktree_cleanup", "terminal_worktree_sweep_run_once",
        "session_grant", "session_revoke", "session_set_permissions",
        "note_create", "note_delete", "work_control", "work_create",
        "supervisor_run_once", "supervisor2_execute_send",
    }
    names = {tool.name for tool in await observer_server.list_tools()}
    assert names & forbidden == set()
    # ...and the denylist is not stale: every name in it is a tool that
    # really does exist on the full surface. A typo here would silently
    # assert nothing.
    full = {tool.name for tool in await build_mcp(TerminalService(
        AppConfig(PermissionsConfig(True, False), ("test-*",), 50, 20))).list_tools()}
    assert forbidden <= full


@pytest.mark.anyio
async def test_observer_tools_are_not_bespoke_reimplementations(observer_server):
    """Every observer tool also exists on the full surface under the same
    name. Guards against this surface growing its own parallel
    implementation of a read, which is how the two would drift apart."""
    observer = {tool.name for tool in await observer_server.list_tools()}
    full = {tool.name for tool in await build_mcp(TerminalService(
        AppConfig(PermissionsConfig(True, False), ("test-*",), 50, 20))).list_tools()}
    assert observer <= full


def test_observer_actor_is_anonymous_outside_a_request():
    # No auth context (this is not inside a request) must not raise, and
    # must not claim an identity it does not have.
    assert observer_actor() == "observer:unknown"


# -- 2. the store's own guarantees ---------------------------------------------


def test_authorization_code_is_single_use(oauth_store):
    code = AuthorizationCode(
        code="abc", scopes=[OBSERVE_SCOPE], expires_at=time.time() + 300, client_id="c1",
        code_challenge="chal", redirect_uri=CLIENT_REDIRECT,
        redirect_uri_provided_explicitly=True, resource=None, subject="dell",
    )
    oauth_store.put_code(code)
    assert oauth_store.get_code("abc") is not None
    oauth_store.delete_code("abc")
    assert oauth_store.get_code("abc") is None


def test_expired_authorization_code_does_not_load(oauth_store):
    oauth_store.put_code(AuthorizationCode(
        code="stale", scopes=[OBSERVE_SCOPE], expires_at=time.time() - 1, client_id="c1",
        code_challenge="chal", redirect_uri=CLIENT_REDIRECT,
        redirect_uri_provided_explicitly=True, resource=None, subject="dell",
    ))
    assert oauth_store.get_code("stale") is None


def test_revoking_either_token_kills_its_sibling(oauth_store):
    now = int(time.time())
    oauth_store.put_token_pair(
        access_token="acc", refresh_token="ref", client_id="c1", scopes=(OBSERVE_SCOPE,),
        resource=None, subject="dell", access_expires_at=now + 3600,
        refresh_expires_at=now + 86400,
    )
    assert oauth_store.get_token("acc", "access") is not None
    # Revoke the REFRESH token; the access token must die with it, which is
    # what makes "revoke the connector" actually stop reads immediately.
    oauth_store.revoke_token("ref")
    assert oauth_store.get_token("acc", "access") is None
    assert oauth_store.get_token("ref", "refresh") is None


def test_tokens_are_not_stored_in_the_clear(oauth_store):
    now = int(time.time())
    oauth_store.put_token_pair(
        access_token="super-secret-access", refresh_token="super-secret-refresh",
        client_id="c1", scopes=(OBSERVE_SCOPE,), resource=None, subject="dell",
        access_expires_at=now + 3600, refresh_expires_at=now + 86400,
    )
    raw = oauth_store.path.read_bytes()
    assert b"super-secret-access" not in raw
    assert b"super-secret-refresh" not in raw


def test_revoke_all_for_subject_drops_every_grant(oauth_store):
    now = int(time.time())
    for n in range(3):
        oauth_store.put_token_pair(
            access_token=f"a{n}", refresh_token=f"r{n}", client_id=f"c{n}",
            scopes=(OBSERVE_SCOPE,), resource=None, subject="dell",
            access_expires_at=now + 3600, refresh_expires_at=now + 86400,
        )
    oauth_store.put_token_pair(
        access_token="other", refresh_token="other-r", client_id="c9",
        scopes=(OBSERVE_SCOPE,), resource=None, subject="someone-else",
        access_expires_at=now + 3600, refresh_expires_at=now + 86400,
    )
    assert oauth_store.revoke_all_for_subject("dell") == 6
    assert oauth_store.get_token("a0", "access") is None
    assert oauth_store.get_token("other", "access") is not None


def test_pending_authorization_is_consumed_once(oauth_store):
    pending = PendingAuthorization(
        request_id="req1", client_id="c1", redirect_uri=CLIENT_REDIRECT,
        redirect_uri_provided_explicitly=True, code_challenge="chal", state="st",
        scopes=(OBSERVE_SCOPE,), resource=None,
    )
    oauth_store.put_pending(pending)
    assert oauth_store.peek_pending("req1") is not None
    assert oauth_store.take_pending("req1") is not None
    assert oauth_store.take_pending("req1") is None


# -- 3. the OAuth flow, end to end ---------------------------------------------


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _register(client: TestClient) -> str:
    response = client.post("/register", json={
        "client_name": "Claude", "redirect_uris": [CLIENT_REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none",
    })
    assert response.status_code in (200, 201), response.text
    return response.json()["client_id"]


def _authorize(client: TestClient, client_id: str, challenge: str, state: str = "xyz") -> str:
    """Returns the `req` id the login page was handed."""
    response = client.get("/authorize", params={
        "client_id": client_id, "redirect_uri": CLIENT_REDIRECT, "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256",
        "state": state, "scope": OBSERVE_SCOPE,
    }, follow_redirects=False)
    assert response.status_code in (302, 303, 307), response.text
    location = response.headers["location"]
    assert location.startswith(f"{PUBLIC_URL}/observer/login?req="), location
    return parse_qs(urlparse(location).query)["req"][0]


def test_protected_resource_metadata_is_published(client):
    response = client.get("/.well-known/oauth-protected-resource")
    assert response.status_code == 200
    assert response.json()["resource"].rstrip("/") == PUBLIC_URL


def test_authorization_server_metadata_is_published(client):
    response = client.get("/.well-known/oauth-authorization-server")
    body = response.json()
    assert response.status_code == 200
    assert body["issuer"].rstrip("/") == PUBLIC_URL
    # Dynamic registration must be advertised: a hosted client has no other
    # way to obtain a client_id.
    assert body["registration_endpoint"].endswith("/register")
    assert "S256" in body["code_challenge_methods_supported"]


def test_login_page_names_the_app_and_says_read_only(client):
    client_id = _register(client)
    _, challenge = _pkce()
    req = _authorize(client, client_id, challenge)
    page = client.get("/observer/login", params={"req": req})
    assert page.status_code == 200
    assert "Claude" in page.text
    assert "chỉ đọc" in page.text


def test_login_page_refuses_an_unknown_request_id(client):
    page = client.get("/observer/login", params={"req": "not-a-real-request"})
    assert page.status_code == 400


def test_wrong_password_does_not_burn_the_connect_attempt(client):
    client_id = _register(client)
    verifier, challenge = _pkce()
    req = _authorize(client, client_id, challenge)

    bad = client.post("/observer/login", data={"req": req, "username": "dell", "password": "wrong"},
                      follow_redirects=False)
    assert bad.status_code == 401
    # The same req must still work -- a typo on a phone keyboard should not
    # force the user back to Claude to start over.
    good = client.post("/observer/login",
                       data={"req": req, "username": "dell", "password": PASSWORD},
                       follow_redirects=False)
    assert good.status_code == 303


def test_full_authorization_code_flow_yields_a_working_token(client):
    client_id = _register(client)
    verifier, challenge = _pkce()
    req = _authorize(client, client_id, challenge, state="state-123")

    login = client.post("/observer/login",
                        data={"req": req, "username": "dell", "password": PASSWORD},
                        follow_redirects=False)
    assert login.status_code == 303
    location = login.headers["location"]
    assert location.startswith(CLIENT_REDIRECT)
    query = parse_qs(urlparse(location).query)
    assert query["state"] == ["state-123"]
    code = query["code"][0]

    token_response = client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": CLIENT_REDIRECT, "code_verifier": verifier,
    })
    assert token_response.status_code == 200, token_response.text
    payload = token_response.json()
    assert payload["token_type"] == "Bearer"
    assert payload["scope"] == OBSERVE_SCOPE
    assert payload["refresh_token"]

    # The code is spent: replaying it must not mint a second grant.
    replay = client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": CLIENT_REDIRECT, "code_verifier": verifier,
    })
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


def test_token_exchange_rejects_a_wrong_pkce_verifier(client):
    client_id = _register(client)
    _, challenge = _pkce()
    req = _authorize(client, client_id, challenge)
    login = client.post("/observer/login",
                        data={"req": req, "username": "dell", "password": PASSWORD},
                        follow_redirects=False)
    code = parse_qs(urlparse(login.headers["location"]).query)["code"][0]

    response = client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": CLIENT_REDIRECT, "code_verifier": secrets.token_urlsafe(48),
    })
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_refresh_rotates_and_retires_the_old_token(client):
    client_id = _register(client)
    verifier, challenge = _pkce()
    req = _authorize(client, client_id, challenge)
    login = client.post("/observer/login",
                        data={"req": req, "username": "dell", "password": PASSWORD},
                        follow_redirects=False)
    code = parse_qs(urlparse(login.headers["location"]).query)["code"][0]
    first = client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": CLIENT_REDIRECT, "code_verifier": verifier,
    }).json()

    second = client.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": first["refresh_token"],
        "client_id": client_id,
    })
    assert second.status_code == 200, second.text
    assert second.json()["access_token"] != first["access_token"]

    replay = client.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": first["refresh_token"],
        "client_id": client_id,
    })
    assert replay.status_code == 400


# -- 4. the MCP route itself is actually protected ------------------------------


def _mcp_headers(token: str | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _obtain_token(client: TestClient) -> str:
    client_id = _register(client)
    verifier, challenge = _pkce()
    req = _authorize(client, client_id, challenge)
    login = client.post("/observer/login",
                        data={"req": req, "username": "dell", "password": PASSWORD},
                        follow_redirects=False)
    code = parse_qs(urlparse(login.headers["location"]).query)["code"][0]
    return client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "redirect_uri": CLIENT_REDIRECT, "code_verifier": verifier,
    }).json()["access_token"]


_INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "test", "version": "0"}},
}


def test_mcp_route_refuses_an_unauthenticated_caller(client):
    response = client.post("/mcp", headers=_mcp_headers(), json=_INITIALIZE)
    assert response.status_code == 401
    # RFC 9728: the 401 must point at the metadata that tells a client where
    # to authenticate, otherwise a hosted client cannot start the flow.
    assert "resource_metadata" in response.headers.get("www-authenticate", "")


def test_mcp_route_refuses_a_forged_token(client):
    response = client.post("/mcp", headers=_mcp_headers("not-a-real-token"), json=_INITIALIZE)
    assert response.status_code == 401


def test_mcp_route_refuses_a_revoked_token(client, oauth_store):
    token = _obtain_token(client)
    assert client.post("/mcp", headers=_mcp_headers(token), json=_INITIALIZE).status_code == 200
    oauth_store.revoke_all_for_subject("dell")
    assert client.post("/mcp", headers=_mcp_headers(token), json=_INITIALIZE).status_code == 401


def test_authenticated_caller_sees_exactly_the_read_only_tools(client):
    token = _obtain_token(client)
    initialize = client.post("/mcp", headers=_mcp_headers(token), json=_INITIALIZE)
    assert initialize.status_code == 200, initialize.text
    session_id = initialize.headers.get("mcp-session-id")

    headers = _mcp_headers(token)
    if session_id:
        headers["mcp-session-id"] = session_id
    client.post("/mcp", headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    listing = client.post("/mcp", headers=headers,
                          json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listing.status_code == 200, listing.text
    body = listing.json() if listing.headers["content-type"].startswith("application/json") \
        else json.loads(listing.text.split("data: ", 1)[1])
    names = {tool["name"] for tool in body["result"]["tools"]}
    assert names == set(OBSERVER_TOOL_NAMES)


# -- 5. the CSP that the OAuth redirect depends on -------------------------------


def test_login_page_csp_permits_the_redirect_back_to_the_client(client):
    """Regression guard for a browser-only failure.

    Chrome and Firefox check `form-action` against the REDIRECT target of a
    form submission, not just its immediate target. The global policy in
    logging_setup.py is `form-action 'self'`, which would block the 303
    back to the OAuth client and break the last hop of the flow -- in a
    browser only. curl, urllib and this test client all happily follow it,
    so nothing else in this file would notice."""
    client_id = _register(client)
    _, challenge = _pkce()
    req = _authorize(client, client_id, challenge)
    page = client.get("/observer/login", params={"req": req})

    policies = [value for name, value in page.headers.multi_items()
                if name.lower() == "content-security-policy"]
    # Exactly one: a browser given two enforces their INTERSECTION, so a
    # second (global) header would re-impose form-action 'self'.
    assert len(policies) == 1, policies
    assert "form-action 'self' https://claude.ai" in policies[0]
    assert "frame-ancestors 'none'" in policies[0]


def test_non_login_responses_still_get_the_global_security_headers(client):
    response = client.get("/.well-known/oauth-authorization-server")
    policies = [value for name, value in response.headers.multi_items()
                if name.lower() == "content-security-policy"]
    assert len(policies) == 1
    assert "form-action 'self'" in policies[0]
    assert response.headers["x-content-type-options"] == "nosniff"
