"""P1 hardening item #10: real SQLite schema version tracking."""
from __future__ import annotations

import sqlite3

from terminal_mcp.audit import AuditStore
from terminal_mcp.schema import Migration, apply_migrations, get_schema_version
from terminal_mcp.supervisor import SupervisorStore
from terminal_mcp.supervisor2 import SupervisorV2Store


def test_apply_migrations_applies_in_ascending_order_and_stamps_version():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE t (x INTEGER)")
    order = []
    migrations = [
        Migration(2, "second", lambda c: order.append(2)),
        Migration(1, "first", lambda c: order.append(1)),  # deliberately out of order in the list
        Migration(3, "third", lambda c: order.append(3)),
    ]
    applied = apply_migrations(connection, migrations)
    assert order == [1, 2, 3]  # applied in version order, not list order
    assert applied == [1, 2, 3]
    assert get_schema_version(connection) == 3


def test_apply_migrations_is_a_noop_once_current():
    connection = sqlite3.connect(":memory:")
    migrations = [Migration(1, "baseline", lambda c: None)]
    assert apply_migrations(connection, migrations) == [1]
    assert apply_migrations(connection, migrations) == []  # second open: nothing to do
    assert get_schema_version(connection) == 1


def test_apply_migrations_only_applies_versions_above_current():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA user_version = 2")
    order = []
    migrations = [
        Migration(1, "already applied", lambda c: order.append(1)),
        Migration(2, "already applied", lambda c: order.append(2)),
        Migration(3, "new", lambda c: order.append(3)),
    ]
    applied = apply_migrations(connection, migrations)
    assert order == [3]
    assert applied == [3]
    assert get_schema_version(connection) == 3


def test_apply_migrations_runs_each_migration_in_its_own_transaction():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE t (x INTEGER)")

    def _insert_and_fail(c):
        c.execute("INSERT INTO t VALUES (1)")
        raise RuntimeError("simulated failure mid-migration")

    migrations = [Migration(1, "will fail", _insert_and_fail)]
    try:
        apply_migrations(connection, migrations)
    except RuntimeError:
        pass
    # The insert was rolled back along with the failed migration -- never
    # left half-applied, and the version counter was never bumped for a
    # migration that didn't actually complete.
    assert list(connection.execute("SELECT * FROM t")) == []
    assert get_schema_version(connection) == 0


def test_real_stores_stamp_a_nonzero_schema_version(tmp_path):
    # Every real store in this project is stamped, not left at SQLite's
    # default of 0 (which would be indistinguishable from "never opened
    # under this system at all").
    audit = AuditStore(tmp_path / "audit.db")
    with sqlite3.connect(audit.path) as connection:
        assert get_schema_version(connection) >= 1

    bindings_path = tmp_path / "bindings.db"
    from terminal_mcp.bindings import BindingStore

    BindingStore(bindings_path)
    with sqlite3.connect(bindings_path) as connection:
        assert get_schema_version(connection) >= 1


def test_v1_and_v2_stores_sharing_one_file_layer_versions_correctly(tmp_path):
    # Real production ordering: SupervisorStore (v1) always opens the
    # shared db file first (build_supervisor_v2 constructs
    # SupervisorV2Store(v1.store.path)) -- v1's baseline (version 1) is
    # stamped first, then v2's own baseline (version 2) layers on top of
    # it on the SAME file/counter, never racing or overwriting it.
    path = tmp_path / "supervisor.db"
    v1_store = SupervisorStore(path)
    with sqlite3.connect(path) as connection:
        version_after_v1 = get_schema_version(connection)
    assert version_after_v1 == 1

    SupervisorV2Store(v1_store.path)
    with sqlite3.connect(path) as connection:
        version_after_v2 = get_schema_version(connection)
    assert version_after_v2 == 2

    # Reopening either store again is a pure no-op version-wise.
    SupervisorStore(path)
    SupervisorV2Store(path)
    with sqlite3.connect(path) as connection:
        assert get_schema_version(connection) == 2
