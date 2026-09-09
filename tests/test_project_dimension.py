"""P0.1 Project Dimension -- migration, backfill, isolation, and the
backward-compatibility contract.

The contract being pinned: a task with `project_id IS NULL` behaves
EXACTLY as it did before migration v6. Every legacy row stays NULL, no
existing query starts filtering implicitly, and no existing API signature
changes.
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from terminal_mcp.queue_store import (COMPLETED, DISPATCHING, QUEUED, RUNNING, VERIFYING,
                                      QueueStore)

PROD_QUEUE_DB = Path.home() / ".local/state/terminal-mcp/queue.db"


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


# --------------------------------------------------------------- migration
def test_migration_adds_column_and_indexes(tmp_path):
    store = QueueStore(tmp_path / "q.db")
    c = sqlite3.connect(store.path)
    cols = {r[1] for r in c.execute("PRAGMA table_info(queue_tasks)")}
    assert "project_id" in cols
    idx = {r[0] for r in c.execute("select name from sqlite_master where type='index'")}
    assert "idx_queue_tasks_project_status" in idx
    assert "idx_queue_lanes_project" in idx
    assert c.execute("PRAGMA user_version").fetchone()[0] >= 6


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "q.db"
    QueueStore(path)
    QueueStore(path)                       # must not raise "duplicate column"
    QueueStore(path)
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] >= 6


@pytest.mark.skipif(not PROD_QUEUE_DB.exists(), reason="no real queue.db on this host")
def test_migrates_a_copy_of_the_REAL_production_database(tmp_path):
    """The migration must be safe on the database that actually exists,
    not just on a fresh one."""
    copy = tmp_path / "prod.db"
    shutil.copy(PROD_QUEUE_DB, copy)
    before = sqlite3.connect(copy)
    tasks_before = before.execute("select count(*) from queue_tasks").fetchone()[0]
    events_before = before.execute("select count(*) from queue_events").fetchone()[0]
    before.close()

    QueueStore(copy)

    after = sqlite3.connect(copy)
    assert after.execute("select count(*) from queue_tasks").fetchone()[0] == tasks_before
    assert after.execute("select count(*) from queue_events").fetchone()[0] == events_before
    # every pre-existing row is untouched/unscoped
    assert after.execute(
        "select count(*) from queue_tasks where project_id is null").fetchone()[0] == tasks_before


# ------------------------------------------------- backward compatibility
def test_tasks_created_without_a_project_are_null(store):
    [task_id] = store.append_tasks("s1", [{"prompt": "p"}])
    task = store.get_task(task_id)
    assert task.project_id is None
    assert task.to_dict()["project_id"] is None


def test_legacy_reads_are_unfiltered(store):
    [legacy] = store.append_tasks("s1", [{"prompt": "a"}])
    [scoped] = store.append_tasks("s2", [{"prompt": "b", "project_id": "git:acme/w"}])
    # the pre-existing per-task read still works for BOTH, unchanged
    assert store.get_task(legacy).prompt == "a"
    assert store.get_task(scoped).prompt == "b"
    # and the pre-existing dispatch candidate read is not project-filtered
    assert store.next_dispatchable_task("s1") is not None


def test_dispatch_path_unaffected_by_a_null_project(store):
    store.append_tasks("s1", [{"prompt": "a"}])
    claimed = store.claim_next_task("s1", claimed_by="engine")
    assert claimed is not None and claimed.project_id is None


# ------------------------------------------------------------- project use
def test_project_id_is_stored_and_returned(store):
    [task_id] = store.append_tasks("s1", [{"prompt": "a", "project_id": "git:acme/w"}])
    assert store.get_task(task_id).project_id == "git:acme/w"


def test_list_tasks_for_project_isolates(store):
    store.append_tasks("s1", [{"prompt": "a", "project_id": "git:acme/a"}])
    store.append_tasks("s2", [{"prompt": "b", "project_id": "git:acme/b"}])
    store.append_tasks("s3", [{"prompt": "legacy"}])
    a = store.list_tasks_for_project("git:acme/a")
    assert [t.prompt for t in a] == ["a"]
    # a legacy NULL task belongs to no project and is never returned
    assert all(t.prompt != "legacy" for t in store.list_tasks_for_project("git:acme/b"))


def test_project_task_counts(store):
    store.append_tasks("s1", [{"prompt": "a", "project_id": "p"}, {"prompt": "b", "project_id": "p"}])
    assert store.project_task_counts("p") == {QUEUED: 2}
    assert store.project_task_counts("other") == {}


def test_set_task_project(store):
    [task_id] = store.append_tasks("s1", [{"prompt": "a"}])
    assert store.set_task_project(task_id, "git:acme/w") is True
    assert store.get_task(task_id).project_id == "git:acme/w"


# ------------------------------------------------------------- backfill
def test_backfill_is_dry_run_by_default(store):
    [task_id] = store.append_tasks("s1", [{"prompt": "a"}])
    plan = store.backfill_project_ids(lambda s: "git:acme/w")
    assert plan["dry_run"] is True and plan["planned"] == 1
    assert store.get_task(task_id).project_id is None                # nothing written


def test_backfill_applies_when_asked(store):
    [a_id] = store.append_tasks("s1", [{"prompt": "a"}])
    [b_id] = store.append_tasks("s2", [{"prompt": "b"}])
    out = store.backfill_project_ids(lambda s: {"s1": "git:acme/a"}.get(s), dry_run=False)
    assert out["planned"] == 1 and out["unresolved"] == 1
    assert store.get_task(a_id).project_id == "git:acme/a"
    assert store.get_task(b_id).project_id is None                    # unresolved stays NULL


def test_backfill_never_overwrites_an_existing_project(store):
    [task_id] = store.append_tasks("s1", [{"prompt": "a", "project_id": "git:acme/original"}])
    store.backfill_project_ids(lambda s: "git:acme/WRONG", dry_run=False)
    assert store.get_task(task_id).project_id == "git:acme/original"


def test_backfill_tolerates_a_failing_resolver(store):
    [task_id] = store.append_tasks("s1", [{"prompt": "a"}])

    def boom(session):
        raise RuntimeError("git exploded")

    out = store.backfill_project_ids(boom, dry_run=False)
    assert out["planned"] == 0 and out["unresolved"] == 1
    assert store.get_task(task_id).project_id is None


def test_backfill_reports_by_project(store):
    store.append_tasks("s1", [{"prompt": "a"}])
    store.append_tasks("s2", [{"prompt": "b"}])
    out = store.backfill_project_ids(lambda s: "git:acme/w")
    assert out["by_project"] == {"git:acme/w": 2}


# ------------------------------------------------------------ lane reuse
def test_lane_project_column_is_reused_not_duplicated(store):
    """v4 already added queue_lanes.project; P0.1 must reuse it rather
    than adding a second competing column."""
    store.append_tasks("s1", [{"prompt": "a"}])
    store.set_lane_project("s1", "git:acme/w")
    cols = {r[1] for r in sqlite3.connect(store.path).execute("PRAGMA table_info(queue_lanes)")}
    assert "project" in cols
    assert "project_id" not in cols, "a duplicate lane project column was created"
    assert any(l["project"] == "git:acme/w" for l in store.list_all_lanes())
