"""OAuth 2.1 authorization server for the public read-only observer surface.

Why this exists: a hosted Claude client (claude.ai, the mobile app) reaches
an MCP server over the public internet and authenticates it with OAuth --
it cannot be given a custom `Authorization` header the way Claude Code's
own MCP config can, and it cannot log in to a Cloudflare Access page on
the operator's behalf. So the observer endpoint has to speak OAuth itself.

Almost none of the protocol is implemented here. `mcp.server.auth` already
ships the metadata, registration, authorization and token endpoints, PKCE
verification, redirect-uri matching and the bearer middleware; this module
supplies only the two things the SDK cannot know:

  1. WHERE the resource owner proves who they are. `authorize()` returns a
     URL instead of a decision, so the browser lands on `/observer/login`
     (registered by `register_observer_login`), which checks the password
     against the SAME WebAuthStore the dashboard uses -- one account set
     for this host, not a second parallel one.
  2. WHERE grants live between requests. `ObserverOAuthStore`, a SQLite
     file beside webauth.db.

Security posture, and how it differs from the dashboard's cookie session:

* Tokens are stored as SHA-256 hashes, never in the clear. A read of the
  database file does not yield a usable bearer token, the same reasoning
  as `WebAuthStore._hash_token`.
* The login step reuses WebAuthStore's own scrypt verification AND its
  rate limiter, so brute force against this endpoint is throttled exactly
  like brute force against /login.
* Authorization codes are single use: `exchange_authorization_code`
  deletes the row it just consumed, so a replayed code is an invalid_grant
  rather than a second token.
* Refresh rotates. `exchange_refresh_token` revokes the presented token
  and issues a new pair, so a stolen refresh token stops working the
  moment the legitimate client next refreshes.
* Scope is a single constant, `observe`. There is deliberately no scope
  that maps to a write: the observer surface has no write tool to gate
  (see observer_app.py), so a scope hierarchy would be decoration.

Dynamic client registration is ENABLED, because a hosted client registers
itself and there is no operator step where a client_id could be pasted in.
That is not the hole it sounds like: registering a client gets you nothing
at all until a human passes the password login above.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import anyio
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from .webauth import WebAuthStore

_log = logging.getLogger(__name__)

# The one scope this resource understands. See the module docstring for
# why there is no second one.
OBSERVE_SCOPE = "observe"

LOGIN_PATH = "/observer/login"

# Lifetimes. The access token is short because refreshing is free and
# silent for the client; the refresh token is long because the alternative
# is the operator re-entering a password on a phone every day. The
# pending-authorization and code windows are short because a human is
# actively in the flow for both.
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
AUTHORIZATION_CODE_TTL_SECONDS = 300
PENDING_AUTHORIZATION_TTL_SECONDS = 600

_TOKEN_BYTES = 32


def default_observer_oauth_db_path() -> Path:
    """Beside webauth.db, and overridable the same way, so an operator who
    relocated one state directory does not end up with the other half of
    the auth state somewhere else."""
    override = os.environ.get("TERMINAL_MCP_OBSERVER_OAUTH_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "observer-oauth.db"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


@dataclass(frozen=True)
class PendingAuthorization:
    """One in-flight `/authorize` request, parked while its human logs in."""

    request_id: str
    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    state: str | None
    scopes: tuple[str, ...]
    resource: str | None


class ObserverOAuthStore:
    """SQLite persistence for clients, pending authorizations, codes and
    tokens. Separate file from webauth.db on purpose: this table set is
    disposable (deleting it logs every remote client out and costs nothing
    else), while webauth.db holds the accounts themselves."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_observer_oauth_db_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as conn:
            # Same connection-per-call/WAL/0600 pattern as webauth.py and
            # every other durable store in this project.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY,
                    info TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending (
                    request_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    redirect_uri_explicit INTEGER NOT NULL,
                    code_challenge TEXT NOT NULL,
                    state TEXT,
                    scopes TEXT NOT NULL,
                    resource TEXT,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS codes (
                    code TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    redirect_uri_explicit INTEGER NOT NULL,
                    code_challenge TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    resource TEXT,
                    subject TEXT,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    token_hash TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    resource TEXT,
                    subject TEXT,
                    expires_at INTEGER NOT NULL,
                    sibling_hash TEXT
                );
                """
            )
        # 0600: the rows are not plaintext credentials, but they are a map
        # of who is connected from where, and the file sits in a shared
        # state directory.
        try:
            self.path.chmod(0o600)
        except OSError:  # pragma: no cover -- unusual filesystem, not fatal
            _log.warning("observer-oauth: could not chmod %s to 0600", self.path)

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # -- clients ---------------------------------------------------------

    def put_client(self, info: OAuthClientInformationFull) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO clients (client_id, info, created_at) VALUES (?, ?, ?)",
                (info.client_id, info.model_dump_json(exclude_none=True), time.time()),
            )

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self._connection() as conn:
            row = conn.execute("SELECT info FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        if row is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate(json.loads(row["info"]))
        except Exception:  # noqa: BLE001 -- a row this process can no longer parse is a row it must not trust
            _log.warning("observer-oauth: dropping unparseable client record %r", client_id)
            return None

    # -- pending authorizations -------------------------------------------

    def put_pending(self, pending: PendingAuthorization) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pending (request_id, client_id, redirect_uri, redirect_uri_explicit,"
                " code_challenge, state, scopes, resource, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pending.request_id, pending.client_id, pending.redirect_uri,
                 int(pending.redirect_uri_provided_explicitly), pending.code_challenge, pending.state,
                 " ".join(pending.scopes), pending.resource,
                 time.time() + PENDING_AUTHORIZATION_TTL_SECONDS),
            )

    def take_pending(self, request_id: str) -> PendingAuthorization | None:
        """Read and delete in one transaction -- a login form submitted
        twice must not mint two authorization codes."""
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM pending WHERE request_id = ?", (request_id,)).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM pending WHERE request_id = ?", (request_id,))
        if row["expires_at"] < time.time():
            return None
        return PendingAuthorization(
            request_id=row["request_id"], client_id=row["client_id"], redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["redirect_uri_explicit"]),
            code_challenge=row["code_challenge"], state=row["state"],
            scopes=tuple(row["scopes"].split()) if row["scopes"] else (), resource=row["resource"],
        )

    def peek_pending(self, request_id: str) -> PendingAuthorization | None:
        """Non-consuming read, for rendering the login page (and for
        re-rendering it after a wrong password)."""
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM pending WHERE request_id = ?", (request_id,)).fetchone()
        if row is None or row["expires_at"] < time.time():
            return None
        return PendingAuthorization(
            request_id=row["request_id"], client_id=row["client_id"], redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["redirect_uri_explicit"]),
            code_challenge=row["code_challenge"], state=row["state"],
            scopes=tuple(row["scopes"].split()) if row["scopes"] else (), resource=row["resource"],
        )

    # -- authorization codes ------------------------------------------------

    def put_code(self, code: AuthorizationCode) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO codes (code, client_id, redirect_uri, redirect_uri_explicit,"
                " code_challenge, scopes, resource, subject, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (code.code, code.client_id, str(code.redirect_uri), int(code.redirect_uri_provided_explicitly),
                 code.code_challenge, " ".join(code.scopes), code.resource, code.subject, code.expires_at),
            )

    def get_code(self, code: str) -> AuthorizationCode | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM codes WHERE code = ?", (code,)).fetchone()
        if row is None or row["expires_at"] < time.time():
            return None
        return AuthorizationCode(
            code=row["code"], scopes=row["scopes"].split() if row["scopes"] else [],
            expires_at=row["expires_at"], client_id=row["client_id"],
            code_challenge=row["code_challenge"], redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=bool(row["redirect_uri_explicit"]),
            resource=row["resource"], subject=row["subject"],
        )

    def delete_code(self, code: str) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM codes WHERE code = ?", (code,))

    # -- tokens -------------------------------------------------------------

    def put_token_pair(self, *, access_token: str, refresh_token: str, client_id: str,
                       scopes: tuple[str, ...], resource: str | None, subject: str | None,
                       access_expires_at: int, refresh_expires_at: int) -> None:
        """Stored as a PAIR, cross-referencing each other's hash, so that
        revoking either one can revoke both -- which RFC 7009 says an
        implementation SHOULD do and which the provider protocol's own
        `revoke_token` docstring asks for explicitly."""
        access_hash, refresh_hash = _hash_token(access_token), _hash_token(refresh_token)
        scope_text = " ".join(scopes)
        with self._connection() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO tokens (token_hash, kind, client_id, scopes, resource, subject,"
                " expires_at, sibling_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (access_hash, "access", client_id, scope_text, resource, subject,
                     access_expires_at, refresh_hash),
                    (refresh_hash, "refresh", client_id, scope_text, resource, subject,
                     refresh_expires_at, access_hash),
                ],
            )

    def get_token(self, token: str, kind: str) -> sqlite3.Row | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM tokens WHERE token_hash = ? AND kind = ?", (_hash_token(token), kind),
            ).fetchone()
        if row is None or row["expires_at"] < int(time.time()):
            return None
        return row

    def revoke_token(self, token: str) -> None:
        token_hash = _hash_token(token)
        with self._connection() as conn:
            row = conn.execute("SELECT sibling_hash FROM tokens WHERE token_hash = ?", (token_hash,)).fetchone()
            hashes = [token_hash]
            if row is not None and row["sibling_hash"]:
                hashes.append(row["sibling_hash"])
            conn.executemany("DELETE FROM tokens WHERE token_hash = ?", [(h,) for h in hashes])

    def revoke_all_for_subject(self, subject: str) -> int:
        """Every grant held by one account. The operator-facing panic
        button -- `terminal-mcp-observer --revoke <user>` -- for a lost
        phone, where rotating the password alone would not help because
        an issued token does not consult it again."""
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM tokens WHERE subject = ?", (subject,))
            return cursor.rowcount or 0

    def purge_expired(self) -> None:
        now = time.time()
        with self._connection() as conn:
            conn.execute("DELETE FROM pending WHERE expires_at < ?", (now,))
            conn.execute("DELETE FROM codes WHERE expires_at < ?", (now,))
            conn.execute("DELETE FROM tokens WHERE expires_at < ?", (int(now),))

    def snapshot(self) -> dict[str, Any]:
        """Counts only -- never a token, never a hash. For the operator CLI."""
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT kind, COUNT(*) AS n, GROUP_CONCAT(DISTINCT subject) AS subjects"
                " FROM tokens WHERE expires_at >= ? GROUP BY kind", (int(time.time()),),
            ).fetchall()
            clients = conn.execute("SELECT COUNT(*) AS n FROM clients").fetchone()["n"]
        by_kind = {row["kind"]: {"count": row["n"], "subjects": (row["subjects"] or "").split(",")}
                   for row in rows}
        return {"registered_clients": clients, "tokens": by_kind}


class ObserverOAuthProvider:
    """The SDK's `OAuthAuthorizationServerProvider`, backed by
    `ObserverOAuthStore`. Deliberately NOT declared as inheriting the
    Protocol: it is structurally typed, and a stray `@override`-style
    mismatch should fail loudly at call time in a test rather than be
    silently accepted by a base class."""

    def __init__(self, store: ObserverOAuthStore, *, public_base_url: str) -> None:
        self.store = store
        # No trailing slash, so every f-string below composes one URL shape.
        self.public_base_url = public_base_url.rstrip("/")

    # -- clients ----------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.put_client(client_info)
        _log.info("observer-oauth: registered client %s (%s)",
                  client_info.client_id, client_info.client_name or "unnamed")

    # -- authorization ------------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the request and send the browser to the password page.

        No decision is made here -- this runs with no human present. The
        SDK has already validated the client and the redirect_uri by this
        point, so what is stored is known-good."""
        self.store.purge_expired()
        scopes = tuple(params.scopes or (OBSERVE_SCOPE,))
        unknown = [scope for scope in scopes if scope != OBSERVE_SCOPE]
        if unknown:
            # Fail rather than silently narrow: a client that thinks it
            # holds a scope this server never granted will make requests
            # on that belief.
            raise AuthorizeError(error="invalid_scope",
                                 error_description=f"unsupported scope(s): {' '.join(unknown)}")
        request_id = secrets.token_urlsafe(24)
        self.store.put_pending(PendingAuthorization(
            request_id=request_id, client_id=client.client_id, redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge, state=params.state, scopes=scopes,
            resource=params.resource,
        ))
        return f"{self.public_base_url}{LOGIN_PATH}?req={quote(request_id)}"

    def complete_authorization(self, pending: PendingAuthorization, *, subject: str) -> str:
        """Called by the login route once a password checked out: mint the
        code and build the redirect back to the client. Synchronous, and
        not part of the provider protocol -- the SDK never calls it."""
        code = AuthorizationCode(
            code=_new_token(), scopes=list(pending.scopes),
            expires_at=time.time() + AUTHORIZATION_CODE_TTL_SECONDS,
            client_id=pending.client_id, code_challenge=pending.code_challenge,
            redirect_uri=pending.redirect_uri,
            redirect_uri_provided_explicitly=pending.redirect_uri_provided_explicitly,
            resource=pending.resource, subject=subject,
        )
        self.store.put_code(code)
        _log.info("observer-oauth: issued authorization code to client %s for %s",
                  pending.client_id, subject)
        return construct_redirect_uri(str(pending.redirect_uri), code=code.code, state=pending.state)

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        code = self.store.get_code(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        # Single use. Deleted BEFORE the tokens are minted, so a code that
        # races with itself cannot produce two live grants.
        self.store.delete_code(authorization_code.code)
        return self._issue(
            client_id=client.client_id, scopes=tuple(authorization_code.scopes),
            resource=authorization_code.resource, subject=authorization_code.subject,
        )

    # -- refresh ------------------------------------------------------------

    async def load_refresh_token(self, client: OAuthClientInformationFull,
                                 refresh_token: str) -> RefreshToken | None:
        row = self.store.get_token(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token, client_id=row["client_id"],
            scopes=row["scopes"].split() if row["scopes"] else [],
            expires_at=int(row["expires_at"]), subject=row["subject"],
        )

    async def exchange_refresh_token(self, client: OAuthClientInformationFull,
                                     refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        requested = tuple(scopes) if scopes else tuple(refresh_token.scopes)
        widened = set(requested) - set(refresh_token.scopes)
        if widened:
            raise TokenError(error="invalid_scope",
                             error_description="refresh cannot widen scope beyond the original grant")
        # Read the stored row BEFORE revoking -- `resource` (RFC 8707) lives
        # only on the row, and revocation deletes it.
        stored = self.store.get_token(refresh_token.token, "refresh")
        resource = stored["resource"] if stored is not None else None
        # Rotation: the presented refresh token (and its paired access
        # token) stop working here, not when they expire.
        self.store.revoke_token(refresh_token.token)
        return self._issue(client_id=client.client_id, scopes=requested,
                           resource=resource, subject=refresh_token.subject)

    # -- verification / revocation -------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self.store.get_token(token, "access")
        if row is None:
            return None
        return AccessToken(
            token=token, client_id=row["client_id"],
            scopes=row["scopes"].split() if row["scopes"] else [],
            expires_at=int(row["expires_at"]), resource=row["resource"], subject=row["subject"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.revoke_token(token.token)
        _log.info("observer-oauth: revoked a grant for client %s", token.client_id)

    # -- internals ------------------------------------------------------------

    def _issue(self, *, client_id: str, scopes: tuple[str, ...],
               resource: str | None, subject: str | None) -> OAuthToken:
        access_token, refresh_token = _new_token(), _new_token()
        now = int(time.time())
        self.store.put_token_pair(
            access_token=access_token, refresh_token=refresh_token, client_id=client_id,
            scopes=scopes, resource=resource, subject=subject,
            access_expires_at=now + ACCESS_TOKEN_TTL_SECONDS,
            refresh_expires_at=now + REFRESH_TOKEN_TTL_SECONDS,
        )
        return OAuthToken(
            access_token=access_token, token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS, scope=" ".join(scopes),
            refresh_token=refresh_token,
        )


def observer_auth_settings(public_base_url: str) -> AuthSettings:
    """This server is its own authorization server AND its own resource
    server, so issuer and resource are the same public origin."""
    base = public_base_url.rstrip("/")
    return AuthSettings(
        issuer_url=base,  # type: ignore[arg-type] -- pydantic coerces the str
        resource_server_url=base,  # type: ignore[arg-type]
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[OBSERVE_SCOPE], default_scopes=[OBSERVE_SCOPE],
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=[OBSERVE_SCOPE],
    )


# -- the login page -----------------------------------------------------------
#
# Same visual language and the same Vietnamese copy as webauth_dashboard's
# /login, because it is the same operator logging in to the same host --
# just arriving from an OAuth redirect instead of from the dashboard.

_PAGE_STYLE = """
  :root { color-scheme: dark; --bg:#0b1020; --panel:#121a2d; --line:#26324b; --text:#eef2ff; --muted:#9aa7bd; --accent:#5b8cff; --err:#ff6b6b; }
  * { box-sizing:border-box }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         min-height:100vh; display:flex; align-items:center; justify-content:center; padding:24px }
  .card { width:100%; max-width:360px; background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:28px }
  h1 { font-size:18px; margin:0 0 4px }
  p.sub { color:var(--muted); font-size:13px; margin:0 0 20px }
  .grant { margin:0 0 18px; padding:10px 12px; border-radius:8px; background:rgba(91,140,255,.10);
           border:1px solid rgba(91,140,255,.35); color:var(--muted); font-size:13px }
  .grant b { color:var(--text) }
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

_GENERIC_LOGIN_ERROR = "Sai tên đăng nhập hoặc mật khẩu."
_EXPIRED_REQUEST_ERROR = (
    "Yêu cầu kết nối đã hết hạn hoặc không hợp lệ. Hãy bấm Connect lại từ Claude."
)


def _rate_limited_error(seconds: float) -> str:
    return f"Quá nhiều lần đăng nhập sai. Thử lại sau {int(seconds) + 1} giây."


def login_page_headers(redirect_uri: str) -> dict[str, str]:
    """Response headers for a page that CONTAINS the login form.

    The Content-Security-Policy is set here, per response, rather than
    left to SecurityHeadersMiddleware's global one, for exactly one
    directive: `form-action`. Submitting this form ends in a 303 to the
    OAuth client's own origin, and Chrome and Firefox both check
    form-action against the REDIRECT target as well as the immediate
    one -- so the middleware's `form-action 'self'` silently breaks the
    last hop of the flow, in the browser only. A smoke test driven by
    curl or urllib never sees it, because neither enforces CSP.

    Only the client's origin is added, taken from the redirect_uri the
    SDK already validated against that client's registration -- not a
    blanket `https:`.
    """
    parsed = urlsplit(redirect_uri)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    form_action = f"form-action 'self' {origin}".strip()
    csp = (
        "default-src 'self'; "
        "script-src 'none'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'; "
        f"{form_action}"
    )
    return {"Cache-Control": "no-store", "Content-Security-Policy": csp}


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _login_page_html(request_id: str, client_label: str, error: str = "") -> str:
    error_html = f'<div class="error">{_escape(error)}</div>' if error else ""
    return f"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kết nối Claude</title><style>{_PAGE_STYLE}</style></head>
<body><div class="card">
  <h1>Terminal MCP</h1>
  <p class="sub">Cho phép một ứng dụng Claude đọc trạng thái máy này.</p>
  <div class="grant">
    <b>{_escape(client_label)}</b> xin quyền <b>chỉ đọc</b>: danh sách session, output terminal,
    và nội dung repo trong thư mục được phép. Không gửi được phím, không sửa được gì.
  </div>
  <form method="POST" action="{LOGIN_PATH}">
    <input type="hidden" name="req" value="{_escape(request_id)}">
    <label for="username">Tên đăng nhập</label>
    <input type="text" id="username" name="username" autocomplete="username" required autofocus maxlength="128">
    <label for="password">Mật khẩu</label>
    <input type="password" id="password" name="password" autocomplete="current-password" required maxlength="256">
    <button type="submit">Cho phép</button>
  </form>
  {error_html}
</div></body></html>"""


def _expired_page_html() -> str:
    return f"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kết nối Claude</title><style>{_PAGE_STYLE}</style></head>
<body><div class="card">
  <h1>Terminal MCP</h1>
  <div class="error">{_EXPIRED_REQUEST_ERROR}</div>
</div></body></html>"""


def register_observer_login(server: Any, *, provider: ObserverOAuthProvider,
                            webauth: WebAuthStore) -> None:
    """Mount GET/POST `/observer/login` -- the human half of the OAuth flow.

    `webauth` is the same store the dashboard authenticates against, so
    there is exactly one set of accounts and one rate limiter for this
    host."""

    def _client_key(request: Request) -> str:
        # CF-Connecting-IP is Cloudflare's real-visitor-IP header, read
        # ONLY to bucket rate-limit counters -- never as an identity.
        # Behind the tunnel the TCP peer is always 127.0.0.1, which is a
        # valid if coarse fallback bucket. Same rule as webauth_dashboard.
        forwarded = request.headers.get("cf-connecting-ip")
        if forwarded:
            return forwarded.strip()[:64]
        client = request.client
        return client.host if client else "unknown"

    def _label_for(client_id: str) -> str:
        client = provider.store.get_client(client_id)
        if client is None:
            return "Ứng dụng Claude"
        return client.client_name or str(client.client_uri or "Ứng dụng Claude")

    @server.custom_route(LOGIN_PATH, methods=["GET"], include_in_schema=False)
    async def observer_login_page(request: Request):
        request_id = request.query_params.get("req", "")
        pending = provider.store.peek_pending(request_id) if request_id else None
        if pending is None:
            return HTMLResponse(_expired_page_html(), status_code=400,
                                headers={"Cache-Control": "no-store"})
        return HTMLResponse(_login_page_html(request_id, _label_for(pending.client_id)),
                            headers=login_page_headers(pending.redirect_uri))

    @server.custom_route(LOGIN_PATH, methods=["POST"], include_in_schema=False)
    async def observer_login_submit(request: Request):
        try:
            form = await request.form()
        except Exception:  # noqa: BLE001 -- a malformed body is just a failed login
            form = {}
        request_id = str(form.get("req") or "")
        pending = provider.store.peek_pending(request_id) if request_id else None
        if pending is None:
            return HTMLResponse(_expired_page_html(), status_code=400,
                                headers={"Cache-Control": "no-store"})

        client_key = _client_key(request)
        wait = await anyio.to_thread.run_sync(webauth.seconds_until_allowed, client_key)
        if wait > 0:
            return HTMLResponse(
                _login_page_html(request_id, _label_for(pending.client_id), _rate_limited_error(wait)),
                status_code=429, headers=login_page_headers(pending.redirect_uri))

        username = str(form.get("username") or "")[:128]
        password = str(form.get("password") or "")[:256]
        user = None
        if username and password:
            # scrypt is deliberately expensive -- off the event loop, same
            # as webauth_dashboard's own /login.
            user = await anyio.to_thread.run_sync(webauth.verify_password, username, password)
        if user is None:
            await anyio.to_thread.run_sync(webauth.record_failure, client_key)
            _log.info("observer-oauth: login failed username=%s client=%s app=%s",
                      username or "(empty)", client_key, pending.client_id)
            return HTMLResponse(
                _login_page_html(request_id, _label_for(pending.client_id), _GENERIC_LOGIN_ERROR),
                status_code=401, headers=login_page_headers(pending.redirect_uri))
        if user.must_change_password:
            # A bootstrap password must not be spendable into a long-lived
            # remote grant -- the dashboard forces the change first, and so
            # does this.
            return HTMLResponse(
                _login_page_html(request_id, _label_for(pending.client_id),
                                 "Tài khoản này phải đổi mật khẩu trên dashboard trước đã."),
                status_code=403, headers=login_page_headers(pending.redirect_uri))

        await anyio.to_thread.run_sync(webauth.record_success, client_key)
        # Consume the pending record only now that it is actually being
        # spent, so a mistyped password does not burn the connect attempt.
        consumed = provider.store.take_pending(request_id)
        if consumed is None:  # pragma: no cover -- lost a race with expiry
            return HTMLResponse(_expired_page_html(), status_code=400,
                                headers={"Cache-Control": "no-store"})
        redirect_to = provider.complete_authorization(consumed, subject=user.username)
        _log.info("observer-oauth: login succeeded username=%s client=%s app=%s",
                  user.username, client_key, consumed.client_id)
        return RedirectResponse(redirect_to, status_code=303,
                                headers={"Cache-Control": "no-store"})
