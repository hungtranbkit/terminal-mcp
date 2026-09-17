"""Commander-like connection projection over execution-aware node health.

This module does not probe the network itself. NodeHealthService remains the
one owner of probing/backoff/self-heal. ConnectionManager only turns that
evidence into a small, stable operator contract so MCP, dashboard and future
clients do not each invent their own meaning of "connected".
"""
from __future__ import annotations

from typing import Any

from .node_models import (
    CONNECTION_CONNECTED, CONNECTION_DEGRADED, CONNECTION_OFFLINE,
    CONNECTION_RECOVERING, Node, connection_state_for_node,
    connection_transport_for_node,
)


class ConnectionManager:
    def node_view(self, node: Node) -> dict[str, Any]:
        return {
            "node_id": node.id,
            "node_name": node.display_name,
            "connection_state": connection_state_for_node(node),
            "connection_transport": connection_transport_for_node(node),
            "status": node.status,
            "health_state": node.health_state,
            "execution_state": node.execution_state,
            "transport_state": node.transport_state,
            "last_seen_at": node.last_heartbeat_at,
            "ping_latency_ms": node.latency_ms,
            "last_successful_probe_at": node.last_successful_probe_at,
            "consecutive_failures": node.consecutive_failures,
            "reconnect_status": node.reconnect_status,
            "retry_at": node.next_retry_at,
            "last_error": node.last_error,
        }

    def fleet_view(self, nodes: list[Node]) -> dict[str, Any]:
        rows = [self.node_view(node) for node in nodes]
        counts = {state: 0 for state in (
            CONNECTION_CONNECTED, CONNECTION_DEGRADED, CONNECTION_OFFLINE,
            CONNECTION_RECOVERING,
        )}
        for row in rows:
            counts[row["connection_state"]] += 1
        return {
            "nodes": rows,
            "summary": {
                "total": len(rows),
                "connected": counts[CONNECTION_CONNECTED],
                "degraded": counts[CONNECTION_DEGRADED],
                "offline": counts[CONNECTION_OFFLINE],
                "recovering": counts[CONNECTION_RECOVERING],
                "counts": counts,
            },
        }
