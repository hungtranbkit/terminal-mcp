"""The M910-failure matrix, as tests rather than a table someone maintains.

Each case below is a way the controller can go away, or a way a node can come
back wrong. They run against real processes and real sqlite stores; nothing
here mocks the thing under test. No real node is touched and the live
controller is never stopped -- "M910 is gone" is always simulated by pointing
a disposable agent at a port nobody is listening on, or by feeding the
registry the state a returning node would produce.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import pathlib

import pytest
import yaml

from terminal_mcp import contract
from terminal_mcp.config import AutoRecoveryConfig
from terminal_mcp.host_metrics import NodeMetrics
from terminal_mcp.lease import PaneLeaseStore
from terminal_mcp.node_models import NODE_OFFLINE, NODE_ONLINE
from terminal_mcp.node_registry import NodeRegistry
from terminal_mcp.recovery_engine import RECOVERY_STATE_RECONNECTED, RecoveryEngine
from terminal_mcp.session_registry import SessionRegistryStore

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _metrics() -> NodeMetrics:
    return NodeMetrics(cpu_percent=1.0, load1=0.1, load5=0.1, load15=0.1, cpu_count=4,
                       ram_total_bytes=1, ram_used_bytes=1, ram_percent=1.0,
                       swap_total_bytes=1, swap_used_bytes=0, swap_percent=0.0,
                       disk_total_bytes=1, disk_used_bytes=1, disk_free_bytes=0, disk_percent=1.0)


class _Controller:
    """Enough controller for the recovery engine, with a settable liveness view."""

    def __init__(self):
        self.reopen_calls: list[str] = []
        self.live: dict[str, str] = {}
        self.rows: dict[str, list[dict]] = {}

    def terminal_registry_reopen(self, qualified, *, requested_by=None):
        self.reopen_calls.append(qualified)
        return {"session": qualified, "recreated_from_registry": True}

    def registry_list(self, node_id, *, recoverable_only=False):
        return {"records": self.rows.get(node_id, [])}

    def terminal_list_sessions(self):
        return {"sessions": [{"name": n, "node_id": nid} for n, nid in self.live.items()]}


@pytest.fixture
def engine(tmp_path):
    registry = SessionRegistryStore(tmp_path / "registry.db")
    controller = _Controller()
    store = PaneLeaseStore(tmp_path / "leases.db")
    return registry, controller, RecoveryEngine(registry, controller, store,
                                                AutoRecoveryConfig(enabled=True))


# -- node registry view of an absent controller/peer ------------------------

def test_a_node_that_stops_heartbeating_ages_to_offline_not_forgotten(tmp_path):
    """A peer going quiet must become visibly OFFLINE and keep its row. A
    forgotten node is worse than an offline one: nothing left to reconcile."""
    registry = NodeRegistry(tmp_path / "nodes.db")
    registry.register("peer", display_name="peer", hostname="h", endpoint="http://p:8790")
    registry.heartbeat("peer", metrics=_metrics(), tmux_session_count=2, agent_counts={},
                       agent_types=("shell",), agent_version="0.12.0", labels=())
    assert registry.get("peer").status == NODE_ONLINE

    # Backdate the heartbeat past the offline threshold.
    import sqlite3
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    with sqlite3.connect(tmp_path / "nodes.db") as conn:
        conn.execute("UPDATE nodes SET last_heartbeat_at = ? WHERE id = ?", (old, "peer"))

    node = registry.get("peer")
    assert node.status == NODE_OFFLINE
    assert node.tmux_session_count == 2       # last-known state retained
    assert registry.get("peer") is not None   # row not dropped


def test_duplicate_heartbeats_do_not_duplicate_a_node(tmp_path):
    registry = NodeRegistry(tmp_path / "nodes.db")
    registry.register("peer", display_name="peer", hostname="h", endpoint="http://p:8790")
    for _ in range(5):
        registry.heartbeat("peer", metrics=_metrics(), tmux_session_count=1, agent_counts={},
                           agent_types=("shell",), agent_version="0.12.0", labels=())
    assert [n.id for n in registry.list()].count("peer") == 1


def test_a_stale_version_peer_is_flagged_not_silently_trusted(tmp_path):
    """The version-skew case that was invisible until the contract existed."""
    registry = NodeRegistry(tmp_path / "nodes.db")
    registry.register("old", display_name="old", hostname="h", endpoint="http://o:8790")
    registry.heartbeat("old", metrics=_metrics(), tmux_session_count=0, agent_counts={},
                       agent_types=("shell",), agent_version="0.12.0", labels=())
    node = registry.get("old")
    compat = contract.compatibility(node.contract_version, node.contract_capabilities)
    assert compat["status"] == "degraded"
    assert compat["missing_capabilities"]      # names what must not be assumed


# -- recovery behaviour when a node comes back ------------------------------

def test_node_returns_with_sessions_intact_reconnects_without_respawning(engine):
    """The common reboot-of-the-CONTROLLER case: the node never went away, so
    nothing may be spawned."""
    registry, controller, recovery = engine
    registry.upsert_seen("n1", "survivor", agent_type="shell", cwd="/w")
    registry.mark_missing("n1", set())
    controller.live = {"survivor": "n1"}

    result = recovery.recover_session("n1", "survivor")
    assert result["soft_reconnect"] is True
    assert result["recovery_state"] == RECOVERY_STATE_RECONNECTED
    assert controller.reopen_calls == []


def test_node_returns_with_sessions_gone_recovers_exactly_once(engine):
    registry, controller, recovery = engine
    registry.upsert_seen("n1", "gone", agent_type="shell", cwd="/w")
    registry.mark_missing("n1", set())
    controller.rows["n1"] = [{"session_name": "gone"}]

    first = recovery.reconcile_node("n1")
    assert all("error" not in r for r in first), first
    assert len(controller.reopen_calls) == 1

    # A second pass while the first result still stands must not spawn again.
    controller.live = {"gone": "n1"}
    second = recovery.reconcile_node("n1")
    assert all(r.get("soft_reconnect") for r in second), second
    assert len(controller.reopen_calls) == 1


def test_a_tombstoned_session_survives_repeated_reconciles(engine):
    """Long offline, then online, then online again -- an operator's stop must
    still be respected on the tenth pass, not just the first."""
    registry, controller, recovery = engine
    registry.upsert_seen("n1", "stopped", agent_type="shell", cwd="/w")
    registry.mark_killed("n1", "stopped", killed_by="operator")
    controller.rows["n1"] = [{"session_name": "stopped"}]

    for _ in range(3):
        results = recovery.reconcile_node("n1")
        assert results[0]["error"] == "RECOVERY_TOMBSTONED"
    assert controller.reopen_calls == []


def test_recovery_respects_a_bounded_attempt_budget(engine, tmp_path):
    """A node that keeps failing must stop being retried, not spin forever."""
    registry, controller, _ = engine
    store = PaneLeaseStore(tmp_path / "leases2.db")
    recovery = RecoveryEngine(registry, controller, store,
                              AutoRecoveryConfig(enabled=True, max_attempts=2))

    def _fail(qualified, *, requested_by=None):
        controller.reopen_calls.append(qualified)
        return {"error": "RECOVERY_FAILED", "session": qualified}

    controller.terminal_registry_reopen = _fail
    registry.upsert_seen("n1", "doomed", agent_type="shell", cwd="/w")
    registry.mark_missing("n1", set())

    seen_block = False
    for _ in range(6):
        result = recovery.recover_session("n1", "doomed")
        if result.get("error") == "RECOVERY_BLOCKED":
            seen_block = True
            break
    assert seen_block, "attempt budget never stopped the retries"
    assert len(controller.reopen_calls) <= 2


# -- sqlite under concurrent writers ----------------------------------------

def test_two_processes_writing_the_same_registry_do_not_corrupt_it(tmp_path):
    """Node agent and controller can share a state path on a co-located node.
    WAL plus short transactions must survive that, or the shared-state design
    is unsafe."""
    db = tmp_path / "shared.db"
    SessionRegistryStore(db)  # create schema once
    script = (
        "import sys;"
        "from terminal_mcp.session_registry import SessionRegistryStore;"
        "s=SessionRegistryStore(sys.argv[1]);"
        "[s.upsert_seen('n1', f'{sys.argv[2]}-{i}', agent_type='shell', cwd='/w') for i in range(40)]"
    )
    procs = [subprocess.Popen([sys.executable, "-c", script, str(db), tag],
                              cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
             for tag in ("a", "b")]
    outs = [(p.wait(timeout=60), p.stdout.read()) for p in procs]
    for code, out in outs:
        assert code == 0, out

    store = SessionRegistryStore(db)
    names = {r.session_name for r in store.list()}
    assert len({n for n in names if n.startswith("a-")}) == 40
    assert len({n for n in names if n.startswith("b-")}) == 40
