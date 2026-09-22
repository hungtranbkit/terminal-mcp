"""Analysis Gate: what it blocks, what it lets through, and what it must not break.

The legacy-compatibility tests are not padding. The gate's whole deployability
rests on the claim that an existing run behaves exactly as it did before, and a
claim like that is worth an explicit test rather than a comment.
"""
from __future__ import annotations

import pytest

from terminal_mcp import work_analysis_gate as ag


# -- helpers -----------------------------------------------------------------

def _assumption(impact="low", resolution="resolved", confidence="high", **extra):
    base = {"id": "A1", "statement": "the node agent answers /v1/repo-evidence",
            "confidence": confidence, "impact": impact, "resolution": resolution}
    base.update(extra)
    return base


def _full(**overrides):
    """A complete, passing `full` profile. Tests override one field at a time so
    a failure names the field that caused it."""
    block = {
        "version": 1,
        "profile": ag.PROFILE_FULL,
        "problem_statement": "workers show IDLE while an agent is running",
        "user_observable_goal": "the Work screen never says idle for a busy terminal",
        "source_of_truth": "work_service.workers() + tmux pane_current_command",
        "state_model": {"n/a": True, "reason": "read-path only, adds no new states"},
        "invariants": ["a pane running an agent is never reported IDLE"],
        "assumptions": [_assumption()],
        "edge_cases": ["pane exists but command is a bare shell"],
        "dangerous_failure_modes": ["dispatching onto a lane a human is using"],
        "acceptance_tests": ["workers() returns RUNNING_MANUAL for a claude pane"],
        "live_verification": "read /dashboard/api/work/workers against gatefix2-work",
        "critic_result": "reviewed: no new state leaks into the dispatch path",
    }
    block.update(overrides)
    return {"analysis_gate": block}


def _fast_fix(**overrides):
    block = {
        "version": 1,
        "profile": ag.PROFILE_FAST_FIX,
        "reproduce": "open the Work screen with gatefix2-work running claude",
        "root_cause": "state derived from queue.current_task only",
        "expected_behavior": "busy-untracked is reported, not IDLE",
        "invariant": "absence of a queue task never implies an idle terminal",
        "regression_test": "tests/test_analysis_gate.py::test_fast_fix_profile_passes",
        "live_verify": "observed on m910 gatefix2-work",
    }
    block.update(overrides)
    return {"analysis_gate": block}


# -- backward compatibility --------------------------------------------------

def test_legacy_run_without_gate_is_not_judged():
    """A run created before this module existed must behave as it did."""
    verdict = ag.evaluate({"anything": "else"})
    assert verdict.allowed is True
    assert verdict.reason == ag.NOT_ENFORCED


def test_legacy_run_with_empty_metadata_is_not_judged():
    assert ag.evaluate({}).allowed is True
    assert ag.evaluate(None).allowed is True


def test_legacy_run_blocked_only_when_deployment_opts_in():
    verdict = ag.evaluate({}, require_gate=True)
    assert verdict.allowed is False
    assert verdict.reason == ag.MISSING_GATE


# -- the happy paths ---------------------------------------------------------

def test_full_profile_passes():
    verdict = ag.evaluate(_full())
    assert verdict.allowed is True, verdict.message()
    assert verdict.reason == ag.PASS
    assert verdict.profile == ag.PROFILE_FULL


def test_fast_fix_profile_passes():
    verdict = ag.evaluate(_fast_fix())
    assert verdict.allowed is True, verdict.message()
    assert verdict.profile == ag.PROFILE_FAST_FIX


def test_profile_defaults_to_full():
    block = _full()
    block["analysis_gate"].pop("profile")
    assert ag.evaluate(block).profile == ag.PROFILE_FULL


# -- missing fields ----------------------------------------------------------

@pytest.mark.parametrize("field", ag.FULL_REQUIRED_FIELDS)
def test_every_full_field_is_required(field):
    block = _full()
    block["analysis_gate"].pop(field)
    verdict = ag.evaluate(block)
    assert verdict.allowed is False
    assert verdict.reason == ag.MISSING_FIELDS
    assert field in verdict.missing_fields


@pytest.mark.parametrize("field", ag.FAST_FIX_REQUIRED_FIELDS)
def test_every_fast_fix_field_is_required(field):
    block = _fast_fix()
    block["analysis_gate"].pop(field)
    verdict = ag.evaluate(block)
    assert verdict.allowed is False
    assert field in verdict.missing_fields


