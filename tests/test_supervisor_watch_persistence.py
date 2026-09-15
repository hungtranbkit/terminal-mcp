"""Watch coverage survives a restart, an old schema, and a bad row.

In scope for the fleet-aware watch lifecycle already built: a controller or
process restart must not silently change which watches are covered, an existing
supervisor.db written before `reconcile_attempts` existed must open and reconcile,
one unparseable row must not cost the others, and driving `run_once` twice or
concurrently must not double-count a poll or corrupt a watch.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from terminal_mcp.config import AppConfig, PermissionsConfig, SessionAccessConfig, SupervisorConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.supervisor import SupervisorService, SupervisorStore, watch_key


def _service(db, **overrides):
    config = AppConfig(PermissionsConfig(True, False), ("test-*", "hp*", "win*", "wtest"), 50, 20,
                       supervisor=SupervisorConfig(**overrides),
                       session_access=SessionAccessConfig(default_read=True, default_input=False))
    return SupervisorService(TerminalService(config), SupervisorStore(db))


class FakeFleet:
    def __init__(self, live=(), down=()):
        self.live = {n: {"state": "RUNNING", "exists": True, "last_output": f"o:{n}"} for n in live}
        self.down = set(down)

    def terminal_status(self, target):
        node = target.partition("/")[0] if "/" in target else ""
        if node and node in self.down:
            return {"error": "NODE_UNREACHABLE", "node_id": node}
        if target in self.live:
            return dict(self.live[target])
        return {"error": "SESSION_NOT_FOUND", "session": target}


def _age(store, key, seconds):
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with store._connection() as connection:
        connection.execute("UPDATE watches SET updated_at = ? WHERE watch_key = ?", (stamp, key))


# == restart: the same DB, a brand-new process =============================

def test_persisted_watches_are_covered_again_after_a_restart(tmp_path):
    """A controller restart discards every in-memory object. What watches exist
    afterwards must come from the DB, unchanged -- and a disabled-but-recoverable
    row must still be recoverable, not quietly re-created as enabled or lost."""
    db = tmp_path / "supervisor.db"
    first = _service(db)
    first.fleet_status = FakeFleet(live=["hp1", "hp2"], down=["hp"]).terminal_status
    for target in ("hp1", "hp2", "hp/gone"):
        first.watch(session=target)
    first.run_once()
    before = {row["watch_key"]: (row["enabled"], row["disabled_reason"])
              for row in first.store.list_watches()}

    # Restart: new service, new store object, same file.
    second = _service(db)
    second.fleet_status = FakeFleet(live=["hp1", "hp2"], down=["hp"]).terminal_status
    after = {row["watch_key"]: (row["enabled"], row["disabled_reason"])
             for row in second.store.list_watches()}

    assert after == before, "the restart changed which watches were covered"
    assert second.status()["watch_count"] == 3


def test_a_recoverable_row_still_recovers_after_a_restart(tmp_path):
    """The whole point of persisting the reason: recovery is not a property of
    the process that disabled the watch."""
    db = tmp_path / "supervisor.db"
    first = _service(db)
    first.fleet_status = FakeFleet(down=["hp"]).terminal_status
    first.watch(session="hp/w1")
    first.run_once()
    key = watch_key("session", "hp/w1")
    assert first.store.get_watch(key)["disabled_reason"] == "node_unreachable"

    second = _service(db)
    second.fleet_status = FakeFleet(live=["hp/w1"]).terminal_status  # the node came back
    _age(second.store, key, 600)
    restored = second.reconcile_watches()["restored"]

    assert [r["watch_key"] for r in restored] == [key]
    assert second.store.get_watch(key)["enabled"] == 1


def test_a_manual_unwatch_survives_a_restart(tmp_path):
    """A person's decision is durable. A restart must not be a way to undo it."""
    db = tmp_path / "supervisor.db"
    first = _service(db)
    first.watch(session="test-keepoff")
    first.unwatch(session="test-keepoff")

    second = _service(db)
    second.fleet_status = FakeFleet(live=["test-keepoff"]).terminal_status
    key = watch_key("session", "test-keepoff")
    _age(second.store, key, 10_000)
    assert second.reconcile_watches()["restored"] == []
    assert second.store.get_watch(key)["disabled_reason"] == "manual_unwatch"


# == an existing DB written before this work ==============================

def test_a_pre_migration_database_opens_and_reconciles(tmp_path):
    """An existing supervisor.db has no `reconcile_attempts` column. Opening it
    must add the column with a default rather than failing, and a row written by
    the old code must be reconcilable -- production's rows are exactly this."""
    db = tmp_path / "legacy.db"
    connection = sqlite3.connect(db)
    connection.execute("""
        CREATE TABLE watches (
            watch_key TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL,
            source TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL DEFAULT 'UNKNOWN', state_since TEXT NOT NULL,
            last_output_hash TEXT, last_output_change_at TEXT, last_activity TEXT,
            iteration_count INTEGER NOT NULL DEFAULT 0,
            same_failure_count INTEGER NOT NULL DEFAULT 0, disabled_reason TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)
    """)
    old = (datetime.now(timezone.utc) - timedelta(seconds=9999)).isoformat()
    connection.execute(
        "INSERT INTO watches (watch_key, kind, target, source, enabled, state, state_since, "
        "iteration_count, disabled_reason, created_at, updated_at) VALUES "
        "('session:hp1','session','hp1','manual',0,'UNKNOWN',?,223,'max_iterations_exceeded',?,?)",
        (old, old, old))
    connection.commit()
    connection.close()

    service = _service(db)
    service.fleet_status = FakeFleet(live=["hp1"]).terminal_status

    row = service.store.get_watch("session:hp1")
    assert row is not None, "the legacy row was lost"
    assert row["reconcile_attempts"] == 0, "the added column must default, not fail"

    restored = service.reconcile_watches()["restored"]
    assert [r["watch_key"] for r in restored] == ["session:hp1"]
    assert service.store.get_watch("session:hp1")["iteration_count"] == 0, \
        "a restored watch must not keep a count already past the ceiling"


