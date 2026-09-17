"""Dogfood: do the budget and the gate actually constrain a RUN?

WHY THIS FILE EXISTS ALONGSIDE tests/test_bug_triage.py

That file already checks `budget_check` and `gate` as functions: given these
numbers, is the verdict right. It proves the arithmetic. It cannot prove the
claim the system actually makes, which is that a worker is STOPPED -- a
verdict nothing consults constrains nothing, and "the budget is enforced" and
"the budget is computable" are different sentences.

So everything below drives `tests/dogfood_worker.DogfoodWorker`, which opens
real files in THIS repository and runs real `git grep`, asking the shipped
`bug_spec` functions for permission before each one. The counts land in a
real `work_telemetry` row. Nothing here re-implements a limit.

WHAT IS CLAIMED, AND WHAT IS NOT

Claimed: a worker that routes its reads and searches through the contract is
stopped at exactly the declared limits, hands the task back instead of
widening it, and cannot get past the budget without leaving a record of why.

NOT claimed: that an unmediated agent obeys a budget it merely read in a
prompt. No test can establish that, and asserting it here would manufacture
exactly the false confidence the rest of this system refuses.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.dogfood_worker import BudgetRefused, DogfoodWorker, SpecNotExecutable

from terminal_mcp import bug_spec as bs
from terminal_mcp import work_inbox as wi
from terminal_mcp import work_planning as wp
from terminal_mcp import work_spec as ws
from terminal_mcp import work_telemetry as wt

REPO_ROOT = Path(__file__).resolve().parent.parent

# Five real files in this package, and a sixth the budget must refuse. Real
# paths on purpose: a run over invented ones would prove the harness, not the
# repository.
FIVE_REAL_FILES = (
    "terminal_mcp/work_service.py",
    "terminal_mcp/work_eligibility.py",
    "terminal_mcp/queue_service.py",
    "terminal_mcp/session_registry.py",
    "terminal_mcp/work_store.py",
)
SIXTH_FILE = "terminal_mcp/queue_store.py"


def _package_file_count() -> int:
    return len(list((REPO_ROOT / "terminal_mcp").glob("*.py")))


@pytest.fixture()
def telemetry():
    return wt.TaskTelemetry(task_id="dogfood-budget")


@pytest.fixture()
def spec_store(tmp_path):
    store = bs.BugSpecStore(tmp_path / "specs.db")
    yield store
    store.close()


def _l1_spec() -> bs.BugSpec:
    """A genuinely L1 spec for a defect this repository really had."""
    return bs.plan_from_report(
        title="A -work session running an agent is shown IDLE on the Work page",
        symptom="the Work page lists a -work session as IDLE while Claude runs in it",
        expected="it reports BUSY whenever something is running in that pane",
        module="work_service",
        files=["terminal_mcp/work_service.py", "terminal_mcp/work_eligibility.py"],
        entry_points=["WorkService.workers"],
        search_terms=["current_task", "occupancy"],
        flow="dashboard route -> WorkService.workers -> queue.status -> session registry",
        suspected_cause="occupancy is derived only from the queue's current_task",
        cause_confidence="HIGH",
        fix_strategy=["read the session's own running command as a second signal"],
        do_not_touch=["terminal_mcp/dashboard.py"],
        regression_areas=["work eligibility"],
        acceptance=["a -work session running an agent with no queue task is not IDLE"],
    )


def _thin_spec() -> bs.BugSpec:
    """What a planner produces when it has not done the analysis yet."""
    return bs.plan_from_report(
        title="The Work page looks wrong",
        symptom="something about the session list is off",
    )


def _worker(spec, telemetry, spec_store=None) -> DogfoodWorker:
    return DogfoodWorker(spec=spec, repo_root=REPO_ROOT, telemetry=telemetry,
                         spec_store=spec_store)


# -- the spec under test is really L1 -----------------------------------------

def test_the_dogfood_spec_is_an_l1_with_the_budget_the_contract_declares(telemetry):
    spec = _l1_spec()
    assert spec.level() == bs.L1
    assert bs.FILE_SEARCH_BUDGET[spec.level()] == {"max_files": 5, "max_search_rounds": 2}
    assert bs.gate(spec)["ready"], "the run below has to start from an executable spec"


# -- property 1: an L1 run really stops at 5 files and 2 search rounds --------

def test_an_l1_run_is_stopped_before_it_opens_a_sixth_file(telemetry):
    worker = _worker(_l1_spec(), telemetry)
    worker.start()

    for path in FIVE_REAL_FILES:
        assert worker.read(path), f"{path} should be readable and non-empty"

    with pytest.raises(BudgetRefused) as refused:
        worker.read(SIXTH_FILE)

    # Stopped BEFORE the read, not scolded after it: the sixth file is never
    # opened, so the budget is a limit rather than a statistic.
    assert worker.opened == list(FIVE_REAL_FILES)
    assert telemetry.files_read == 5
    assert "6 files read (budget 5)" in refused.value.check["exceeded"]


def test_an_l1_run_is_stopped_before_a_third_search_round(telemetry):
    worker = _worker(_l1_spec(), telemetry)
    worker.start()

    # Real greps over this repository. The first term really does occur here.
    assert worker.search("current_task"), "the spec's own search term must match something"
    worker.search("occupancy")

    with pytest.raises(BudgetRefused) as refused:
        worker.search("session")

    assert telemetry.search_rounds == 2
    assert worker.searched == ["current_task", "occupancy"]
    assert any("search rounds" in line for line in refused.value.check["exceeded"])


def test_the_refusal_hands_an_l1_back_rather_than_turning_it_into_an_investigation(telemetry):
    worker = _worker(_l1_spec(), telemetry)
    worker.start()
    for path in FIVE_REAL_FILES:
        worker.read(path)

    with pytest.raises(BudgetRefused) as refused:
        worker.read(SIXTH_FILE)

    check = refused.value.check
    # The action is the point. "You are at 6 of 5" tells a worker nothing it
    # can do; NEEDS_REDEFINE tells it exactly what to do next.
    assert check["action"] == bs.NEEDS_REDEFINE
    assert "was not actually an L1" in check["note"]


def test_an_l1_run_touches_a_fraction_of_the_repository(telemetry):
    """The honest version of "narrowing": a count of real files, not a guess."""
    worker = _worker(_l1_spec(), telemetry)
    worker.start()
    for path in FIVE_REAL_FILES:
        worker.read(path)

    package = _package_file_count()
    assert len(worker.opened) <= 5
    assert len(worker.opened) < package / 10, (
        f"the run opened {len(worker.opened)} of {package} package files; "
        f"that is not narrowing")


# -- the only way past the budget is an explicit, recorded escalation ---------

def test_the_budget_yields_only_to_an_explicit_recorded_escalation(telemetry):
    worker = _worker(_l1_spec(), telemetry)
    worker.start()
    for path in FIVE_REAL_FILES:
        worker.read(path)
    with pytest.raises(BudgetRefused):
        worker.read(SIXTH_FILE)

    report = worker.escalate(
        why="the named files do not contain the occupancy decision",
        known="workers() reads queue.status; the IDLE label is produced there",
        missing="which module owns the session's own running-command signal",
        redefine_request="name the module that reports the live pane command")

    # Only now does the sixth read go through, and the reason is on the row.
    assert worker.read(SIXTH_FILE)
    assert telemetry.files_read == 6
    assert telemetry.budget_escalations == 1
    assert any("budget escalation" in note for note in telemetry.notes)
    assert report["TOKEN_BUDGET_STATUS"] == bs.HIT_SOFT_LIMIT


def test_an_escalation_without_a_reason_is_refused(telemetry):
    """An unexplained overrun and a justified one must not be the same row."""
    with pytest.raises(ValueError):
        telemetry.record_budget_escalation("   ")
    assert telemetry.budget_escalations == 0


def test_the_escalation_continues_the_task_instead_of_resetting_it(telemetry, spec_store):
    spec = spec_store.save(_l1_spec())
    worker = _worker(spec, telemetry, spec_store)
    worker.start()

    report = worker.escalate(why="scope widened", known="the flow is confirmed",
                             missing="the second occupancy signal",
                             redefine_request="name the module that owns it")

    assert report["TASK_CONTINUES"] is True
    assert report["BUG"] == spec.bug_id
    # The same spec, still on record with its analysis: a redefine adds
    # detail to this, it does not start a second one.
    assert spec_store.get(spec.bug_id).suspected_root_cause


# -- property 5: an incomplete spec stops the worker BEFORE any audit ---------

def test_an_incomplete_spec_stops_the_worker_before_it_opens_anything(telemetry):
    worker = _worker(_thin_spec(), telemetry)
    report = worker.start()

    assert report["ready"] is False
    assert report["handoff"] is None

    with pytest.raises(SpecNotExecutable):
        worker.read("terminal_mcp/work_service.py")
    with pytest.raises(SpecNotExecutable):
        worker.search("current_task")

    # The measurable claim: nothing was read and nothing was searched, out of
    # a package of this many files. This is what "no broad repo audit" means
    # when it is a fact rather than an instruction.
    assert worker.opened == [] and worker.searched == []
    assert telemetry.files_read == 0 and telemetry.search_rounds == 0
    assert _package_file_count() > 50, "the repo being audited would be a big one"


def test_the_blocked_worker_hands_back_questions_instead_of_findings(telemetry):
    worker = _worker(_thin_spec(), telemetry)
    worker.start()

    reply = worker.blocked_by
    assert reply["STATUS"] == bs.NEEDS_REDEFINE
    assert reply["MISSING"]
    questions = reply["QUESTIONS_FOR_PLANNER"]
    assert questions and all(q.strip() for q in questions)
    # Each one names a concrete thing the planner can supply. A thin spec is
    # sent back with the list of what to fill in, never with "can you give
    # more detail" -- that would spend a round trip to learn nothing.
    assert any(q.endswith("?") for q in questions)
    assert not any("more detail" in q.lower() or "clarify" in q.lower() for q in questions)
    assert "not investigating further" in reply["NOTE"]
    # The hand-back itself is counted, so a fleet can see how often specs
    # arrive unusable rather than only how long tasks took.
    assert telemetry.redefine_count == 1


def test_the_same_spec_resumes_after_refinement_rather_than_a_second_one(telemetry):
    spec = _thin_spec()
    worker = _worker(spec, telemetry)
    assert worker.start()["ready"] is False
    original_id = spec.bug_id

    # The planner adds what was missing. To the SAME spec object -- this is
    # the resume, and it is why the analysis already done is not thrown away.
    spec.expected_behavior = "it reports BUSY whenever something runs in that pane"
    spec.likely_module = "work_service"
    spec.likely_files = ("terminal_mcp/work_service.py",)
    spec.relevant_flow = "dashboard route -> WorkService.workers -> queue.status"
    spec.suspected_root_cause = "occupancy comes only from the queue's current_task"
    spec.root_cause_confidence = "HIGH"
    spec.fix_strategy = ("read the session's own running command as a second signal",)
    spec.do_not_touch = ("terminal_mcp/dashboard.py",)
    spec.acceptance_criteria = ("a -work session running an agent is not shown IDLE",)

    resumed = _worker(spec, wt.TaskTelemetry(task_id="dogfood-budget"))
    report = resumed.start()

    assert report["ready"] is True
    assert spec.bug_id == original_id, "a refinement must not mint a new spec"
    assert resumed.read("terminal_mcp/work_service.py")


def test_a_refined_spec_does_not_create_a_second_queue_task(tmp_path):
    """The "same task" half of the claim, against the real queue engine."""
    from terminal_mcp.queue_service import QueueService
    from terminal_mcp.queue_store import QueueStore

    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    task = queue.create_task("Work page shows IDLE", "investigate", session=None)

    spec = _thin_spec()
    spec.queue_task_id = task["task_id"]
    worker = _worker(spec, wt.TaskTelemetry(task_id=task["task_id"]))
    assert worker.start()["ready"] is False

    spec.expected_behavior = "it reports BUSY when something runs in that pane"
    spec.likely_module = "work_service"
    spec.likely_files = ("terminal_mcp/work_service.py",)
    spec.relevant_flow = "workers() -> queue.status"
    spec.suspected_root_cause = "occupancy comes only from current_task"
    spec.root_cause_confidence = "HIGH"
    spec.fix_strategy = ("add a second signal",)
    spec.do_not_touch = ("terminal_mcp/dashboard.py",)
    spec.acceptance_criteria = ("a busy -work session is not shown IDLE",)
    assert _worker(spec, wt.TaskTelemetry()).start()["ready"] is True

    # One task before the redefine and one after: the refinement resumed it.
    assert spec.queue_task_id == task["task_id"]
    assert queue.board()["counts"]["backlog"] == 1


def test_the_planning_pipeline_resumes_the_same_spec_id(tmp_path):
    """The same property at the other end of the system, on the real repo."""
    store = ws.WorkSpecStore(tmp_path / "work_specs.db")
    # The task type is stated rather than inferred: what is under test here is
    # the resume, and tests/test_dogfood_work_v1.py already owns the question
    # of whether a defect report classifies as a BUG.
    planned = wp.plan("The Work page shows a busy -work session as IDLE",
                      store=store, task_type=ws.BUG, cwd=str(REPO_ROOT))
    assert planned.status == ws.NEEDS_REDEFINE
    assert planned.spec.redefine_reason, "the refusal has to persist, not just return"

    resumed = wp.redefine(store, planned.spec.spec_id, {
        "symptom": "a -work session running Claude is shown as IDLE",
        "expected_behavior": "it shows BUSY whenever something is running in it",
        "likely_module": "work_service",
        "likely_files": ["terminal_mcp/work_service.py"],
        "entry_points": ["WorkService.workers"],
        "search_terms": ["current_task"],
        "relevant_flow": "dashboard route -> WorkService.workers -> queue.status",
        "hypothesis": "occupancy is derived only from the queue's current_task",
        "fix_strategy": ["read session state as a second signal"],
        "do_not_touch": ["work_eligibility.py"],
        "test_plan": ["a busy -work session with no queue task is not IDLE"],
        "test_runbook": "test_gate",
        "acceptance_criteria": ["the Work page shows BUSY for a running -work session"],
    })

    assert resumed.status == ws.SPEC_READY, resumed.gate_report["missing"]
    assert resumed.spec.spec_id == planned.spec.spec_id
    assert resumed.spec.redefine_count >= 1
    # The analysis from the first pass is still attached to the resumed spec.
    assert resumed.spec.source_commit == planned.spec.source_commit


# -- property 3: HARD parks on a human, and the SAME issue resumes ------------

@pytest.fixture()
def inbox(tmp_path):
    store = wi.InboxStore(tmp_path / "inbox.db")
    yield wi.InboxService(store, planner_concurrency=2, claim_lease_seconds=900)
    store.close()


HARD_REPORT = ("Sessions are lost after re-login, it used to work before v2 and "
               "only happens under concurrent use")


def test_a_hard_report_is_triaged_hard_and_asks_at_most_three_precise_questions():
    verdict = bs.triage("Sessions lost after re-login", HARD_REPORT)
    assert verdict["difficulty"] == "HARD"
    assert verdict["assist_recommended"] is True

    spec = bs.plan_from_report(title="Sessions lost after re-login", symptom=HARD_REPORT)
    request = bs.developer_assist_request(
        spec, verdict, findings=["the session row survives; the pane does not"],
        hypotheses=["the registry is keyed on something the re-login changes"])

    assert request is not None
    assert 1 <= len(request["questions"]) <= bs.MAX_ASSIST_QUESTIONS
    # Precise means answerable in a sentence. A vague ask spends a developer's
    # attention for nothing, which is how they stop answering at all.
    assert all(q.endswith("?") for q in request["questions"])
    assert not any("more detail" in q.lower() for q in request["questions"])
    # The analysis travels WITH the questions, so the developer answers in one
    # line instead of reconstructing the context first.
    assert request["current_findings"] and request["current_hypotheses"]
    assert "never block" in request["if_unavailable"]


def test_a_parked_issue_is_not_handed_to_another_planner(inbox):
    issue_id = inbox.capture(f"- {HARD_REPORT}")["issues"][0]["issue_id"]
    inbox.claim_for_planning("planner-1")
    inbox.request_user_hint(issue_id, ["Does it fail before or after the auth step?"],
                            findings=["the registry row survives the re-login"])

    assert inbox.store.get(issue_id).state == wi.NEEDS_USER_HINT
    # Re-planning an issue that is waiting on a human would discard the
    # analysis that produced the question.
    assert inbox.claim_for_planning("planner-2")["status"] == "NOTHING_TO_CLAIM"


def test_a_hint_request_without_a_question_is_refused(inbox):
    issue_id = inbox.capture(f"- {HARD_REPORT}")["issues"][0]["issue_id"]
    result = inbox.request_user_hint(issue_id, [])
    assert result["error"] == "QUESTIONS_REQUIRED"
    assert inbox.store.get(issue_id).state != wi.NEEDS_USER_HINT


def test_the_same_issue_resumes_with_its_earlier_analysis_intact(inbox):
    issue_id = inbox.capture(f"- {HARD_REPORT}")["issues"][0]["issue_id"]
    inbox.claim_for_planning("planner-1")
    findings = ["the registry row survives the re-login",
                "the pane is gone before the dashboard reads it"]
    inbox.request_user_hint(issue_id, ["Does it fail before or after the auth step?"],
                            findings=findings)

    inbox.attach_human_hint(issue_id, "it fails after auth, only on the second login")
    resumed = inbox.store.get(issue_id)

    assert resumed.issue_id == issue_id, "a hint must resume the issue, not replace it"
    assert resumed.state == wi.TRIAGED
    assert resumed.metadata["findings_before_hint"] == findings
    assert resumed.questions, "the question that was asked stays on the record"
    assert resumed.human_hints == ("it fails after auth, only on the second login",)
    # And it is claimable again -- by the same id, with everything above still on it.
    assert inbox.claim_for_planning("planner-2")["issue"]["issue_id"] == issue_id


# -- property 4: the plan handshake is recorded as telemetry ------------------

@pytest.mark.parametrize("status", [bs.PLAN_CONFIRMED, bs.PLAN_ADJUSTED, bs.PLAN_MISMATCH])
def test_each_plan_verdict_reaches_both_the_telemetry_row_and_the_spec(
        status, telemetry, spec_store):
    spec = spec_store.save(_l1_spec())
    worker = _worker(spec, telemetry, spec_store)
    worker.start()
    worker.verify_plan(status, note="checked against HEAD")

    assert telemetry.plan_status == status
    assert telemetry.plan_note == "checked against HEAD"
    # Same vocabulary in both places, so the two records can be compared.
    assert spec_store.get(spec.bug_id).plan_status == status


def test_a_plan_verdict_survives_the_telemetry_store(tmp_path, telemetry):
    store = wt.TelemetryStore(tmp_path / "telemetry.db")
    try:
        telemetry.record_plan_outcome(bs.PLAN_MISMATCH, note="the named function moved")
        store.save(telemetry)
        row = store.get(telemetry.telemetry_id)
        assert row["plan_status"] == bs.PLAN_MISMATCH
        assert row["plan_note"] == "the named function moved"
        # Reopened rows keep the verdict, so a restart does not lose it.
        assert wt.TaskTelemetry.from_dict(row).plan_status == bs.PLAN_MISMATCH
    finally:
        store.close()


def test_an_invented_plan_verdict_is_refused(telemetry):
    with pytest.raises(ValueError):
        telemetry.record_plan_outcome("PLAN_PROBABLY_FINE")
    assert telemetry.plan_status is None


def test_an_unanswered_plan_check_is_never_counted_as_a_confirmation():
    answered = wt.TaskTelemetry(task_id="a")
    answered.record_plan_outcome(bs.PLAN_MISMATCH)
    summary = wt.summarise([answered, wt.TaskTelemetry(task_id="b")])

    outcomes = summary["plan_outcomes"]
    assert outcomes[bs.PLAN_MISMATCH] == 1
    assert outcomes[bs.PLAN_CONFIRMED] == 0
    assert outcomes["unreported"] == 1
    assert outcomes["answered"] == 1
    assert outcomes["mismatch_rate"] == 1.0


def test_a_mismatch_rate_over_no_verdicts_is_unavailable_rather_than_zero():
    """Zero would read as "our specs are always right"; it means nobody checked."""
    summary = wt.summarise([wt.TaskTelemetry(task_id="a"), wt.TaskTelemetry(task_id="b")])
    assert summary["plan_outcomes"]["mismatch_rate"] is None
    assert summary["plan_outcomes"]["unreported"] == 2


def test_budget_escalations_are_aggregated_so_the_budget_can_be_audited():
    spent = wt.TaskTelemetry(task_id="a")
    spent.record_budget_escalation("the named files did not contain the cause")
    summary = wt.summarise([spent, wt.TaskTelemetry(task_id="b")])
    assert summary["budget_escalations"] == 1
