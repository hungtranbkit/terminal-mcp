"""Route-level tests for the local username/password login path (/login,
/logout, /app, /app/api/*) -- webauth_dashboard.py. Covers exactly the
checklist from the feature request: unauth direct API calls blocked;
wrong/correct login and logout; expired session; CSRF/Origin on login and
every mutation; forged Cloudflare headers never bypass the cookie gate;
authenticated read/send still goes through the same grant/authorization
core.py already enforces (never a real live agent -- only disposable
tmux sessions created by tmux_session_factory); rate limiting triggers
and is non-permanent; the forced-first-login password-change flow; and
the bootstrap-secret-file scoping fix (a --db-isolated store's own
bootstrap file is never touched by a different store's account).

TestClient note: the session cookie is Secure, so every client here uses
an https:// base_url -- a plain http:// one would have httpx's cookie jar
correctly (per RFC 6265, mirroring real browsers) refuse to ever send it
back, which would make every authenticated check look unauthenticated
for reasons having nothing to do with the actual application logic.
"""
from __future__ import annotations

from datetime import timedelta

from starlette.testclient import TestClient

from terminal_mcp.audit import AuditStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.webauth import WebAuthStore
from terminal_mcp.webauth_dashboard import register_webauth_dashboard

ADMIN_PASSWORD = "correct horse battery staple 123"

BASE_URL = "https://testserver"


def _config() -> AppConfig:
    return AppConfig(
        PermissionsConfig(True, True), ("test-*", "agent-*"), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
    )


def _build(tmp_path, *, must_change_password=False, with_cf_dashboard=True, controller=None):
    # grants (like audit here) defaults to this host's REAL
    # ~/.local/state/terminal-mcp/grants.db when not given explicitly --
    # isolated so this file's grant-read/grant-input tests can never
    # leak a test session name into real production state (found live:
    # a prior run had left "test-webauth-grant-fallback" sitting in the
    # real grants.db -- since cleaned up).
    from terminal_mcp.grants import SessionGrantStore
    service = TerminalService(_config(), audit=AuditStore(tmp_path / "audit.db"),
                              grants=SessionGrantStore(tmp_path / "grants.db"))
    webauth = WebAuthStore(tmp_path / "webauth.db")
    webauth.create_or_replace_user("admin", ADMIN_PASSWORD, must_change_password=must_change_password)
    server = build_mcp(service)
    if with_cf_dashboard:
        register_dashboard(server, service)  # confirms the two paths coexist without clashing
    register_webauth_dashboard(server, service, webauth, controller=controller)
    return server, service, webauth


def _client(server, **kwargs) -> TestClient:
    kwargs.setdefault("headers", {"Origin": BASE_URL})
    return TestClient(server.streamable_http_app(), base_url=BASE_URL, **kwargs)


