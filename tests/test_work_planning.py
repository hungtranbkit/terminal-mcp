"""The pipeline has to run in an order, degrade without failing, and resume.

Three properties an operator depends on and a worker cannot recover from if
they are wrong:

  * the ORDER -- knowledge before delta before reuse. Reversing any two means
    paying for the wide search before the narrow one that would have made it
    unnecessary, which is the whole saving.
  * DEGRADES -- a project with no knowledge map, no prior specs and no
    registry still gets a spec. Raising instead would make the system unusable
    in exactly the projects that need it most.
  * RESUMES -- a NEEDS_REDEFINE keeps the same spec id and the work already
    done, so adding the missing detail continues the task instead of starting
    a new one.
"""
from __future__ import annotations

import pytest

from terminal_mcp import work_planning as wp
from terminal_mcp import work_spec as ws


@pytest.fixture
def store(tmp_path):
    return ws.WorkSpecStore(tmp_path / "specs.db")


class _FakeModule:
    def __init__(self, name, summary):
        self.name = name
        self.summary = summary
        self.confidence = "HIGH"
        self.confidence_reason = "nothing changed under its paths"
        self.paths = (f"terminal_mcp/{name}.py",)
        self.last_verified_commit = "abc123"


class _FakeKnowledge:
    def __init__(self, *modules):
        self._modules = list(modules)

    def module_states(self):
        return self._modules


# -- the order ---------------------------------------------------------------------

def test_the_pipeline_runs_every_stage_in_the_declared_order(store):
    result = wp.plan("Add CSV export to the reports page", store=store)

    assert [s.name for s in result.stages] == list(wp.STAGE_ORDER)


def test_knowledge_runs_before_the_git_delta(store):
    """The map says where to look; git says what has moved under it since. In
    the other order a stale claim reaches the spec before it is checked."""
    names = [s.name for s in wp.plan("anything", store=store).stages]
    assert names.index(wp.KNOWLEDGE) < names.index(wp.DELTA)
    assert names.index(wp.DELTA) < names.index(wp.REUSE)


# -- degrades rather than failing ----------------------------------------------------

def test_a_project_with_nothing_indexed_still_gets_a_spec(store):
    result = wp.plan("Add CSV export", store=store, knowledge=None, registry=None)

    assert result.spec.spec_id
    assert result.status == ws.NEEDS_REDEFINE
    knowledge_stage = next(s for s in result.stages if s.name == wp.KNOWLEDGE)
    assert knowledge_stage.ok is False
    assert knowledge_stage.gaps, "the absence has to be reported, not silently empty"


def test_a_gap_is_distinguishable_from_a_finding_of_nothing(store):
    """"This module has no known issues" and "there is no knowledge map" look
    identical if a stage reports only findings."""
    with_map = wp.plan("Add CSV export", store=store,
                       knowledge=_FakeKnowledge(_FakeModule("tunnel", "keeps the tunnel up")))
    stage = next(s for s in with_map.stages if s.name == wp.KNOWLEDGE)

    assert stage.ok is True, "a map exists"
    assert stage.gaps == ["no indexed module overlaps this request"]


def test_an_empty_request_is_recorded_as_a_failed_capture(store):
    result = wp.plan("   ", store=store)
    capture = next(s for s in result.stages if s.name == wp.CAPTURE)
    assert capture.ok is False


# -- what the pipeline puts on the spec ------------------------------------------------

def test_a_matching_knowledge_module_fills_the_locator(store):
    knowledge = _FakeKnowledge(
        _FakeModule("export", "serialises report rows to CSV downloads"))
    result = wp.plan("Add CSV export of report rows", store=store, knowledge=knowledge)

    assert result.spec.likely_module == "export"
    assert "terminal_mcp/export.py" in result.spec.likely_files
    assert result.spec.knowledge_confidence == "HIGH"
    assert result.counters["knowledge_hits"] == 1


def test_the_head_commit_is_recorded_so_drift_is_visible_later(store):
    result = wp.plan("Add CSV export", store=store, cwd=".")
    assert result.spec.source_commit, "a spec without a commit cannot be checked for drift"


