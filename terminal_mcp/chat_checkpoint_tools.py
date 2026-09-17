"""MCP tool registration for durable ChatGPT orchestration recovery."""
from __future__ import annotations

from typing import Any

from .orchestrator_checkpoint import OrchestratorCheckpointError, OrchestratorCheckpointStore


def register_chat_checkpoint_tools(server: Any, store: OrchestratorCheckpointStore, projects: Any) -> None:
    def call(operation, *args, **kwargs) -> dict:
        try:
            return operation(*args, **kwargs)
        except OrchestratorCheckpointError as exc:
            return exc.to_dict()

    @server.tool()
    def terminal_chat_checkpoint(
        project_id: str,
        summary: str,
        current_goal: str = "",
        decisions: list | None = None,
        active_tasks: list | None = None,
        blockers: list | None = None,
        next_actions: list | None = None,
        merge_deploy_state: str = "",
        source_chat: str | None = None,
    ) -> dict:
        """Persist ChatGPT-level orchestration state for one project.

        Call BEFORE long analysis or multi-agent delegation, AFTER important
        decisions/dispatches, and BEFORE merge/deploy. Include the goal,
        decisions, delegated task/session/branch status, blockers and next
        actions a new ChatGPT thread would otherwise lose. The write is
        append-only and never starts or changes project work.
        """
        return call(
            store.checkpoint,
            project_id,
            summary,
            current_goal=current_goal,
            decisions=decisions,
            active_tasks=active_tasks,
            blockers=blockers,
            next_actions=next_actions,
            merge_deploy_state=merge_deploy_state,
            source_chat=source_chat,
        )

    @server.tool()
    def terminal_chat_recover(project_id: str) -> dict:
        """Recover after a ChatGPT thread/new-chat reset.

        In a NEW CHAT, call this FIRST for the project. It returns the latest
        durable orchestration checkpoint plus `live_project_status` read fresh
        from queue/backlog/worker state. Then use terminal_knowledge_recover
        for any terminal session that needs lower-level context.
        """
        result = call(store.recover, project_id)
        if "error" in result:
            return result
        try:
            result["live_project_status"] = projects.status(project_id)
        except Exception:  # recovery must survive an unavailable live projection
            result["live_project_status"] = {
                "error": "PROJECT_STATUS_UNAVAILABLE",
                "detail": "live project status could not be read",
            }
        return result

    @server.tool()
    def terminal_chat_checkpoint_list(project_id: str | None = None, limit: int = 20) -> dict:
        """List recent durable ChatGPT orchestration checkpoints.

        Use this for history/audit. For normal new-chat recovery prefer
        terminal_chat_recover because it also returns current live project state.
        """
        return call(store.list, project_id, limit=limit)
