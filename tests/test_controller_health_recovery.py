"""Ordinary controller reads must retry failed execution probes after backoff."""
from datetime import datetime

import pytest

from terminal_mcp.controller import ControllerService
from terminal_mcp.node_client import NodeClientError
from terminal_mcp.node_health import NodeHealthPolicy
from terminal_mcp.node_models import (
    HEALTH_AUTH_UNAUTHORIZED,
    HEALTH_DEGRADED,
    HEALTH_EXECUTION_DOWN,
    HEALTH_EXECUTION_OK,
)
from tests.test_node_health import METRICS, setup_health


@pytest.mark.parametrize("view", ["list_nodes", "node_status"])
@pytest.mark.parametrize("failure, expected", [
    (TimeoutError("reboot in progress"), HEALTH_DEGRADED),
    ({"execution_ok": False, "agent_process_alive": False}, HEALTH_EXECUTION_DOWN),
    (NodeClientError("credentials rejected", http_status=401), HEALTH_AUTH_UNAUTHORIZED),
])
def test_status_reads_recover_after_backoff_without_admitting_failed_nodes(
    tmp_path, view, failure, expected,
):
    clock, registry, service, client = setup_health(
        tmp_path, outcomes=[failure, {"execution_ok": True}])
    service.set_policy("n1", NodeHealthPolicy(True, "graceful_agent_restart"))
    controller = ControllerService(
        registry, local_node_id="n1", local_client=client, node_health=service)

    def read():
        if view == "list_nodes":
            return controller.list_nodes()[0]
        return controller.node_status("n1")

    failed = read()
    assert failed.health_state == expected
    assert failed.status == "degraded"
    assert client.probes == 1
    heals_after_failure = client.heals
    if expected == HEALTH_AUTH_UNAUTHORIZED:
        assert heals_after_failure == 0

    # A fresh transport heartbeat after reboot cannot stand in for execution
    # recovery, and frequent dashboard polling must respect persisted backoff.
    registry.heartbeat(
        "n1", metrics=METRICS, tmux_session_count=0, agent_counts={},
        agent_types=("shell",), agent_version="test", labels=(), now=clock(),
    )
    for _ in range(3):
        assert read().health_state == expected
        assert controller.node_sessions("n1")["error"] == "NODE_UNREACHABLE"
    assert client.probes == 1
    assert client.heals == heals_after_failure

    clock.value = datetime.fromisoformat(failed.next_retry_at)
    recovered = read()
    assert client.probes == 2
    assert recovered.health_state == HEALTH_EXECUTION_OK
    assert recovered.status == "online"
    assert recovered.consecutive_failures == 0
    assert recovered.next_retry_at is None
    assert recovered.last_error is None
    assert read().health_state == HEALTH_EXECUTION_OK
    assert client.probes == 2  # healthy probe interval also remains in force
