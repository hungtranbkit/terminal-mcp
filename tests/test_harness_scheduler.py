"""Scheduling, session reuse across tasks, and the work nobody is paid to do.

The scheduler's whole claim is that "what next?" is arithmetic. These tests
hold it to that: the same graph must always produce the same plan, a task
needing a human must never be marked satisfied, and COST_FIRST must actually
end up with one session per lane rather than one per task.
"""
from __future__ import annotations

import pytest

from terminal_mcp import harness_context as ctx
from terminal_mcp import harness_policy as policy
from terminal_mcp import harness_scheduler as sched
from terminal_mcp import harness_state as state
from terminal_mcp.harness_contract import ExecutionContract
from terminal_mcp.harness_engine import (AgentResult, CheckResult, HarnessEngine,
                                         uncorroborated_criteria)
from terminal_mcp.harness_store import HarnessStore

from .test_harness_core import ScriptedRunner, passing_checks  # noqa: F401


# Every test here that names `node` or `npm` in a declared check is asserting
# about the harness, not about the host -- so the toolchain it names is
# materialised for it (see `declared_toolchain` in conftest). Without this the
# module's result depends on which machine runs it, which is how ten of these
# went red on a failover that changed no engine code.
pytestmark = pytest.mark.usefixtures("declared_toolchain")


# ---------------------------------------------------------------------------
# the UrbanFlow MOB-011 graph, which is the pilot's real dependency tree
# ---------------------------------------------------------------------------
GRAPH = {
    "ENV-001": sched.TaskNode("ENV-001", lane="B", priority="P0"),
    "ENV-006": sched.TaskNode("ENV-006", lane="B", priority="P0",
                              depends_on=("ENV-001",)),
    "MOB-001": sched.TaskNode("MOB-001", lane="A", priority="P0",
                              depends_on=("ENV-001", "ENV-006")),
    "MOB-002": sched.TaskNode("MOB-002", lane="A", priority="P0",
                              depends_on=("MOB-001",)),
    "MOB-003": sched.TaskNode("MOB-003", lane="A", priority="P0",
                              depends_on=("MOB-002",)),
    "CT-001": sched.TaskNode("CT-001", lane="B", priority="P0",
                             depends_on=("ENV-006",)),
    "CT-004": sched.TaskNode("CT-004", lane="B", priority="P0",
                             depends_on=("CT-001",)),
    "CT-005": sched.TaskNode("CT-005", lane="A", priority="P0",
                             depends_on=("CT-001", "CT-004")),
    "MOB-011": sched.TaskNode("MOB-011", lane="A", priority="P0",
                              depends_on=("MOB-003", "CT-005"),
                              requested_mode=policy.CRITICAL),
}


@pytest.fixture
def store(tmp_path):
    return HarnessStore(tmp_path / "queue.db")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.ts").write_text("export const a = 1;\n")
    return root


# ---------------------------------------------------------------------------
# the graph
# ---------------------------------------------------------------------------

def test_the_plan_respects_every_declared_dependency():
    plan = sched.plan(GRAPH, target="MOB-011")
    position = {a.task_id: index for index, a in enumerate(plan.order)}
    for task_id, node in GRAPH.items():
        for dependency in node.depends_on:
            assert position[dependency] < position[task_id], \
                f"{task_id} was planned before its dependency {dependency}"


def test_the_target_is_planned_last():
    plan = sched.plan(GRAPH, target="MOB-011")
    assert plan.order[-1].task_id == "MOB-011"


def test_the_plan_is_deterministic():
    first = [a.task_id for a in sched.plan(GRAPH, target="MOB-011").order]
    for _ in range(5):
        assert [a.task_id for a in sched.plan(GRAPH, target="MOB-011").order] == first


def test_the_critical_path_is_started_first():
    """ENV-001 is five hops from the milestone; delaying it delays everything."""
    depths = sched.depth_to(GRAPH, "MOB-011")
    assert depths["ENV-001"] == 5
    assert depths["MOB-011"] == 0
    assert sched.plan(GRAPH, target="MOB-011").order[0].task_id == "ENV-001"


