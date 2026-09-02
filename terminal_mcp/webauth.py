"""Local username/password authentication for the dashboard's alternate,
non-Cloudflare-Access entry point (/login, /app/*) -- see webauth_dashboard.py
for the routes this backs, and README's "Password login" section for the
full operational picture. This module is deliberately narrow: one local
account store, real session cookies, and non-permanent rate limiting --
no registration, no RBAC, no email/password-reset flow. It never widens
what the existing Cloudflare-Access-gated /dashboard path can do, and the
two paths share no session/cookie state at all.

Password hashing uses the standard library's hashlib.scrypt (OpenSSL-
backed, no extra dependency) -- a real, memory-hard KDF, never a bespoke
scheme and never plaintext. Session tokens are high-entropy random values
(secrets.token_urlsafe); only their SHA-256 hash is ever stored, so a
leaked database dump alone cannot be replayed as a live session cookie
(same "hash what you store" posture the token deliberately avoids
needing an HMAC secret for, since the token itself is already
unguessable).
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .schema import Migration, apply_migrations

WEBAUTH_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: webauth_users + webauth_sessions + webauth_login_attempts", lambda connection: None),
]

SESSION_TTL = timedelta(hours=12)
SESSION_COOKIE_NAME = "terminal_mcp_session"

# scrypt cost parameters -- N=2**14 (16384), r=8, p=1 is the OWASP-cited
# "interactive login" baseline (roughly RFC 7914's own recommended
# minimum), sized for a single local operator login, not a
# high-throughput multi-tenant service.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN = 2**14, 8, 1, 32
_SALT_BYTES = 16
_SESSION_TOKEN_BYTES = 32

# Rate limiting: non-permanent, exponential backoff past a small
# threshold of consecutive failures, capped at a bounded ceiling -- never
# a lasting lockout an operator would need to manually clear.
RATE_LIMIT_THRESHOLD = 5
RATE_LIMIT_BACKOFF_BASE_SECONDS = 5.0
RATE_LIMIT_BACKOFF_CAP_SECONDS = 900.0  # 15 minutes


def default_webauth_db_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_WEBAUTH_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "webauth.db"


def _hash_password(password: str, *, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt if salt is not None else secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
    )
    return digest, salt


def _verify_password(password: str, digest: bytes, salt: bytes) -> bool:
    candidate, _ = _hash_password(password, salt=salt)
    return hmac.compare_digest(candidate, digest)


@dataclass(frozen=True)
class WebAuthUser:
    username: str
    must_change_password: bool


class WebAuthStore:
    """Same connection-per-call/WAL/0600 pattern every other durable store
    in this project already uses (grants.py/lease.py/bindings.py)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_webauth_db_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS webauth_users (
                    username TEXT PRIMARY KEY,
                    password_hash BLOB NOT NULL,
                    password_salt BLOB NOT NULL,
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS webauth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    last_used_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS webauth_login_attempts (
                    client_key TEXT PRIMARY KEY,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_failure_at TEXT,
                    locked_until TEXT
                )
                """
            )
            apply_migrations(connection, WEBAUTH_MIGRATIONS)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextlib.contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # -- users --------------------------------------------------------

    def has_any_user(self) -> bool:
        with self._connection() as connection:
            row = connection.execute("SELECT 1 FROM webauth_users LIMIT 1").fetchone()
        return row is not None

    def create_or_replace_user(self, username: str, password: str, *, must_change_password: bool = False) -> None:
        digest, salt = _hash_password(password)
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO webauth_users
                   (username, password_hash, password_salt, must_change_password, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(username) DO UPDATE SET
                     password_hash = excluded.password_hash, password_salt = excluded.password_salt,
                     must_change_password = excluded.must_change_password, updated_at = excluded.updated_at""",
                (username, digest, salt, int(must_change_password), now, now),
            )

    def set_password(self, username: str, password: str) -> bool:
        """Returns False if the username does not exist -- callers (the
        local CLI) should create the account first rather than silently
        no-op through this."""
        digest, salt = _hash_password(password)
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE webauth_users SET password_hash=?, password_salt=?, must_change_password=0, updated_at=? "
                "WHERE username=?",
                (digest, salt, now, username),
            )
        if cursor.rowcount == 1:
            self.destroy_all_sessions_for(username)  # a password change invalidates every existing session
            return True
        return False

    def verify_password(self, username: str, password: str) -> WebAuthUser | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT username, password_hash, password_salt, must_change_password FROM webauth_users "
                "WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            # Still pay the same scrypt cost on an unknown username --
            # a cheap, real timing-side-channel hardening step, not a
            # correctness requirement.
            _hash_password(password)
            return None
        if not _verify_password(password, row["password_hash"], row["password_salt"]):
            return None
        return WebAuthUser(username=row["username"], must_change_password=bool(row["must_change_password"]))

    # -- sessions -------------------------------------------------------

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_session(self, username: str, *, ttl: timedelta = SESSION_TTL) -> str:
        token = secrets.token_urlsafe(_SESSION_TOKEN_BYTES)
        now = datetime.now(timezone.utc)
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO webauth_sessions (token_hash, username, created_at, expires_at, last_used_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (self._hash_token(token), username, now.isoformat(), (now + ttl).isoformat(), now.isoformat()),
            )
        return token

    def resolve_session(self, token: str) -> WebAuthUser | None:
        if not token:
            return None
        token_hash = self._hash_token(token)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT s.username, s.expires_at, u.must_change_password FROM webauth_sessions s "
                "JOIN webauth_users u ON u.username = s.username WHERE s.token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
                connection.execute("DELETE FROM webauth_sessions WHERE token_hash = ?", (token_hash,))
                return None
            connection.execute(
                "UPDATE webauth_sessions SET last_used_at = ? WHERE token_hash = ?",
                (datetime.now(timezone.utc).isoformat(), token_hash),
            )
        return WebAuthUser(username=row["username"], must_change_password=bool(row["must_change_password"]))

    def destroy_session(self, token: str) -> None:
        if not token:
            return
        with self._connection() as connection:
            connection.execute("DELETE FROM webauth_sessions WHERE token_hash = ?", (self._hash_token(token),))

    def destroy_all_sessions_for(self, username: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM webauth_sessions WHERE username = ?", (username,))

    def purge_expired_sessions(self) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM webauth_sessions WHERE expires_at < ?", (datetime.now(timezone.utc).isoformat(),)
            )

    # -- rate limiting, keyed by caller-supplied client_key (remote IP) --

    def seconds_until_allowed(self, client_key: str) -> float:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT locked_until FROM webauth_login_attempts WHERE client_key = ?", (client_key,)
            ).fetchone()
        if row is None or row["locked_until"] is None:
            return 0.0
        remaining = (datetime.fromisoformat(row["locked_until"]) - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, remaining)

    def record_failure(self, client_key: str) -> None:
        now = datetime.now(timezone.utc)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT failure_count FROM webauth_login_attempts WHERE client_key = ?", (client_key,)
            ).fetchone()
            count = (row["failure_count"] if row is not None else 0) + 1
            locked_until = None
            if count >= RATE_LIMIT_THRESHOLD:
                backoff = min(
                    RATE_LIMIT_BACKOFF_CAP_SECONDS,
                    RATE_LIMIT_BACKOFF_BASE_SECONDS * (2 ** (count - RATE_LIMIT_THRESHOLD)),
                )
                locked_until = (now + timedelta(seconds=backoff)).isoformat()
            connection.execute(
                """INSERT INTO webauth_login_attempts (client_key, failure_count, last_failure_at, locked_until)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(client_key) DO UPDATE SET
                     failure_count = excluded.failure_count, last_failure_at = excluded.last_failure_at,
                     locked_until = excluded.locked_until""",
                (client_key, count, now.isoformat(), locked_until),
            )

    def record_success(self, client_key: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM webauth_login_attempts WHERE client_key = ?", (client_key,))