def test_the_new_status_fields_are_present_for_a_legacy_row(tmp_path):
    """An operator on an upgraded controller must get the truthful counts for
    rows they already had, not only for newly created ones."""
    db = tmp_path / "legacy2.db"
    service = _service(db)
    service.watch(session="test-a")
    service.store.set_enabled(watch_key("session", "test-a"), False,
                              disabled_reason="max_iterations_exceeded")
    status = service.status()
    assert status["disabled_watch_count"] == 1
    assert status["recoverable_disabled_count"] == 1
    assert status["intentionally_excluded_count"] == 0


# == a bad row must not cost the good ones ================================

def test_an_unparseable_timestamp_does_not_stop_reconciliation(tmp_path):
    """A corrupt/stale row is data, not a crash. The others still get their
    chance in the same pass."""
    db = tmp_path / "s.db"
    service = _service(db)
    service.fleet_status = FakeFleet(live=["hp1", "hp2"]).terminal_status
    for target in ("hp1", "hp2"):
        service.watch(session=target)
        service.store.set_enabled(watch_key("session", target), False,
                                  disabled_reason="target_missing")
    with service.store._connection() as connection:
        connection.execute("UPDATE watches SET updated_at = 'not-a-timestamp' "
                           "WHERE watch_key = ?", (watch_key("session", "hp1"),))
    _age(service.store, watch_key("session", "hp2"), 600)

    result = service.reconcile_watches()

    handled = {r["watch_key"] for r in result["restored"]} | {r["watch_key"] for r in result["skipped"]}
    assert handled == {watch_key("session", "hp1"), watch_key("session", "hp2")}, \
        "a corrupt row aborted the pass"
    assert watch_key("session", "hp2") in {r["watch_key"] for r in result["restored"]}


def test_a_watch_whose_target_probe_raises_is_skipped_not_fatal(tmp_path):
    db = tmp_path / "s.db"
    service = _service(db)

    def _boom(target):
        raise TimeoutError("node hung")

    service.watch(session="hp/x")
    service.store.set_enabled(watch_key("session", "hp/x"), False, disabled_reason="node_unreachable")
    service.fleet_status = _boom
    _age(service.store, watch_key("session", "hp/x"), 600)

    result = service.reconcile_watches()
    assert result["restored"] == []
    assert result["skipped"], "the raising probe was not even reported"


# == duplicate and concurrent run_once ====================================

def test_two_run_once_passes_do_not_double_count_one_poll(tmp_path, tmux_session_factory):
    """`supervisor_run_once` is directly callable, so an operator and the loop can
    both drive it. Each pass is one poll per watch -- never two."""
    session = tmux_session_factory("test-dup-poll", "bash -lc 'sleep 60'")
    service = _service(tmp_path / "s.db")
    service.watch(session=session)
    key = watch_key("session", session)

    service.run_once()
    first = service.store.get_watch(key)["iteration_count"]
    service.run_once()
    second = service.store.get_watch(key)["iteration_count"]

    assert first == 1
    assert second == 2, "a pass did not advance exactly one poll"


def test_concurrent_run_once_leaves_every_watch_consistent(tmp_path, tmux_session_factory):
    """Two threads driving run_once at once must not corrupt a row or lose a
    watch. The counter may land anywhere in a legal range -- what must hold is
    that every watch is still present, still enabled, and readable."""
    session = tmux_session_factory("test-conc-poll", "bash -lc 'sleep 60'")
    service = _service(tmp_path / "s.db")
    service.watch(session=session)
    key = watch_key("session", session)
    errors: list[BaseException] = []

    def _drive():
        try:
            for _ in range(4):
                service.run_once()
        except BaseException as exc:  # noqa: BLE001 -- the point is that nothing escapes
            errors.append(exc)

    threads = [threading.Thread(target=_drive) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == [], f"concurrent run_once raised: {errors[:2]}"
    row = service.store.get_watch(key)
    assert row is not None and row["enabled"] == 1
    assert row["iteration_count"] >= 1
    assert len(service.store.list_watches()) == 1, "a watch was duplicated or lost"


def test_reconciliation_is_idempotent_within_a_backoff_window(tmp_path):
    """Called repeatedly by the loop, it must restore once and then decline --
    otherwise attempts burn and the backoff means nothing."""
    db = tmp_path / "s.db"
    service = _service(db)
    service.fleet_status = FakeFleet(live=["hp1"]).terminal_status
    service.watch(session="hp1")
    key = watch_key("session", "hp1")
    service.store.set_enabled(key, False, disabled_reason="target_missing")
    _age(service.store, key, 600)

    assert len(service.reconcile_watches()["restored"]) == 1
    for _ in range(3):
        assert service.reconcile_watches()["restored"] == [], "an enabled watch was 'restored' again"
    assert service.store.get_watch(key)["reconcile_attempts"] == 1
