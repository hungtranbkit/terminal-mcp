"""PM/Orchestrator deterministic router -- pure unit coverage (docs/
REQUIREMENTS.md §20.2). No I/O, no real session/store involved -- see
pm_router.py's own module docstring for why this is deliberately a pure
function of plain data. pm_service.py's own tests cover real candidate
assembly + persistence; test_pm_mcp_tools.py covers the MCP surface."""
from __future__ import annotations

from terminal_mcp.pm_router import (
    BLOCKED, NO_ELIGIBLE_WORKER, ROUTED, WorkerCandidate, hard_gate_failure, route_task,
)


def _task(**metadata) -> dict:
    return {"id": "t1", "metadata": metadata}


def _candidate(session: str, **overrides) -> WorkerCandidate:
    base = {"node_id": "local", "session": session, "os": "linux", "runtime_tools": (), "online": True,
           "permissions_ok": True}
    base.update(overrides)
    return WorkerCandidate(**base)


# -- hard gate ---------------------------------------------------------------

def test_hard_gate_passes_a_fully_eligible_candidate():
    task = _task()
    candidate = _candidate("worker-a")
    assert hard_gate_failure(task, candidate) is None


def test_hard_gate_rejects_offline_candidate():
    task = _task()
    candidate = _candidate("worker-a", online=False)
    assert "not online" in hard_gate_failure(task, candidate)


def test_hard_gate_rejects_no_input_permission():
    task = _task()
    candidate = _candidate("worker-a", permissions_ok=False)
    assert "permission" in hard_gate_failure(task, candidate)


def test_hard_gate_rejects_os_mismatch():
    task = _task(required_os="windows")
    candidate = _candidate("worker-a", os="linux")
    assert "OS mismatch" in hard_gate_failure(task, candidate)


def test_hard_gate_accepts_os_match_case_insensitive():
    task = _task(required_os="Windows")
    candidate = _candidate("worker-a", os="WINDOWS")
    assert hard_gate_failure(task, candidate) is None


def test_hard_gate_rejects_missing_required_capability():
    task = _task(required_capabilities=["wpf", "dotnet"])
    candidate = _candidate("worker-a", skills=({"name": "wpf", "confidence": 0.9},))
    failure = hard_gate_failure(task, candidate)
    assert failure is not None and "dotnet" in failure


def test_hard_gate_accepts_when_all_required_capabilities_present():
    task = _task(required_capabilities=["wpf", "dotnet"])
    candidate = _candidate("worker-a", skills=({"name": "wpf"}, {"name": "dotnet"}))
    assert hard_gate_failure(task, candidate) is None


def test_hard_gate_rejects_project_affinity_mismatch():
    task = _task(project="OfflinePOS")
    candidate = _candidate("worker-a", project_affinity="OtherProject")
    assert "project affinity mismatch" in hard_gate_failure(task, candidate)


def test_hard_gate_no_affinity_declared_never_blocks():
    # Task declares a project but candidate has no affinity set at all --
    # never a hard failure (only an explicit MISMATCH is disqualifying).
    task = _task(project="OfflinePOS")
    candidate = _candidate("worker-a", project_affinity=None)
    assert hard_gate_failure(task, candidate) is None


def test_hard_gate_rejects_explicitly_excluded_session():
    task = _task(excluded_sessions=["worker-a"])
    candidate = _candidate("worker-a")
    assert "excluded" in hard_gate_failure(task, candidate)


def test_hard_gate_rejects_role_mismatch():
    task = _task(required_role="integration")
    candidate = _candidate("worker-a", role="developer")
    assert "role mismatch" in hard_gate_failure(task, candidate)


# -- routing: no eligible worker ----------------------------------------------

def test_route_task_no_eligible_worker_stays_unassigned_never_dropped():
    task = _task(required_os="windows")
    candidates = [_candidate("linux-a", os="linux"), _candidate("linux-b", os="linux")]
    decision = route_task(task, candidates)
    assert decision.status == NO_ELIGIBLE_WORKER
    assert decision.chosen is None
    assert decision.evidence["candidates_considered"] == 2


def test_route_task_empty_candidate_list_is_no_eligible_worker():
    decision = route_task(_task(), [])
    assert decision.status == NO_ELIGIBLE_WORKER


# -- routing: pinned session/node --------------------------------------------

def test_route_task_respects_explicit_pin_even_if_not_top_scored():
    task = _task(pinned_session="worker-b", project="Proj")
    high_score = _candidate("worker-a", project_affinity="Proj")  # would win on score alone
    pinned = _candidate("worker-b")  # no affinity bonus, but explicitly pinned
    decision = route_task(task, [high_score, pinned])
    assert decision.status == ROUTED
    assert decision.chosen.session == "worker-b"
    assert "human pin" in decision.reason


def test_route_task_pin_to_ineligible_session_blocks_never_reroutes():
    task = _task(pinned_session="worker-b", required_os="windows")
    pinned_but_wrong_os = _candidate("worker-b", os="linux")
    other_eligible = _candidate("worker-a", os="windows")  # would otherwise be a perfectly good match
    decision = route_task(task, [pinned_but_wrong_os, other_eligible])
    assert decision.status == BLOCKED
    assert decision.chosen is None
    assert "pinned session/node is not eligible" in decision.reason
    assert "OS mismatch" in decision.reason


