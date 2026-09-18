"""The RESCUE transport: a persistent reverse-SSH tunnel a node opens
OUT to a gateway host the controller owns, so the node stays reachable
when its primary path (Tailscale, or the LAN) is down -- and with no
router/NAT/firewall change on the node's side, ever.

    node (Windows)                 gateway VPS                controller
    sshd :22  <--- -R 127.0.0.1:P:127.0.0.1:22 --->  :P  <--- ssh -J ---

Two hard rules this module enforces rather than documents:

  1. The reverse listener binds 127.0.0.1 ON THE GATEWAY, never 0.0.0.0.
     A reverse port that binds the wildcard address publishes every
     enrolled node's SSH port straight onto the public internet. The
     argv built here always spells the bind address out (`-R
     127.0.0.1:P:127.0.0.1:22`) instead of the `-R P:host:22` short
     form, which inherits the gateway's own GatewayPorts setting -- i.e.
     it would silently become world-reachable the day someone flips that
     sshd option. Belt and braces: the authorized_keys line generated
     for the gateway pins `permitlisten="127.0.0.1:P"`, so sshd itself
     refuses any other forward from that key even if the node were
     compromised and asked for one.

  2. Port allocation is PERSISTENT and COLLISION-FREE. `port` carries a
     UNIQUE constraint and every allocation happens inside a BEGIN
     IMMEDIATE transaction, so two nodes enrolling at the same instant
     can never be handed the same port -- one of them loses the insert
     race and retries against the freshly-committed state. Allocation is
     idempotent per node: re-running the installer, or repairing a node,
     returns the port it already owns rather than leaking a new one.

Host-key trust: the node NEVER runs with StrictHostKeyChecking=no. The
controller ships the gateway's own public host key line (a PUBLIC value,
safe to put in a bootstrap payload) and the installer writes it to a
dedicated known_hosts file used only for this tunnel; the ssh invocation
pins `StrictHostKeyChecking=yes` plus `UserKnownHostsFile` at that file.
A gateway whose host key changes hard-fails the tunnel instead of
silently trusting a new one.

Safe disabled state: with `nodes.onboarding.rescue.enabled: false` (the
default, and the correct setting until an operator has actually stood a
gateway up), every function here still works -- `describe()` reports
`configured=False` with the exact reason, allocation is skipped, and the
installer prints "Rescue: not configured by the server" instead of
pretending. Nothing in this module invents a hostname or a credential.
"""
from __future__ import annotations

import contextlib
import os
import re
import shlex
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import Migration, apply_migrations

RESCUE_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: reverse-SSH rescue port allocations", lambda connection: None),
]

# Reasons describe() gives when the gateway is not usable. Each one maps
# to a specific line of admin setup in docs/windows-node-onboarding.md.
REASON_DISABLED = "rescue_disabled"
REASON_NO_HOST = "gateway_host_not_configured"
REASON_NO_USER = "gateway_user_not_configured"
REASON_NO_HOST_KEY = "gateway_host_key_not_configured"
REASON_BAD_PORT_RANGE = "gateway_port_range_invalid"
REASON_EXHAUSTED = "gateway_port_range_exhausted"

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,253}[A-Za-z0-9])?$")
_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# One known_hosts entry: "<host-or-pattern> <keytype> <base64>". Never a
# private key -- validated so a misconfiguration that pasted one in is
# caught here instead of shipped to a node.
_HOST_KEY_RE = re.compile(r"^\S+\s+(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521))\s+[A-Za-z0-9+/=]{32,}\s*$")


def default_rescue_store_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_RESCUE_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "rescue.db"


@dataclass(frozen=True)
class RescueAllocation:
    node_id: str
    port: int
    allocated_at: str
    released_at: str | None
    public_key: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "port": self.port, "allocated_at": self.allocated_at,
                "released_at": self.released_at, "has_public_key": bool(self.public_key)}


