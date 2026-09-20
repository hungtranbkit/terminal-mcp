"""TMCP-HARNESS-001: the cost claim, measured here rather than quoted.

WHY THIS FILE EXISTS SEPARATELY FROM THE PILOT

The pilot measured a real multi-task run against the real UrbanFlow
repository. Those numbers are real, and they are also NOT REPRODUCIBLE on
every machine: the repository lives on one host, and every pilot test skips
where it is absent. A cost claim that can only be re-checked on one laptop is
a claim nobody else can audit, and it silently becomes folklore.

So the MECHANICS the claim rests on are measured here, on a synthetic graph,
with the real engine, the real store and the real counters -- reproducible
anywhere. What this file proves is not "the pilot cost N tokens"; it is that
each saving the pilot's arithmetic depends on actually happens:

  * a Planner is skipped when the definition already says what a contract
    needs                                        -> test_planner_*
  * an Evaluator is skipped when declared checks decide the criteria
                                                 -> test_evaluator_*
  * a second task in a lane inherits the first's session instead of paying a
    fresh context load                           -> test_session_*
  * a revision to that session is a delta, not a re-sent full context
                                                 -> test_revision_*
  * the engine never wakes a model on a timer    -> test_no_poll_*

The comparison figures are labelled `modelled` in the report and stay that
way here: naive_baseline is an accounting model of the shape being replaced,
never a measurement of a run that happened. The two are never added together.
"""
from __future__ import annotations

import pytest

from terminal_mcp import harness_context as ctx
from terminal_mcp import harness_policy as policy
from terminal_mcp import harness_scheduler as sched
from terminal_mcp import harness_state as state
from terminal_mcp import queue_store as qs
from terminal_mcp.harness_engine import AgentResult
from terminal_mcp.harness_service import HarnessService
from terminal_mcp.harness_store import HarnessStore

from .test_harness_core import ScriptedRunner, passing_checks

pytestmark = pytest.mark.usefixtures("declared_toolchain")


@pytest.fixture
def rig(tmp_path):
    queue_store = qs.QueueStore(tmp_path / "queue.db")
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "card.tsx").write_text("export const Card = () => null;\n")

    class Q:
        pass

    queue = Q()
    queue.store = queue_store
    runner = ScriptedRunner()
    service = HarnessService(store=HarnessStore(queue_store.path), queue=queue,
                             repo_root=str(repo), runner=runner,
                             check_runner=passing_checks)
    return service, queue_store, runner


def _task(queue_store, title, prompt):
    task_id, = queue_store.append_tasks("demo", [{"title": title, "prompt": prompt}])
    return task_id


# ---------------------------------------------------------------------------
# the Planner skip
# ---------------------------------------------------------------------------

def test_a_complete_definition_costs_no_planner_call(rig):
    """Paying a frontier model to perform a template substitution is the
    purest form of the waste this feature removes."""
    service, queue_store, runner = rig
    task_id = _task(queue_store, "Commute card", "add an ETA to the commute card")

    started = service.start(task_id=task_id,
                            acceptance=["the card shows an ETA"],
                            checks=["npm test"], steps=24)
    run_id = started["run"]["id"]

    assert [call["role"] for call in runner.calls].count(policy.PLANNER) == 0
    assert service.store.efficiency(run_id)["planner_skipped"] >= 1


def test_a_critical_task_pays_for_its_planner_anyway():
    """The skip is refused for CRITICAL work no matter how complete the
    definition looks -- otherwise the cheapest path would also be the one
    that skips the most review."""
    required, why = policy.planner_required(
        policy.CRITICAL, acceptance=["it works"], checks=["npm test"])

    assert required is True, why

    # And the same complete definition DOES buy the skip below CRITICAL, so
    # the refusal above is about the risk tier and not about the definition.
    skipped, _why = policy.planner_required(
        policy.STANDARD, acceptance=["it works"], checks=["npm test"],
        scope="rewrite the payment authorisation")
    assert skipped is False

    # ...and STANDARD still plans when any of the three is absent, because a
    # template cannot invent what "done" means.
    for gap in ({"acceptance": ()}, {"checks": ()}, {"scope": ""}):
        args = {"acceptance": ["it works"], "checks": ["npm test"],
                "scope": "rewrite the payment authorisation", **gap}
        needed, why = policy.planner_required(policy.STANDARD, **args)
        assert needed is True, why


