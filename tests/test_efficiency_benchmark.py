"""The benchmark's own tests, because a measuring instrument can lie too.

Two things are defended here. First, that the corpus is what it claims: every
REAL case is still a commit in this repository, and a synthetic one is marked
as synthetic wherever it appears. Second, that the harness cannot flatter the
system it measures -- a briefing that misses the fix site earns no credit
however small it was, a module is never chosen on evidence the shipped code
would reject, no case is ever scored against its own answer, and a token
saving is never reported for counters nobody recorded.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from terminal_mcp import efficiency_benchmark as eb
from terminal_mcp.bug_spec import BugSpec, BugSpecStore
from terminal_mcp.project_knowledge import ProjectKnowledge

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS = REPO_ROOT / "benchmarks" / "tokeff_corpus.json"


@pytest.fixture(scope="module")
def corpus():
    return eb.load_corpus(CORPUS)


# -- the corpus is what it says it is ----------------------------------------

def test_every_real_case_is_still_a_commit_in_this_repository(corpus):
    """A corpus that rots into fiction keeps its REAL label while the evidence
    behind it is gone."""
    report = eb.validate_corpus(corpus, repo_root=REPO_ROOT)
    assert report["problems"] == []
    assert report["ok"] is True


def test_the_corpus_is_mostly_real_and_covers_every_named_category(corpus):
    assert len(corpus) >= 10, "the task asks for 10-20 cases"
    assert len(corpus) <= 20
    real = [c for c in corpus if c.is_real]
    assert len(real) >= 10, "real cases must outnumber written ones"
    assert {c.category for c in corpus} == {
        "ui", "simple_logic", "backend", "auth", "session", "unknown"}


def test_a_synthetic_case_says_so_and_says_why(corpus):
    synthetic = [c for c in corpus if not c.is_real]
    assert synthetic, "the corpus declares synthetic cases"
    for case in synthetic:
        assert case.origin == "SYNTHETIC"
        assert "SYNTHETIC" in case.note, "the reason must travel with the case"
        assert not case.commit, "a synthetic case must not claim a commit"


def test_real_fix_paths_exclude_tests_and_docs(corpus):
    """Locating the test that noticed a defect is not locating the defect."""
    for case in corpus:
        for path in case.fix_paths:
            assert not path.startswith(("tests/", "docs/"))
            assert not path.endswith(".md")


# -- terms are derived, never hand-picked ------------------------------------

def test_terms_are_deterministic_and_drop_bookkeeping_words():
    symptom = "P0 fix: config.submit was clobbered by config.submit_watchdog"
    first = eb.search_terms(symptom)
    assert first == eb.search_terms(symptom)
    assert "fix" not in [t.lower() for t in first]
    # An identifier keeps its shape: it is what somebody would grep for.
    assert "config.submit_watchdog" in first


def test_the_term_budget_is_respected():
    terms = eb.search_terms("dashboard mobile portrait layout squeeze overlap tabs",
                            budget=3)
    assert len(terms) == 3


def test_short_and_numeric_tokens_are_not_terms():
    assert eb.search_terms("fix p0 the id 42 bug") == []


# -- figures carry their provenance ------------------------------------------

def test_a_figure_says_how_it_was_obtained():
    assert eb.Figure.real(3, method="counted").label == eb.REAL
    assert eb.Figure.estimate(3, method="proxy").display().startswith("~")
    missing = eb.Figure.unavailable("nobody recorded it")
    assert missing.value is None
    assert "UNAVAILABLE" in missing.display()


# -- the baseline is really measured, against the real repository ------------

@pytest.fixture()
def tiny_repo(tmp_path):
    """A real git repository, because what a grep is worth depends on git."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "widget.py").write_text("def render_widget():\n    return 'tofu'\n")
    (repo / "pkg" / "other.py").write_text("WIDGET_LIMIT = 3\n")
    (repo / "pkg" / "unrelated.py").write_text("x = 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_widget.py").write_text("def test_render_widget(): ...\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@e", "-c", "user.name=t",
                    "commit", "-qm", "init"], check=True)
    return repo


def test_grep_really_searches_and_skips_tests(tiny_repo):
    found = eb.grep_files(tiny_repo, "render_widget")
    assert "pkg/widget.py" in found
    # The test that names the same symbol is excluded by the same rule the
    # corpus uses for fix paths.
    assert not any(path.startswith("tests/") for path in found)


