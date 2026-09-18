"""Orchestration V1 -- the WORKER view (roles, capabilities, liveness).

Before this, "worker" was a (node_id, session) string pair with no runtime
entity behind it, and `role` was a nullable free-text column matched by
string equality -- so "verifier", "Verifier" and "verifer" were three
different roles and none was wrong.
"""
from __future__ import annotations

import pytest

from terminal_mcp import queue_store as qs
from terminal_mcp.node_models import NODE_ONLINE, Node
from terminal_mcp.pm_store import PMStore
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.worker_registry import (
    ALL_ROLES,
    ROLE_INTEGRATOR,
    ROLE_VERIFIER,
    ROLE_WORKER,
    WORKER_BUSY,
    WORKER_IDLE,
    WORKER_OFFLINE,
    WorkerRegistry,
    normalise_roles,
)

PROJECT = "git:github.com/acme/widget"


def node(node_id, *, capabilities=(), platform="linux", status=NODE_ONLINE, heartbeat=None):
    return Node(id=node_id, display_name=node_id, hostname=node_id, endpoint="local",
                status=status, draining=False, last_heartbeat_at=heartbeat, latency_ms=None,
                cpu_percent=None, cpu_percent_smoothed=None, load1=None, load5=None, load15=None,
                cpu_count=None, ram_total_bytes=None, ram_used_bytes=None, ram_percent=None,
                ram_percent_smoothed=None, swap_total_bytes=None, swap_used_bytes=None,
                swap_percent=None, swap_percent_smoothed=None, disk_total_bytes=None,
                disk_used_bytes=None, disk_free_bytes=None, disk_percent=None,
                tmux_session_count=None, capabilities=tuple(capabilities), platform=platform)


class _FakeNodes:
    def __init__(self, nodes):
        self._nodes = nodes

    def list(self):
        return list(self._nodes)


@pytest.fixture
def rig(tmp_path):
    pm = PMStore(tmp_path / "pm.db")
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    nodes = _FakeNodes([node("local", capabilities=["python", "playwright"]),
                        node("dell-5530", capabilities=[], platform="windows"),
                        node("gone", capabilities=["python"], status="offline")])
    return WorkerRegistry(pm_store=pm, node_registry=nodes, queue=queue), pm, queue


# -- 1. Roles are an enum now -------------------------------------------

def test_roles_are_normalised_and_unknown_ones_dropped():
    """Dropping rather than raising: role data is operator-typed, and one
    typo should narrow eligibility, never break the read that lists it."""
    assert normalise_roles(["worker", "VERIFIER", "Worker"]) == (ROLE_WORKER, ROLE_VERIFIER)
    assert normalise_roles("integrator") == (ROLE_INTEGRATOR,)
    assert normalise_roles(["verifer", "nonsense"]) == ()
    assert normalise_roles(None) == ()


def test_a_worker_can_hold_several_roles(rig):
    registry, _, _ = rig
    result = registry.declare("local", "s1", roles=[ROLE_WORKER, ROLE_VERIFIER])
    assert result["declared"] is True
    worker = registry.get("local", "s1")
    assert worker.roles == (ROLE_WORKER, ROLE_VERIFIER)
    assert worker.has_role("verifier") and worker.has_role(ROLE_WORKER)
    assert not worker.has_role(ROLE_INTEGRATOR)


def test_an_entirely_invalid_role_set_is_refused(rig):
    registry, _, _ = rig
    result = registry.declare("local", "s1", roles=["nonsense"])
    assert result["error"] == "INVALID_ROLE"
    assert result["allowed"] == list(ALL_ROLES)


def test_a_session_with_no_profile_defaults_to_plain_worker(rig):
    """Every existing session already is one in practice, so the default
    changes nothing."""
    registry, _, queue = rig
    queue.store.set_tasks("s-undeclared", [{"title": "t", "prompt": "p"}])
    worker = registry.get("local", "s-undeclared")
    assert worker is not None
    assert worker.roles == (ROLE_WORKER,) and worker.has_profile is False


# -- 2. Declared vs detected is preserved, not flattened ----------------

def test_probed_and_declared_capabilities_stay_distinguishable(rig):
    """capability_probe exists because a node whose operator WROTE
    `claude:` into config but never installed it was still scheduled as
    claude-capable. Merging the two axes would destroy that distinction."""
    registry, _, _ = rig
    registry.declare("local", "s1", roles=[ROLE_VERIFIER],
                     skills=[{"name": "browser-qa", "confidence": 0.9}],
                     runtime_tools=["chrome-devtools"])
    worker = registry.get("local", "s1")

    assert "python" in worker.detected_capabilities        # probed on the node
    assert "chrome-devtools" in worker.declared_capabilities  # asserted by an operator
    assert "browser-qa" in worker.declared_capabilities       # a declared skill
    assert "python" not in worker.declared_capabilities
    assert set(worker.capabilities) >= {"python", "chrome-devtools", "browser-qa"}


def test_can_may_require_probed_capability_only(rig):
    registry, _, _ = rig
    registry.declare("local", "s1", runtime_tools=["dotnet"])
    worker = registry.get("local", "s1")
    assert worker.can(["dotnet"]) is True
    assert worker.can(["dotnet"], trust_declared=False) is False, \
        "a merely-declared tool must not satisfy a trusted requirement"
    assert worker.can(["python"], trust_declared=False) is True


