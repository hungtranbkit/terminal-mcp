from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.run_journal import RunJournalStore
from terminal_mcp.session_deletion import deletion_preflight


def _services(tmp_path):
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    journal = RunJournalStore(tmp_path / "journal.db")
    return queue, journal


def test_preflight_allows_idle_session(tmp_path):
    queue, journal = _services(tmp_path)
    assert deletion_preflight("idle", queue=queue, run_journal=journal)["ok"] is True


def test_preflight_refuses_queued_or_running_task(tmp_path):
    queue, journal = _services(tmp_path)
    created = queue.enqueue("busy", "tiny task")
    result = deletion_preflight("busy", queue=queue, run_journal=journal)
    assert result["error"] == "SESSION_HAS_ACTIVE_TASK"
    assert result["references"]["active_tasks"][0]["task_id"] == created["task_id"]


def test_preflight_refuses_active_journal_but_preserves_completed_history(tmp_path):
    queue, journal = _services(tmp_path)
    run = journal.start_run("project", "journaled", "binding")
    assert deletion_preflight("journaled", queue=queue, run_journal=journal)["error"] == "SESSION_HAS_ACTIVE_RUN"
    journal.update_run(run["run_id"], state="completed", result_summary="done", completed=True)
    assert deletion_preflight("journaled", queue=queue, run_journal=journal)["ok"] is True


def test_preflight_accepts_qualified_session_references(tmp_path):
    queue, journal = _services(tmp_path)
    queue.enqueue("remote-session", "tiny task")
    result = deletion_preflight("node-a/remote-session", queue=queue, run_journal=journal)
    assert result["error"] == "SESSION_HAS_ACTIVE_TASK"
