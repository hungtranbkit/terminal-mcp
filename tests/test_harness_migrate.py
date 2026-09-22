"""Backup, verify, migrate, roll back -- against a database with data in it.

The failure these defend against is not a broken migration. It is a correct
migration applied to a file nobody could get back: a backup taken with `cp`
from a live WAL database, which opens fine and is missing the last minutes of
work, and which is discovered to be short only when it is needed.
"""
from __future__ import annotations

import sqlite3

import pytest

from terminal_mcp.harness_migrate import (MigrationRefused, backup_then_migrate,
                                          rollback, snapshot, verify_snapshot)
from terminal_mcp.harness_schema import HARNESS_TABLES
from terminal_mcp.queue_store import QUEUE_MIGRATIONS, QueueStore
from terminal_mcp.schema import Migration, get_schema_version


@pytest.fixture
def populated(tmp_path):
    """A v14-era database with real rows: the shape production is in."""
    path = tmp_path / "queue.db"
    store = QueueStore(path)
    store.set_tasks("lane-a", [{"prompt": f"task {i}", "title": f"T{i}"} for i in range(6)])
    # Reconstruct a genuine v14 file: drop what v15/v16 added and wind the
    # stamp back. Winding the stamp back alone would leave the tables in
    # place, and then the migration under test would have nothing to do --
    # a fixture that is easier to satisfy than production tests nothing.
    connection = sqlite3.connect(path)
    try:
        for name in HARNESS_TABLES:
            connection.execute(f'DROP TABLE IF EXISTS "{name}"')
        connection.execute("PRAGMA user_version = 14")
        connection.commit()
    finally:
        connection.close()
    return path


def _counts(path):
    connection = sqlite3.connect(path)
    try:
        return {row[0]: connection.execute(f'SELECT COUNT(*) FROM "{row[0]}"').fetchone()[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'").fetchall()}
    finally:
        connection.close()


def _schema(path):
    connection = sqlite3.connect(path)
    try:
        version = get_schema_version(connection)
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        return version, tables
    finally:
        connection.close()


def test_the_backup_is_a_real_snapshot_not_a_file_copy(populated):
    """A copy of a live WAL database is not a database -- the -wal holds
    committed pages the main file does not have yet."""
    backup = snapshot(populated)
    assert backup.is_file()
    verified = verify_snapshot(populated, backup)
    assert verified["ok"], verified["problems"]
    assert _counts(backup)["queue_tasks"] == 6


def test_a_backup_that_does_not_verify_stops_the_migration(populated, monkeypatch):
    """The whole point of taking the backup first is to be able to refuse."""
    import terminal_mcp.harness_migrate as migrate

    monkeypatch.setattr(migrate, "verify_snapshot", lambda *a, **k: {
        "ok": False, "problems": ["queue_tasks: source has 6 rows, backup has 2"],
        "integrity": "ok"})
    with pytest.raises(MigrationRefused) as excinfo:
        migrate.backup_then_migrate(populated, QUEUE_MIGRATIONS)
    assert "nothing was migrated" in str(excinfo.value)
    assert "backup has 2" in str(excinfo.value)
    connection = sqlite3.connect(populated)
    try:
        assert get_schema_version(connection) == 14, "the database must be untouched"
    finally:
        connection.close()


def test_a_dry_run_reports_what_would_happen_and_changes_nothing(populated):
    report = backup_then_migrate(populated, QUEUE_MIGRATIONS, dry_run=True)
    assert report.dry_run is True
    assert report.applied == (15, 16)
    assert report.version_after == 14
    version, tables = _schema(populated)
    assert version == 14
    assert not (set(HARNESS_TABLES) & tables)


def test_migrating_adds_the_harness_tables_and_touches_no_existing_row(populated):
    before = _counts(populated)
    report = backup_then_migrate(populated, QUEUE_MIGRATIONS)

    assert report.version_before == 14
    assert report.version_after == 16
    assert report.applied == (15, 16)
    assert set(HARNESS_TABLES) <= set(report.tables_added)
    assert report.integrity == "ok"
    assert report.rows_preserved is True
    after = _counts(populated)
    for name, count in before.items():
        assert after[name] == count, f"{name} changed"


def test_running_it_twice_is_a_no_op(populated):
    backup_then_migrate(populated, QUEUE_MIGRATIONS)
    second = backup_then_migrate(populated, QUEUE_MIGRATIONS)
    assert second.applied == ()
    assert second.tables_added == ()
    assert second.rows_preserved is True


def test_rollback_restores_the_version_an_older_binary_will_accept(populated):
    """Additive migrations never need a rollback to recover DATA. They need
    one to recover a VERSION: an older binary refuses a newer user_version."""
    report = backup_then_migrate(populated, QUEUE_MIGRATIONS)
    assert report.version_after == 16

    restored = rollback(populated, report.backup_path)
    assert restored["user_version"] == 14
    version, tables = _schema(populated)
    assert version == 14
    assert not (set(HARNESS_TABLES) & tables)
    assert _counts(populated)["queue_tasks"] == 6


def test_rollback_clears_the_stale_wal_beside_the_restored_file(populated, tmp_path):
    """A restored main file with the old -wal beside it comes back up still
    carrying the pages that were just rolled back."""
    report = backup_then_migrate(populated, QUEUE_MIGRATIONS)
    wal = tmp_path / "queue.db-wal"
    wal.write_bytes(b"stale")
    rollback(populated, report.backup_path)
    assert not wal.exists()


def test_a_corrupt_backup_is_never_restored(populated, tmp_path):
    bad = tmp_path / "corrupt.backup"
    bad.write_bytes(b"not a database at all")
    with pytest.raises(MigrationRefused):
        rollback(populated, bad)


def test_a_migration_that_loses_rows_is_refused_and_names_the_backup(populated):
    """The check that makes "additive" a property of what happened to THIS
    file rather than of the SQL as written."""
    def destructive(connection):
        connection.execute("DELETE FROM queue_tasks")

    with pytest.raises(MigrationRefused) as excinfo:
        backup_then_migrate(
            populated, list(QUEUE_MIGRATIONS) + [Migration(99, "destructive", destructive)])
    assert "restore" in str(excinfo.value)
    assert ".backup" in str(excinfo.value)


def test_migrating_a_database_that_is_not_there_is_refused(tmp_path):
    with pytest.raises(MigrationRefused):
        backup_then_migrate(tmp_path / "nope.db", QUEUE_MIGRATIONS)
