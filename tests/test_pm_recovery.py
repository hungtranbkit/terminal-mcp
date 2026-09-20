"""The PM recovers work whose RUNTIME died, and never anything else.

The failure: a task bound to a session on a node that goes offline reads as
"already placed" to both of the existing sweeps. `routable_tasks` skips it
(routing_state is BOUND, so by definition it is not waiting for a runtime) and
`bound_unstarted_tasks` re-drives a lane that is no longer there. So it stops,
looking perfectly assigned, and a project silently stalls.

The two halves that matter:

  * durable ownership SURVIVES -- project, agent, pinned skills, prompt,
    priority, evidence. Only the runtime binding is released.
  * live work is NEVER moved on a timer. A model mid-turn looks exactly like
    a stall from outside, and re-dispatching it runs the same work twice.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from terminal_mcp.pm_recovery import (PM_HELD, PM_REASSIGNED, PM_RELEASED, PM_SKIPPED,
                                      ProjectPMRecovery)
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import BOUND, QueueStore


@dataclass
class _Node:
    id: str
    status: str = "online"
    display_name: str = "n"


class _Fleet:
    """A controller that answers only what the PM asks it."""

    def __init__(self, *, nodes, sessions, unreachable=()):
        self._nodes = nodes
        self._sessions = sessions
        self._unreachable = list(unreachable)

    def list_nodes(self):
        return list(self._nodes)

    def terminal_list_sessions(self):
        return {"sessions": [dict(row) for row in self._sessions],
                "unreachable_nodes": list(self._unreachable)}


class _Router:
    def __init__(self, *, session=None, node_id=None, reason="re-routed"):
        self.routed: list[str] = []
        self._session = session
        self._node_id = node_id
        self._reason = reason

    def route_task(self, task_id):
        self.routed.append(task_id)

        class _Outcome:
            outcome = "ROUTED" if self._session else "DEFERRED"
            session = self._session
            node_id = self._node_id
            reason = self._reason
        return _Outcome()


def _bound_task(store, *, session, node_id, status=None, project="proj",
                agent_id="proj-core", skills=("core-engineering@1",)):
    created = QueueService(store).create_task("title", "do the work", session=session,
                                              project=project)
    task_id = created["task_id"]
    store.set_agent_binding(task_id, agent_id=agent_id, skill_ids=list(skills))
    store.set_task_project(task_id, project)
    store.bind_task_to_session(task_id, session, node_id=node_id, routing_state=BOUND,
                               evidence={"reason": "test"},
                               agent_id=agent_id, skill_ids=list(skills))
    if status:
        store.transition_task(task_id, status, event_type="TEST")
    return task_id


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


# ---------------------------------------------------------------------------
# What counts as a stalled runtime.
# ---------------------------------------------------------------------------

def test_a_task_on_an_offline_node_is_recovered_and_re_routed(store):
    task_id = _bound_task(store, session="dead-session", node_id="gone")
    fleet = _Fleet(nodes=[_Node(id="gone", status="offline"), _Node(id="live")],
                   sessions=[{"name": "healthy", "node_id": "live"}])
    router = _Router(session="healthy", node_id="live")

    result = ProjectPMRecovery(store, router=router, controller=fleet).sweep()

    assert result["recovered"] == 1
    row = result["results"][0]
    assert row["decision"] == PM_REASSIGNED
    assert row["released_session"] == "dead-session"
    assert row["new_session"] == "healthy"
    assert "offline" in row["reason"]
    assert router.routed == [task_id]


def test_a_session_that_vanished_from_the_fleet_is_recovered(store):
    task_id = _bound_task(store, session="ghost", node_id="live")
    fleet = _Fleet(nodes=[_Node(id="live")],
                   sessions=[{"name": "somebody-else", "node_id": "live"}])

    result = ProjectPMRecovery(store, router=_Router(session="somebody-else", node_id="live"),
                               controller=fleet).sweep()
    assert result["recovered"] == 1
    assert "not in the fleet listing" in result["results"][0]["reason"]


def test_a_healthy_binding_is_left_completely_alone(store):
    _bound_task(store, session="worker", node_id="live")
    fleet = _Fleet(nodes=[_Node(id="live")], sessions=[{"name": "worker", "node_id": "live"}])
    router = _Router(session="other")

    result = ProjectPMRecovery(store, router=router, controller=fleet).sweep()

    assert result["recovered"] == 0 and result["healthy"] == 1
    assert router.routed == []


def test_running_work_is_never_moved_on_a_timer(store):
    """A long agent turn is indistinguishable from a stall from out here.
    Re-dispatching it would run the same work twice, on two sessions."""
    _bound_task(store, session="worker", node_id="live", status="PRECHECK")
    store.transition_task(store.lane_status("worker")["tasks"][0]["id"], "READY",
                          event_type="TEST")
    task_id = store.lane_status("worker")["tasks"][0]["id"]
    store.transition_task(task_id, "DISPATCHING", event_type="TEST")
    store.transition_task(task_id, "RUNNING", event_type="TEST")

    fleet = _Fleet(nodes=[_Node(id="live")], sessions=[{"name": "worker", "node_id": "live"}])
    recovery = ProjectPMRecovery(store, router=_Router(session="x"), controller=fleet,
                                 clock=lambda: time.time() + 99_999,
                                 unstarted_stall_seconds=1.0, waiting_stall_seconds=1.0)

    result = recovery.sweep()
    assert result["recovered"] == 0, "a RUNNING task was moved off its session"


def test_a_task_bound_but_unstarted_past_policy_is_recovered(store):
    _bound_task(store, session="worker", node_id="live")
    fleet = _Fleet(nodes=[_Node(id="live")], sessions=[{"name": "worker", "node_id": "live"}])
    recovery = ProjectPMRecovery(store, router=_Router(session="worker", node_id="live"),
                                 controller=fleet,
                                 clock=lambda: time.time() + 10_000,
                                 unstarted_stall_seconds=300.0)

    result = recovery.sweep()
    assert result["recovered"] == 1
    assert "without starting" in result["results"][0]["reason"]


def test_a_fleet_we_could_not_read_is_never_absence_evidence(store):
    """The single most dangerous mistake available here is treating an
    unreachable node's session list as empty."""
    _bound_task(store, session="worker", node_id="live")
    fleet = _Fleet(nodes=[_Node(id="live")], sessions=[],
                   unreachable=[{"node_id": "live", "status": "timeout"}])
    router = _Router(session="elsewhere")

    result = ProjectPMRecovery(store, router=router, controller=fleet).sweep()
    assert result["fleet_evidence_usable"] is False
    assert result["recovered"] == 0 and router.routed == []


