"""capability_profile.py -- the shared capability model and the routing
diagnosis (blg_orch_no_workers_declared).

The property every test here defends: a worker nobody declared is UNKNOWN,
never "capable of everything". The whole reason capability routing had zero
candidates was that three different situations answered identically; these
tests pin each of them to its own code.
"""
from __future__ import annotations

import pytest

from terminal_mcp.capability_profile import (
    CANDIDATES_AVAILABLE,
    CAPABILITY_UNKNOWN,
    CandidateView,
    MATCH_MISSING,
    MATCH_OK,
    MATCH_UNKNOWN,
    NO_WORKERS_ONLINE,
    NO_WORKERS_REGISTERED,
    SOURCE_DETECTED,
    SOURCE_UNDECLARED,
    WORKERS_BUSY,
    WORKERS_INELIGIBLE,
    WORKERS_LACK_CAPABILITY,
    declared_capability_set,
    diagnose,
    match_capabilities,
    normalise_capabilities,
)


# -- 1. The declared set: runtime_tools AND skills -------------------------

def test_declared_set_merges_runtime_tools_and_skill_names():
    """The quiet bug this fixes: pm_router matched `skills` only, so a
    profile declaring runtime_tools=["playwright"] was judged unable to do
    playwright work while the field sat there populated."""
    declared = declared_capability_set(
        runtime_tools=["playwright", "dotnet"],
        skills=[{"name": "browser-qa", "confidence": 0.9}, {"name": "wpf"}])
    assert set(declared) == {"playwright", "dotnet", "browser-qa", "wpf"}


def test_declared_set_is_casefolded_deduplicated_and_order_stable():
    declared = declared_capability_set(runtime_tools=["Playwright", "playwright", " DOTNET "],
                                       skills=[{"name": "Playwright"}, {"confidence": 1.0}])
    assert declared == ("playwright", "dotnet")


def test_normalise_drops_empties_and_accepts_a_bare_string():
    assert normalise_capabilities(["a", "", None, " B "]) == ("a", "b")
    assert normalise_capabilities("Solo") == ("solo",)
    assert normalise_capabilities(None) == ()


# -- 2. Matching -----------------------------------------------------------

def test_matching_capability_from_a_declaration():
    match = match_capabilities(["playwright"], declared=["playwright", "dotnet"])
    assert match.verdict == MATCH_OK and match.ok
    assert match.matched == ("playwright",) and match.missing == ()
    assert match.reason() is None


def test_matching_capability_from_a_probe():
    """A probe is a measurement, so it satisfies a requirement even with no
    profile at all -- that is detection, not an assumption."""
    match = match_capabilities(["python"], detected=["python"], has_profile=False)
    assert match.verdict == MATCH_OK
    assert match.source == SOURCE_DETECTED


def test_non_matching_capability_is_missing_not_unknown():
    match = match_capabilities(["playwright"], declared=["dotnet"])
    assert match.verdict == MATCH_MISSING
    assert match.missing == ("playwright",)
    assert "missing required capabilities" in match.reason()


def test_unknown_profile_is_unknown_not_missing():
    """"nobody ever said" is a different fact from "it cannot do this", and
    a different operator action: declare it, versus find another worker."""
    match = match_capabilities(["playwright"], has_profile=False)
    assert match.verdict == MATCH_UNKNOWN
    assert match.source == SOURCE_UNDECLARED
    assert "no declared capability profile" in match.reason()
    assert "terminal_worker_declare" in match.reason()


@pytest.mark.parametrize("required", [["playwright"], ["a", "b"], ["anything-at-all"]])
def test_an_undeclared_worker_is_never_capable_of_everything(required):
    """THE safety invariant. An empty pool satisfies no non-empty
    requirement -- structurally, not by convention."""
    match = match_capabilities(required, declared=(), detected=(), has_profile=False)
    assert match.verdict == MATCH_UNKNOWN
    assert not match.ok


def test_multi_capability_matching_is_AND_and_reports_only_what_is_missing():
    match = match_capabilities(["playwright", "dotnet", "wpf"],
                               declared=["playwright"], detected=["dotnet"])
    assert match.verdict == MATCH_MISSING
    assert match.matched == ("playwright", "dotnet")
    assert match.missing == ("wpf",)


def test_multi_capability_satisfied_across_both_sources():
    match = match_capabilities(["playwright", "python"],
                               declared=["playwright"], detected=["python"])
    assert match.verdict == MATCH_OK


def test_an_empty_requirement_matches_everyone_including_the_undeclared():
    """Unchanged from match_nodes_by_capability: tightening this would make
    every unconstrained task unroutable overnight."""
    assert match_capabilities([], has_profile=False).verdict == MATCH_OK
    assert match_capabilities(None, declared=["x"]).verdict == MATCH_OK