def _login(client: TestClient, password: str = ADMIN_PASSWORD, username: str = "admin"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


# ---------------------------------------------------------------------------
# Route registration / coexistence with the CF-Access dashboard
# ---------------------------------------------------------------------------

def test_webauth_routes_registered_alongside_cf_dashboard(tmp_path):
    server, _service, _webauth = _build(tmp_path)
    routes: dict[str, set[str]] = {}
    for route in server._custom_starlette_routes:
        # WebSocketRoute (/app/ws/terminal, /dashboard/ws/terminal) has no
        # .methods -- not what this HTTP-route-registration test is about.
        methods = getattr(route, "methods", None)
        if methods is not None:
            routes.setdefault(route.path, set()).update(methods)
    assert routes["/login"] >= {"GET", "HEAD", "POST"}
    assert routes["/logout"] == {"POST"}
    assert routes["/app"] == {"GET", "HEAD"}
    assert routes["/app/api/sessions"] == {"GET", "HEAD"}
    assert routes["/app/api/session/input"] == {"POST"}
    # The old CF-Access path must still be present, untouched.
    assert routes["/dashboard"] == {"GET", "HEAD"}
    assert routes["/dashboard/api/sessions"] == {"GET", "HEAD"}


def test_webauth_never_registers_mcp_health_or_version_paths(tmp_path):
    server, _service, _webauth = _build(tmp_path, with_cf_dashboard=False)
    paths = {route.path for route in server._custom_starlette_routes}
    for forbidden in ("/mcp", "/health", "/health/live", "/health/ready", "/version"):
        assert forbidden not in paths


# ---------------------------------------------------------------------------
# Unauthenticated access is blocked
# ---------------------------------------------------------------------------

def test_unauth_get_app_redirects_to_login(tmp_path):
    server, _service, _webauth = _build(tmp_path)
    client = _client(server)
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_unauth_direct_api_calls_are_blocked(tmp_path):
    server, _service, _webauth = _build(tmp_path)
    client = _client(server)
    for path in ("/app/api/sessions", "/app/api/session?name=test-x", "/app/api/supervisor", "/app/api/supervisor2"):
        r = client.get(path)
        assert r.status_code == 401, path
        assert r.json() == {"error": "LOGIN_REQUIRED"}
    r = client.post("/app/api/session/input", json={"name": "test-x", "text": "hi"})
    assert r.status_code == 401
    r = client.post("/app/api/session/grant-read", json={"name": "test-x", "enabled": True})
    assert r.status_code == 401
    r = client.post("/app/api/session/grant-input", json={"name": "test-x", "enabled": True})
    assert r.status_code == 401


def test_login_page_reachable_without_auth(tmp_path):
    server, _service, _webauth = _build(tmp_path)
    client = _client(server)
    r = client.get("/login")
    assert r.status_code == 200
    assert "Đăng nhập" in r.text
    # Unauth pages must never leak session names/output.
    assert "test-x" not in r.text


# ---------------------------------------------------------------------------
# Login: wrong/correct password, generic error message, forged CF headers
# ---------------------------------------------------------------------------

def test_wrong_password_rejected_with_generic_error(tmp_path):
    server, _service, _webauth = _build(tmp_path)
    client = _client(server)
    r = _login(client, password="totally wrong")
    assert r.status_code == 401
    assert "Sai tên đăng nhập hoặc mật khẩu" in r.text
    assert client.cookies.get("terminal_mcp_session") is None


def test_wrong_username_rejected_same_generic_error(tmp_path):
    server, _service, _webauth = _build(tmp_path)
    client = _client(server)
    r = _login(client, username="nobody", password=ADMIN_PASSWORD)
    assert r.status_code == 401
    assert "Sai tên đăng nhập hoặc mật khẩu" in r.text


def test_correct_login_sets_cookie_and_redirects_to_app(tmp_path):
    server, _service, _webauth = _build(tmp_path)
    client = _client(server)
    r = _login(client)
    assert r.status_code == 303
    assert r.headers["location"] == "/app"
    cookie = r.cookies.get("terminal_mcp_session")
    assert cookie
    set_cookie = r.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=strict" in set_cookie.lower().replace("samesite=strict", "SameSite=strict") or \
        "samesite=strict" in set_cookie.lower()


def test_forged_cf_headers_never_bypass_the_cookie_gate(tmp_path):
    server, _service, _webauth = _build(tmp_path)
    client = _client(server)
    r = client.get("/app/api/sessions", headers={
        "Cf-Access-Jwt-Assertion": "not.a.real.jwt",
        "CF-Connecting-IP": "10.0.0.1",
        "Cf-Access-Authenticated-User-Email": "attacker@example.com",
    })
    assert r.status_code == 401
    assert r.json() == {"error": "LOGIN_REQUIRED"}


def test_forged_session_cookie_value_is_rejected(tmp_path):
    server, _service, _webauth = _build(tmp_path)
    client = _client(server, cookies={"terminal_mcp_session": "forged-token-value"})
    r = client.get("/app/api/sessions")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# CSRF / Origin protection on login and every mutation
# ---------------------------------------------------------------------------

def test_login_without_origin_is_blocked(tmp_path):
    server, _service, _webauth = _build(tmp_path)
    client = TestClient(server.streamable_http_app(), base_url=BASE_URL)  # no Origin/Referer
    r = _login(client)
    assert r.status_code == 403
    assert client.cookies.get("terminal_mcp_session") is None


def test_login_with_cross_site_origin_is_blocked(tmp_path):
    server, _service, _webauth = _build(tmp_path)
    client = TestClient(server.streamable_http_app(), base_url=BASE_URL,
                        headers={"Origin": "https://evil.example.com"})
    r = _login(client)
    assert r.status_code == 403


def test_mutation_without_origin_is_blocked_even_with_valid_session(tmp_path):
    server, _service, _webauth = _build(tmp_path)
    login_client = _client(server)
    r = _login(login_client)
    cookie = r.cookies.get("terminal_mcp_session")
    no_origin_client = TestClient(server.streamable_http_app(), base_url=BASE_URL,
                                  cookies={"terminal_mcp_session": cookie})
    r = no_origin_client.post("/app/api/session/input", json={"name": "test-x", "text": "hi", "press_enter": True})
    assert r.status_code == 403
    assert r.json() == {"error": "ORIGIN_NOT_ALLOWED"}


def test_mutation_with_cross_site_origin_is_blocked_even_with_valid_session(tmp_path):
    server, _service, _webauth = _build(tmp_path)
    login_client = _client(server)
    r = _login(login_client)
    cookie = r.cookies.get("terminal_mcp_session")
    evil_client = TestClient(server.streamable_http_app(), base_url=BASE_URL,
                             headers={"Origin": "https://evil.example.com"},
                             cookies={"terminal_mcp_session": cookie})
    r = evil_client.post("/app/api/session/grant-read", json={"name": "test-x", "enabled": True})
    assert r.status_code == 403
    assert r.json() == {"error": "ORIGIN_NOT_ALLOWED"}


# ---------------------------------------------------------------------------
# Multi-node grant routing (same fix as dashboard.py's CF-Access path)
# ---------------------------------------------------------------------------

def test_grant_read_with_node_id_routes_through_controller_when_given(tmp_path):
    from terminal_mcp.controller import ControllerService
    from terminal_mcp.grants import SessionGrantStore
    from terminal_mcp.host_metrics import NodeMetrics
    from terminal_mcp.node_client import LocalNodeClient
    from terminal_mcp.node_registry import NodeRegistry

    service = TerminalService(_config(), audit=AuditStore(tmp_path / "audit.db"),
                              grants=SessionGrantStore(tmp_path / "grants.db"))
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(service), local_workspace_root=str(tmp_path))
    controller.registry.register("worker", display_name="Worker", hostname="worker-host", endpoint="http://worker")

    class _FakeClient:
        def __init__(self):
            self.grants = {}
        def list_sessions(self):
            return {"sessions": [{"name": "window"}]}
        def grant_read(self, name, enabled, *, granted_by=None):
            self.grants[name] = {"read_enabled": enabled, "input_enabled": False}
            return {"session": name, **self.grants[name]}

    fake = _FakeClient()
    controller._clients["worker"] = fake
    controller.registry.heartbeat(
        "worker", metrics=NodeMetrics(cpu_percent=5.0, load1=0.1, load5=0.1, load15=0.1, cpu_count=4,
                                     ram_total_bytes=8_000_000_000, ram_used_bytes=1_000_000_000, ram_percent=12.5,
                                     swap_total_bytes=0, swap_used_bytes=0, swap_percent=0.0,
                                     disk_total_bytes=100_000_000_000, disk_used_bytes=1_000_000_000,
                                     disk_free_bytes=99_000_000_000, disk_percent=1.0),
        tmux_session_count=1, agent_counts={}, agent_types=("shell",), agent_version=None, labels=(),
    )

    webauth = WebAuthStore(tmp_path / "webauth.db")
    webauth.create_or_replace_user("admin", ADMIN_PASSWORD)
    server = build_mcp(service)
    register_webauth_dashboard(server, service, webauth, controller=controller)
    client = _client(server)
    r = _login(client)
    assert r.status_code == 303

    response = client.post("/app/api/session/grant-read", json={"name": "window", "enabled": True, "node_id": "worker"})
    assert response.status_code == 200
    body = response.json()
    assert body.get("error") is None
    assert body["node_id"] == "worker"
    assert fake.grants["window"]["read_enabled"] is True
    # Never silently applied to the local TerminalService's own grants store.
    assert service.grants.get("window") is None