def test_the_baseline_reports_the_most_selective_term_not_the_union(tiny_repo):
    case = eb.BenchmarkCase(
        case_id="t1", origin="REAL", category="ui",
        symptom="render_widget shows tofu instead of a widget",
        fix_paths=("pkg/widget.py",), parent="HEAD")
    result = eb.measure_baseline(case, repo_root=tiny_repo)
    assert result.best_surface.label == eb.ESTIMATE       # a proxy, and it says so
    assert result.search_calls.label == eb.REAL           # these greps really ran
    assert result.best_surface.value <= result.union_surface.value
    assert result.contains_fix_path is True
    assert result.analysis == eb.FULL_ANALYSIS


def test_a_baseline_that_never_reaches_the_fix_site_says_so(tiny_repo):
    case = eb.BenchmarkCase(case_id="t2", origin="REAL", category="unknown",
                            symptom="nothing here matches anything at all",
                            fix_paths=("pkg/widget.py",), parent="HEAD")
    assert eb.measure_baseline(case, repo_root=tiny_repo).contains_fix_path is False


# -- the assisted side may not be chosen on evidence the system would reject --

@pytest.fixture()
def mapped_repo(tiny_repo):
    knowledge = ProjectKnowledge(tiny_repo)
    knowledge.record_module("widgets", paths=["pkg/widget.py"],
                            summary="widget rendering and its tofu fallback")
    return tiny_repo, knowledge


def test_a_weak_module_match_is_no_match(mapped_repo):
    """The first harness took the argmax and 'chose' modules on scores of
    0.03 -- measuring a decision procedure the system does not use."""
    _, knowledge = mapped_repo
    name, score = eb.choose_module("something entirely unrelated happened once",
                                   knowledge=knowledge)
    assert name is None
    assert score < eb.MENTION_THRESHOLD


def test_a_strong_module_match_is_taken(mapped_repo):
    _, knowledge = mapped_repo
    name, _ = eb.choose_module("widget rendering shows tofu", knowledge=knowledge)
    assert name == "widgets"


def test_the_briefing_is_counted_from_what_it_actually_returned(mapped_repo, tmp_path):
    repo, knowledge = mapped_repo
    store = BugSpecStore(tmp_path / "specs.db")
    case = eb.BenchmarkCase(case_id="t3", origin="REAL", category="ui",
                            symptom="widget rendering shows tofu",
                            fix_paths=("pkg/widget.py",), parent="HEAD")
    result = eb.measure_assisted(case, knowledge=knowledge, spec_store=store,
                                 repo_root=repo)
    assert result.surface.label == eb.REAL
    assert result.search_calls.value == 0
    assert "pkg/widget.py" in result.files
    assert result.contains_fix_path is True
    assert result.module_is_right is True


def test_an_unmapped_fix_site_is_coverage_not_a_wrong_choice(mapped_repo, tmp_path):
    repo, knowledge = mapped_repo
    store = BugSpecStore(tmp_path / "specs.db")
    case = eb.BenchmarkCase(case_id="t4", origin="REAL", category="backend",
                            symptom="widget rendering shows tofu",
                            fix_paths=("pkg/other.py",), parent="HEAD")
    result = eb.measure_assisted(case, knowledge=knowledge, spec_store=store,
                                 repo_root=repo)
    # `None`, not False: a choice cannot be wrong about a module the map does
    # not contain, and the two must not aggregate as the same failure.
    assert result.module_is_right is None
    assert result.owning_modules == ()


# -- a small briefing that misses earns nothing ------------------------------

def _case_result(*, baseline_surface: int, baseline_hit: bool, assisted_files: list[str],
                 assisted_hit: bool) -> eb.CaseResult:
    case = eb.BenchmarkCase(case_id="c", origin="REAL", category="ui",
                            symptom="s", fix_paths=("pkg/widget.py",))
    baseline = eb.BaselineResult(
        terms=["widget"], per_term={"widget": baseline_surface}, best_term="widget",
        best_surface=eb.Figure.estimate(baseline_surface, method="proxy"),
        union_surface=eb.Figure.estimate(baseline_surface, method="proxy"),
        search_calls=eb.Figure.real(1, method="ran"),
        seconds=eb.Figure.real(0.01, method="clock"),
        contains_fix_path=baseline_hit)
    assisted = eb.AssistedResult(
        chosen_module="widgets", module_score=0.5, module_is_right=True,
        owning_modules=("widgets",), files=assisted_files,
        surface=eb.Figure.real(len(assisted_files), method="named"),
        search_calls=eb.Figure.real(0, method="none"),
        seconds=eb.Figure.real(0.01, method="clock"),
        retrieval_status=eb.NO_MATCH, analysis=eb.FULL_ANALYSIS,
        contains_fix_path=assisted_hit)
    return eb.CaseResult(case=case, baseline=baseline, assisted=assisted)