def test_trust_declared_false_counts_probed_capability_only():
    assert match_capabilities(["dotnet"], declared=["dotnet"]).ok
    assert not match_capabilities(["dotnet"], declared=["dotnet"], trust_declared=False).ok
    assert match_capabilities(["dotnet"], detected=["dotnet"], trust_declared=False).ok


def test_a_declared_worker_that_lacks_everything_is_missing_not_unknown():
    """It HAS a profile, so silence about a capability is an answer."""
    match = match_capabilities(["playwright"], declared=(), has_profile=True)
    assert match.verdict == MATCH_MISSING


# -- 3. Diagnosis precedence ----------------------------------------------

def _view(key="local/s1", **overrides) -> CandidateView:
    return CandidateView(key=key, **overrides)


def test_no_workers_registered_when_the_walk_is_empty():
    result = diagnose([], required_capabilities=["playwright"])
    assert result["code"] == NO_WORKERS_REGISTERED
    assert result["counts"]["total"] == 0
    assert result["eligible"] == []


def test_candidates_available_when_something_is_eligible():
    result = diagnose([_view()], required_capabilities=[])
    assert result["code"] == CANDIDATES_AVAILABLE
    assert result["eligible"] == ["local/s1"]


def test_node_offline_is_its_own_code():
    result = diagnose([_view(online=False), _view("local/s2", online=False)])
    assert result["code"] == NO_WORKERS_ONLINE
    assert result["counts"]["offline"] == 2


def test_a_busy_capable_worker_reports_capacity_not_a_capability_gap():
    view = _view(busy=True, capability=match_capabilities(["playwright"], declared=["playwright"]))
    result = diagnose([view], required_capabilities=["playwright"])
    assert result["code"] == WORKERS_BUSY
    assert result["counts"]["busy"] == 1


def test_workers_lack_capability_when_declared_and_none_has_it():
    view = _view(capability=match_capabilities(["playwright"], declared=["dotnet"]))
    result = diagnose([view], required_capabilities=["playwright"])
    assert result["code"] == WORKERS_LACK_CAPABILITY


def test_capability_unknown_when_nobody_declared_anything():
    view = _view(has_profile=False,
                 capability=match_capabilities(["playwright"], has_profile=False))
    result = diagnose([view], required_capabilities=["playwright"])
    assert result["code"] == CAPABILITY_UNKNOWN
    assert result["counts"]["undeclared"] == 1
    assert "declare" in result["reason"]


def test_unknown_outranks_missing_so_the_actionable_answer_wins():
    """With one undeclared worker and one that genuinely lacks the tool,
    "declare your workers" is the move that can actually change the
    outcome."""
    result = diagnose([
        _view("local/declared", capability=match_capabilities(["wpf"], declared=["dotnet"])),
        _view("local/undeclared", has_profile=False,
              capability=match_capabilities(["wpf"], has_profile=False)),
    ], required_capabilities=["wpf"])
    assert result["code"] == CAPABILITY_UNKNOWN
    assert result["counts"] == {"total": 2, "eligible": 0, "offline": 0, "busy": 0,
                                "missing_capability": 1, "unknown_capability": 1,
                                "ineligible": 0, "undeclared": 1}


def test_offline_outranks_every_other_blocker_for_that_worker():
    """Nothing else about an offline worker is knowable, so its capability
    verdict must not be reported as the blocker."""
    view = _view(online=False, busy=True,
                 capability=match_capabilities(["wpf"], declared=["dotnet"]))
    assert view.verdict == "OFFLINE"


def test_other_hard_constraints_fall_through_to_workers_ineligible():
    view = _view(ineligible_reason="project affinity mismatch")
    result = diagnose([view])
    assert result["code"] == WORKERS_INELIGIBLE
    assert result["candidates"][0]["ineligible_reason"] == "project affinity mismatch"


def test_every_candidate_is_reported_even_when_one_is_eligible():
    """The walk is the explainability surface: an operator needs to see the
    workers that did NOT qualify, not only the winner."""
    result = diagnose([
        _view("local/ok"),
        _view("local/offline", online=False),
        _view("local/undeclared", has_profile=False,
              capability=match_capabilities(["wpf"], has_profile=False)),
    ], required_capabilities=["wpf"])
    assert result["code"] == CANDIDATES_AVAILABLE
    assert [c["key"] for c in result["candidates"]] == ["local/ok", "local/offline",
                                                        "local/undeclared"]
    assert [c["verdict"] for c in result["candidates"]] == ["ELIGIBLE", "OFFLINE", "UNKNOWN"]
