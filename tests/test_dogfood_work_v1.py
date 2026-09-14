"""Dogfood: plan a real BUG and a real FEATURE of THIS repo, end to end.

WHY A TEST AND NOT A SCRIPT

The claim the Work runtime makes is "a worker no longer re-derives the analysis
from an empty repository". That claim is only worth anything if it is checked
the same way everything else is -- on every run, against the real repo, with
the pipeline that actually ships. A one-off script proves it worked once on
someone's laptop.

WHAT IS MEASURED, AND WHAT IS DELIBERATELY NOT

Measured, because they are counted: how many files the handoff points a worker
at, how many candidates the reuse search found, how many redefine rounds the
spec needed, and how that file count compares with the whole package. That last
ratio is the honest version of "narrowing": it is a count of files, not a guess
about tokens.

NOT measured, and never asserted: token savings. No provider token count is
available in this process, and a number invented here would be indistinguishable
from a measured one -- which would make every efficiency claim in the system
untrustworthy, including the true ones. `work_telemetry` reports tokens with
their provenance, or reports them unavailable. This file does neither by
pretending.

BOTH SCENARIOS ARE REAL

The BUG is the Work-UI occupancy defect that actually shipped: `workers()`
derived occupancy solely from the queue's current_task, so a `-work` session
running Claude with no queued task reported IDLE and looked free to dispatch
into. The FEATURE is a small one this repo genuinely lacks.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from terminal_mcp import work_planning as wp
from terminal_mcp import work_spec as ws

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def store(tmp_path):
    """A fresh spec store per test: the dogfood must not depend on, or
    pollute, whatever the developer's real store happens to hold."""
    return ws.WorkSpecStore(tmp_path / "dogfood_specs.db")


@pytest.fixture
def knowledge():
    """The repo's REAL knowledge map if it has one, otherwise None.

    Not faked. A dogfood that substitutes a convenient map proves the fake
    works. If the map is absent the pipeline is supposed to degrade, and these
    tests assert that it does rather than skipping.
    """
    try:
        from terminal_mcp.project_knowledge import ProjectKnowledge, canonical_root

        root = canonical_root(str(REPO_ROOT))
        return ProjectKnowledge(root) if root else None
    except Exception:  # noqa: BLE001 -- absence is a valid state to test
        return None


def _package_file_count() -> int:
    return len(list((REPO_ROOT / "terminal_mcp").glob("*.py")))


# -- scenario 1: a real BUG ----------------------------------------------------------

BUG_REPORT = (
    "The Work page shows a -work session as IDLE while Claude is actually "
    "running in it, so it looks free to dispatch into and a prompt would land "
    "on a live conversation."
)


def test_dogfood_bug_is_classified_planned_and_refused_until_it_is_executable(
        store, knowledge):
    result = wp.plan(BUG_REPORT, store=store, knowledge=knowledge,
                     cwd=str(REPO_ROOT))

    assert result.spec.task_type == ws.BUG, "a defect report must classify as a BUG"
    assert [s.name for s in result.stages] == list(wp.STAGE_ORDER)
    # The report alone is not an executable spec, and saying so is the point:
    # a worker handed this would have to re-derive everything it lacks.
    assert result.status == ws.NEEDS_REDEFINE
    assert result.gate_report["handoff"] is None
    assert result.spec.redefine_reason, "the refusal has to persist, not just return"