# ---------------------------------------------------------------------------
# the Evaluator skip
# ---------------------------------------------------------------------------

def test_declared_checks_decide_the_verdict_without_an_evaluator(rig):
    """The engine reads the exit statuses, not the Builder's opinion of
    them. That is the difference between self-testing and self-approval."""
    service, queue_store, runner = rig
    task_id = _task(queue_store, "Commute card", "add an ETA to the commute card")

    started = service.start(task_id=task_id, acceptance=["the card shows an ETA"],
                            checks=["npm test"], steps=24)
    run_id = started["run"]["id"]

    assert [call["role"] for call in runner.calls].count(policy.EVALUATOR) == 0
    assert service.store.efficiency(run_id)["evaluator_skipped"] >= 1
    assert service.store.require_run(run_id).stage == state.MERGE_READY


def test_the_skip_is_bought_back_when_a_check_does_not_decide(rig):
    """An uncorroborated criterion buys exactly one evaluator call -- the
    saving is given up precisely where it would otherwise record a false
    statement in the audit trail."""
    from terminal_mcp.harness_engine import CheckResult, HarnessEngine

    service, queue_store, _runner = rig
    task_id = _task(queue_store, "Node version", "pin node to 24")

    def always_v26(commands, *, cwd=None, **kwargs):
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
    engine = HarnessEngine(service.store, runner=runner, repo_root=service.repo_root,
                           check_runner=always_v26)
    run, _ = engine.start(task_id=task_id, prompt="pin node to 24",
                          title="Node version", project_id="demo",
                          acceptance=["node -v reports v24.x"], checks=["node -v"],
                          changed_paths=["src"])
    engine.step(run.id)
    holder["contract"] = service.store.get_contract(run.id)
    for _ in range(3):
        engine.step(run.id)

    assert runner.roles() == [policy.BUILDER, policy.EVALUATOR]
    decisions = [entry["decision"]
                 for entry in service.store.efficiency(run.id)["cost_policy_decisions"]]
    assert any("evaluator escalation" in entry for entry in decisions)


# ---------------------------------------------------------------------------
# session reuse across a lane
# ---------------------------------------------------------------------------

def test_cost_first_opens_one_session_per_lane_not_one_per_task():
    """The saving is a full context load per task after the first."""
    nodes = {
        "A-1": sched.TaskNode("A-1", lane="A", priority="P0"),
        "A-2": sched.TaskNode("A-2", lane="A", priority="P0", depends_on=("A-1",)),
        "A-3": sched.TaskNode("A-3", lane="A", priority="P0", depends_on=("A-2",)),
    }

    plan = sched.plan(nodes, target="A-3", mode=sched.COST_FIRST)

    fresh = [a for a in plan.order if not a.inherits_session]
    assert len(plan.order) == 3
    assert len(fresh) == 1, "one session for the lane, not one per task"
    assert plan.session_reuses == 2


def test_speed_first_pays_a_context_load_for_every_task():
    """What the saving is measured AGAINST, stated as a real alternative
    rather than a hypothetical."""
    nodes = {
        "A-1": sched.TaskNode("A-1", lane="A", priority="P0"),
        "A-2": sched.TaskNode("A-2", lane="A", priority="P0", depends_on=("A-1",)),
        "A-3": sched.TaskNode("A-3", lane="A", priority="P0", depends_on=("A-2",)),
    }

    cost = sched.plan(nodes, target="A-3", mode=sched.COST_FIRST)
    speed = sched.plan(nodes, target="A-3", mode=sched.SPEED_FIRST)

    assert cost.session_reuses > speed.session_reuses


# ---------------------------------------------------------------------------
# the revision delta
# ---------------------------------------------------------------------------

