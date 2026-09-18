"""Lifecycle Close-Loop V1 -- the three new migrations, against REAL
pre-existing database files.

A migration that only ever runs on a fresh database proves nothing: the
whole point is the file that already has rows in it. Worse, `schema.py`'s
own apply_migrations cannot roll back DDL -- Python's sqlite3 does not
open a transaction for CREATE/ALTER, so a crash part-way through a
migration leaves its tables on disk while PRAGMA user_version still reads
the OLD value, and the next startup re-runs the whole function. A bare
CREATE TABLE or ALTER TABLE there makes the database permanently
unopenable.

So each migration is exercised twice over: once applied to a file rewound
to its previous schema version (with data in it), and once re-invoked
directly on an already-migrated file, which is exactly the shape of the
crash-and-restart replay.
"""
from __future__ import annotations

import sqlite3

from terminal_mcp.integration_store import IntegrationStore, _add_v3_batch_main_commit_sha
from terminal_mcp.lifecycle_store import LifecycleStore
from terminal_mcp.release_store import ReleaseStore, _add_v2_request_key


def _user_version(path) -> int:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


def _rewind(path, *, version: int, drop_index: str, table: str, column: str) -> None:
    """Turn a migrated file back into its previous on-disk shape -- the
    real thing, not a hand-built stub: the row data stays exactly as the
    production code wrote it."""
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"DROP INDEX IF EXISTS {drop_index}")
        connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    finally:
        connection.close()


def test_release_request_key_migration_applies_to_an_existing_database(tmp_path):
    path = tmp_path / "release.db"
    store = ReleaseStore(path)
    existing = store.create_release(project="p", task_id="t1", environment="dev", artifact_ref="sha1")

    _rewind(path, version=1, drop_index="idx_releases_request_key",
            table="releases", column="request_key")
    assert _user_version(path) == 1

    migrated = ReleaseStore(path)
    assert _user_version(path) == 2
    # The pre-existing row is untouched and still readable.
    recovered = migrated.get_release(existing.id)
    assert recovered is not None
    assert recovered.artifact_ref == "sha1"
    assert recovered.request_key is None
    # And the new guarantee is live on the migrated file.
    first = migrated.create_release(project="p", task_id="t2", environment="dev",
                                    artifact_ref="sha2", request_key="k1")
    second = migrated.create_release(project="p", task_id="t2", environment="dev",
                                     artifact_ref="sha2", request_key="k1")
    assert first.id == second.id
    assert len(migrated.list_releases(project="p")) == 2


def test_release_migration_survives_a_crash_and_replay(tmp_path):
    path = tmp_path / "release.db"
    ReleaseStore(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        # Exactly what a crash-then-restart does: run it again on a file
        # that already has everything it creates.
        _add_v2_request_key(connection)
        _add_v2_request_key(connection)
    finally:
        connection.close()
    assert ReleaseStore(path) is not None


def test_integration_main_commit_sha_migration_applies_to_an_existing_database(tmp_path):
    path = tmp_path / "integration.db"
    store = IntegrationStore(path)
    handoff = store.publish_handoff(project="p", task_id="t1", origin_session="s",
                                    branch="b", commit_sha="c" * 40, base_sha="d" * 40)
    batch = store.create_batch("p", [handoff.id])

    _rewind(path, version=2, drop_index="idx_integration_batches_promoted",
            table="integration_batches", column="main_commit_sha")
    assert _user_version(path) == 2

    migrated = IntegrationStore(path)
    assert _user_version(path) == 3
    recovered = migrated.get_batch(batch.id)
    assert recovered is not None
    assert recovered.main_commit_sha is None, "a batch promoted before this migration reports None"


def test_integration_migration_survives_a_crash_and_replay(tmp_path):
    path = tmp_path / "integration.db"
    IntegrationStore(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        _add_v3_batch_main_commit_sha(connection)
        _add_v3_batch_main_commit_sha(connection)
    finally:
        connection.close()
    assert IntegrationStore(path) is not None


def test_lifecycle_store_survives_a_crash_mid_migration(tmp_path):
    """The failure this project has already been bitten by: tables on
    disk, user_version still old, so v1 runs again from the top."""
    path = tmp_path / "lifecycle.db"
    LifecycleStore(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA user_version = 0")
        connection.commit()
    finally:
        connection.close()

    reopened = LifecycleStore(path)  # must not raise "table already exists"
    assert _user_version(path) == 1
    reopened.record_cleanup(task_id="t1", release_id="r1", project="p", repo_path="/r",
                            worktree_path="/w", branch="b", outcome="CLEANED")
    assert reopened.get_cleanup("t1")["outcome"] == "CLEANED"


def test_lifecycle_claim_settle_is_durable_across_reopen(tmp_path):
    path = tmp_path / "lifecycle.db"
    store = LifecycleStore(path)
    assert store.claim("k1", kind="release") is True
    store.settle("k1", {"action": "CREATED"})

    reopened = LifecycleStore(path)
    assert reopened.claim("k1", kind="release") is False
    assert reopened.result_for("k1") == {"action": "CREATED"}


def test_lifecycle_abandoned_claim_is_reclaimable_but_a_settled_one_is_not(tmp_path):
    store = LifecycleStore(tmp_path / "lifecycle.db")
    assert store.claim("k1", kind="cleanup") is True
    # Still in flight and young -> a concurrent caller must not steal it.
    assert store.claim("k1", kind="cleanup") is False
    # Abandoned (the claimant died) -> reclaimable.
    assert store.claim("k1", kind="cleanup", stale_after_seconds=0) is True

    store.settle("k1", {"action": "CLEANED"})
    # A settled key is never reclaimable, no matter how old.
    assert store.claim("k1", kind="cleanup", stale_after_seconds=0) is False


def test_release_claim_never_reopens_a_settled_effect(tmp_path):
    store = LifecycleStore(tmp_path / "lifecycle.db")
    store.claim("k1", kind="handoff")
    store.settle("k1", {"action": "PUBLISHED"})
    store.release_claim("k1")
    assert store.result_for("k1") == {"action": "PUBLISHED"}
    assert store.claim("k1", kind="handoff") is False

    # An UNSETTLED claim, by contrast, is handed back cleanly.
    store.claim("k2", kind="handoff")
    store.release_claim("k2")
    assert store.claim("k2", kind="handoff") is True


def test_prune_settled_never_removes_an_in_flight_claim(tmp_path):
    store = LifecycleStore(tmp_path / "lifecycle.db")
    store.claim("settled", kind="release")
    store.settle("settled", {"action": "CREATED"})
    store.claim("inflight", kind="release")

    assert store.prune_settled(0) == 0, "a non-positive retention must prune nothing"
    store.prune_settled(-1)
    assert store.result_for("settled") is not None
    assert store.claim("inflight", kind="release") is False, "the in-flight claim must survive"