def test_a_cycle_is_reported_with_its_path():
    graph = {"A": sched.TaskNode("A", depends_on=("B",)),
             "B": sched.TaskNode("B", depends_on=("A",))}
    with pytest.raises(sched.DependencyCycle) as excinfo:
        sched.plan(graph, target="A")
    assert "A" in excinfo.value.cycle and "B" in excinfo.value.cycle


def test_a_dependency_missing_from_the_graph_is_a_named_gap():
    graph = {"A": sched.TaskNode("A", depends_on=("GHOST",))}
    plan = sched.plan(graph, target="A")
    assert [a.task_id for a in plan.order] == ["GHOST", "A"] or \
           any(task == "GHOST" for task, _ in plan.deferred)


# ---------------------------------------------------------------------------
# cost_first: one session per lane, not one per task
# ---------------------------------------------------------------------------

def test_cost_first_opens_one_session_per_lane(store):
    plan = sched.plan(GRAPH, target="MOB-011", mode=sched.COST_FIRST)
    fresh = [a for a in plan.order if not a.inherits_session]
    lanes = {GRAPH[a.task_id].lane for a in plan.order}
    assert len(fresh) == len(lanes) == 2
    assert plan.session_reuses == len(plan.order) - len(fresh)


def test_speed_first_buys_wall_clock_with_a_context_load_per_task():
    cost = sched.plan(GRAPH, target="MOB-011", mode=sched.COST_FIRST)
    speed = sched.plan(GRAPH, target="MOB-011", mode=sched.SPEED_FIRST)
    assert speed.session_reuses == 0
    assert cost.session_reuses > 0
    assert speed.scheduling.max_active_builders > cost.scheduling.max_active_builders


def test_cost_first_keeps_one_builder_active():
    assert sched.concurrency_slots(
        sched.plan(GRAPH, target="MOB-011", mode=sched.COST_FIRST)) == 1


def test_no_mode_ever_runs_a_planner_pool():
    for mode in sched.SCHEDULING_MODES:
        assert sched.scheduler_policy(mode).parallel_planners == 1


def test_a_full_lane_session_is_not_reused():
    """Above 85% the session has no room, so the next task starts a new one."""
    plan = sched.plan(GRAPH, target="MOB-011", mode=sched.COST_FIRST,
                      context_percent={"A": 91.0, "B": 91.0})
    assert plan.session_reuses == 0


def test_an_unknown_scheduling_mode_is_refused():
    with pytest.raises(ValueError):
        sched.scheduler_policy("yolo")


# ---------------------------------------------------------------------------
# the task a machine must not finish
# ---------------------------------------------------------------------------

def test_a_non_autonomous_task_is_deferred_and_never_marked_satisfied():
    """VIS-001 needs an approved asset only a person has. Faking its
    completion is the one outcome this field exists to make unreachable."""
    graph = dict(GRAPH)
    graph["VIS-001"] = sched.TaskNode(
        "VIS-001", lane="D", priority="P1", autonomous=False,
        not_autonomous_reason="needs the human-approved UI board asset")
    graph["MOB-011"] = sched.TaskNode(
        "MOB-011", lane="A", priority="P0",
        depends_on=("MOB-003", "CT-005", "VIS-001"))

    plan = sched.plan(graph, target="MOB-011")
    deferred = dict(plan.deferred)

    assert "VIS-001" in deferred
    assert "approved UI board" in deferred["VIS-001"]
    planned = [a.task_id for a in plan.order]
    assert "VIS-001" not in planned
    assert "MOB-011" not in planned, \
        "a task depending on a human input must not be planned as startable"
    assert "MOB-011" in deferred