def test_a_revision_to_the_same_session_sends_a_delta_not_the_context_again(rig):
    from terminal_mcp.harness_engine import CheckResult, HarnessEngine

    service, queue_store, _ = rig
    task_id = _task(queue_store, "Commute card", "add an ETA to the commute card")
    calls = {"n": 0}

    def fails_once(commands, *, cwd=None, **kwargs):
        calls["n"] += 1
        code = 1 if calls["n"] == 1 else 0
        return [CheckResult(command=c, exit_code=code, output="") for c in commands]

    runner = ScriptedRunner([
        AgentResult(payload={"ok": True}, session_id="builder-1", agent="builder",
                    commit="c1", context_percent=20.0),
        AgentResult(payload={"ok": True}, session_id="builder-1", agent="builder",
                    commit="c2", context_percent=25.0),
    ])
    engine = HarnessEngine(service.store, runner=runner, repo_root=service.repo_root,
                           check_runner=fails_once)
    run, _ = engine.start(task_id=task_id, prompt="add an ETA", title="Commute card",
                          project_id="demo", acceptance=["the card shows an ETA"],
                          checks=["npm test"], changed_paths=["src"])
    for _ in range(8):
        engine.step(run.id)

    efficiency = service.store.efficiency(run.id)
    assert efficiency["delta_prompts"] >= 1, \
        "the second attempt reuses the session that built the first"
    builder_calls = [call for call in runner.calls if call["role"] == policy.BUILDER]
    assert len(builder_calls) == 2
    first, second = builder_calls
    assert first["delta"] is False and second["delta"] is True
    assert second["tokens"] < first["tokens"], \
        "a delta is the failed criteria and nothing else"
    # The first Builder is given no session -- there is none yet -- and
    # RETURNS the one it opened. The revision is sent to exactly that
    # session, which is the only thing that makes a delta sound: the history
    # the delta refers to lives there. The engine works this out from the
    # session id rather than from a flag a caller could get wrong.
    assert first["session_id"] is None
    assert second["session_id"] == "builder-1"


# ---------------------------------------------------------------------------
# no model is woken on a timer
# ---------------------------------------------------------------------------

# The engine's freedom from loops, sleeps and timers is asserted structurally
# (by parsing, not grepping) in
# test_harness_core.test_the_engine_contains_no_loop_no_sleep_and_no_thread.
# Not repeated here: a second, weaker copy of that check would be the kind of
# thing that gets "fixed" by relaxing it.


def test_the_modelled_baseline_charges_for_the_poll_the_engine_does_not_make():
    """The comparison is only honest if it prices the thing that was removed."""
    quiet = ctx.naive_baseline(full_prompt_tokens=8_000, iterations=1, run_seconds=0)
    an_hour = ctx.naive_baseline(full_prompt_tokens=8_000, iterations=1,
                                 run_seconds=3600)

    assert an_hour.llm_calls > quiet.llm_calls
    assert an_hour.detail["pm_poll_calls"] > 0


# ---------------------------------------------------------------------------
# measured and modelled are never added together
# ---------------------------------------------------------------------------

def test_a_real_run_costs_strictly_fewer_calls_than_the_modelled_old_shape(rig):
    """The claim, made on numbers this machine can reproduce: the MEASURED
    calls of a real run, against the MODELLED calls of the pipeline being
    replaced, for the same task and the same iteration count."""
    service, queue_store, runner = rig
    task_id = _task(queue_store, "Commute card", "add an ETA to the commute card")

    started = service.start(task_id=task_id, acceptance=["the card shows an ETA"],
                            checks=["npm test"], steps=24)
    run_id = started["run"]["id"]
    run = service.store.require_run(run_id)
    measured = service.store.efficiency(run_id)

    modelled = ctx.naive_baseline(full_prompt_tokens=8_000,
                                  iterations=max(1, run.current_iteration))

    assert measured["llm_calls"] < modelled.llm_calls
    # One Builder call, and nothing else: no Planner, no Evaluator, no poll.
    assert measured["llm_calls"] == 1
    assert modelled.detail["planner_calls"] == 1
    assert modelled.detail["evaluator_calls"] >= 1


def test_the_report_labels_every_figure_measured_or_modelled(rig):
    """They are never summed. A single number mixing a measurement with an
    accounting model is a number nobody can defend."""
    service, queue_store, _ = rig
    task_id = _task(queue_store, "Commute card", "add an ETA")
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                            steps=8)

    summary = service.projections_for_tasks([task_id])[task_id]

    # Everything the service reports on a card is MEASURED -- counters the
    # store incremented, never a model of anything.
    for counter, value in summary["efficiency"].items():
        assert isinstance(value, int), counter
    assert "modelled" not in summary
    assert "baseline" not in summary
