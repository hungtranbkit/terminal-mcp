"""Auto Recovery -- MCP tool surface. Exercises the real MCP call path
(server.call_tool) with a real local disposable tmux session -- the
underlying engine/lock/policy mechanics are test_recovery_engine.py's
own job; this file is the tool-wiring layer."""
from __future__ import annotations

import json
import subprocess
import time
import uuid

import pytest

from terminal_mcp.config import AppConfig, AutoRecoveryConfig, InputPolicyConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.controller import build_default_controller
from terminal_mcp.core import TerminalService
from terminal_mcp.mcp_app import build_mcp

from tests.conftest import tmux_cmd


async def _call(server, tool_name, **kwargs):
    result = await server.call_tool(tool_name, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def _tmux(*args, check=False):
    return subprocess.run([*tmux_cmd(), *args], check=check, capture_output=True, text=True, timeout=10)


def _rig_config(tmp_path, *, auto_recovery_enabled=False) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("mcprec-*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("mcprec-*",)),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),)),
        auto_recovery=AutoRecoveryConfig(enabled=auto_recovery_enabled, managed_sessions_only=False),
    )


@pytest.fixture(autouse=True)
def _isolated_session_registry(tmp_path, monkeypatch):
    """conftest redirects XDG_STATE_HOME ONCE per test SESSION, so every
    test in a run otherwise shares a single session_registry.db. That is
    real cross-test leakage here, not a theoretical one: these tests all
    drive the same session name, so test_recovery_set_policy_round_trip's
    explicit `enabled=False` override survived into
    test_recovery_reconcile_node_through_mcp and blocked it with
    RECOVERY_BLOCKED ("auto_recovery disabled for this session") -- a
    failure that only appeared when the file was run as a whole, and
    vanished when the test was run alone. Give every test its own
    registry file instead; the env var is read at construction time, so
    setting it before the rig builds TerminalService is sufficient."""
    monkeypatch.setenv("TERMINAL_MCP_SESSION_REGISTRY_DB", str(tmp_path / "session_registry.db"))


@pytest.fixture
def rig(tmp_path):
    service = TerminalService(_rig_config(tmp_path))
    controller = build_default_controller(service)
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=(), agent_version=None)
    server = build_mcp(service, controller=controller)
    yield server, service
    _tmux("kill-session", "-t", "mcprec-a", check=False)


@pytest.fixture
def rig_auto_enabled(tmp_path):
    service = TerminalService(_rig_config(tmp_path, auto_recovery_enabled=True))
    controller = build_default_controller(service)
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=(), agent_version=None)
    server = build_mcp(service, controller=controller)
    yield server, service
    _tmux("kill-session", "-t", "mcprec-a", check=False)


@pytest.mark.anyio
async def test_recovery_status_for_unknown_session(rig):
    server, _service = rig
    result = await _call(server, "terminal_recovery_status", node_id="local", session_name="no-such-session")
    assert result["error"] == "REGISTRY_RECORD_NOT_FOUND"


@pytest.mark.anyio
async def test_full_recovery_round_trip_through_real_mcp_tools(rig):
    server, service = rig
    root = str(service.config.session_lifecycle.allowed_cwd_roots[0])
    created = await _call(server, "terminal_create_session", name="mcprec-a", agent_type="shell", cwd=root)
    assert created["state"] == "READY"
    time.sleep(0.3)
    await _call(server, "terminal_list_sessions")  # reconcile -> ACTIVE

    status = await _call(server, "terminal_recovery_status", node_id="local", session_name="mcprec-a")
    assert status["status"] == "ACTIVE"

    _tmux("kill-session", "-t", "mcprec-a")
    await _call(server, "terminal_list_sessions")  # reconcile -> MISSING

    bulk = await _call(server, "terminal_recovery_list", node_id="local")
    assert any(r["session_name"] == "mcprec-a" for r in bulk["records"])

    # config.auto_recovery.enabled defaults False (this rig never opts
    # in globally) -- force=true is the real, supported manual-operator
    # override for exactly this case (terminal_recover_session's own
    # docstring), proven here through the real MCP call path.
    recovered = await _call(server, "terminal_recover_session", node_id="local", session_name="mcprec-a", force=True)
    assert "error" not in recovered, recovered
    assert recovered["recovery_state"] == "RECOVERY_DEGRADED"  # shell -> never resumable, honest

    status_after = await _call(server, "terminal_recovery_status", node_id="local", session_name="mcprec-a")
    assert status_after["status"] == "ACTIVE"
    assert status_after["recovery_generation"] == 1


