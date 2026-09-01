"""Periodic SQLite maintenance -- P1 hardening item #9: audit/action
retention pruning and WAL checkpointing.

Deliberately independent of Supervisor Loop v1's own background loop and
of config.supervisor.enabled: audit.db accumulates from any
terminal_send_text/_keys call regardless of whether that optional feature
is on, so this is baseline database hygiene every deployment needs, not
something gated behind an unrelated opt-in. Same daemon-thread-with-a-
stop-Event shape as SupervisorLoop (server_http.py has no asyncio/
lifespan hook to attach a coroutine-based task to instead).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .audit import AuditStore
from .config import MaintenanceConfig
from .lease import PaneLeaseStore
from .supervisor2 import SupervisorV2Store

_LOGGER = logging.getLogger(__name__)


def checkpoint_wal(path: Path) -> None:
    """PASSIVE: never blocks on or interrupts another connection's
    in-progress transaction (unlike FULL/RESTART/TRUNCATE) -- this is
    background hygiene, not a correctness requirement, so it only ever
    does as much as it safely can right now and tries again next cycle."""
    try:
        connection = sqlite3.connect(path, timeout=5)
        try:
            connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        finally:
            connection.close()
    except Exception:
        _LOGGER.warning("maintenance: WAL checkpoint failed", extra={"db_path": str(path)}, exc_info=True)


class MaintenanceLoop:
    def __init__(self, *, audit: AuditStore, supervisor2_store: SupervisorV2Store | None,
                bindings_path: Path | None, config: MaintenanceConfig,
                leases: PaneLeaseStore | None = None) -> None:
        self._audit = audit
        self._supervisor2_store = supervisor2_store
        self._bindings_path = bindings_path
        # P0 Part B: leases is a plain, mandatory-by-default dependency
        # (like the others here) rather than truly optional -- defaults to
        # the same shared on-disk store every TerminalService uses unless
        # a caller (tests) injects an isolated one.
        self._leases = leases or PaneLeaseStore()
        self._config = config
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="terminal-mcp-maintenance", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def run_once(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        try:
            result["audit_pruned"] = self._audit.prune(self._config.audit_retention)
            result["idempotency_keys_pruned"] = self._audit.prune_idempotency_keys(
                self._config.idempotency_key_retention_days
            )
        except Exception:
            _LOGGER.exception("maintenance: audit prune failed")
        if self._supervisor2_store is not None:
            try:
                result["actions_pruned"] = self._supervisor2_store.prune_actions(self._config.action_retention)
            except Exception:
                _LOGGER.exception("maintenance: action prune failed")
        try:
            # Housekeeping only -- acquire()'s own expiry check already
            # makes an expired row harmless without this ever running (see
            # lease.py); this just keeps pane_leases from accumulating a
            # row per pane ever touched, forever.
            result["leases_pruned"] = self._leases.prune_expired()
        except Exception:
            _LOGGER.exception("maintenance: lease prune failed")
        for path in self._db_paths():
            checkpoint_wal(path)
        if any(result.get(k) for k in ("audit_pruned", "actions_pruned", "leases_pruned")):
            _LOGGER.info("maintenance: pruned rows", extra=result)
        return result

    def _db_paths(self) -> list[Path]:
        paths = [self._audit.path, self._leases.path]
        if self._supervisor2_store is not None:
            paths.append(self._supervisor2_store.path)
        if self._bindings_path is not None:
            paths.append(self._bindings_path)
        return paths

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.run_once()
            self._stop_event.wait(self._config.interval_seconds)
