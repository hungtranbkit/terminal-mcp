"""Node bearer tokens that can be rotated and revoked.

blg_a3cc401d8275. Today a node has exactly one token, with no identity, no
version and no revocation path. Rotating it means hand-editing two files
on two machines and restarting the node -- done for real on 2026-09-09,
when `macbook` was rotated and `dell-5530` was deferred because its
restart would have cost six live sessions. A credential you cannot afford
to rotate is one you cannot revoke either.

What this adds
--------------
A durable record per token rather than a single opaque string:

    node_id  token_id  status   created_at  activated_at  expires_at  revoked_at

`token_id` is a short fingerprint derived from the token itself, so a log
line, an audit row or an operator can refer to *which* token without ever
naming it. Three states:

    ACTIVE    the current token; exactly one per node
    GRACE     the previous token, still accepted until expires_at
    REVOKED   refused forever, immediately

The GRACE window is what makes rotation possible without a restart. The
controller issues a new token, the node picks it up on its own schedule,
and the old one keeps working in between. Without it, rotation is
simultaneous-or-broken, which is exactly why dell-5530 was deferred.

Two directions, two different needs
-----------------------------------
INBOUND (node -> controller: heartbeat, deregister) only needs to VERIFY,
so this store keeps **sha256 hashes and never plaintext**. A stolen
credentials.db cannot be replayed.

OUTBOUND (controller -> node agent) must PRESENT the token, so the
plaintext stays where it already lives: connection_store's 0600 token
file. This module does not duplicate or replace that, and deliberately
offers no way to read a token back.

Fail-closed
-----------
`verify` returns a verdict, never a bare bool. A token that matches a
REVOKED row is `REVOKED`, not "no match" -- the distinction matters
because a revoked credential in active use is an incident, not a typo.
A token that matches nothing is `UNKNOWN`. An expired GRACE row is
`EXPIRED`. Only ACTIVE and unexpired GRACE return `OK`.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .schema import Migration, apply_migrations

CREDENTIAL_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: versioned, revocable node credentials", lambda connection: None),
]

ACTIVE = "ACTIVE"
GRACE = "GRACE"
REVOKED = "REVOKED"
STATUSES = (ACTIVE, GRACE, REVOKED)

# verify() verdicts
OK = "OK"
UNKNOWN = "UNKNOWN"
EXPIRED = "EXPIRED"
VERDICTS = (OK, UNKNOWN, EXPIRED, REVOKED)

DEFAULT_GRACE_SECONDS = 900  # 15 minutes: long enough for a node to notice, short enough to matter

_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def default_credentials_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_NODE_CREDENTIALS_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "node-credentials.db"


def token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def token_fingerprint(token: str) -> str:
    """A short, stable, NON-reversible id for one token.

    Derived from the hash rather than the token, so it can appear in logs,
    audit rows and operator conversation without leaking anything: 12 hex
    characters of sha256 identifies which credential is meant while being
    useless for authenticating as it."""
    return token_hash(token)[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


@dataclass(frozen=True)
class Credential:
    """One token record. Carries no secret -- there is no field here that
    could leak one, which is what makes it safe to log or return."""
    node_id: str
    token_id: str
    status: str
    created_at: str
    activated_at: str | None
    expires_at: str | None
    revoked_at: str | None
    revoked_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "token_id": self.token_id, "status": self.status,
                "created_at": self.created_at, "activated_at": self.activated_at,
                "expires_at": self.expires_at, "revoked_at": self.revoked_at,
                "revoked_reason": self.revoked_reason}


@dataclass(frozen=True)
class VerifyResult:
    verdict: str
    token_id: str | None = None
    detail: str = ""

    @property
    def accepted(self) -> bool:
        return self.verdict == OK

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "token_id": self.token_id, "detail": self.detail,
                "accepted": self.accepted}


class NodeCredentialStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_credentials_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS node_credentials (
                    token_hash TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    activated_at TEXT,
                    expires_at TEXT,
                    revoked_at TEXT,
                    revoked_reason TEXT
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_credentials_node "
                               "ON node_credentials(node_id, status)")
            apply_migrations(connection, CREDENTIAL_MIGRATIONS)
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @contextlib.contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    # -- issuance ---------------------------------------------------------

    def adopt(self, node_id: str, token: str, *, now: datetime | None = None) -> Credential:
        """Bring an EXISTING token under management without changing it.

        Backward compatibility: every node enrolled before this store
        existed has a working token in connection_store and an env var.
        Adopting records it as ACTIVE so it keeps working and becomes
        rotatable, rather than forcing a flag day. Idempotent -- adopting
        the same token twice returns the same record."""
        node_id = self._validate(node_id)
        moment = now or _now()
        digest = token_hash(token)
        existing = self._by_hash(digest)
        if existing is not None:
            return existing
        return self._insert(node_id, token, status=ACTIVE, moment=moment, activated=True)

    def rotate(self, node_id: str, *, grace_seconds: int = DEFAULT_GRACE_SECONDS,
               now: datetime | None = None, new_token: str | None = None) -> tuple[Credential, str]:
        """Mint a new ACTIVE token and move the current one to GRACE.

        Returns (record, plaintext). The plaintext is returned HERE and
        nowhere else -- the store keeps only its hash, so a caller that
        loses it must rotate again rather than look it up.

        The grace window is the whole point: the node keeps authenticating
        with its old token until it picks up the new one. Set
        grace_seconds=0 for an immediate cutover when the old token is
        believed compromised.
        """
        node_id = self._validate(node_id)
        moment = now or _now()
        token = new_token or secrets.token_hex(32)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Demote the current ACTIVE token. A second rotation while
                # a GRACE token already exists revokes that older one --
                # two grace windows at once would widen the accepted set
                # every time somebody clicked rotate.
                connection.execute(
                    "UPDATE node_credentials SET status = ?, revoked_at = ?, revoked_reason = ? "
                    "WHERE node_id = ? AND status = ?",
                    (REVOKED, _iso(moment), "superseded_by_newer_rotation", node_id, GRACE))
                if grace_seconds > 0:
                    connection.execute(
                        "UPDATE node_credentials SET status = ?, expires_at = ? "
                        "WHERE node_id = ? AND status = ?",
                        (GRACE, _iso(moment + timedelta(seconds=grace_seconds)), node_id, ACTIVE))
                else:
                    connection.execute(
                        "UPDATE node_credentials SET status = ?, revoked_at = ?, revoked_reason = ? "
                        "WHERE node_id = ? AND status = ?",
                        (REVOKED, _iso(moment), "immediate_rotation", node_id, ACTIVE))
                connection.execute(
                    "INSERT INTO node_credentials (token_hash, node_id, token_id, status, created_at, activated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (token_hash(token), node_id, token_fingerprint(token), ACTIVE,
                     _iso(moment), _iso(moment)))
                connection.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise
        record = self._by_hash(token_hash(token))
        assert record is not None
        return record, token

    # -- revocation -------------------------------------------------------

    def revoke(self, node_id: str, *, token_id: str | None = None, reason: str = "manual_revoke",
               now: datetime | None = None) -> list[Credential]:
        """Refuse a credential from this moment on.

        With no token_id, revokes EVERY non-revoked token for the node --
        which is what "revoke this node" means and is the safe default.
        With one, revokes just that token, so a leaked old credential can
        be killed without disturbing the node's current one.

        Idempotent: revoking an already-revoked token changes nothing and
        returns the record as it stands."""
        node_id = self._validate(node_id)
        moment = now or _now()
        with self._connection() as connection:
            if token_id:
                connection.execute(
                    "UPDATE node_credentials SET status = ?, revoked_at = ?, revoked_reason = ? "
                    "WHERE node_id = ? AND token_id = ? AND status != ?",
                    (REVOKED, _iso(moment), reason, node_id, token_id, REVOKED))
                rows = connection.execute(
                    "SELECT * FROM node_credentials WHERE node_id = ? AND token_id = ?",
                    (node_id, token_id)).fetchall()
            else:
                connection.execute(
                    "UPDATE node_credentials SET status = ?, revoked_at = ?, revoked_reason = ? "
                    "WHERE node_id = ? AND status != ?",
                    (REVOKED, _iso(moment), reason, node_id, REVOKED))
                rows = connection.execute(
                    "SELECT * FROM node_credentials WHERE node_id = ?", (node_id,)).fetchall()
        return [_row_to_credential(row) for row in rows]

    # -- verification -----------------------------------------------------

    def verify(self, node_id: str, token: str, *, now: datetime | None = None) -> VerifyResult:
        """Fail-closed, and specific about WHY.

        A revoked token in active use is an incident and must be
        distinguishable from a typo, so REVOKED is its own verdict rather
        than folded into UNKNOWN. Matching is by hash and the final
        comparison is constant-time."""
        moment = now or _now()
        if not token:
            return VerifyResult(UNKNOWN, None, "no token presented")
        digest = token_hash(token)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM node_credentials WHERE token_hash = ?", (digest,)).fetchone()
        if row is None:
            return VerifyResult(UNKNOWN, None, "token is not recognised for any node")
        # A token belonging to a DIFFERENT node is not a partial match --
        # it is unknown for this one, and saying otherwise would confirm
        # that the token is valid somewhere.
        if not hmac.compare_digest(str(row["node_id"]), str(node_id)):
            return VerifyResult(UNKNOWN, None, "token is not recognised for any node")
        token_id = row["token_id"]
        if row["status"] == REVOKED:
            return VerifyResult(REVOKED, token_id,
                                f"token {token_id} was revoked ({row['revoked_reason'] or 'no reason recorded'})")
        if row["status"] == GRACE:
            if row["expires_at"] and _iso(moment) >= row["expires_at"]:
                return VerifyResult(EXPIRED, token_id, f"grace window for token {token_id} has closed")
            return VerifyResult(OK, token_id, f"token {token_id} accepted during its grace window")
        if row["status"] == ACTIVE:
            return VerifyResult(OK, token_id, f"token {token_id} is active")
        # An unrecognised status is ambiguous, and ambiguous means refuse.
        return VerifyResult(UNKNOWN, token_id, f"token {token_id} has an unrecognised status")

    # -- read-only views ---------------------------------------------------

    def list_for(self, node_id: str) -> list[Credential]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM node_credentials WHERE node_id = ? ORDER BY created_at DESC",
                (node_id,)).fetchall()
        return [_row_to_credential(row) for row in rows]

    def active_token_id(self, node_id: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT token_id FROM node_credentials WHERE node_id = ? AND status = ?",
                (node_id, ACTIVE)).fetchone()
        return row["token_id"] if row else None

    def is_managed(self, node_id: str) -> bool:
        """Whether this node has any credential record at all. False means
        fall back to the legacy env-var check -- see the dashboard's
        verification site."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM node_credentials WHERE node_id = ? LIMIT 1", (node_id,)).fetchone()
        return row is not None

    def expire_grace(self, *, now: datetime | None = None) -> int:
        """Move GRACE rows past their window to REVOKED. Housekeeping only
        -- verify() already refuses them, so this never changes an auth
        outcome, it just stops the table implying they are live."""
        moment = now or _now()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE node_credentials SET status = ?, revoked_at = ?, revoked_reason = ? "
                "WHERE status = ? AND expires_at IS NOT NULL AND expires_at <= ?",
                (REVOKED, _iso(moment), "grace_window_expired", GRACE, _iso(moment)))
        return cursor.rowcount

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _validate(node_id: str) -> str:
        candidate = str(node_id or "").strip()
        if not _NODE_ID_RE.match(candidate):
            raise ValueError(f"invalid node_id {node_id!r}")
        return candidate

    def _by_hash(self, digest: str) -> Credential | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM node_credentials WHERE token_hash = ?", (digest,)).fetchone()
        return _row_to_credential(row) if row else None

    def _insert(self, node_id: str, token: str, *, status: str, moment: datetime,
                activated: bool) -> Credential:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO node_credentials (token_hash, node_id, token_id, status, created_at, activated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (token_hash(token), node_id, token_fingerprint(token), status, _iso(moment),
                 _iso(moment) if activated else None))
        record = self._by_hash(token_hash(token))
        assert record is not None
        return record


def _row_to_credential(row: sqlite3.Row) -> Credential:
    return Credential(node_id=row["node_id"], token_id=row["token_id"], status=row["status"],
                      created_at=row["created_at"], activated_at=row["activated_at"],
                      expires_at=row["expires_at"], revoked_at=row["revoked_at"],
                      revoked_reason=row["revoked_reason"])
