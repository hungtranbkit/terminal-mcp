"""Policy gate for the retired Terminal MCP durable task queue.

The queue database and readers stay available so historical/running tasks can be
observed and cleaned up. New queue submissions and manual queue dispatch are
disabled by default. Direct session control (create_session + send/send_wait)
is the supported workflow.
"""
from __future__ import annotations

import os
from typing import Any

QUEUE_DISABLED_ERROR = "QUEUE_DISABLED_USE_DIRECT_SESSION"
QUEUE_DISABLED_MESSAGE = (
    "Terminal MCP queue submission is disabled, so this created NO task and dispatched "
    "nothing. For tracked work use create_session (no initial_prompt), then "
    "terminal_turn action=supervise with text and an idempotency_key; it returns a durable "
    "task_id that action=supervise_status reads from any client."
)
#: The direct replacement for a queue `start`, as concrete call shapes (issue
#: #52): a client told only "create_session_then_send" had no task_id to track.
QUEUE_DISABLED_NEXT_CALLS = (
    {"action": "create_session", "target": "<new session name>", "agent_type": "claude|codex",
     "working_directory": "<repo>", "note": "omit initial_prompt; dispatch with supervise"},
    {"action": "supervise", "target": "<session>", "text": "<task>",
     "idempotency_key": "<stable key; reuse it when retrying>", "title": "<short summary>"},
    {"action": "supervise_status", "task_id": "<dt_... from supervise>"},
)

QUEUE_SUBMISSION_ACTIONS = frozenset({
    "start",
    "enqueue_task",
    "route_start",
    "agent_start",
    "project_start",
    "queue_resume",
    "queue_run_once",
})

QUEUE_DISPATCH_ACTIONS = frozenset({
    "task_route",
    "queue_rescue_once",
    "queue_run_once",
    "queue_loop_run_once",
})


def queue_submission_enabled() -> bool:
    """Server-side escape hatch; disabled unless explicitly opted in."""
    return os.environ.get("TERMINAL_MCP_ENABLE_QUEUE", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def queue_disabled_response(*, action: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "FAILED",
        "error": QUEUE_DISABLED_ERROR,
        "message": QUEUE_DISABLED_MESSAGE,
        "queue_submission_enabled": False,
        "next_action": "create_session_then_supervise",
        "next_calls": [dict(call) for call in QUEUE_DISABLED_NEXT_CALLS],
        "task_created": False,
    }
    if action:
        result["action"] = action
    return result
