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


# -- the bridge into the execution queue (pipeline) ---------------------------

class _FakeQueue:
    """Stands in for QueueService: records enqueues, honours request_key."""

    def __init__(self):
        self.calls = []
        self._by_key = {}

    def enqueue(self, session, prompt, *, title=None, priority=0, metadata=None,
                request_key=None):
        if request_key and request_key in self._by_key:
            return {"status": "TASK_ACCEPTED", "task_id": self._by_key[request_key],
                    "session": session, "deduplicated": True}
        task_id = f"task_{len(self.calls)}"
        if request_key:
            self._by_key[request_key] = task_id
        self.calls.append({"session": session, "prompt": prompt, "title": title,
                           "priority": priority, "metadata": metadata or {},
                           "request_key": request_key})
        return {"status": "TASK_ACCEPTED", "task_id": task_id, "session": session,
                "deduplicated": False}


def test_a_ready_issue_becomes_a_real_queue_task(service):
    from terminal_mcp.work_inbox import promote_to_queue

    issue_id = service.capture("- do the thing\n")["issues"][0]["issue_id"]
    service.transition(issue_id, wi.READY)
    queue = _FakeQueue()
    result = promote_to_queue(service, issue_id, session="demo-work", queue=queue)
    assert result["status"] == "PROMOTED"
    # On a real lane, not the unassigned backlog where tasks sit forever.
    assert queue.calls[0]["session"] == "demo-work"
    assert service.store.get(issue_id).state == wi.EXECUTING
    assert service.store.get(issue_id).queue_task_id == result["task_id"]


def test_the_issue_id_is_the_request_key(service):
    from terminal_mcp.work_inbox import promote_to_queue

    issue_id = service.capture("- do the thing\n")["issues"][0]["issue_id"]
    service.transition(issue_id, wi.READY)
    queue = _FakeQueue()
    promote_to_queue(service, issue_id, session="demo-work", queue=queue)
    assert queue.calls[0]["request_key"] == f"issue:{issue_id}"


def test_promoting_twice_returns_the_same_task(service):
    from terminal_mcp.work_inbox import promote_to_queue

    issue_id = service.capture("- do the thing\n")["issues"][0]["issue_id"]
    service.transition(issue_id, wi.READY)
    queue = _FakeQueue()
    first = promote_to_queue(service, issue_id, session="demo-work", queue=queue)
    second = promote_to_queue(service, issue_id, session="demo-work", queue=queue)
    assert second["status"] == "ALREADY_PROMOTED"
    assert second["task_id"] == first["task_id"]
    assert len(queue.calls) == 1          # no second task was created


def test_a_finished_issue_is_not_promoted(service):
    from terminal_mcp.work_inbox import promote_to_queue

    issue_id = service.capture("- x\n")["issues"][0]["issue_id"]
    service.transition(issue_id, wi.DONE)
    queue = _FakeQueue()
    assert promote_to_queue(service, issue_id, session="demo-work",
                            queue=queue)["error"] == "ISSUE_TERMINAL"
    assert queue.calls == []


def test_a_failed_enqueue_leaves_the_issue_untouched(service):
    from terminal_mcp.work_inbox import promote_to_queue

    class _Refusing:
        def enqueue(self, *a, **k):
            return {"error": "LANE_NOT_A_WORK_SESSION"}

    issue_id = service.capture("- x\n")["issues"][0]["issue_id"]
    service.transition(issue_id, wi.READY)
    result = promote_to_queue(service, issue_id, session="m1", queue=_Refusing())
    assert result["error"] == "ENQUEUE_FAILED"
    # Not silently marked EXECUTING when nothing was queued.
    assert service.store.get(issue_id).state == wi.READY
    assert service.store.get(issue_id).queue_task_id is None


class _FakeQueueStore:
    def __init__(self, statuses):
        self.statuses = statuses

    def get_task(self, task_id):
        status = self.statuses.get(task_id)
        if status is None:
            return None
        return type("T", (), {"status": status})()


def test_issue_state_follows_the_queue(service):
    from terminal_mcp.work_inbox import promote_to_queue, sync_from_queue

    issue_id = service.capture("- x\n")["issues"][0]["issue_id"]
    service.transition(issue_id, wi.READY)
    queue = _FakeQueue()
    task_id = promote_to_queue(service, issue_id, session="demo-work",
                               queue=queue)["task_id"]
    moved = sync_from_queue(service, queue_store=_FakeQueueStore({task_id: "COMPLETED"}))
    assert moved and moved[0]["state"] == wi.DONE
    assert service.store.get(issue_id).state == wi.DONE


