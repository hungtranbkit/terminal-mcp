from __future__ import annotations

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.run_journal import RunJournalStore
from terminal_mcp.work_service import WorkService
from terminal_mcp.work_store import WorkStore


def _config():
    return AppConfig(PermissionsConfig(True, True), ("test-*",), 50, 20,
                     InputPolicyConfig(allowed_session_patterns=("test-*",)))


def _tool(server, name):
    return server._tool_manager._tools[name].fn


def _wired(tmp_path, journal=None):
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    work = WorkService(WorkStore(tmp_path / "work.db"), queue=queue)
    journal = journal if journal is not None else RunJournalStore(tmp_path / "journal.db")
    server = build_mcp(TerminalService(_config()), queue=queue, work=work,
                       run_journal=journal, default_optional_services=False)
    return server, work, journal


def test_checkpoint_resume_and_recovery_surface(tmp_path):
    server, work, journal = _wired(tmp_path)
    created = work.create(title="T", goal="G", lane="test-work", project_id="p")
    work_id = created["work"]["work_id"]
    journal.start_run("p", "test-work", "work-binding", run_id=work_id,
                      root_task_id=work_id, next_action="continue")

    cp = _tool(server, "work_checkpoint")(
        work_id=work_id, idempotency_key="cp-1", state="RUNNING", summary="slice saved",
        completed_json='["a"]', remaining_json='["b"]', evidence_json='{"ok": true}')
    replay = _tool(server, "work_checkpoint")(
        work_id=work_id, idempotency_key="cp-1", state="RUNNING", summary="ignored")
    assert cp["checkpoint_id"] == replay["checkpoint_id"]

    recovered = _tool(server, "work_recover")(project_id="p")
    assert any(row["work_id"] == work_id for row in recovered["works"])
    attached = _tool(server, "work_attach")(work_id=work_id, after_event_id=0)
    assert attached["snapshot"]["work_id"] == work_id
    events = _tool(server, "work_events_since")(work_id=work_id, after_event_id=0)
    assert events["events"]

    recent = _tool(server, "terminal_resume_recent")(project_id="p")
    assert recent[0]["run_id"] == work_id
    assert recent[0]["work_snapshot"]["work_id"] == work_id


def test_checkpoint_rejects_bad_json_without_writing(tmp_path):
    server, work, _journal = _wired(tmp_path)
    work_id = work.create(title="T", goal="G", lane="test-work")["work"]["work_id"]
    result = _tool(server, "work_checkpoint")(
        work_id=work_id, idempotency_key="bad", state="RUNNING", summary="bad",
        completed_json='{"not":"a list"}')
    assert result["error"] == "CHECKPOINT_JSON_INVALID"
    assert work.store.checkpoints_for(work_id) == []


def test_journal_failure_does_not_undo_checkpoint(tmp_path):
    class BrokenJournal:
        def get_run(self, _run_id):
            raise RuntimeError("journal offline")

    server, work, _journal = _wired(tmp_path, journal=BrokenJournal())
    work_id = work.create(title="T", goal="G", lane="test-work")["work"]["work_id"]
    result = _tool(server, "work_checkpoint")(
        work_id=work_id, idempotency_key="cp", state="RUNNING", summary="safe")
    assert result["checkpoint_id"]
    assert "journal offline" in result["journal_error"]
    assert len(work.store.checkpoints_for(work_id)) == 1
