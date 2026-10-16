"""Machine-readable contract for ChatGPT's direct Terminal MCP execution path.

This module is intentionally pure data. It must not import Paperclip or any
orchestration runtime. Paperclip is an optional, separate control plane; direct
Terminal MCP remains available for inspection, debugging, remote execution,
browser verification, and break-glass operations.
"""

from __future__ import annotations

DIRECT_CONTRACT_VERSION = "1.1.0"

# Canonical terminal_turn actions that must remain usable without Paperclip.
REQUIRED_DIRECT_TURN_ACTIONS = frozenset({
    "start",
    "inspect",
    "send",
    "send_wait",
    "wait",
    "resume",
    "list_sessions",
    "list_nodes",
    "create_session",
    "delete_session",
    "task_status",
    "task_batch_status",
    "browser_verify",
    "browser_screenshot",
    "browser_status",
    "browser_stop",
})

# Public standalone tools that must remain discoverable for direct/break-glass use.
REQUIRED_DIRECT_TOOLS = frozenset({
    "terminal_turn",
    "terminal_list_sessions",
    "terminal_list_nodes",
    "terminal_create_session",
    "terminal_delete_session",
    "terminal_task_status",
    "terminal_task_batch_status",
    "browser_verify",
    "browser_screenshot",
    "browser_status",
    "browser_stop",
})

# Queue/orchestration entry points intentionally excluded from the direct contract.
RETIRED_QUEUE_ACTIONS = frozenset({
    "enqueue_task",
    "route_start",
    "agent_start",
    "project_start",
    "enqueue",
})

DIRECT_USE_CASES = (
    "inspect_or_debug",
    "remote_machine_operation",
    "browser_verification",
    "break_glass_recovery",
    "short_targeted_execution",
)

PAPERCLIP_USE_CASES = (
    "project_issue_orchestration",
    "agent_assignment",
    "heartbeat_and_long_running_coordination",
    "budget_and_governance",
    "workspace_or_worktree_lifecycle",
)


def contract() -> dict[str, object]:
    """Return a serialization-friendly snapshot for docs/tests/clients."""

    return {
        "version": DIRECT_CONTRACT_VERSION,
        "independentOfPaperclip": True,
        "requiredTurnActions": sorted(REQUIRED_DIRECT_TURN_ACTIONS),
        "requiredTools": sorted(REQUIRED_DIRECT_TOOLS),
        "codexDirectStart": {"requiresIdle": True, "requiresRunning": True,
                             "remoteIdleGuardSupported": False, "aliases": ["run", "dispatch"]},
        "retiredQueueActions": sorted(RETIRED_QUEUE_ACTIONS),
        "directUseCases": list(DIRECT_USE_CASES),
        "paperclipUseCases": list(PAPERCLIP_USE_CASES),
    }