@pytest.mark.anyio
async def test_session_create_warns_about_same_task_and_project_sprawl(rig):
    server, service = rig
    root = str(service.config.session_lifecycle.allowed_cwd_roots[0])
    suffix = uuid.uuid4().hex[:10]
    names = [f"mcprec-{suffix}-{letter}" for letter in "abcd"]
    try:
        for name in names[:3]:
            created = await _call(server, "terminal_create_session", name=name,
                                  agent_type="shell", working_directory=root)
            assert "error" not in created
        server.task_labels.on_task(node_id="local", session=names[0], text="same task",
                                   title="echo duplicate task")
        assert any(key[1] == names[0] for key in server.task_labels.store.task_label_index())
        listing = await _call(server, "terminal_list_sessions")
        assert len(listing["sessions"]) >= 3, listing
        assert any(row.get("cwd") == root for row in listing["sessions"]), listing

        advisory = await _call(server, "terminal_create_session", name=names[3],
                               agent_type="shell", working_directory=root,
                               initial_prompt="echo duplicate task")
        assert advisory["error"] == "SESSION_CREATE_ADVISORY"
        assert advisory["created"] is False
        assert _tmux("has-session", "-t", names[3], check=False).returncode != 0
        created = await _call(server, "terminal_create_session", name=names[3],
                              agent_type="shell", working_directory=root,
                              initial_prompt="echo duplicate task", confirm_advisories=True)
        assert "error" not in created
        assert "advisories" in created, created
        codes = {advice["code"] for advice in created["advisories"]}
        assert codes == {"SAME_TASK_SESSION_EXISTS", "PROJECT_SESSION_SPRAWL"}
        reuse = next(advice for advice in created["advisories"]
                     if advice["code"] == "SAME_TASK_SESSION_EXISTS")
        assert reuse["sessions"] == [names[0]]
        assert "reuse" in reuse["recommendation"]
    finally:
        for name in names:
            _tmux("kill-session", "-t", name, check=False)


@pytest.mark.anyio
async def test_recovery_set_policy_round_trip(rig):
    server, service = rig
    root = str(service.config.session_lifecycle.allowed_cwd_roots[0])
    await _call(server, "terminal_create_session", name="mcprec-a", agent_type="shell", cwd=root)
    time.sleep(0.3)
    await _call(server, "terminal_list_sessions")

    result = await _call(server, "terminal_recovery_set_policy", node_id="local", session_name="mcprec-a",
                         enabled=False)
    assert result["auto_recovery_enabled"] is False
    status = await _call(server, "terminal_recovery_status", node_id="local", session_name="mcprec-a")
    assert status["auto_recovery_enabled"] is False


@pytest.mark.anyio
async def test_checkpoint_session_through_mcp(rig):
    server, service = rig
    root = str(service.config.session_lifecycle.allowed_cwd_roots[0])
    await _call(server, "terminal_create_session", name="mcprec-a", agent_type="shell", cwd=root)
    time.sleep(0.3)
    await _call(server, "terminal_list_sessions")

    result = await _call(server, "terminal_checkpoint_session", node_id="local", session_name="mcprec-a",
                         detail="manual smoke checkpoint")
    assert result["last_checkpoint_detail"] == "manual smoke checkpoint"


@pytest.mark.anyio
async def test_recovery_reconcile_node_through_mcp(rig_auto_enabled):
    server, service = rig_auto_enabled
    root = str(service.config.session_lifecycle.allowed_cwd_roots[0])
    await _call(server, "terminal_create_session", name="mcprec-a", agent_type="shell", cwd=root)
    time.sleep(0.3)
    await _call(server, "terminal_list_sessions")
    _tmux("kill-session", "-t", "mcprec-a")
    await _call(server, "terminal_list_sessions")

    result = await _call(server, "terminal_recovery_reconcile_node", node_id="local")
    assert any(r.get("session") == "mcprec-a" and "error" not in r for r in result["results"])


@pytest.mark.anyio
async def test_recovery_loop_status_and_run_once_through_mcp(rig):
    server, _service = rig
    status = await _call(server, "terminal_recovery_loop_status")
    assert status["running"] is False  # build_mcp constructs it but never starts it
    result = await _call(server, "terminal_recovery_loop_run_once")
    assert "results" in result
