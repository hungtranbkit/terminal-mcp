from __future__ import annotations

import sqlite3

import pytest

from terminal_mcp.work_store import WorkError, WorkStore


def _run_and_task(store: WorkStore, *, queue_task_id: str = "queue-1"):
    run = store.create_run(title="Checkpoint test", goal="Keep callback progress")
    task = store.add_task(run.work_id, title="Do it", queue_task_id=queue_task_id)
    return run, task


def test_checkpoint_persists_across_reopen(tmp_path):
    path = tmp_path / "work.db"
    store = WorkStore(path)
    run, task = _run_and_task(store)
    recorded = store.record_checkpoint(
        run.work_id, work_task_id=task["work_task_id"], idempotency_key="cp-1",
        state="RUNNING", summary="halfway", completed=["a"])
    store.close()

    reopened = WorkStore(path)
    assert reopened.latest_checkpoint(task["work_task_id"]) == recorded
    assert recorded["queue_task_id"] == "queue-1"
    assert recorded["kind"] == "CHECKPOINT"


def test_idempotent_replay_returns_original_and_emits_one_event(tmp_path):
    store = WorkStore(tmp_path / "work.db")
    run, task = _run_and_task(store)
    first = store.record_checkpoint(
        run.work_id, work_task_id=task["work_task_id"], idempotency_key="same",
        state="RUNNING", summary="original", actor="worker")
    replay = store.record_checkpoint(
        "not-the-original-work", idempotency_key="same", state="FAILED",
        summary="retry payload")

    assert replay == first
    assert [event["kind"] for event in store.events_for(run.work_id)].count(
        "checkpoint_recorded") == 1


def test_rejects_unknown_work_and_task_owned_by_another_work(tmp_path):
    store = WorkStore(tmp_path / "work.db")
    run, _ = _run_and_task(store)
    other, other_task = _run_and_task(store, queue_task_id="queue-2")

    with pytest.raises(WorkError, match="unknown work run"):
        store.record_checkpoint(
            "missing", idempotency_key="missing-work", state="RUNNING", summary="no")
    with pytest.raises(WorkError, match="does not belong"):
        store.record_checkpoint(
            run.work_id, work_task_id=other_task["work_task_id"],
            idempotency_key="wrong-owner", state="RUNNING", summary="no")

    assert store.checkpoints_for(other.work_id) == []


def test_latest_and_ordering_are_separate_for_checkpoints_and_results(tmp_path, monkeypatch):
    store = WorkStore(tmp_path / "work.db")
    run, task = _run_and_task(store)
    timestamps = iter(("2026-01-01T00:00:01+00:00", "2026-01-01T00:00:02+00:00",
                       "2026-01-01T00:00:03+00:00"))
    monkeypatch.setattr("terminal_mcp.work_store._now", lambda: next(timestamps))
    first = store.record_checkpoint(
        run.work_id, work_task_id=task["work_task_id"], idempotency_key="first",
        state="RUNNING", summary="first")
    result = store.record_result_manifest(
        run.work_id, work_task_id=task["work_task_id"], idempotency_key="result",
        state="COMPLETE", summary="done")
    latest = store.record_checkpoint(
        run.work_id, work_task_id=task["work_task_id"], idempotency_key="latest",
        state="VERIFYING", summary="latest")

    assert store.latest_checkpoint(task["work_task_id"]) == latest
    assert store.latest_result(task["work_task_id"]) == result
    assert store.checkpoints_for(run.work_id, limit=2) == [latest, result]
    assert first not in store.checkpoints_for(run.work_id, limit=2)


def test_json_fields_round_trip_without_exposing_queue_state(tmp_path):
    store = WorkStore(tmp_path / "work.db")
    run, task = _run_and_task(store)
    values = {
        "completed": [{"step": 1}],
        "remaining": ["tests"],
        "blockers": [{"reason": "waiting", "retry": True}],
        "changed_files": ["terminal_mcp/work_store.py"],
        "evidence": {"pytest": {"passed": 5}},
    }
    row = store.record_result_manifest(
        run.work_id, work_task_id=task["work_task_id"], idempotency_key="json",
        state="COMPLETE", summary="done", next_hint="ship", commit_sha="abc123",
        **values)

    for key, value in values.items():
        assert row[key] == value
    columns = {item[1] for item in sqlite3.connect(store.path).execute(
        "PRAGMA table_info(work_checkpoints)")}
    assert "queue_state" not in columns
