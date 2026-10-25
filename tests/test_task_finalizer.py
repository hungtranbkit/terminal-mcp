"""Task finalizer: stale RUNNING/BLOCKED/FAILED rows settle on evidence.

Shapes come from the live HP direct_tasks.db (2026-10-26): FAILED by
SESSION_RUNTIME_MISSING / NODE_UNREACHABLE / STATUS_PROBE_TIMEOUT, FAILED by
CONTINUATION_LIMIT_REACHED, explicit BLOCKED reasons, and DONE from a marker.
"""
from types import SimpleNamespace

import pytest

from terminal_mcp.direct_task import DirectTaskStore
from terminal_mcp.lease import ResourceLockStore
from terminal_mcp.task_finalizer import (BLOCKED, DONE_VERIFIED, FAILED, QA_PENDING, RUNNING,
                                         WAITING_INPUT, FinalizerStore, TaskFinalizer, decide)
from tests.test_compact_tools import service

TID = "dt_0123456789abcdef"


def task(**fields):
    base = {"task_id": TID, "target": "dell-linux/codex-a", "state": "RUNNING", "reason": None,
            "completed_at": None, "continuations": 0, "max_continuations": 6,
            "receipt": "EXECUTION_ACCEPTED", "accepted_at": 1.0}
    return {**base, **fields}


IDLE = {"state": "IDLE", "exists": True, "output": "› Ask Codex to do anything"}
DONE_TAIL = {**IDLE, "output": f"• All tests pass.\nTMCP-DONE: {TID}\n› Ask Codex to do anything"}
QA_PASS = {"verdict": "PASS", "reviewer": "qa-bot", "evidence": ["https://github.com/o/r/pull/9#ci", "abc1234"]}


# -- decide(): pure verdicts ---------------------------------------------------

def test_idle_composer_is_never_done():
    spent = task(state="FAILED", completed_at=5.0, continuations=6,
                 reason="CONTINUATION_LIMIT_REACHED: 6 continuations without an explicit DONE/BLOCKED")
    verdict = decide(spent, IDLE)
    assert verdict["state"] == FAILED and verdict["reason"].startswith("IDLE_WITHOUT_COMPLETION_MARKER")
    assert decide(task(), IDLE)["state"] == RUNNING  # budget left: the supervisor continues


def test_done_marker_without_qa_is_only_qa_pending():
    assert decide(task(), DONE_TAIL)["state"] == QA_PENDING
    assert decide(task(state="DONE", completed_at=5.0), IDLE)["state"] == QA_PENDING


@pytest.mark.parametrize("qa", [
    {"verdict": "PASS", "reviewer": "qa-bot", "evidence": []},
    {"verdict": "PASS", "reviewer": "", "evidence": ["x"]},
    {"verdict": "PASS", "reviewer": "qa-bot", "evidence": ["  "]},
])
def test_qa_without_reviewer_or_evidence_does_not_verify(qa):
    assert decide(task(), DONE_TAIL, qa=qa)["state"] == QA_PENDING


def test_done_with_qa_pass_and_evidence_is_verified():
    verdict = decide(task(), DONE_TAIL, qa=QA_PASS)
    assert verdict["state"] == DONE_VERIFIED and verdict["evidence"] == QA_PASS["evidence"]
    assert decide(task(), DONE_TAIL, qa={**QA_PASS, "verdict": "FAIL"})["state"] == FAILED


def test_late_marker_reconciles_an_observation_failure():
    # FAILED by "target not observable", but the agent did print the marker.
    lost = task(state="FAILED", completed_at=5.0,
                reason="target not observable (node): NODE_UNREACHABLE: ")
    assert decide(lost, DONE_TAIL)["state"] == QA_PENDING


