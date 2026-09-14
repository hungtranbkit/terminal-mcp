"""The planning contract has to hold for work that is not a bug.

The gap this covers: `bug_spec` requires a symptom and a root cause, so a
planner writing a feature spec either leaves it half-empty or invents prose to
get past the gate. Both push the analysis back into the worker, which is the
cost a spec exists to remove.

So these assert the properties an operator and a worker actually depend on:
a feature is refused until its BOUNDARIES exist, reuse is not optional, the
budget for a feature is larger than for a bug but still finite, and the
questions handed back are answerable rather than a score.
"""
from __future__ import annotations

import pytest

from terminal_mcp import work_spec as ws
from terminal_mcp.bug_spec import L1, L2, L3, NEEDS_REDEFINE, SPEC_READY
from terminal_mcp.project_knowledge import SecretInKnowledge


@pytest.fixture
def store(tmp_path):
    return ws.WorkSpecStore(tmp_path / "specs.db")


def _complete_feature(**overrides) -> ws.WorkSpec:
    """A feature spec with every required field earned, so a test can remove
    exactly one thing and assert on that one thing."""
    spec = ws.plan_from_request(
        title="CSV export for the reports page",
        requirement="An operator can download the current report as CSV",
        task_type=ws.FEATURE_NEW,
        problem="Finance exports by hand and the numbers drift",
        user_value="Finance stops retyping the numbers into a spreadsheet",
        expected_outcome="An Export button on the reports page downloads a CSV",
        scope=("the reports page export button", "a CSV serialiser for report rows"),
        out_of_scope=("XLSX", "scheduled/emailed exports", "a settings screen"),
        arch_impact="no new service; one route on the existing dashboard app",
        reuse_candidates=("redaction.redact_output for the cell values",
                          "the existing report query in report_service.rows()"),
        likely_files=("terminal_mcp/dashboard.py",),
        api_contract="GET /dashboard/api/reports/export.csv -> text/csv, same auth guard",
        existing_patterns=("dashboard routes register via register_dashboard()",),
        implementation_plan=("add the serialiser", "add the route", "wire the button"),
        test_plan=("unit: serialiser quotes embedded commas",),
        test_runbook="test_gate",
        acceptance_criteria=("clicking Export downloads a CSV whose rows match the table",),
        dependencies=(),
        risks=("a large report could block the event loop",),
    )
    for key, value in overrides.items():
        setattr(spec, key, value)
    return spec


# -- a feature is not executable until its boundaries exist -----------------------

def test_a_bare_feature_request_is_refused_with_answerable_questions():
    """The failure mode this prevents: a worker receiving "add CSV export" and
    deciding the scope itself, three files deep."""
    spec = ws.plan_from_request(title="Add CSV export",
                                requirement="Operators want CSV")
    report = ws.gate(spec)

    assert report["status"] == NEEDS_REDEFINE
    assert report["handoff"] is None, "a spec that is not ready hands nothing to a worker"
    assert "out_of_scope" in report["missing"]
    assert "reuse_candidates" in report["missing"]
    # The reply is something a planner can act on without reading the score.
    questions = report["reply_to_planner"]["QUESTIONS_FOR_PLANNER"]
    assert questions and all(q.endswith("?") for q in questions)


def test_scope_without_out_of_scope_still_fails_the_gate():
    """Scope alone does not bound a feature -- "add CSV export" with no
    out-of-scope line is how XLSX and a scheduler get built too."""
    spec = _complete_feature(out_of_scope=())
    report = ws.gate(spec)

    assert report["status"] == NEEDS_REDEFINE
    assert "out_of_scope" in report["missing"]


@pytest.mark.parametrize("dropped", ["requirement", "problem", "user_value",
                                     "expected_outcome", "scope", "out_of_scope",
                                     "reuse_candidates", "acceptance_criteria"])