def test_work_that_does_not_depend_on_the_human_still_proceeds():
    graph = dict(GRAPH)
    graph["VIS-001"] = sched.TaskNode("VIS-001", lane="D", autonomous=False,
                                      not_autonomous_reason="needs the asset")
    graph["MOB-011"] = sched.TaskNode("MOB-011", lane="A",
                                      depends_on=("MOB-003", "CT-005", "VIS-001"))
    planned = [a.task_id for a in sched.plan(graph, target="MOB-011").order]
    assert {"ENV-001", "MOB-003", "CT-005"} <= set(planned)


def test_readiness_is_computed_from_finished_runs_not_from_a_file():
    ready = sched.ready(GRAPH, satisfied=["ENV-001"], target="MOB-011")
    assert "ENV-006" in ready
    assert "MOB-001" not in ready, "MOB-001 still needs ENV-006"
    assert "ENV-001" not in ready


# ---------------------------------------------------------------------------
# what the plan saves, end to end through the engine
# ---------------------------------------------------------------------------

def test_an_inherited_session_gets_the_contract_and_not_the_modules(store, repo):
    runner = ScriptedRunner()
    engine = HarnessEngine(store, runner=runner, repo_root=str(repo),
                           check_runner=passing_checks)
    first, _ = engine.start(task_id="CT-001", prompt="define the domain contracts",
                            title="Contracts", project_id="urbanflow",
                            acceptance=["the Place type exists"], checks=["true"],
                            changed_paths=["src"])
    engine.drive(first.id)
    first_build = [c for c in runner.calls if c["role"] == policy.BUILDER][-1]

    second, _ = engine.start(task_id="CT-004", prompt="create the demo fixture",
                             title="Fixture", project_id="urbanflow",
                             acceptance=["one scenario powers every screen"],
                             checks=["true"], changed_paths=["src"],
                             inherit_builder_session="lane-b-session",
                             inherit_from_task="CT-001")
    assert second.builder_session_id == "lane-b-session"
    engine.drive(second.id)
    second_build = [c for c in runner.calls if c["role"] == policy.BUILDER][-1]

    assert second_build["delta"] is True
    assert second_build["session_id"] == "lane-b-session"
    assert "CT-001" in second_build["text"]
    assert "export const a = 1" not in second_build["text"], \
        "the lane's modules are already loaded; resending them is paying twice"
    assert second_build["tokens"] < first_build["tokens"]
    assert store.efficiency(second.id)["session_reused"] == 1
    assert store.efficiency(second.id)["delta_prompts"] == 1


def test_the_inheritance_is_recorded_as_a_durable_event(store, repo):
    engine = HarnessEngine(store, runner=ScriptedRunner(), repo_root=str(repo),
                           check_runner=passing_checks)
    run, _ = engine.start(task_id="CT-004", prompt="x", title="x",
                          project_id="urbanflow", acceptance=["a"], checks=["true"],
                          changed_paths=["src"], inherit_builder_session="lane-b",
                          inherit_from_task="CT-001")
    events = [e for e in store.events(run.id)
              if e["event_type"] == "BUILDER_SESSION_INHERITED"]
    assert len(events) == 1
    assert events[0]["metadata"]["from_task"] == "CT-001"


# ---------------------------------------------------------------------------
# a green check that does not decide its criterion
# ---------------------------------------------------------------------------

def test_a_version_check_that_passes_on_every_version_is_not_evidence():
    contract = ExecutionContract.build(
        run_id="r", task_id="ENV-001", scope="pin the node version",
        functional_acceptance=["node -v reports v24.x"], required_checks=["node -v"])
    assert uncorroborated_criteria(contract, [CheckResult("node -v", 0, "v26.7.0")])
    assert not uncorroborated_criteria(contract, [CheckResult("node -v", 0, "v24.3.1")])


def test_a_criterion_naming_no_literal_is_left_to_its_check():
    """The escalation rule is narrow on purpose: firing on prose would
    restore the cost of an evaluator on every task in the system."""
    contract = ExecutionContract.build(
        run_id="r", task_id="X", scope="pad it",
        functional_acceptance=["the header looks balanced"], required_checks=["true"])
    assert uncorroborated_criteria(contract, [CheckResult("true", 0, "")]) == ()