def test_a_briefing_that_misses_the_fix_site_is_a_miss_however_small_it_was():
    result = _case_result(baseline_surface=20, baseline_hit=True,
                          assisted_files=["pkg/unrelated.py"], assisted_hit=False)
    assert result.verdict == "MISSED"


def test_neither_path_reaching_the_site_is_counted_apart_not_as_a_loss():
    result = _case_result(baseline_surface=2, baseline_hit=False,
                          assisted_files=[], assisted_hit=False)
    assert result.verdict == "UNLOCATABLE"


def test_shrinking_only_counts_when_the_site_was_named():
    smaller = _case_result(baseline_surface=9, baseline_hit=True,
                           assisted_files=["pkg/widget.py"], assisted_hit=True)
    tied = _case_result(baseline_surface=1, baseline_hit=True,
                        assisted_files=["pkg/widget.py"], assisted_hit=True)
    assert smaller.verdict == "SHRUNK"
    assert tied.verdict == "NO_SHRINK"


def test_unlocatable_cases_are_excluded_from_the_rate_they_cannot_speak_to():
    results = [_case_result(baseline_surface=9, baseline_hit=True,
                            assisted_files=["pkg/widget.py"], assisted_hit=True),
               _case_result(baseline_surface=2, baseline_hit=False,
                            assisted_files=[], assisted_hit=False)]
    summary = eb.summarise(results)
    assert summary["located_denominator"] == 1
    assert summary["located_rate"] == 1.0


# -- leave-one-out is real ----------------------------------------------------

def test_a_case_is_never_scored_against_its_own_answer(tmp_path, mapped_repo):
    _, knowledge = mapped_repo
    cases = [eb.BenchmarkCase(case_id=f"c{i}", origin="REAL", category="ui",
                              symptom=f"widget {i} renders tofu",
                              fix_paths=("pkg/widget.py",)) for i in range(3)]
    store = BugSpecStore(tmp_path / "specs.db")
    eb.seed_store(cases, exclude="c1", store=store, knowledge=knowledge)
    assert store.get("bench_c1") is None
    assert store.get("bench_c0") is not None


def test_each_case_gets_its_own_store_file(tmp_path):
    """Sharing one file leaks the spec written for a later case back into its
    own turn -- the first version of this harness did exactly that."""
    first = eb.case_store_path(tmp_path / "specs.db", "real-abc1234")
    second = eb.case_store_path(tmp_path / "specs.db", "real-def5678")
    assert first != second
    assert first.suffix == ".db"


# -- provider usage is looked for, never assumed ------------------------------

def test_usage_is_unavailable_when_no_store_was_given():
    usage = eb.collect_usage(None, case_ids=["a", "b"])
    assert usage["available"] is False
    assert usage["figure"]["label"] == eb.UNAVAILABLE
    assert usage["figure"]["value"] is None


def test_usage_is_unavailable_when_a_real_store_holds_no_counters(tmp_path):
    from terminal_mcp.work_telemetry import TaskTelemetry, TelemetryStore

    store = TelemetryStore(tmp_path / "telemetry.db")
    try:
        store.save(TaskTelemetry(task_id="case-1"))      # a row, but no usage
        usage = eb.collect_usage(store, case_ids=["case-1"])
    finally:
        store.close()
    assert usage["available"] is False
    assert usage["tasks_with_usage"] == 0
    assert "predate" in usage["figure"]["method"] or "no provider reported" in \
        usage["figure"]["method"]


def test_usage_becomes_real_the_moment_a_provider_reports_counters(tmp_path):
    """The same code path that reports UNAVAILABLE today reports a measurement
    the day a runtime supplies one. Nothing here needs to change for that."""
    from terminal_mcp.work_telemetry import TaskTelemetry, TelemetryStore

    store = TelemetryStore(tmp_path / "telemetry.db")
    try:
        record = TaskTelemetry(task_id="case-1")
        record.record_provider_usage({"total_tokens": 1234}, provider="runtime")
        store.save(record)
        usage = eb.collect_usage(store, case_ids=["case-1"])
    finally:
        store.close()
    assert usage["available"] is True
    assert usage["figure"]["label"] == eb.REAL
    assert usage["figure"]["value"] == 1234


# -- acceptance is decided by the numbers ------------------------------------

def _summary(located: float | None, shrunk: float | None, cases: int = 18):
    return {"cases": cases, "located_rate": located, "shrunk_rate": shrunk,
            "located_denominator": cases, "shrunk_denominator": cases}


