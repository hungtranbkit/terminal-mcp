"""Alternate, local-username/password entry point to the same dashboard --
/login, /logout, /app, /app/api/* -- coexisting with, and never weakening,
the existing Cloudflare-Access-gated /dashboard path in dashboard.py.

Security posture, load-bearing and worth stating precisely:
  - This path is reached over a SEPARATE public hostname that carries no
    Cloudflare Access application (see README's "Password login" section
    for the exact DNS/tunnel setup and why). Every request that reaches
    these routes is therefore authenticated HERE, by this module, in
    full -- never by trusting a Cf-Access-Jwt-Assertion header, a
    CF-Connecting-IP header, or any other Cloudflare-supplied header as
    proof of identity. Those headers are never even read for
    authentication purposes anywhere in this file (CF-Connecting-IP is
    read ONLY as a rate-limit bucketing key, a low-stakes decision --
    spoofing it just resets an attacker's own counter, it cannot forge a
    session).
  - The two auth mechanisms are fully isolated: this module never checks
    the Cf-Access-Jwt-Assertion header, and dashboard.py's CF-Access
    guards never check this module's session cookie. A forged cookie on
    /dashboard does nothing; a replayed/forged CF Access JWT on /app does
    nothing.
  - The tunnel ingress config for the new hostname (see README) allow-
    lists ONLY /login, /logout, /app -- /mcp, /health/*, /version, and
    /dashboard/* are not reachable through it at all, at the tunnel layer,
    regardless of anything this app does. That is the primary boundary;
    this module's own route set is a second, redundant one (it simply
    never registers anything under those other paths).
  - A valid session here grants exactly the same, and no more, session/
    grant/input authorization core.py already enforces for the dashboard
    -- terminal_send_text_granted's identity pinning, input_policy, the
    sensitive-command floor, and the TARGET_AWAITING_APPROVAL guard are
    all unchanged and still apply. Logging in never grants a tmux session
    read/input access on its own, and never touches SessionGrantStore.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import anyio
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket

from .controller import ControllerService
from .core import TerminalService
from .dashboard import DASHBOARD_HTML, INPUT_ERROR_STATUS, SESSIONS_ADMIN_HTML, WEBTERM_HTML
from .permissions import input_session_allowed, session_allowed, valid_session_name
from .supervisor import SupervisorService, SupervisorStore
from .supervisor2 import SupervisorV2Service, build_supervisor_v2
from .webauth import SESSION_COOKIE_NAME, SESSION_TTL, WebAuthStore
from .webterm import WebTerminalProcess, pump_websocket
from .webterm_assets import ASSETS

_log = logging.getLogger(__name__)

# The exact same DASHBOARD_HTML markup/JS, mounted at a different path --
# every absolute `/dashboard/api/...` fetch() call is rewritten to
# `/app/api/...` by simple, complete substring replacement (verified by
# test_webauth_dashboard.py to catch every literal occurrence in the
# source, so this can never silently drift out of sync as new API calls
# are added to dashboard.py's own JS). A logout control is appended to
# the page chrome; nothing else about the markup differs.
_LOGOUT_BUTTON = (
    '<form method="POST" action="/logout" style="display:inline"><button type="submit" '
    'style="background:#2b3f66;border:1px solid #26324b;border-radius:8px;color:#eef2ff;'
    'padding:4px 10px;cursor:pointer;font:inherit;font-size:12px">Đăng xuất</button></form>'
)
APP_DASHBOARD_HTML = (
    DASHBOARD_HTML.replace("/dashboard/api/", "/app/api/")
    .replace('href="/dashboard/sessions"', 'href="/app/sessions"')
    .replace(
        '<span class="live" id="liveBadge">● LIVE</span>',
        f'<span class="live" id="liveBadge">● LIVE</span> {_LOGOUT_BUTTON}',
    )
)
# Same page, mounted under /app/sessions -- see SESSIONS_ADMIN_HTML's own
# module-level comment in dashboard.py for why this is a full duplicate
# view of the same data/mutations rather than a new privilege surface.
# The row-level "↗ Mở" link and the back-to-terminal link both point at
# /dashboard normally; rewritten to /app the same way the API prefix is.
APP_SESSIONS_ADMIN_HTML = (
    SESSIONS_ADMIN_HTML.replace("/dashboard/api/", "/app/api/")
    .replace('href="/dashboard"', 'href="/app"')
    .replace("`/dashboard#", "`/app#")
    .replace("const WEBTERM_PAGE = '/dashboard/terminal';", "const WEBTERM_PAGE = '/app/terminal';")
    .replace(
        '<span class="live" id="liveBadge">● LIVE</span>',
        f'<span class="live" id="liveBadge">● LIVE</span> {_LOGOUT_BUTTON}',
    )
)
# The web terminal page itself (webterm.py) -- same complete-substring-
# rewrite convention as every other page above: /dashboard/assets/* (the
# vendored xterm.js/css) and /dashboard/ws/terminal both need an /app/
# counterpart route (registered below) since the webauth tunnel's ingress
# only forwards /login, /logout, /app -- see this module's own docstring.
APP_WEBTERM_HTML = (
    WEBTERM_HTML.replace('href="/dashboard/sessions"', 'href="/app/sessions"')
    .replace("/dashboard/assets/", "/app/assets/")
    .replace("const WS_PATH = '/dashboard/ws/terminal';", "const WS_PATH = '/app/ws/terminal';")
)

_PAGE_STYLE = """
  :root { color-scheme: dark; --bg:#0b1020; --panel:#121a2d; --line:#26324b; --text:#eef2ff; --muted:#9aa7bd; --accent:#5b8cff; --err:#ff6b6b; }
  * { box-sizing:border-box }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         min-height:100vh; display:flex; align-items:center; justify-content:center; padding:24px }
  .card { width:100%; max-width:360px; background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:28px }
  h1 { font-size:18px; margin:0 0 4px }
  p.sub { color:var(--muted); font-size:13px; margin:0 0 20px }
  label { display:block; font-size:13px; color:var(--muted); margin:14px 0 6px }
  input[type=text], input[type=password] {
    width:100%; padding:10px 12px; border-radius:8px; border:1px solid var(--line); background:#0f1730;
    color:var(--text); font-size:16px;
  }
  button { width:100%; margin-top:20px; padding:11px; border-radius:8px; border:none; background:var(--accent);
           color:#fff; font-size:15px; font-weight:600; cursor:pointer }
  button:hover { filter:brightness(1.08) }
  .error { margin-top:14px; padding:10px 12px; border-radius:8px; background:rgba(255,107,107,.12);
           border:1px solid rgba(255,107,107,.4); color:var(--err); font-size:13px }
"""


def _login_page_html(error: str = "") -> str:
    error_html = f'<div class="error">{error}</div>' if error else ""
    return f"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Đăng nhập</title><style>{_PAGE_STYLE}</style></head>
<body><div class="card">
  <h1>Terminal MCP</h1>
  <p class="sub">Đăng nhập bằng tài khoản cục bộ.</p>
  <form method="POST" action="/login">
    <label for="username">Tên đăng nhập</label>
    <input type="text" id="username" name="username" autocomplete="username" required autofocus maxlength="128">
    <label for="password">Mật khẩu</label>
    <input type="password" id="password" name="password" autocomplete="current-password" required maxlength="256">
    <button type="submit">Đăng nhập</button>
  </form>
  {error_html}
</div></body></html>"""


def _password_form_html(username: str, *, forced: bool, error: str = "") -> str:
    error_html = f'<div class="error">{error}</div>' if error else ""
    intro = (
        "Tài khoản này đang dùng mật khẩu khởi tạo tạm thời -- phải đổi trước khi "
        "dùng được dashboard." if forced else
        "Đổi mật khẩu cho tài khoản hiện tại."
    )
    logout_form = (
        '<form method="POST" action="/logout" style="margin-top:14px">'
        '<button type="submit" style="background:#3a2430">Đăng xuất</button></form>'
        if forced else ""
    )
    return f"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Đổi mật khẩu</title><style>{_PAGE_STYLE}</style></head>
<body><div class="card">
  <h1>Đổi mật khẩu</h1>
  <p class="sub">{intro}</p>
  <form method="POST" action="/app/password">
    <label for="current_password">Mật khẩu hiện tại</label>
    <input type="password" id="current_password" name="current_password" autocomplete="current-password" required maxlength="256">
    <label for="new_password">Mật khẩu mới (tối thiểu 12 ký tự)</label>
    <input type="password" id="new_password" name="new_password" autocomplete="new-password" required minlength="12" maxlength="256">
    <label for="confirm_password">Xác nhận mật khẩu mới</label>
    <input type="password" id="confirm_password" name="confirm_password" autocomplete="new-password" required minlength="12" maxlength="256">
    <button type="submit">Đổi mật khẩu</button>
  </form>
  {error_html}
  {logout_form}
</div></body></html>"""


_GENERIC_LOGIN_ERROR = "Sai tên đăng nhập hoặc mật khẩu."


def _rate_limited_error(seconds: float) -> str:
    return f"Quá nhiều lần đăng nhập sai. Thử lại sau {int(seconds) + 1} giây."


def _origin_allowed(request: Request, allowed_origins: tuple[str, ...]) -> bool:
    # Identical rule to dashboard.py's own _origin_allowed (kept as a
    # separate, self-contained copy rather than a shared import so this
    # module has no dependency on dashboard.py's private closures) -- a
    # missing/cross-origin Origin or Referer is refused outright before
    # the request touches anything.
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return False
    parsed = urlparse(origin)
    origin_value = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    if not origin_value:
        return False
    host = request.headers.get("host", "")
    same_origin = {f"https://{host}", f"http://{host}"}
    return origin_value in same_origin or origin_value in allowed_origins


def register_webauth_dashboard(server: MCPServer, terminal: TerminalService, webauth: WebAuthStore,
                               supervisor: SupervisorService | None = None,
                               supervisor_v2: SupervisorV2Service | None = None,
                               controller: ControllerService | None = None) -> None:
    if supervisor is None:
        supervisor = SupervisorService(terminal, SupervisorStore())
    if supervisor_v2 is None:
        supervisor_v2 = build_supervisor_v2(supervisor)
    # `controller` (multi-node grant routing fix -- same one dashboard.py's
    # /dashboard/api/session/grant-read|input already use): optional so an
    # existing caller that builds this module standalone (a test, or a
    # future single-node-only embedding) keeps working unchanged -- with
    # none given, grant-read/grant-input fall back to the exact single-
    # node behavior this module always had (local TerminalService only).
    # This module's session lifecycle (create/detach/delete) and web
    # terminal WS are NOT routed through `controller` -- a deliberate,
    # documented scope cut (see docs/multi-node.md): this login path is
    # the secondary, non-Cloudflare-Access dashboard entry point, and
    # giving it full multi-node parity is a larger, separate change.

    def _client_key(request: Request) -> str:
        # CF-Connecting-IP is Cloudflare's own real-visitor-IP header --
        # read here ONLY to bucket rate-limit counters (never for
        # authentication; see this module's own docstring). Falls back to
        # the immediate TCP peer (which, behind the tunnel, is always
        # 127.0.0.1 -- still a valid, if coarse, bucket if that header is
        # ever absent for some reason).
        return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")

    def _session_user(request: Request):
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not token:
            return None
        return webauth.resolve_session(token)

    def _require_session_page(request: Request):
        user = _session_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=303), None
        return None, user

    def _require_session_api(request: Request):
        # A JSON 401, not a redirect: a fetch() call following a redirect
        # to an HTML login page would just fail to parse as JSON anyway --
        # fail fast and explicitly instead. A session forced to change its
        # password is refused here too -- only /app/password and /logout
        # are reachable until that succeeds, never the actual session/
        # grant/send API surface, regardless of what /app itself renders.
        user = _session_user(request)
        if user is None:
            return JSONResponse({"error": "LOGIN_REQUIRED"}, status_code=401), None
        if user.must_change_password:
            return JSONResponse({"error": "PASSWORD_CHANGE_REQUIRED"}, status_code=403), None
        return None, user

    def _mutation_guard(request: Request):
        if not _origin_allowed(request, terminal.config.dashboard.allowed_origins):
            return JSONResponse({"error": "ORIGIN_NOT_ALLOWED"}, status_code=403), None
        return _require_session_api(request)

    @server.custom_route("/login", methods=["GET"], include_in_schema=False)
    async def login_page(request: Request):
        if _session_user(request) is not None:
            return RedirectResponse("/app", status_code=303)
        return HTMLResponse(_login_page_html(), headers={"Cache-Control": "no-store"})

    @server.custom_route("/login", methods=["POST"], include_in_schema=False)
    async def login_submit(request: Request):
        if not _origin_allowed(request, terminal.config.dashboard.allowed_origins):
            return HTMLResponse(_login_page_html(_GENERIC_LOGIN_ERROR), status_code=403,
                                headers={"Cache-Control": "no-store"})
        client_key = _client_key(request)
        wait = await anyio.to_thread.run_sync(webauth.seconds_until_allowed, client_key)
        if wait > 0:
            return HTMLResponse(_login_page_html(_rate_limited_error(wait)), status_code=429,
                                headers={"Cache-Control": "no-store"})
        try:
            form = await request.form()
        except Exception:
            form = {}
        username = str(form.get("username") or "")[:128]
        password = str(form.get("password") or "")[:256]
        user = None
        if username and password:
            user = await anyio.to_thread.run_sync(webauth.verify_password, username, password)
        if user is None:
            await anyio.to_thread.run_sync(webauth.record_failure, client_key)
            _log.info("webauth login failed username=%s client=%s", username or "(empty)", client_key)
            return HTMLResponse(_login_page_html(_GENERIC_LOGIN_ERROR), status_code=401,
                                headers={"Cache-Control": "no-store"})
        await anyio.to_thread.run_sync(webauth.record_success, client_key)
        token = await anyio.to_thread.run_sync(webauth.create_session, user.username)
        _log.info("webauth login succeeded username=%s client=%s", user.username, client_key)
        response = RedirectResponse("/app", status_code=303)
        response.set_cookie(
            SESSION_COOKIE_NAME, token, max_age=int(SESSION_TTL.total_seconds()),
            httponly=True, secure=True, samesite="strict", path="/",
        )
        return response

    @server.custom_route("/logout", methods=["POST"], include_in_schema=False)
    async def logout(request: Request):
        # Logging out is deliberately permissive about Origin (the worst
        # case of a forged cross-site logout is logging the victim out --
        # an annoyance, never a privilege escalation) but still requires
        # an existing valid session to actually do anything.
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if token:
            await anyio.to_thread.run_sync(webauth.destroy_session, token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return response

    @server.custom_route("/app", methods=["GET"], include_in_schema=False)
    async def app_page(request: Request):
        blocked, user = _require_session_page(request)
        if blocked is not None:
            return blocked
        if user.must_change_password:
            return RedirectResponse("/app/password", status_code=303)
        return HTMLResponse(APP_DASHBOARD_HTML, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    @server.custom_route("/app/sessions", methods=["GET"], include_in_schema=False)
    async def app_sessions_admin_page(request: Request):
        # Same session requirement as /app itself -- a forced-change
        # session is redirected to /app/password just like /app is,
        # never allowed to reach this second view of the same data either.
        blocked, user = _require_session_page(request)
        if blocked is not None:
            return blocked
        if user.must_change_password:
            return RedirectResponse("/app/password", status_code=303)
        return HTMLResponse(APP_SESSIONS_ADMIN_HTML, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    @server.custom_route("/app/password", methods=["GET"], include_in_schema=False)
    async def app_password_page(request: Request):
        # Deliberately _require_session_page, not _require_session_api's
        # must_change_password-refusing variant -- this IS the one page a
        # forced-change session must be able to reach (along with
        # /logout, which needs no session check to still function).
        blocked, user = _require_session_page(request)
        if blocked is not None:
            return blocked
        return HTMLResponse(_password_form_html(user.username, forced=user.must_change_password),
                            headers={"Cache-Control": "no-store"})

    @server.custom_route("/app/password", methods=["POST"], include_in_schema=False)
    async def app_password_submit(request: Request):
        blocked, user = _require_session_page(request)
        if blocked is not None:
            return blocked
        if not _origin_allowed(request, terminal.config.dashboard.allowed_origins):
            return HTMLResponse(
                _password_form_html(user.username, forced=user.must_change_password, error="Yêu cầu không hợp lệ."),
                status_code=403, headers={"Cache-Control": "no-store"},
            )
        try:
            form = await request.form()
        except Exception:
            form = {}
        current = str(form.get("current_password") or "")[:256]
        new = str(form.get("new_password") or "")[:256]
        confirm = str(form.get("confirm_password") or "")[:256]

        def _render(error: str, status_code: int) -> HTMLResponse:
            return HTMLResponse(
                _password_form_html(user.username, forced=user.must_change_password, error=error),
                status_code=status_code, headers={"Cache-Control": "no-store"},
            )

        if await anyio.to_thread.run_sync(webauth.verify_password, user.username, current) is None:
            return _render("Mật khẩu hiện tại không đúng.", 401)
        if len(new) < 12:
            return _render("Mật khẩu mới phải có ít nhất 12 ký tự.", 400)
        if new != confirm:
            return _render("Xác nhận mật khẩu mới không khớp.", 400)
        if new == current:
            return _render("Mật khẩu mới phải khác mật khẩu hiện tại.", 400)
        # set_password invalidates EVERY existing session for this user,
        # including the one this very request is using -- a fresh session
        # is issued immediately below so the user is not logged out by
        # the act of changing their own password.
        await anyio.to_thread.run_sync(webauth.set_password, user.username, new)
        token = await anyio.to_thread.run_sync(webauth.create_session, user.username)
        _log.info("webauth password changed username=%s", user.username)
        # Local import: avoids a module-level circular import (server_http
        # imports this module to register these routes).
        from .server_http import delete_bootstrap_secret_if_matches

        if await anyio.to_thread.run_sync(delete_bootstrap_secret_if_matches, user.username, webauth.path):
            _log.info("webauth: removed one-time bootstrap secret file after password change (username=%s)",
                      user.username)
        response = RedirectResponse("/app", status_code=303)
        response.set_cookie(
            SESSION_COOKIE_NAME, token, max_age=int(SESSION_TTL.total_seconds()),
            httponly=True, secure=True, samesite="strict", path="/",
        )
        return response

    @server.custom_route("/app/api/sessions", methods=["GET"], include_in_schema=False)
    async def app_sessions(request: Request):
        blocked, _user = _require_session_api(request)
        if blocked is not None:
            return blocked
        listed = await anyio.to_thread.run_sync(terminal.dashboard_list_sessions)
        rows = listed.get("sessions")
        if isinstance(rows, list):
            async def _fill_state(row: dict) -> None:
                if not row.get("effective_read"):
                    row["state"] = "RESTRICTED"
                    return
                fetch = terminal.terminal_status if row["allowed"] else terminal.terminal_status_granted
                status = await anyio.to_thread.run_sync(fetch, row["name"])
                row["state"] = status.get("state", "UNKNOWN")

            async with anyio.create_task_group() as tg:
                for row in rows:
                    tg.start_soon(_fill_state, row)
            rows.sort(key=lambda r: r["name"])
            rows.sort(key=lambda r: r.get("activity") or "", reverse=True)
            rows.sort(key=lambda r: 0 if r.get("state") == "WAITING_INPUT" else 1)
        return JSONResponse(listed, headers={"Cache-Control": "no-store"})

    @server.custom_route("/app/api/session", methods=["GET"], include_in_schema=False)
    async def app_session_detail(request: Request):
        blocked, _user = _require_session_api(request)
        if blocked is not None:
            return blocked
        name = request.query_params.get("name", "")
        if not session_allowed(name, terminal.config) and not (
            (grant := terminal.grants.get(name)) is not None and grant.read_enabled
        ):
            block_reason = await anyio.to_thread.run_sync(terminal._input_grant_block_reason, name)
            return JSONResponse(
                {"error": "READ_RESTRICTED", "session": name, "input_block_reason": block_reason},
                status_code=403, headers={"Cache-Control": "no-store"},
            )
        use_granted = not session_allowed(name, terminal.config)
        status_fn = terminal.terminal_status_granted if use_granted else terminal.terminal_status
        tail_fn = (lambda: terminal.terminal_tail_granted(name, ansi=True)) if use_granted \
            else (lambda: terminal.terminal_tail(name, ansi=True))
        status_result: dict = {}
        tail_result: dict = {}

        async def _status() -> None:
            status_result.update(await anyio.to_thread.run_sync(status_fn, name))

        async def _tail() -> None:
            tail_result.update(await anyio.to_thread.run_sync(tail_fn))

        async with anyio.create_task_group() as tg:
            tg.start_soon(_status)
            tg.start_soon(_tail)

        status, tail = status_result, tail_result
        if "error" in status:
            return JSONResponse(status, status_code=403 if status["error"] in
                                ("ACCESS_DENIED", "READ_RESTRICTED") else 404)
        if "error" in tail:
            return JSONResponse(tail, status_code=403 if tail["error"] == "READ_RESTRICTED" else 404)
        grant = terminal.grants.get(name)
        input_allowed = bool(
            terminal.config.permissions.terminal_input
            and (input_session_allowed(name, terminal.config) or (grant is not None and grant.input_enabled))
        )
        allowed = session_allowed(name, terminal.config)
        body = {
            "session": name, "status": status, "tail": tail, "input_allowed": input_allowed,
            "allowed": allowed,
            "grant": {"read_enabled": bool(grant and grant.read_enabled),
                     "input_enabled": bool(grant and grant.input_enabled)},
        }
        if not allowed and not input_allowed:
            body["input_block_reason"] = await anyio.to_thread.run_sync(terminal._input_grant_block_reason, name)
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @server.custom_route("/app/api/session/input", methods=["POST"], include_in_schema=False)
    async def app_session_input(request: Request):
        blocked, user = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = body.get("name") if isinstance(body, dict) else None
        text = body.get("text") if isinstance(body, dict) else None
        press_enter = bool(body.get("press_enter", False)) if isinstance(body, dict) else False
        idempotency_key = body.get("idempotency_key") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name or not isinstance(text, str) or not text:
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        if idempotency_key is not None and not isinstance(idempotency_key, str):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        _log.info("webauth session_input session=%s username=%s", name, user.username)
        grant = terminal.grants.get(name)
        use_granted = grant is not None and not input_session_allowed(name, terminal.config)
        send_fn = terminal.terminal_send_text_granted if use_granted else terminal.terminal_send_text
        result = await anyio.to_thread.run_sync(
            lambda: send_fn(name, text, press_enter=press_enter, idempotency_key=idempotency_key)
        )
        status_code = 200
        if "error" in result:
            status_code = INPUT_ERROR_STATUS.get(result["error"], 400)
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    def _qualify_grant_name(name: str, body: dict) -> str:
        # Same multi-node grant-routing fix as dashboard.py's own
        # /dashboard/api/session/grant-read|input (see that module's
        # identical helper for the full rationale/bug this fixes) --
        # duplicated rather than shared, matching this file's existing
        # one-route-pair-per-concept posture. A no-op (returns `name`
        # unchanged) when `controller` was never given to this module.
        node_id = body.get("node_id") if isinstance(body, dict) else None
        if (controller is not None and isinstance(node_id, str) and node_id
                and node_id != controller.local_node_id and "/" not in name):
            return f"{node_id}/{name}"
        return name

    @server.custom_route("/app/api/session/grant-read", methods=["POST"], include_in_schema=False)
    async def app_session_grant_read(request: Request):
        blocked, user = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = body.get("name") if isinstance(body, dict) else None
        enabled = body.get("enabled") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name or not isinstance(enabled, bool):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        qualified = _qualify_grant_name(name, body)
        granted_by = f"webauth:{user.username}"
        _log.info("webauth grant_read session=%s enabled=%s username=%s", qualified, enabled, user.username)
        if controller is not None:
            result = await anyio.to_thread.run_sync(
                lambda: controller.terminal_grant_session_read(qualified, enabled, granted_by=granted_by))
        else:
            result = await anyio.to_thread.run_sync(lambda: terminal.grant_session_read(name, enabled, granted_by=granted_by))
        terminal.audit.record(
            action="grant_read", session=name, result="GRANTED" if (enabled and "error" not in result)
            else ("REVOKED" if "error" not in result else "BLOCKED"),
            reason=result.get("error") or granted_by, source_transport="dashboard",
        )
        status_code = 200 if "error" not in result else INPUT_ERROR_STATUS.get(result["error"], 400)
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    @server.custom_route("/app/api/session/grant-input", methods=["POST"], include_in_schema=False)
    async def app_session_grant_input(request: Request):
        blocked, user = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = body.get("name") if isinstance(body, dict) else None
        enabled = body.get("enabled") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name or not isinstance(enabled, bool):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        qualified = _qualify_grant_name(name, body)
        granted_by = f"webauth:{user.username}"
        _log.info("webauth grant_input session=%s enabled=%s username=%s", qualified, enabled, user.username)
        if controller is not None:
            result = await anyio.to_thread.run_sync(
                lambda: controller.terminal_grant_session_input(qualified, enabled, granted_by=granted_by))
        else:
            result = await anyio.to_thread.run_sync(lambda: terminal.grant_session_input(name, enabled, granted_by=granted_by))
        terminal.audit.record(
            action="grant_input", session=name, result="GRANTED" if (enabled and "error" not in result)
            else ("REVOKED" if "error" not in result else "BLOCKED"),
            reason=result.get("error") or granted_by, source_transport="dashboard",
        )
        status_code = 200 if "error" not in result else INPUT_ERROR_STATUS.get(result["error"], 400)
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    # -- Session lifecycle: create/detach/delete -- same TerminalService
    # methods dashboard.py's /dashboard/api/session/* routes and the MCP
    # tools use; see dashboard.py's own routes for the fuller comments.

    @server.custom_route("/app/api/session/create", methods=["POST"], include_in_schema=False)
    async def app_session_create(request: Request):
        blocked, user = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = body.get("name") if isinstance(body, dict) else None
        agent_type = body.get("agent_type", "shell") if isinstance(body, dict) else None
        cwd = body.get("cwd") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name or not isinstance(agent_type, str):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        if cwd is not None and not isinstance(cwd, str):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        granted_by = f"webauth:{user.username}"
        _log.info("webauth create_session name=%s agent_type=%s username=%s", name, agent_type, user.username)
        result = await anyio.to_thread.run_sync(
            lambda: terminal.terminal_create_session(name, agent_type, cwd, requested_by=granted_by)
        )
        status_code = 200 if "error" not in result else INPUT_ERROR_STATUS.get(result["error"], 400)
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    @server.custom_route("/app/api/session/detach", methods=["POST"], include_in_schema=False)
    async def app_session_detach(request: Request):
        blocked, user = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name:
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        _log.info("webauth detach_session name=%s username=%s", name, user.username)
        result = await anyio.to_thread.run_sync(terminal.terminal_detach_session, name)
        status_code = 200 if "error" not in result else INPUT_ERROR_STATUS.get(result["error"], 400)
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    @server.custom_route("/app/api/session/delete", methods=["POST"], include_in_schema=False)
    async def app_session_delete(request: Request):
        blocked, user = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name:
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        _log.info("webauth delete_session name=%s username=%s", name, user.username)
        result = await anyio.to_thread.run_sync(terminal.terminal_delete_session, name)
        if "error" not in result:
            await anyio.to_thread.run_sync(lambda: supervisor.unwatch(session=name, delete=False))
        status_code = 200 if "error" not in result else INPUT_ERROR_STATUS.get(result["error"], 400)
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    # -- Web terminal: xterm.js over a WebSocket, attached directly to an
    # existing tmux session's real pty -- same feature, same
    # TerminalService.terminal_web_terminal_access authorization decision
    # (core.py), same WebTerminalProcess/pump_websocket (webterm.py) as
    # dashboard.py's /dashboard/ws/terminal; only the auth layer in front
    # of it differs (this module's cookie session, not Cloudflare Access),
    # exactly like every other route pair in this file.

    @server.custom_route("/app/assets/{filename}", methods=["GET"], include_in_schema=False)
    async def app_webterm_asset(request: Request):
        # Same public-static-asset posture as dashboard.py's
        # /dashboard/assets/{filename} -- no session content, so
        # deliberately not behind _require_session_api.
        asset = ASSETS.get(request.path_params["filename"])
        if asset is None:
            return Response(status_code=404)
        content, content_type = asset
        return Response(content, media_type=content_type,
                        headers={"Cache-Control": "public, max-age=86400, immutable"})

    @server.custom_route("/app/terminal", methods=["GET"], include_in_schema=False)
    async def app_webterm_page(request: Request):
        blocked, user = _require_session_page(request)
        if blocked is not None:
            return blocked
        if user.must_change_password:
            return RedirectResponse("/app/password", status_code=303)
        return HTMLResponse(APP_WEBTERM_HTML, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    async def app_terminal_ws(websocket: WebSocket) -> None:
        if not _origin_allowed(websocket, terminal.config.dashboard.allowed_origins):
            await websocket.close(code=4403)
            return
        # _session_user is the exact same closure /app/api/* routes use --
        # only touches .cookies (present on WebSocket too), so this is
        # true reuse, not a parallel copy of the session-lookup logic.
        user = _session_user(websocket)
        if user is None:
            await websocket.close(code=4401)
            return
        if user.must_change_password:
            await websocket.close(code=4403)
            return
        session = websocket.query_params.get("session", "")
        takeover_requested = websocket.query_params.get("takeover") == "1"
        if not valid_session_name(session):
            await websocket.close(code=4400)
            return
        access = await anyio.to_thread.run_sync(terminal.terminal_web_terminal_access, session)
        if "error" in access:
            code = 4404 if access["error"] == "SESSION_NOT_FOUND" else 4403
            await websocket.close(code=code)
            return
        input_enabled = bool(access["input"])
        takeover = takeover_requested and input_enabled
        await websocket.accept()
        _log.info("webauth web_terminal_open session=%s input=%s takeover=%s username=%s",
                  session, input_enabled, takeover, user.username)
        terminal.audit.record(action="web_terminal_open", session=session, result="OPENED",
                              reason=f"input={input_enabled} takeover={takeover} webauth:{user.username}",
                              source_transport="dashboard")
        proc = await anyio.to_thread.run_sync(
            lambda: WebTerminalProcess(terminal.tmux.binary, session, readonly=not input_enabled, takeover=takeover)
        )
        try:
            await websocket.send_json({"type": "ready", "session": session, "readonly": not input_enabled,
                                       "attached": access.get("attached", False)})
            await pump_websocket(websocket, proc)
        finally:
            await anyio.to_thread.run_sync(proc.close)
            terminal.audit.record(action="web_terminal_close", session=session, result="CLOSED",
                                  source_transport="dashboard")

    # Same "no WebSocket-route decorator on MCPServer.custom_route" reason
    # dashboard.py's own /dashboard/ws/terminal registration documents --
    # appended to the exact same _custom_starlette_routes list every
    # @server.custom_route call (both modules) already populates.
    server._custom_starlette_routes.append(
        WebSocketRoute("/app/ws/terminal", endpoint=app_terminal_ws, name="app_terminal_ws")
    )

    @server.custom_route("/app/api/supervisor", methods=["GET"], include_in_schema=False)
    async def app_supervisor_summary(request: Request):
        blocked, _user = _require_session_api(request)
        if blocked is not None:
            return blocked

        def _compute() -> dict:
            status = supervisor.status()
            events = supervisor.list_events(unacknowledged_only=True, limit=20)["events"]
            return {"status": status, "events": events}

        result = await anyio.to_thread.run_sync(_compute)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @server.custom_route("/app/api/supervisor/ack", methods=["POST"], include_in_schema=False)
    async def app_supervisor_ack(request: Request):
        blocked, _user = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        event_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(event_id, int):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        result = await anyio.to_thread.run_sync(supervisor.ack_event, event_id)
        status_code = 404 if "error" in result else 200
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    @server.custom_route("/app/api/supervisor2", methods=["GET"], include_in_schema=False)
    async def app_supervisor2_summary(request: Request):
        blocked, _user = _require_session_api(request)
        if blocked is not None:
            return blocked

        def _compute() -> list[dict]:
            rows = []
            for watch in supervisor.list_watches()["watches"]:
                policy = supervisor_v2.store.get_policy(watch["watch_key"])
                if policy["policy_mode"] == "observe_only" and policy["created_at"] is None:
                    continue
                actions = supervisor_v2.store.list_actions(watch_key=watch["watch_key"], limit=1)
                rows.append({
                    "watch_key": watch["watch_key"], "target": watch["target"], "kind": watch["kind"],
                    "watch_state": watch["state"], "policy": policy,
                    "latest_action": actions[0] if actions else None,
                })
            return rows

        rows = await anyio.to_thread.run_sync(_compute)
        return JSONResponse({"watches": rows}, headers={"Cache-Control": "no-store"})

    @server.custom_route("/app/api/supervisor2/pause", methods=["POST"], include_in_schema=False)
    async def app_supervisor2_pause(request: Request):
        blocked, _user = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        target = body.get("target") if isinstance(body, dict) else None
        kind = body.get("kind") if isinstance(body, dict) else None
        if not isinstance(target, str) or kind not in ("session", "binding"):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        kwargs = {"session": target} if kind == "session" else {"binding": target}
        result = await anyio.to_thread.run_sync(lambda: supervisor_v2.set_policy(policy_mode="observe_only", **kwargs))
        status_code = 404 if "error" in result else 200
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})