def test_the_reuse_verdict_is_recorded_on_the_spec(store):
    result = wp.plan("Add CSV export", store=store)
    assert result.spec.reuse_decisions
    assert any(v in result.spec.reuse_decisions[0]
               for v in ("REUSE", "EXTEND", "NEW"))


def test_prior_work_is_counted_and_surfaced(store):
    first = wp.plan("Add CSV export to the reports page", store=store)
    first.spec.likely_module = "dashboard"
    store.save(first.spec)

    second = wp.plan("Add CSV export to the sessions page", store=store)

    assert second.counters["similar_hits"] >= 1
    similar = next(s for s in second.stages if s.name == wp.SIMILAR)
    assert similar.findings


# -- the gate, and resuming ------------------------------------------------------------

def test_an_incomplete_plan_persists_why_it_was_refused(store):
    result = wp.plan("Add CSV export", store=store)

    assert result.status == ws.NEEDS_REDEFINE
    saved = store.get(result.spec.spec_id)
    assert saved.redefine_reason, "the reason must survive, not just be returned"
    assert saved.redefine_missing
    assert saved.redefine_count == 1


def test_a_redefine_resumes_the_same_spec_rather_than_starting_a_new_one(store):
    first = wp.plan("Add CSV export to the reports page", store=store)
    spec_id = first.spec.spec_id

    resumed = wp.redefine(store, spec_id, {
        "problem": "Finance exports by hand and the numbers drift",
        "user_value": "Finance stops retyping numbers",
        "expected_outcome": "an Export button downloads a CSV",
        "scope": ["the export button", "a serialiser"],
        "out_of_scope": ["XLSX", "scheduled exports"],
        "arch_impact": "one route on the existing app",
        "reuse_candidates": ["redaction.redact_output"],
        "existing_patterns": ["routes register via register_dashboard()"],
        "implementation_plan": ["serialiser", "route", "button"],
        "likely_files": ["terminal_mcp/dashboard.py"],
        "api_contract": "GET /export.csv -> text/csv",
        "test_plan": ["unit: quoting"],
        "test_runbook": "test_gate",
        "acceptance_criteria": ["clicking Export downloads a CSV"],
        "risks": ["large reports"],
    })

    assert resumed.spec.spec_id == spec_id, "a redefine must not create a second spec"
    assert resumed.status == ws.SPEC_READY, resumed.gate_report["missing"]
    assert resumed.gate_report["handoff"] is not None
    assert len(store.list()) == 1, "one spec, one task, resumed"


def test_the_redefine_count_survives_being_cleared(store):
    """How many rounds a spec took is the number that says whether the planner
    is learning the shape of this project."""
    first = wp.plan("Add CSV export", store=store)
    assert first.spec.redefine_count == 1

    resumed = wp.redefine(store, first.spec.spec_id, {"problem": "still not enough"})
    assert resumed.status == ws.NEEDS_REDEFINE
    assert resumed.spec.redefine_count == 2
    assert store.get(first.spec.spec_id).redefine_count == 2


def test_redefining_an_unknown_spec_is_an_error(store):
    with pytest.raises(KeyError):
        wp.redefine(store, "nope", {})


def test_an_unknown_field_in_a_redefine_is_refused(store):
    first = wp.plan("Add CSV export", store=store)
    with pytest.raises(ValueError):
        wp.redefine(store, first.spec.spec_id, {"not_a_field": 1})


# -- counters are counted, never invented -----------------------------------------------

def test_counters_are_counts_and_carry_no_token_estimate(store):
    result = wp.plan("Add CSV export", store=store)
    payload = result.as_dict()

    assert set(payload["counters"]) >= {"knowledge_hits", "similar_hits",
                                        "runbook_hits", "redefine_count"}
    assert all(isinstance(v, int) for v in payload["counters"].values())
    assert not any("token" in key for key in payload["counters"]), \
        "token counts belong to the provider, reported with their provenance"