def test_a_failed_task_marks_the_issue_failed_not_lost(service):
    from terminal_mcp.work_inbox import promote_to_queue, sync_from_queue

    issue_id = service.capture("- x\n")["issues"][0]["issue_id"]
    service.transition(issue_id, wi.READY)
    queue = _FakeQueue()
    task_id = promote_to_queue(service, issue_id, session="demo-work",
                               queue=queue)["task_id"]
    sync_from_queue(service, queue_store=_FakeQueueStore({task_id: "FAILED"}))
    # Visible and retriable, never silently dropped.
    assert service.store.get(issue_id).state == wi.FAILED


def test_a_still_running_task_does_not_move_the_issue(service):
    from terminal_mcp.work_inbox import promote_to_queue, sync_from_queue

    issue_id = service.capture("- x\n")["issues"][0]["issue_id"]
    service.transition(issue_id, wi.READY)
    queue = _FakeQueue()
    task_id = promote_to_queue(service, issue_id, session="demo-work",
                               queue=queue)["task_id"]
    assert sync_from_queue(service, queue_store=_FakeQueueStore({task_id: "RUNNING"})) == []
    assert service.store.get(issue_id).state == wi.EXECUTING


# -- a claim is also a briefing (items 8/9) -----------------------------------
#
# `context_pack.retrieval_result` and `build_context_pack` were implemented
# and tested long before anything called them. Built-but-unwired is worse than
# absent: the capability reads as done while every planner still starts from
# an empty repository. These tests pin the wiring, not the retrieval logic --
# that has its own file.