# ---------------------------------------------------------------------------
# What must survive a recovery.
# ---------------------------------------------------------------------------

def test_durable_ownership_survives_the_recovery(store):
    task_id = _bound_task(store, session="dead", node_id="gone",
                          project="mapping-app", agent_id="mapping-app-ui",
                          skills=("frontend-ui@1", "core-engineering@2"))
    fleet = _Fleet(nodes=[_Node(id="gone", status="offline"), _Node(id="live")],
                   sessions=[{"name": "fresh", "node_id": "live"}])

    ProjectPMRecovery(store, router=_Router(session="fresh", node_id="live"),
                      controller=fleet).sweep()

    task = store.get_task(task_id)
    assert task.project_id == "mapping-app"
    assert task.agent_id == "mapping-app-ui"
    assert list(task.skill_ids) == ["frontend-ui@1", "core-engineering@2"]
    assert task.prompt == "do the work"


def test_every_decision_lands_in_the_durable_handoff_history(store):
    task_id = _bound_task(store, session="dead", node_id="gone")
    fleet = _Fleet(nodes=[_Node(id="gone", status="offline"), _Node(id="live")],
                   sessions=[{"name": "fresh", "node_id": "live"}])

    ProjectPMRecovery(store, router=_Router(session="fresh", node_id="live"),
                      controller=fleet).sweep()

    history = [row for row in store.get_task(task_id).migration_history
               if row.get("kind") == QueueStore.PM_MIGRATION_KIND]
    assert len(history) == 2, "the release and the re-route must both be recorded"
    assert history[0]["decision"] == PM_RELEASED
    assert history[0]["from"] == "dead"
    assert history[1]["decision"] == PM_REASSIGNED
    assert history[1]["evidence"]["new_session"] == "fresh"
    assert store.assignment_history(task_id)["migration_history"]