@pytest.mark.parametrize("empty", ["", "   ", [], {}, None])
def test_empty_is_not_filled(empty):
    """An empty edge-case list must not read as 'edge cases considered'."""
    verdict = ag.evaluate(_full(edge_cases=empty))
    assert verdict.allowed is False
    assert "edge_cases" in verdict.missing_fields


def test_fast_fix_does_not_require_full_fields():
    """The short profile is short on purpose."""
    verdict = ag.evaluate(_fast_fix())
    assert verdict.allowed is True
    assert "dangerous_failure_modes" not in verdict.missing_fields


# -- the n/a escape hatch ----------------------------------------------------

def test_state_model_may_be_declared_not_applicable_with_a_reason():
    verdict = ag.evaluate(_full(state_model={"n/a": True, "reason": "read-path only"}))
    assert verdict.allowed is True


def test_state_model_na_without_a_reason_is_still_missing():
    verdict = ag.evaluate(_full(state_model={"n/a": True}))
    assert verdict.allowed is False
    assert "state_model" in verdict.missing_fields


def test_only_state_model_gets_special_na_validation():
    verdict = ag.evaluate(_full(invariants={"n/a": True, "reason": "none"}))
    assert verdict.allowed is True
    # `invariants` is a non-empty dict, so it is "filled"; the point of this
    # test is that NOT_APPLICABLE_ALLOWED is narrow by construction.
    assert ag.NOT_APPLICABLE_ALLOWED == frozenset({"state_model"})


# -- assumptions -------------------------------------------------------------

def test_high_impact_unresolved_assumption_blocks():
    verdict = ag.evaluate(_full(assumptions=[_assumption(impact="high",
                                                         resolution="unresolved")]))
    assert verdict.allowed is False
    assert verdict.reason == ag.UNRESOLVED_HIGH_IMPACT_ASSUMPTION
    assert verdict.blocking_assumptions


def test_high_impact_resolved_assumption_passes():
    verdict = ag.evaluate(_full(assumptions=[_assumption(impact="high",
                                                         resolution="resolved")]))
    assert verdict.allowed is True, verdict.message()


def test_low_impact_unresolved_assumption_does_not_block():
    verdict = ag.evaluate(_full(assumptions=[_assumption(impact="low",
                                                         resolution="unresolved")]))
    assert verdict.allowed is True, verdict.message()


@pytest.mark.parametrize("drop", ["confidence", "impact", "resolution"])
def test_assumption_missing_a_required_key_is_malformed(drop):
    bad = _assumption()
    bad.pop(drop)
    verdict = ag.evaluate(_full(assumptions=[bad]))
    assert verdict.allowed is False
    assert verdict.reason == ag.MALFORMED_ASSUMPTION


def test_unrecognised_impact_fails_closed():
    """An impact this build does not recognise is not 'probably low'."""
    verdict = ag.evaluate(_full(assumptions=[_assumption(impact="catastrophic")]))
    assert verdict.allowed is False
    assert verdict.reason == ag.MALFORMED_ASSUMPTION


def test_assumptions_must_be_a_list():
    verdict = ag.evaluate(_full(assumptions={"id": "A1"}))
    assert verdict.allowed is False
    assert verdict.reason == ag.MALFORMED_ASSUMPTION


# -- fail-closed on unknown evidence ----------------------------------------

def test_unreadable_version_fails_closed():
    verdict = ag.evaluate(_full(version="one"))
    assert verdict.allowed is False
    assert verdict.reason == ag.UNSUPPORTED_VERSION


def test_future_version_is_refused_not_approximated():
    verdict = ag.evaluate(_full(version=ag.GATE_VERSION + 1))
    assert verdict.allowed is False
    assert verdict.reason == ag.UNSUPPORTED_VERSION


def test_unknown_profile_fails_closed():
    verdict = ag.evaluate(_full(profile="vibes"))
    assert verdict.allowed is False
    assert verdict.reason == ag.UNKNOWN_PROFILE


def test_non_object_gate_block_fails_closed():
    verdict = ag.evaluate({"analysis_gate": "yes"})
    assert verdict.allowed is False
    assert verdict.reason == ag.MALFORMED_GATE


# -- verdict shape -----------------------------------------------------------

def test_verdict_message_names_the_missing_fields():
    block = _full()
    block["analysis_gate"].pop("invariants")
    message = ag.evaluate(block).message()
    assert "invariants" in message


def test_verdict_is_json_serialisable():
    import json
    json.dumps(ag.evaluate(_full()).as_dict())
