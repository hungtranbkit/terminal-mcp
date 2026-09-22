"""Phase 2 worker discovery + work-conserving dispatch coverage
(`terminal_mcp/worker_discovery.py`, backlog `blg_orch_no_workers_declared`).

Same posture as test_pm_store.py: tmp_path-scoped db, disposable fixture names,
no real tmux/node/controller anywhere. The decision logic is a pure function, so
every eligibility rule is asserted directly rather than through a service.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from terminal_mcp.pm_router import WorkerCandidate
from terminal_mcp.pm_store import SOURCE_AUTO, SOURCE_DECLARED, PMStore
from terminal_mcp.worker_discovery import (
    SKIP_AGENT_TYPE_NOT_ELIGIBLE,
    SKIP_ALL_WORKERS_AT_WIP,
    SKIP_DISCOVERY_DISABLED,
    SKIP_MISSING_IDENTITY,
    SKIP_NODE_OFFLINE,
    SKIP_NOT_ACTIVE,
    SKIP_NO_ELIGIBLE_WORKER,
    SKIP_NO_INPUT_GRANT,
    SKIP_NO_READ_GRANT,
    SKIP_RECOVERY_IN_PROGRESS,
    SKIP_RESERVED_CAPACITY,
    SKIP_STALE_LAST_SEEN,
    SKIP_TASK_BLOCKED,
    SKIP_TASK_HAS_DEPENDENCY,
    WorkerDiscoveryPolicy,
    WorkerDiscoveryService,
    coverage,
    evaluate_session,
)

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
ON = WorkerDiscoveryPolicy(enabled=True)


def session_row(**overrides):
    """A session the registry would call eligible: ACTIVE, claude, both grants,
    seen seconds ago. Every test below changes exactly one thing about it."""
    row = {
        "node_id": "local", "session_name": "worker-a", "agent_type": "claude",
        "status": "ACTIVE", "input_granted": 1, "read_granted": 1,
        "last_seen_at": (NOW - timedelta(seconds=30)).isoformat(),
        "repo_root": "/repo/widget", "backend_type": "tmux", "recovery_state": None,
    }
    row.update(overrides)
    return row


@pytest.fixture
def store(tmp_path):
    return PMStore(tmp_path / "pm.db")


class FakeRegistry:
    def __init__(self, records):
        self._records = records
        self.list_calls = 0

    def list(self, *, statuses=None, **_kwargs):
        self.list_calls += 1
        if statuses is None:
            return list(self._records)
        return [r for r in self._records if r.get("status") in statuses]


class FakeNode:
    def __init__(self, node_id, status="online", platform="linux", runtime_tools=()):
        self.id = node_id
        self.status = status
        self.platform = platform
        self.runtime_tools = runtime_tools


class FakeController:
    def __init__(self, nodes):
        self._nodes = nodes

    def list_nodes(self):
        return list(self._nodes)


# -- the gap this feature exists to close -------------------------------------

def test_a_live_session_with_no_profile_is_discovered_and_declared(store):
    """The whole bug: a worker that was never declared is invisible to routing.
    Reconciliation cannot help it, because there is nothing to reconcile."""
    assert store.list_capabilities() == []  # the measured production state

    service = WorkerDiscoveryService(FakeRegistry([session_row()]), store, policy=ON)
    report = service.reconcile(now=NOW)

    assert report["created"] == ["local/worker-a"]
    profiles = store.list_capabilities()
    assert [p.key() for p in profiles] == ["local/worker-a"]
    assert profiles[0].source == SOURCE_AUTO
    assert "claude" in {s["name"] for s in profiles[0].skills}


def test_discovery_is_off_by_default_and_writes_nothing(store):
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), store)
    report = service.reconcile(now=NOW)
    assert report["created"] == []
    assert report["skipped_counts"] == {SKIP_DISCOVERY_DISABLED: 1}
    assert store.list_capabilities() == []


# -- who must NOT be declared (goal 5) ----------------------------------------

@pytest.mark.parametrize("overrides,expected", [
    ({"status": "MISSING"}, SKIP_NOT_ACTIVE),
    ({"status": "KILLED"}, SKIP_NOT_ACTIVE),
    ({"input_granted": 0}, SKIP_NO_INPUT_GRANT),
    ({"read_granted": 0}, SKIP_NO_READ_GRANT),
    ({"agent_type": "shell"}, SKIP_AGENT_TYPE_NOT_ELIGIBLE),
    ({"agent_type": None}, SKIP_AGENT_TYPE_NOT_ELIGIBLE),
    ({"node_id": None}, SKIP_MISSING_IDENTITY),
    ({"session_name": ""}, SKIP_MISSING_IDENTITY),
    ({"recovery_state": "RECOVERING"}, SKIP_RECOVERY_IN_PROGRESS),
])
def test_ineligible_sessions_are_refused_with_a_named_reason(overrides, expected):
    decision = evaluate_session(session_row(**overrides), now=NOW, policy=ON)
    assert decision.declare is False
    assert decision.reason == expected


def test_a_stale_active_row_is_not_trusted_as_live():
    """A crashed poller leaves ACTIVE rows behind; age is the real signal."""
    old = session_row(last_seen_at=(NOW - timedelta(hours=3)).isoformat())
    assert evaluate_session(old, now=NOW, policy=ON).reason == SKIP_STALE_LAST_SEEN


def test_an_unparseable_last_seen_counts_as_stale_not_fresh():
    decision = evaluate_session(session_row(last_seen_at="not-a-timestamp"), now=NOW, policy=ON)
    assert decision.reason == SKIP_STALE_LAST_SEEN


def test_an_offline_node_is_never_declared():
    decision = evaluate_session(session_row(), node_online=False, now=NOW, policy=ON)
    assert decision.reason == SKIP_NODE_OFFLINE


def test_offline_node_is_skipped_through_the_real_service(store):
    service = WorkerDiscoveryService(
        FakeRegistry([session_row()]), store,
        controller=FakeController([FakeNode("local", status="offline")]), policy=ON)
    report = service.reconcile(now=NOW)
    assert report["created"] == []
    assert report["skipped_counts"] == {SKIP_NODE_OFFLINE: 1}
    assert store.list_capabilities() == []


# -- never overwrite a human (goal 3, goal 8) ---------------------------------

def test_discovery_never_overwrites_a_declared_profile(store):
    store.upsert_capability("local", "worker-a", role="VERIFIER",
                            skills=[{"name": "playwright", "confidence": 0.9}])
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), store, policy=ON)
    report = service.reconcile(now=NOW)

    assert report["skipped_declared"] == ["local/worker-a"]
    kept = store.get_capability("local", "worker-a")
    assert kept.role == "VERIFIER"                                  # not reset to WORKER
    assert {s["name"] for s in kept.skills} == {"playwright"}        # not replaced
    assert kept.source == SOURCE_DECLARED


def test_a_pre_migration_row_with_null_source_is_treated_as_declared(store):
    """Unprovable origin is treated as a human's -- the safe default."""
    store.upsert_capability("local", "worker-a", role="curated")
    with store._connection() as connection:  # simulate a row written before migration 2
        connection.execute("UPDATE capability_profiles SET source = NULL")
    outcome, profile = store.upsert_discovered_capability("local", "worker-a", role="WORKER")
    assert outcome == store.DISCOVERY_SKIPPED_DECLARED
    assert profile.role == "curated"


