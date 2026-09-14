"""A `-work` session running an agent is BUSY, queue assignment or not.

The bug: workers() derived occupancy solely from the queue's current_task, so
a `-work` session running Claude that nobody had queued a task on reported
IDLE. Reproduced live on gatefix2-work -- pane command `claude`, session state
RUNNING, and the Work page showing it as free to dispatch into. Sending there
would have typed over a live conversation.

Eligibility and occupancy are different questions and are kept apart here:
"may the runtime use this session" is not "is something running in it".
"""

from __future__ import annotations

import pytest

from terminal_mcp.work_service import WorkService, _occupancy
from terminal_mcp.work_store import WorkStore


@pytest.fixture
def work(tmp_path):
    return WorkService(WorkStore(tmp_path / "work.db"))


def _row(name="demo-work", **overrides):
    row = {"name": name, "node_id": "local", "input_allowed": True}
    row.update(overrides)
    return row


def _ok_status(**overrides):
    """A status payload shaped like ONE real producer, never a mix.

    terminal_status reports `state`; terminal_input_context reports `status`.
    Seeding both and overriding one produced a payload no caller ever sends,
    and the contradiction showed up as a confusing assertion rather than a
    real defect.
    """
    status = {"exists": True}
    if "status" not in overrides:
        status["state"] = "IDLE"
    status.update(overrides)
    return status


class _Queue:
    """Minimal queue stand-in: one lane, one optional current task."""

    def __init__(self, current=None):
        self.current = current

    def status(self, session):
        return {"session": session, "current_task": self.current}


# -- the occupancy signal itself ----------------------------------------------

@pytest.mark.parametrize("command", ["claude", "codex", "claude.exe",
                                     "/usr/local/bin/claude", "python"])
def test_a_non_shell_command_counts_as_occupied(command):
    occupied, evidence = _occupancy({"current_command": command}, None)
    assert occupied is True
    assert evidence["agent_running"] is True


@pytest.mark.parametrize("command", ["bash", "sh", "zsh", "fish", "pwsh", "cmd.exe"])
def test_a_bare_shell_is_not_occupied(command):
    assert _occupancy({"current_command": command}, {"state": "IDLE"})[0] is False


def test_an_active_session_state_counts_even_under_a_shell():
    assert _occupancy({"current_command": "bash"}, {"state": "RUNNING"})[0] is True
    # A session holding a prompt open is mid-task; sending into it would land
    # on whatever it is asking.
    assert _occupancy({"current_command": "bash"}, {"state": "WAITING_INPUT"})[0] is True


def test_no_evidence_at_all_is_not_occupied():
    occupied, evidence = _occupancy({}, None)
    assert occupied is False
    assert evidence["current_command"] is None


def test_either_payload_shape_is_accepted():
    # terminal_status says `state`; terminal_input_context says `status`.
    assert _occupancy({}, {"current_command": "claude"})[0] is True
    assert _occupancy({}, {"status": "RUNNING"})[0] is True


# -- the regression: an agent running with no queue task ----------------------

def test_a_work_session_running_claude_is_not_idle(work):
    """The exact live bug, as a test."""
    result = work.workers(sessions=[_row("gatefix2-work")],
                          statuses={"gatefix2-work": _ok_status(
                              current_command="claude", status="RUNNING")})
    worker = result["workers"][0]
    assert worker["state"] == "RUNNING_MANUAL"
    assert worker["busy_untracked"] is True
    assert worker["occupancy"] == "manual"
    # Eligibility is a separate question and must not be clobbered.
    assert worker["eligible"] is True
    assert worker["current_task"] is None


def test_the_evidence_for_being_busy_is_reported(work):
    result = work.workers(sessions=[_row("gatefix2-work")],
                          statuses={"gatefix2-work": _ok_status(
                              current_command="claude", status="RUNNING")})
    evidence = result["workers"][0]["occupancy_evidence"]
    assert evidence["current_command"] == "claude"
    assert evidence["session_state"] == "RUNNING"


def test_an_idle_shell_worker_is_still_idle(work):
    result = work.workers(sessions=[_row("free-work")],
                          statuses={"free-work": _ok_status(current_command="bash")})
    worker = result["workers"][0]
    assert worker["state"] == "IDLE"
    assert worker["busy_untracked"] is False
    assert worker["occupancy"] == "free"


def test_idle_requires_eligibility_as_well_as_being_free(work):
    result = work.workers(
        sessions=[_row("denied-work", input_allowed=False,
                       input_denied_reason="input disabled")],
        statuses={"denied-work": _ok_status(current_command="bash")})
    # Free, but not usable -- that is not IDLE.
    assert result["workers"][0]["state"] != "IDLE"


# -- queue-assigned behaviour must not regress --------------------------------

def test_a_queue_assigned_worker_is_still_busy(work):
    work.queue = _Queue({"id": "t1", "title": "do it", "status": "RUNNING"})
    result = work.workers(sessions=[_row("busy-work")],
                          statuses={"busy-work": _ok_status(
                              current_command="claude", status="RUNNING")})
    worker = result["workers"][0]
    assert worker["state"] == "BUSY"
    assert worker["occupancy"] == "queue"
    # Busy because the QUEUE put work here, so it is not "untracked".
    assert worker["busy_untracked"] is False
    assert worker["current_task"]["task_id"] == "t1"


def test_a_finished_queue_task_does_not_keep_a_worker_busy(work):
    work.queue = _Queue({"id": "t1", "title": "done", "status": "COMPLETED"})
    result = work.workers(sessions=[_row("idle-work")],
                          statuses={"idle-work": _ok_status(current_command="bash")})
    assert result["workers"][0]["state"] == "IDLE"


def test_a_dead_session_is_offline_not_busy(work):
    result = work.workers(sessions=[_row("dead-work")],
                          statuses={"dead-work": {"exists": False,
                                                  "current_command": "claude"}})
    worker = result["workers"][0]
    assert worker["state"] == "OFFLINE"
    # A session that is gone cannot be "busy untracked".
    assert worker["busy_untracked"] is False


def test_ordinary_sessions_never_appear(work):
    result = work.workers(sessions=[_row("m1"), _row("terminal-mcp-main")],
                          statuses={})
    assert result["workers"] == []


def test_busy_workers_sort_before_manual_before_idle(work):
    work.queue = _Queue({"id": "t1", "title": "x", "status": "RUNNING"})
    rows = [_row("c-idle-work"), _row("a-manual-work"), _row("b-queue-work")]
    statuses = {"c-idle-work": _ok_status(current_command="bash"),
                "a-manual-work": _ok_status(current_command="claude"),
                "b-queue-work": _ok_status(current_command="claude")}
    # One shared queue stand-in marks every lane busy, so narrow it to one.
    work.queue = type("Q", (), {"status": lambda self, s: {
        "current_task": {"id": "t1", "title": "x", "status": "RUNNING"}
        if s == "b-queue-work" else None}})()
    states = [w["state"] for w in work.workers(sessions=rows, statuses=statuses)["workers"]]
    assert states == ["BUSY", "RUNNING_MANUAL", "IDLE"]