def test_route_task_pin_to_nonexistent_session_blocks_with_clear_reason():
    task = _task(pinned_session="ghost-session")
    decision = route_task(task, [_candidate("worker-a")])
    assert decision.status == BLOCKED
    assert "no capability profile found" in decision.reason


def test_route_task_pinned_node_disambiguates_same_session_name_on_two_nodes():
    task = _task(pinned_session="worker-a", pinned_node="node-2")
    wrong_node = _candidate("worker-a", node_id="node-1")
    right_node = _candidate("worker-a", node_id="node-2")
    decision = route_task(task, [wrong_node, right_node])
    assert decision.status == ROUTED
    assert decision.chosen.node_id == "node-2"


# -- routing: soft scoring -----------------------------------------------------

def test_route_task_prefers_project_affinity_match():
    task = _task(project="OfflinePOS")
    matching = _candidate("worker-a", project_affinity="OfflinePOS")
    non_matching = _candidate("worker-b", project_affinity="OtherProject")
    decision = route_task(task, [matching, non_matching])
    assert decision.status == ROUTED
    assert decision.chosen.session == "worker-a"
    assert decision.score_breakdown["project_affinity_match"] == 10.0


def test_route_task_prefers_more_skill_matches():
    task = _task(required_capabilities=["wpf"], preferred_capabilities=["docker", "playwright"])
    fewer_skills = _candidate("worker-a", skills=({"name": "wpf"},))
    more_skills = _candidate("worker-b", skills=({"name": "wpf"}, {"name": "docker"}, {"name": "playwright"}))
    decision = route_task(task, [fewer_skills, more_skills])
    assert decision.chosen.session == "worker-b"


def test_route_task_prefers_idle_lower_queue_depth():
    task = _task()
    busy = _candidate("worker-a", queue_depth=5)
    idle = _candidate("worker-b", queue_depth=0)
    decision = route_task(task, [busy, idle])
    assert decision.chosen.session == "worker-b"


def test_route_task_fairness_boosts_a_less_recently_picked_candidate():
    task = _task()
    picked_often = _candidate("worker-a", picks_since_last_fairness_reset=10)
    rarely_picked = _candidate("worker-b", picks_since_last_fairness_reset=0)
    decision = route_task(task, [picked_often, rarely_picked])
    assert decision.chosen.session == "worker-b"


def test_route_task_deterministic_tie_break_by_session_name():
    # Two genuinely identical candidates -- must always pick the same one
    # (alphabetically first session name), never depend on list/dict order.
    task = _task()
    candidates = [_candidate("worker-z"), _candidate("worker-a")]
    decision1 = route_task(task, candidates)
    decision2 = route_task(task, list(reversed(candidates)))
    assert decision1.chosen.session == "worker-a"
    assert decision2.chosen.session == "worker-a"


def test_route_task_never_routes_windows_task_to_linux_candidate():
    # Explicit acceptance example from the task's own spec: "WPF/Windows
    # tasks never to Linux".
    task = _task(required_os="windows", required_capabilities=["wpf"])
    linux_candidate = _candidate("linux-worker", os="linux", skills=({"name": "wpf"},))
    windows_candidate = _candidate("windows-worker", os="windows", skills=({"name": "wpf"},))
    decision = route_task(task, [linux_candidate, windows_candidate])
    assert decision.status == ROUTED
    assert decision.chosen.session == "windows-worker"


def test_route_task_reason_and_score_breakdown_are_populated_for_explainability():
    task = _task(project="Proj", required_capabilities=["docker"])
    candidate = _candidate("worker-a", project_affinity="Proj", skills=({"name": "docker"},))
    decision = route_task(task, [candidate])
    assert decision.reason  # non-empty, human-readable
    assert "total" in decision.score_breakdown
    assert decision.evidence["candidates_considered"] == 1


# -- WIP limit (§20.6 Phase A) ---------------------------------------------

def test_hard_gate_rejects_candidate_at_its_wip_limit():
    task = _task()
    candidate = _candidate("worker-a", max_queued=2, queue_depth=2)
    failure = hard_gate_failure(task, candidate)
    assert failure is not None and "WIP limit" in failure


def test_hard_gate_accepts_candidate_under_its_wip_limit():
    task = _task()
    candidate = _candidate("worker-a", max_queued=2, queue_depth=1)
    assert hard_gate_failure(task, candidate) is None


def test_hard_gate_unbounded_by_default_even_at_high_queue_depth():
    task = _task()
    candidate = _candidate("worker-a", max_queued=None, queue_depth=1000)
    assert hard_gate_failure(task, candidate) is None


def test_route_task_prefers_a_worker_under_its_wip_limit_over_one_at_it():
    task = _task()
    at_limit = _candidate("worker-a", max_queued=1, queue_depth=1)
    under_limit = _candidate("worker-b", max_queued=5, queue_depth=1)
    decision = route_task(task, [at_limit, under_limit])
    assert decision.status == ROUTED
    assert decision.chosen.session == "worker-b"
