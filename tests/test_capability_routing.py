"""blg_orch_no_workers_declared -- capability routing across every surface
that answers "who can do this": the PM router, the worker registry, the
node scheduler and the verify queue.

Two things are pinned here. First, the DIAGNOSIS: "nothing registered",
"busy", "lacks the capability" and "nobody declared anything" are four
different answers with four different operator actions, and before this
they were one sentence. Second, BACKWARD COMPATIBILITY: every profile row
written before this change, and every caller that never passes the new
fields, must behave exactly as it did.

SAFETY: every session name below is a disposable fixture string -- never
`window`/`window2`/`wtest`.
"""
from __future__ import annotations

import pytest

from terminal_mcp.capability_profile import (
    CANDIDATES_AVAILABLE,
    CAPABILITY_UNKNOWN,
    MATCH_MISSING,
    MATCH_OK,
    MATCH_UNKNOWN,
    NO_WORKERS_ONLINE,
    NO_WORKERS_REGISTERED,
    WORKERS_BUSY,
    WORKERS_LACK_CAPABILITY,
)
from terminal_mcp.node_models import (
    CAPACITY_HEALTHY,
    CAPACITY_OVERLOADED,
    NODE_OFFLINE,
    NODE_ONLINE,
    PLATFORM_LINUX,
    Node,
)
from terminal_mcp.pm_router import (
    NO_ELIGIBLE_WORKER, ROUTED, WorkerCandidate, diagnose_candidates, hard_gate_failure, route_task,
)
from terminal_mcp.pm_service import PMService
from terminal_mcp.pm_store import PMStore
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.scheduler import choose_node
from terminal_mcp.verify_queue import diagnose_nodes
from terminal_mcp.worker_registry import (
    ROLE_VERIFIER, ROLE_WORKER, WorkerRegistry,
)


def _task(**metadata) -> dict:
    return {"id": "t1", "metadata": metadata}


def _candidate(session: str, **overrides) -> WorkerCandidate:
    base = {"node_id": "local", "session": session, "os": "linux", "online": True,
            "permissions_ok": True}
    base.update(overrides)
    return WorkerCandidate(**base)


def _node(node_id, *, status=NODE_ONLINE, capabilities=(), platform=PLATFORM_LINUX,
          agent_types=("shell",), labels=(), draining=False, capacity=CAPACITY_HEALTHY,
          sessions=0, max_sessions=None) -> Node:
    return Node(
        id=node_id, display_name=node_id, hostname=node_id, endpoint="local", status=status,
        draining=draining, last_heartbeat_at="2026-01-01T00:00:00+00:00", latency_ms=1.0,
        cpu_percent=10.0, cpu_percent_smoothed=10.0, load1=1.0, load5=1.0, load15=1.0,
        cpu_count=8, ram_total_bytes=16 * 1024 ** 3, ram_used_bytes=0, ram_percent=30.0,
        ram_percent_smoothed=30.0, swap_total_bytes=0, swap_used_bytes=0, swap_percent=0.0,
        swap_percent_smoothed=0.0, disk_total_bytes=500 * 1024 ** 3, disk_used_bytes=0,
        disk_free_bytes=100 * 1024 ** 3, disk_percent=10.0, tmux_session_count=sessions,
        agent_counts={}, agent_types=agent_types, agent_version="0.13.0", labels=labels,
        max_sessions=max_sessions, capacity_status=capacity, capabilities=tuple(capabilities),
        platform=platform,
    )


class _FakeNodes:
    def __init__(self, nodes):
        self._nodes = list(nodes)

    def list(self):
        return list(self._nodes)


# =========================================================================
# 1. PM router -- the declared-capability gate
# =========================================================================

def test_a_profile_declaring_runtime_tools_is_finally_eligible():
    """The quiet bug: hard_gate_failure matched `skills` only, so this
    candidate -- which declares playwright -- was rejected as unable to do
    playwright work."""
    task = _task(required_capabilities=["playwright"])
    candidate = _candidate("worker-a", runtime_tools=("playwright",))
    assert hard_gate_failure(task, candidate) is None


def test_a_profile_declaring_the_skill_is_still_eligible():
    """...and the path that always worked must keep working."""
    task = _task(required_capabilities=["playwright"])
    candidate = _candidate("worker-a", skills=({"name": "Playwright", "confidence": 0.9},))
    assert hard_gate_failure(task, candidate) is None


