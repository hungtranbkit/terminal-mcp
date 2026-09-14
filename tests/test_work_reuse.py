"""Before anything new gets built, what already exists has to be found.

On a bug the expensive mistake is re-deriving the analysis. On a feature it is
building a second implementation of something the repository already has --
which costs not just the tokens that built it, but every future change, since
from then on both have to be found and kept in agreement.

So these assert the property that matters: a NEW verdict is a finding backed
by a search, never the default that falls out of having looked nowhere.
"""
from __future__ import annotations

import pytest

from terminal_mcp import work_reuse as wr
from terminal_mcp import work_spec as ws


@pytest.fixture
def store(tmp_path):
    return ws.WorkSpecStore(tmp_path / "specs.db")


def _spec(title, *, module="dashboard", files=("terminal_mcp/dashboard.py",),
          task_type=ws.FEATURE_NEW, **kw):
    spec = ws.plan_from_request(title=title, task_type=task_type,
                                requirement=kw.pop("requirement", title))
    spec.likely_module = module
    spec.likely_files = tuple(files)
    for key, value in kw.items():
        setattr(spec, key, value)
    return spec


# -- prior specs -------------------------------------------------------------------

def test_a_near_identical_prior_feature_is_found_and_scored(store):
    store.save(_spec("CSV export for the reports page",
                     implementation_plan=("add serialiser", "add route")))
    target = _spec("CSV export for the sessions page")

    found = wr.similar_work(store, target)

    assert found, "a prior feature in the same module must surface"
    assert found[0].kind == "spec"
    assert found[0].score > wr.MENTION_THRESHOLD
    assert any("same module" in r for r in found[0].reasons)
    assert found[0].detail["implementation_plan"] == ["add serialiser", "add route"]


def test_the_search_crosses_task_types(store):
    """The REFACTOR that reshaped a module is often the most useful thing to
    read before adding a feature to it."""
    store.save(_spec("Restructure the dashboard route registration",
                     task_type=ws.REFACTOR))
    target = _spec("Add an export route to the dashboard")

    refs = {c.detail["task_type"] for c in wr.similar_work(store, target)}
    assert ws.REFACTOR in refs


def test_an_unrelated_prior_spec_is_not_surfaced(store):
    store.save(_spec("Rotate the node agent TLS material",
                     module="node_agent", files=("terminal_mcp/node_agent.py",)))
    target = _spec("CSV export for the reports page")

    assert wr.similar_work(store, target) == []


def test_a_spec_never_matches_itself(store):
    spec = store.save(_spec("CSV export"))
    assert wr.similar_work(store, spec) == []


# -- the verdict -------------------------------------------------------------------

def test_a_close_prior_spec_says_extend_rather_than_build_beside_it(store):
    store.save(_spec("CSV export for the reports page"))
    target = _spec("CSV export for the reports page")

    analysis = wr.analyse(target, store=store)

    assert analysis["verdict"] == wr.EXTEND
    assert "extend it rather than building beside it" in analysis["why"]


def test_nothing_found_is_a_searched_finding_not_a_default(store):
    """`NEW` with no search behind it is an omission, not a verdict."""
    analysis = wr.analyse(_spec("Something nobody has ever built"), store=store)

    assert analysis["verdict"] == wr.NEW
    assert analysis["searched"], "the verdict must say what was actually searched"
    assert "prior spec" in " ".join(analysis["searched"])


def test_partial_overlap_says_read_before_writing(store):
    store.save(_spec("Export sessions as JSON", files=("terminal_mcp/dashboard.py",)))
    target = _spec("Export reports as CSV")

    analysis = wr.analyse(target, store=store)
    assert analysis["verdict"] in (wr.REUSE, wr.EXTEND)
    assert analysis["candidates"]["specs"]


# -- knowledge and runbooks -----------------------------------------------------------

class _FakeModule:
    def __init__(self, name, summary, confidence="MEDIUM"):
        self.name = name
        self.summary = summary
        self.confidence = confidence
        self.confidence_reason = "nothing changed under its paths"
        self.paths = (f"terminal_mcp/{name}.py",)
        self.last_verified_commit = "abc123"


class _FakeKnowledge:
    def __init__(self, modules):
        self._modules = modules

    def module_states(self):
        return self._modules


def test_a_knowledge_module_whose_purpose_overlaps_is_offered_with_its_confidence():
    knowledge = _FakeKnowledge([
        _FakeModule("export", "serialises report rows to CSV and JSON downloads"),
        _FakeModule("tunnel", "keeps the cloudflared tunnel healthy")])
    target = _spec("Add CSV download of report rows")

    found = wr.knowledge_candidates(target, knowledge=knowledge)

    assert [c.ref for c in found] == ["export"]
    assert any("confidence MEDIUM" in r for r in found[0].reasons), \
        "a module is offered with how much it can be trusted right now"


