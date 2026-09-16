from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from terminal_mcp.config import NodeHealthConfig
from terminal_mcp.host_metrics import NodeMetrics
from terminal_mcp.lease import ResourceLockStore
from terminal_mcp.node_client import NodeClientError
from terminal_mcp.node_health import NodeHealthLoop, NodeHealthPolicy, NodeHealthService
from terminal_mcp.node_models import (
    HEALTH_AUTH_UNAUTHORIZED,
    HEALTH_DEGRADED,
    HEALTH_EXECUTION_DOWN,
    HEALTH_EXECUTION_OK,
    HEALTH_OFFLINE,
    HEALTH_TRANSPORT_ONLINE,
)
from terminal_mcp.node_registry import NodeRegistry


METRICS = NodeMetrics(*([None] * 15))


class Clock:
    def __init__(self) -> None:
        self.value = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class FakeClient:
    def __init__(self, outcomes=None) -> None:
        self.outcomes = list(outcomes or [{"execution_ok": True, "agent_process_alive": True}])
        self.probes = 0
        self.heals = 0

    def health(self):
        return {"status": "ok", "agent_generation": "g1"}

    def execution_probe(self, timeout_seconds):
        assert timeout_seconds < 5
        self.probes += 1
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def self_heal(self, action):
        assert action == "graceful_agent_restart"
        self.heals += 1
        return {"ok": True}


def setup_health(tmp_path, *, outcomes=None, config=None):
    clock = Clock()
    registry = NodeRegistry(tmp_path / "nodes.db")
    registry.register("n1", display_name="Node", hostname="n1", endpoint="http://n1")
    registry.heartbeat(
        "n1", metrics=METRICS, tmux_session_count=0, agent_counts={},
        agent_types=("shell",), agent_version="test", labels=(), now=clock(),
    )
    service = NodeHealthService(
        registry, ResourceLockStore(tmp_path / "leases.db"),
        config or NodeHealthConfig(backoff_jitter_ratio=0), now=clock, jitter=lambda _a, _b: 0,
    )
    return clock, registry, service, FakeClient(outcomes)


def test_fresh_transport_and_execution_probe_is_online(tmp_path):
    _clock, registry, service, client = setup_health(tmp_path)
    result = service.evaluate(registry.get("n1"), client)
    assert result.transport_state == HEALTH_TRANSPORT_ONLINE
    assert result.health_state == HEALTH_EXECUTION_OK
    assert result.status == "online"
    assert result.last_successful_probe_at
    assert result.consecutive_failures == 0


def test_dead_execution_child_is_never_online(tmp_path):
    _clock, registry, service, client = setup_health(
        tmp_path, outcomes=[{"execution_ok": False, "agent_process_alive": False}])
    first = service.evaluate(registry.get("n1"), client, force_probe=True)
    second = service.evaluate(registry.get("n1"), client, force_probe=True)
    assert first.health_state == HEALTH_EXECUTION_DOWN
    assert second.health_state == HEALTH_EXECUTION_DOWN
    assert second.status == "degraded"


def test_timeout_degrades_then_execution_down_and_backoff_suppresses_probe(tmp_path):
    clock, registry, service, client = setup_health(tmp_path, outcomes=[TimeoutError("probe timed out")])
    first = service.evaluate(registry.get("n1"), client)
    suppressed = service.evaluate(registry.get("n1"), client)
    assert first.health_state == HEALTH_DEGRADED
    assert suppressed.reconnect_status == "BACKOFF"
    assert client.probes == 1
    clock.advance(5)
    second = service.evaluate(registry.get("n1"), client)
    assert second.health_state == HEALTH_EXECUTION_DOWN
    assert client.probes == 2


def test_stale_heartbeat_is_offline_despite_cached_success(tmp_path):
    clock, registry, service, client = setup_health(tmp_path)
    service.evaluate(registry.get("n1"), client)
    clock.advance(181)
    stale = registry.get("n1", now=clock())
    result = service.evaluate(stale, client)
    assert result.health_state == HEALTH_OFFLINE
    assert result.status == "offline"
    assert client.probes == 1


def test_recovery_clears_failures_and_backoff(tmp_path):
    clock, registry, service, client = setup_health(
        tmp_path, outcomes=[TimeoutError("timeout"), {"execution_ok": True, "agent_process_alive": True}])
    service.evaluate(registry.get("n1"), client)
    clock.advance(5)
    recovered = service.evaluate(registry.get("n1"), client)
    assert recovered.health_state == HEALTH_EXECUTION_OK
    assert recovered.consecutive_failures == 0
    assert recovered.next_retry_at is None
    assert recovered.last_error is None


def test_restart_preserves_backoff_and_avoids_duplicate_self_heal(tmp_path):
    config = NodeHealthConfig(execution_down_after_failures=2, backoff_jitter_ratio=0)
    _clock, registry, service, client = setup_health(
        tmp_path, outcomes=[TimeoutError("timeout")], config=config)
    service.set_policy("n1", NodeHealthPolicy(True, "graceful_agent_restart"))
    service.evaluate(registry.get("n1"), client, force_probe=True)
    service.evaluate(registry.get("n1"), client, force_probe=True)
    assert client.heals == 1
    restarted = NodeHealthService(
        NodeRegistry(tmp_path / "nodes.db"), ResourceLockStore(tmp_path / "leases.db"),
        config, now=service.now, jitter=lambda _a, _b: 0,
    )
    restarted.set_policy("n1", NodeHealthPolicy(True, "graceful_agent_restart"))
    result = restarted.evaluate(restarted.registry.get("n1"), client)
    assert result.reconnect_status == "BACKOFF"
    assert client.heals == 1
    assert client.probes == 2

    # Even after the circuit opens again, the same process generation does
    # not receive another restart request.
    service.evaluate(registry.get("n1"), client, force_probe=True)
    assert client.heals == 1
    assert registry.get("n1").reconnect_status == "SELF_HEAL_AWAITING_REPLACEMENT"


def test_backoff_delay_is_exponential_and_capped(tmp_path):
    config = NodeHealthConfig(backoff_base_seconds=3, backoff_max_seconds=10,
                              backoff_jitter_ratio=0)
    _clock, _registry, service, _client = setup_health(tmp_path, config=config)
    assert [service._delay(count) for count in range(1, 7)] == [3, 6, 10, 10, 10, 10]


def test_unauthorized_is_report_only_and_sanitized(tmp_path):
    error = NodeClientError("Bearer super-secret-token rejected", http_status=401)
    _clock, registry, service, client = setup_health(tmp_path, outcomes=[error])
    service.set_policy("n1", NodeHealthPolicy(True, "graceful_agent_restart"))
    result = service.evaluate(registry.get("n1"), client)
    assert result.health_state == HEALTH_AUTH_UNAUTHORIZED
    assert result.reconnect_status == "REPORT_ONLY"
    assert "super-secret-token" not in (result.last_error or "")
    assert client.heals == 0


def test_health_loop_start_is_idempotent_and_stops():
    calls = []
    reached = threading.Event()

    def reconcile():
        calls.append(1)
        reached.set()

    loop = NodeHealthLoop(reconcile, 60)
    loop.start()
    first_thread = loop._thread
    loop.start()
    assert loop._thread is first_thread
    assert reached.wait(1)
    loop.stop()
    assert not loop._thread.is_alive()
    assert len(calls) == 1
