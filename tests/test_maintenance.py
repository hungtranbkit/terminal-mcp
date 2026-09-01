"""P1 hardening item #9: audit/action retention pruning and WAL
checkpointing."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

from terminal_mcp.audit import AuditStore
from terminal_mcp.config import MaintenanceConfig, load_config
from terminal_mcp.lease import PaneLeaseStore
from terminal_mcp.maintenance import MaintenanceLoop, checkpoint_wal
from terminal_mcp.supervisor2 import SupervisorV2Store


# ---------------------------------------------------------------------------
# AuditStore.prune / prune_idempotency_keys
# ---------------------------------------------------------------------------


def test_audit_prune_keeps_only_the_most_recent_n_rows(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    for i in range(10):
        store.record(action="send_text", session=f"s{i}", result="SENT")
    pruned = store.prune(4)
    assert pruned == 6
    remaining = store.list(limit=50)
    assert len(remaining) == 4
    # The most recent ones survive (highest ids / latest calls), not an
    # arbitrary subset.
    assert {row["session"] for row in remaining} == {"s6", "s7", "s8", "s9"}


def test_audit_prune_is_a_noop_when_under_the_retention_limit(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    store.record(action="send_text", session="only-one", result="SENT")
    assert store.prune(100) == 0
    assert len(store.list(limit=50)) == 1


def test_prune_idempotency_keys_removes_only_stale_entries(tmp_path):
    store = AuditStore(tmp_path / "audit.db")
    store.claim_idempotency_key("fresh-key")
    store.claim_idempotency_key("stale-key")
    old_iso = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE idempotent_sends SET created_at = ? WHERE idempotency_key = ?", (old_iso, "stale-key"),
        )
    pruned = store.prune_idempotency_keys(older_than_days=30)
    assert pruned == 1
    assert store.get_idempotent_result("fresh-key") is None  # still claimed, just no result yet
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute("SELECT idempotency_key FROM idempotent_sends").fetchall()
    assert [r[0] for r in rows] == ["fresh-key"]


# ---------------------------------------------------------------------------
# SupervisorV2Store.prune_actions
# ---------------------------------------------------------------------------


def test_prune_actions_keeps_only_the_most_recent_n(tmp_path):
    store = SupervisorV2Store(tmp_path / "supervisor.db")
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(store.path) as connection:
        for i in range(10):
            connection.execute(
                """INSERT INTO supervisor_actions
                (watch_key, event_id, state, prompt_hash, created_at, updated_at)
                VALUES (?, ?, 'completed', 'hash', ?, ?)""",
                (f"session:s{i}", i, now, now),
            )
    pruned = store.prune_actions(3)
    assert pruned == 7
    remaining = store.list_actions(limit=50)
    assert len(remaining) == 3


# ---------------------------------------------------------------------------
# WAL checkpoint
# ---------------------------------------------------------------------------


def test_checkpoint_wal_never_raises_on_a_missing_db(tmp_path):
    checkpoint_wal(tmp_path / "does-not-exist" / "nope.db")  # must not raise


def test_checkpoint_wal_succeeds_on_a_real_wal_mode_db(tmp_path):
    path = tmp_path / "real.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE t (x INTEGER)")
    connection.execute("INSERT INTO t VALUES (1)")
    connection.commit()
    connection.close()
    checkpoint_wal(path)  # must not raise; real PASSIVE checkpoint against a real db


# ---------------------------------------------------------------------------
# MaintenanceLoop
# ---------------------------------------------------------------------------


def test_maintenance_loop_run_once_prunes_across_both_stores(tmp_path):
    audit = AuditStore(tmp_path / "audit.db")
    for i in range(5):
        audit.record(action="send_text", session=f"s{i}", result="SENT")
    v2_store = SupervisorV2Store(tmp_path / "supervisor.db")
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(v2_store.path) as connection:
        for i in range(5):
            connection.execute(
                """INSERT INTO supervisor_actions
                (watch_key, event_id, state, prompt_hash, created_at, updated_at)
                VALUES (?, ?, 'completed', 'hash', ?, ?)""",
                (f"session:s{i}", i, now, now),
            )
    config = MaintenanceConfig(audit_retention=2, action_retention=2, idempotency_key_retention_days=30)
    loop = MaintenanceLoop(
        audit=audit, supervisor2_store=v2_store, bindings_path=None, config=config,
        leases=PaneLeaseStore(tmp_path / "leases.db"),
    )
    result = loop.run_once()
    assert result["audit_pruned"] == 3
    assert result["actions_pruned"] == 3
    assert len(audit.list(limit=50)) == 2
    assert len(v2_store.list_actions(limit=50)) == 2


def test_maintenance_loop_survives_a_broken_store(tmp_path):
    # An exception pruning one store must never prevent the other from
    # being pruned, or crash the loop -- same isolation principle as
    # supervisor.py's per-watch poll isolation (P1 items #7/#8).
    audit = AuditStore(tmp_path / "audit.db")
    audit.record(action="send_text", session="s", result="SENT")

    class ExplodingV2Store:
        path = tmp_path / "fake.db"

        def prune_actions(self, retention):
            raise RuntimeError("synthetic failure")

    config = MaintenanceConfig(audit_retention=100)
    loop = MaintenanceLoop(
        audit=audit, supervisor2_store=ExplodingV2Store(), bindings_path=None, config=config,
        leases=PaneLeaseStore(tmp_path / "leases.db"),
    )
    result = loop.run_once()  # must not raise
    assert "actions_pruned" not in result
    assert result["audit_pruned"] == 0  # nothing to prune yet, but the audit side still ran


def test_maintenance_loop_starts_and_stops_cleanly(tmp_path):
    audit = AuditStore(tmp_path / "audit.db")
    config = MaintenanceConfig(interval_seconds=60)
    loop = MaintenanceLoop(audit=audit, supervisor2_store=None, bindings_path=None, config=config,
                          leases=PaneLeaseStore(tmp_path / "leases.db"))
    loop.start()
    try:
        deadline = time.monotonic() + 2
        while not loop.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert loop.is_alive()
    finally:
        loop.stop()
    assert not loop.is_alive()


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_maintenance_config_defaults():
    config = MaintenanceConfig()
    assert config.interval_seconds == 1800
    assert config.audit_retention == 20_000
    assert config.action_retention == 5_000
    assert config.idempotency_key_retention_days == 30


def test_load_config_parses_maintenance_block(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "allowed_session_patterns: ['test-*']\n"
        "maintenance:\n"
        "  interval_seconds: 300\n"
        "  audit_retention: 500\n"
        "  action_retention: 100\n"
        "  idempotency_key_retention_days: 7\n"
    )
    config = load_config(config_path)
    assert config.maintenance.interval_seconds == 300
    assert config.maintenance.audit_retention == 500
    assert config.maintenance.action_retention == 100
    assert config.maintenance.idempotency_key_retention_days == 7


def test_load_config_rejects_too_short_maintenance_interval(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "allowed_session_patterns: ['test-*']\nmaintenance:\n  interval_seconds: 5\n"
    )
    import pytest

    with pytest.raises(ValueError, match="interval_seconds"):
        load_config(config_path)


def test_load_config_maintenance_defaults_when_unset(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("allowed_session_patterns: ['test-*']\n")
    config = load_config(config_path)
    assert config.maintenance == MaintenanceConfig()