def test_removing_a_required_field_never_makes_a_spec_readier(dropped):
    """The perverse edge a weighted threshold has on its own.

    `level()` is derived from the evidence present, so deleting a field can
    drop the spec to a lower level, which lowers the threshold, which lets the
    THINNER spec pass. Caught for real: removing `out_of_scope` from a complete
    feature spec turned NEEDS_REDEFINE into SPEC_READY.

    A second edge, caught the same way: ADDING required fields raised the total
    weight, so dropping `user_value` stopped being decisive. Weight says how
    much a gap costs, not whether the task can be executed without it.
    """
    complete = _complete_feature()
    assert ws.gate(complete)["status"] == SPEC_READY

    thinner = _complete_feature()
    setattr(thinner, dropped, () if isinstance(getattr(thinner, dropped), tuple) else "")

    assert ws.gate(thinner)["status"] == NEEDS_REDEFINE, \
        f"dropping {dropped} made the spec pass"


def test_a_blocking_field_is_reported_apart_from_a_scoring_shortfall():
    """A planner needs to know which gaps are unbuyable: no amount of detail
    elsewhere substitutes for a missing rollback plan."""
    spec = _complete_feature(out_of_scope=())
    report = ws.gate(spec)

    assert report["mandatory_missing"] == ["out_of_scope"]
    assert report["reply_to_planner"]["BLOCKING"] == ["out_of_scope"]


def test_reuse_candidates_are_required_not_advisory():
    """A planner that has not looked for existing code has not finished
    planning: the expensive failure is a second implementation."""
    spec = _complete_feature(reuse_candidates=())
    report = ws.gate(spec)

    assert report["status"] == NEEDS_REDEFINE
    assert "reuse_candidates" in report["missing"]


def test_a_complete_feature_spec_is_ready_and_hands_off_the_reuse_list():
    spec = _complete_feature()
    report = ws.gate(spec)

    assert report["status"] == SPEC_READY, report["missing"]
    handoff = report["handoff"]
    assert handoff["TYPE"] == ws.FEATURE_NEW
    assert handoff["OUT_OF_SCOPE"], "the boundary must survive into the handoff"
    assert handoff["REUSE_FIRST"], "the worker is told what to reuse before building"
    assert "PLAN_CONFIRMED" in handoff["VERIFY_PLAN_FIRST"], \
        "a spec is a hypothesis; the worker still verifies before changing anything"


# -- every type gets the fields it actually needs ---------------------------------

@pytest.mark.parametrize("task_type", ws.TASK_TYPES)
def test_every_task_type_has_a_required_field_set(task_type):
    """A type nobody enumerated is a type whose spec nobody validates."""
    assert ws.REQUIRED_FIELDS_BY_TYPE.get(task_type), task_type


def test_a_research_task_is_refused_without_a_boundary():
    """An open-ended investigation with no boundary is how a "quick look"
    becomes a repository audit."""
    spec = ws.plan_from_request(title="Should we move to Postgres?",
                                task_type=ws.RESEARCH,
                                research_question="Is Postgres worth the migration?")
    report = ws.gate(spec)

    assert report["status"] == NEEDS_REDEFINE
    assert "out_of_scope" in report["missing"]


def test_a_deploy_task_is_refused_without_a_rollback_plan():
    spec = ws.plan_from_request(title="Deploy the reports build to staging",
                                task_type=ws.DEPLOY,
                                requirement="ship build 1.4.2 to staging")
    report = ws.gate(spec)

    assert report["status"] == NEEDS_REDEFINE
    assert "rollback_plan" in report["missing"]


def test_a_refactor_must_say_what_proves_behaviour_unchanged():
    spec = ws.plan_from_request(title="Extract the queue claim helper",
                                task_type=ws.REFACTOR,
                                requirement="one claim path instead of three")
    report = ws.gate(spec)

    assert "regression_areas" in report["missing"], \
        "a refactor whose acceptance is 'nothing changed' must say how that is shown"


# -- the level is derived from evidence, never asserted ---------------------------

def test_a_feature_naming_no_files_and_no_contract_cannot_be_l1():
    spec = ws.plan_from_request(title="Add a billing page",
                                requirement="operators can see invoices")
    assert spec.level() == L3


def test_a_feature_with_place_shape_and_boundary_is_l1():
    assert _complete_feature().level() == L1


