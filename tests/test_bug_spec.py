"""The Bug Planner -> Executor contract: levels, the completeness gate, handoff.

The property under test throughout: a worker handed a thin spec asks for a
better one rather than earning the missing detail itself, because earning it
is the re-analysis this whole layer exists to remove.
"""

from __future__ import annotations

import pytest

from terminal_mcp import bug_spec as bs


def _full(**kwargs):
    base = dict(
        title="Badge chồng tên session", symptom="badge đè lên tên khi màn hình hẹp",
        expected="badge nằm cạnh tên", module="work_ui",
        files=["terminal_mcp/dashboard.py"], flow="renderWorks -> badge template",
        suspected_cause="thiếu flex gap giữa badge và tên", cause_confidence="HIGH",
        fix_strategy=["thêm gap 6px vào .work-row"], acceptance=["không chồng ở 360px"],
        do_not_touch=["terminal_mcp/queue_engine.py"])
    base.update(kwargs)
    # The runbook is DERIVED by the classifier, not supplied by the caller:
    # a planner cannot hand a worker a lighter gate than the change deserves.
    return bs.plan_from_report(**base)


# -- levels are derived ------------------------------------------------------

def test_a_complete_spec_is_l1():
    assert _full().level() == bs.L1


def test_confident_prose_without_files_is_not_l1():
    # This is the check that stops a planner talking itself into certainty.
    spec = _full(files=[], suspected_cause="chắc chắn do CSS", cause_confidence="HIGH")
    assert spec.level() == bs.L2


def test_a_low_confidence_cause_is_not_l1():
    assert _full(cause_confidence="LOW").level() == bs.L2


def test_a_spec_with_no_area_at_all_is_l3():
    assert bs.plan_from_report(title="Có gì đó sai", symptom="chậm").level() == bs.L3


# -- completeness gate -------------------------------------------------------

def test_a_full_spec_passes_the_gate():
    report = bs.gate(_full())
    assert report["ready"] is True
    assert report["score"] >= bs.COMPLETENESS_THRESHOLDS[bs.L1]
    assert report["handoff"]
    assert "reply_to_planner" not in report


def test_a_thin_spec_is_sent_back_with_specific_questions():
    report = bs.gate(bs.plan_from_report(title="Badge sai", symptom="nhìn sai sai",
                                         module="work_ui"))
    assert report["ready"] is False
    reply = report["reply_to_planner"]
    assert reply["STATUS"] == bs.NEEDS_REDEFINE
    assert reply["MISSING"]
    assert reply["QUESTIONS_FOR_PLANNER"]
    for question in reply["QUESTIONS_FOR_PLANNER"]:
        assert question.endswith("?")
    # And it says plainly why it is not just going and finding out.
    assert "re-analysis" in reply["NOTE"]


def test_the_reply_is_ready_to_send_without_composing_one():
    reply = bs.gate(bs.plan_from_report(title="x", symptom="y"))["reply_to_planner"]
    assert set(reply) >= {"STATUS", "BUG", "LEVEL", "COMPLETENESS", "MISSING",
                          "QUESTIONS_FOR_PLANNER"}


def test_a_higher_level_demands_more_evidence():
    assert (bs.COMPLETENESS_THRESHOLDS[bs.L1]
            > bs.COMPLETENESS_THRESHOLDS[bs.L2]
            > bs.COMPLETENESS_THRESHOLDS[bs.L3])


def test_missing_fields_are_weighted_by_what_they_cost_a_worker():
    weights = {name: weight for name, weight, _ in bs.REQUIRED_FIELDS}
    # A missing hypothesis costs far more than a missing do-not-touch list.
    assert weights["hypothesis"] > weights["do_not_touch"]
    assert weights["acceptance_criteria"] > weights["deploy_level"]


def test_an_l3_must_still_state_its_own_limits():
    # L3 may be vague about the cause, never about its boundary: an
    # open-ended investigation is how a "quick look" becomes an audit.
    assert bs.L3_REQUIRED == ("reproduction", "search boundary",
                              "max investigation scope")