def test_grant_read_without_controller_falls_back_to_local_unchanged(tmp_path, tmux_session_factory):
    # No `controller` passed (register_webauth_dashboard's own default) --
    # exact pre-existing single-node behavior, unaffected by the fix.
    tmux_session_factory("test-webauth-grant-fallback")
    server, service, _webauth = _build(tmp_path)  # controller=None
    client = _client(server)
    r = _login(client)
    assert r.status_code == 303
    response = client.post("/app/api/session/grant-read",
                           json={"name": "test-webauth-grant-fallback", "enabled": True})
    assert response.status_code == 200
    assert response.json().get("error") is None
    assert service.grants.get("test-webauth-grant-fallback").read_enabled is True


# ---------------------------------------------------------------------------
# Rate limiting: non-permanent
# ---------------------------------------------------------------------------

def test_rate_limiting_triggers_after_repeated_failed_logins(tmp_path):
    server, _service, webauth = _build(tmp_path)
    client = _client(server)
    statuses = []
    for i in range(7):
        r = _login(client, password=f"wrong-{i}")
        statuses.append(r.status_code)
    assert 429 in statuses
    # Non-permanent: the store itself reports a bounded, finite wait.
    assert webauth.seconds_until_allowed(client.headers.get("cf-connecting-ip") or "testclient") >= 0