def test_both_rates_below_the_bar_is_a_fail():
    verdict = eb.acceptance(_summary(0.29, 0.40), usage={"available": False})
    assert verdict["verdict"] == "FAIL"


def test_both_rates_at_the_bar_is_a_pass():
    verdict = eb.acceptance(_summary(0.8, 0.6), usage={"available": False})
    assert verdict["verdict"] == "PASS"


def test_too_few_cases_is_inconclusive_whatever_the_rates_say():
    verdict = eb.acceptance(_summary(1.0, 1.0, cases=3), usage={"available": False})
    assert verdict["verdict"] == "INCONCLUSIVE"


def test_an_unmeasured_token_claim_never_decides_the_verdict():
    without = eb.acceptance(_summary(0.8, 0.6), usage={"available": False})
    with_usage = eb.acceptance(_summary(0.8, 0.6), usage={"available": True,
                                                          "figure": {"method": "m"}})
    assert without["verdict"] == with_usage["verdict"] == "PASS"
    assert without["token_claim"]["status"] == "UNVERIFIED"
    assert with_usage["token_claim"]["status"] == "MEASURED"


# -- the report tells on itself ----------------------------------------------

def test_the_wording_pair_divergence_is_detected():
    real = _case_result(baseline_surface=9, baseline_hit=True,
                        assisted_files=["pkg/widget.py"], assisted_hit=True)
    object.__setattr__(real.case, "case_id", "real-x")
    reported = _case_result(baseline_surface=9, baseline_hit=True,
                            assisted_files=[], assisted_hit=False)
    object.__setattr__(reported.case, "case_id", "synthetic-x")
    object.__setattr__(reported.case, "mirrors", "real-x")
    sensitivity = eb.wording_sensitivity([real, reported])
    assert sensitivity["diverging"] == 1
    assert "optimistic" in sensitivity["note"]


def test_conclusions_warn_when_a_stratum_is_too_small_to_conclude_from():
    report = {
        "summary": {"cases": 18, "fix_site_unmapped": 11},
        "stratified": {
            "fix_site_mapped": {"summary": {"cases": 7, "located_rate": 0.71,
                                            "located_denominator": 7,
                                            "shrunk_rate": 0.4,
                                            "shrunk_denominator": 5}},
            "fix_site_unmapped": {"summary": {"cases": 11, "located_rate": 0.0}},
        },
        "usage": {"available": False, "figure": {"method": "none"}},
        "wording_sensitivity": {"diverging": 0, "pairs": []},
    }
    statements = eb.conclusions(report)
    underpowered = [s for s in statements if s.get("caveat")]
    assert underpowered, "a rate over 7 cases must not be reported as a result"
    assert "UNDERPOWERED" in underpowered[0]["caveat"]


def test_the_rendered_report_never_shows_a_token_saving_it_does_not_have():
    report = {
        "corpus": {"cases": 2, "real": 1, "synthetic": 1},
        "methodology": ["stated"],
        "summary": {**_summary(0.5, 0.5, cases=1),
                    "verdicts": {"SHRUNK": 1, "NO_SHRINK": 0, "MISSED": 0,
                                 "UNLOCATABLE": 0},
                    "mean_baseline_best_surface": {"value": 4, "label": eb.ESTIMATE,
                                                   "method": "proxy"},
                    "mean_assisted_surface": {"value": 1, "label": eb.REAL,
                                              "method": "named"},
                    "mean_baseline_searches": {"value": 4, "label": eb.REAL,
                                               "method": "ran"},
                    "mean_assisted_searches": {"value": 0, "label": eb.REAL,
                                               "method": "none"},
                    "mean_seconds": {"baseline": {"value": 0.1, "label": eb.REAL,
                                                  "method": "clock"},
                                     "assisted": {"value": 0.1, "label": eb.REAL,
                                                  "method": "clock"}},
                    "module_choice": {"right": 1, "wrong": 0, "unmapped": 0},
                    "analysis_depth": {"assisted": {eb.FULL_ANALYSIS: 1}},
                    "fix_site_unmapped": 0},
        "results": [],
        "usage": {"available": False,
                  "figure": {"value": None, "label": eb.UNAVAILABLE,
                             "method": "nobody reported counters"}},
        "acceptance": {"verdict": "INCONCLUSIVE", "checks": {},
                       "token_claim": {"status": "UNVERIFIED", "detail": "none"}},
    }
    rendered = eb.render_markdown(report)
    assert "UNAVAILABLE" in rendered
    assert "UNVERIFIED" in rendered
    assert "INCONCLUSIVE" in rendered


# -- the published artifacts describe the corpus that is committed -----------

