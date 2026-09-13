"""Rapid Capture Inbox, the issue state machine, and the planner pool."""

from __future__ import annotations

import pytest

from terminal_mcp import work_inbox as wi
from terminal_mcp.work_inbox import InboxService, InboxStore


@pytest.fixture
def service(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    yield InboxService(store, planner_concurrency=3, claim_lease_seconds=900)
    store.close()


BATCH = """- Badge WORK chồng lên tên session trên mobile
- Nút Refresh không phản hồi khi click
- Migration bảng work_runs bị lỗi khi rollback
- Session mất sau khi re-login, used to work before v2
- Trang Fleet load chậm hơn 10 giây
"""


# -- capture (item 12) --------------------------------------------------------

def test_a_batch_splits_into_one_issue_each(service):
    result = service.capture(BATCH, project="terminal-mcp")
    assert result["captured"] == 5
    assert len({entry["issue_id"] for entry in result["issues"]}) == 5
    assert all(entry["issue_id"].startswith("iss_") for entry in result["issues"])


def test_capture_does_no_deep_analysis(service):
    # Every captured issue starts at NEW: capture records, planning analyses.
    result = service.capture(BATCH)
    states = {entry["state"] for entry in result["issues"]}
    assert states <= {wi.NEW, wi.DUPLICATE}


def test_a_multi_line_item_stays_one_issue(service):
    text = ("- Nút Refresh không phản hồi khi click\n"
            "  chỉ xảy ra sau khi reload trang\n"
            "  và chỉ trên Safari\n"
            "- Badge lệch trên mobile\n")
    assert service.capture(text)["captured"] == 2      # not 5, one per line


def test_paragraphs_split_when_there_are_no_bullets(service):
    text = "Badge lệch trên mobile.\n\nQueue không dispatch task nào.\n"
    assert service.capture(text)["captured"] == 2


def test_empty_capture_is_refused_not_silently_accepted(service):
    assert service.capture("   ")["error"] == "NOTHING_TO_CAPTURE"


def test_capture_assigns_a_rough_type(service):
    issues = service.capture(BATCH)["issues"]
    by_title = {entry["title"]: entry["type"] for entry in issues}
    assert by_title["Badge WORK chồng lên tên session trên mobile"] == "ui"
    assert by_title["Migration bảng work_runs bị lỗi khi rollback"] == "data"


def test_a_duplicate_is_flagged_and_linked_never_dropped(service):
    first = service.capture("- Badge WORK chồng lên tên session trên mobile\n")
    second = service.capture("- Badge WORK chồng lên tên session ở mobile\n")
    entry = second["issues"][0]
    assert entry["state"] == wi.DUPLICATE
    assert entry["duplicate_of"] == first["issues"][0]["issue_id"]
    # Still persisted -- a second report is a fact, not noise.
    assert service.store.get(entry["issue_id"]) is not None


def test_distinct_issues_are_not_treated_as_duplicates(service):
    result = service.capture(BATCH)
    assert result["duplicates"] == 0


# -- state machine (item 14) --------------------------------------------------

def test_states_are_durable_across_a_new_store(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    service = InboxService(store)
    issue_id = service.capture("- Badge lệch\n")["issues"][0]["issue_id"]
    service.transition(issue_id, wi.TRIAGED, actor="planner")
    store.close()

    reopened = InboxStore(tmp_path / "inbox.db")
    assert reopened.get(issue_id).state == wi.TRIAGED
    assert [event["to_state"] for event in reopened.history(issue_id)] == [wi.NEW, wi.TRIAGED]
    reopened.close()


def test_every_transition_is_recorded(service):
    issue_id = service.capture("- Badge lệch\n")["issues"][0]["issue_id"]
    for state in (wi.TRIAGED, wi.READY, wi.EXECUTING, wi.DONE):
        service.transition(issue_id, state, actor="test")
    history = [event["to_state"] for event in service.store.history(issue_id)]
    assert history == [wi.NEW, wi.TRIAGED, wi.READY, wi.EXECUTING, wi.DONE]


def test_an_unknown_state_is_refused(service):
    issue_id = service.capture("- x\n")["issues"][0]["issue_id"]
    assert service.transition(issue_id, "MADE_UP")["error"] == "UNKNOWN_STATE"


def test_a_finished_issue_does_not_silently_reopen(service):
    issue_id = service.capture("- x\n")["issues"][0]["issue_id"]
    service.transition(issue_id, wi.DONE)
    assert service.transition(issue_id, wi.EXECUTING)["error"] == "ISSUE_TERMINAL"
    # REWORK is the one legitimate way back, and it is explicit.
    assert service.transition(issue_id, wi.REWORK)["state"] == wi.REWORK


# -- planner pool (item 13) ---------------------------------------------------

def test_bounded_concurrency_is_honoured(service):
    service.capture(BATCH)
    claims = [service.claim_for_planning(f"planner-{i}") for i in range(4)]
    assert [c["status"] for c in claims[:3]] == ["CLAIMED"] * 3
    assert claims[3]["status"] == "AT_CAPACITY"
    assert claims[3]["limit"] == 3


def test_two_planners_never_claim_the_same_issue(service):
    service.capture(BATCH)
    claimed = [service.claim_for_planning(f"planner-{i}")["issue"]["issue_id"]
               for i in range(3)]
    assert len(set(claimed)) == 3


def test_an_empty_inbox_says_so(service):
    assert service.claim_for_planning("planner-1")["status"] == "NOTHING_TO_CLAIM"


def test_priority_first_then_oldest(service):
    service.capture("- low priority thing\n", priority=0)
    service.capture("- urgent thing\n", priority=10)
    claimed = service.claim_for_planning("planner-1")["issue"]
    assert claimed["short_title"] == "urgent thing"


def test_a_stale_claim_is_reclaimed_after_restart(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    service = InboxService(store, planner_concurrency=1, claim_lease_seconds=-1)
    service.capture("- something\n")
    first = service.claim_for_planning("planner-crashed")
    assert first["status"] == "CLAIMED"

    # A planner that died still holds the slot until its lease lapses.
    revived = InboxService(InboxStore(tmp_path / "inbox.db"), planner_concurrency=1)
    reclaimed = revived.reclaim_stale()
    assert first["issue"]["issue_id"] in reclaimed
    assert revived.claim_for_planning("planner-new")["status"] == "CLAIMED"
    store.close()


def test_a_live_claim_is_not_stolen(service):
    service.capture("- something\n")
    service.claim_for_planning("planner-1")
    assert service.reclaim_stale() == []


def test_a_per_project_cap_stops_one_project_starving_another(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    service = InboxService(store, planner_concurrency=3, project_concurrency={"noisy": 1})
    service.capture("- a\n- b\n- c\n", project="noisy")
    service.capture("- quiet project issue\n", project="quiet")
    first = service.claim_for_planning("planner-1")
    second = service.claim_for_planning("planner-2")
    assert first["issue"]["project"] == "noisy"
    # The noisy project has used its cap, so the next slot goes elsewhere.
    assert second["issue"]["project"] == "quiet"
    store.close()


def test_claiming_skips_issues_waiting_on_a_human(service):
    issue_id = service.capture("- something\n")["issues"][0]["issue_id"]
    service.request_user_hint(issue_id, ["Which module owns this?"])
    # Re-planning it would discard the analysis that produced the question.
    assert service.claim_for_planning("planner-1")["status"] == "NOTHING_TO_CLAIM"


# -- developer assist (items 6/7) ---------------------------------------------

def test_user_hint_requires_a_real_question(service):
    issue_id = service.capture("- x\n")["issues"][0]["issue_id"]
    assert service.request_user_hint(issue_id, ["  "])["error"] == "QUESTIONS_REQUIRED"


def test_questions_are_capped_at_three(service):
    issue_id = service.capture("- x\n")["issues"][0]["issue_id"]
    result = service.request_user_hint(issue_id, [f"Question {i}?" for i in range(6)])
    assert len(result["questions"]) == 3


def test_prior_analysis_survives_the_hint_round_trip(service):
    issue_id = service.capture("- session mất sau re-login\n")["issues"][0]["issue_id"]
    service.request_user_hint(issue_id, ["Before or after auth completes?"],
                              findings=["grant rows unchanged"])
    answer = service.attach_human_hint(issue_id, "chỉ xảy ra qua tunnel")
    issue = service.store.get(issue_id)
    assert answer["state"] == wi.TRIAGED            # the SAME issue resumes
    assert issue.human_hints == ("chỉ xảy ra qua tunnel",)
    assert issue.metadata["findings_before_hint"] == ["grant rows unchanged"]
    assert issue.questions == ("Before or after auth completes?",)
    assert "verify it against the current code" in answer["note"]


def test_a_hint_carrying_a_secret_is_refused(service):
    from terminal_mcp.project_knowledge import SecretInKnowledge

    issue_id = service.capture("- x\n")["issues"][0]["issue_id"]
    with pytest.raises(SecretInKnowledge):
        service.attach_human_hint(issue_id, "the token is ghp_" + "a" * 36)


def test_an_answered_issue_can_be_claimed_again(service):
    issue_id = service.capture("- x\n")["issues"][0]["issue_id"]
    service.request_user_hint(issue_id, ["Which module?"])
    service.attach_human_hint(issue_id, "work_ui")
    assert service.claim_for_planning("planner-1")["status"] == "CLAIMED"


# -- reporting ----------------------------------------------------------------

def test_summary_reports_real_counts(service):
    service.capture(BATCH)
    service.claim_for_planning("planner-1")
    summary = service.summary()
    assert summary["total"] == 5
    assert summary["counts"][wi.PLANNING] == 1
    assert summary["planning_active"] == 1
    assert summary["claimed_by"] == ["planner-1"]