@pytest.fixture()
def repo(tmp_path):
    import subprocess

    from terminal_mcp.project_knowledge import ProjectKnowledge

    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@e",
                        "-c", "user.name=t", *args], check=True, capture_output=True)

    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "steady.py").write_text("steady = 1\n")
    (root / "deleted.py").write_text("gone = 1\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    return ProjectKnowledge(root), head


@pytest.fixture()
def specs(tmp_path):
    from terminal_mcp.bug_spec import BugSpecStore

    store = BugSpecStore(tmp_path / "specs.db")
    yield store
    store.close()


def _briefed(tmp_path, specs, knowledge=None):
    store = InboxStore(tmp_path / "briefed-inbox.db")
    return InboxService(store, spec_store=specs, knowledge=knowledge)


def _prior_badge_bug(specs, *, files, commit=None):
    from terminal_mcp.bug_spec import plan_from_report

    spec = plan_from_report(
        title="Badge overlaps the session name",
        symptom="the work badge overlaps the session name on a narrow screen",
        module="work_ui", files=files, suspected_cause="missing flex gap",
        cause_confidence="HIGH", fix_strategy=["add a 6px gap"],
        acceptance=["no overlap"])
    spec.source_commit = commit
    return specs.save(spec)


def _triaged_badge_issue(service):
    issue_id = service.capture("- Badge overlaps the session name on mobile"
                               )["issues"][0]["issue_id"]
    # A module is what lifts a match from "worth reading" to "worth starting
    # from": without one, retrieval cannot score a past bug high enough to be
    # offered for reuse, and should not pretend otherwise.
    service.transition(issue_id, wi.TRIAGED, likely_module="work_ui")
    return issue_id


def test_claiming_an_issue_hands_over_the_retrieval_with_it(tmp_path, specs, repo):
    knowledge, head = repo
    prior = _prior_badge_bug(specs, files=["steady.py"], commit=head)
    service = _briefed(tmp_path, specs, knowledge)
    _triaged_badge_issue(service)

    claimed = service.claim_for_planning("planner-1")
    assert claimed["status"] == "CLAIMED"
    # Before a single file is opened, not after the investigation reports.
    assert claimed["retrieval"]["status"] == "REUSED_BUG_SPEC"
    assert claimed["retrieval"]["reused_bug_id"] == prior.bug_id


def test_a_reused_spec_arrives_with_its_paths_already_checked(tmp_path, specs, repo):
    knowledge, head = repo
    _prior_badge_bug(specs, files=["steady.py", "deleted.py"], commit=head)
    (knowledge.root / "deleted.py").unlink()
    service = _briefed(tmp_path, specs, knowledge)
    _triaged_badge_issue(service)

    check = service.claim_for_planning("planner-1")["retrieval"]["path_check"]
    assert check["missing"] == ["deleted.py"]
    assert check["verified"] is True


def test_the_context_pack_is_built_for_the_module_the_match_names(tmp_path, specs, repo):
    knowledge, head = repo
    knowledge.record_module("work_ui", paths=["steady.py"], summary="the work panel")
    _prior_badge_bug(specs, files=["steady.py"], commit=head)
    service = _briefed(tmp_path, specs, knowledge)
    _triaged_badge_issue(service)

    pack = service.claim_for_planning("planner-1")["context_pack"]
    assert pack["module"] == "work_ui"
    assert pack["summary"] == "the work panel"
    assert "MODULE work_ui" in pack["render"]


def test_the_verdict_outlives_the_planner_that_received_it(tmp_path, specs, repo):
    knowledge, head = repo
    _prior_badge_bug(specs, files=["steady.py"], commit=head)
    service = _briefed(tmp_path, specs, knowledge)
    issue_id = _triaged_badge_issue(service)
    service.claim_for_planning("planner-1")

    # A lease can expire; the next planner must see what the last one was told.
    assert service.store.get(issue_id).retrieval_status == "REUSED_BUG_SPEC"
    kinds = [event["kind"] for event in service.store.history(issue_id)]
    assert "retrieval" in kinds


def test_the_issue_row_keeps_a_summary_not_the_whole_briefing(tmp_path, specs, repo):
    knowledge, head = repo
    _prior_badge_bug(specs, files=["steady.py"], commit=head)
    service = _briefed(tmp_path, specs, knowledge)
    issue_id = _triaged_badge_issue(service)
    service.claim_for_planning("planner-1")

    record = service.store.get(issue_id).metadata["retrieval"]
    assert record["status"] == "REUSED_BUG_SPEC"
    assert record["path_check"]["checked"] == 1
    # The root causes and fix strategies travel to the planner, once. Storing
    # them here too would move the same paragraphs twice in a feature whose
    # whole purpose is to move fewer of them.
    assert set(record["matches"][0]) == {"bug_id", "score", "title"}


def test_a_match_is_not_written_back_as_the_issue_module(tmp_path, specs, repo):
    knowledge, head = repo
    _prior_badge_bug(specs, files=["steady.py"], commit=head)
    service = _briefed(tmp_path, specs, knowledge)
    issue_id = service.capture("- Badge overlaps the session name on mobile"
                               )["issues"][0]["issue_id"]
    service.claim_for_planning("planner-1")

    # A module inferred from a fuzzy match would score the NEXT retrieval
    # higher for no new evidence -- the system growing confident by talking
    # to itself.
    assert service.store.get(issue_id).likely_module is None


def test_an_inbox_with_no_history_wired_says_so_rather_than_staying_silent(service):
    service.capture("- Badge overlaps the session name on mobile")
    claimed = service.claim_for_planning("planner-1")
    assert claimed["status"] == "CLAIMED"
    # "Nobody searched" and "nothing was found" demand opposite next steps.
    assert claimed["retrieval"]["status"] == wi.RETRIEVAL_UNAVAILABLE


def test_a_failing_lookup_never_costs_the_claim(tmp_path):
    class _Exploding:
        def iter_recent(self, **_kwargs):
            raise RuntimeError("spec database is locked")

        def recent_for_module(self, *_args, **_kwargs):
            raise RuntimeError("spec database is locked")

    store = InboxStore(tmp_path / "inbox.db")
    service = InboxService(store, spec_store=_Exploding())
    service.capture("- Badge overlaps the session name on mobile")

    claimed = service.claim_for_planning("planner-1")
    assert claimed["status"] == "CLAIMED"
    assert claimed["retrieval"]["status"] == wi.RETRIEVAL_FAILED
    assert "spec database is locked" in claimed["retrieval"]["detail"]


def test_retrieval_does_not_persist_the_query_it_asked_with(tmp_path, specs, repo):
    knowledge, head = repo
    _prior_badge_bug(specs, files=["steady.py"], commit=head)
    service = _briefed(tmp_path, specs, knowledge)
    _triaged_badge_issue(service)
    service.claim_for_planning("planner-1")

    # The throwaway spec built to ASK must not join the history the next
    # question reads, or the inbox slowly answers itself.
    assert len(list(specs.iter_recent(limit=50))) == 1
