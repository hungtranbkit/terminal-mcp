"""Fleet-controller safety checks for destructive session lifecycle calls."""
from __future__ import annotations

from typing import Any


def deletion_preflight(session: str, *, queue: Any, run_journal: Any,
                       supervisor: Any = None, direct_tasks: Any = None) -> dict[str, Any]:
    """Return references and refuse only work that could still execute.

    The owning node performs the final attached/lease/recovery checks next to
    the actual kill.  This controller-side half protects the durable stores
    that intentionally do not exist on remote node agents.
    """
    if run_journal is not None and hasattr(run_journal, "reap_stale_waits"):
        # Abandoned wait continuations are passive observers; finalize them
        # so they can never accumulate into a permanent delete blocker.
        run_journal.reap_stale_waits()
    task_refs = queue.deletion_references(session) if queue is not None else []
    direct_refs = direct_tasks.active_for(session) if direct_tasks is not None else []
    journal_refs = run_journal.active_for_session(session) if run_journal is not None else []
    references = {
        "active_tasks": task_refs,
        "active_runs": [{"run_id": row.get("run_id"), "state": row.get("state")}
                        for row in journal_refs],
        "supervisor_watches": [],
        "supervised_tasks": [{"task_id": row.get("task_id"), "state": row.get("state")}
                             for row in direct_refs],
    }
    if supervisor is not None:
        listed = supervisor.list_watches()
        watches = listed.get("watches", []) if isinstance(listed, dict) else listed
        bare = session.split("/", 1)[-1]
        references["supervisor_watches"] = [
            {"watch_key": row.get("watch_key"), "enabled": bool(row.get("enabled"))}
            for row in watches if row.get("target") in {session, bare}
        ]
    if task_refs:
        return {"error": "SESSION_HAS_ACTIVE_TASK", "session": session, "references": references}
    if journal_refs:
        return {"error": "SESSION_HAS_ACTIVE_RUN", "session": session, "references": references}
    if direct_refs:
        return {"error": "SESSION_HAS_ACTIVE_SUPERVISED_TASK", "session": session,
                "references": references,
                "next_action": "supervise_complete or supervise_cancel the task, then delete"}
    if run_journal is not None and hasattr(run_journal, "cancel_waits_for_target"):
        references["cancelled_waits"] = run_journal.cancel_waits_for_target(
            session, reason="session deletion requested")
    return {"ok": True, "session": session, "references": references}


def delete_ui_blocker(row: dict[str, Any], *, queue_references: list[dict[str, Any]],
                      journal_references: list[dict[str, Any]]) -> str | None:
    if row.get("attached"):
        return "SESSION_ATTACHED"
    if queue_references:
        return "SESSION_HAS_ACTIVE_TASK"
    if journal_references:
        return "SESSION_HAS_ACTIVE_RUN"
    return None