def test_dogfood_bug_reaches_ready_and_hands_over_a_bounded_worklist(store, knowledge):
    planned = wp.plan(BUG_REPORT, store=store, knowledge=knowledge, cwd=str(REPO_ROOT))

    # The evidence a planner would actually have gathered for this defect --
    # the real files, the real acceptance criterion.
    resumed = wp.redefine(store, planned.spec.spec_id, {
        "symptom": "a -work session running Claude is shown as IDLE on the Work page",
        "expected_behavior": "it shows BUSY whenever something is running in it",
        "likely_module": "work_service",
        "likely_files": ["terminal_mcp/work_service.py", "terminal_mcp/dashboard.py"],
        "entry_points": ["WorkService.workers"],
        "search_terms": ["current_task", "IDLE"],
        "relevant_flow": "dashboard route -> WorkService.workers -> queue.status",
        "hypothesis": ("occupancy is derived only from the queue's current_task, so a "
                       "session busy outside the queue reports IDLE"),
        "fix_strategy": ["read session state/current_command as a second signal"],
        "do_not_touch": ["work_eligibility.py -- permission is a separate question"],
        "test_plan": ["a -work session running claude with no queue task is not IDLE"],
        "test_runbook": "test_gate",
        "acceptance_criteria": [
            "the Work page shows BUSY for a -work session running an agent, "
            "queue assignment or not"],
    })

    assert resumed.status == ws.SPEC_READY, resumed.gate_report["missing"]
    handoff = resumed.gate_report["handoff"]

    # The whole claim, made checkable: the worker is pointed at a handful of
    # files rather than at the repository.
    named = handoff["WHERE"]["files"]
    assert named, "a READY spec must tell the worker where to look"
    assert len(named) <= 5
    assert len(named) < _package_file_count() / 10, (
        f"the handoff names {len(named)} files out of {_package_file_count()} in the "
        f"package; that is not narrowing")
    assert handoff["BUDGET"]["files"] > 0
    assert "NEEDS_REDEFINE" in handoff["BUDGET"]["on_exceed"]


# -- scenario 2: a real FEATURE ------------------------------------------------------

FEATURE_REQUEST = (
    "Add a work_spec_export tool that returns one spec as a single markdown "
    "document, so a spec can be pasted into a review without the reviewer "
    "calling five separate tools."
)


def test_dogfood_feature_classifies_as_a_feature_and_is_bounded_before_it_is_ready(
        store, knowledge):
    result = wp.plan(FEATURE_REQUEST, store=store, knowledge=knowledge,
                     cwd=str(REPO_ROOT))

    assert result.spec.task_type == ws.FEATURE_NEW
    assert result.status == ws.NEEDS_REDEFINE
    # The fields a feature cannot be executed without, and a bare request never
    # supplies. This is the difference from bug_spec, which would have asked
    # this feature for a root cause.
    blocking = result.gate_report["mandatory_missing"]
    assert "out_of_scope" in blocking
    assert "acceptance_criteria" in blocking
    questions = result.gate_report["reply_to_planner"]["QUESTIONS_FOR_PLANNER"]
    assert questions and all(q.endswith("?") for q in questions)


def test_dogfood_feature_runs_the_reuse_search_before_anything_is_built(store, knowledge):
    """The expensive failure on a feature is a second implementation. The
    verdict must be recorded with what was searched, so NEW is a finding."""
    result = wp.plan(FEATURE_REQUEST, store=store, knowledge=knowledge,
                     cwd=str(REPO_ROOT))

    reuse_stage = next(s for s in result.stages if s.name == wp.REUSE)
    assert reuse_stage.findings, "the reuse verdict must be recorded"
    assert reuse_stage.detail["searched"], "a verdict with no search behind it is not one"
    assert result.spec.reuse_decisions
    assert any(v in result.spec.reuse_decisions[0] for v in ("REUSE", "EXTEND", "NEW"))


def test_dogfood_feature_finds_its_own_prior_spec_on_a_second_pass(store, knowledge):
    """Planning a near-identical feature twice must surface the first one --
    this is the mechanism that stops the second implementation getting built.
    """
    first = wp.plan(FEATURE_REQUEST, store=store, knowledge=knowledge,
                    cwd=str(REPO_ROOT))
    first.spec.likely_module = "work_spec"
    first.spec.likely_files = ("terminal_mcp/work_spec.py",)
    store.save(first.spec)

    second = wp.plan(
        "Add a tool that renders a work spec as one markdown document for review",
        store=store, knowledge=knowledge, cwd=str(REPO_ROOT))

    assert second.counters["similar_hits"] >= 1, \
        "a near-identical prior spec must be found before anything is rebuilt"
    similar_stage = next(s for s in second.stages if s.name == wp.SIMILAR)
    assert similar_stage.findings


