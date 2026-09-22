"""Analysis Gate unit tests (docs/AI_ANALYSIS_GATE.md, §20.6 Phase F).

Pure-function tests only -- no queue, no store, no session. The
queue/coordinator integration and the backward-compatibility guarantees
live in tests/test_analysis_gate_queue.py.
"""
from __future__ import annotations

import pytest

from terminal_mcp.analysis_gate import (
    ADVISORY, ENFORCE, GATE_VERSION, NEEDS_CLARIFICATION, OFF,
    PROFILE_FAST_FIX, PROFILE_FULL, PROFILE_NONE, READY,
    AnalysisGatePolicy, check_analysis_gate, extract_analysis, resolve_profile,
)


def _full_contract(**overrides):
    contract = {
        "profile": "full",
        "problem_statement": "coordinator dispatches tasks nobody analysed",
        "user_observable_goal": "an unanalysed feature task never reaches READY",
        "source_of_truth": "coordinator.py review() -- the only PRECHECK->READY edge",
        "evidence": ["read coordinator.py:359-620", "ran pytest tests/test_coordinator.py"],
        "invariants": ["legacy tasks keep dispatching exactly as before"],
        "assumptions": [],
        "acceptance_tests": ["gated task without a contract is BLOCKED"],
        "live_verification": "drive a real disposable task through engine.tick()",
    }
    contract.update(overrides)
    return contract


def _task(analysis=None, metadata=None):
    return {"analysis": analysis or {}, "metadata": metadata or {}}


# -- profile resolution / legacy safety ---------------------------------

def test_unclassified_task_is_not_gated_by_default():
    """The legacy guarantee: a task created before this gate existed has
    no analysis and no task_class, and must pass untouched."""
    result = check_analysis_gate(_task())
    assert result["status"] == READY
    assert result["profile"] == PROFILE_NONE
    assert result["enforced"] is False
    assert "legacy/unclassified" in result["reason"]


def test_unclassified_task_is_gated_when_classification_is_required():
    policy = AnalysisGatePolicy(require_classification=True)
    result = check_analysis_gate(_task(), policy=policy)
    assert result["status"] == NEEDS_CLARIFICATION
    assert result["profile"] == PROFILE_FULL


@pytest.mark.parametrize("task_class,expected", [
    ("implementation", PROFILE_FULL), ("feature", PROFILE_FULL), ("fix", PROFILE_FULL),
    ("bugfix", PROFILE_FULL), ("refactor", PROFILE_FULL), ("migration", PROFILE_FULL),
    ("fast_fix", PROFILE_FAST_FIX), ("hotfix", PROFILE_FAST_FIX),
    ("chore", PROFILE_NONE), ("docs", PROFILE_NONE), ("incident", PROFILE_NONE),
])
def test_task_class_maps_to_profile(task_class, expected):
    assert resolve_profile(_task(metadata={"task_class": task_class})) == expected


def test_legacy_type_key_is_honoured_as_well_as_task_class():
    assert resolve_profile(_task(metadata={"type": "feature"})) == PROFILE_FULL


def test_explicit_profile_beats_inferred_class():
    task = _task(analysis={"profile": "fast_fix"}, metadata={"task_class": "feature"})
    assert resolve_profile(task) == PROFILE_FAST_FIX


def test_unrecognised_profile_escalates_rather_than_bypassing():
    """A typo'd profile must never become a way out of the gate."""
    task = _task(analysis={"profile": "ful"}, metadata={"task_class": "feature"})
    assert resolve_profile(task) == PROFILE_FULL
    result = check_analysis_gate(task)
    assert result["status"] == NEEDS_CLARIFICATION
    assert any("profile" in str(field) for field in result["missing_fields"])


def test_analysis_is_read_from_metadata_when_no_column_value():
    task = {"metadata": {"analysis": _full_contract(), "task_class": "feature"}}
    assert extract_analysis(task)["problem_statement"]
    assert check_analysis_gate(task)["status"] == READY


# -- full profile -------------------------------------------------------

def test_complete_full_contract_is_ready():
    result = check_analysis_gate(_task(analysis=_full_contract(), metadata={"task_class": "feature"}))
    assert result["status"] == READY
    assert result["profile"] == PROFILE_FULL
    assert result["enforced"] is True
    assert result["gate_version"] == GATE_VERSION
    assert result["missing_fields"] == []


@pytest.mark.parametrize("field", [
    "problem_statement", "user_observable_goal", "source_of_truth",
    "evidence", "invariants", "acceptance_tests", "live_verification",
])
def test_each_required_full_field_blocks_when_missing(field):
    contract = _full_contract()
    del contract[field]
    result = check_analysis_gate(_task(analysis=contract, metadata={"task_class": "feature"}))
    assert result["status"] == NEEDS_CLARIFICATION
    assert field in result["missing_fields"]