def test_blocked_marker_input_prompt_and_usage_limit():
    blocked_tail = {**IDLE, "output": f"TMCP-BLOCKED: {TID} owner must add the CNAME"}
    assert decide(task(), blocked_tail)["state"] == BLOCKED
    prompt = {"kind": "CODEX_SANDBOX_APPROVAL", "detail": "x", "next_action": "owner approves"}
    verdict = decide(task(), {**IDLE, "state": "WAITING_INPUT", "input_required": True,
                              "input_prompt": prompt})
    assert (verdict["state"], verdict["reason"], verdict["next_action"]) == (
        WAITING_INPUT, "CODEX_SANDBOX_APPROVAL", "owner approves")
    assert decide(task(), {**IDLE, "blocked_reason": "AGENT_USAGE_LIMIT"})["state"] == BLOCKED


@pytest.mark.parametrize("error", ["SESSION_RUNTIME_MISSING", "SESSION_NOT_FOUND"])
def test_gone_session_without_marker_fails_at_once(error):
    verdict = decide(task(state="FAILED", completed_at=5.0), {"error": error})
    assert verdict["state"] == FAILED and verdict["reason"].startswith("SESSION_GONE")


def test_unobservable_is_retried_then_failed_at_the_limit():
    first = decide(task(), {"error": "STATUS_PROBE_TIMEOUT"}, attempts=0, max_attempts=3)
    assert first["state"] == RUNNING and first["transient"]
    last = decide(task(), {"error": "STATUS_PROBE_TIMEOUT"}, attempts=2, max_attempts=3)
    assert last["state"] == FAILED and last["reason"].startswith("UNOBSERVABLE")


def test_obsolete_only_with_a_review():
    verdict = decide(task(state="BLOCKED", completed_at=5.0), IDLE,
                     review={"verdict": "OBSOLETE", "reviewer": "owner", "reason": "superseded by #95"})
    assert verdict["state"] == FAILED and verdict["obsolete"]


# -- TaskFinalizer: durable, bounded, side effects -----------------------------

class FakeGitHub:
    def __init__(self, state="open"):
        self.current, self.closed = state, []

    def state(self, repo, number):
        return self.current

    def close(self, repo, number, *, reason, comment):
        self.closed.append((repo, number, reason, comment))
        self.current = "closed"
        return True


@pytest.fixture
def rig(tmp_path):
    tasks = DirectTaskStore(tmp_path / "direct.db")
    tasks.insert({**task(), "mode": "agent", "steps": [], "created_at": 1.0, "updated_at": 1.0,
                  "max_continuations": 6})
    seen = {"status": dict(IDLE), "tail": IDLE["output"]}
    clock = [1000.0]
    locks = ResourceLockStore(tmp_path / "locks.db")
    github = FakeGitHub()
    finalizer = TaskFinalizer(
        tasks, FinalizerStore(tmp_path / "final.db"),
        status=lambda target: dict(seen["status"]),
        tail=lambda target, lines: {"output": seen["tail"]},
        locks=locks, github=github, clock=lambda: clock[0], max_attempts=3)
    return SimpleNamespace(tasks=tasks, finalizer=finalizer, seen=seen, clock=clock,
                           locks=locks, github=github)


def test_qa_pass_verifies_releases_leases_and_closes_issue_only_when_applied(rig):
    rig.locks.acquire("proj", "branch:fix", TID, reason="task branch")
    rig.seen["tail"] = DONE_TAIL["output"]
    assert rig.finalizer.link_issue(TID, repo="hungtranbkit/terminal-mcp", number=93)["status"] == "OK"
    pending = rig.finalizer.reconcile(TID)
    assert pending["state"] == QA_PENDING and pending["issue_action"] == "none"
    assert pending["leases_released_count"] == 1
    assert rig.locks.holder("proj", "branch:fix") is None
    assert rig.finalizer.record_qa(TID, verdict="PASS", reviewer="qa", evidence=[])["error"] == \
        "QA_EVIDENCE_REQUIRED"
    dry = rig.finalizer.record_qa(TID, verdict="PASS", reviewer="qa", evidence=["sha 6efafde", "pytest 707 passed"])
    assert dry["state"] == DONE_VERIFIED
    assert (dry["issue_action"], dry["applied"]) == ("close_completed", False)
    assert rig.github.closed == []
    applied = rig.finalizer.reconcile(TID, apply_github=True)
    assert applied["applied"] is True
    assert rig.github.closed[0][2] == "completed" and "6efafde" in rig.github.closed[0][3]
    # Settled: a repeat never closes twice.
    assert rig.finalizer.reconcile(TID, apply_github=True)["issue_closed_as"] == "completed"
    assert len(rig.github.closed) == 1


