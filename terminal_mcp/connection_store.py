"""Durable metadata for a node connected via the LAN-discovery/remote-
connect feature -- one row per node telling us HOW to reach/re-establish
it: `transport_type` (lan_ssh / cloudflare_ssh / agent_token / manual),
`hostname`/`username`/`port` (SSH-based transports only), and
`host_key_fingerprint` (SSH host-key pin -- see remote_connect.py's own
pinning docstring for why this is never silently updated).

What this NEVER stores, on purpose, matching every other store in this
project's own "no plaintext secret on disk" discipline (bindings.py/
grants.py/lease.py/node_registry.py's own `auth_token_ref` being a
reference string, never the secret itself): the SSH password/private
key/passphrase used to bootstrap a node (used once, in-memory, for the
duration of the bootstrap subprocess call, then discarded -- see
remote_connect.py), and the node-agent's own bearer token (written
instead to a 0600 file under connection_store.py's own tokens/
subdirectory, referenced here only by `token_file` PATH). A restart of
this controller process loses the in-memory NodeClient for a connected
node (same limitation config.yaml-declared remote nodes already have --
see controller.py's ControllerService.__init__), but THIS row (plus the
token file) is enough to re-establish it with one click, no credentials
re-entered -- see dashboard.py's node-reconnect route.
"""
from __future__ import annotations

import contextlib
import os
import re
import secrets
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .schema import Migration, apply_migrations

CONNECTION_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: connections table for the LAN-discovery/remote-connect feature",
             lambda connection: None),
]

TRANSPORT_LAN_SSH = "lan_ssh"
TRANSPORT_CLOUDFLARE_SSH = "cloudflare_ssh"
TRANSPORT_AGENT_TOKEN = "agent_token"
TRANSPORT_MANUAL = "manual"
TRANSPORTS = (TRANSPORT_LAN_SSH, TRANSPORT_CLOUDFLARE_SSH, TRANSPORT_AGENT_TOKEN, TRANSPORT_MANUAL)

_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def default_connection_store_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_CONNECTIONS_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "connections.db"


@dataclass(frozen=True)
class Connection:
    node_id: str
    transport_type: str
    endpoint: str  # the node-agent's own http(s)://host:port -- what RemoteNodeClient is built from
    hostname: str | None
    username: str | None
    port: int | None
    host_key_fingerprint: str | None
    token_file: str | None  # path to a 0600 file holding the bearer token -- never the secret itself
    created_at: str
    updated_at: str


class ConnectionStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_connection_store_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.tokens_dir = self.path.parent / "node-tokens"
        self.tokens_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._initialize()

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

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS connections (
                    node_id TEXT PRIMARY KEY,
                    transport_type TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    hostname TEXT,
                    username TEXT,
                    port INTEGER,
                    host_key_fingerprint TEXT,
                    token_file TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            apply_migrations(connection, CONNECTION_MIGRATIONS)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> Connection | None:
        if row is None:
            return None
        return Connection(
            node_id=row["node_id"], transport_type=row["transport_type"], endpoint=row["endpoint"],
            hostname=row["hostname"], username=row["username"], port=row["port"],
            host_key_fingerprint=row["host_key_fingerprint"], token_file=row["token_file"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def get(self, node_id: str) -> Connection | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM connections WHERE node_id = ?", (node_id,)).fetchone()
        return self._from_row(row)

    def list(self) -> list[Connection]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM connections ORDER BY node_id").fetchall()
        return [c for row in rows if (c := self._from_row(row)) is not None]

    def save(self, node_id: str, *, transport_type: str, endpoint: str, hostname: str | None = None,
             username: str | None = None, port: int | None = None,
             host_key_fingerprint: str | None = None, token_file: str | None = None) -> Connection:
        if transport_type not in TRANSPORTS:
            raise ValueError(f"unknown transport_type {transport_type!r}")
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO connections (node_id, transport_type, endpoint, hostname, username, port,
                                            host_key_fingerprint, token_file, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    transport_type = excluded.transport_type, endpoint = excluded.endpoint,
                    hostname = excluded.hostname, username = excluded.username, port = excluded.port,
                    host_key_fingerprint = excluded.host_key_fingerprint, token_file = excluded.token_file,
                    updated_at = excluded.updated_at""",
                (node_id, transport_type, endpoint, hostname, username, port,
                 host_key_fingerprint, token_file, now, now),
            )
        return self.get(node_id)  # type: ignore[return-value]

    def delete(self, node_id: str) -> bool:
        connection_row = self.get(node_id)
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM connections WHERE node_id = ?", (node_id,))
        if connection_row and connection_row.token_file:
            with contextlib.suppress(OSError):
                Path(connection_row.token_file).unlink()
        return cursor.rowcount == 1

    # -- token file management ------------------------------------------------
    # A node-agent bearer token is written here, 0600, never inside the
    # sqlite row itself (same "reference, not the secret" posture as
    # node_registry.py's own auth_token_ref column) -- see this module's
    # own docstring for the full rationale.

    def write_token(self, node_id: str, token: str) -> str:
        if not _NODE_ID_RE.match(node_id):
            raise ValueError(f"invalid node_id {node_id!r}")
        target = self.tokens_dir / f"{node_id}.token"
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, token.encode())
        finally:
            os.close(fd)
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
        return str(target)

    def read_token(self, token_file: str) -> str | None:
        try:
            return Path(token_file).read_text().strip()
        except OSError:
            return None


def generate_node_token() -> str:
    """One place this feature generates a fresh bearer token -- same
    entropy/format as dashboard.py's existing node_generate_onboarding
    route (secrets.token_hex), so a discovery-connected node's token is
    exactly as strong as a manually-onboarded one."""
    return secrets.token_hex(32)
