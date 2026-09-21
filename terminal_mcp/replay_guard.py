"""Persistent replay protection for machine heartbeat requests.

A bearer token authenticates the node, but by itself does not make a captured
heartbeat single-use. This store binds a bounded timestamp window to a nonce
and persists consumed nonces so a controller restart cannot reopen that replay
window.
"""
from __future__ import annotations

import contextlib
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

HEADER_TIMESTAMP = "X-Terminal-MCP-Timestamp"
HEADER_NONCE = "X-Terminal-MCP-Nonce"

OK = "OK"
LEGACY_ACCEPTED = "LEGACY_ACCEPTED"
MISSING_HEADERS = "MISSING_HEADERS"
INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
STALE_TIMESTAMP = "STALE_TIMESTAMP"
FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
INVALID_NONCE = "INVALID_NONCE"
REPLAYED_NONCE = "REPLAYED_NONCE"

_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


@dataclass(frozen=True)
class ReplayCheck:
    accepted: bool
    verdict: str
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"accepted": self.accepted, "verdict": self.verdict, "detail": self.detail}


class HeartbeatReplayGuard:
    """Durable nonce/timestamp gate for authenticated node heartbeats."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_skew_seconds: float = 120.0,
        retention_seconds: float = 300.0,
        require_headers: bool = False,
    ) -> None:
        if max_skew_seconds <= 0:
            raise ValueError("max_skew_seconds must be positive")
        if retention_seconds < max_skew_seconds * 2:
            raise ValueError("retention_seconds must cover at least twice max_skew_seconds")
        self.path = Path(path)
        self.max_skew_seconds = float(max_skew_seconds)
        self.retention_seconds = float(retention_seconds)
        self.require_headers = bool(require_headers)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS heartbeat_replay_nonces (
                    node_id TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    request_timestamp REAL NOT NULL,
                    seen_at REAL NOT NULL,
                    PRIMARY KEY (node_id, nonce)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_heartbeat_replay_seen_at "
                "ON heartbeat_replay_nonces(seen_at)"
            )
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
            yield connection
            connection.commit()
        finally:
            connection.close()

    def check_and_record(
        self,
        node_id: str,
        timestamp: str | None,
        nonce: str | None,
        *,
        now: float | None = None,
    ) -> ReplayCheck:
        if not timestamp or not nonce:
            if self.require_headers:
                return ReplayCheck(False, MISSING_HEADERS, "timestamp and nonce headers are required")
            return ReplayCheck(True, LEGACY_ACCEPTED, "legacy heartbeat accepted during rollout")

        if not _NONCE_RE.fullmatch(nonce):
            return ReplayCheck(False, INVALID_NONCE, "nonce must be 16-128 URL-safe characters")
        try:
            request_time = float(timestamp)
        except (TypeError, ValueError):
            return ReplayCheck(False, INVALID_TIMESTAMP, "timestamp must be unix seconds")

        current = time.time() if now is None else float(now)
        age = current - request_time
        if age > self.max_skew_seconds:
            return ReplayCheck(False, STALE_TIMESTAMP, "heartbeat timestamp is outside the allowed past skew")
        if age < -self.max_skew_seconds:
            return ReplayCheck(False, FUTURE_TIMESTAMP, "heartbeat timestamp is outside the allowed future skew")

        cutoff = current - self.retention_seconds
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM heartbeat_replay_nonces WHERE seen_at < ?", (cutoff,))
            try:
                connection.execute(
                    "INSERT INTO heartbeat_replay_nonces "
                    "(node_id, nonce, request_timestamp, seen_at) VALUES (?, ?, ?, ?)",
                    (node_id, nonce, request_time, current),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return ReplayCheck(False, REPLAYED_NONCE, "heartbeat nonce was already consumed")
            connection.commit()
        finally:
            connection.close()
        return ReplayCheck(True, OK, "fresh heartbeat nonce accepted")