@pytest.mark.parametrize("empty", ["", "   ", [], {}])
def test_an_empty_field_counts_as_missing_not_declared(empty):
    """"Filled the shape in but said nothing" is the exact failure this
    gate exists to catch."""
    result = check_analysis_gate(
        _task(analysis=_full_contract(problem_statement=empty), metadata={"task_class": "feature"}))
    assert result["status"] == NEEDS_CLARIFICATION
    assert "problem_statement" in result["missing_fields"]


def test_assumptions_must_be_declared_explicitly_on_the_full_profile():
    contract = _full_contract()
    del contract["assumptions"]
    result = check_analysis_gate(_task(analysis=contract, metadata={"task_class": "feature"}))
    assert result["status"] == NEEDS_CLARIFICATION
    assert any("assumptions" in str(f) for f in result["missing_fields"])


# -- assumptions: the rule that matters most ----------------------------

def _assumption(**overrides):
    item = {"statement": "inbound orders always carry a numeric order_number",
            "confidence": "HIGH", "impact": "HIGH", "status": "OPEN"}
    item.update(overrides)
    return item


@pytest.mark.parametrize("impact", ["HIGH", "CRITICAL"])
def test_unresolved_high_impact_assumption_blocks_ready(impact):
    result = check_analysis_gate(_task(
        analysis=_full_contract(assumptions=[_assumption(impact=impact)]),
        metadata={"task_class": "feature"}))
    assert result["status"] == NEEDS_CLARIFICATION
    assert len(result["unresolved_assumptions"]) == 1
    assert result["unresolved_assumptions"][0]["impact"] == impact
    assert "unresolved high-impact assumption" in result["reason"]


def test_resolved_high_impact_assumption_allows_ready():
    resolved = _assumption(status="RESOLVED",
                           resolution="queried the live API: order_number is a string, handled")
    result = check_analysis_gate(_task(
        analysis=_full_contract(assumptions=[resolved]), metadata={"task_class": "feature"}))
    assert result["status"] == READY
    assert result["unresolved_assumptions"] == []


def test_resolved_without_a_resolution_does_not_resolve_anything():
    """Ticking the box to get past the gate is refused."""
    result = check_analysis_gate(_task(
        analysis=_full_contract(assumptions=[_assumption(status="RESOLVED")]),
        metadata={"task_class": "feature"}))
    assert result["status"] == NEEDS_CLARIFICATION
    assert result["unresolved_assumptions"][0]["has_resolution"] is False


def test_low_impact_unresolved_assumption_does_not_block():
    result = check_analysis_gate(_task(
        analysis=_full_contract(assumptions=[_assumption(impact="LOW", confidence="LOW")]),
        metadata={"task_class": "feature"}))
    assert result["status"] == READY


def test_high_confidence_does_not_unblock_a_high_impact_assumption():
    """Impact decides, never confidence -- a confident guess is a guess."""
    result = check_analysis_gate(_task(
        analysis=_full_contract(assumptions=[_assumption(confidence="HIGH", impact="CRITICAL")]),
        metadata={"task_class": "feature"}))
    assert result["status"] == NEEDS_CLARIFICATION


def test_assumption_with_no_declared_impact_is_treated_as_blocking():
    item = {"statement": "probably fine", "confidence": "MEDIUM", "status": "OPEN"}
    result = check_analysis_gate(_task(
        analysis=_full_contract(assumptions=[item]), metadata={"task_class": "feature"}))
    assert result["status"] == NEEDS_CLARIFICATION
    assert result["unresolved_assumptions"][0]["impact"] is None


def test_malformed_assumption_is_reported_not_ignored():
    result = check_analysis_gate(_task(
        analysis=_full_contract(assumptions=["just a string"]), metadata={"task_class": "feature"}))
    assert result["status"] == NEEDS_CLARIFICATION
    assert any("assumptions[0]" in str(f) for f in result["missing_fields"])


# -- critic review ------------------------------------------------------

@pytest.mark.parametrize("category", [
    "state-machine", "workflow", "auth", "security", "deploy",
    "data-model", "multi-agent", "automation", "destructive",
])
def test_critic_result_is_required_for_each_critic_category(category):
    result = check_analysis_gate(_task(
        analysis=_full_contract(categories=[category]), metadata={"task_class": "feature"}))
    assert result["status"] == NEEDS_CLARIFICATION
    assert result["critic_required_categories"] == [category]
    assert any("critic_result" in str(f) for f in result["missing_fields"])


def test_critic_result_present_satisfies_the_category_requirement():
    result = check_analysis_gate(_task(
        analysis=_full_contract(categories=["state-machine"],
                                critic_result="checked every VALID_TRANSITIONS edge; no new edge added"),
        metadata={"task_class": "feature"}))
    assert result["status"] == READY


