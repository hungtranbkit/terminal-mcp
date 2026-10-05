"""Regression: list_nodes -> create_session consistency and reconnect flapping.

Live failure (2026-10-06): ChatGPT saw m910 online+healthy in list_nodes and
then NODE_UNREACHABLE from create_session; hp-linux online in list_nodes and
then NODE_NOT_FOUND. Two causes, both covered here:

* inside one controller, a node whose heartbeats resumed after a gap stayed
  DEGRADED behind execution-probe backoff earned before the gap (up to
  backoff_max_seconds), so it alternated online/degraded/offline long after
  it was back;
* two controllers served one tunnel, so consecutive calls were answered by
  controllers with different registries. A NODE_NOT_FOUND now names the
  answering controller and the nodes it knows.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from terminal_mcp.config import NodeHealthConfig
from terminal_mcp.controller import ControllerService
from terminal_mcp.lease import ResourceLockStore
from terminal_mcp.node_client import NodeClientError
from terminal_mcp.node_health import NodeHealthService
from terminal_mcp.node_registry import NodeRegistry
from tests.test_node_health import METRICS


class RemoteAgent:
    """Stand-in for a remote terminal-node-agent: health, execution probe,
    session listing and create. `reachable=False` models a stalled host."""

    def __init__(self) -> None:
        self.reachable = True
        self.sessions: dict[str, dict[str, Any]] = {}
        self.probes = 0

    def _check(self) -> None:
        if not self.reachable:
            raise NodeClientError("URLError: timed out")

    def health(self) -> dict[str, Any]:
        self._check()
        return {"status": "ok", "agent_generation": "g1"}

    def execution_probe(self, timeout_seconds: float) -> dict[str, Any]:
        self.probes += 1
        self._check()
        return {"execution_ok": True, "agent_process_alive": True}

    def list_sessions(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        self._check()
        return {"sessions": [{"name": name, **row} for name, row in self.sessions.items()]}

    def create_session(self, name: str, agent_type: str = "shell", cwd: str | None = None, **_kw) -> dict[str, Any]:
        self._check()
        self.sessions[name] = {"agent_type": agent_type, "cwd": cwd}
        return {"session": name, "state": "READY", "agent_type": agent_type, "cwd": cwd}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _heartbeat(registry: NodeRegistry, node_id: str, *, at: datetime | None = None) -> None:
    registry.heartbeat(node_id, metrics=METRICS, tmux_session_count=0, agent_counts={},
                       agent_types=("shell",), agent_version="test", labels=(), now=at or _now())


def _setup(tmp_path, *, node_ids=("m910",)):
    registry = NodeRegistry(tmp_path / "nodes.db")
    # Large backoff so the pre-gap retry time is unambiguously in the future.
    health = NodeHealthService(
        registry, ResourceLockStore(tmp_path / "leases.db"),
        NodeHealthConfig(backoff_base_seconds=120.0, backoff_max_seconds=300.0, backoff_jitter_ratio=0),
        jitter=lambda _a, _b: 0,
    )
    controller = ControllerService(registry, local_node_id="hp-test", node_health=health)
    agents = {}
    for node_id in node_ids:
        registry.register(node_id, display_name=node_id, hostname=node_id, endpoint=f"http://{node_id}:8790")
        agents[node_id] = controller._clients[node_id] = RemoteAgent()
        _heartbeat(registry, node_id)
    return registry, controller, agents


def _listed(controller: ControllerService, node_id: str):
    return next(node for node in controller.list_nodes() if node.id == node_id)


def _age_heartbeat(registry: NodeRegistry, node_id: str, seconds: float) -> None:
    """Model a node that stopped pushing `seconds` ago (host stall)."""
    stale = (_now() - timedelta(seconds=seconds)).isoformat()
    with sqlite3.connect(registry.path) as connection:
        connection.execute("UPDATE nodes SET last_heartbeat_at = ? WHERE id = ?", (stale, node_id))


def test_reconnect_heartbeat_recovers_without_waiting_out_pre_gap_backoff(tmp_path):
    registry, controller, agents = _setup(tmp_path)
    agent = agents["m910"]

    # Host stall begins: heartbeat still fresh, probes time out -> backoff.
    agent.reachable = False
    failed = controller.node_status("m910")
    assert failed.status == "degraded"
    assert failed.next_retry_at is not None
    assert datetime.fromisoformat(failed.next_retry_at) > _now() + timedelta(seconds=60)

    # Heartbeats stop for longer than the fresh window: transport offline.
    _age_heartbeat(registry, "m910", 200)
    assert _listed(controller, "m910").status == "offline"
    assert controller.terminal_create_session("s-offline", node="m910")["error"] == "NODE_UNREACHABLE"

    # Host recovers and the agent pushes again (reconnect).
    agent.reachable = True
    _heartbeat(registry, "m910")
    assert registry.get("m910").next_retry_at is None

    listed = _listed(controller, "m910")
    assert listed.status == "online", (listed.health_state, listed.reconnect_status)
    created = controller.terminal_create_session("s-after-reconnect", node="m910")
    assert created.get("error") is None, created
    assert created["node_id"] == "m910"


def test_heartbeat_inside_fresh_window_keeps_backoff(tmp_path):
    registry, controller, agents = _setup(tmp_path)
    agents["m910"].reachable = False
    failed = controller.node_status("m910")
    probes = agents["m910"].probes

    agents["m910"].reachable = True
    _heartbeat(registry, "m910")  # no gap: the agent never stopped pushing
    assert registry.get("m910").next_retry_at == failed.next_retry_at
    for _ in range(3):
        assert _listed(controller, "m910").status == "degraded"
    assert agents["m910"].probes == probes


def test_list_nodes_online_implies_create_session_succeeds_through_flapping(tmp_path):
    registry, controller, agents = _setup(tmp_path, node_ids=("m910", "dell-linux"))
    # Scripted stall/recover cycles for m910; dell-linux stays healthy.
    script = [True, True, False, False, True, True, False, True, True, True]
    created = 0
    for step, reachable in enumerate(script):
        agent = agents["m910"]
        was_reachable = agent.reachable
        agent.reachable = reachable
        if reachable:
            if not was_reachable:
                _age_heartbeat(registry, "m910", 200)  # it went quiet while stalled
            _heartbeat(registry, "m910")
        else:
            controller.node_status("m910")  # a read during the stall records the failure
        _heartbeat(registry, "dell-linux")

        for node in controller.list_nodes():
            if node.id == controller.local_node_id:
                continue
            result = controller.terminal_create_session(f"probe-{node.id}-{step}", node=node.id)
            if node.status == "online" and agents[node.id].reachable:
                assert result.get("error") is None, (step, node.id, result)
                assert result["node_id"] == node.id
                created += 1
            else:
                # Listed online from a still-valid cached probe while the host
                # had just stalled: create fails, and that failure must be
                # what the very next list_nodes reports -- never a lingering
                # "online" that keeps sending creates into NODE_UNREACHABLE.
                assert result["error"] == "NODE_UNREACHABLE", (step, node.id, result)
                assert _listed(controller, node.id).status != "online", (step, node.id)
        # The recovered node must be online in the SAME step it reconnects.
        if reachable:
            assert _listed(controller, "m910").status == "online", step
    assert created >= len(script) + script.count(True)


def test_node_not_found_names_the_answering_controller(tmp_path):
    _registry, controller, _agents = _setup(tmp_path)
    result = controller.terminal_create_session("s-ghost", node="hp-linux")
    assert result["error"] == "NODE_NOT_FOUND"
    assert result["controller_node_id"] == "hp-test"
    assert sorted(result["known_node_ids"]) == ["hp-test", "m910"]


def test_unreachable_create_carries_controller_and_health(tmp_path):
    registry, controller, _agents = _setup(tmp_path)
    _age_heartbeat(registry, "m910", 400)
    result = controller.terminal_create_session("s-dark", node="m910")
    assert result["error"] == "NODE_UNREACHABLE"
    assert result["controller_node_id"] == "hp-test"
    assert result["health_state"]


def test_http_level_agent_error_does_not_degrade_node(tmp_path):
    _registry, controller, agents = _setup(tmp_path)
    assert _listed(controller, "m910").status == "online"

    def refuse(*_a, **_kw):
        raise NodeClientError("POST /v1/sessions -> HTTP 409: busy", http_status=409)

    agents["m910"].create_session = refuse
    assert controller.terminal_create_session("s-409", node="m910")["error"] == "NODE_UNREACHABLE"
    # The agent answered; reachability evidence is unchanged.
    assert _listed(controller, "m910").status == "online"