class RescuePortAllocator:
    def __init__(self, path: str | Path | None = None, *, port_range: tuple[int, int] = (22000, 22999)) -> None:
        self.path = Path(path) if path is not None else default_rescue_store_path()
        self.port_range = port_range
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rescue_ports (
                    node_id TEXT PRIMARY KEY,
                    port INTEGER NOT NULL UNIQUE,
                    allocated_at TEXT NOT NULL,
                    released_at TEXT,
                    public_key TEXT
                )
                """
            )
            apply_migrations(connection, RESCUE_MIGRATIONS)
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @contextlib.contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def allocate(self, node_id: str, *, public_key: str | None = None,
                 now: datetime | None = None) -> RescueAllocation:
        """Idempotent per node. Raises RuntimeError(REASON_EXHAUSTED) when
        every port in the range is taken -- never silently reuses a port
        that belongs to another node."""
        if not _NODE_ID_RE.match(node_id or ""):
            raise ValueError(f"invalid node_id {node_id!r}")
        low, high = self.port_range
        if low < 1024 or high > 65535 or low > high:
            raise ValueError(REASON_BAD_PORT_RANGE)
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT * FROM rescue_ports WHERE node_id = ?", (node_id,)).fetchone()
                if row is not None:
                    # Re-running the installer or a repair: same port, and
                    # the (possibly rotated) public key is refreshed.
                    connection.execute(
                        "UPDATE rescue_ports SET released_at = NULL, public_key = COALESCE(?, public_key) "
                        "WHERE node_id = ?", (public_key, node_id))
                    row = connection.execute("SELECT * FROM rescue_ports WHERE node_id = ?", (node_id,)).fetchone()
                    connection.execute("COMMIT")
                    return _row_to_allocation(row)
                taken = {int(r["port"]) for r in connection.execute("SELECT port FROM rescue_ports").fetchall()}
                port = next((candidate for candidate in range(low, high + 1) if candidate not in taken), None)
                if port is None:
                    connection.execute("ROLLBACK")
                    raise RuntimeError(REASON_EXHAUSTED)
                connection.execute(
                    "INSERT INTO rescue_ports (node_id, port, allocated_at, public_key) VALUES (?, ?, ?, ?)",
                    (node_id, port, stamp, public_key))
                row = connection.execute("SELECT * FROM rescue_ports WHERE node_id = ?", (node_id,)).fetchone()
                connection.execute("COMMIT")
                return _row_to_allocation(row)
            except RuntimeError:
                raise
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("ROLLBACK")
                raise

    def get(self, node_id: str) -> RescueAllocation | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM rescue_ports WHERE node_id = ?", (node_id,)).fetchone()
        return _row_to_allocation(row) if row is not None else None

    def list(self) -> list[RescueAllocation]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM rescue_ports ORDER BY port").fetchall()
        return [_row_to_allocation(row) for row in rows]

    def release(self, node_id: str) -> bool:
        """Frees the port for reuse AND drops the stored public key -- part
        of Remove Node, so a decommissioned machine's key is not left
        sitting in the allocation table."""
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM rescue_ports WHERE node_id = ?", (node_id,))
        return cursor.rowcount == 1


def _row_to_allocation(row: sqlite3.Row) -> RescueAllocation:
    return RescueAllocation(node_id=row["node_id"], port=int(row["port"]),
                            allocated_at=row["allocated_at"], released_at=row["released_at"],
                            public_key=row["public_key"])


# -- gateway description ----------------------------------------------------

@dataclass(frozen=True)
class GatewayDescription:
    """What the enrollment payload and the dashboard both read. `configured`
    is the single answer to "can a node actually build a rescue tunnel
    today"; `reason` says which piece is missing when it cannot."""
    configured: bool
    reason: str | None
    host: str | None
    port: int
    user: str | None
    host_key: str | None
    keepalive_interval_seconds: int
    keepalive_count_max: int
    retry_seconds: int

    def to_dict(self, *, include_host_key: bool = False) -> dict[str, Any]:
        payload = {
            "configured": self.configured, "reason": self.reason, "host": self.host,
            "port": self.port, "user": self.user,
            "keepalive_interval_seconds": self.keepalive_interval_seconds,
            "keepalive_count_max": self.keepalive_count_max, "retry_seconds": self.retry_seconds,
        }
        if include_host_key:
            payload["host_key"] = self.host_key
        return payload


def describe(rescue_config) -> GatewayDescription:
    """Validates an operator's rescue config WITHOUT touching the network.
    Every failure is a named reason, never a silent `configured=False`."""
    base = GatewayDescription(
        configured=False, reason=None, host=None, port=int(getattr(rescue_config, "gateway_port", 22) or 22),
        user=None, host_key=None,
        keepalive_interval_seconds=int(getattr(rescue_config, "keepalive_interval_seconds", 30) or 30),
        keepalive_count_max=int(getattr(rescue_config, "keepalive_count_max", 3) or 3),
        retry_seconds=int(getattr(rescue_config, "retry_seconds", 15) or 15),
    )
    if not getattr(rescue_config, "enabled", False):
        return _with(base, reason=REASON_DISABLED)
    host = (getattr(rescue_config, "gateway_host", "") or "").strip()
    user = (getattr(rescue_config, "gateway_user", "") or "").strip()
    host_key = (getattr(rescue_config, "gateway_host_key", "") or "").strip()
    if not host or not _HOST_RE.match(host):
        return _with(base, reason=REASON_NO_HOST)
    if not user or not _USER_RE.match(user):
        return _with(base, reason=REASON_NO_USER, host=host)
    if not host_key or not _HOST_KEY_RE.match(host_key):
        return _with(base, reason=REASON_NO_HOST_KEY, host=host, user=user)
    low = int(getattr(rescue_config, "port_range_start", 0) or 0)
    high = int(getattr(rescue_config, "port_range_end", 0) or 0)
    if low < 1024 or high > 65535 or low > high:
        return _with(base, reason=REASON_BAD_PORT_RANGE, host=host, user=user)
    return _with(base, configured=True, reason=None, host=host, user=user, host_key=host_key)