def test_non_critic_category_needs_no_critic_result():
    result = check_analysis_gate(_task(
        analysis=_full_contract(categories=["ui-copy"]), metadata={"task_class": "feature"}))
    assert result["status"] == READY
    assert result["critic_required_categories"] == []


def test_categories_are_read_from_metadata_too():
    result = check_analysis_gate(_task(
        analysis=_full_contract(), metadata={"task_class": "feature", "categories": ["auth"]}))
    assert result["status"] == NEEDS_CLARIFICATION
    assert result["critic_required_categories"] == ["auth"]


# -- fast fix -----------------------------------------------------------

def _fast_fix(**overrides):
    contract = {
        "profile": "fast_fix",
        "reproduce": "reproduced on a real disposable lane, 3/3",
        "root_cause": "position was compared before the lane row existed",
        "expected_behavior": "task lands at the end of the target lane",
        "invariant": "no existing task changes position",
        "regression_test": "test_move_task_to_empty_lane",
        "verify_fix": "re-ran the real move on the disposable lane",
    }
    contract.update(overrides)
    return contract


def test_complete_fast_fix_contract_is_ready():
    result = check_analysis_gate(_task(analysis=_fast_fix(), metadata={"task_class": "fast_fix"}))
    assert result["status"] == READY
    assert result["profile"] == PROFILE_FAST_FIX


@pytest.mark.parametrize("field", [
    "reproduce", "root_cause", "expected_behavior", "invariant", "regression_test", "verify_fix",
])
def test_each_fast_fix_field_is_required(field):
    contract = _fast_fix()
    del contract[field]
    result = check_analysis_gate(_task(analysis=contract, metadata={"task_class": "fast_fix"}))
    assert result["status"] == NEEDS_CLARIFICATION
    assert field in result["missing_fields"]


def test_fast_fix_does_not_require_the_full_contract_fields():
    result = check_analysis_gate(_task(analysis=_fast_fix(), metadata={"task_class": "fast_fix"}))
    assert "problem_statement" not in result["missing_fields"]
    assert "live_verification" not in result["missing_fields"]


def test_fast_fix_still_blocks_on_a_high_impact_unresolved_assumption():
    result = check_analysis_gate(_task(
        analysis=_fast_fix(assumptions=[_assumption()]), metadata={"task_class": "fast_fix"}))
    assert result["status"] == NEEDS_CLARIFICATION


# -- machine-readable contract / policy ---------------------------------

def test_result_is_machine_readable_and_human_readable():
    result = check_analysis_gate(_task(analysis={"profile": "full"}, metadata={"task_class": "feature"}))
    for key in ("status", "profile", "gate_version", "enforcement", "enforced",
                "missing_fields", "unresolved_assumptions", "critic_required_categories", "reason"):
        assert key in result, key
    assert isinstance(result["missing_fields"], list)
    assert all(isinstance(item, str) for item in result["missing_fields"])
    assert isinstance(result["reason"], str) and result["reason"].strip()


def test_advisory_mode_reports_but_never_blocks():
    policy = AnalysisGatePolicy(enforcement=ADVISORY)
    result = check_analysis_gate(_task(metadata={"task_class": "feature"}), policy=policy)
    assert result["status"] == READY          # never blocks
    assert result["enforced"] is False
    assert result["advisory_status"] == NEEDS_CLARIFICATION
    assert result["missing_fields"]           # but still reports everything
    assert result["reason"].startswith("ADVISORY ONLY")


def test_off_mode_disables_the_gate_entirely():
    policy = AnalysisGatePolicy(enforcement=OFF)
    result = check_analysis_gate(_task(metadata={"task_class": "feature"}), policy=policy)
    assert result["status"] == READY
    assert result["missing_fields"] == []


def test_invalid_enforcement_is_refused_at_construction():
    with pytest.raises(ValueError):
        AnalysisGatePolicy(enforcement="sometimes")


def test_gate_never_raises_on_malformed_input():
    for task in ({}, {"analysis": None, "metadata": None}, {"analysis": "nope", "metadata": []},
                 {"metadata": {"task_class": 17}}, {"analysis": {"assumptions": {"a": 1}},
                                                    "metadata": {"task_class": "feature"}}):
        result = check_analysis_gate(task)
        assert result["status"] in (READY, NEEDS_CLARIFICATION)
        assert isinstance(result["reason"], str)


def test_default_policy_is_the_backward_compatible_one():
    from terminal_mcp.analysis_gate import DEFAULT_POLICY
    assert DEFAULT_POLICY.enforcement == ENFORCE
    assert DEFAULT_POLICY.require_classification is False
