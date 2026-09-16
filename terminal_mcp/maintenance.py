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
from .event_bus import EventBus
from .lease import PaneLeaseStore, ResourceLockStore
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
                leases: PaneLeaseStore | None = None,
                resource_locks: ResourceLockStore | None = None,
                events: Any = None, lifecycle: Any = None,
                lifecycle_enabled: bool = False,
                lifecycle_reconcile_limit: int = 50) -> None:
        self._audit = audit
        self._supervisor2_store = supervisor2_store
        self._bindings_path = bindings_path
        # P0 Part B: leases is a plain, mandatory-by-default dependency
        # (like the others here) rather than truly optional -- defaults to
        # the same shared on-disk store every TerminalService uses unless
        # a caller (tests) injects an isolated one.
        self._leases = leases or PaneLeaseStore()
        # Same database file, same housekeeping cadence -- constructed
        # here rather than passed in because, like _leases, there is
        # exactly one sensible instance and it is cheap to open.
        self._resource_locks = resource_locks or ResourceLockStore(self._leases.path)
        self._events = events or EventBus()
        # Lifecycle Close-Loop V1. This loop is the right host for the
        # reconcile pass: it already runs on a fixed interval independent
        # of supervisor/queue/integration opt-ins, and the pass is exactly
        # the same shape as the pruning beside it -- bounded, idempotent,
        # safe to skip, safe to repeat. `lifecycle_enabled` gates only the
        # AUTOMATIC invocation; LifecycleService's own methods stay
        # callable by hand either way.
        self._lifecycle = lifecycle
        self._lifecycle_enabled = lifecycle_enabled
        self._lifecycle_reconcile_limit = lifecycle_reconcile_limit
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
        try:
            # P0.6: the same housekeeping for resource_locks. Also
            # harmless to skip -- acquire()'s expiry check makes a lapsed
            # row reclaimable without this -- but a fleet that locks many
            # files would otherwise keep a row per resource ever touched.
            result["resource_locks_pruned"] = self._resource_locks.prune_expired()
        except Exception:
            _LOGGER.exception("maintenance: resource lock prune failed")
        if self._lifecycle is not None:
            try:
                result["lifecycle_keys_pruned"] = self._lifecycle.store.prune_settled(
                    self._config.lifecycle_key_retention_days)
            except Exception:
                _LOGGER.exception("maintenance: lifecycle key prune failed")
            if self._lifecycle_enabled:
                try:
                    # Never allowed to break database hygiene: a reconcile
                    # that raises must still leave the WAL checkpointing
                    # below to run, so the whole pass is wrapped rather
                    # than each edge (LifecycleService.reconcile already
                    # isolates its three sweeps internally).
                    result["lifecycle"] = self._lifecycle.reconcile(
                        limit=self._lifecycle_reconcile_limit)
                except Exception:
                    _LOGGER.exception("maintenance: lifecycle reconcile failed")
        for path in self._db_paths():
            checkpoint_wal(path)
        if any(result.get(k) for k in ("audit_pruned", "actions_pruned", "leases_pruned", "resource_locks_pruned")):
            _LOGGER.info("maintenance: pruned rows", extra=result)
        return result

    def _db_paths(self) -> list[Path]:
        # events.db was missing here: the bus is a durable append-only log
        # that grows forever and was never WAL-checkpointed or pruned by any
        # code path. An unlisted store simply never gets maintained.
        # release_store.db was missing here: the release state machine is
        # durable, WAL-journalled and now written by an automatic pass, so
        # it needs the same checkpointing as every other store. lifecycle.db
        # joins it for the same reason.
        paths = [self._audit.path, self._leases.path, self._events.path]
        if self._lifecycle is not None:
            paths.append(self._lifecycle.store.path)
            release_store = getattr(getattr(self._lifecycle, "release", None), "store", None)
            release_path = getattr(release_store, "path", None)
            if release_path is not None:
                paths.append(release_path)
        if self._supervisor2_store is not None:
            paths.append(self._supervisor2_store.path)
        if self._bindings_path is not None:
            paths.append(self._bindings_path)
        return paths

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.run_once()
            self._stop_event.wait(self._config.interval_seconds)
