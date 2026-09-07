"""PM summary + backlog hygiene -- docs/REQUIREMENTS.md §20.6 Phase D.
A new, small AGGREGATION over already-real data (`QueueService.board`/
`pending_counts`/`list_active_incidents`, optionally `ControllerService.
list_nodes` for real CPU/RAM/capacity_status per node, §12's own
already-real `host_metrics.py` collection surfaced through it) -- never
a new source of truth, never a second task/event store. "Human controls
destructive close" (task's own explicit words): every hygiene finding
(stale task, duplicate group) is READ-ONLY detection; the one action
that actually changes anything (`close_task_with_confirmation`) refuses
outright unless the caller explicitly passes `confirmed=True` -- there
is no automatic close, ever, from this module.

Deliberately NOT wired into a background loop/scheduler here -- "daily/
weekly" is a real, disclosed scope cut: this module computes the
summary/hygiene report fresh on every call (same "no auto-loop yet"
posture as PM/Planner's own route_all_unassigned), leaving an actual
cron/scheduled trigger as a future increment (a plain `terminal_pm_
daily_summary` MCP tool call today, in whatever cadence a caller
chooses, already gets the real, current data)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .queue_service import QueueService

DEFAULT_STALE_AFTER_HOURS = 24.0


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def generate_summary(queue: QueueService, *, controller: Any | None = None) -> dict[str, Any]:
    """Real, fleet-wide snapshot -- every field a direct read of
    already-real data, nothing re-derived/guessed. `controller` is
    OPTIONAL (best-effort, same posture as pm_service.py's own optional
    controller) -- when given, adds a real per-node capacity summary
    (§12's own already-real CPU/RAM/capacity_status collection, no new
    metrics code)."""
    board = queue.board()
    pending = queue.pending_counts()
    incidents = queue.list_active_incidents()
    summary: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "board_counts": board["counts"],
        "total_pending_tasks": sum(pending.values()),
        "lanes_with_pending_work": len(pending),
        "active_incidents": incidents["count"],
    }
    if controller is not None:
        try:
            nodes = controller.list_nodes()
        except Exception:  # noqa: BLE001 -- best-effort only, never blocks the summary
            nodes = []
        summary["node_capacity"] = [
            {"node_id": node.id, "status": node.status, "capacity_status": node.capacity_status,
             "cpu_percent": node.cpu_percent, "ram_percent": node.ram_percent}
            for node in nodes
        ]
    return summary


def detect_stale_backlog_tasks(queue: QueueService, *,
                               stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS) -> dict[str, Any]:
    """Real backlog hygiene: any Backlog/Queued task whose own
    `created_at` is older than `stale_after_hours` -- flagged for human
    review, NEVER auto-closed. RUNNING/blocked-review/done tasks are
    never flagged as "stale backlog" (they are not sitting idle in the
    same sense)."""
    board = queue.board()
    now = datetime.now(timezone.utc)
    stale: list[dict[str, Any]] = []
    for task in board["backlog"] + board["queued"]:
        created_at = _parse_iso(task.get("created_at"))
        if created_at is None:
            continue
        age_hours = (now - created_at).total_seconds() / 3600.0
        if age_hours >= stale_after_hours:
            stale.append({**task, "age_hours": round(age_hours, 1)})
    stale.sort(key=lambda t: t["age_hours"], reverse=True)
    return {"stale_tasks": stale, "count": len(stale), "stale_after_hours": stale_after_hours}


def detect_duplicate_tasks(queue: QueueService) -> dict[str, Any]:
    """Real, mechanical duplicate detection: two or more still-open
    tasks (not `done`) with the IDENTICAL prompt text -- flagged, never
    auto-merged/closed. A byte-for-byte match only (never a fuzzy/
    semantic guess -- this project's own standing "deterministic,
    never guessed" discipline)."""
    board = queue.board()
    open_tasks = board["backlog"] + board["queued"] + board["running"] + board["blocked_review"]
    by_prompt: dict[str, list[dict[str, Any]]] = {}
    for task in open_tasks:
        by_prompt.setdefault(task["prompt"], []).append(task)
    duplicate_groups = [
        {"prompt": prompt, "task_ids": [t["id"] for t in tasks], "count": len(tasks)}
        for prompt, tasks in by_prompt.items() if len(tasks) > 1
    ]
    duplicate_groups.sort(key=lambda g: g["count"], reverse=True)
    return {"duplicate_groups": duplicate_groups, "count": len(duplicate_groups)}


EMERGENCY_STOP_REASON_PREFIX = "EMERGENCY STOP:"
"""Marks a lane's `paused_reason` as one set BY emergency_stop_all_lanes
(rather than an unrelated, pre-existing manual pause) so
emergency_resume_all_lanes can tell the two apart."""


def emergency_stop_all_lanes(queue: QueueService, *, reason: str, confirmed: bool = False) -> dict[str, Any]:
    """Fleet-wide Emergency Stop (§20.6 Phase E, "security/control-plane"
    -- an operator/PM needs one big red button when something is
    genuinely going wrong across the fleet). Pauses EVERY lane at once
    via the existing, real, already-idempotent QueueService.pause/
    QueueStore.pause_lane mechanism (§10) -- never a new stop/kill
    mechanism, and this is a QUEUE DISPATCH stop only: it never touches
    a session's own tmux/ConPTY process, never sends anything, never
    kills a session (this project's own standing "never disrupt a real
    attended session" discipline). A lane already paused (for any
    reason) is left untouched -- its own existing paused_reason is not
    overwritten. Same "human controls destructive action, refuses
    without confirmed=True" posture as close_task_with_confirmation
    above -- blast radius here is the WHOLE fleet, so the confirmation
    gate matters even more here, not less."""
    if not confirmed:
        return {"error": "CONFIRMATION_REQUIRED",
                "detail": "emergency stop pauses EVERY lane in the fleet -- call again with confirmed=true"}
    if not reason:
        return {"error": "REASON_REQUIRED"}
    paused_lanes = []
    for lane in queue.store.list_all_lanes():
        if lane["paused"]:
            continue
        queue.pause(lane["session"], reason=f"{EMERGENCY_STOP_REASON_PREFIX} {reason}")
        paused_lanes.append(lane["session"])
    return {"paused_lanes": paused_lanes, "count": len(paused_lanes), "reason": reason}


def emergency_resume_all_lanes(queue: QueueService) -> dict[str, Any]:
    """The Emergency Stop undo -- resumes ONLY lanes whose current
    paused_reason was set BY emergency_stop_all_lanes (starts with
    EMERGENCY_STOP_REASON_PREFIX). A lane a human/PM had already
    deliberately paused for an unrelated reason BEFORE the emergency
    stop is left exactly as they left it -- this never guesses at
    whether an unrelated pause is also "safe" to lift."""
    resumed_lanes = []
    for lane in queue.store.list_all_lanes():
        if lane["paused"] and (lane["paused_reason"] or "").startswith(EMERGENCY_STOP_REASON_PREFIX):
            queue.resume(lane["session"])
            resumed_lanes.append(lane["session"])
    return {"resumed_lanes": resumed_lanes, "count": len(resumed_lanes)}


def close_task_with_confirmation(queue: QueueService, task_id: str, *, reason: str,
                                 confirmed: bool = False) -> dict[str, Any]:
    """The ONLY action in this module that changes anything -- refuses
    outright (CONFIRMATION_REQUIRED) unless `confirmed=True` is
    explicitly passed, and always requires a real `reason` -- "human
    controls destructive close" (task's own explicit words), never an
    automatic hygiene sweep that closes anything by itself. Reuses the
    existing, real cancel mechanism (`QueueStore.cancel_task`) -- never
    a second way to close a task."""
    if not confirmed:
        return {"error": "CONFIRMATION_REQUIRED", "task_id": task_id,
                "reason": "backlog hygiene never auto-closes a task -- a human must review it and "
                         "call this again with confirmed=true"}
    if not reason:
        return {"error": "REASON_REQUIRED", "task_id": task_id}
    existing = queue.store.get_task(task_id)
    if existing is None:
        return {"error": "TASK_NOT_FOUND", "task_id": task_id}
    try:
        updated = queue.store.cancel_task(task_id)
    except Exception as exc:  # noqa: BLE001 -- surfaces InvalidTransitionError etc. as a real error, not a crash
        return {"error": "CANCEL_FAILED", "task_id": task_id, "detail": str(exc)}
    return {"task": updated.to_dict(), "reason": reason}
