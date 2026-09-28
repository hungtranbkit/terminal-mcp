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
    "Terminal MCP queue submission is disabled. "
    "Use create_session, then direct send/send_wait to that session."
)

QUEUE_SUBMISSION_ACTIONS = frozenset({
    "start",
    "enqueue_task",
    "route_start",
    "agent_start",
    "project_start",
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
        "next_action": "create_session_then_send",
    }
    if action:
        result["action"] = action
    return result