def test_research_is_never_l1_however_much_is_written():
    spec = ws.plan_from_request(
        title="Compare queue backends", task_type=ws.RESEARCH,
        research_question="Which backend survives a restart mid-claim?",
        out_of_scope=("anything not already vendored",),
        likely_files=("terminal_mcp/queue_store.py",),
        api_contract="n/a")
    assert spec.level() in (L2, L3), "if the answer were known it would not be research"


# -- budgets are per type, and they are soft -------------------------------------

def test_a_feature_gets_a_larger_budget_than_a_bug_at_the_same_level():
    """A feature legitimately reads more: it has to find the pattern to follow
    and the code to reuse. It is still not a licence to read the repository."""
    feature = ws.FILE_SEARCH_BUDGET[ws.FEATURE_NEW][L1]["max_files"]
    bug = ws.FILE_SEARCH_BUDGET[ws.BUG][L1]["max_files"]
    assert feature > bug
    assert feature < 20, "larger, but still targeted"


def test_going_over_budget_on_an_l1_spec_hands_it_back_rather_than_widening():
    spec = _complete_feature()
    assert spec.level() == L1
    verdict = ws.budget_check(spec, files_read=99, search_rounds=1)

    assert verdict["within_budget"] is False
    assert verdict["action"] == NEEDS_REDEFINE
    assert verdict["exceeded"], "the worker is told which allowance it crossed"


def test_going_over_budget_on_an_unknown_spec_asks_for_a_reason_not_a_stop():
    spec = ws.plan_from_request(title="Investigate slow dashboard",
                                task_type=ws.RESEARCH,
                                research_question="why is the dashboard slow?")
    verdict = ws.budget_check(spec, files_read=999, search_rounds=99)

    assert verdict["action"] == "escalate_with_reason"
    assert verdict["within_budget"] is False


def test_budget_stays_within_the_declared_limits_when_nothing_is_exceeded():
    spec = _complete_feature()
    verdict = ws.budget_check(spec, files_read=1, search_rounds=1)
    assert verdict["within_budget"] is True
    assert verdict["action"] == "continue"


# -- type inference is a default the planner overrides ----------------------------

@pytest.mark.parametrize("text,expected", [
    ("the export button is broken and throws", ws.BUG),
    ("lỗi: nút export không chạy", ws.BUG),
    ("add CSV export to reports", ws.FEATURE_NEW),
    ("thêm tính năng xuất CSV", ws.FEATURE_NEW),
    ("refactor the queue claim helper", ws.REFACTOR),
    ("integrate with the MISA webhook", ws.INTEGRATION),
    ("research whether Postgres is worth it", ws.RESEARCH),
    ("deploy 1.4.2 to staging", ws.DEPLOY),
])
def test_task_type_is_inferred_from_the_request(text, expected):
    assert ws.infer_task_type(text) == expected


def test_an_unrecognised_request_defaults_to_feature_not_bug():
    """Being asked for boundaries on something that turns out to be a bug is
    cheaper than being asked for a root cause that does not exist."""
    assert ws.infer_task_type("zzzz qqqq") == ws.FEATURE_NEW


def test_an_explicit_task_type_overrides_inference():
    spec = ws.plan_from_request(title="the export button is broken",
                                task_type=ws.FEATURE_NEW,
                                requirement="rebuild export properly")
    assert spec.task_type == ws.FEATURE_NEW


def test_an_unknown_task_type_is_refused():
    with pytest.raises(ValueError):
        ws.plan_from_request(title="x", task_type="SOMETHING_ELSE")


# -- classification is not re-derived here ---------------------------------------

def test_an_auth_touching_feature_is_escalated_by_the_shared_classifier():
    """The exclusion list lives in one place; this module must not have a
    second opinion about which changes are risky."""
    spec = ws.plan_from_request(title="Add SSO login for operators",
                                requirement="operators sign in with SSO")
    assert spec.execution_mode in ("SAFE", "NORMAL")
    assert spec.risk in ("HIGH", "MEDIUM")


# -- persistence ------------------------------------------------------------------

