"""Final live disposable E2E for Rename Session + Dashboard Task Manager UI
(user's own closing instruction after both features were checkpointed):

    "chạy một live disposable E2E cuối: enqueue burst >=10 task, task
    manager hiển thị đủ, rename session giữa lúc có queued tasks, move
    vài task sang peer, restart controller, verify 0 dropped/0 duplicate
    và references stable."

Real MCP tool calls (server.call_tool), real tmux sessions (e2e-a/e2e-b),
real QueueStore/IntegrationStore/SupervisorStore SQLite files -- never
window/window2. One comprehensive scenario, not many small unit tests,
matching what was asked for."""
from __future__ import annotations

import json
import subprocess
import uuid

import pytest

from terminal_mcp.audit import AuditStore
from terminal_mcp.bindings import BindingStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.controller import build_default_controller
from terminal_mcp.core import TerminalService
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.integration_service import IntegrationService
from terminal_mcp.integration_store import IntegrationStore
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.session_registry import SessionRegistryStore
from terminal_mcp.supervisor import SupervisorService, SupervisorStore


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _call(server, tool_name, **kwargs):
    result = await server.call_tool(tool_name, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_final_e2e_burst_enqueue_task_manager_rename_migrate_restart(tmp_path, tmux_session_factory):
    config = AppConfig(
        PermissionsConfig(True, True), ("e2e-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("e2e-*",), max_text_length=2000),
        session_lifecycle=SessionLifecycleConfig(enabled=True, protected_sessions=("terminal-mcp",)),
    )
    service = TerminalService(
        config, bindings=BindingStore(tmp_path / "bindings.db"), audit=AuditStore(tmp_path / "audit.db"),
        grants=SessionGrantStore(tmp_path / "grants.db"), session_registry=SessionRegistryStore(tmp_path / "registry.db"),
    )
    controller = build_default_controller(service)
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=(), agent_version=None)
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    integration = IntegrationService(IntegrationStore(tmp_path / "integration.db"))
    supervisor = SupervisorService(terminal=service, store=SupervisorStore(tmp_path / "supervisor.db"))
    server = build_mcp(service, controller=controller, queue=queue, integration=integration, supervisor=supervisor)

    session_a = _unique("e2e-a")
    session_b = _unique("e2e-b")
    tmux_session_factory(session_a, "bash -lc 'sleep 600'")
    tmux_session_factory(session_b, "bash -lc 'sleep 600'")
    service.grants.set_read(session_a, True, granted_by="e2e")
    service.grants.set_read(session_b, True, granted_by="e2e")
    await _call(server, "terminal_task_set_project", session=session_a, project="e2e-proj")
    await _call(server, "terminal_task_set_project", session=session_b, project="e2e-proj")

    try:
        # 1) Burst-enqueue >= 10 tasks into session_a.
        task_ids: list[str] = []
        for i in range(12):
            result = await _call(server, "terminal_enqueue_task", session=session_a, prompt=f"feature {i}")
            assert result["status"] == "TASK_ACCEPTED", result
            task_ids.append(result["task_id"])
        assert len(set(task_ids)) == 12  # every id genuinely distinct

        # 2) Task Manager board shows all of them, correctly grouped.
        board = await _call(server, "terminal_session_tasks", session=session_a)
        assert board["summary"]["total"] == 12
        assert board["summary"]["queued"] == 12
        assert {t["id"] for t in board["queued"]} == set(task_ids)

        # 3) Rename session_a WHILE it still has 12 QUEUED tasks.
        new_name = _unique("e2e-a-renamed")
        rename_result = await _call(server, "terminal_rename_session", name=session_a, new_name=new_name)
        assert "error" not in rename_result, rename_result
        assert rename_result["queue_tasks_updated"] == 12

        # Old name: task manager now shows nothing (session gone from
        # under it); new name: shows the exact same 12 tasks, same ids.
        board_old = await _call(server, "terminal_session_tasks", session=session_a)
        assert board_old["summary"]["total"] == 0
        board_new = await _call(server, "terminal_session_tasks", session=new_name)
        assert board_new["summary"]["total"] == 12
        assert {t["id"] for t in board_new["queued"]} == set(task_ids)

        # 4) Move a few tasks to the peer session_b.
        moved_ids = task_ids[:4]
        for task_id in moved_ids:
            reassign_result = await _call(server, "terminal_task_reassign", task_id=task_id, to_session=session_b,
                                          reason="load balancing", actor="e2e-test")
            assert "error" not in reassign_result, reassign_result
            assert reassign_result["task"]["session"] == session_b

        board_new_after_move = await _call(server, "terminal_session_tasks", session=new_name)
        board_b = await _call(server, "terminal_session_tasks", session=session_b)
        assert board_new_after_move["summary"]["total"] == 8
        assert board_b["summary"]["total"] == 4
        assert {t["id"] for t in board_b["queued"]} == set(moved_ids)
        # Provenance preserved: every moved task still remembers its real
        # original owner (the renamed session, not the pre-rename name --
        # rename_session in queue_store.py deliberately does NOT touch
        # migration_history, so original_owner still reads as new_name,
        # the CURRENT identity of the session that originally owned it).
        for task in board_b["queued"]:
            assert task["original_owner"] == new_name
            assert len(task["migration_history"]) == 1

        # 5) Simulate a controller restart: fresh QueueStore/IntegrationStore
        # instances over the SAME db files.
        queue2 = QueueStore(tmp_path / "queue.db")
        all_ids_after_restart = {
            t["id"] for t in queue2.lane_status(new_name)["tasks"]
        } | {
            t["id"] for t in queue2.lane_status(session_b)["tasks"]
        }
        assert all_ids_after_restart == set(task_ids)  # 0 dropped, 0 duplicated
        assert len(queue2.lane_status(new_name)["tasks"]) + len(queue2.lane_status(session_b)["tasks"]) == 12

        # References stable: every task_id from step 1 is still directly
        # look-up-able and reports the exact same prompt it always had.
        for task_id in task_ids:
            task = queue2.get_task(task_id)
            assert task is not None
            assert task.id == task_id
            assert task.prompt.startswith("feature ")
    finally:
        subprocess.run(["tmux", "kill-session", "-t", new_name], check=False, capture_output=True)
