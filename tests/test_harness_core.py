"""TMCP-HARNESS-001: the single execution state machine and its cost policy.

WHAT THESE TESTS ARE ACTUALLY DEFENDING

Most of them assert an ABSENCE, because every defect this feature exists to
remove was something extra happening:

* a failing test putting a task in front of a human  -> test_fail_revises_*
* "retry" restarting a task from its original prompt -> test_resume_*
* a second store disagreeing with the queue          -> test_migration_*
* a Planner invoked to fill in a template            -> test_planner_skipped_*
* a full context resent on every revision            -> test_revision_is_delta
* a model woken on a timer to ask "anything new?"    -> test_engine_has_no_loop

SAFETY: every database here is tmp_path-scoped and every session name is a
fixture. Nothing in this file touches the production queue database or a real
tmux session.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from terminal_mcp import harness_context as ctx
from terminal_mcp import harness_policy as policy
from terminal_mcp import harness_state as state
from terminal_mcp.harness_contract import (ExecutionContract,
                                           InsufficientSpecification,
                                           MalformedVerdict, criterion_id,
                                           definition_hash, parse_verdict)
from terminal_mcp.harness_engine import (AgentResult, CheckResult, HarnessEngine,
                                         run_checks)
from terminal_mcp.harness_schema import HARNESS_SCHEMA_VERSION, HARNESS_TABLES
from terminal_mcp.harness_store import HarnessStore, LeaseNotHeld, RunNotFound
from terminal_mcp.queue_store import QueueStore


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    return HarnessStore(tmp_path / "queue.db")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "header.css").write_text(".header{padding:8px}\n")
    (root / "src" / "card.tsx").write_text("export const Card = () => null;\n")
    return root


class ScriptedRunner:
    """An AgentRunner that records what it was asked and answers from a script.

    Deliberately dumb: the point of these tests is what the ENGINE decides to
    send and when, so the runner must not contain any decision of its own.
    """

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.calls: list[dict] = []

    def run(self, *, role, prompt, run, tier, session_id, iteration):
        self.calls.append({"role": role, "iteration": iteration, "tier": tier,
                           "session_id": session_id, "delta": prompt.delta,
                           "text": prompt.text,
                           "tokens": prompt.tokens_estimate})
        if self.answers:
            answer = self.answers.pop(0)
            if callable(answer):
                answer = answer(run, iteration)
            return answer
        return AgentResult(payload={"ok": True},
                           session_id=session_id or f"sess-{run.id[-6:]}",
                           agent=f"{role}-agent", commit=f"commit{iteration}",
                           context_percent=40.0, completion_tokens_estimate=250)

    def roles(self):
        return [c["role"] for c in self.calls]


def passing_checks(commands, *, cwd=None, **kwargs):
    return [CheckResult(command=c, exit_code=0, output="ok") for c in commands]


def failing_checks(commands, *, cwd=None, **kwargs):
    return [CheckResult(command=c, exit_code=1, output="AssertionError: padding is 8px")
            for c in commands]


def flaky_checks(fail_times: int):
    seen = {"n": 0}

    def runner(commands, *, cwd=None, **kwargs):
        seen["n"] += 1
        if seen["n"] <= fail_times:
            return failing_checks(commands)
        return passing_checks(commands)

    return runner


# ---------------------------------------------------------------------------
# 1. the database is the queue's database
# ---------------------------------------------------------------------------

def test_migration_lands_harness_tables_in_the_queue_database(tmp_path):
    """One file. A HarnessRun and the queue_task it drives must be able to
    commit together, which is impossible across two databases."""
    path = tmp_path / "queue.db"
    QueueStore(path)  # the QUEUE store opens first -- the common case
    connection = sqlite3.connect(path)
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "queue_tasks" in tables
    assert set(HARNESS_TABLES) <= tables
    assert connection.execute("PRAGMA user_version").fetchone()[0] == HARNESS_SCHEMA_VERSION


def test_either_store_migrates_the_file_identically(tmp_path):
    harness_first, queue_first = tmp_path / "a.db", tmp_path / "b.db"
    HarnessStore(harness_first)
    QueueStore(queue_first)

    def snapshot(path):
        connection = sqlite3.connect(path)
        return sorted(row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))

    assert snapshot(harness_first) == snapshot(queue_first)


def test_migrating_twice_is_a_no_op(tmp_path):
    path = tmp_path / "queue.db"
    HarnessStore(path)
    before = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
    HarnessStore(path)
    QueueStore(path)
    after = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
    assert before == after == HARNESS_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 2. the state machine
# ---------------------------------------------------------------------------

def test_a_failing_evaluation_has_an_edge_to_revising():
    assert state.is_valid_transition(state.EVALUATING, state.REVISING)


def test_no_resumable_stage_can_reach_init():
    """The structural reason "infra retry" can never mean "start over".

    If any of these edges existed, a retry path could legally walk a run back
    to INIT and rebuild it from the original prompt -- the single most
    expensive defect in the pipeline this replaces.
    """
    for stage in state.RESUMABLE_STAGES:
        assert state.INIT not in state.VALID_TRANSITIONS[stage], stage
    for stage in (state.FAILED_INFRA, state.RECOVERY_REQUIRED,
                  state.CONTEXT_ROLLOVER, state.BLOCKED):
        assert state.INIT not in state.VALID_TRANSITIONS[stage], stage


def test_every_stage_projects_to_a_renderable_label():
    for stage in state.STAGES:
        assert state.project_stage(stage) in state.PROJECTED_STAGES


def test_an_edge_that_does_not_exist_is_refused_not_corrected():
    with pytest.raises(state.InvalidStageTransition):
        state.require_transition(state.MERGE_READY, state.BUILDING)


# ---------------------------------------------------------------------------
# 3. the contract
# ---------------------------------------------------------------------------

def _contract(run_id="run1", **overrides):
    fields = {"functional_acceptance": ["the header has 16px padding"],
              "required_checks": ["npm run lint"]}
    fields.update(overrides)
    return ExecutionContract.build(run_id=run_id, task_id="T1",
                                   scope="pad the header", **fields)


def test_the_hash_covers_what_the_work_is_and_nothing_else():
    first = _contract()
    second = _contract()
    assert first.content_hash == second.content_hash
    assert first.id != second.id  # identity of the ROW differs; of the WORK does not


def test_changing_acceptance_changes_the_hash():
    assert _contract().content_hash != _contract(
        functional_acceptance=["the header has 24px padding"]).content_hash


def test_a_definition_without_a_decidable_bar_is_refused_at_planning():
    with pytest.raises(InsufficientSpecification) as excinfo:
        ExecutionContract.build(run_id="r", task_id="t", scope="make it nicer")
    assert "acceptance criteria" in excinfo.value.missing
    assert "required checks" in excinfo.value.missing


def test_a_redefine_is_a_new_version_not_an_edit():
    original = _contract().freeze()
    revised = original.redefine(functional_acceptance=["the header has 24px padding"])
    assert revised.version == original.version + 1
    assert revised.frozen is False
    assert original.functional_acceptance == ("the header has 16px padding",)


def test_a_frozen_contract_refuses_a_field_change():
    from terminal_mcp.harness_contract import ContractImmutable
    with pytest.raises(ContractImmutable):
        _contract().freeze().require_mutable("scope")


@pytest.mark.parametrize("payload", ["looks good", "LGTM", "all good"])
def test_prose_approval_is_a_parse_error(payload):
    with pytest.raises(MalformedVerdict) as excinfo:
        parse_verdict(payload, _contract(), iteration=1)
    assert "not a verdict" in str(excinfo.value)


def test_a_verdict_missing_a_criterion_says_which_one():
    contract = _contract(functional_acceptance=["a", "b"])
    with pytest.raises(MalformedVerdict) as excinfo:
        parse_verdict({"result": "pass", "criteria": [
            {"criterion_id": criterion_id("functional", "a"), "result": "pass",
             "evidence": "lint exit 0"}]}, contract, iteration=1)
    assert criterion_id("functional", "b") in str(excinfo.value)


def test_a_criterion_without_evidence_is_refused():
    contract = _contract()
    with pytest.raises(MalformedVerdict) as excinfo:
        parse_verdict({"result": "pass", "criteria": [
            {"criterion_id": contract.criterion_ids()[0], "result": "pass",
             "evidence": ""}]}, contract, iteration=1)
    assert "no evidence" in str(excinfo.value)


def test_an_evaluator_contradicting_itself_is_refused():
    """`pass` overall while marking a criterion failed. The per-criterion
    detail carries the evidence, so the summary is what must give way."""
    contract = _contract()
    with pytest.raises(MalformedVerdict) as excinfo:
        parse_verdict({"result": "pass", "criteria": [
            {"criterion_id": contract.criterion_ids()[0], "result": "fail",
             "evidence": "measured 8px"}]}, contract, iteration=1)
    assert "per-criterion results say" in str(excinfo.value)


def test_feedback_carries_only_what_failed():
    contract = _contract(functional_acceptance=["a", "b"])
    verdict = parse_verdict({"criteria": [
        {"criterion_id": criterion_id("functional", "a"), "result": "pass",
         "evidence": "lint exit 0"},
        {"criterion_id": criterion_id("functional", "b"), "result": "fail",
         "evidence": "measured 8px"}]}, contract, iteration=1)
    feedback = verdict.feedback()
    assert [entry["text"] for entry in feedback["fix_only"]] == ["b"]
    assert feedback["already_passing"] == [criterion_id("functional", "a")]


# ---------------------------------------------------------------------------
# 4. the policy -- difficulty triage as a function
# ---------------------------------------------------------------------------

def test_triage_is_deterministic():
    first = policy.mode_for("fix the login redirect")
    assert first == policy.mode_for("fix the login redirect")


def test_a_risk_area_forces_critical_however_small_the_change():
    mode, areas, _ = policy.mode_for("tweak the auth token expiry by one second")
    assert mode == policy.CRITICAL
    assert "auth" in areas


def test_a_requested_mode_may_escalate_but_never_de_escalate():
    mode, _, reasons = policy.mode_for(
        "change the database migration for sessions", requested_mode=policy.LIGHT)
    assert mode == policy.CRITICAL
    assert any("refused de-escalation" in r for r in reasons)

    mode, _, reasons = policy.mode_for("rename a label",
                                       requested_mode=policy.CRITICAL)
    assert mode == policy.CRITICAL
    assert any("escalated by request" in r for r in reasons)


def test_the_planner_is_skipped_only_when_the_definition_can_carry_a_contract():
    assert policy.planner_required(policy.LIGHT)[0] is False
    assert policy.planner_required(policy.CRITICAL, acceptance=["a"], checks=["c"],
                                   scope="s")[0] is True
    assert policy.planner_required(policy.STANDARD, acceptance=["a"], checks=["c"],
                                   scope="s")[0] is False
    required, why = policy.planner_required(policy.STANDARD, acceptance=[], checks=["c"],
                                            scope="s")
    assert required is True and "acceptance" in why


def test_only_critical_pays_for_an_independent_evaluator():
    assert policy.evaluator_required(policy.CRITICAL)[0] is True
    assert policy.evaluator_required(policy.STANDARD)[0] is False
    assert policy.evaluator_required(policy.LIGHT)[0] is False


def test_the_budget_never_says_stop():
    """A hard cap turns an expensive run into a wasted one."""
    actions = {policy.budget_action(spent, 100)
               for spent in (0, 50, 80, 95, 120, 10_000)}
    assert "stop" not in actions
    assert policy.budget_action(10_000, 100) == policy.BUDGET_ESCALATE
    assert policy.budget_action(0, None) == policy.BUDGET_OK


def test_an_unknown_context_window_continues_rather_than_guessing():
    assert policy.context_action(None) == policy.CONTINUE
    assert policy.may_reuse_builder(None) is True
    assert policy.may_reuse_builder(84.9) is True
    assert policy.may_reuse_builder(85.0) is False
    assert policy.context_action(99.0) == policy.REPLACE_SESSION


def test_repeated_failure_promotes_every_role_to_the_strongest_tier():
    assert policy.tier_for(policy.LIGHT, policy.BUILDER) == policy.TIER_BALANCED
    assert policy.tier_for(policy.LIGHT, policy.BUILDER,
                           failed_iterations=2) == policy.TIER_FRONTIER


def test_no_parallel_planners_or_evaluator_consensus_by_default():
    cost = policy.cost_policy_for(policy.CRITICAL)
    assert cost.parallel_planners == 1
    assert cost.evaluator_consensus == 1


# ---------------------------------------------------------------------------
# 5. the store
# ---------------------------------------------------------------------------

def test_a_repeated_start_resolves_to_the_same_run(store):
    key = "run:p:t:abc"
    first, created_first = store.create_run(task_id="t", prompt="p", request_key=key)
    second, created_second = store.create_run(task_id="t", prompt="p", request_key=key)
    assert created_first is True and created_second is False
    assert first.id == second.id


def test_the_event_log_has_no_update_path(store):
    """The absence of the method IS the enforcement."""
    assert not hasattr(store, "update_event")
    assert not hasattr(store, "delete_event")


def test_a_named_caller_without_the_lease_is_refused(store):
    run, _ = store.create_run(task_id="t", prompt="p")
    assert store.acquire_lease(run.id, "engine-a") is True
    assert store.acquire_lease(run.id, "engine-b") is False
    with pytest.raises(LeaseNotHeld):
        store.advance(run.id, state.PLANNING, owner="engine-b")
    store.advance(run.id, state.PLANNING, owner="engine-a")


def test_a_failing_test_cannot_be_put_in_front_of_a_human(store):
    with pytest.raises(policy.NotAHumanDecision):
        store.open_decision(reason="tests_failed", question="the build is red")
    decision = store.open_decision(reason=policy.MISSING_CREDENTIAL,
                                   question="which API key?")
    assert decision["status"] == "open"


def test_a_contract_version_cannot_be_overwritten(store):
    run, _ = store.create_run(task_id="t", prompt="p")
    contract = _contract(run_id=run.id)
    store.save_contract(contract)
    with pytest.raises(ValueError):
        store.save_contract(contract)


def test_efficiency_counters_accumulate_and_refuse_unknown_names(store):
    run, _ = store.create_run(task_id="t", prompt="p")
    store.bump_efficiency(run.id, llm_calls=1, prompt_tokens_estimate=100)
    store.bump_efficiency(run.id, llm_calls=2, prompt_tokens_estimate=50)
    record = store.efficiency(run.id)
    assert record["llm_calls"] == 3
    assert record["prompt_tokens_estimate"] == 150
    assert store.require_run(run.id).tokens_spent_estimate == 150
    with pytest.raises(ValueError):
        store.bump_efficiency(run.id, made_up_counter=1)


def test_a_missing_run_is_a_lookup_error_not_none(store):
    with pytest.raises(RunNotFound):
        store.require_run("hrn_nope")


def test_overrides_resolve_most_specific_first(store):
    store.set_policy_override(scope="global", scope_key="*", mode=policy.STANDARD)
    store.set_policy_override(scope="project", scope_key="P",
                              write_authority=policy.SUPERVISED)
    store.set_policy_override(scope="task", scope_key="T", mode=policy.CRITICAL)
    merged = store.resolve_policy_overrides(project_id="P", task_id="T")
    assert merged["mode"] == policy.CRITICAL
    assert merged["write_authority"] == policy.SUPERVISED


# ---------------------------------------------------------------------------
# 6. context packs and delta prompts
# ---------------------------------------------------------------------------

def test_a_pack_respects_its_byte_ceiling_and_names_what_it_left_out(store, repo):
    for index in range(40):
        (repo / "src" / f"f{index:02d}.ts").write_text("x" * 4000)
    assembler = ctx.ContextAssembler(store, repo_root=repo, pack_bytes=20_000,
                                     max_files=5)
    pack, _hit = assembler.build_pack(project_id="P", modules=["src"])
    assert len(pack.files) <= 5
    assert pack.bytes <= 20_000 + 4_000  # ceiling plus the rendered headers
    assert pack.omitted, "an omitted file must be NAMED, never silently dropped"
    assert "Present but not included" in pack.render()


def test_the_cache_key_is_the_hash_of_what_went_in(store, repo):
    assembler = ctx.ContextAssembler(store, repo_root=repo)
    first, hit_first = assembler.build_pack(project_id="P", modules=["src/header.css"])
    second, hit_second = assembler.build_pack(project_id="P", modules=["src/header.css"])
    assert hit_first is False and hit_second is True
    assert first.cache_key == second.cache_key

    (repo / "src" / "header.css").write_text(".header{padding:16px}\n")
    third, hit_third = assembler.build_pack(project_id="P", modules=["src/header.css"])
    assert hit_third is False, "a changed file must produce a different key"
    assert third.cache_key != first.cache_key


def test_a_pack_cannot_read_outside_the_repository(store, repo, tmp_path):
    (tmp_path / "secret.env").write_text("TOKEN=hunter2")
    assembler = ctx.ContextAssembler(store, repo_root=repo)
    pack, _ = assembler.build_pack(project_id="P", modules=["../secret.env"])
    assert pack.files == ()
    assert "hunter2" not in pack.render()


def test_skill_injection_is_bounded_and_deterministic():
    skills = [ctx.SkillRef(id=f"s{i}", version="1", body="y" * 10_000, tags=("ui",))
              for i in range(8)]
    selected, rejected = ctx.select_skills(skills, tags=["ui"])
    assert len(selected) <= policy.MAX_INJECTED_SKILLS
    assert sum(s.bytes for s in selected) <= policy.MAX_SKILL_BYTES
    assert rejected
    assert [s.id for s in selected] == [s.id for s in
                                        ctx.select_skills(skills, tags=["ui"])[0]]


def test_a_revision_prompt_carries_the_failures_and_not_the_contract(store):
    contract = _contract(functional_acceptance=["padding is 16px", "the card has a border"])
    verdict = parse_verdict({"criteria": [
        {"criterion_id": criterion_id("functional", "padding is 16px"),
         "result": "fail", "evidence": "measured 8px"},
        {"criterion_id": criterion_id("functional", "the card has a border"),
         "result": "pass", "evidence": "screenshot shows a 1px border"}]},
        contract, iteration=1)
    assembler = ctx.ContextAssembler(store)
    delta = assembler.revision_prompt(evaluation=verdict, contract=contract, iteration=2)
    full = assembler.builder_prompt(contract=contract, iteration=1)

    assert delta.delta is True
    assert "measured 8px" in delta.text
    assert "the card has a border" not in delta.text.split("Already passing")[0]
    assert contract.scope not in delta.text, "the contract is not resent"
    assert delta.tokens_estimate < full.tokens_estimate


def test_the_naive_baseline_counts_the_calls_this_design_removes():
    baseline = ctx.naive_baseline(full_prompt_tokens=10_000, iterations=3,
                                  run_seconds=600)
    assert baseline.detail["planner_calls"] == 1
    assert baseline.detail["builder_calls"] == 3
    assert baseline.detail["evaluator_calls"] == 3
    assert baseline.detail["pm_poll_calls"] == 10, "one model call a minute, saying no"
    assert baseline.llm_calls == 1 + 3 + 3 + 10


# ---------------------------------------------------------------------------
# 7. the engine
# ---------------------------------------------------------------------------

def _engine(store, repo, runner=None, checks=passing_checks, **kwargs):
    return HarnessEngine(store, runner=runner or ScriptedRunner(), repo_root=str(repo),
                         check_runner=checks, **kwargs)


def test_a_light_run_costs_exactly_one_llm_call(store, repo):
    """The Planner is a template substitution and the Evaluator is a shell
    exit status. Only the Builder is reasoning, so only the Builder is paid."""
    runner = ScriptedRunner()
    engine = _engine(store, repo, runner)
    run, _ = engine.start(task_id="VIS-1", prompt="pad the header", title="Header padding",
                          project_id="demo", acceptance=["the header has 16px padding"],
                          checks=["true"], changed_paths=["src/header.css"])
    assert run.mode == policy.LIGHT
    engine.drive(run.id)

    assert store.require_run(run.id).stage == state.MERGE_READY
    assert runner.roles() == [policy.BUILDER]
    efficiency = store.efficiency(run.id)
    assert efficiency["llm_calls"] == 1
    assert efficiency["planner_skipped"] == 1
    assert efficiency["evaluator_skipped"] == 1


def test_a_failing_check_revises_and_never_asks_a_human(store, repo):
    runner = ScriptedRunner()
    engine = _engine(store, repo, runner, checks=flaky_checks(1))
    run, _ = engine.start(task_id="MOB-2", prompt="build the commute card",
                          title="Commute card", project_id="demo",
                          acceptance=["the card shows an ETA"], checks=["npm test"],
                          changed_paths=["src"])
    stages = [outcome.to_stage for outcome in engine.drive(run.id)]

    assert state.REVISING in stages
    assert state.BLOCKED not in stages
    assert store.require_run(run.id).stage == state.MERGE_READY
    assert store.list_decisions(status="open", run_id=run.id) == []


def test_a_failing_check_costs_no_evaluator_call_at_all(store, repo):
    """A declared check exiting non-zero is not a judgement call."""
    runner = ScriptedRunner()
    engine = _engine(store, repo, runner, checks=failing_checks)
    run, _ = engine.start(task_id="MOB-3", prompt="x", title="x", project_id="demo",
                          acceptance=["a"], checks=["npm test"], changed_paths=["src"],
                          requested_mode=policy.CRITICAL)
    engine.step(run.id)  # plan
    engine.step(run.id)  # open iteration
    engine.step(run.id)  # build
    engine.step(run.id)  # evaluate
    assert policy.EVALUATOR not in runner.roles()
    assert store.efficiency(run.id)["evaluator_skipped"] >= 1


def test_the_revision_reuses_the_session_and_sends_a_delta(store, repo):
    runner = ScriptedRunner()
    engine = _engine(store, repo, runner, checks=flaky_checks(1))
    run, _ = engine.start(task_id="MOB-4", prompt="build the card", title="Card",
                          project_id="demo", acceptance=["the card shows an ETA"],
                          checks=["npm test"], changed_paths=["src"])
    engine.drive(run.id)

    builds = [call for call in runner.calls if call["role"] == policy.BUILDER]
    assert len(builds) == 2
    assert builds[0]["delta"] is False and builds[0]["session_id"] is None
    assert builds[1]["delta"] is True and builds[1]["session_id"] is not None
    assert builds[1]["tokens"] < builds[0]["tokens"]
    efficiency = store.efficiency(run.id)
    assert efficiency["session_reused"] == 1
    assert efficiency["delta_prompts"] == 1 and efficiency["full_prompts"] == 1


def test_critical_pays_for_an_independent_evaluator_in_a_fresh_session(store, repo):
    contract_holder = {}

    def evaluator_answer(run, iteration):
        contract = contract_holder["contract"]
        return AgentResult(payload={"result": "pass", "summary": "verified", "criteria": [
            {"criterion_id": cid, "result": "pass", "evidence": "screenshot attached"}
            for cid in contract.criterion_ids()]},
            session_id="evaluator-session", agent="evaluator-agent")

    runner = ScriptedRunner([
        AgentResult(payload={"scope": "rotate the session token",
                             "functional_acceptance": ["tokens rotate on login"],
                             "required_checks": ["pytest -q"]},
                    agent="planner-agent"),
        AgentResult(payload={"ok": True}, session_id="builder-session",
                    agent="builder-agent", commit="abc123", context_percent=30.0),
        evaluator_answer,
    ])
    engine = _engine(store, repo, runner)
    run, _ = engine.start(task_id="SEC-1", prompt="rotate the session auth token",
                          title="Token rotation", project_id="demo", changed_paths=["src"])
    assert run.mode == policy.CRITICAL

    engine.step(run.id)   # plan (a real Planner call: CRITICAL always plans)
    contract_holder["contract"] = store.get_contract(run.id)
    engine.step(run.id)   # open iteration
    engine.step(run.id)   # build
    engine.step(run.id)   # evaluate

    assert runner.roles() == [policy.PLANNER, policy.BUILDER, policy.EVALUATOR]
    evaluator_call = runner.calls[-1]
    assert evaluator_call["session_id"] is None, \
        "a CRITICAL evaluator must not inherit the builder's session"
    assert "builder" not in evaluator_call["text"].lower().split("evaluator")[0]
    assert store.require_run(run.id).stage == state.MERGE_READY


def test_the_planner_may_add_criteria_but_never_drop_the_declared_ones(store, repo):
    runner = ScriptedRunner([
        AgentResult(payload={"scope": "rotate tokens",
                             "functional_acceptance": ["a new criterion the planner added"],
                             "required_checks": ["pytest -q"]}, agent="planner-agent"),
    ])
    engine = _engine(store, repo, runner)
    run, _ = engine.start(task_id="SEC-2", prompt="rotate the auth token",
                          title="Rotate", project_id="demo",
                          acceptance=["the declared criterion from the task"],
                          checks=["npm test"], changed_paths=["src"])
    engine.step(run.id)
    contract = store.get_contract(run.id)
    assert "the declared criterion from the task" in contract.functional_acceptance
    assert "a new criterion the planner added" in contract.functional_acceptance
    assert "npm test" in contract.required_checks


def test_a_malformed_verdict_is_an_infra_failure_not_a_product_failure(store, repo):
    """The code is not wrong because the evaluator answered badly. Burning a
    product iteration on it would spend a revision fixing nothing."""
    runner = ScriptedRunner([
        AgentResult(payload={"scope": "s", "functional_acceptance": ["a"],
                             "required_checks": ["true"]}, agent="planner"),
        AgentResult(payload={"ok": True}, session_id="b1", agent="builder", commit="c1"),
        AgentResult(payload="looks good", agent="evaluator"),
    ])
    engine = _engine(store, repo, runner)
    run, _ = engine.start(task_id="SEC-3", prompt="change the auth flow", title="Auth",
                          project_id="demo", changed_paths=["src"])
    engine.step(run.id); engine.step(run.id); engine.step(run.id)
    outcome = engine.step(run.id)

    assert outcome.to_stage == state.FAILED_INFRA
    assert store.require_run(run.id).current_iteration == 1, \
        "an evaluator glitch must not consume a product iteration"


def test_resume_returns_to_the_same_stage_iteration_and_worktree(store, repo):
    runner = ScriptedRunner()
    engine = _engine(store, repo, runner)
    run, _ = engine.start(task_id="MOB-5", prompt="build it", title="Build",
                          project_id="demo", acceptance=["it builds"], checks=["true"],
                          changed_paths=["src"])
    engine.step(run.id)
    engine.step(run.id)
    store.patch_run(run.id, worktree_path="/tmp/wt/mob-5", branch="feat/mob-5")
    engine.step(run.id)  # build -> EVALUATING, with a checkpoint written
    before = store.require_run(run.id)

    engine.record_infra_failure(run.id, "the node agent went away")
    assert store.require_run(run.id).stage == state.FAILED_INFRA

    outcome = engine.resume(run.id)
    after = store.require_run(run.id)
    assert outcome.to_stage == before.stage
    assert after.current_iteration == before.current_iteration
    assert after.worktree_path == "/tmp/wt/mob-5"
    assert after.branch == "feat/mob-5"


def test_three_infra_failures_become_a_human_question(store, repo):
    engine = _engine(store, repo)
    run, _ = engine.start(task_id="MOB-6", prompt="x", title="x", project_id="demo",
                          acceptance=["a"], checks=["true"], changed_paths=["src"])
    engine.step(run.id)
    for _ in range(policy.INFRA_FAILURE_ESCALATION_THRESHOLD - 1):
        engine.record_infra_failure(run.id, "node offline")
        engine.resume(run.id)
    outcome = engine.record_infra_failure(run.id, "node offline")

    assert outcome.to_stage == state.BLOCKED
    reasons = [d["reason"] for d in store.list_decisions(run_id=run.id)]
    assert policy.REPEATED_INFRA_FAILURE in reasons


def test_the_iteration_cap_is_the_only_verdict_path_to_a_human(store, repo):
    engine = _engine(store, repo, checks=failing_checks)
    run, _ = engine.start(task_id="MOB-7", prompt="x", title="x", project_id="demo",
                          acceptance=["a"], checks=["npm test"], changed_paths=["src"])
    engine.drive(run.id, max_steps=40)
    final = store.require_run(run.id)

    assert final.stage == state.BLOCKED
    assert final.current_iteration == final.max_iterations
    decisions = store.list_decisions(run_id=run.id)
    assert [d["reason"] for d in decisions] == [policy.MAX_ITERATIONS_REACHED]
    assert "converge" in decisions[0]["question"]


def test_shadow_authority_writes_nothing_outside_the_harness_tables(store, repo):
    projected: list[tuple[str, str]] = []
    engine = _engine(store, repo,
                     queue_projector=lambda run, label: projected.append((run.id, label)))
    run, _ = engine.start(task_id="SHDW-1", prompt="x", title="x", project_id="demo",
                          acceptance=["a"], checks=["true"], changed_paths=["src"],
                          write_authority=policy.SHADOW)
    engine.drive(run.id)

    assert projected == [], "SHADOW must not project onto the queue task"
    assert store.require_run(run.id).shadow_of_task_status == "MERGE_READY"


def test_supervised_authority_projects_the_label_the_board_renders(store, repo):
    projected: list[tuple[str, str]] = []
    engine = _engine(store, repo,
                     queue_projector=lambda run, label: projected.append((run.id, label)))
    run, _ = engine.start(task_id="SUP-1", prompt="x", title="x", project_id="demo",
                          acceptance=["a"], checks=["true"], changed_paths=["src"],
                          write_authority=policy.SUPERVISED)
    engine.drive(run.id)

    labels = [label for _run_id, label in projected]
    assert labels[-1] == "MERGE_READY"
    assert set(labels) <= set(state.PROJECTED_STAGES)


def test_supervised_may_not_self_approve_a_merge(store, repo):
    from terminal_mcp.harness_engine import HarnessError
    engine = _engine(store, repo)
    run, _ = engine.start(task_id="SUP-2", prompt="x", title="x", project_id="demo",
                          acceptance=["a"], checks=["true"], changed_paths=["src"],
                          write_authority=policy.SUPERVISED)
    engine.drive(run.id)
    with pytest.raises(HarnessError):
        engine.approve_merge(run.id, approved_by="engine")
    outcome = engine.approve_merge(run.id, approved_by="hung")
    assert outcome.to_stage == state.MERGED


# ---------------------------------------------------------------------------
# 8. checkpointed session replacement
# ---------------------------------------------------------------------------

def test_a_replacement_builder_continues_the_same_run_iteration_and_worktree(store, repo):
    """The proof that a lost session is not a lost task.

    Everything that says WHERE the work is -- run, iteration, branch,
    worktree, contract -- is unchanged; only the session id moves.
    """
    runner = ScriptedRunner()
    engine = _engine(store, repo, runner, checks=flaky_checks(1))
    run, _ = engine.start(task_id="MOB-8", prompt="build the card", title="Card",
                          project_id="demo", acceptance=["the card shows an ETA"],
                          checks=["npm test"], changed_paths=["src"])
    engine.step(run.id)
    engine.step(run.id)
    store.patch_run(run.id, worktree_path="/tmp/wt/mob-8", branch="feat/mob-8")
    engine.step(run.id)   # build (session opened, checkpoint written)
    engine.step(run.id)   # evaluate -> REVISING

    before = store.require_run(run.id)
    handover = engine.replace_builder_session(run.id, new_session_id="builder-2",
                                              reason="context rollover at 97%")
    after = store.require_run(run.id)

    assert handover["previous_session"] != "builder-2"
    assert after.id == before.id
    assert after.current_iteration == before.current_iteration
    assert after.worktree_path == before.worktree_path == "/tmp/wt/mob-8"
    assert after.branch == before.branch == "feat/mob-8"
    assert after.contract_hash == before.contract_hash
    assert after.builder_session_id == "builder-2"

    events = [e for e in store.events(run.id)
              if e["event_type"] == "BUILDER_SESSION_REPLACED"]
    assert len(events) == 1
    assert events[0]["metadata"]["worktree_path"] == "/tmp/wt/mob-8"

    # The replacement gets a FULL prompt plus the checkpoint -- it has no
    # history for a delta to refer to.
    engine.step(run.id)   # REVISING -> BUILDING
    engine.step(run.id)   # build with the replacement session
    last_build = [c for c in runner.calls if c["role"] == policy.BUILDER][-1]
    assert last_build["delta"] is False
    assert "You are continuing an interrupted build" in last_build["text"]
    assert "/tmp/wt/mob-8" in last_build["text"]
    assert store.efficiency(run.id)["rollover_count"] == 1


def test_a_checkpoint_records_where_the_work_is_not_just_that_it_happened(store):
    run, _ = store.create_run(task_id="t", prompt="p")
    checkpoint = store.save_checkpoint(
        run.id, task_id="t", iteration=2, branch="feat/x",
        commit_sha="abc123", worktree_path="/tmp/wt/x", session_id="s1",
        checks=["npm test"], remaining=["wire the ETA"], note="half done")
    assert checkpoint["worktree_path"] == "/tmp/wt/x"
    assert checkpoint["branch"] == "feat/x"
    assert checkpoint["remaining"] == ["wire the ETA"]
    assert store.require_run(run.id).last_checkpoint_id == checkpoint["id"]


# ---------------------------------------------------------------------------
# 9. the absences that make it cheap
# ---------------------------------------------------------------------------

def test_the_engine_contains_no_loop_no_sleep_and_no_thread():
    """The continuous-PM defect, asserted structurally.

    A polling loop inside the engine runs whether or not anything happened.
    The old one woke a model once a minute; over a day that is 1,440 calls
    whose entire output is "nothing changed". `step()` is called by whoever
    has a reason to believe something changed, and by nobody else.
    """
    import ast
    import inspect

    from terminal_mcp import harness_engine

    # Parsed, not grepped: this module's own docstring explains at length that
    # it contains no `while True`, and a string scan would fail on the
    # explanation rather than on any code.
    tree = ast.parse(inspect.getsource(harness_engine))
    for node in ast.walk(tree):
        if isinstance(node, ast.While):
            assert not (isinstance(node.test, ast.Constant) and node.test.value), \
                "`while True` reintroduces a polling loop"
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            assert name not in ("time.sleep", "asyncio.sleep", "threading.Thread",
                                "threading.Timer"), f"{name} reintroduces a timer"
    imported = {alias.name for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    assert "threading" not in imported and "time" not in imported


def test_real_checks_run_and_report_their_exit_status(tmp_path):
    """The evaluator substitute is a real subprocess, not a simulation."""
    results = run_checks(["true", "false", "echo hello"], cwd=str(tmp_path))
    assert [r.exit_code for r in results] == [0, 1, 0]
    assert results[2].output == "hello"
    assert [r.ok for r in results] == [True, False, True]


def test_every_check_runs_even_after_one_fails(tmp_path):
    """Three failures found together cost one revision; found one at a time
    they cost three."""
    results = run_checks(["false", "false", "false"], cwd=str(tmp_path))
    assert len(results) == 3


def test_definition_hash_is_stable_and_scoped():
    first = definition_hash(project_id="P", task_id="T", mode="light", prompt="p",
                            acceptance=["a"], checks=["c"])
    assert first == definition_hash(project_id="P", task_id="T", mode="light",
                                    prompt="p", acceptance=["a"], checks=["c"])
    assert first != definition_hash(project_id="P", task_id="T", mode="critical",
                                    prompt="p", acceptance=["a"], checks=["c"])