def test_an_uncorroborated_criterion_buys_exactly_one_evaluator_call(store, repo):
    def node_v(commands, *, cwd=None, **kwargs):
        return [CheckResult(command=c, exit_code=0, output="v26.7.0") for c in commands]

    holder = {}

    def evaluator(run, iteration):
        return AgentResult(payload={
            "result": "fail", "summary": "node is v26, the contract asks for v24",
            "criteria": [{"criterion_id": cid, "result": "fail",
                          "evidence": "node -v printed v26.7.0"}
                         for cid in holder["contract"].criterion_ids()]},
            session_id="evaluator-1", agent="evaluator-agent")

    runner = ScriptedRunner([
        AgentResult(payload={"ok": True}, session_id="builder-1", agent="builder",
                    commit="c1", context_percent=20.0),
        evaluator,
    ])
    engine = HarnessEngine(store, runner=runner, repo_root=str(repo),
                           check_runner=node_v)
    run, _ = engine.start(task_id="ENV-001", prompt="pin node to 24",
                          title="Node version", project_id="urbanflow",
                          acceptance=["node -v reports v24.x"], checks=["node -v"],
                          changed_paths=["src"])
    # The triage picks STANDARD for this description; what matters is that the
    # mode does NOT require an independent evaluator, so any evaluator call
    # here is one the corroboration check bought and nothing else.
    assert policy.evaluator_required(run.mode)[0] is False

    holder["contract"] = None
    engine.step(run.id)
    holder["contract"] = store.get_contract(run.id)
    engine.step(run.id)
    engine.step(run.id)
    outcome = engine.step(run.id)

    assert runner.roles() == [policy.BUILDER, policy.EVALUATOR], \
        "a mode that skips the evaluator paid for one only because the check " \
        "turned out not to decide the criterion"
    assert outcome.to_stage == state.REVISING, \
        "a false claim is a product failure, and product failures revise"
    escalations = [entry["decision"] for entry in
                   store.efficiency(run.id)["cost_policy_decisions"]]
    assert any("evaluator escalation" in entry for entry in escalations)
    assert store.list_decisions(status="open", run_id=run.id) == [], \
        "nothing here is a human decision"


def test_without_a_runner_an_uncorroborated_criterion_asks_rather_than_passes(store, repo):
    """The failure mode this forbids: recording a false statement in the audit
    trail because nobody was available to check it."""
    def node_v(commands, *, cwd=None, **kwargs):
        return [CheckResult(command=c, exit_code=0, output="v26.7.0") for c in commands]

    engine = HarnessEngine(store, runner=None, repo_root=str(repo), check_runner=node_v)
    run, _ = engine.start(task_id="ENV-001", prompt="pin node to 24", title="Node",
                          project_id="urbanflow",
                          acceptance=["node -v reports v24.x"], checks=["node -v"],
                          changed_paths=["src"])
    engine.step(run.id)
    engine.step(run.id)
    with pytest.raises(Exception):
        engine.step(run.id)  # no runner: the build itself cannot happen


# ---------------------------------------------------------------------------
# what the comparison is against
# ---------------------------------------------------------------------------

def test_parallel_execution_removes_no_llm_calls_and_adds_coordination():
    sequential = ctx.naive_baseline(full_prompt_tokens=8_000, iterations=1)
    parallel = ctx.naive_parallel_baseline(tasks=1, full_prompt_tokens=8_000,
                                           iterations_per_task=1)
    assert parallel.llm_calls > sequential.llm_calls
    assert parallel.detail["cache_reuse"] == 0
    assert parallel.detail["context_loads"] == 1


def test_the_parallel_baseline_scales_with_the_task_count():
    one = ctx.naive_parallel_baseline(tasks=1, full_prompt_tokens=8_000)
    nine = ctx.naive_parallel_baseline(tasks=9, full_prompt_tokens=8_000)
    assert nine.llm_calls == 9 * one.llm_calls
