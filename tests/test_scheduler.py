"""scheduler.py: choose_node's eligibility gate + deterministic scoring
(task item 6). Pure function over plain Node dataclasses -- no I/O, no
registry, no tmux needed."""
from __future__ import annotations

from terminal_mcp.node_models import (
    CAPACITY_HEALTHY,
    CAPACITY_OVERLOADED,
    NODE_OFFLINE,
    NODE_ONLINE,
    PLATFORM_LINUX,
    Node,
)
from terminal_mcp.scheduler import choose_node


def _node(id, *, status=NODE_ONLINE, draining=False, capacity=CAPACITY_HEALTHY,
          ram_percent=30.0, cpu_percent=10.0, sessions=0, agent_types=("shell",),
          max_sessions=None, disk_free_bytes=100 * 1024 ** 3, labels=(), overload_reasons=(),
          platform=PLATFORM_LINUX) -> Node:
    return Node(
        id=id, display_name=id, hostname=f"{id}-host", endpoint="local", status=status,
        draining=draining, last_heartbeat_at="2026-01-01T00:00:00+00:00", latency_ms=1.0,
        cpu_percent=cpu_percent, cpu_percent_smoothed=cpu_percent, load1=1.0, load5=1.0, load15=1.0,
        cpu_count=8, ram_total_bytes=16 * 1024 ** 3, ram_used_bytes=0, ram_percent=ram_percent,
        ram_percent_smoothed=ram_percent, swap_total_bytes=0, swap_used_bytes=0, swap_percent=0.0,
        swap_percent_smoothed=0.0, disk_total_bytes=500 * 1024 ** 3, disk_used_bytes=0,
        disk_free_bytes=disk_free_bytes, disk_percent=10.0, tmux_session_count=sessions,
        agent_counts={}, agent_types=agent_types, agent_version="0.13.0", labels=labels,
        max_sessions=max_sessions, capacity_status=capacity, overload_reasons=overload_reasons,
        platform=platform,
    )


def test_picks_the_single_eligible_node():
    result = choose_node([_node("dell")])
    assert result.node_id == "dell"
    assert result.candidates_considered == 1
    assert result.excluded == ()


def test_no_nodes_registered_at_all():
    result = choose_node([])
    assert result.node_id is None
    assert "no nodes registered" in result.reason


def test_prefers_more_ram_headroom():
    busy = _node("busy", ram_percent=85.0)
    idle = _node("idle", ram_percent=20.0)
    result = choose_node([busy, idle])
    assert result.node_id == "idle"


def test_ram_ties_broken_by_cpu_headroom():
    a = _node("a", ram_percent=30.0, cpu_percent=50.0)
    b = _node("b", ram_percent=30.0, cpu_percent=10.0)
    result = choose_node([a, b])
    assert result.node_id == "b"


def test_ram_and_cpu_ties_broken_by_fewer_sessions():
    a = _node("a", ram_percent=30.0, cpu_percent=10.0, sessions=5)
    b = _node("b", ram_percent=30.0, cpu_percent=10.0, sessions=1)
    result = choose_node([a, b])
    assert result.node_id == "b"


def test_full_tie_broken_deterministically_by_node_id():
    a = _node("zzz", ram_percent=30.0, cpu_percent=10.0, sessions=0)
    b = _node("aaa", ram_percent=30.0, cpu_percent=10.0, sessions=0)
    result1 = choose_node([a, b])
    result2 = choose_node([b, a])  # order-independence: same winner regardless of input order
    assert result1.node_id == result2.node_id == "zzz"  # tuple compare: ("zzz" > "aaa")


def test_offline_node_excluded():
    result = choose_node([_node("dell", status=NODE_OFFLINE)])
    assert result.node_id is None
    assert result.excluded[0][0] == "dell"
    assert "status=offline" in result.excluded[0][1]


def test_draining_node_excluded():
    online_but_draining = _node("dell", draining=True)
    result = choose_node([online_but_draining])
    assert result.node_id is None
    assert "draining" in result.excluded[0][1]


def test_overloaded_node_excluded():
    result = choose_node([_node("dell", capacity=CAPACITY_OVERLOADED, overload_reasons=("RAM 95%",))])
    assert result.node_id is None
    assert "overloaded" in result.excluded[0][1]
    assert "RAM 95%" in result.excluded[0][1]


