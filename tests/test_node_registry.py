"""node_registry.py: NodeRegistry persistence, status derivation, and the
overload heuristic (classify_capacity). classify_capacity is tested as a
pure function (plain dict in, tuple out) separately from the store, since
that is exactly how it is meant to be exercised -- see its own docstring.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from terminal_mcp.host_metrics import NodeMetrics
from terminal_mcp.node_models import (
    CAPACITY_BUSY,
    CAPACITY_HEALTHY,
    CAPACITY_OVERLOADED,
    CAPACITY_UNKNOWN,
    NODE_DEGRADED,
    NODE_OFFLINE,
    NODE_ONLINE,
    NodeHeartbeatThresholds,
    OverloadThresholds,
)
from terminal_mcp.node_registry import NodeRegistry, classify_capacity


def _metrics(**overrides) -> NodeMetrics:
    base = dict(cpu_percent=20.0, load1=1.0, load5=1.0, load15=1.0, cpu_count=8,
               ram_total_bytes=16_000_000_000, ram_used_bytes=4_000_000_000, ram_percent=25.0,
               swap_total_bytes=1_000_000_000, swap_used_bytes=0, swap_percent=0.0,
               disk_total_bytes=500_000_000_000, disk_used_bytes=100_000_000_000,
               disk_free_bytes=400_000_000_000, disk_percent=20.0)
    base.update(overrides)
    return NodeMetrics(**base)


# -- classify_capacity: pure heuristic -----------------------------------

def test_classify_capacity_healthy_baseline():
    thresholds = OverloadThresholds()
    status, reasons = classify_capacity(
        {"ram_percent_smoothed": 30.0, "cpu_percent_smoothed": 10.0, "swap_percent_smoothed": 0.0,
         "load1": 1.0, "cpu_count": 8, "disk_percent": 20.0, "high_cpu_since": None,
         "high_load_since": None, "_now": "2026-01-01T00:00:00+00:00"},
        thresholds,
    )
    assert status == CAPACITY_HEALTHY
    assert reasons == []


def test_classify_capacity_ram_busy_then_overloaded():
    thresholds = OverloadThresholds()
    common = {"cpu_percent_smoothed": 10.0, "swap_percent_smoothed": 0.0, "load1": 1.0, "cpu_count": 8,
             "disk_percent": 20.0, "high_cpu_since": None, "high_load_since": None,
             "_now": "2026-01-01T00:00:00+00:00"}
    busy_status, busy_reasons = classify_capacity({**common, "ram_percent_smoothed": 82.0}, thresholds)
    assert busy_status == CAPACITY_BUSY
    assert any("RAM" in r for r in busy_reasons)
    overloaded_status, _ = classify_capacity({**common, "ram_percent_smoothed": 92.0}, thresholds)
    assert overloaded_status == CAPACITY_OVERLOADED


def test_classify_capacity_swap_alone_never_triggers_overload():
    # "swap usage tăng + RAM cao => Overloaded" -- swap alone, with RAM
    # comfortably low, must never trip Overloaded on its own.
    thresholds = OverloadThresholds()
    status, reasons = classify_capacity(
        {"ram_percent_smoothed": 30.0, "cpu_percent_smoothed": 10.0, "swap_percent_smoothed": 50.0,
         "load1": 1.0, "cpu_count": 8, "disk_percent": 20.0, "high_cpu_since": None,
         "high_load_since": None, "_now": "2026-01-01T00:00:00+00:00"},
        thresholds,
    )
    assert status == CAPACITY_HEALTHY
    assert reasons == []


def test_classify_capacity_swap_plus_high_ram_is_overloaded():
    thresholds = OverloadThresholds()
    status, reasons = classify_capacity(
        {"ram_percent_smoothed": 85.0, "cpu_percent_smoothed": 10.0, "swap_percent_smoothed": 25.0,
         "load1": 1.0, "cpu_count": 8, "disk_percent": 20.0, "high_cpu_since": None,
         "high_load_since": None, "_now": "2026-01-01T00:00:00+00:00"},
        thresholds,
    )
    assert status == CAPACITY_OVERLOADED
    assert any("swap" in r for r in reasons)


def test_classify_capacity_cpu_high_but_not_yet_sustained_is_busy_not_overloaded():
    thresholds = OverloadThresholds(sustained_seconds=300.0)
    status, _ = classify_capacity(
        {"ram_percent_smoothed": 30.0, "cpu_percent_smoothed": 97.0, "swap_percent_smoothed": 0.0,
         "load1": 1.0, "cpu_count": 8, "disk_percent": 20.0,
         "high_cpu_since": "2026-01-01T00:00:00+00:00",  # just crossed, 0s elapsed
         "high_load_since": None, "_now": "2026-01-01T00:00:00+00:00"},
        thresholds,
    )
    assert status == CAPACITY_BUSY  # high, but not SUSTAINED yet


def test_classify_capacity_cpu_sustained_past_overload_threshold_is_overloaded():
    thresholds = OverloadThresholds(sustained_seconds=300.0)
    status, reasons = classify_capacity(
        {"ram_percent_smoothed": 30.0, "cpu_percent_smoothed": 97.0, "swap_percent_smoothed": 0.0,
         "load1": 1.0, "cpu_count": 8, "disk_percent": 20.0,
         "high_cpu_since": "2026-01-01T00:00:00+00:00",
         "high_load_since": None, "_now": "2026-01-01T00:10:00+00:00"},  # 600s later
        thresholds,
    )
    assert status == CAPACITY_OVERLOADED
    assert any("sustained" in r for r in reasons)


def test_classify_capacity_load1_over_cores_factor_is_busy():
    thresholds = OverloadThresholds(load_factor_busy=1.2)
    status, reasons = classify_capacity(
        {"ram_percent_smoothed": 30.0, "cpu_percent_smoothed": 10.0, "swap_percent_smoothed": 0.0,
         "load1": 10.0, "cpu_count": 8, "disk_percent": 20.0,  # 10 > 8*1.2=9.6
         "high_cpu_since": None, "high_load_since": None, "_now": "2026-01-01T00:00:00+00:00"},
        thresholds,
    )
    assert status == CAPACITY_BUSY
    assert any("load1" in r for r in reasons)


def test_classify_capacity_disk_low_free_is_overloaded():
    thresholds = OverloadThresholds(disk_free_overloaded_percent=10.0)
    status, reasons = classify_capacity(
        {"ram_percent_smoothed": 30.0, "cpu_percent_smoothed": 10.0, "swap_percent_smoothed": 0.0,
         "load1": 1.0, "cpu_count": 8, "disk_percent": 95.0,  # 5% free < 10%
         "high_cpu_since": None, "high_load_since": None, "_now": "2026-01-01T00:00:00+00:00"},
        thresholds,
    )
    assert status == CAPACITY_OVERLOADED
    assert any("disk free" in r for r in reasons)


def test_classify_capacity_no_metrics_at_all_is_unknown():
    thresholds = OverloadThresholds()
    status, reasons = classify_capacity(
        {"ram_percent_smoothed": None, "cpu_percent_smoothed": None, "swap_percent_smoothed": None,
         "load1": None, "cpu_count": None, "disk_percent": None, "high_cpu_since": None,
         "high_load_since": None, "_now": "2026-01-01T00:00:00+00:00"},
        thresholds,
    )
    assert status == CAPACITY_UNKNOWN
    assert reasons == []


# -- NodeRegistry: persistence + derived status ---------------------------

def test_register_then_heartbeat_produces_online_healthy_node(tmp_path):
    registry = NodeRegistry(tmp_path / "nodes.db")
    registry.register("dell", display_name="Dell", hostname="dell-host", endpoint="local")
    node = registry.heartbeat("dell", metrics=_metrics(), tmux_session_count=5,
                              agent_counts={"claude": 3}, agent_types=("shell", "claude"),
                              agent_version="0.13.0", labels=("primary",))
    assert node is not None
    assert node.status == NODE_ONLINE
    assert node.capacity_status == CAPACITY_HEALTHY
    assert node.tmux_session_count == 5
    assert node.agent_counts == {"claude": 3}
    assert node.labels == ("primary",)


def test_heartbeat_for_unregistered_node_returns_none(tmp_path):
    registry = NodeRegistry(tmp_path / "nodes.db")
    result = registry.heartbeat("ghost", metrics=_metrics(), tmux_session_count=0,
                                agent_counts={}, agent_types=(), agent_version=None, labels=())
    assert result is None


def test_status_derived_online_degraded_offline_from_heartbeat_age(tmp_path):
    thresholds = NodeHeartbeatThresholds(degraded_after_seconds=60.0, offline_after_seconds=180.0)
    registry = NodeRegistry(tmp_path / "nodes.db", heartbeat_thresholds=thresholds)
    registry.register("n1", display_name="N1", hostname="h1", endpoint="local")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    registry.heartbeat("n1", metrics=_metrics(), tmux_session_count=0, agent_counts={},
                       agent_types=(), agent_version=None, labels=(), now=now)

    assert registry.get("n1", now=now + timedelta(seconds=30)).status == NODE_ONLINE
    assert registry.get("n1", now=now + timedelta(seconds=120)).status == NODE_DEGRADED
    assert registry.get("n1", now=now + timedelta(seconds=300)).status == NODE_OFFLINE


def test_never_heartbeated_node_is_offline(tmp_path):
    registry = NodeRegistry(tmp_path / "nodes.db")
    registry.register("n1", display_name="N1", hostname="h1", endpoint="local")
    node = registry.get("n1")
    assert node.status == NODE_OFFLINE
    assert node.capacity_status == CAPACITY_UNKNOWN


def test_registry_row_persists_through_offline_period_item_10(tmp_path):
    # Task item 10: "session registry không biến mất ngay; mark stale/
    # offline" -- the row must still be there (and reappear as ONLINE the
    # instant a fresh heartbeat arrives), never deleted just because it
    # went quiet for a while.
    registry = NodeRegistry(tmp_path / "nodes.db")
    registry.register("n1", display_name="N1", hostname="h1", endpoint="local")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    registry.heartbeat("n1", metrics=_metrics(), tmux_session_count=2, agent_counts={},
                       agent_types=(), agent_version=None, labels=(), now=now)
    long_offline = registry.get("n1", now=now + timedelta(hours=48))
    assert long_offline is not None
    assert long_offline.status == NODE_OFFLINE
    assert long_offline.tmux_session_count == 2  # last known value, never wiped

    reconnected = registry.heartbeat("n1", metrics=_metrics(), tmux_session_count=3, agent_counts={},
                                     agent_types=(), agent_version=None, labels=(), now=now + timedelta(hours=49))
    assert reconnected.status == NODE_ONLINE
    assert reconnected.tmux_session_count == 3


def test_ewma_smoothing_dampens_a_single_spike(tmp_path):
    thresholds = OverloadThresholds(smoothing_alpha=0.4)
    registry = NodeRegistry(tmp_path / "nodes.db", overload_thresholds=thresholds)
    registry.register("n1", display_name="N1", hostname="h1", endpoint="local")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for _ in range(5):
        node = registry.heartbeat("n1", metrics=_metrics(ram_percent=20.0), tmux_session_count=0,
                                  agent_counts={}, agent_types=(), agent_version=None, labels=(), now=now)
        now += timedelta(seconds=20)
    assert node.ram_percent_smoothed == pytest.approx(20.0, abs=0.5)

    # One single spike to 100% -- smoothed value must move only partway,
    # never jump straight to the raw spike value.
    spiked = registry.heartbeat("n1", metrics=_metrics(ram_percent=100.0), tmux_session_count=0,
                                agent_counts={}, agent_types=(), agent_version=None, labels=(), now=now)
    assert spiked.ram_percent == 100.0  # raw IS the spike
    assert spiked.ram_percent_smoothed == pytest.approx(20.0 + 0.4 * (100.0 - 20.0), abs=0.5)
    assert spiked.ram_percent_smoothed < 60.0  # meaningfully damped, not the raw spike


def test_draining_flag_persists_independent_of_status(tmp_path):
    registry = NodeRegistry(tmp_path / "nodes.db")
    registry.register("n1", display_name="N1", hostname="h1", endpoint="local")
    assert registry.get("n1").draining is False
    assert registry.set_draining("n1", True) is True
    node = registry.get("n1")
    assert node.draining is True
    assert node.status == NODE_OFFLINE  # draining is independent of online/offline
    assert registry.set_draining("unknown-node", True) is False


def test_register_is_idempotent_and_never_resets_metrics(tmp_path):
    registry = NodeRegistry(tmp_path / "nodes.db")
    registry.register("n1", display_name="N1", hostname="h1", endpoint="local")
    registry.heartbeat("n1", metrics=_metrics(ram_percent=42.0), tmux_session_count=7, agent_counts={},
                       agent_types=(), agent_version=None, labels=())
    registry.register("n1", display_name="N1 renamed", hostname="h1", endpoint="local")
    node = registry.get("n1")
    assert node.display_name == "N1 renamed"
    assert node.tmux_session_count == 7  # untouched by a plain re-register


def test_deregister_removes_the_row(tmp_path):
    registry = NodeRegistry(tmp_path / "nodes.db")
    registry.register("n1", display_name="N1", hostname="h1", endpoint="local")
    assert registry.deregister("n1") is True
    assert registry.get("n1") is None
    assert registry.deregister("n1") is False  # idempotent -- already gone