def test_issue_stays_open_without_qa_or_review(rig):
    rig.finalizer.link_issue(TID, repo="o/r", number=1)
    rig.seen["status"] = {**IDLE, "blocked_reason": "AGENT_USAGE_LIMIT"}
    result = rig.finalizer.reconcile(TID, apply_github=True)
    assert result["state"] == BLOCKED and result["issue_action"] == "none"
    assert rig.github.closed == []


def test_obsolete_review_closes_not_planned(rig):
    rig.finalizer.link_issue(TID, repo="o/r", number=2)
    assert rig.finalizer.record_review(TID, verdict="OBSOLETE", reviewer="", reason="x")["error"] == \
        "INVALID_REVIEW"
    rig.finalizer.record_review(TID, verdict="OBSOLETE", reviewer="owner", reason="superseded by PR #95")
    result = rig.finalizer.reconcile(TID, apply_github=True)
    assert result["state"] == FAILED and rig.github.closed[0][2] == "not_planned"


def test_already_closed_issue_is_not_touched(rig):
    rig.github.current = "closed"
    rig.seen["tail"] = DONE_TAIL["output"]
    rig.finalizer.link_issue(TID, repo="o/r", number=3)
    rig.finalizer.record_qa(TID, verdict="PASS", reviewer="qa", evidence=["log"])
    assert rig.finalizer.reconcile(TID, apply_github=True)["issue_closed_as"] == "already_closed"
    assert rig.github.closed == []


def test_retries_back_off_and_stop_at_the_limit(rig):
    rig.seen["status"] = {"error": "NODE_UNREACHABLE"}
    first = rig.finalizer.reconcile(TID)
    assert first["attempts"] == 1 and first["next_attempt_at"] == 1060.0
    assert rig.finalizer.sweep()["reconciled"] == 0  # not due yet
    rig.clock[0] = 1061.0
    assert rig.finalizer.reconcile(TID)["attempts"] == 2
    rig.clock[0] = 5000.0
    final = rig.finalizer.reconcile(TID)
    assert final["state"] == FAILED and final["reason"].startswith("UNOBSERVABLE")
    assert rig.finalizer.reconcile(TID)["already_final"] is True
    assert rig.finalizer.sweep()["reconciled"] == 0  # settled rows are never re-observed


def test_waiting_input_keeps_leases(rig):
    rig.locks.acquire("proj", "file:a.py", TID)
    rig.seen["status"] = {**IDLE, "state": "WAITING_INPUT", "input_required": True}
    assert rig.finalizer.reconcile(TID)["state"] == WAITING_INPUT
    assert rig.locks.holder("proj", "file:a.py") is not None


# -- terminal_turn wiring ------------------------------------------------------

def test_supervise_finalize_turn_action(rig, tmp_path):
    tools, _, _ = service(tmp_path / "journal.db")
    assert tools.turn(action="supervise_finalize", task_id=TID)["error"] == "ACTION_UNAVAILABLE"
    tools.finalizer = rig.finalizer
    result = tools.turn(action="supervise_finalize", task_id=TID)
    assert result["action"] == "supervise_finalize" and result["state"] == RUNNING
    assert tools.turn(action="supervise_finalize", args={"op": "nope"}, task_id=TID)["error"] == "INVALID_OP"
    assert tools.turn(action="supervise_finalize", args={"op": "sweep", "limit": 0})["error"] == "INVALID_LIMIT"
    swept = tools.turn(action="supervise_finalize", args={"op": "sweep"})
    assert swept["status"] == "OK"
