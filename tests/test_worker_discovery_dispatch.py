"""Phase 2, part 2: watch coverage, stalled-state visibility, and
work-conserving dispatch (`terminal_mcp/worker_discovery.py`).

The fakes here model the two behaviours that actually matter and that a mock
would hide: `upsert_watch` re-enables and re-pins an existing watch, and
`route_task(mode="AUTO")` assigns through the queue rather than sending.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from terminal_mcp.pm_router import WorkerCandidate
from terminal_mcp.pm_store import PMStore
from terminal_mcp.worker_discovery import (
    WATCH_CREATED,
    WATCH_EXISTS,
    WATCH_REFUSED,
    WorkerDiscoveryPolicy,
    WorkerDiscoveryService,
    classify_watch_states,
)

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
ON = WorkerDiscoveryPolicy(enabled=True)


def session_row(**overrides):
    row = {
        "node_id": "local", "session_name": "worker-a", "agent_type": "claude",
        "status": "ACTIVE", "input_granted": 1, "read_granted": 1,
        "last_seen_at": (NOW - timedelta(seconds=30)).isoformat(),
        "repo_root": "/repo/widget", "backend_type": "tmux", "recovery_state": None,
    }
    row.update(overrides)
    return row


class FakeRegistry:
    def __init__(self, records):
        self._records = records

    def list(self, *, statuses=None, **_kwargs):
        if statuses is None:
            return list(self._records)
        return [r for r in self._records if r.get("status") in statuses]


class FakeSupervisor:
    """Models the real `upsert_watch` contract: creating is fine, but a repeat
    upsert of an EXISTING watch re-enables it, re-pins identity and mints a new
    completion nonce. Those three effects are what make blind re-watching
    unsafe, so the fake counts them."""

    def __init__(self, watches=(), denied=()):
        self.watches = {w["target"]: dict(w) for w in watches}
        self.denied = set(denied)
        self.repins = 0
        self.nonce_mints = 0
        self.watch_calls = []

    def list_watches(self):
        return {"watches": [dict(w) for w in self.watches.values()]}

    def watch(self, session=None, binding=None, required_verifiers=None, source="manual"):
        self.watch_calls.append((session, source))
        if session in self.denied:
            return {"error": "ACCESS_DENIED", "session": session}
        if session in self.watches:
            self.repins += 1          # re-pin identity
            self.nonce_mints += 1     # invalidate the outstanding nonce
            self.watches[session].update(enabled=True, disabled_reason=None)
            return {"target": session, "created": False}
        self.watches[session] = {"target": session, "kind": "session", "enabled": True,
                                 "state": "UNKNOWN", "source": source}
        self.nonce_mints += 1
        return {"target": session, "created": True}


class FakePMService:
    def __init__(self, candidates, store=None):
        self._candidates_list = list(candidates)
        self.routed = []
        self.store = store

    def _candidates(self):
        return list(self._candidates_list)

    def route_task(self, task_id, *, mode="SUGGEST"):
        self.routed.append((task_id, mode))
        return {"task_id": task_id, "decision": {"status": "ROUTED"},
                "assign_result": {"ok": True, "session": "worker-a"}}


@pytest.fixture
def store(tmp_path):
    return PMStore(tmp_path / "pm.db")


def worker(session, **kwargs):
    kwargs.setdefault("skills", ({"name": "claude", "confidence": 1.0},))
    return WorkerCandidate(node_id="local", session=session, **kwargs)


# -- watch coverage (requirement 1) -------------------------------------------

def test_an_eligible_worker_with_no_watch_gets_one(store):
    supervisor = FakeSupervisor()
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), store,
                                     supervisor=supervisor, policy=ON)
    report = service.reconcile(now=NOW)
    assert report["watches"] == {WATCH_CREATED: ["worker-a"]}
    assert supervisor.watch_calls == [("worker-a", "auto-discovery")]


def test_an_existing_watch_is_never_re_upserted(store):
    """The important one. Re-watching on every tick would re-enable disabled
    watches, re-pin identity, and invalidate the outstanding completion nonce
    -- silently breaking submission/completion confirmation."""
    supervisor = FakeSupervisor([{"target": "worker-a", "kind": "session",
                                  "enabled": True, "state": "IDLE"}])
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), store,
                                     supervisor=supervisor, policy=ON)
    for _ in range(5):
        report = service.reconcile(now=NOW)

    assert report["watches"] == {WATCH_EXISTS: ["worker-a"]}
    assert supervisor.watch_calls == []   # never touched
    assert supervisor.repins == 0
    assert supervisor.nonce_mints == 0


def test_a_disabled_watch_is_left_disabled(store):
    """A watch an operator or phase-1 recovery disabled stays disabled --
    discovery must not fight the lane that owns it."""
    supervisor = FakeSupervisor([{"target": "worker-a", "kind": "session",
                                  "enabled": False, "state": "ERROR",
                                  "disabled_reason": "operator"}])
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), store,
                                     supervisor=supervisor, policy=ON)
    service.reconcile(now=NOW)
    assert supervisor.watches["worker-a"]["enabled"] is False
    assert supervisor.watch_calls == []


def test_a_watch_refused_by_access_control_is_recorded_not_retried_blindly(store):
    supervisor = FakeSupervisor(denied={"worker-a"})
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), store,
                                     supervisor=supervisor, policy=ON)
    report = service.reconcile(now=NOW)
    assert report["watches"] == {WATCH_REFUSED: ["worker-a"]}


def test_watch_creation_is_idempotent_across_a_supervisor_restart(store):
    """Restart = a fresh service object reading the same persisted watches.
    The second run must create nothing."""
    supervisor = FakeSupervisor()
    registry = FakeRegistry([session_row(), session_row(session_name="worker-b")])

    first = WorkerDiscoveryService(registry, store, supervisor=supervisor, policy=ON)
    first_report = first.reconcile(now=NOW)

    restarted = WorkerDiscoveryService(registry, store, supervisor=supervisor, policy=ON)
    second_report = restarted.reconcile(now=NOW)

    assert sorted(first_report["watches"][WATCH_CREATED]) == ["worker-a", "worker-b"]
    assert WATCH_CREATED not in second_report["watches"]
    assert sorted(second_report["watches"][WATCH_EXISTS]) == ["worker-a", "worker-b"]
    assert len(supervisor.watches) == 2
    assert supervisor.repins == 0


def test_two_sessions_declared_in_one_pass_get_one_watch_each(store):
    supervisor = FakeSupervisor()
    registry = FakeRegistry([session_row(), session_row(session_name="worker-b")])
    service = WorkerDiscoveryService(registry, store, supervisor=supervisor, policy=ON)
    service.reconcile(now=NOW)
    assert sorted(supervisor.watches) == ["worker-a", "worker-b"]


# -- stalled states become actionable (requirement 6) -------------------------

def test_waiting_input_is_actionable_not_counted_as_free():
    states = classify_watch_states([
        {"target": "w1", "state": "WAITING_INPUT", "enabled": True, "state_since": "2026-09-14T11:00:00+00:00"},
        {"target": "w2", "state": "IDLE", "enabled": True},
        {"target": "w3", "state": "RUNNING", "enabled": True},
    ])
    assert states["free"] == ["w2"]
    assert states["busy"] == ["w3"]
    assert states["stalled_count"] == 1
    stalled = states["stalled"][0]
    assert stalled["target"] == "w1"
    assert stalled["state"] == "WAITING_INPUT"
    assert "cannot self-advance" in stalled["action"]
    assert stalled["since"] == "2026-09-14T11:00:00+00:00"


@pytest.mark.parametrize("state", ["ERROR", "BLOCKED", "FAILED"])
def test_every_stalled_state_carries_an_action(state):
    states = classify_watch_states([{"target": "w1", "state": state, "enabled": True}])
    assert states["stalled_count"] == 1
    assert states["stalled"][0]["action"]


def test_a_disabled_watch_is_not_counted_as_capacity_or_as_stalled():
    states = classify_watch_states([{"target": "w1", "state": "IDLE", "enabled": False}])
    assert states == {"free": [], "busy": [], "stalled": [], "stalled_count": 0}


# -- work-conserving dispatch (requirement 2) ---------------------------------

def test_ready_tasks_and_idle_workers_are_dispatched_without_intervention(store):
    supervisor = FakeSupervisor()
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), store,
                                     supervisor=supervisor, policy=ON)
    pm = FakePMService([worker("worker-a"), worker("worker-b")])
    tasks = [{"id": "t1", "status": "UNASSIGNED"}, {"id": "t2", "status": "UNASSIGNED"}]

    result = service.dispatch(pm_service=pm, tasks=tasks, apply=True, now=NOW)

    assert [t for t, _ in pm.routed] == ["t1", "t2"]
    assert all(mode == "AUTO" for _, mode in pm.routed)
    assert result["coverage"]["ready_idle_mismatch"] is False
    assert len(result["assigned"]) == 2


def test_dispatch_dry_run_changes_nothing(store):
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), store,
                                     supervisor=FakeSupervisor(), policy=ON)
    pm = FakePMService([worker("worker-a")])
    result = service.dispatch(pm_service=pm, tasks=[{"id": "t1", "status": "UNASSIGNED"}], now=NOW)
    assert pm.routed == []
    assert result["applied"] is False
    assert len(result["coverage"]["assignable"]) == 1


def test_dispatch_respects_reserve_capacity(store):
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), store,
                                     supervisor=FakeSupervisor(), policy=ON)
    pm = FakePMService([worker("worker-a")])
    result = service.dispatch(pm_service=pm, tasks=[{"id": "t1", "status": "UNASSIGNED"}],
                              reserve_workers=1, apply=True, now=NOW)
    assert pm.routed == []
    assert result["coverage"]["skipped_counts"] == {"RESERVED_CAPACITY": 1}


def test_dispatch_surfaces_stalled_workers_alongside_coverage(store):
    supervisor = FakeSupervisor([{"target": "worker-a", "kind": "session",
                                  "enabled": True, "state": "WAITING_INPUT"}])
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), store,
                                     supervisor=supervisor, policy=ON)
    pm = FakePMService([worker("worker-a")])
    result = service.dispatch(pm_service=pm, tasks=[], now=NOW)
    assert result["attention_required"][0]["target"] == "worker-a"
    assert result["attention_required"][0]["state"] == "WAITING_INPUT"


def test_a_second_dispatch_pass_does_not_reassign_an_already_assigned_task(store):
    """Duplicate-dispatch guard: an assigned task is no longer READY, so it is
    simply not in the second pass's input. Nothing here re-claims it."""
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), store,
                                     supervisor=FakeSupervisor(), policy=ON)
    pm = FakePMService([worker("worker-a")])
    tasks = [{"id": "t1", "status": "UNASSIGNED"}]

    service.dispatch(pm_service=pm, tasks=tasks, apply=True, now=NOW)
    service.dispatch(pm_service=pm, tasks=[], apply=True, now=NOW)  # t1 no longer READY

    assert [t for t, _ in pm.routed] == ["t1"]


def test_dispatch_never_sends_anything(store):
    """The guarded send path is untouched: dispatch assigns, and the keystroke
    still belongs to queue_engine._dispatch."""
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), store,
                                     supervisor=FakeSupervisor(), policy=ON)
    pm = FakePMService([worker("worker-a")])
    assert not hasattr(service, "send")
    result = service.dispatch(pm_service=pm, tasks=[{"id": "t1", "status": "UNASSIGNED"}],
                              apply=True, now=NOW)
    # the only outbound effect is an assignment recorded through the queue
    assert result["assigned"][0]["assign_result"] == {"ok": True, "session": "worker-a"}
