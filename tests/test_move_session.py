"""controller.py's terminal_move_session (task item 7): explicit
create-on-target -> verify READY -> stop-source workflow.

The SOURCE node is a real TerminalService against the real tmux server on
this host (so "stop source" is a genuine tmux kill, verified against real
tmux state, not a mock). The TARGET node is a FakeNodeClient, not a
second real TerminalService: two TerminalService instances on this one
host would both talk to the SAME physical tmux server (TmuxClient has no
per-instance socket override), so they cannot actually simulate two
independent nodes' tmux state -- using one for "the other node" would be
a misleading test, not a faithful one. A FakeNodeClient standing in for
the target still lets every real decision this method makes (ordering,
error handling, when the source actually gets killed) be verified
against real tmux evidence on the source side, which is exactly where
the one truly dangerous action (killing a real session) happens.
"""
from __future__ import annotations

import subprocess
from typing import Any

import pytest

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.controller import ControllerService
from terminal_mcp.core import TerminalService
from terminal_mcp.host_metrics import NodeMetrics
from terminal_mcp.node_client import LocalNodeClient, NodeClientError
from terminal_mcp.node_registry import NodeRegistry


def _tmux_has_session(name: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", name], capture_output=True).returncode == 0


def _config(tmp_path) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True),
        allowed_session_patterns=("move-*",),
        max_capture_lines=200,
        default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("move-*",)),
        session_lifecycle=SessionLifecycleConfig(
            enabled=True, allowed_cwd_roots=(str(tmp_path),), protected_sessions=(),
            launch_commands=(("claude", "true"),),
        ),
    )


def _bare_metrics() -> NodeMetrics:
    return NodeMetrics(cpu_percent=5.0, load1=0.1, load5=0.1, load15=0.1, cpu_count=4,
                       ram_total_bytes=8_000_000_000, ram_used_bytes=1_000_000_000, ram_percent=12.5,
                       swap_total_bytes=0, swap_used_bytes=0, swap_percent=0.0,
                       disk_total_bytes=100_000_000_000, disk_used_bytes=1_000_000_000,
                       disk_free_bytes=99_000_000_000, disk_percent=1.0)


class FakeTargetClient:
    """Stands in for a real remote node's NodeClient -- see module
    docstring for why this isn't a second real TerminalService."""

    def __init__(self, create_response: dict[str, Any] | None = None, *, raise_on_create: bool = False) -> None:
        self.create_response = create_response if create_response is not None else {"state": "READY", "session": "x"}
        self.raise_on_create = raise_on_create
        self.create_calls: list[tuple] = []
        self.existing_sessions: set[str] = set()

    def create_session(self, name, agent_type="shell", cwd=None, **kwargs):
        self.create_calls.append((name, agent_type, cwd))
        if name in self.existing_sessions:
            return {"error": "SESSION_ALREADY_EXISTS", "session": name}
        if self.raise_on_create:
            raise NodeClientError("simulated target unreachable")
        response = dict(self.create_response)
        response.setdefault("session", name)
        return response

    def kill_session(self, name, confirm_name, **kwargs):
        return {"error": "NOT_USED_IN_THESE_TESTS"}

    def list_sessions(self):
        return {"sessions": []}


@pytest.fixture
def controller_with_fake_target(tmp_path):
    service = TerminalService(_config(tmp_path))
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(service), local_workspace_root=str(tmp_path))
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=("shell", "claude"), agent_version=None)
    controller.registry.register("node-b", display_name="Node B", hostname="node-b-host", endpoint="http://node-b:8790")
    controller.registry.heartbeat("node-b", metrics=_bare_metrics(), tmux_session_count=0, agent_counts={},
                                  agent_types=("shell", "claude"), agent_version=None, labels=())
    yield controller, service
    for name in ("move-src",):
        subprocess.run(["tmux", "kill-session", "-t", name], check=False, capture_output=True)


def test_successful_move_creates_on_target_then_kills_real_source(controller_with_fake_target, tmp_path):
    controller, service = controller_with_fake_target
    fake_target = FakeTargetClient(create_response={"state": "READY"})
    controller._clients["node-b"] = fake_target

    controller.terminal_create_session("move-src", "shell", str(tmp_path), node="local")
    assert _tmux_has_session("move-src")

    result = controller.terminal_move_session("move-src", "node-b", agent_type="shell")
    assert result.get("error") is None
    assert result["moved_from"] == "local"
    assert result["moved_to"] == "node-b"
    assert result["node_id"] == "node-b"
    assert fake_target.create_calls == [("move-src", "shell", None)]

    # Real evidence: the source tmux session is actually gone now, and
    # only NOW -- create was called before kill.
    assert not _tmux_has_session("move-src")

    # Routing cache now points at node-b (status() itself isn't called
    # here -- it would hit our fake client, which deliberately implements
    # only what this test needs, not the full NodeClient surface).
    resolution = controller.resolve_session("move-src")
    assert resolution["node_id"] == "node-b"


