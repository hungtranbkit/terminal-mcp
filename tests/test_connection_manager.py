from __future__ import annotations

from dataclasses import replace

from terminal_mcp.connection_manager import ConnectionManager
from terminal_mcp.node_models import (
    CONNECTION_CONNECTED, CONNECTION_DEGRADED, CONNECTION_OFFLINE,
    CONNECTION_RECOVERING, HEALTH_AUTH_UNAUTHORIZED, HEALTH_EXECUTION_DOWN,
    HEALTH_EXECUTION_OK, HEALTH_OFFLINE, HEALTH_TRANSPORT_ONLINE, Node,
)


def _node(**overrides):
    base = dict(
        id="n1", display_name="Node 1", hostname="n1", endpoint="http://n1:8790",
        status="online", draining=False, last_heartbeat_at="2026-09-17T00:00:00+00:00",
        latency_ms=12.5, cpu_percent=None, cpu_percent_smoothed=None, load1=None, load5=None,
        load15=None, cpu_count=None, ram_total_bytes=None, ram_used_bytes=None, ram_percent=None,
        ram_percent_smoothed=None, swap_total_bytes=None, swap_used_bytes=None, swap_percent=None,
        swap_percent_smoothed=None, disk_total_bytes=None, disk_used_bytes=None, disk_free_bytes=None,
        disk_percent=None, tmux_session_count=0, transport_status="online",
        transport_state=HEALTH_TRANSPORT_ONLINE, health_state=HEALTH_EXECUTION_OK,
        execution_state=HEALTH_EXECUTION_OK, reconnect_status="IDLE",
    )
    base.update(overrides)
    return Node(**base)


def test_connection_projection_has_four_stable_operator_states():
    manager = ConnectionManager()
    connected = _node()
    recovering = replace(connected, health_state=HEALTH_EXECUTION_DOWN,
                         execution_state=HEALTH_EXECUTION_DOWN, reconnect_status="BACKOFF",
                         consecutive_failures=2)
    degraded = replace(connected, health_state=HEALTH_AUTH_UNAUTHORIZED,
                       execution_state=HEALTH_AUTH_UNAUTHORIZED, reconnect_status="REPORT_ONLY")
    offline = replace(connected, status="offline", transport_status="offline",
                      transport_state=HEALTH_OFFLINE, health_state=HEALTH_OFFLINE)
    assert manager.node_view(connected)["connection_state"] == CONNECTION_CONNECTED
    assert manager.node_view(recovering)["connection_state"] == CONNECTION_RECOVERING
    assert manager.node_view(degraded)["connection_state"] == CONNECTION_DEGRADED
    assert manager.node_view(offline)["connection_state"] == CONNECTION_OFFLINE


def test_fleet_summary_counts_connection_states_and_preserves_diagnostics():
    manager = ConnectionManager()
    connected = _node()
    recovering = replace(connected, id="n2", health_state=HEALTH_EXECUTION_DOWN,
                         execution_state=HEALTH_EXECUTION_DOWN, reconnect_status="BACKOFF",
                         next_retry_at="2026-09-17T00:01:00+00:00", last_error="timeout")
    result = manager.fleet_view([connected, recovering])
    assert result["summary"] == {
        "total": 2, "connected": 1, "degraded": 0, "offline": 0, "recovering": 1,
        "counts": {"CONNECTED": 1, "DEGRADED": 0, "OFFLINE": 0, "RECOVERING": 1},
    }
    row = next(item for item in result["nodes"] if item["node_id"] == "n2")
    assert row["retry_at"] == "2026-09-17T00:01:00+00:00"
    assert row["last_error"] == "timeout"
