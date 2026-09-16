"""The gate as wired into `WorkStore.transition_run`.

`test_analysis_gate.py` proves the evaluator's rules. This file proves the
*wiring*: that the hook sits on the right edge, refuses without writing, leaves
an audit trail, and does not change what a legacy run can do.

Kept separate because they fail for different reasons and a failure here means
"the hook is wrong", not "a rule is wrong".
"""
from __future__ import annotations

import pytest

from terminal_mcp import work_store as ws
from terminal_mcp.analysis_gate import AnalysisGateError


@pytest.fixture()
def store(tmp_path):
    return ws.WorkStore(tmp_path / "work.db")


@pytest.fixture()
def strict_store(tmp_path):
    return ws.WorkStore(tmp_path / "strict.db", require_analysis_gate=True)


def _complete_gate(**overrides):
    block = {
        "version": 1,
        "profile": "full",
        "problem_statement": "p",
        "user_observable_goal": "g",
        "source_of_truth": "s",
        "state_model": {"n/a": True, "reason": "read-path only"},
        "invariants": ["i"],
        "assumptions": [{"id": "A1", "confidence": "high", "impact": "low",
                         "resolution": "resolved"}],
        "edge_cases": ["e"],
        "dangerous_failure_modes": ["d"],
        "acceptance_tests": ["a"],
        "live_verification": "v",
        "critic_result": "c",
    }
    block.update(overrides)
    return {"analysis_gate": block}


def _run(store, metadata=None, title="t"):
    return store.create_run(title=title, goal="goal", lane="demo-work",
                            metadata=metadata or {})


def _events(store, work_id):
    return [e["kind"] for e in store.events_for(work_id)]


# -- backward compatibility --------------------------------------------------

def test_legacy_run_reaches_ready_unchanged(store):
    """The deployability claim, as an executable assertion."""
    run = _run(store, metadata={})
    updated = store.transition_run(run.work_id, ws.READY)
    assert updated.state == ws.READY


def test_legacy_run_with_unrelated_metadata_reaches_ready(store):
    run = _run(store, metadata={"origin": "dashboard"})
    assert store.transition_run(run.work_id, ws.READY).state == ws.READY


# -- opted-in runs -----------------------------------------------------------

def test_complete_gate_reaches_ready(store):
    run = _run(store, metadata=_complete_gate())
    assert store.transition_run(run.work_id, ws.READY).state == ws.READY


def test_missing_field_refuses_and_leaves_state_untouched(store):
    block = _complete_gate()
    block["analysis_gate"].pop("invariants")
    run = _run(store, metadata=block)
    before = store.get_run(run.work_id).state

    with pytest.raises(AnalysisGateError) as excinfo:
        store.transition_run(run.work_id, ws.READY)

    assert "invariants" in str(excinfo.value)
    assert store.get_run(run.work_id).state == before, \
        "a refused transition must not write the state it refused"


def test_refusal_is_recorded_as_an_event(store):
    """A refusal nobody can debug later is a refusal that will be worked around."""
    block = _complete_gate()
    block["analysis_gate"].pop("acceptance_tests")
    run = _run(store, metadata=block)

    with pytest.raises(AnalysisGateError):
        store.transition_run(run.work_id, ws.READY)

    assert "analysis_gate_refused" in _events(store, run.work_id)


def test_high_impact_unresolved_assumption_refuses(store):
    block = _complete_gate(assumptions=[{"id": "A1", "confidence": "low",
                                          "impact": "high",
                                          "resolution": "unresolved"}])
    run = _run(store, metadata=block)
    with pytest.raises(AnalysisGateError) as excinfo:
        store.transition_run(run.work_id, ws.READY)
    assert "A1" in str(excinfo.value)
    assert store.get_run(run.work_id).state != ws.READY


def test_fast_fix_profile_reaches_ready(store):
    run = _run(store, metadata={"analysis_gate": {
        "version": 1, "profile": "fast_fix",
        "reproduce": "r", "root_cause": "rc", "expected_behavior": "e",
        "invariant": "i", "regression_test": "t", "live_verify": "v"}})
    assert store.transition_run(run.work_id, ws.READY).state == ws.READY


# -- the hook is on ONE edge -------------------------------------------------

def test_only_the_ready_edge_is_gated(store):
    """An incomplete run still moves through the rest of its lifecycle.

    Gating every edge would strand runs that are already executing, which is a
    worse failure than letting one start.
    """
    block = _complete_gate()
    block["analysis_gate"].pop("critic_result")
    run = _run(store, metadata=block)

    store.transition_run(run.work_id, ws.PLANNING)
    assert store.get_run(run.work_id).state == ws.PLANNING
    store.transition_run(run.work_id, ws.CANCELLED)
    assert store.get_run(run.work_id).state == ws.CANCELLED


def test_running_to_verifying_is_never_gated(store):
    """A run whose analysis would FAIL the gate still finishes its lifecycle.

    Started directly in RUNNING rather than degraded mid-flight: `WorkStore`
    has no metadata-update method, and inventing one here would test a seam
    that does not exist.
    """
    block = _complete_gate()
    block["analysis_gate"].pop("invariants")
    run = store.create_run(title="t", goal="g", lane="demo-work",
                           metadata=block, state=ws.RUNNING)
    assert store.transition_run(run.work_id, ws.VERIFYING).state == ws.VERIFYING


# -- the gate never masks a more basic error ---------------------------------

def test_invalid_edge_raises_work_error_not_gate_error(store):
    """COMPLETE is terminal. That is a transition fault, not an analysis fault,
    and must be reported as one even when the gate would also have refused."""
    block = _complete_gate()
    block["analysis_gate"].pop("invariants")
    run = _run(store, metadata=block)
    store.transition_run(run.work_id, ws.CANCELLED)

    with pytest.raises(ws.WorkError) as excinfo:
        store.transition_run(run.work_id, ws.READY)
    assert not isinstance(excinfo.value, AnalysisGateError)


# -- deployment-wide strict mode ---------------------------------------------

def test_strict_mode_refuses_a_run_with_no_gate(strict_store):
    run = _run(strict_store, metadata={})
    with pytest.raises(AnalysisGateError) as excinfo:
        strict_store.transition_run(run.work_id, ws.READY)
    assert "MISSING_GATE" in str(excinfo.value) or "requires one" in str(excinfo.value)


def test_strict_mode_still_accepts_a_complete_gate(strict_store):
    run = _run(strict_store, metadata=_complete_gate())
    assert strict_store.transition_run(run.work_id, ws.READY).state == ws.READY


def test_default_store_is_not_strict(store):
    assert store.require_analysis_gate is False