def test_dogfood_feature_reaches_ready_with_a_contract_and_an_acceptance(store, knowledge):
    planned = wp.plan(FEATURE_REQUEST, store=store, knowledge=knowledge,
                      cwd=str(REPO_ROOT))

    resumed = wp.redefine(store, planned.spec.spec_id, {
        "problem": ("reviewing a spec means calling work_spec_get plus the gate and "
                    "reading raw JSON"),
        "user_value": "a reviewer reads one document instead of assembling five calls",
        "expected_outcome": "work_spec_export returns the spec as one markdown string",
        "scope": ["a render function on WorkSpec", "one MCP tool that returns it"],
        "out_of_scope": ["HTML export", "a UI page", "exporting more than one spec"],
        "arch_impact": "no new module; a method plus a tool beside the existing ones",
        "reuse_candidates": ["WorkSpec.handoff() already shapes the compact view",
                             "context_pack.ContextPack.render() is the rendering precedent"],
        "existing_patterns": ["MCP tools return dicts and never raise; errors are "
                              "returned as {'error': ...}"],
        "implementation_plan": ["add WorkSpec.render()", "add the work_spec_export tool",
                                "test the tool over the real MCP call path"],
        "likely_files": ["terminal_mcp/work_spec.py", "terminal_mcp/mcp_app.py"],
        "api_contract": "work_spec_export(spec_id) -> {'markdown': str} | {'error': ...}",
        "test_plan": ["the export contains every mandatory field for the spec's type"],
        "test_runbook": "test_gate",
        "acceptance_criteria": [
            "calling work_spec_export on a READY spec returns markdown containing its "
            "requirement, scope, out-of-scope and acceptance criteria"],
        "dependencies": [],
        "risks": ["a very large spec could produce an unwieldy document"],
    })

    assert resumed.status == ws.SPEC_READY, resumed.gate_report["missing"]
    handoff = resumed.gate_report["handoff"]
    assert handoff["OUT_OF_SCOPE"], "a feature without a boundary is not finishable"
    assert handoff["REUSE_FIRST"], "the worker is told what to reuse before building"
    assert handoff["IMPLEMENTATION_PLAN"]
    assert len(handoff["WHERE"]["files"]) < _package_file_count() / 10


# -- what the dogfood is allowed to claim ----------------------------------------------

def test_the_pipeline_publishes_counts_and_never_a_token_estimate(store, knowledge):
    """The guardrail on this system's own honesty.

    An invented token number would be indistinguishable from a measured one,
    which would make every efficiency claim untrustworthy -- including the
    claims that happen to be true.
    """
    result = wp.plan(FEATURE_REQUEST, store=store, knowledge=knowledge,
                     cwd=str(REPO_ROOT))
    payload = result.as_dict()

    assert all(isinstance(v, int) for v in payload["counters"].values())
    assert not any("token" in key.lower() for key in payload["counters"])
    assert "token" not in str(payload["stages"]).lower()


def test_the_pipeline_records_the_commit_it_planned_against(store, knowledge):
    """Without it, a spec cannot be checked for drift later -- the map may have
    moved under the plan between planning and execution."""
    result = wp.plan(BUG_REPORT, store=store, knowledge=knowledge, cwd=str(REPO_ROOT))
    assert result.spec.source_commit, "planning inside a git repo must record HEAD"


@pytest.mark.skipif(not os.environ.get("TERMINAL_MCP_DOGFOOD_STRICT"),
                    reason="only meaningful once the repo has an indexed knowledge map")
def test_dogfood_uses_the_real_knowledge_map_when_one_exists(store, knowledge):
    """Opt-in, because a fresh clone has no map and failing there would say
    nothing about the pipeline. With TERMINAL_MCP_DOGFOOD_STRICT set, it
    asserts the map is actually being consulted rather than quietly skipped.
    """
    assert knowledge is not None, "strict dogfood requires an indexed knowledge map"
    result = wp.plan(BUG_REPORT, store=store, knowledge=knowledge, cwd=str(REPO_ROOT))
    assert result.counters["knowledge_hits"] > 0