def test_a_declared_worker_without_the_capability_reports_missing():
    task = _task(required_capabilities=["wpf"])
    candidate = _candidate("worker-a", runtime_tools=("playwright",))
    failure = hard_gate_failure(task, candidate)
    assert "missing required capabilities" in failure and "wpf" in failure


def test_an_undeclared_worker_reports_capability_unknown_and_is_not_eligible():
    """Advisory, never silently capable: the session is visible, the reason
    says what to do about it, and it does not pass the gate."""
    task = _task(required_capabilities=["playwright"])
    candidate = _candidate("worker-a", has_profile=False)
    failure = hard_gate_failure(task, candidate)
    assert "capability unknown" in failure
    assert "terminal_worker_declare" in failure


def test_an_undeclared_worker_on_a_probed_node_can_still_match():
    """A probe is a measurement, so the fallback is useful rather than
    merely permissive -- and it is still not "capable of everything"."""
    task = _task(required_capabilities=["python"])
    candidate = _candidate("worker-a", has_profile=False, detected_capabilities=("python",))
    assert hard_gate_failure(task, candidate) is None
    assert "capability unknown" in hard_gate_failure(
        _task(required_capabilities=["wpf"]), candidate)


def test_an_undeclared_worker_is_not_granted_every_role():
    task = _task(required_role="verifier")
    assert "role unknown" in hard_gate_failure(task, _candidate("w", has_profile=False))


def test_a_declared_profile_with_no_role_still_passes_a_role_requirement():
    """Pre-existing behaviour, deliberately unchanged: tightening it would
    make currently-routable tasks unroutable."""
    task = _task(required_role="verifier")
    assert hard_gate_failure(task, _candidate("worker-a", role=None)) is None


def test_multi_capability_requires_all_of_them():
    task = _task(required_capabilities=["playwright", "wpf"])
    partial = _candidate("worker-a", runtime_tools=("playwright",))
    full = _candidate("worker-b", runtime_tools=("playwright",), skills=({"name": "wpf"},))
    assert "wpf" in hard_gate_failure(task, partial)
    assert hard_gate_failure(task, full) is None


# =========================================================================
# 2. PM router -- routing diagnosis
# =========================================================================

def test_route_task_with_no_candidates_says_no_workers_registered():
    decision = route_task(_task(required_capabilities=["playwright"]), [])
    assert decision.status == NO_ELIGIBLE_WORKER
    assert decision.diagnosis["code"] == NO_WORKERS_REGISTERED
    assert decision.evidence["diagnosis"]["code"] == NO_WORKERS_REGISTERED
    assert NO_WORKERS_REGISTERED in decision.reason


def test_route_task_distinguishes_lacking_from_unknown():
    task = _task(required_capabilities=["wpf"])
    lacking = route_task(task, [_candidate("worker-a", runtime_tools=("playwright",))])
    unknown = route_task(task, [_candidate("worker-b", has_profile=False)])
    assert lacking.diagnosis["code"] == WORKERS_LACK_CAPABILITY
    assert unknown.diagnosis["code"] == CAPABILITY_UNKNOWN


def test_route_task_reports_a_capable_but_busy_worker_as_capacity():
    """max_queued is the router's only hard "busy" -- queue_depth alone is
    a soft-scoring nudge, and reporting that as BUSY would be a lie."""
    task = _task(required_capabilities=["playwright"])
    busy = _candidate("worker-a", runtime_tools=("playwright",), max_queued=1, queue_depth=1)
    decision = route_task(task, [busy])
    assert decision.status == NO_ELIGIBLE_WORKER
    assert decision.diagnosis["code"] == WORKERS_BUSY
    assert decision.diagnosis["counts"]["busy"] == 1


def test_route_task_reports_every_worker_offline():
    decision = route_task(_task(), [_candidate("worker-a", online=False)])
    assert decision.diagnosis["code"] == NO_WORKERS_ONLINE


def test_a_busy_capable_worker_outranks_an_incapable_idle_one():
    """Precedence check: the fleet CAN do this work, so the honest answer
    is capacity, not a capability gap."""
    task = _task(required_capabilities=["playwright"])
    decision = route_task(task, [
        _candidate("worker-a", runtime_tools=("playwright",), max_queued=1, queue_depth=1),
        _candidate("worker-b", runtime_tools=("dotnet",)),
    ])
    assert decision.diagnosis["code"] == WORKERS_BUSY


def test_a_successful_route_still_routes_and_reports_availability():
    task = _task(required_capabilities=["playwright"])
    candidate = _candidate("worker-a", runtime_tools=("playwright",))
    decision = route_task(task, [candidate])
    assert decision.status == ROUTED and decision.chosen.session == "worker-a"
    assert diagnose_candidates(task, [candidate])["code"] == CANDIDATES_AVAILABLE