def test_draining_node_excluded_even_when_others_are_healthy():
    draining = _node("drain-me", draining=True)
    healthy = _node("healthy")
    result = choose_node([draining, healthy])
    assert result.node_id == "healthy"
    assert any(nid == "drain-me" for nid, _ in result.excluded)


def test_agent_type_capability_mismatch_excluded():
    shell_only = _node("shell-only", agent_types=("shell",))
    result = choose_node([shell_only], required_agent_type="claude")
    assert result.node_id is None
    assert "agent_type" in result.excluded[0][1]


def test_shell_agent_type_always_eligible_even_with_no_reported_types():
    node = _node("bare", agent_types=())
    result = choose_node([node], required_agent_type="shell")
    assert result.node_id == "bare"


def test_agent_type_capability_match_selects_node():
    claude_capable = _node("claude-node", agent_types=("shell", "claude"))
    shell_only = _node("shell-only", agent_types=("shell",), ram_percent=5.0)  # more headroom but ineligible
    result = choose_node([claude_capable, shell_only], required_agent_type="claude")
    assert result.node_id == "claude-node"


def test_at_max_sessions_excluded():
    result = choose_node([_node("full", sessions=10, max_sessions=10)])
    assert result.node_id is None
    assert "max_sessions" in result.excluded[0][1]


def test_below_disk_floor_excluded():
    result = choose_node([_node("low-disk", disk_free_bytes=100 * 1024 * 1024)],  # 100 MiB
                         min_disk_free_bytes=1024 ** 3)  # 1 GiB required
    assert result.node_id is None
    assert "disk_free_bytes" in result.excluded[0][1]


def test_required_label_missing_excluded():
    result = choose_node([_node("unlabeled")], labels_required=("gpu",))
    assert result.node_id is None
    assert "labels" in result.excluded[0][1]


def test_required_label_present_selects_node():
    labeled = _node("gpu-node", labels=("gpu",))
    unlabeled = _node("plain", ram_percent=5.0)  # more headroom, but lacks the required label
    result = choose_node([labeled, unlabeled], labels_required=("gpu",))
    assert result.node_id == "gpu-node"


def test_excluded_nodes_still_reported_alongside_a_successful_pick():
    winner = _node("dell")
    loser = _node("overloaded-node", capacity=CAPACITY_OVERLOADED)
    result = choose_node([winner, loser])
    assert result.node_id == "dell"
    assert result.candidates_considered == 2
    assert any(nid == "overloaded-node" for nid, _ in result.excluded)


# -- platform requirement (multi-node Windows support) ----------------------

def test_no_platform_requirement_ignores_platform_entirely():
    linux_node = _node("dell", platform="linux")
    windows_node = _node("m910", platform="windows", ram_percent=5.0)  # more headroom
    result = choose_node([linux_node, windows_node])
    assert result.node_id == "m910"  # picked purely on headroom, platform irrelevant


def test_required_platform_excludes_the_other_platform():
    linux_node = _node("dell", platform="linux")
    windows_node = _node("m910", platform="windows", ram_percent=5.0)  # more headroom, but wrong platform
    result = choose_node([linux_node, windows_node], required_platform="linux")
    assert result.node_id == "dell"
    assert any(nid == "m910" and "platform" in why for nid, why in result.excluded)


def test_required_platform_windows_selects_windows_node():
    linux_node = _node("dell", platform="linux", ram_percent=5.0)  # more headroom, but wrong platform
    windows_node = _node("m910", platform="windows")
    result = choose_node([linux_node, windows_node], required_platform="windows")
    assert result.node_id == "m910"


def test_required_platform_no_matching_node_is_no_eligible_node():
    linux_node = _node("dell", platform="linux")
    result = choose_node([linux_node], required_platform="windows")
    assert result.node_id is None
    assert "platform" in result.excluded[0][1]


def test_windows_node_with_no_agent_binaries_only_eligible_for_shell():
    # Real cross-platform scenario: a Windows node whose config found no
    # claude/codex CLI at all (agent_availability.py) reports
    # agent_types=("shell",) only -- exactly like a Linux node in the
    # same situation, no platform-specific carve-out needed in the
    # scheduler itself for this case (agent_type filtering already
    # handles it identically on every platform).
    windows_node = _node("m910", platform="windows", agent_types=("shell",))
    result = choose_node([windows_node], required_agent_type="claude")
    assert result.node_id is None
    assert "agent_type" in result.excluded[0][1]