def test_a_missing_knowledge_map_is_not_a_planning_failure():
    assert wr.knowledge_candidates(_spec("anything"), knowledge=None) == []


class _FakeRegistry:
    def __init__(self, rows):
        self._rows = rows

    def list(self):
        return self._rows


def test_a_stale_runbook_is_still_offered_with_its_state():
    """Knowing a procedure exists and needs re-verifying beats writing a
    second one beside it."""
    registry = _FakeRegistry([
        {"id": "export_csv_smoke", "name": "export csv smoke", "status": "STALE",
         "command": ["scripts/agent/export_smoke.sh"]},
        {"id": "tunnel_check", "name": "tunnel check", "status": "VERIFIED",
         "command": ["scripts/agent/tunnel.sh"]}])
    target = _spec("Add CSV export")

    found = wr.runbook_candidates(target, registry=registry)

    assert [c.ref for c in found] == ["export_csv_smoke"]
    assert any("status STALE" in r for r in found[0].reasons)


def test_a_broken_registry_does_not_break_planning():
    class _Boom:
        def list(self):
            raise RuntimeError("registry unreadable")

    assert wr.runbook_candidates(_spec("x"), registry=_Boom()) == []


# -- writing the analysis back onto the spec -------------------------------------------

def test_the_analysis_fills_an_empty_reuse_list(store):
    store.save(_spec("CSV export for the reports page"))
    target = _spec("CSV export for the sessions page")
    assert target.reuse_candidates == ()

    wr.apply_to_spec(target, wr.analyse(target, store=store))

    assert target.reuse_candidates, "an empty list is filled from the search"
    assert target.reuse_decisions, "the verdict is recorded as a decision"


def test_the_analysis_never_overwrites_what_a_planner_already_named(store):
    store.save(_spec("CSV export for the reports page"))
    target = _spec("CSV export for the sessions page")
    target.reuse_candidates = ("the planner's own considered answer",)

    wr.apply_to_spec(target, wr.analyse(target, store=store))

    assert target.reuse_candidates == ("the planner's own considered answer",), \
        "a human said something the search cannot; it is not replaced"


def test_applying_twice_does_not_duplicate_the_decision(store):
    target = _spec("Something new")
    analysis = wr.analyse(target, store=store)
    wr.apply_to_spec(target, analysis)
    wr.apply_to_spec(target, analysis)

    assert len(target.reuse_decisions) == 1


# -- module scoring has to be on the same scale as spec scoring --------------------

def test_a_module_named_by_the_request_is_found_even_with_little_prose_overlap():
    """The defect this covers, measured on this repo's real map.

    Module matching used a raw Jaccard similarity against MENTION_THRESHOLD,
    which was calibrated for the COMPOSITE spec score. Different units:
    Jaccard divides by the union, so a fifteen-word request against a
    twenty-word module description cannot reach 0.25 even when it is
    unmistakably about that module. The correct module for a real bug report
    scored 0.065 and was discarded, so the knowledge stage returned nothing on
    real input while appearing to work.
    """
    knowledge = _FakeKnowledge([
        _FakeModule("work_runtime", "runs and supervises tasks"),
        _FakeModule("tunnel", "keeps the cloudflared tunnel healthy")])
    target = _spec("the work page shows a session as idle while it is running")

    found = wr.knowledge_candidates(target, knowledge=knowledge)

    assert [c.ref for c in found] == ["work_runtime"]
    assert found[0].score >= wr.MENTION_THRESHOLD
    assert any("names" in r for r in found[0].reasons)


def test_a_path_stem_counts_as_naming_the_module():
    """`work_runtime` owning `work_service.py` should be found by a request
    that says "service" -- the files a module owns name it too."""
    module = _FakeModule("runtime", "supervises things")
    module.paths = ("terminal_mcp/work_service.py",)
    target = _spec("the work service reports the wrong state")

    found = wr.knowledge_candidates(target, knowledge=_FakeKnowledge([module]))

    assert [c.ref for c in found] == ["runtime"]


def test_shared_path_noise_alone_does_not_make_a_match():
    """Every module in this project lives under terminal_mcp/ and ends in .py.
    Matching on those would score every module equally and distinguish none."""
    module = _FakeModule("tunnel", "keeps the cloudflared tunnel healthy")
    module.paths = ("terminal_mcp/tunnel_watchdog.py",)
    target = _spec("add a terminal mcp py thing")

    assert wr.knowledge_candidates(target, knowledge=_FakeKnowledge([module])) == []