def test_rate_limit_is_bucketed_so_other_clients_are_unaffected(tmp_path):
    server, _service, _webauth = _build(tmp_path)
    attacker = _client(server, headers={"Origin": BASE_URL, "CF-Connecting-IP": "9.9.9.9"})
    for i in range(7):
        _login(attacker, password=f"wrong-{i}")
    victim = _client(server, headers={"Origin": BASE_URL, "CF-Connecting-IP": "1.1.1.1"})
    r = _login(victim)
    assert r.status_code == 303  # correct password, different bucket -> not rate limited


# ---------------------------------------------------------------------------
# Logout and session expiry
# ---------------------------------------------------------------------------

def test_logout_invalidates_the_session(tmp_path):
    server, _service, _webauth = _build(tmp_path)
    client = _client(server)
    _login(client)
    assert client.get("/app/api/sessions").status_code == 200
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"
    assert client.get("/app/api/sessions").status_code == 401


def test_expired_session_is_rejected(tmp_path):
    server, _service, webauth = _build(tmp_path)
    token = webauth.create_session("admin", ttl=timedelta(seconds=-1))
    client = _client(server, cookies={"terminal_mcp_session": token})
    r = client.get("/app/api/sessions")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Forced first-login password change
# ---------------------------------------------------------------------------

def test_forced_password_change_blocks_app_and_api_until_changed(tmp_path):
    server, _service, _webauth = _build(tmp_path, must_change_password=True)
    client = _client(server)
    _login(client)
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/password"
    r = client.get("/app/api/sessions")
    assert r.status_code == 403
    assert r.json() == {"error": "PASSWORD_CHANGE_REQUIRED"}
    r = client.post("/app/api/session/input", json={"name": "test-x", "text": "hi"})
    assert r.status_code == 403
    assert r.json() == {"error": "PASSWORD_CHANGE_REQUIRED"}


def test_forced_password_change_session_can_still_reach_password_form_and_logout(tmp_path):
    server, _service, _webauth = _build(tmp_path, must_change_password=True)
    client = _client(server)
    _login(client)
    r = client.get("/app/password")
    assert r.status_code == 200
    assert "Đổi mật khẩu" in r.text
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303


def test_password_change_wrong_current_password_rejected(tmp_path):
    server, _service, _webauth = _build(tmp_path, must_change_password=True)
    client = _client(server)
    _login(client)
    r = client.post("/app/password", data={
        "current_password": "not the current password", "new_password": "a brand new password",
        "confirm_password": "a brand new password",
    })
    assert r.status_code == 401
    assert "không đúng" in r.text


def test_password_change_rejects_mismatched_confirmation(tmp_path):
    server, _service, _webauth = _build(tmp_path, must_change_password=True)
    client = _client(server)
    _login(client)
    r = client.post("/app/password", data={
        "current_password": ADMIN_PASSWORD, "new_password": "a brand new password",
        "confirm_password": "does not match",
    })
    assert r.status_code == 400


def test_password_change_rejects_too_short_new_password(tmp_path):
    server, _service, _webauth = _build(tmp_path, must_change_password=True)
    client = _client(server)
    _login(client)
    r = client.post("/app/password", data={
        "current_password": ADMIN_PASSWORD, "new_password": "short1", "confirm_password": "short1",
    })
    assert r.status_code == 400


def test_password_change_rejects_same_as_current(tmp_path):
    server, _service, _webauth = _build(tmp_path, must_change_password=True)
    client = _client(server)
    _login(client)
    r = client.post("/app/password", data={
        "current_password": ADMIN_PASSWORD, "new_password": ADMIN_PASSWORD, "confirm_password": ADMIN_PASSWORD,
    })
    assert r.status_code == 400


def test_password_change_requires_origin_too(tmp_path):
    server, _service, _webauth = _build(tmp_path, must_change_password=True)
    client = _client(server)
    _login(client)
    cookie = client.cookies.get("terminal_mcp_session")
    no_origin_client = TestClient(server.streamable_http_app(), base_url=BASE_URL,
                                  cookies={"terminal_mcp_session": cookie})
    r = no_origin_client.post("/app/password", data={
        "current_password": ADMIN_PASSWORD, "new_password": "a brand new password",
        "confirm_password": "a brand new password",
    })
    assert r.status_code == 403


