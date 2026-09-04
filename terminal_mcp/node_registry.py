"""Durable node registry -- one row per node (local or remote), same
connection/schema/permission pattern as every other store in this project
(audit.py/bindings.py/grants.py/lease.py/killed_sessions.py lineage).

Owns two things, deliberately kept together since they operate on the
exact same row at the exact same write (a heartbeat): persistence, and
the overload heuristic (item 5) that turns a raw metrics sample into
`capacity_status`/`overload_reasons`, smoothed (EWMA) and duration-aware
(CPU/load must be high for `sustained_seconds`, not just one noisy
sample, before a Busy reading escalates to Overloaded) so the dashboard
and scheduler never see a status that flaps on a single spike.

`status` (online/degraded/offline) is NEVER persisted -- always DERIVED
from `last_heartbeat_at` age at read time (get()/list()), so a registry
row can sit there for a node that's been offline for a week and still
correctly report "offline" without a background sweep needing to keep
touching it. This is also exactly what task item 10 asks for: "session
registry không biến mất ngay; mark stale/offline" -- the row persists,
only the derived label changes.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .host_metrics import NodeMetrics
from .node_models import (
    CAPACITY_BUSY,
    CAPACITY_HEALTHY,
    CAPACITY_OVERLOADED,
    CAPACITY_UNKNOWN,
    Node,
    NodeHeartbeatThresholds,
    OverloadThresholds,
)
from .schema import Migration, apply_migrations

NODE_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: nodes as of the multi-node design", lambda connection: None),
]


def default_registry_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_NODE_REGISTRY_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "nodes.db"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def classify_capacity(metrics_row: dict[str, Any], thresholds: OverloadThresholds) -> tuple[str, list[str]]:
    """Pure function: (smoothed metrics + duration flags) -> (capacity_
    status, reasons). Operates on SMOOTHED values where smoothing applies
    (ram/cpu/swap), raw for disk (disk fill is monotonic-ish and slow --
    smoothing it only adds lag to a real "about to fill up" warning).
    `metrics_row` is a plain dict (not a Node) so this is testable with a
    bare dict literal, no store/dataclass construction needed."""
    reasons: list[str] = []
    ram = metrics_row.get("ram_percent_smoothed")
    swap = metrics_row.get("swap_percent_smoothed")
    cpu = metrics_row.get("cpu_percent_smoothed")
    load1 = metrics_row.get("load1")
    cpu_count = metrics_row.get("cpu_count")
    disk_percent = metrics_row.get("disk_percent")
    high_cpu_since = metrics_row.get("high_cpu_since")
    high_load_since = metrics_row.get("high_load_since")
    now = metrics_row.get("_now")  # injected by heartbeat()/for tests; ISO string

    overloaded = False
    busy = False

    if ram is not None:
        if ram >= thresholds.ram_overloaded_percent:
            overloaded = True
            reasons.append(f"RAM {ram:.0f}% >= {thresholds.ram_overloaded_percent:.0f}%")
        elif ram >= thresholds.ram_busy_percent:
            busy = True
            reasons.append(f"RAM {ram:.0f}% >= {thresholds.ram_busy_percent:.0f}%")

    # "swap usage tăng + RAM cao => Overloaded" -- swap alone (a host can
    # have swap configured and lightly used with plenty of free RAM, not
    # a problem) is only an overload SIGNAL combined with RAM already
    # being at least Busy-level; never triggers off swap in isolation.
    if swap is not None and ram is not None and swap >= thresholds.swap_overloaded_percent and ram >= thresholds.ram_busy_percent:
        overloaded = True
        reasons.append(f"swap {swap:.0f}% with RAM {ram:.0f}% -- actively paging under memory pressure")

    if cpu is not None and now is not None:
        if cpu >= thresholds.cpu_busy_percent:
            since = high_cpu_since or now  # first crossing -- not yet sustained
            elapsed = _elapsed_seconds(since, now)
            if cpu >= thresholds.cpu_overloaded_percent and elapsed >= thresholds.sustained_seconds:
                overloaded = True
                reasons.append(f"CPU {cpu:.0f}% sustained {elapsed:.0f}s")
            elif elapsed >= thresholds.sustained_seconds:
                busy = True
                reasons.append(f"CPU {cpu:.0f}% sustained {elapsed:.0f}s")
            else:
                busy = True  # high but not yet "sustained" -- Busy, not yet Overloaded-eligible
                reasons.append(f"CPU {cpu:.0f}% (recently elevated)")

    if load1 is not None and cpu_count and now is not None:
        threshold = cpu_count * thresholds.load_factor_busy
        if load1 > threshold:
            since = high_load_since or now
            elapsed = _elapsed_seconds(since, now)
            busy = True
            reasons.append(f"load1 {load1:.1f} > {threshold:.1f} ({cpu_count} cores x {thresholds.load_factor_busy})"
                          + (f", sustained {elapsed:.0f}s" if elapsed >= thresholds.sustained_seconds else ""))

    if disk_percent is not None:
        disk_free_percent = 100.0 - disk_percent
        if disk_free_percent < thresholds.disk_free_overloaded_percent:
            overloaded = True
            reasons.append(f"disk free {disk_free_percent:.0f}% < {thresholds.disk_free_overloaded_percent:.0f}%")

    if overloaded:
        return CAPACITY_OVERLOADED, reasons
    if busy:
        return CAPACITY_BUSY, reasons
    if ram is None and cpu is None and disk_percent is None:
        return CAPACITY_UNKNOWN, []
    return CAPACITY_HEALTHY, []


def _elapsed_seconds(since_iso: str, now_iso: str) -> float:
    try:
        since = datetime.fromisoformat(since_iso)
        now = datetime.fromisoformat(now_iso)
        return max(0.0, (now - since).total_seconds())
    except ValueError:
        return 0.0


def _ewma(previous: float | None, raw: float | None, alpha: float) -> float | None:
    if raw is None:
        return previous
    if previous is None:
        return raw
    return alpha * raw + (1 - alpha) * previous


class NodeRegistry:
    def __init__(self, path: str | Path | None = None, *,
                overload_thresholds: OverloadThresholds | None = None,
                heartbeat_thresholds: NodeHeartbeatThresholds | None = None) -> None:
        self.path = Path(path) if path is not None else default_registry_path()
        self.overload_thresholds = overload_thresholds or OverloadThresholds()
        self.heartbeat_thresholds = heartbeat_thresholds or NodeHeartbeatThresholds()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    hostname TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    auth_token_ref TEXT,
                    draining INTEGER NOT NULL DEFAULT 0,
                    last_heartbeat_at TEXT,
                    latency_ms REAL,
                    cpu_percent REAL, cpu_percent_smoothed REAL,
                    load1 REAL, load5 REAL, load15 REAL, cpu_count INTEGER,
                    ram_total_bytes INTEGER, ram_used_bytes INTEGER,
                    ram_percent REAL, ram_percent_smoothed REAL,
                    swap_total_bytes INTEGER, swap_used_bytes INTEGER,
                    swap_percent REAL, swap_percent_smoothed REAL,
                    disk_total_bytes INTEGER, disk_used_bytes INTEGER,
                    disk_free_bytes INTEGER, disk_percent REAL,
                    tmux_session_count INTEGER,
                    agent_counts TEXT NOT NULL DEFAULT '{}',
                    agent_types TEXT NOT NULL DEFAULT '[]',
                    agent_version TEXT,
                    labels TEXT NOT NULL DEFAULT '[]',
                    max_sessions INTEGER,
                    high_cpu_since TEXT,
                    high_load_since TEXT,
                    capacity_status TEXT NOT NULL DEFAULT 'unknown',
                    overload_reasons TEXT NOT NULL DEFAULT '[]',
                    registered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            apply_migrations(connection, NODE_MIGRATIONS)
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

    # -- registration -----------------------------------------------------

    def register(self, node_id: str, *, display_name: str, hostname: str, endpoint: str,
                auth_token_ref: str | None = None, max_sessions: int | None = None) -> None:
        """Idempotent: registering an already-known node id updates its
        static fields (display_name/hostname/endpoint/max_sessions) but
        never touches its metrics/heartbeat history -- registering is a
        config-time operation, never a substitute for heartbeat()."""
        now_iso = _iso(_now())
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO nodes (id, display_name, hostname, endpoint, auth_token_ref, max_sessions,
                                      registered_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name = excluded.display_name, hostname = excluded.hostname,
                    endpoint = excluded.endpoint, auth_token_ref = excluded.auth_token_ref,
                    max_sessions = excluded.max_sessions, updated_at = excluded.updated_at""",
                (node_id, display_name, hostname, endpoint, auth_token_ref, max_sessions, now_iso, now_iso),
            )

    def deregister(self, node_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        return cursor.rowcount == 1

    def set_draining(self, node_id: str, draining: bool) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE nodes SET draining = ?, updated_at = ? WHERE id = ?",
                (int(draining), _iso(_now()), node_id),
            )
        return cursor.rowcount == 1

    # -- heartbeat ----------------------------------------------------------

    def heartbeat(self, node_id: str, *, metrics: NodeMetrics, tmux_session_count: int,
                 agent_counts: dict[str, int], agent_types: tuple[str, ...],
                 agent_version: str | None, labels: tuple[str, ...], latency_ms: float | None = None,
                 now: datetime | None = None) -> Node | None:
        """Writes a fresh sample, applies EWMA smoothing on top of
        whatever was previously stored, updates the sustained-high-CPU/
        load duration trackers, recomputes capacity_status/
        overload_reasons, and returns the resulting Node -- all in one
        transaction, so a concurrent get() never observes a half-updated
        row. Returns None if `node_id` was never register()ed."""
        now = now or _now()
        now_iso = _iso(now)
        thresholds = self.overload_thresholds

        with self._connection() as connection:
            existing = connection.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
            if existing is None:
                return None
            prev = dict(existing)

            ram_smoothed = _ewma(prev.get("ram_percent_smoothed"), metrics.ram_percent, thresholds.smoothing_alpha)
            cpu_smoothed = _ewma(prev.get("cpu_percent_smoothed"), metrics.cpu_percent, thresholds.smoothing_alpha)
            swap_smoothed = _ewma(prev.get("swap_percent_smoothed"), metrics.swap_percent, thresholds.smoothing_alpha)

            high_cpu_since = prev.get("high_cpu_since")
            if cpu_smoothed is not None and cpu_smoothed >= thresholds.cpu_busy_percent:
                high_cpu_since = high_cpu_since or now_iso
            else:
                high_cpu_since = None

            high_load_since = prev.get("high_load_since")
            load_threshold = (metrics.cpu_count or 0) * thresholds.load_factor_busy
            if metrics.load1 is not None and metrics.cpu_count and metrics.load1 > load_threshold:
                high_load_since = high_load_since or now_iso
            else:
                high_load_since = None

            classify_input = {
                "ram_percent_smoothed": ram_smoothed, "cpu_percent_smoothed": cpu_smoothed,
                "swap_percent_smoothed": swap_smoothed, "load1": metrics.load1, "cpu_count": metrics.cpu_count,
                "disk_percent": metrics.disk_percent, "high_cpu_since": high_cpu_since,
                "high_load_since": high_load_since, "_now": now_iso,
            }
            capacity_status, reasons = classify_capacity(classify_input, thresholds)

            connection.execute(
                """UPDATE nodes SET
                    last_heartbeat_at = ?, latency_ms = ?,
                    cpu_percent = ?, cpu_percent_smoothed = ?,
                    load1 = ?, load5 = ?, load15 = ?, cpu_count = ?,
                    ram_total_bytes = ?, ram_used_bytes = ?, ram_percent = ?, ram_percent_smoothed = ?,
                    swap_total_bytes = ?, swap_used_bytes = ?, swap_percent = ?, swap_percent_smoothed = ?,
                    disk_total_bytes = ?, disk_used_bytes = ?, disk_free_bytes = ?, disk_percent = ?,
                    tmux_session_count = ?, agent_counts = ?, agent_types = ?, agent_version = ?, labels = ?,
                    high_cpu_since = ?, high_load_since = ?, capacity_status = ?, overload_reasons = ?,
                    updated_at = ?
                WHERE id = ?""",
                (now_iso, latency_ms,
                 metrics.cpu_percent, cpu_smoothed,
                 metrics.load1, metrics.load5, metrics.load15, metrics.cpu_count,
                 metrics.ram_total_bytes, metrics.ram_used_bytes, metrics.ram_percent, ram_smoothed,
                 metrics.swap_total_bytes, metrics.swap_used_bytes, metrics.swap_percent, swap_smoothed,
                 metrics.disk_total_bytes, metrics.disk_used_bytes, metrics.disk_free_bytes, metrics.disk_percent,
                 tmux_session_count, json.dumps(agent_counts), json.dumps(list(agent_types)), agent_version,
                 json.dumps(list(labels)), high_cpu_since, high_load_since, capacity_status, json.dumps(reasons),
                 now_iso, node_id),
            )
        return self.get(node_id, now=now)

    # -- reads (status is ALWAYS derived here, never trusted from storage) --

    def _row_to_node(self, row: sqlite3.Row, *, now: datetime) -> Node:
        data = dict(row)
        last_heartbeat = data.get("last_heartbeat_at")
        status = self._derive_status(last_heartbeat, now)
        return Node(
            id=data["id"], display_name=data["display_name"], hostname=data["hostname"],
            endpoint=data["endpoint"], status=status, draining=bool(data["draining"]),
            last_heartbeat_at=last_heartbeat, latency_ms=data.get("latency_ms"),
            cpu_percent=data.get("cpu_percent"), cpu_percent_smoothed=data.get("cpu_percent_smoothed"),
            load1=data.get("load1"), load5=data.get("load5"), load15=data.get("load15"),
            cpu_count=data.get("cpu_count"),
            ram_total_bytes=data.get("ram_total_bytes"), ram_used_bytes=data.get("ram_used_bytes"),
            ram_percent=data.get("ram_percent"), ram_percent_smoothed=data.get("ram_percent_smoothed"),
            swap_total_bytes=data.get("swap_total_bytes"), swap_used_bytes=data.get("swap_used_bytes"),
            swap_percent=data.get("swap_percent"), swap_percent_smoothed=data.get("swap_percent_smoothed"),
            disk_total_bytes=data.get("disk_total_bytes"), disk_used_bytes=data.get("disk_used_bytes"),
            disk_free_bytes=data.get("disk_free_bytes"), disk_percent=data.get("disk_percent"),
            tmux_session_count=data.get("tmux_session_count"),
            agent_counts=json.loads(data.get("agent_counts") or "{}"),
            agent_types=tuple(json.loads(data.get("agent_types") or "[]")),
            agent_version=data.get("agent_version"),
            labels=tuple(json.loads(data.get("labels") or "[]")),
            max_sessions=data.get("max_sessions"),
            capacity_status=data.get("capacity_status") or CAPACITY_UNKNOWN,
            overload_reasons=tuple(json.loads(data.get("overload_reasons") or "[]")),
            registered_at=data.get("registered_at"), updated_at=data.get("updated_at"),
        )

    def _derive_status(self, last_heartbeat_at: str | None, now: datetime) -> str:
        from .node_models import NODE_DEGRADED, NODE_OFFLINE, NODE_ONLINE
        if not last_heartbeat_at:
            return NODE_OFFLINE
        try:
            heartbeat = datetime.fromisoformat(last_heartbeat_at)
        except ValueError:
            return NODE_OFFLINE
        age = (now - heartbeat).total_seconds()
        if age <= self.heartbeat_thresholds.degraded_after_seconds:
            return NODE_ONLINE
        if age <= self.heartbeat_thresholds.offline_after_seconds:
            return NODE_DEGRADED
        return NODE_OFFLINE

    def get(self, node_id: str, *, now: datetime | None = None) -> Node | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return self._row_to_node(row, now=now or _now()) if row is not None else None

    def list(self, *, now: datetime | None = None) -> list[Node]:
        now = now or _now()
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM nodes ORDER BY id").fetchall()
        return [self._row_to_node(row, now=now) for row in rows]