def test_a_binding_that_changed_since_the_scan_is_skipped_not_stolen(store):
    """Between the scan and the decision another router may legitimately have
    moved this task. Releasing then would take a HEALTHY runtime away."""
    task_id = _bound_task(store, session="dead", node_id="gone")
    fleet = _Fleet(nodes=[_Node(id="gone", status="offline"), _Node(id="live")],
                   sessions=[{"name": "fresh", "node_id": "live"}])
    recovery = ProjectPMRecovery(store, router=_Router(session="fresh"), controller=fleet)

    scanned = store.get_task(task_id)
    store.release_execution_binding(task_id, reason="somebody else got there first")
    store.bind_task_to_session(task_id, "fresh", node_id="live", routing_state=BOUND,
                               evidence={"reason": "other router"})

    row = recovery._recover_one(scanned, reason="node offline", actor="pm", dry_run=False)
    assert row["decision"] == PM_SKIPPED
    assert "another router" in row["detail"]


def test_dry_run_changes_nothing(store):
    task_id = _bound_task(store, session="dead", node_id="gone")
    fleet = _Fleet(nodes=[_Node(id="gone", status="offline")], sessions=[])
    router = _Router(session="x")

    result = ProjectPMRecovery(store, router=router, controller=fleet).sweep(dry_run=True)

    assert result["dry_run"] is True
    assert result["results"][0]["decision"] == PM_HELD
    assert result["results"][0]["would"] == PM_REASSIGNED
    assert router.routed == []
    assert store.get_task(task_id).execution_session == "dead"


def test_a_sweep_can_be_scoped_to_one_project(store):
    _bound_task(store, session="a", node_id="gone", project="alpha")
    _bound_task(store, session="b", node_id="gone", project="beta")
    fleet = _Fleet(nodes=[_Node(id="gone", status="offline")], sessions=[])

    result = ProjectPMRecovery(store, router=_Router(), controller=fleet).sweep(project_id="alpha")
    assert {row["project_id"] for row in result["results"]} == {"alpha"}


def test_with_no_router_the_task_is_merely_made_routable_again(store):
    task_id = _bound_task(store, session="dead", node_id="gone")
    fleet = _Fleet(nodes=[_Node(id="gone", status="offline")], sessions=[])

    result = ProjectPMRecovery(store, router=None, controller=fleet).sweep()

    assert result["results"][0]["decision"] == PM_RELEASED
    task = store.get_task(task_id)
    assert task.execution_session is None
    assert task.routing_state not in ("BOUND", "SPAWNED")


def test_a_task_queued_behind_live_work_is_not_called_stalled(store):
    """A lane is serial. The second task in it is waiting for the one ahead,
    not for a runtime -- moving it is thrash dressed as recovery."""
    queue = QueueService(store)
    first = queue.create_task("running", "the work ahead", session="worker")["task_id"]
    store.bind_task_to_session(first, "worker", node_id="live", routing_state=BOUND,
                               evidence={"reason": "test"})
    for status in ("PRECHECK", "READY", "DISPATCHING", "RUNNING"):
        store.transition_task(first, status, event_type="TEST")
    _bound_task(store, session="worker", node_id="live")

    fleet = _Fleet(nodes=[_Node(id="live")], sessions=[{"name": "worker", "node_id": "live"}])
    router = _Router(session="elsewhere")
    recovery = ProjectPMRecovery(store, router=router, controller=fleet,
                                 clock=lambda: time.time() + 10_000,
                                 unstarted_stall_seconds=300.0)

    result = recovery.sweep()
    assert result["recovered"] == 0, result["results"]
    assert router.routed == []
