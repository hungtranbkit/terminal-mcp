from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from terminal_mcp.run_journal import (
    MAX_ACTION_CHARS,
    MAX_ERROR_CHARS,
    MAX_SUMMARY_CHARS,
    RunJournalStore,
)


def _start(store: RunJournalStore, run_id: str = "run-1", project: str = "project-a"):
    return store.start_run(
        project, "session-a", "binding-a", run_id=run_id,
        root_task_id="work-1", metadata={"attempt": 1, "owner": "worker-a"},
    )


def test_default_path_permissions_reopen_and_wal(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    first = RunJournalStore()
    _start(first)
    first.record("run-1", "event-1", tool_name="terminal_status", state="working")

    assert first.path == tmp_path / "terminal-mcp" / "run_journal.db"
    assert first.path.parent.stat().st_mode & 0o777 == 0o700
    assert first.path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(first.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    reopened = RunJournalStore(first.path)
    assert reopened.get_run("run-1")["root_task_id"] == "work-1"
    assert reopened.entries("run-1")[0]["event_key"] == "event-1"


def test_start_and_event_are_idempotent(tmp_path):
    store = RunJournalStore(tmp_path / "journal.db")
    original = _start(store)
    replayed_run = store.start_run(
        "different", "different", "different", run_id="run-1", state="different",
        metadata={"prompt": "ignored during an idempotent replay"},
    )
    assert replayed_run == original

    original_entry = store.record(
        "run-1", "event-1", tool_name="terminal_send", state="working",
        result_summary="sent command",
    )
    replayed_entry = store.record(
        "run-1", "event-1", tool_name="other", state="failed", error=object(),
    )
    assert replayed_entry == original_entry
    assert len(store.entries("run-1")) == 1


def test_entries_cursor_order_and_limit_clamp(tmp_path):
    store = RunJournalStore(tmp_path / "journal.db")
    _start(store)
    made = [
        store.record("run-1", f"event-{i}", tool_name="tool", state="working")
        for i in range(205)
    ]
    page = store.entries("run-1", after_id=made[1]["id"], limit=500)
    assert [entry["id"] for entry in page] == sorted(entry["id"] for entry in page)
    assert page[0]["id"] == made[2]["id"]
    assert len(page) == 200


def test_recent_active_order_completion_and_project_filter(tmp_path):
    store = RunJournalStore(tmp_path / "journal.db")
    _start(store, "old-a", "project-a")
    _start(store, "only-b", "project-b")
    _start(store, "new-a", "project-a")
    store.update_run("old-a", state="done", completed=True, result_summary="complete")
    store.update_run("only-b", next_action="continue")
    store.record("new-a", "new-event", tool_name="status", state="waiting")

    assert [run["run_id"] for run in store.recent(active_only=True)] == ["new-a", "only-b"]
    assert [run["run_id"] for run in store.recent(project_id="project-a")] == ["new-a", "old-a"]
    completed = store.get_run("old-a")
    assert completed["state"] == "done"
    assert completed["completed_at"] is not None


def test_record_updates_run_and_resume_is_compact_with_latest_cursor(tmp_path):
    store = RunJournalStore(tmp_path / "journal.db")
    _start(store)
    first = store.record(
        "run-1", "one", tool_name="send", state="working", next_action="check",
    )
    latest = store.record(
        "run-1", "two", tool_name="status", state="waiting", result_summary="blocked",
    )

    run = store.get_run("run-1")
    assert run["state"] == "waiting"
    assert run["next_action"] == "check"
    resumed = store.resume_recent()
    assert len(resumed) == 1
    assert resumed[0]["latest_entry"] == latest
    assert resumed[0]["cursor"] == latest["id"] > first["id"]


def test_bounded_redacted_fields_and_restricted_metadata(tmp_path):
    store = RunJournalStore(tmp_path / "journal.db")
    run = store.start_run(
        "p", "s", "b", run_id="run-1",
        next_action="a" * (MAX_ACTION_CHARS + 10),
        result_summary="token=plain-secret " + "s" * (MAX_SUMMARY_CHARS + 10),
        metadata={"access_key": "token=metadata-secret"},
    )
    assert len(run["next_action"]) == MAX_ACTION_CHARS
    assert len(run["result_summary"]) <= MAX_SUMMARY_CHARS
    assert "plain-secret" not in run["result_summary"]
    assert "metadata-secret" not in str(run["metadata"])

    entry = store.record(
        "run-1", "event", tool_name="tool", state="failed",
        error="e" * (MAX_ERROR_CHARS + 10),
        next_action="a" * (MAX_ACTION_CHARS + 10),
        result_summary="s" * (MAX_SUMMARY_CHARS + 10),
    )
    assert len(entry["error"]) == MAX_ERROR_CHARS
    assert len(entry["next_action"]) == MAX_ACTION_CHARS
    assert len(entry["result_summary"]) == MAX_SUMMARY_CHARS

    with pytest.raises(ValueError, match="not allowed"):
        store.start_run("p", "s", "b", run_id="bad", metadata={"prompt": "do this"})
    with pytest.raises(TypeError, match="JSON scalars"):
        store.start_run("p", "s", "b", run_id="nested", metadata={"safe": ["not scalar"]})


def test_unknown_run_fails_clearly(tmp_path):
    store = RunJournalStore(tmp_path / "journal.db")
    with pytest.raises(KeyError, match="unknown run: missing"):
        store.get_run("missing")
    with pytest.raises(KeyError, match="unknown run: missing"):
        store.record("missing", "event", tool_name="tool", state="working")
    with pytest.raises(KeyError, match="unknown run: missing"):
        store.entries("missing")
    with pytest.raises(KeyError, match="unknown run: missing"):
        store.update_run("missing", state="failed")


def test_schema_has_no_payload_columns(tmp_path):
    store = RunJournalStore(tmp_path / "journal.db")
    with sqlite3.connect(store.path) as connection:
        run_columns = {row[1] for row in connection.execute("PRAGMA table_info(journal_runs)")}
        entry_columns = {row[1] for row in connection.execute("PRAGMA table_info(journal_entries)")}
    forbidden = {"prompt", "args", "arguments", "output", "tool_output", "transcript", "secrets"}
    assert not (run_columns | entry_columns) & forbidden


def test_schema_creation_is_additive(tmp_path):
    path = tmp_path / "journal.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('keep')")
    RunJournalStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM unrelated").fetchone()[0] == "keep"