def test_diagnose_candidates_is_read_only_and_agrees_with_the_gate():
    task = _task(required_capabilities=["wpf"])
    candidates = [_candidate("worker-a", runtime_tools=("wpf",)),
                  _candidate("worker-b", has_profile=False)]
    result = diagnose_candidates(task, candidates)
    assert result["eligible"] == ["local/worker-a"]
    assert [hard_gate_failure(task, c) is None for c in candidates] == [True, False]


# =========================================================================
# 3. Old records -- backward compatibility
# =========================================================================

def test_a_candidate_built_the_old_way_is_treated_as_declared():
    """Every existing caller constructs WorkerCandidate without the new
    fields; defaulting has_profile to True is what keeps their behaviour
    byte-identical."""
    old_style = WorkerCandidate(node_id="local", session="worker-a", os="linux",
                                runtime_tools=("docker",), role="developer",
                                skills=({"name": "docker"},))
    assert old_style.has_profile is True
    assert old_style.detected_capabilities == ()
    task = _task(required_capabilities=["kubernetes"])
    assert "missing required capabilities" in hard_gate_failure(task, old_style)


def test_an_old_profile_row_with_only_skills_still_matches(tmp_path):
    """A capability_profiles row written before runtime_tools was ever
    populated: skills-only, role free text, no max_queued."""
    store = PMStore(tmp_path / "pm.db")
    store.upsert_capability("local", "legacy-a", os="linux", role="developer",
                            skills=[{"name": "docker", "confidence": 0.8}])
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    service = PMService(store, queue)
    candidates = service._candidates()
    assert len(candidates) == 1 and candidates[0].has_profile is True
    assert hard_gate_failure(_task(required_capabilities=["docker"]), candidates[0]) is None


def test_a_session_with_no_profile_at_all_keeps_its_default_worker_role(tmp_path):
    """worker_registry's own long-standing default, re-asserted here
    because the undeclared path now depends on it."""
    store = PMStore(tmp_path / "pm.db")
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    queue.store.set_tasks("legacy-session", [{"title": "t", "prompt": "p"}])
    registry = WorkerRegistry(pm_store=store, queue=queue)
    worker = registry.get("local", "legacy-session")
    assert worker.has_profile is False and worker.roles == (ROLE_WORKER,)


# =========================================================================
# 4. WorkerRegistry.diagnose
# =========================================================================

@pytest.fixture
def rig(tmp_path):
    store = PMStore(tmp_path / "pm.db")
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    nodes = _FakeNodes([_node("local", capabilities=["python"]),
                        _node("gone", capabilities=["python"], status=NODE_OFFLINE)])
    return WorkerRegistry(pm_store=store, node_registry=nodes, queue=queue), store, queue


def test_registry_diagnoses_an_empty_fleet(rig):
    registry, _, _ = rig
    assert registry.diagnose(required_capabilities=["wpf"])["code"] == NO_WORKERS_REGISTERED


def test_registry_diagnoses_a_matching_worker(rig):
    registry, _, _ = rig
    registry.declare("local", "cap-a", roles=[ROLE_VERIFIER], runtime_tools=["playwright"])
    result = registry.diagnose(required_capabilities=["playwright"], role=ROLE_VERIFIER)
    assert result["code"] == CANDIDATES_AVAILABLE
    assert result["eligible"] == ["local/cap-a"]


def test_registry_diagnoses_a_non_matching_worker(rig):
    registry, _, _ = rig
    registry.declare("local", "cap-a", runtime_tools=["playwright"])
    result = registry.diagnose(required_capabilities=["wpf"])
    assert result["code"] == WORKERS_LACK_CAPABILITY
    assert result["candidates"][0]["capability"]["verdict"] == MATCH_MISSING


def test_registry_diagnoses_an_undeclared_worker_as_unknown(rig):
    registry, _, queue = rig
    queue.store.set_tasks("cap-undeclared", [{"title": "t", "prompt": "p"}])
    result = registry.diagnose(required_capabilities=["wpf"])
    assert result["code"] == CAPABILITY_UNKNOWN
    assert result["counts"]["undeclared"] == 1
    assert result["candidates"][0]["capability"]["verdict"] == MATCH_UNKNOWN


