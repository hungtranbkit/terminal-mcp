"""Rename Session feature -- task: "bổ sung tính năng Rename Session trực
tiếp trên Dashboard". Real disposable tmux sessions throughout (never
window/window2 -- the standing safety constraint for every queue/
coordinator/integration feature this project has built applies here too,
even though rename itself is a lighter-weight operation than those).

Covers: the real tmux rename itself, that bindings/grants/session_registry/
queue tasks/integration handoffs/supervisor watches all keep working under
the new name (never lost, never duplicated), collision/validation refusal,
old-name redirect via the controller's alias map, and that everything
persisted (not the alias, which is documented as non-persistent) survives
a simulated controller restart (fresh store instances over the same db
files)."""
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


def _service(tmp_path, *, bindings=None, grants=None, session_registry=None) -> TerminalService:
    config = AppConfig(
        PermissionsConfig(True, True), ("rn-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("rn-*",), max_text_length=2000),
        session_lifecycle=SessionLifecycleConfig(enabled=True, protected_sessions=("terminal-mcp",)),
    )
    return TerminalService(
        config, bindings=bindings or BindingStore(tmp_path / "bindings.db"),
        audit=AuditStore(tmp_path / "audit.db"),
        grants=grants or SessionGrantStore(tmp_path / "grants.db"),
        session_registry=session_registry or SessionRegistryStore(tmp_path / "registry.db"),
    )


async def _call(server, tool_name, **kwargs):
    result = await server.call_tool(tool_name, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.fixture
def rig(tmp_path, tmux_session_factory):
    service = _service(tmp_path)
    controller = build_default_controller(service)
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=(), agent_version=None)
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    integration = IntegrationService(IntegrationStore(tmp_path / "integration.db"))
    supervisor = SupervisorService(terminal=service, store=SupervisorStore(tmp_path / "supervisor.db"))
    server = build_mcp(service, controller=controller, queue=queue, integration=integration, supervisor=supervisor)
    return {"server": server, "service": service, "controller": controller, "queue": queue,
           "integration": integration, "supervisor": supervisor, "tmp_path": tmp_path,
           "make_worker": lambda name: tmux_session_factory(name, "bash -lc 'sleep 300'")}


def _kill_tmux(name: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", name], check=False, capture_output=True)


@pytest.mark.anyio
async def test_real_tmux_session_is_actually_renamed(rig):
    server, service, make_worker = rig["server"], rig["service"], rig["make_worker"]
    old, new = _unique("rn-old"), _unique("rn-new")
    make_worker(old)
    try:
        pid_before = service.tmux.get_session(old).pane_pid

        result = await _call(server, "terminal_rename_session", name=old, new_name=new)
        assert "error" not in result, result
        assert result["old_name"] == old
        assert result["new_name"] == new

        assert service.tmux.get_session(old) is None  # real tmux session actually gone under the old name

        gone = await _call(server, "terminal_status", session=old)
        # Redirected via the controller's alias map -- NOT a flat
        # SESSION_NOT_FOUND for a bare, unqualified old name.
        assert "error" not in gone
        assert gone["redirected_from"] == old
        assert gone["exists"] is True

        after_info = service.tmux.get_session(new)
        assert after_info is not None
        assert after_info.pane_pid == pid_before  # same real process, never restarted
    finally:
        _kill_tmux(new)


@pytest.mark.anyio
async def test_binding_and_grant_follow_the_rename(rig):
    server, service, make_worker = rig["server"], rig["service"], rig["make_worker"]
    old, new = _unique("rn-old"), _unique("rn-new")
    make_worker(old)
    try:
        service.bindings.put("my-binding", old, read_enabled=True, input_enabled=True)
        service.grants.set_read(old, True, granted_by="test")
        service.grants.set_input(old, True, granted_by="test", pinned_session_id="$99", pinned_pane_id="%99",
                                 pinned_created_epoch=123)

        result = await _call(server, "terminal_rename_session", name=old, new_name=new)
        assert "error" not in result, result
        assert result["bindings_updated"] == 1
        assert result["grant_renamed"] is True

        binding = service.bindings.get("my-binding")
        assert binding.session == new

        grant = service.grants.get(new)
        assert grant is not None
        assert grant.read_enabled and grant.input_enabled
        assert grant.pinned_session_id == "$99"  # identity pin carried over untouched
        assert service.grants.get(old) is None
    finally:
        _kill_tmux(new)


@pytest.mark.anyio
async def test_session_registry_history_is_preserved_not_reset(rig):
    server, service, make_worker = rig["server"], rig["service"], rig["make_worker"]
    old, new = _unique("rn-old"), _unique("rn-new")
    make_worker(old)
    try:
        record = service.session_registry.upsert_seen(service.REGISTRY_LOCAL_NODE_ID, old, backend_type="tmux")
        original_created_at = record.created_at

        result = await _call(server, "terminal_rename_session", name=old, new_name=new)
        assert "error" not in result, result

        renamed = service.session_registry.get(service.REGISTRY_LOCAL_NODE_ID, new)
        assert renamed is not None
        assert renamed.created_at == original_created_at  # history preserved, not reset to "now"
        assert service.session_registry.get(service.REGISTRY_LOCAL_NODE_ID, old) is None
    finally:
        _kill_tmux(new)


@pytest.mark.anyio
async def test_queue_tasks_follow_the_rename_including_a_running_one(rig):
    server, queue, make_worker = rig["server"], rig["queue"], rig["make_worker"]
    old, new = _unique("rn-old"), _unique("rn-new")
    make_worker(old)
    try:
        queued = await _call(server, "terminal_enqueue_task", session=old, prompt="a queued task")
        running = await _call(server, "terminal_enqueue_task", session=old, prompt="a running task")
        queue.store.transition_task(running["task_id"], "PRECHECK", event_type="TEST")
        queue.store.transition_task(running["task_id"], "READY", event_type="TEST")
        queue.store.transition_task(running["task_id"], "DISPATCHING", event_type="TEST")
        queue.store.transition_task(running["task_id"], "RUNNING", event_type="TEST")

        result = await _call(server, "terminal_rename_session", name=old, new_name=new)
        assert "error" not in result, result
        assert result["queue_tasks_updated"] == 2

        status_new = await _call(server, "terminal_queue_status", session=new)
        ids = {row["id"] for row in status_new["tasks"]}
        assert queued["task_id"] in ids
        assert running["task_id"] in ids
        running_row = next(row for row in status_new["tasks"] if row["id"] == running["task_id"])
        assert running_row["status"] == "RUNNING"  # rename never disturbs a live task's own state

        status_old = await _call(server, "terminal_queue_status", session=old)
        assert status_old["queued_count"] == 0
        assert status_old.get("tasks", []) == []
    finally:
        _kill_tmux(new)


@pytest.mark.anyio
async def test_integration_handoff_origin_session_follows_the_rename(rig):
    server, integration, make_worker = rig["server"], rig["integration"], rig["make_worker"]
    old, new = _unique("rn-old"), _unique("rn-new")
    make_worker(old)
    try:
        handoff = integration.store.publish_handoff(
            project="proj-rn", task_id="t1", origin_session=old, branch="feature/x",
            commit_sha="a" * 40, base_sha="b" * 40,
        )

        result = await _call(server, "terminal_rename_session", name=old, new_name=new)
        assert "error" not in result, result
        assert result["integration_handoffs_updated"] == 1

        refreshed = integration.store.get_handoff(handoff.id)
        assert refreshed.origin_session == new
    finally:
        _kill_tmux(new)


@pytest.mark.anyio
async def test_supervisor_watch_is_rekeyed_not_orphaned(rig):
    server, supervisor, make_worker = rig["server"], rig["supervisor"], rig["make_worker"]
    old, new = _unique("rn-old"), _unique("rn-new")
    make_worker(old)
    try:
        supervisor.watch(session=old)
        assert supervisor.store.get_watch(f"session:{old}") is not None

        result = await _call(server, "terminal_rename_session", name=old, new_name=new)
        assert "error" not in result, result
        assert result["watches_renamed"] == 1

        assert supervisor.store.get_watch(f"session:{old}") is None
        renamed_watch = supervisor.store.get_watch(f"session:{new}")
        assert renamed_watch is not None
        assert renamed_watch["target"] == new
    finally:
        _kill_tmux(new)


@pytest.mark.anyio
async def test_rename_refuses_a_name_collision(rig):
    server, make_worker = rig["server"], rig["make_worker"]
    a, b = _unique("rn-a"), _unique("rn-b")
    make_worker(a)
    make_worker(b)
    result = await _call(server, "terminal_rename_session", name=a, new_name=b)
    assert result["error"] == "NAME_COLLISION"
    # Both sessions completely untouched.
    status_a = await _call(server, "terminal_status", session=a)
    status_b = await _call(server, "terminal_status", session=b)
    assert "error" not in status_a and "error" not in status_b


@pytest.mark.anyio
async def test_rename_refuses_invalid_new_name(rig):
    server, make_worker = rig["server"], rig["make_worker"]
    old = _unique("rn-old")
    make_worker(old)
    result = await _call(server, "terminal_rename_session", name=old, new_name="../etc/passwd")
    assert result["error"] == "INVALID_NEW_SESSION_NAME"
    still_there = await _call(server, "terminal_status", session=old)
    assert "error" not in still_there


@pytest.mark.anyio
async def test_rename_refuses_same_name(rig):
    server, make_worker = rig["server"], rig["make_worker"]
    old = _unique("rn-old")
    make_worker(old)
    result = await _call(server, "terminal_rename_session", name=old, new_name=old)
    assert result["error"] == "SAME_NAME"


@pytest.mark.anyio
async def test_rename_refuses_a_missing_session(rig):
    server = rig["server"]
    result = await _call(server, "terminal_rename_session", name=_unique("rn-ghost"), new_name=_unique("rn-new"))
    assert result["error"] == "SESSION_NOT_FOUND"


@pytest.mark.anyio
async def test_restart_recovers_the_rename_everywhere_no_duplicate(rig):
    """item: 'restart controller after rename' -- fresh store instances
    over the SAME db files ('the controller restarted') see the renamed
    session's data already correctly persisted; nothing duplicated,
    nothing lost. The old-name alias redirect is explicitly NOT expected
    to survive this (documented, disclosed limitation -- see
    ControllerService._rename_aliases's own docstring) -- only real,
    durable data is asserted here."""
    server = rig["server"]
    tmp_path = rig["tmp_path"]
    make_worker = rig["make_worker"]
    old, new = _unique("rn-old"), _unique("rn-new")
    make_worker(old)
    try:
        await _call(server, "terminal_enqueue_task", session=old, prompt="a task")
        result = await _call(server, "terminal_rename_session", name=old, new_name=new)
        assert "error" not in result, result

        queue2 = QueueStore(tmp_path / "queue.db")
        tasks_new = queue2.lane_status(new)["tasks"]
        tasks_old = queue2.lane_status(old)["tasks"]
        assert len(tasks_new) == 1
        assert len(tasks_old) == 0

        registry2 = SessionRegistryStore(tmp_path / "registry.db")
        assert registry2.get("local", new) is not None
        assert registry2.get("local", old) is None
    finally:
        _kill_tmux(new)


def test_dashboard_rename_route_propagates_to_queue_and_integration_too(tmp_path, tmux_session_factory):
    """The dashboard's own /dashboard/api/session/rename route (separate
    code path from the MCP tool, dashboard.py doesn't share build_mcp's
    in-process tool registry) must give the SAME real propagation the
    MCP tool does -- real HTTP request, real tmux rename, real queue/
    integration/supervisor state all checked afterward."""
    from starlette.testclient import TestClient
    from terminal_mcp.dashboard import register_dashboard
    from terminal_mcp.mcp_app import build_mcp as _build_mcp

    service = _service(tmp_path)
    controller = build_default_controller(service)
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=(), agent_version=None)
    queue = QueueService(QueueStore(tmp_path / "queue2.db"))
    integration = IntegrationService(IntegrationStore(tmp_path / "integration2.db"))
    supervisor = SupervisorService(terminal=service, store=SupervisorStore(tmp_path / "supervisor2.db"))
    server = _build_mcp(service, controller=controller, queue=queue, integration=integration, supervisor=supervisor)
    register_dashboard(server, service, supervisor=supervisor, controller=controller,
                       queue=queue, integration=integration)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})

    old, new = _unique("rn-old"), _unique("rn-new")
    tmux_session_factory(old, "bash -lc 'sleep 300'")
    try:
        queue.store.append_tasks(old, [{"prompt": "a task"}])
        handoff = integration.store.publish_handoff(
            project="proj-rn2", task_id="t1", origin_session=old, branch="feature/x",
            commit_sha="a" * 40, base_sha="b" * 40,
        )
        supervisor.watch(session=old)

        response = client.post("/dashboard/api/session/rename", json={"name": old, "new_name": new})
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["queue_tasks_updated"] == 1
        assert body["integration_handoffs_updated"] == 1
        assert body["watches_renamed"] == 1

        assert queue.store.lane_status(new)["tasks"]
        assert integration.store.get_handoff(handoff.id).origin_session == new
        assert supervisor.store.get_watch(f"session:{new}") is not None
    finally:
        _kill_tmux(new)