def test_failed_create_on_target_never_touches_real_source(controller_with_fake_target, tmp_path):
    # THE key safety property: verified against a REAL tmux session, not
    # just the returned dict -- a target creation failure must leave the
    # source completely alive.
    controller, service = controller_with_fake_target
    fake_target = FakeTargetClient(create_response={"error": "LAUNCHER_NOT_CONFIGURED"})
    controller._clients["node-b"] = fake_target

    controller.terminal_create_session("move-src", "shell", str(tmp_path), node="local")
    assert _tmux_has_session("move-src")

    result = controller.terminal_move_session("move-src", "node-b", agent_type="codex")
    assert result["error"] == "LAUNCHER_NOT_CONFIGURED"
    assert result["phase"] == "create_on_target"

    # Real tmux evidence: source untouched.
    assert _tmux_has_session("move-src")
    resolution = controller.resolve_session("move-src")
    assert resolution["node_id"] == "local"
    assert service.terminal_status("move-src")["exists"] is True


def test_target_created_but_not_ready_never_stops_real_source(controller_with_fake_target, tmp_path):
    # CREATED (still starting) or FAILED -- neither counts as a confirmed
    # move; the real source must survive either just as strictly as an
    # outright create error.
    controller, service = controller_with_fake_target
    fake_target = FakeTargetClient(create_response={"state": "CREATED"})
    controller._clients["node-b"] = fake_target

    controller.terminal_create_session("move-src", "shell", str(tmp_path), node="local")
    result = controller.terminal_move_session("move-src", "node-b", agent_type="shell")
    assert result["error"] == "MOVE_TARGET_NOT_READY"
    assert result["target_state"] == "CREATED"

    assert _tmux_has_session("move-src")
    assert service.terminal_status("move-src")["exists"] is True


def test_target_unreachable_during_create_never_touches_real_source(controller_with_fake_target, tmp_path):
    controller, service = controller_with_fake_target
    fake_target = FakeTargetClient(raise_on_create=True)
    controller._clients["node-b"] = fake_target

    controller.terminal_create_session("move-src", "shell", str(tmp_path), node="local")
    result = controller.terminal_move_session("move-src", "node-b", agent_type="shell")
    assert result["error"] == "NODE_UNREACHABLE"
    assert result["phase"] == "create_on_target"

    assert _tmux_has_session("move-src")


def test_move_to_same_node_rejected_source_untouched(controller_with_fake_target, tmp_path):
    controller, service = controller_with_fake_target
    controller.terminal_create_session("move-src", "shell", str(tmp_path), node="local")
    result = controller.terminal_move_session("move-src", "local", agent_type="shell")
    assert result["error"] == "ALREADY_ON_THAT_NODE"
    assert _tmux_has_session("move-src")


def test_move_to_unknown_node(controller_with_fake_target, tmp_path):
    controller, service = controller_with_fake_target
    controller.terminal_create_session("move-src", "shell", str(tmp_path), node="local")
    result = controller.terminal_move_session("move-src", "ghost-node", agent_type="shell")
    assert result["error"] == "NODE_NOT_FOUND"
    assert _tmux_has_session("move-src")


def test_move_to_draining_target_rejected_source_untouched(controller_with_fake_target, tmp_path):
    controller, service = controller_with_fake_target
    controller._clients["node-b"] = FakeTargetClient()
    controller.registry.set_draining("node-b", True)
    controller.terminal_create_session("move-src", "shell", str(tmp_path), node="local")
    result = controller.terminal_move_session("move-src", "node-b", agent_type="shell")
    assert result["error"] == "NODE_DRAINING"
    assert _tmux_has_session("move-src")


def test_move_nonexistent_session(controller_with_fake_target, tmp_path):
    controller, _service = controller_with_fake_target
    controller._clients["node-b"] = FakeTargetClient()
    result = controller.terminal_move_session("move-does-not-exist", "node-b", agent_type="shell")
    assert result["error"] == "SESSION_NOT_FOUND"


def test_move_when_target_already_has_that_name_leaves_source_untouched(controller_with_fake_target, tmp_path):
    controller, service = controller_with_fake_target
    fake_target = FakeTargetClient()
    fake_target.existing_sessions.add("move-src")
    controller._clients["node-b"] = fake_target

    controller.terminal_create_session("move-src", "shell", str(tmp_path), node="local")
    result = controller.terminal_move_session("move-src", "node-b", agent_type="shell")
    assert result["error"] == "SESSION_ALREADY_EXISTS"
    assert result["phase"] == "create_on_target"
    assert _tmux_has_session("move-src")