# -- idempotency / retry / concurrency (goal 3) -------------------------------

def test_reconcile_is_idempotent_across_repeated_passes(store):
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), store, policy=ON)
    first = service.reconcile(now=NOW)
    second = service.reconcile(now=NOW)
    third = service.reconcile(now=NOW)

    assert first["created"] == ["local/worker-a"]
    assert second["created"] == [] and second["updated"] == ["local/worker-a"]
    assert third["updated"] == ["local/worker-a"]
    assert len(store.list_capabilities()) == 1  # never duplicated


def test_concurrent_reconcilers_converge_on_one_profile(store):
    """Two services racing over the same registry must not produce two rows or
    fight over ownership -- the write re-reads the stored row inside itself."""
    registry = FakeRegistry([session_row(), session_row(session_name="worker-b")])
    a = WorkerDiscoveryService(registry, store, policy=ON)
    b = WorkerDiscoveryService(registry, store, policy=ON)

    for _ in range(3):  # interleave the passes
        a.reconcile(now=NOW)
        b.reconcile(now=NOW)

    keys = sorted(p.key() for p in store.list_capabilities())
    assert keys == ["local/worker-a", "local/worker-b"]
    assert all(p.source == SOURCE_AUTO for p in store.list_capabilities())


def test_reconcile_only_walks_active_rows(store):
    """Bounded: the registry holds 304 rows on this host and 5 are ACTIVE."""
    registry = FakeRegistry([session_row()] + [
        session_row(session_name=f"dead-{i}", status="MISSING") for i in range(50)])
    service = WorkerDiscoveryService(registry, store, policy=ON)
    report = service.reconcile(now=NOW)
    assert report["considered"] == 1
    assert report["created"] == ["local/worker-a"]


# -- work-conserving dispatch coverage (goals 2, 4, 7) ------------------------

