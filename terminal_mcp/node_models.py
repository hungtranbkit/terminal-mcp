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


# Node platform -- reported by the node agent itself (never guessed
# centrally, same posture as agent_types below).
PLATFORM_LINUX = "linux"
PLATFORM_WINDOWS = "windows"
PLATFORM_MACOS = "macos"
"""macOS. Spelled `macos`, not `darwin`, because `macos` was ALREADY this
project's word for it everywhere it was discussed before a node could
report it -- verify_queue.py's routing docstring, docs/REQUIREMENTS.md,
docs/ORCHESTRATION_ARCHITECTURE.md and the roadmap all say `macos`, and
backlog item blg_20dc778df7ac's own acceptance criterion says
"platform='macos' (or darwin)". Introducing `darwin` as the stored value
would have meant a second word for one thing, which is how a vocabulary
starts drifting. `darwin` is what the OS calls itself and is accepted as
an INPUT alias below; `macos` is what this fleet stores."""

KNOWN_PLATFORMS = (PLATFORM_LINUX, PLATFORM_WINDOWS, PLATFORM_MACOS)

_PLATFORM_ALIASES: dict[str, str] = {
    # sys.platform values, current and historical, plus the spellings a
    # human or a foreign agent might send. Everything here is an exact
    # match on an already-lowercased, already-stripped string -- there is
    # deliberately no prefix/substring/fuzzy matching and no inference
    # from hostname, since "the box is called macbook" is a guess about
    # the operator's naming habits, not a fact about the OS.
    "darwin": PLATFORM_MACOS,
    "macos": PLATFORM_MACOS,
    "mac": PLATFORM_MACOS,
    "osx": PLATFORM_MACOS,
    "mac os x": PLATFORM_MACOS,
    "linux": PLATFORM_LINUX,
    "linux2": PLATFORM_LINUX,   # sys.platform on Python 2-era/older builds
    "win32": PLATFORM_WINDOWS,  # sys.platform on 64-bit Windows too
    "win64": PLATFORM_WINDOWS,
    "windows": PLATFORM_WINDOWS,
    "cygwin": PLATFORM_WINDOWS,
    "msys": PLATFORM_WINDOWS,
}


def canonical_platform(raw: str | None, *, default: str = PLATFORM_LINUX) -> str:
    """Maps a reported platform string to this fleet's canonical value.

    WHY THIS EXISTS: `node_agent._heartbeat_loop` had `platform="linux"`
    as a parameter DEFAULT and the POSIX `main()` never passed one, so
    every node that is not Windows reported itself as Linux -- including
    macOS, which runs that same POSIX agent (blg_20dc778df7ac). The bug
    was a missing detection, so the fix is a real detection plus one
    place that decides what the detected value is called.

    THREE RULES, in priority order:

    1. An empty/None report means "this agent does not report a
       platform" -- an agent older than the multi-node Windows work --
       and yields `default` (PLATFORM_LINUX at every current call site).
       That is exactly the behaviour those call sites already had, kept
       deliberately: an old agent's heartbeat must not start meaning
       something new the day this function lands.

    2. A recognised alias maps to its canonical value. `darwin` ->
       `macos` is the whole point; `linux`/`win32`/... are listed so
       this function is the ONE answer to "what platform is that
       string", not a macOS special case bolted onto the side.

    3. An UNRECOGNISED non-empty value is lowercased, stripped, and
       RETURNED AS-IS -- never mapped to `default`, never to "unknown".
       If some future node reports `freebsd`, that is a fact worth
       keeping: it will simply not match linux/windows/macos routing
       (exact match, so it fails closed) and it will display as
       `freebsd`. Folding it into `linux` would recreate this very bug
       for the next platform; folding it into `unknown` would throw away
       the only information anyone had.

    Pure and total: never raises, never touches the network, the
    filesystem, or the hostname."""
    if raw is None:
        return default
    text = str(raw).strip().casefold()
    if not text:
        return default
    return _PLATFORM_ALIASES.get(text, text)

# session_backend -- which SessionBackend (session_backend.py) a node's
# own TerminalService was actually constructed with. Distinct from
# `platform` (a Linux node could theoretically run a non-tmux backend in
# the future; today it's a strict 1:1) -- kept as its own field so the
# dashboard/doctor output says WHAT is managing sessions, not just WHICH
# OS, without assuming they're always the same thing.
SESSION_BACKEND_TMUX = "tmux"
SESSION_BACKEND_WINDOWS_PTY = "windows_pty"


@dataclass(frozen=True)
class NodeCapabilities:
    """What a node CAN do -- reported by the node agent itself at
    heartbeat time (never assumed/guessed centrally). agent_types: which
    launch_commands this node's own config.session_lifecycle recognizes
    AND whose launcher binary was actually found on this node (via
    shutil.which -- see agent_availability.py) -- the scheduler (item 6's
    "agent capability phù hợp") only ever places a session needing
    agent_type=X on a node that actually lists X here, whether that
    means "claude not configured" or "configured but the binary isn't
    actually installed on this node" (task's own explicit Windows
    requirement, applied identically on Linux too -- this was a real,
    pre-existing gap on Linux as well, fixed once for both platforms).
    platform/session_backend/shell_capabilities/wsl_available: the rest
    of the multi-node Windows support capability report (task's own
    explicit field list)."""
    agent_types: tuple[str, ...] = ("shell",)
    agent_version: str | None = None  # this project's own __version__ on that node
    labels: tuple[str, ...] = ()
    # P0.3: PROBED tool/runtime capabilities (git/node/docker/dotnet/
    # playwright/...). Deliberately separate from `labels`, which is an
    # operator-supplied grouping tag -- see capability_probe.py for why
    # conflating declared and probed capability is the bug this avoids.
    capabilities: tuple[str, ...] = ()
    platform: str = PLATFORM_LINUX
    session_backend: str = SESSION_BACKEND_TMUX
    shell_capabilities: tuple[str, ...] = ()
    wsl_available: bool = False


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
    # P0.3: PROBED tool/runtime capabilities (git/node/docker/dotnet/
    # playwright/...). Deliberately separate from `labels`, which is an
    # operator-supplied grouping tag -- see capability_probe.py for why
    # conflating declared and probed capability is the bug this avoids.
    capabilities: tuple[str, ...] = ()
    max_sessions: int | None = None
    capacity_status: str = CAPACITY_UNKNOWN
    overload_reasons: tuple[str, ...] = ()
    registered_at: str | None = None
    updated_at: str | None = None
    # Multi-node Windows support -- reported by the node agent itself,
    # never guessed centrally (same posture as agent_types above).
    platform: str = PLATFORM_LINUX
    session_backend: str = SESSION_BACKEND_TMUX
    shell_capabilities: tuple[str, ...] = ()
    wsl_available: bool = False


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
        "labels": list(node.labels), "capabilities": list(node.capabilities),
        "max_sessions": node.max_sessions,
        "capacity_status": node.capacity_status, "overload_reasons": list(node.overload_reasons),
        "registered_at": node.registered_at, "updated_at": node.updated_at,
        "platform": node.platform, "session_backend": node.session_backend,
        "shell_capabilities": list(node.shell_capabilities), "wsl_available": node.wsl_available,
        "claude_available": "claude" in node.agent_types, "codex_available": "codex" in node.agent_types,
    }
