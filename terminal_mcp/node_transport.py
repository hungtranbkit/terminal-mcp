"""Which PATH the controller actually reaches a node over, and what to do
when the good one dies.

A node can be addressable more than one way at once -- a Tailscale
address, a LAN address, and a reverse-SSH port held open on the rescue
gateway. Before this module the controller kept exactly one `endpoint`
string per node in the registry, so "the node is unreachable" and "the
address we happened to write down six weeks ago is stale" were the same
observation, and a node with a perfectly healthy rescue tunnel still
read as offline.

What this adds:

  * A durable record per (node, transport kind) -- endpoint, health,
    last_success_at, last_failure_at, last_error. Health is a FACT that
    was measured, never a guess: nothing here marks a transport healthy
    because it "should" be.

  * `TransportResolver.resolve()`, which walks the kinds in preference
    order (tailscale -> lan -> reverse_ssh), probes each, and returns the
    FIRST one that answered, together with the full attempt list. A
    failure of every path returns `unreachable` with the per-path reason
    attached -- never a bare False, and never a silent fall-through to a
    path the operator has not trusted.

Trust rule: a transport row is only ever created by an authenticated
enrollment, by the node's own authenticated heartbeat, or by an operator
action. Nothing here learns a new address from an unauthenticated source
and then dials it -- that is how a failover mechanism becomes an SSRF.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .schema import Migration, apply_migrations

TRANSPORT_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: per-node transport records", lambda connection: None),
]

KIND_TAILSCALE = "tailscale"
KIND_LAN = "lan"
KIND_REVERSE_SSH = "reverse_ssh"
KINDS = (KIND_TAILSCALE, KIND_LAN, KIND_REVERSE_SSH)

# Preference order. Tailscale first (encrypted, NAT-traversing, direct),
# then plain LAN (fast but only works on-segment), then the rescue tunnel
# (always available but an extra hop through the gateway).
PREFERENCE = (KIND_TAILSCALE, KIND_LAN, KIND_REVERSE_SSH)

HEALTH_UNKNOWN = "unknown"
HEALTH_HEALTHY = "healthy"
HEALTH_FAILING = "failing"
HEALTH_DISABLED = "disabled"
HEALTHS = (HEALTH_UNKNOWN, HEALTH_HEALTHY, HEALTH_FAILING, HEALTH_DISABLED)


def default_transport_store_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_TRANSPORTS_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "transports.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Transport:
    node_id: str
    kind: str
    endpoint: str          # what a client is built from: "http://host:port" (agent) or "ssh://host:port"
    host: str | None
    port: int | None
    health: str
    last_success_at: str | None
    last_failure_at: str | None
    last_error: str | None
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "kind": self.kind, "endpoint": self.endpoint,
                "host": self.host, "port": self.port, "health": self.health,
                "last_success_at": self.last_success_at, "last_failure_at": self.last_failure_at,
                "last_error": self.last_error, "updated_at": self.updated_at}


class TransportStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_transport_store_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS node_transports (
                    node_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    host TEXT,
                    port INTEGER,
                    health TEXT NOT NULL DEFAULT 'unknown',
                    last_success_at TEXT,
                    last_failure_at TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (node_id, kind)
                )
                """
            )
            apply_migrations(connection, TRANSPORT_MIGRATIONS)
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

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

    def upsert(self, node_id: str, kind: str, *, endpoint: str, host: str | None = None,
               port: int | None = None, health: str | None = None) -> Transport:
        if kind not in KINDS:
            raise ValueError(f"unknown transport kind {kind!r}")
        if health is not None and health not in HEALTHS:
            raise ValueError(f"unknown health {health!r}")
        now = _now_iso()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO node_transports (node_id, kind, endpoint, host, port, health, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(node_id, kind) DO UPDATE SET
                       endpoint = excluded.endpoint, host = excluded.host, port = excluded.port,
                       health = COALESCE(?, node_transports.health), updated_at = excluded.updated_at""",
                (node_id, kind, endpoint, host, port, health or HEALTH_UNKNOWN, now, health),
            )
        result = self.get(node_id, kind)
        assert result is not None
        return result

    def record_result(self, node_id: str, kind: str, *, ok: bool, error: str | None = None) -> None:
        """The ONLY way health changes. A probe that succeeded stamps
        last_success_at; one that failed stamps last_failure_at AND the
        reason -- so "why is this node on the rescue path today" is
        answerable from the row rather than from a log grep."""
        now = _now_iso()
        with self._connection() as connection:
            if ok:
                connection.execute(
                    "UPDATE node_transports SET health = ?, last_success_at = ?, last_error = NULL, "
                    "updated_at = ? WHERE node_id = ? AND kind = ?",
                    (HEALTH_HEALTHY, now, now, node_id, kind))
            else:
                connection.execute(
                    "UPDATE node_transports SET health = ?, last_failure_at = ?, last_error = ?, "
                    "updated_at = ? WHERE node_id = ? AND kind = ?",
                    (HEALTH_FAILING, now, (error or "")[:500], now, node_id, kind))

    def set_health(self, node_id: str, kind: str, health: str) -> None:
        if health not in HEALTHS:
            raise ValueError(f"unknown health {health!r}")
        with self._connection() as connection:
            connection.execute("UPDATE node_transports SET health = ?, updated_at = ? WHERE node_id = ? AND kind = ?",
                               (health, _now_iso(), node_id, kind))

    def get(self, node_id: str, kind: str) -> Transport | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM node_transports WHERE node_id = ? AND kind = ?",
                                     (node_id, kind)).fetchone()
        return _row_to_transport(row) if row is not None else None

    def list_for(self, node_id: str) -> list[Transport]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM node_transports WHERE node_id = ?", (node_id,)).fetchall()
        order = {kind: index for index, kind in enumerate(PREFERENCE)}
        return sorted((_row_to_transport(row) for row in rows), key=lambda t: order.get(t.kind, 99))

    def list_all(self) -> list[Transport]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM node_transports ORDER BY node_id, kind").fetchall()
        return [_row_to_transport(row) for row in rows]

    def delete_node(self, node_id: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM node_transports WHERE node_id = ?", (node_id,))
        return cursor.rowcount


def _row_to_transport(row: sqlite3.Row) -> Transport:
    return Transport(node_id=row["node_id"], kind=row["kind"], endpoint=row["endpoint"],
                     host=row["host"], port=row["port"], health=row["health"],
                     last_success_at=row["last_success_at"], last_failure_at=row["last_failure_at"],
                     last_error=row["last_error"], updated_at=row["updated_at"])


# -- resolver ---------------------------------------------------------------

@dataclass(frozen=True)
class Attempt:
    kind: str
    endpoint: str
    ok: bool
    latency_ms: float | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "endpoint": self.endpoint, "ok": self.ok,
                "latency_ms": self.latency_ms, "error": self.error}


@dataclass(frozen=True)
class Resolution:
    """`kind`/`endpoint` are None exactly when `reachable` is False. There
    is no third state: a caller never has to guess whether an empty
    endpoint meant "not tried" or "tried and failed" -- `attempts` says
    what happened to every candidate."""
    node_id: str
    reachable: bool
    kind: str | None
    endpoint: str | None
    reason: str | None
    attempts: tuple[Attempt, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "reachable": self.reachable, "transport": self.kind,
                "endpoint": self.endpoint, "reason": self.reason,
                "attempts": [attempt.to_dict() for attempt in self.attempts]}


# Probe signature: (transport) -> (ok, error). Injected so the resolver is
# testable without a network and so the caller decides what "reachable"
# means for its own transport kind (an HTTP ping for the agent endpoint, a
# TCP connect for a rescue port).
Prober = Callable[[Transport], "tuple[bool, str | None]"]

REASON_NO_TRANSPORTS = "no_transports_recorded"
REASON_ALL_FAILED = "all_transports_failed"
REASON_ALL_DISABLED = "all_transports_disabled"


class TransportResolver:
    def __init__(self, store: TransportStore, *, preference: tuple[str, ...] = PREFERENCE) -> None:
        self.store = store
        self.preference = preference

    def candidates(self, node_id: str) -> list[Transport]:
        """Preference-ordered, with operator-disabled paths dropped. A
        disabled transport is never probed and never selected -- "không
        chuyển sang route không trust"."""
        order = {kind: index for index, kind in enumerate(self.preference)}
        rows = [t for t in self.store.list_for(node_id)
                if t.kind in order and t.health != HEALTH_DISABLED]
        return sorted(rows, key=lambda t: order[t.kind])

    def resolve(self, node_id: str, *, probe: Prober, record: bool = True) -> Resolution:
        # Scoped to this resolver's own preference set: a "Test Rescue" on
        # a node that has only a LAN transport recorded means "no rescue
        # transport", not "every transport is disabled". Those are
        # different problems with different fixes.
        all_rows = [t for t in self.store.list_for(node_id) if t.kind in self.preference]
        candidates = self.candidates(node_id)
        if not candidates:
            reason = REASON_ALL_DISABLED if all_rows else REASON_NO_TRANSPORTS
            return Resolution(node_id=node_id, reachable=False, kind=None, endpoint=None,
                              reason=reason, attempts=())
        attempts: list[Attempt] = []
        for transport in candidates:
            started = time.monotonic()
            try:
                ok, error = probe(transport)
            except Exception as exc:  # noqa: BLE001 -- a prober raising is a failed probe, not a crashed resolve
                ok, error = False, f"{type(exc).__name__}: {exc}"
            latency_ms = (time.monotonic() - started) * 1000.0
            attempts.append(Attempt(kind=transport.kind, endpoint=transport.endpoint, ok=ok,
                                    latency_ms=round(latency_ms, 2) if ok else None,
                                    error=None if ok else (error or "probe failed")))
            if record:
                self.store.record_result(node_id, transport.kind, ok=ok, error=None if ok else error)
            if ok:
                return Resolution(node_id=node_id, reachable=True, kind=transport.kind,
                                  endpoint=transport.endpoint, reason=None, attempts=tuple(attempts))
        return Resolution(node_id=node_id, reachable=False, kind=None, endpoint=None,
                          reason=REASON_ALL_FAILED, attempts=tuple(attempts))

# -- transport probes --------------------------------------------------
# What "reachable" actually means per transport kind, measured, never
# assumed. Both are pure-stdlib and bounded: a probe that hangs is a
# dashboard button that hangs.

GENERIC_SETUP_CODE = "TMCP-00000-00000-00000"


def probe_ssh_banner(host: str, port: int, *, timeout: float = 6.0) -> tuple[bool, str | None]:
    """A direct TCP connect that reads the SSH identification string.

    This, and not an HTTP ping, is the right primary-transport probe for a
    node onboarded by windows-setup.ps1: on the Minimal profile sshd IS
    the node's inbound surface -- there is no agent HTTP port to ping. It
    authenticates nothing (it cannot, and does not need to): it answers
    "is a real sshd listening at this address", which is exactly what
    "primary transport is up" means.
    """
    if not host:
        return False, "no address recorded"
    try:
        with socket.create_connection((host, int(port or 22)), timeout=timeout) as connection:
            connection.settimeout(timeout)
            banner = connection.recv(255)
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if banner.startswith(b"SSH-"):
        return True, None
    return False, f"not an SSH service (got {banner[:32]!r})"


def probe_reverse_tunnel(gateway, reverse_port: int, *,
                         runner=subprocess.run, timeout: float = 20.0) -> tuple[bool, str | None]:
    """Is the node's reverse tunnel actually alive on the gateway?

    Connects THROUGH the gateway (ProxyJump) to 127.0.0.1:<reverse_port>
    and reads how far it gets. Authentication to the node is expected to
    fail -- that is fine and is the point: reaching a real sshd there
    proves the node is holding the forward open, while "channel open
    failed"/"connection refused" proves it is not. Classifying the two is
    what makes this a real test rather than a ping of the gateway.

    Requires the controller to have SSH access to the gateway. Where it
    does not, that is reported as its own reason rather than as a dead
    tunnel -- the two are very different problems.
    """
    if not gateway.configured:
        return False, gateway.reason
    if shutil.which("ssh") is None:
        return False, "ssh client not installed on the controller"
    argv = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ProxyJump={gateway.user}@{gateway.host}:{gateway.port}",
        "-o", "PreferredAuthentications=none",
        "-p", str(int(reverse_port)),
        "terminal-mcp-probe@127.0.0.1", "true",
    ]
    try:
        completed = runner(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    stderr = (completed.stderr or "").strip()
    lowered = stderr.lower()
    # Reached a real sshd on the far end of the tunnel.
    if "permission denied" in lowered or "no supported authentication" in lowered \
            or "authentication method" in lowered or "connection closed by remote host" in lowered:
        return True, None
    if "channel" in lowered and "open failed" in lowered:
        return False, "gateway accepted us but nothing is listening on the reverse port -- the node's tunnel is down"
    if "connection refused" in lowered:
        return False, "the reverse port is not open on the gateway -- the node's tunnel is down"
    if "could not resolve" in lowered or "no route to host" in lowered or "connection timed out" in lowered:
        return False, f"gateway unreachable: {stderr.splitlines()[0] if stderr else 'timeout'}"
    if completed.returncode == 0:
        return True, None
    return False, (stderr.splitlines()[0] if stderr else f"ssh exited {completed.returncode}")