# -- handoff -----------------------------------------------------------------

def test_the_handoff_is_compact_and_carries_the_essentials():
    handoff = _full().handoff()
    assert len(handoff) < 2000
    for expected in ("BUG ", bs.L1, "work_ui", "terminal_mcp/dashboard.py",
                     "test_ui_fast"):
        assert expected in handoff


def test_the_handoff_names_runbooks_rather_than_copying_commands():
    handoff = _full().handoff()
    assert "test_ui_fast" in handoff
    # The registry owns the command; copying it here would let them diverge.
    assert "pytest" not in handoff and "bash " not in handoff


def test_uncertainty_is_stated_rather_than_smoothed_over():
    spec = _full()
    spec.uncertain = ("không rõ có ảnh hưởng tới node Windows không",)
    assert "UNCERTAIN" in spec.handoff()


# -- plan verification -------------------------------------------------------

@pytest.mark.parametrize("status", [bs.PLAN_CONFIRMED, bs.PLAN_ADJUSTED, bs.PLAN_MISMATCH])
def test_plan_outcomes_are_recorded(tmp_path, status):
    store = bs.BugSpecStore(tmp_path / "specs.db")
    spec = store.save(_full())
    store.record_plan_outcome(spec.bug_id, status=status, note="checked against HEAD")
    reloaded = store.get(spec.bug_id)
    assert reloaded.plan_status == status
    assert "checked against HEAD" in reloaded.plan_note
    store.close()


def test_a_new_spec_starts_unverified():
    assert _full().plan_status == bs.PLAN_PENDING


# -- escalation --------------------------------------------------------------

def test_escalation_preserves_the_task_instead_of_resetting_it():
    report = bs.escalation(_full(), why="cause not in the named files",
                           known="badge renders from two templates",
                           missing="which template the mobile view uses",
                           redefine_request="name the mobile template")
    assert report["TOKEN_BUDGET_STATUS"] == bs.HIT_SOFT_LIMIT
    # The work already done stays; the planner adds detail and this resumes.
    assert report["TASK_CONTINUES"] is True
    assert set(report) >= {"WHY", "WHAT_IS_KNOWN", "WHAT_IS_MISSING", "REDEFINE_REQUEST"}


def test_escalation_refuses_to_carry_a_secret():
    from terminal_mcp.project_knowledge import SecretInKnowledge
    with pytest.raises(SecretInKnowledge):
        bs.escalation(_full(), why="ghp_" + "a" * 36, known="k", missing="m",
                      redefine_request="r")


# -- compact success output --------------------------------------------------

def test_a_successful_result_is_facts_not_narrative():
    result = bs.compact_result(plan_status=bs.PLAN_CONFIRMED,
                               root_cause="thiếu flex gap",
                               changed=["terminal_mcp/dashboard.py"],
                               test_runbook="test_ui_fast", test_passed=True,
                               deploy_url="http://preview.local/work")
    assert result["PLAN"] == "CONFIRMED"
    assert result["TEST"] == "PASS test_ui_fast"
    assert result["CHANGED"] == ["terminal_mcp/dashboard.py"]
    # Nothing here is a transcript, a rationale or a passing log.
    assert len(repr(result)) < 400


def test_a_result_without_a_deploy_says_none_rather_than_implying_one():
    result = bs.compact_result(plan_status=bs.PLAN_CONFIRMED, root_cause="x",
                               changed=[], test_runbook=None, test_passed=False)
    assert result["DEPLOY"] == "none"
    assert result["TEST"].startswith("FAIL")


# -- secrets -----------------------------------------------------------------

def test_a_spec_may_name_an_env_var_but_never_its_value(tmp_path):
    from terminal_mcp.project_knowledge import SecretInKnowledge
    store = bs.BugSpecStore(tmp_path / "specs.db")
    ok = _full(suspected_cause="TERMINAL_MCP_NODE_TOKEN is not set on that node")
    assert store.save(ok)
    with pytest.raises(SecretInKnowledge):
        store.save(_full(suspected_cause="token is ghp_" + "b" * 36))
    store.close()