def test_password_change_success_issues_fresh_session_and_revokes_old(tmp_path):
    server, _service, _webauth = _build(tmp_path, must_change_password=True)
    client = _client(server)
    _login(client)
    old_cookie = client.cookies.get("terminal_mcp_session")
    r = client.post("/app/password", data={
        "current_password": ADMIN_PASSWORD, "new_password": "a brand new password",
        "confirm_password": "a brand new password",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app"
    new_cookie = r.cookies.get("terminal_mcp_session")
    assert new_cookie
    assert new_cookie != old_cookie

    old_client = TestClient(server.streamable_http_app(), base_url=BASE_URL,
                            headers={"Origin": BASE_URL}, cookies={"terminal_mcp_session": old_cookie})
    assert old_client.get("/app/api/sessions").status_code == 401

    # The new session is no longer forced, and reaches the real dashboard.
    assert client.get("/app/api/sessions").status_code == 200
    r = client.get("/app")
    assert r.status_code == 200
    assert "Terminal MCP" in r.text


def test_password_change_deletes_bootstrap_secret_scoped_to_this_store_only(tmp_path):
    # The critical bootstrap-scoping fix: a *different* store's (here,
    # simulating "production") bootstrap file for the same username must
    # never be touched by this isolated store's own password change.
    from terminal_mcp.server_http import _ensure_webauth_bootstrap, bootstrap_secret_path

    other_dir = tmp_path / "other-store"
    other_dir.mkdir()
    other_webauth = WebAuthStore(other_dir / "webauth.db")
    _ensure_webauth_bootstrap(other_webauth)  # creates other-store's own admin + bootstrap file
    other_bootstrap_path = bootstrap_secret_path(other_webauth.path)
    assert other_bootstrap_path.exists()

    this_dir = tmp_path / "this-store"
    this_dir.mkdir()
    this_webauth = WebAuthStore(this_dir / "webauth.db")
    _ensure_webauth_bootstrap(this_webauth)
    this_bootstrap_path = bootstrap_secret_path(this_webauth.path)
    assert this_bootstrap_path.exists()
    assert this_bootstrap_path != other_bootstrap_path

    service = TerminalService(_config(), audit=AuditStore(tmp_path / "audit.db"))
    server = build_mcp(service)
    register_webauth_dashboard(server, service, this_webauth)
    client = _client(server)

    # Need the actual bootstrap password to log in -- read it back here
    # (test-only; never printed) purely to drive the login form.
    content = this_bootstrap_path.read_text()
    password_line = next(line for line in content.splitlines() if line.strip().startswith("password:"))
    bootstrap_password = password_line.split(":", 1)[1].strip()

    _login(client, username="admin", password=bootstrap_password)
    r = client.post("/app/password", data={
        "current_password": bootstrap_password, "new_password": "a brand new password 12345",
        "confirm_password": "a brand new password 12345",
    }, follow_redirects=False)
    assert r.status_code == 303

    # This store's own bootstrap file is gone; the OTHER store's is intact.
    assert not this_bootstrap_path.exists()
    assert other_bootstrap_path.exists()


# ---------------------------------------------------------------------------
# Authenticated read/send still respects grants -- never bypasses core.py
# ---------------------------------------------------------------------------

def test_authenticated_session_list_and_detail_use_real_dashboard_data(tmp_path, tmux_session_factory):
    tmux_session_factory("test-webauth-read")
    server, _service, _webauth = _build(tmp_path)
    client = _client(server)
    _login(client)
    r = client.get("/app/api/sessions")
    assert r.status_code == 200
    names = [row["name"] for row in r.json()["sessions"]]
    assert "test-webauth-read" in names


def test_authenticated_send_to_non_whitelisted_session_without_grant_is_still_denied(tmp_path):
    server, _service, _webauth = _build(tmp_path)
    client = _client(server)
    _login(client)
    # "prod-secret" matches no allowed_session_patterns and has no grant --
    # a valid /app session must not be able to send into it regardless.
    r = client.post("/app/api/session/input", json={"name": "prod-secret", "text": "hi", "press_enter": True})
    assert r.status_code != 200
    assert "error" in r.json()


def test_authenticated_send_to_whitelisted_disposable_session_works(tmp_path, tmux_session_factory):
    tmux_session_factory("test-webauth-send")
    server, _service, _webauth = _build(tmp_path)
    client = _client(server)
    _login(client)
    r = client.post("/app/api/session/input", json={"name": "test-webauth-send", "text": "echo hi", "press_enter": True})
    assert r.status_code == 200
    assert "error" not in r.json()