def worker(session, **kwargs):
    kwargs.setdefault("skills", ({"name": "claude", "confidence": 1.0},))
    return WorkerCandidate(node_id="local", session=session, **kwargs)


def test_multiple_idle_workers_and_multiple_ready_tasks_are_all_assignable():
    tasks = [{"id": f"t{i}", "status": "UNASSIGNED"} for i in range(3)]
    workers = [worker(f"w{i}") for i in range(3)]
    result = coverage(tasks, workers)
    assert len(result["assignable"]) == 3
    assert result["ready_idle_mismatch"] is False
    assert result["skipped"] == {}


def test_capability_mismatch_is_reported_not_silently_idle():
    tasks = [{"id": "t1", "status": "UNASSIGNED",
              "metadata": {"required_capabilities": ["wpf"]}}]
    result = coverage(tasks, [worker("w1")])
    assert result["assignable"] == []
    assert result["skipped"] == {SKIP_NO_ELIGIBLE_WORKER: ["t1"]}
    # idle capacity exists, but for a STATED reason -- not a mismatch alarm
    assert result["idle_workers"] == 1
    assert result["ready_idle_mismatch"] is False


def test_capability_match_still_routes_to_the_capable_worker():
    tasks = [{"id": "t1", "status": "UNASSIGNED",
              "metadata": {"required_capabilities": ["wpf"]}}]
    capable = worker("w-win", skills=({"name": "wpf", "confidence": 1.0},))
    result = coverage(tasks, [worker("w-linux"), capable])
    assert result["assignable"] == [{"task_id": "t1", "candidates": ["local/w-win"]}]


def test_offline_worker_is_not_counted_as_capacity():
    result = coverage([{"id": "t1", "status": "UNASSIGNED"}], [worker("w1", online=False)])
    assert result["idle_workers"] == 0
    assert result["skipped"] == {SKIP_NO_ELIGIBLE_WORKER: ["t1"]}


def test_wip_limited_worker_is_reported_separately_from_a_capability_mismatch():
    busy = worker("w1", queue_depth=1, max_queued=1)
    result = coverage([{"id": "t1", "status": "UNASSIGNED"}], [busy])
    assert result["skipped"] == {SKIP_ALL_WORKERS_AT_WIP: ["t1"]}
    assert result["idle_workers"] == 0


def test_reserved_capacity_is_a_legitimate_reason_to_stay_idle():
    tasks = [{"id": "t1", "status": "UNASSIGNED"}]
    result = coverage(tasks, [worker("w1")], reserve_workers=1)
    assert result["assignable"] == []
    assert result["available_workers"] == 0
    assert result["skipped"] == {SKIP_RESERVED_CAPACITY: ["t1"]}
    assert result["ready_idle_mismatch"] is False


def test_dependencies_and_blockers_are_distinguished():
    tasks = [
        {"id": "dep", "status": "UNASSIGNED", "depends_on": ["other"]},
        {"id": "blk", "status": "BLOCKED"},
        {"id": "ok", "status": "UNASSIGNED"},
    ]
    result = coverage(tasks, [worker("w1")])
    assert result["skipped"][SKIP_TASK_HAS_DEPENDENCY] == ["dep"]
    assert result["skipped"][SKIP_TASK_BLOCKED] == ["blk"]
    assert [a["task_id"] for a in result["assignable"]] == ["ok"]


def test_no_ready_task_means_no_mismatch_however_idle_the_fleet_is():
    result = coverage([], [worker("w1"), worker("w2")])
    assert result["ready_tasks"] == 0
    assert result["idle_workers"] == 2
    assert result["ready_idle_mismatch"] is False


def test_each_ready_task_is_offered_once_and_never_duplicated():
    tasks = [{"id": "t1", "status": "UNASSIGNED"}, {"id": "t2", "status": "UNASSIGNED"}]
    result = coverage(tasks, [worker("w1"), worker("w2")])
    offered = [a["task_id"] for a in result["assignable"]]
    assert offered == ["t1", "t2"]
    assert len(offered) == len(set(offered))


def test_every_ready_task_lands_in_exactly_one_bucket():
    """No task may be silently dropped between assignable and skipped."""
    tasks = [
        {"id": "a", "status": "UNASSIGNED"},
        {"id": "b", "status": "UNASSIGNED", "depends_on": ["a"]},
        {"id": "c", "status": "BLOCKED"},
        {"id": "d", "status": "UNASSIGNED", "metadata": {"required_capabilities": ["wpf"]}},
    ]
    result = coverage(tasks, [worker("w1")])
    accounted = {a["task_id"] for a in result["assignable"]}
    for ids in result["skipped"].values():
        accounted |= set(ids)
    assert accounted == {"a", "b", "c", "d"}