def test_registry_matches_an_undeclared_worker_on_probed_capability(rig):
    """`python` is PROBED on the local node, so an undeclared session there
    is eligible for it -- a measurement, not an assumption."""
    registry, _, queue = rig
    queue.store.set_tasks("cap-undeclared", [{"title": "t", "prompt": "p"}])
    result = registry.diagnose(required_capabilities=["python"])
    assert result["code"] == CANDIDATES_AVAILABLE
    assert result["candidates"][0]["capability"]["verdict"] == MATCH_OK


def test_registry_reports_a_busy_worker_as_busy(rig):
    registry, _, queue = rig
    registry.declare("local", "cap-a", runtime_tools=["playwright"])
    queue.store.set_tasks("cap-a", [{"title": "t", "prompt": "p"}])
    queue.store.claim_next_task("cap-a", claimed_by="engine-1")
    result = registry.diagnose(required_capabilities=["playwright"])
    assert result["code"] == WORKERS_BUSY


def test_registry_reports_an_offline_node(rig):
    registry, _, _ = rig
    registry.declare("gone", "cap-a", runtime_tools=["playwright"])
    result = registry.diagnose(required_capabilities=["playwright"])
    assert result["code"] == NO_WORKERS_ONLINE
    assert result["counts"]["offline"] == 1


def test_registry_reports_multi_capability_partial_match(rig):
    registry, _, _ = rig
    registry.declare("local", "cap-a", runtime_tools=["playwright"])
    result = registry.diagnose(required_capabilities=["playwright", "wpf"])
    assert result["code"] == WORKERS_LACK_CAPABILITY
    assert result["candidates"][0]["capability"]["missing"] == ["wpf"]
    assert result["candidates"][0]["capability"]["matched"] == ["playwright"]


def test_registry_role_gap_is_unknown_for_undeclared_and_missing_for_declared(rig):
    registry, _, queue = rig
    registry.declare("local", "cap-a", roles=[ROLE_WORKER])
    queue.store.set_tasks("cap-undeclared", [{"title": "t", "prompt": "p"}])
    declared_only = registry.diagnose(role=ROLE_VERIFIER)
    by_key = {c["key"]: c for c in declared_only["candidates"]}
    assert by_key["local/cap-a"]["role_match"] == MATCH_MISSING
    assert by_key["local/cap-undeclared"]["role_match"] == MATCH_UNKNOWN
    assert declared_only["code"] == CAPABILITY_UNKNOWN


def test_registry_diagnose_changes_nothing(rig):
    """Pure read: it must not declare, claim or create anything."""
    registry, store, queue = rig
    before = (len(store.list_capabilities()), len(queue.store.list_all_lanes()))
    registry.diagnose(required_capabilities=["wpf"], role=ROLE_VERIFIER, project_id="p")
    assert (len(store.list_capabilities()), len(queue.store.list_all_lanes())) == before


# =========================================================================
# 5. PMService -- the opt-in undeclared fallback
# =========================================================================

@pytest.fixture
def service(tmp_path):
    store = PMStore(tmp_path / "pm.db")
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    nodes = _FakeNodes([_node("local", capabilities=["python"])])
    registry = WorkerRegistry(pm_store=store, node_registry=nodes, queue=queue)
    return PMService(store, queue, workers=registry), queue


def test_undeclared_sessions_are_invisible_to_routing_by_default(service):
    """The behaviour-preserving default: a session nobody declared is not
    routed to just because it exists."""
    pm, queue = service
    queue.store.set_tasks("cap-undeclared", [{"title": "t", "prompt": "p"}])
    assert pm._candidates() == []
    assert len(pm._candidates(include_undeclared=True)) == 1


def test_an_included_undeclared_session_still_cannot_satisfy_a_requirement(service):
    pm, queue = service
    queue.store.set_tasks("cap-undeclared", [{"title": "t", "prompt": "p"}])
    candidate = pm._candidates(include_undeclared=True)[0]
    assert candidate.has_profile is False
    assert "capability unknown" in hard_gate_failure(
        _task(required_capabilities=["wpf"]), candidate)


def test_an_included_undeclared_session_inherits_probed_node_capability(service):
    pm, queue = service
    queue.store.set_tasks("cap-undeclared", [{"title": "t", "prompt": "p"}])
    candidate = pm._candidates(include_undeclared=True)[0]
    assert "python" in candidate.detected_capabilities
    assert hard_gate_failure(_task(required_capabilities=["python"]), candidate) is None