def test_capability_matching_is_AND(rig):
    registry, _, _ = rig
    registry.declare("local", "s1")
    worker = registry.get("local", "s1")
    assert worker.can(["python", "playwright"]) is True
    assert worker.can(["python", "dotnet"]) is False


def test_platform_is_a_routable_capability(rig):
    """dell-5530 reports an EMPTY probed list, so without platform it would
    be unreachable by every capability query."""
    registry, _, queue = rig
    registry.declare("dell-5530", "win1", roles=[ROLE_VERIFIER])
    worker = registry.get("dell-5530", "win1")
    assert "windows" in worker.detected_capabilities
    assert worker.can(["windows"]) is True
    matched = registry.list_workers(role=ROLE_VERIFIER, required_capabilities=["windows"])
    assert [w.key for w in matched] == ["dell-5530/win1"]


# -- 3. Staleness is reported, never guessed ----------------------------

def test_capability_age_is_surfaced_rather_than_assumed_fresh(tmp_path):
    """Node capabilities carry no verified_at column, so heartbeat age is
    the only honest freshness signal. An online node with a three-month-old
    probe must not look identical to one probed 20 seconds ago."""
    pm = PMStore(tmp_path / "pm.db")
    nodes = _FakeNodes([node("local", capabilities=["python"],
                             heartbeat="2020-01-01T00:00:00+00:00")])
    registry = WorkerRegistry(pm_store=pm, node_registry=nodes)
    registry.declare("local", "s1")
    worker = registry.get("local", "s1")
    assert worker.capability_age_seconds is not None
    assert worker.capability_age_seconds > 60 * 60 * 24 * 365


def test_a_missing_heartbeat_reports_unknown_age_not_zero(rig):
    registry, _, _ = rig
    registry.declare("local", "s1")
    assert registry.get("local", "s1").capability_age_seconds is None


# -- 4. Liveness ---------------------------------------------------------

def test_worker_status_reflects_node_and_task_state(rig):
    registry, _, queue = rig
    registry.declare("local", "idle-session")
    registry.declare("gone", "dead-session")
    assert registry.get("local", "idle-session").status == WORKER_IDLE
    assert registry.get("gone", "dead-session").status == WORKER_OFFLINE

    queue.store.set_tasks("busy-session", [{"title": "t", "prompt": "p"}])
    claimed = queue.store.claim_next_task("busy-session", claimed_by="w")
    busy = registry.get("local", "busy-session")
    assert busy.status == WORKER_BUSY
    assert busy.current_task_id == claimed.id


def test_offline_workers_are_excluded_by_default(rig):
    registry, _, _ = rig
    registry.declare("local", "s1")
    registry.declare("gone", "s2")
    assert {w.key for w in registry.list_workers()} == {"local/s1"}
    assert {w.key for w in registry.list_workers(online_only=False)} == {"local/s1", "gone/s2"}


def test_queue_depth_counts_this_workers_open_tasks(rig):
    registry, _, queue = rig
    registry.declare("local", "s1")
    for i in range(3):
        queue.store.set_tasks("s1", [{"title": f"t{i}", "prompt": "p"}], replace_pending=False)
    assert registry.get("local", "s1").queue_depth == 3


# -- 5. Filtering + summary ---------------------------------------------

def test_listing_filters_by_role_capability_and_project(rig):
    registry, _, _ = rig
    registry.declare("local", "impl", roles=[ROLE_WORKER], project_affinity=PROJECT)
    registry.declare("local", "verify", roles=[ROLE_VERIFIER])
    registry.declare("dell-5530", "winverify", roles=[ROLE_VERIFIER])

    assert {w.key for w in registry.list_workers(role=ROLE_VERIFIER)} == \
        {"local/verify", "dell-5530/winverify"}
    assert {w.key for w in registry.list_workers(role=ROLE_VERIFIER,
                                                 required_capabilities=["windows"])} == \
        {"dell-5530/winverify"}
    # Affinity narrows; a worker with NO affinity stays eligible everywhere.
    keys = {w.key for w in registry.list_workers(project_id=PROJECT)}
    assert "local/impl" in keys and "local/verify" in keys
    assert {w.key for w in registry.list_workers(project_id="other")} == \
        {"local/verify", "dell-5530/winverify"}


def test_roles_summary_answers_can_this_fleet_verify_anything(rig):
    registry, _, _ = rig
    assert registry.roles_summary()[ROLE_VERIFIER] == 0
    registry.declare("local", "v", roles=[ROLE_VERIFIER])
    registry.declare("local", "w", roles=[ROLE_WORKER, ROLE_VERIFIER])
    summary = registry.roles_summary()
    assert summary[ROLE_VERIFIER] == 2 and summary[ROLE_WORKER] == 1
    assert summary[ROLE_DEPLOYER := "DEPLOYER"] == 0


# -- 6. It owns no state -------------------------------------------------

def test_the_registry_adds_no_table_and_no_store(tmp_path):
    """Everything a worker is made of is already persisted in three places
    that each own their piece. A `workers` table would duplicate all three
    and immediately start drifting."""
    import pathlib
    source = (pathlib.Path(__file__).parent.parent / "terminal_mcp" / "worker_registry.py").read_text()
    assert "CREATE TABLE" not in source
    assert "Migration(" not in source
    assert "sqlite3" not in source


def test_it_degrades_rather_than_fails_without_dependencies():
    empty = WorkerRegistry()
    assert empty.list_workers() == []
    assert empty.declare("n", "s")["error"] == "PM_STORE_UNAVAILABLE"
    assert empty.get("n", "s") is None