def test_the_published_result_matches_the_committed_corpus(corpus):
    """A report that quietly describes a different corpus is worse than none."""
    published = json.loads((REPO_ROOT / "benchmarks" / "tokeff_result.json")
                           .read_text(encoding="utf-8"))
    assert [row["case_id"] for row in published["results"]] == [c.case_id for c in corpus]
    assert published["corpus"]["real"] == sum(1 for c in corpus if c.is_real)
    # The bar was written down before the run, and is published with it.
    assert published["acceptance_criteria_preregistered"] == eb.ACCEPTANCE


def test_the_published_report_states_its_verdict_and_its_methodology():
    text = (REPO_ROOT / "docs" / "TOKEFF_BENCHMARK.md").read_text(encoding="utf-8")
    assert "Verdict:" in text
    assert "Methodology, stated before measuring" in text
    assert "UNAVAILABLE" in text, "the unmeasured figures must stay visible"


# -- the knowledge map this repository actually ships -------------------------

def test_every_package_file_is_claimed_by_exactly_one_indexed_module():
    """The indexing rule is completeness, and a rule nobody checks is a wish.

    This is also what keeps the map honest against the benchmark it was
    grown for: a map that covered only the modules the corpus asks about
    would score well and mean nothing.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "index_modules", REPO_ROOT / "scripts" / "knowledge" / "index_modules.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    missing, duplicated = module.check_completeness(REPO_ROOT)
    assert missing == [], f"package files claimed by no module: {missing}"
    assert duplicated == [], f"package files claimed twice: {duplicated}"


def test_every_indexed_path_still_exists():
    """A map that names files that are gone is worse than a smaller one: it
    sends a worker to read something that is not there."""
    knowledge = ProjectKnowledge(REPO_ROOT)
    missing = [path for m in knowledge.module_states()
               for path in (m.paths or ()) if not (REPO_ROOT / path).exists()]
    assert missing == [], f"indexed paths that no longer exist: {missing}"


def test_the_map_covers_the_package_it_claims_to():
    knowledge = ProjectKnowledge(REPO_ROOT)
    indexed = {p for m in knowledge.module_states() for p in (m.paths or ())}
    package = {f"terminal_mcp/{p.name}"
               for p in (REPO_ROOT / "terminal_mcp").glob("*.py")
               if p.name != "__init__.py"}
    assert package - indexed == set()


def test_map_provenance_is_recorded_with_the_measurement():
    """A retrieval result only means something beside the map it came from."""
    knowledge = ProjectKnowledge(REPO_ROOT)
    provenance = eb.map_provenance(knowledge, repo_root=REPO_ROOT)
    assert provenance["available"] is True
    assert provenance["package_coverage"] == 1.0
    assert provenance["modules"] >= 30
    # Modules whose largest file carries no docstring have no summary, and the
    # report says which rather than inventing one.
    assert isinstance(provenance["modules_without_summary"], list)


def test_a_missing_map_is_reported_not_raised(tmp_path):
    class Broken:
        def module_states(self):
            raise RuntimeError("no map here")

    provenance = eb.map_provenance(Broken(), repo_root=tmp_path)
    assert provenance["available"] is False
    assert "no map here" in provenance["detail"]


# -- the secondary comparison is reported, and never decides anything --------

def test_the_union_comparison_is_secondary_and_absent_when_the_site_was_missed():
    located = _case_result(baseline_surface=1, baseline_hit=True,
                           assisted_files=["pkg/widget.py"], assisted_hit=True)
    # A miss has nothing to compare: None, not False.
    missed = _case_result(baseline_surface=1, baseline_hit=True,
                          assisted_files=[], assisted_hit=False)
    assert missed.smaller_than_union is None
    # The union in the fixture equals the best surface, so 1 file is not smaller.
    assert located.smaller_than_union is False


def test_the_secondary_figure_does_not_enter_the_verdict():
    summary = _summary(0.29, 0.40)
    summary["secondary_vs_union_baseline"] = {"smaller": 8, "denominator": 8}
    assert eb.acceptance(summary, usage={"available": False})["verdict"] == "FAIL"


def test_the_report_records_which_map_each_prior_run_measured():
    """'Did indexing help?' cannot be answered by one run, so the runs that
    came before are published beside the current one."""
    assert eb.PRIOR_RUNS, "the earlier runs are evidence, not scratch work"
    for run in eb.PRIOR_RUNS:
        assert run["map"] and run["located"] and run["note"]
    text = (REPO_ROOT / "docs" / "TOKEFF_BENCHMARK.md").read_text(encoding="utf-8")
    assert "How this has moved" in text
    for run in eb.PRIOR_RUNS:
        assert run["date"] in text
