"""Conservative, controller-local resource lease admission for a node fleet.

This module is intentionally NOT wired to the production scheduler yet.
It uses a single-controller SQLite database and never runs code on worker
nodes. Node-side resource enforcement, authenticated peer paths and network
measurement are separate milestones (issue #96).

Published offers are *allocatable* resource budgets after owner reservations,
not total hardware memory/CPU; readings older than max_age_seconds are unsafe.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time
from typing import Callable


class CapacityUnavailable(ValueError):
    """Allocation rejected; a caller should retain the task without dispatch."""


@dataclass(frozen=True)
class NodeOffer:
    node_id: str
    cpu_millis: int
    memory_bytes: int
    observed_at: float
    health: str = "online"
    max_age_seconds: float = 30.0

    def validate(self) -> None:
        if not self.node_id.strip():
            raise ValueError("node_id is required")
        if self.cpu_millis < 0 or self.memory_bytes < 0:
            raise ValueError("resource budgets must be nonnegative")
        if self.health not in {"online", "degraded", "offline", "unknown"}:
            raise ValueError("invalid health")
        if self.max_age_seconds <= 0 or self.observed_at < 0:
            raise ValueError("invalid offer timing")


@dataclass(frozen=True)
class ResourceLease:
    task_id: str
    node_id: str
    cpu_millis: int
    memory_bytes: int
    expires_at: float


class ResourceLeaseStore:
    """Atomic reservations for ONE control plane, backed by its local SQLite.

    Every writer uses BEGIN IMMEDIATE, so concurrent admissions cannot see the
    same available capacity. Do not put this SQLite database on shared NFS.
    Worker resource enforcement remains a separately required integration.
    """

    def __init__(self, database_path: str | Path, *, clock: Callable[[], float] = time.time):
        self.database_path = str(database_path)
        self.clock = clock
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS mesh_resource_offers (
                    node_id TEXT PRIMARY KEY,
                    cpu_millis INTEGER NOT NULL CHECK(cpu_millis >= 0),
                    memory_bytes INTEGER NOT NULL CHECK(memory_bytes >= 0),
                    observed_at REAL NOT NULL,
                    max_age_seconds REAL NOT NULL CHECK(max_age_seconds > 0),
                    health TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mesh_resource_leases (
                    task_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    cpu_millis INTEGER NOT NULL CHECK(cpu_millis >= 0),
                    memory_bytes INTEGER NOT NULL CHECK(memory_bytes >= 0),
                    expires_at REAL NOT NULL,
                    PRIMARY KEY(task_id, node_id)
                );
                CREATE INDEX IF NOT EXISTS idx_mesh_lease_expiration
                    ON mesh_resource_leases(expires_at);
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = sqlite3.Row
        return db

    def update_offer(self, offer: NodeOffer) -> None:
        offer.validate()
        now = self.clock()
        if offer.observed_at > now + 5:
            raise ValueError("future-dated node offer")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO mesh_resource_offers
                (node_id, cpu_millis, memory_bytes, observed_at, max_age_seconds, health)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                  cpu_millis=excluded.cpu_millis,
                  memory_bytes=excluded.memory_bytes,
                  observed_at=excluded.observed_at,
                  max_age_seconds=excluded.max_age_seconds,
                  health=excluded.health
                WHERE excluded.observed_at >= mesh_resource_offers.observed_at""",
                (offer.node_id, offer.cpu_millis, offer.memory_bytes,
                 offer.observed_at, offer.max_age_seconds, offer.health))
            db.commit()

    def reserve(self, *, task_id: str, node_id: str, cpu_millis: int,
                memory_bytes: int, ttl_seconds: float = 120) -> ResourceLease:
        if not task_id.strip() or not node_id.strip():
            raise ValueError("task_id and node_id are required")
        if cpu_millis < 0 or memory_bytes < 0 or cpu_millis + memory_bytes == 0:
            raise ValueError("positive CPU and/or RAM reservation required")
        if ttl_seconds <= 0 or ttl_seconds > 86400:
            raise ValueError("invalid lease TTL")
        now = self.clock()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM mesh_resource_leases WHERE expires_at <= ?", (now,))
                existing = db.execute("""SELECT * FROM mesh_resource_leases
                    WHERE task_id=? AND node_id=?""", (task_id, node_id)).fetchone()
                # Retries must not mint a second lease or silently resize one.
                if existing:
                    if (existing["cpu_millis"], existing["memory_bytes"]) != (cpu_millis, memory_bytes):
                        raise CapacityUnavailable("existing lease has different resource amounts")
                    lease = ResourceLease(task_id, node_id, cpu_millis,
                                          memory_bytes, existing["expires_at"])
                    db.commit()
                    return lease
                offer = db.execute("SELECT * FROM mesh_resource_offers WHERE node_id=?",
                                   (node_id,)).fetchone()
                if not offer or offer["health"] != "online":
                    raise CapacityUnavailable("node missing or not execution-healthy")
                if offer["observed_at"] > now + 5 or now - offer["observed_at"] > offer["max_age_seconds"]:
                    raise CapacityUnavailable("node offer is stale")
                used = db.execute("""SELECT COALESCE(SUM(cpu_millis), 0) AS cpu,
                        COALESCE(SUM(memory_bytes), 0) AS mem FROM mesh_resource_leases
                        WHERE node_id=?""", (node_id,)).fetchone()
                if cpu_millis + used["cpu"] > offer["cpu_millis"] or memory_bytes + used["mem"] > offer["memory_bytes"]:
                    raise CapacityUnavailable("insufficient reserved node capacity")
                expires_at = now + ttl_seconds
                db.execute("""INSERT INTO mesh_resource_leases
                    (task_id, node_id, cpu_millis, memory_bytes, expires_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (task_id, node_id, cpu_millis, memory_bytes, expires_at))
                db.commit()
                return ResourceLease(task_id, node_id, cpu_millis, memory_bytes, expires_at)
            except Exception:
                db.rollback()
                raise

    def renew(self, *, task_id: str, node_id: str, ttl_seconds: float = 120) -> ResourceLease:
        if ttl_seconds <= 0 or ttl_seconds > 86400:
            raise ValueError("invalid lease TTL")
        now = self.clock()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                lease = db.execute("""SELECT * FROM mesh_resource_leases WHERE
                    task_id=? AND node_id=? AND expires_at>?""", (task_id, node_id, now)).fetchone()
                offer = db.execute("SELECT * FROM mesh_resource_offers WHERE node_id=?", (node_id,)).fetchone()
                if not lease or not offer or offer["health"] != "online" or (
                        now - offer["observed_at"] > offer["max_age_seconds"]):
                    raise CapacityUnavailable("lease is expired or node health is stale")
                expires_at = now + ttl_seconds
                db.execute("UPDATE mesh_resource_leases SET expires_at=? WHERE task_id=? AND node_id=?",
                           (expires_at, task_id, node_id))
                db.commit()
                return ResourceLease(task_id, node_id, lease["cpu_millis"],
                                     lease["memory_bytes"], expires_at)
            except Exception:
                db.rollback()
                raise

    def release(self, *, task_id: str, node_id: str) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("DELETE FROM mesh_resource_leases WHERE task_id=? AND node_id=?",
                               (task_id, node_id)).rowcount
            db.commit()
            return bool(count)

    def snapshot(self) -> list[dict]:
        """Read-only summary; UNKNOWN/stale nodes contribute no schedulable capacity."""
        now = self.clock()
        with self._connect() as db:
            offers = db.execute("SELECT * FROM mesh_resource_offers ORDER BY node_id").fetchall()
            results = []
            for offer in offers:
                used = db.execute("""SELECT COALESCE(SUM(cpu_millis),0) cpu,
                    COALESCE(SUM(memory_bytes),0) mem FROM mesh_resource_leases
                    WHERE node_id=? AND expires_at>?""", (offer["node_id"], now)).fetchone()
                eligible = (offer["health"] == "online" and
                            -5 <= now - offer["observed_at"] <= offer["max_age_seconds"])
                results.append({
                    "node_id": offer["node_id"], "health": offer["health"],
                    "fresh": -5 <= now-offer["observed_at"] <= offer["max_age_seconds"],
                    "eligible": eligible, "observed_at": offer["observed_at"],
                    "cpu_millis_available": max(0, offer["cpu_millis"]-used["cpu"]) if eligible else 0,
                    "memory_bytes_available": max(0, offer["memory_bytes"]-used["mem"]) if eligible else 0,
                    "cpu_millis_leased": used["cpu"], "memory_bytes_leased": used["mem"],
                })
            return results