def _with(base: GatewayDescription, **changes) -> GatewayDescription:
    fields = {
        "configured": base.configured, "reason": base.reason, "host": base.host, "port": base.port,
        "user": base.user, "host_key": base.host_key,
        "keepalive_interval_seconds": base.keepalive_interval_seconds,
        "keepalive_count_max": base.keepalive_count_max, "retry_seconds": base.retry_seconds,
    }
    fields.update(changes)
    return GatewayDescription(**fields)


# -- argv / authorized_keys builders ----------------------------------------

def build_tunnel_ssh_options(gateway: GatewayDescription, *, reverse_port: int, local_ssh_port: int = 22,
                            identity_file: str, known_hosts_file: str) -> list[str]:
    """The ssh(1) argument list the NODE runs to hold the tunnel open.

    Every flag here is load-bearing:
      -N                          no remote command, forwarding only
      -T                          no pty
      -R 127.0.0.1:P:127.0.0.1:22 loopback-only listener (see module docstring)
      ExitOnForwardFailure=yes    a tunnel that could not bind must FAIL, not
                                  sit there looking connected while forwarding
                                  nothing -- the supervising loop then retries
      ServerAliveInterval/CountMax detects a dead network in ~90s and exits so
                                  the loop can rebuild, instead of a zombie TCP
      StrictHostKeyChecking=yes   with our own pinned known_hosts file
      IdentitiesOnly=yes          never offer some other key that happens to be
                                  in an agent on the node
    """
    if reverse_port <= 0 or reverse_port > 65535:
        raise ValueError("reverse_port out of range")
    return [
        "-N", "-T",
        "-i", identity_file,
        "-p", str(gateway.port),
        "-R", f"127.0.0.1:{reverse_port}:127.0.0.1:{local_ssh_port}",
        "-o", "ExitOnForwardFailure=yes",
        "-o", f"ServerAliveInterval={gateway.keepalive_interval_seconds}",
        "-o", f"ServerAliveCountMax={gateway.keepalive_count_max}",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts_file}",
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        f"{gateway.user}@{gateway.host}",
    ]


def build_authorized_keys_line(*, reverse_port: int, public_key: str, node_id: str) -> str:
    """The line an admin adds to the gateway account's authorized_keys.

    `restrict` turns everything off, then `port-forwarding` turns back on
    exactly the one capability the node needs, and `permitlisten` narrows
    that to this node's own loopback port. A node holding this key can do
    nothing on the gateway except bind 127.0.0.1:<its own port>."""
    key = " ".join(str(public_key or "").split()[:2])
    if not key:
        raise ValueError("public_key is empty")
    return (f'restrict,port-forwarding,permitlisten="127.0.0.1:{reverse_port}" '
            f'{key} terminal-mcp-rescue-{node_id}')


def build_controller_ssh_argv(gateway: GatewayDescription, *, reverse_port: int, node_user: str,
                              node_known_hosts: str, gateway_known_hosts: str,
                              identity_file: str | None = None) -> list[str]:
    """How the CONTROLLER reaches a node THROUGH the gateway: a ProxyJump
    to the gateway, then a connection to the loopback port the node is
    holding open there. Built as an argv list, never a shell string, same
    posture as remote_connect.build_ssh_argv."""
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={node_known_hosts}",
            "-o", f"GlobalKnownHostsFile={gateway_known_hosts}",
            "-o", f"ProxyJump={gateway.user}@{gateway.host}:{gateway.port}"]
    if identity_file:
        argv += ["-i", identity_file, "-o", "IdentitiesOnly=yes"]
    argv += ["-p", str(reverse_port), f"{node_user}@127.0.0.1"]
    return argv


def quote_for_shell(argv: list[str]) -> str:
    """Only ever used to PRINT a command for an operator to paste (docs,
    dashboard "how to reach this node"), never to execute one."""
    return " ".join(shlex.quote(part) for part in argv)
