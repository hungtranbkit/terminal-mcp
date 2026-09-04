"""Multi-node session management -- shared data shapes (registry rows,
overload thresholds/config). See node_registry.py for persistence + the
overload heuristic, node_client.py for how a node is actually talked to,
controller.py for routing, scheduler.py for Auto placement.

Design note repeated in every module in this feature (worth stating once,
here, since every other file assumes it): the LOCAL node (this Dell
deployment today) is a node like any other, never a special case in
business logic -- only its *transport* (LocalNodeClient, in-process, no
network hop) differs from a remote node's (RemoteNodeClient, HTTP +
bearer token). See docs/multi-node.md for the full architecture writeup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# -- node lifecycle status (DERIVED from heartbeat recency at read time,
# never persisted directly -- see NodeRegistry.get/list) -------------------
NODE_ONLINE = "online"
NODE_DEGRADED = "degraded"
NODE_OFFLINE = "offline"
NODE_STATUSES = (NODE_ONLINE, NODE_DEGRADED, NODE_OFFLINE)

# A node whose operator has set draining=True is excluded from the
# scheduler regardless of its online/offline status -- draining is
# reported as a separate boolean (task's own field list), not folded into
# the status enum, since a node can be simultaneously "online" (healthy,
# reachable, heartbeating) AND "draining" (operator wants no NEW sessions
# placed here, existing ones untouched).

# -- capacity/overload status (see node_registry.py's classify_capacity) ---
CAPACITY_HEALTHY = "healthy"
CAPACITY_BUSY = "busy"
CAPACITY_OVERLOADED = "overloaded"
CAPACITY_UNKNOWN = "unknown"  # metrics not yet available (e.g. never heartbeated)
CAPACITY_STATUSES = (CAPACITY_HEALTHY, CAPACITY_BUSY, CAPACITY_OVERLOADED, CAPACITY_UNKNOWN)


@dataclass(frozen=True)
class OverloadThresholds:
    """Soft defaults straight from the task's own spec -- every field is
    operator-configurable (config.yaml's nodes.overload_thresholds), never
    hardcoded past this dataclass's defaults. `sustained_seconds`: how
    long CPU/load must stay above their threshold before a Busy reading
    escalates to Overloaded -- see NodeRegistry's high_cpu_since/
    high_load_since tracking for how "sustained" is actually measured
    (real elapsed wall-clock time above threshold across heartbeats, not
    a fixed sample count, so it stays correct however often heartbeats
    actually arrive)."""
    ram_busy_percent: float = 80.0
    ram_overloaded_percent: float = 90.0
    swap_overloaded_percent: float = 20.0  # combined with ram_busy_percent+ -- see classify_capacity
    cpu_busy_percent: float = 85.0
    cpu_overloaded_percent: float = 95.0
    sustained_seconds: float = 300.0  # "N phút" -- 5 minutes, a reasonable default N
    load_factor_busy: float = 1.2  # load1 > cores * this -> busy-eligible
    disk_free_overloaded_percent: float = 10.0  # disk free BELOW this -> overloaded (no new sessions)
    # Smoothing (task item 5: "không chỉ dùng snapshot tức thời"): simple
    # EWMA, applied at every heartbeat write -- see NodeRegistry.heartbeat.
    # alpha closer to 1.0 = more responsive/less smoothed; 0.4 damps a
    # single noisy sample to well under half its own swing.
    smoothing_alpha: float = 0.4


@dataclass(frozen=True)
class NodeHeartbeatThresholds:
    """When a node's status (online/degraded/offline) is DERIVED from
    heartbeat age -- see node_registry.py's classify_status. Configurable
    (config.yaml's nodes.heartbeat_interval_seconds and a multiple of it),
    never hardcoded past these defaults."""
    degraded_after_seconds: float = 60.0
    offline_after_seconds: float = 180.0


@dataclass(frozen=True)
class NodeCapabilities:
    """What a node CAN do -- reported by the node agent itself at
    heartbeat time (never assumed/guessed centrally). agent_types: which
    launch_commands this node's own config.session_lifecycle recognizes
    (e.g. ("shell","claude","codex")) -- the scheduler (item 6's "agent
    capability phù hợp") only ever places a session needing agent_type=X
    on a node that actually lists X here."""
    agent_types: tuple[str, ...] = ("shell",)
    agent_version: str | None = None  # this project's own __version__ on that node
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class Node:
    """One row from NodeRegistry, with status/capacity_status already
    resolved (DERIVED fields, computed at read time -- see
    NodeRegistry.get/list, never trust a caller-cached copy of this for
    longer than the length of one request)."""
    id: str
    display_name: str
    hostname: str
    endpoint: str  # "local" for the in-process node, else "http://host:port"
    status: str  # NODE_STATUSES -- derived from heartbeat age
    draining: bool
    last_heartbeat_at: str | None
    latency_ms: float | None
    cpu_percent: float | None
    cpu_percent_smoothed: float | None
    load1: float | None
    load5: float | None
    load15: float | None
    cpu_count: int | None
    ram_total_bytes: int | None
    ram_used_bytes: int | None
    ram_percent: float | None
    ram_percent_smoothed: float | None
    swap_total_bytes: int | None
    swap_used_bytes: int | None
    swap_percent: float | None
    swap_percent_smoothed: float | None
    disk_total_bytes: int | None
    disk_used_bytes: int | None
    disk_free_bytes: int | None
    disk_percent: float | None
    tmux_session_count: int | None
    agent_counts: dict[str, int] = field(default_factory=dict)
    agent_types: tuple[str, ...] = ()
    agent_version: str | None = None
    labels: tuple[str, ...] = ()
    max_sessions: int | None = None
    capacity_status: str = CAPACITY_UNKNOWN
    overload_reasons: tuple[str, ...] = ()
    registered_at: str | None = None
    updated_at: str | None = None


def node_to_dict(node: Node) -> dict[str, Any]:
    """One JSON/MCP-tool-result shape for a Node, shared by dashboard.py's
    node routes and mcp_app.py's terminal_list_nodes/terminal_node_status
    -- never two independently-drifting serializations of the same
    dataclass. Smoothed metrics are preferred over the raw instantaneous
    sample where both exist (see node_registry.py's own EWMA note)."""
    return {
        "id": node.id, "display_name": node.display_name, "hostname": node.hostname,
        "endpoint": node.endpoint, "status": node.status, "draining": node.draining,
        "last_heartbeat_at": node.last_heartbeat_at, "latency_ms": node.latency_ms,
        "cpu_percent": node.cpu_percent_smoothed if node.cpu_percent_smoothed is not None else node.cpu_percent,
        "load1": node.load1, "load5": node.load5, "load15": node.load15, "cpu_count": node.cpu_count,
        "ram_percent": node.ram_percent_smoothed if node.ram_percent_smoothed is not None else node.ram_percent,
        "ram_total_bytes": node.ram_total_bytes, "ram_used_bytes": node.ram_used_bytes,
        "swap_percent": node.swap_percent_smoothed if node.swap_percent_smoothed is not None else node.swap_percent,
        "disk_percent": node.disk_percent, "disk_total_bytes": node.disk_total_bytes,
        "disk_used_bytes": node.disk_used_bytes, "disk_free_bytes": node.disk_free_bytes,
        "tmux_session_count": node.tmux_session_count, "agent_counts": node.agent_counts,
        "agent_types": list(node.agent_types), "agent_version": node.agent_version,
        "labels": list(node.labels), "max_sessions": node.max_sessions,
        "capacity_status": node.capacity_status, "overload_reasons": list(node.overload_reasons),
        "registered_at": node.registered_at, "updated_at": node.updated_at,
    }