def test_a_declared_session_is_never_duplicated_by_the_fallback(service):
    pm, queue = service
    queue.store.set_tasks("cap-a", [{"title": "t", "prompt": "p"}])
    pm.upsert_capability("local", "cap-a", runtime_tools=["playwright"])
    candidates = pm._candidates(include_undeclared=True)
    assert [c.session for c in candidates] == ["cap-a"]
    assert candidates[0].has_profile is True


def test_diagnose_task_explains_an_unroutable_task(service):
    pm, queue = service
    queue.store.set_tasks("cap-undeclared", [{"title": "t", "prompt": "p"}])
    task_id = queue.create_task("t", "p", session=None,
                                metadata={"required_capabilities": ["wpf"]})["task_id"]
    result = pm.diagnose_task(task_id)
    assert result["diagnosis"]["code"] == CAPABILITY_UNKNOWN
    assert result["include_undeclared"] is True


def test_eligible_workers_now_carries_the_diagnosis(service):
    pm, queue = service
    task_id = queue.create_task("t", "p", session=None)["task_id"]
    result = pm.eligible_workers(task_id)
    assert result["diagnosis"]["code"] == NO_WORKERS_REGISTERED
    assert result["total_candidates"] == 0


def test_pm_service_without_a_registry_ignores_include_undeclared(tmp_path):
    """Every existing construction site wires no registry; asking for the
    fallback there must be a no-op, never an AttributeError."""
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    queue.store.set_tasks("cap-undeclared", [{"title": "t", "prompt": "p"}])
    pm = PMService(PMStore(tmp_path / "pm.db"), queue)
    assert pm._candidates(include_undeclared=True) == []


# =========================================================================
# 6. Node scheduler + verify queue share the same codes
# =========================================================================

def test_scheduler_says_no_workers_registered_for_an_empty_fleet():
    result = choose_node([])
    assert result.node_id is None
    assert result.diagnosis["code"] == NO_WORKERS_REGISTERED


def test_scheduler_says_no_workers_online_when_every_node_is_offline():
    result = choose_node([_node("dell", status=NODE_OFFLINE)])
    assert result.diagnosis["code"] == NO_WORKERS_ONLINE


def test_scheduler_separates_capability_from_capacity():
    """An overloaded node that ALSO lacks the agent type is a capability
    gap: reporting "busy" would send an operator to fix the wrong thing."""
    incapable = choose_node([_node("dell", capacity=CAPACITY_OVERLOADED)],
                            required_agent_type="claude")
    assert incapable.diagnosis["code"] == WORKERS_LACK_CAPABILITY
    busy = choose_node([_node("dell", agent_types=("shell", "claude"),
                              capacity=CAPACITY_OVERLOADED)],
                       required_agent_type="claude")
    assert busy.diagnosis["code"] == WORKERS_BUSY


def test_scheduler_platform_requirement_is_a_capability_gap():
    result = choose_node([_node("dell", platform="linux")], required_platform="windows")
    assert result.diagnosis["code"] == WORKERS_LACK_CAPABILITY


def test_scheduler_label_named_like_a_platform_cannot_satisfy_it():
    """Namespaced keys: a label literally called "windows" is not a
    platform."""
    result = choose_node([_node("dell", platform="linux", labels=("windows",))],
                         required_platform="windows")
    assert result.diagnosis["code"] == WORKERS_LACK_CAPABILITY


def test_scheduler_success_still_picks_a_node_and_reports_availability():
    result = choose_node([_node("dell")])
    assert result.node_id == "dell"
    assert result.diagnosis["code"] == CANDIDATES_AVAILABLE


def test_verify_queue_node_diagnosis_uses_the_same_codes():
    assert diagnose_nodes([])["code"] == NO_WORKERS_REGISTERED
    assert diagnose_nodes([_node("dell", status=NODE_OFFLINE)],
                          required_capabilities=["python"])["code"] == NO_WORKERS_ONLINE
    assert diagnose_nodes([_node("dell", capabilities=["python"])],
                          required_capabilities=["wpf"])["code"] == WORKERS_LACK_CAPABILITY
    assert diagnose_nodes([_node("dell", capabilities=["python"])],
                          required_capabilities=["python"])["code"] == CANDIDATES_AVAILABLE


def test_a_node_never_answers_capability_unknown():
    """A node's capability set is PROBED, so an empty one means "measured
    to have nothing" -- CAPABILITY_UNKNOWN is a worker-level answer only."""
    result = diagnose_nodes([_node("dell", capabilities=[])], required_capabilities=["wpf"])
    assert result["code"] == WORKERS_LACK_CAPABILITY
    assert result["candidates"][0]["capability"]["verdict"] == MATCH_MISSING
