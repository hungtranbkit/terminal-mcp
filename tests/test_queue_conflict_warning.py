"""P0 persist-before-dispatch, item 10: a raw terminal_send_text call
made directly (bypassing the durable queue) while a session has an
active queue task must never be silent -- the response carries a
queue_conflict_warning and the event is recorded to that task's own
audit trail. The send itself still proceeds (backward compatibility,
item 12) -- this is a WARNING, never a block.

Real tmux session, real MCP call path."""
from __future__ import annotations

import json
import time

import pytest

from terminal_mcp.audit import AuditStore
from terminal_mcp.bindings import BindingStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import DISPATCHING, RUNNING, QueueStore


def _service(tmp_path) -> TerminalService:
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("test-*",), max_text_length=2000),
    )
    return TerminalService(config, bindings=BindingStore(tmp_path / "bindings.db"),
                          audit=AuditStore(tmp_path / "audit.db"))


async def _call(server, name, **kwargs):
    result = await server.call_tool(name, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_raw_send_while_a_queue_task_is_active_carries_a_warning_and_is_audited(
    tmux_session_factory, tmp_path,
):
    session = tmux_session_factory("test-conflict-warn", "bash -lc 'read x; echo GOT:$x; sleep 10'")
    time.sleep(0.2)
    service = _service(tmp_path)
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    server = build_mcp(service, queue=queue)

    (task_id,) = queue.store.set_tasks(session, [{"prompt": "a real queued task"}])
    queue.store.transition_task(task_id, "PRECHECK", event_type="TEST")
    queue.store.transition_task(task_id, "READY", event_type="TEST")
    queue.store.transition_task(task_id, DISPATCHING, event_type="TEST")
    queue.store.transition_task(task_id, RUNNING, event_type="TEST")

    result = await _call(server, "terminal_send_text", session=session, text="y", press_enter=True)
    assert result["sent"] is True  # the send itself still proceeded, unblocked
    assert "queue_conflict_warning" in result
    assert task_id in result["queue_conflict_warning"]

    events = queue.store.list_events(session, limit=20)
    assert any(e["event_type"] == "RAW_SEND_DURING_ACTIVE_QUEUE_TASK" for e in events)


@pytest.mark.anyio
async def test_raw_send_with_no_active_queue_task_carries_no_warning(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-conflict-clean", "bash -lc 'read x; echo GOT:$x; sleep 10'")
    time.sleep(0.2)
    service = _service(tmp_path)
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    server = build_mcp(service, queue=queue)

    result = await _call(server, "terminal_send_text", session=session, text="y", press_enter=True)
    assert result["sent"] is True
    assert "queue_conflict_warning" not in result
