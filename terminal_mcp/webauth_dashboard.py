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
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from .core import TerminalService
from .dashboard import DASHBOARD_HTML, INPUT_ERROR_STATUS
from .permissions import input_session_allowed, session_allowed
from .supervisor import SupervisorService, SupervisorStore
from .supervisor2 import SupervisorV2Service, build_supervisor_v2
from .webauth import SESSION_COOKIE_NAME, SESSION_TTL, WebAuthStore

_log = logging.getLogger(__name__)

# The exact same DASHBOARD_HTML markup/JS, mounted at a different path --
# every absolute `/dashboard/api/...` fetch() call is rewritten to
# `/app/api/...` by simple, complete substring replacement (verified by
# test_webauth_dashboard.py to catch every literal occurrence in the
# source, so this can never silently drift out of sync as new API calls
# are added to dashboard.py's own JS). A logout control is appended to
# the page chrome; nothing else about the markup differs.
APP_DASHBOARD_HTML = DASHBOARD_HTML.replace("/dashboard/api/", "/app/api/").replace(
    '<span class="live" id="liveBadge">● LIVE</span>',
    '<span class="live" id="liveBadge">● LIVE</span> '
    '<form method="POST" action="/logout" style="display:inline"><button type="submit" '
    'style="background:#2b3f66;border:1px solid #26324b;border-radius:8px;color:#eef2ff;'
    'padding:4px 10px;cursor:pointer;font:inherit;font-size:12px">Đăng xuất</button></form>',
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
                               supervisor_v2: SupervisorV2Service | None = None) -> None:
    if supervisor is None:
        supervisor = SupervisorService(terminal, SupervisorStore())
    if supervisor_v2 is None:
        supervisor_v2 = build_supervisor_v2(supervisor)

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
        granted_by = f"webauth:{user.username}"
        _log.info("webauth grant_read session=%s enabled=%s username=%s", name, enabled, user.username)
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
        granted_by = f"webauth:{user.username}"
        _log.info("webauth grant_input session=%s enabled=%s username=%s", name, enabled, user.username)
        result = await anyio.to_thread.run_sync(lambda: terminal.grant_session_input(name, enabled, granted_by=granted_by))
        terminal.audit.record(
            action="grant_input", session=name, result="GRANTED" if (enabled and "error" not in result)
            else ("REVOKED" if "error" not in result else "BLOCKED"),
            reason=result.get("error") or granted_by, source_transport="dashboard",
        )
        status_code = 200 if "error" not in result else INPUT_ERROR_STATUS.get(result["error"], 400)
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

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