def test_a_spec_round_trips_through_the_store(store):
    spec = store.save(_complete_feature())
    loaded = store.get(spec.spec_id)

    assert loaded is not None
    assert loaded.task_type == ws.FEATURE_NEW
    assert loaded.out_of_scope == spec.out_of_scope
    assert loaded.reuse_candidates == spec.reuse_candidates
    assert ws.gate(loaded)["status"] == SPEC_READY


def test_specs_can_be_listed_by_type_and_linked_to_a_parent(store):
    parent = store.save(_complete_feature())
    child = _complete_feature()
    child.spec_id = "spec_child"
    child.parent_spec_id = parent.spec_id
    child.title = "subtask: the serialiser"
    store.save(child)

    assert [s.spec_id for s in store.children(parent.spec_id)] == ["spec_child"]
    assert len(store.list(task_type=ws.FEATURE_NEW)) == 2
    assert store.list(task_type=ws.BUG) == []


def test_a_secret_is_refused_rather_than_stripped(store):
    """A spec is long-lived and rarely re-read -- the worst place for a quiet
    strip to fail open."""
    with pytest.raises(SecretInKnowledge):
        ws.plan_from_request(
            title="Add export",
            requirement="use api_key=sk-live-abcdef0123456789abcdef0123456789")


# -- what a spec records about the run it was planned for -------------------------

def test_the_budget_is_recorded_on_the_spec_not_only_returned():
    """"What was this worker allowed to read" has to stay answerable later,
    when the level may have moved."""
    spec = _complete_feature()
    assert spec.file_budget == ws.FILE_SEARCH_BUDGET[ws.FEATURE_NEW][L1]["max_files"]
    assert spec.search_budget > 0


def test_the_handoff_carries_the_budget_and_what_to_do_when_it_is_spent():
    handoff = ws.gate(_complete_feature())["handoff"]
    assert handoff["BUDGET"]["files"] > 0
    assert "NEEDS_REDEFINE" in handoff["BUDGET"]["on_exceed"], \
        "a worker over budget hands back rather than wandering"


def test_the_policy_version_is_bound_at_plan_time(tmp_path, monkeypatch):
    """Bound when the spec is planned, not read at execution time: a policy
    that changes mid-flight must not silently redefine what a running task
    agreed to."""
    spec = _complete_feature()
    # The repo ships a canonical policy, so a version is always bindable; the
    # property under test is that it is RECORDED, whatever it says.
    assert spec.policy_version, "a spec must record which rules it ran under"
    assert spec.policy_hash


def test_a_missing_policy_leaves_the_binding_empty_rather_than_inventing_one(monkeypatch):
    import terminal_mcp.work_policy as wp

    def _boom(_cwd):
        raise RuntimeError("no policy here")

    monkeypatch.setattr(wp, "load_policy", _boom)
    spec = ws.WorkSpec(spec_id="s1", title="t")
    ws.bind_policy(spec)
    assert spec.policy_version == ""


# -- a defect described as behaviour, not as a category ---------------------------

@pytest.mark.parametrize("report", [
    "The Work page shows a -work session as IDLE while Claude is actually running in it",
    "the export button shows 0 rows instead of the real count",
    "it still shows the old total after a refresh",
    "the badge says OFFLINE even though the node answered",
    "màn hình vẫn hiện số cũ sau khi lưu",
])
def test_a_defect_reported_as_observed_versus_expected_classifies_as_a_bug(report):
    """Found by the dogfood, not by review.

    The real Work-UI occupancy report used none of the words "bug", "broken",
    "error" or "fails" -- it just described what the screen showed against what
    was true. It fell through to FEATURE_NEW, and the gate then asked a defect
    for its user value and an out-of-scope list. Most real reports describe the
    behaviour rather than naming its category.
    """
    assert ws.infer_task_type(report) == ws.BUG


@pytest.mark.parametrize("request_text", [
    "Should we move to Postgres?",
    "Add a work_spec_export tool that returns one spec as markdown",
    "refactor the queue claim helper so there is one claim path",
])
def test_the_contrast_markers_do_not_swallow_ordinary_requests(request_text):
    """The markers are multi-word on purpose: a bare "should" or "while"
    appears in perfectly ordinary feature and research requests."""
    assert ws.infer_task_type(request_text) != ws.BUG
