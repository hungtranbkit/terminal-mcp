"""The classification rules, and the failures they exist to prevent.

Most of these are one sentence each: "this input must not be called IDLE."
That is the whole point -- the report is only worth opening if `IDLE` means
demonstrably free rather than nothing-was-observed.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from terminal_mcp import system_report as sr

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)


def _ts(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def _row(**kw):
    base = {"name": "demo", "node_id": "local", "last_activity_at": _ts(10)}
    base.update(kw)
    return base


def _classify(row=None, **kw):
    return sr.classify_session(row or _row(), now=NOW, **kw)


# -- detached is not idle ----------------------------------------------------

def test_a_detached_session_running_an_agent_is_not_idle():
    """Acceptance 1. Detachment is a property of the terminal, not the work."""
    view = _classify(_row(current_command="claude", attached=False))
    assert view.state == sr.RUNNING_MANUAL
    assert view.state != sr.IDLE
    assert view.evidence["agent_running"] is True


def test_an_old_but_occupied_session_is_still_occupied():
    """Staleness must not outrank a running agent -- an agent thinking for an
    hour produces no output and is the busiest thing on the machine."""
    view = _classify(_row(current_command="claude", last_activity_at=_ts(7200)))
    assert view.state == sr.RUNNING_MANUAL


def test_a_plain_shell_with_fresh_activity_is_idle():
    view = _classify(_row(current_command="bash"))
    assert view.state == sr.IDLE


@pytest.mark.parametrize("shell", ["bash", "zsh", "-bash", "/usr/bin/fish", "cmd.exe", "pwsh"])
def test_shells_do_not_count_as_occupancy(shell):
    assert _classify(_row(current_command=shell)).state == sr.IDLE


# -- recent output alone proves nothing --------------------------------------

def test_recent_output_does_not_imply_busy_when_the_task_is_done():
    """Acceptance 2. The lane has nothing in flight and the pane is a shell;
    fresh output is just the tail of finished work."""
    view = _classify(_row(current_command="bash", last_activity_at=_ts(1)),
                     lane_task=None)
    assert view.state == sr.IDLE
    assert view.task_id is None


# -- ambiguity is UNKNOWN, never IDLE ---------------------------------------

def test_no_command_and_no_state_is_unknown_not_idle():
    """Acceptance 3. This is the whole feature in one assertion."""
    view = _classify(_row(current_command=None), status={})
    assert view.state == sr.UNKNOWN
    assert "absence of evidence" in view.reason


def test_stale_evidence_is_stale_not_idle():
    view = _classify(_row(current_command="bash", last_activity_at=_ts(3600)))
    assert view.state == sr.STALE
    assert view.stale is True
    assert "older than" in view.reason


def test_unparseable_timestamp_does_not_read_as_just_seen():
    view = _classify(_row(current_command="bash", last_activity_at="not-a-date"))
    assert view.last_activity_age_seconds is None
    assert view.state == sr.IDLE, "unknown age must not be treated as stale either"


# -- the node outranks everything local -------------------------------------

def test_an_offline_node_makes_its_sessions_offline():
    view = _classify(_row(current_command="claude"), node={"id": "hp", "status": "offline"})
    assert view.state == sr.OFFLINE
    assert "offline" in view.reason


def test_a_dead_pane_is_offline():
    assert _classify(_row(), status={"exists": False}).state == sr.OFFLINE


# -- the queue's claim -------------------------------------------------------

def test_a_queue_task_in_flight_is_busy():
    view = _classify(lane_task={"id": "t1", "title": "build", "status": "RUNNING"})
    assert view.state == sr.BUSY
    assert (view.task_id, view.task_title) == ("t1", "build")
    assert view.occupancy == "queue"


@pytest.mark.parametrize("status", ["BLOCKED", "FAILED", "PAUSED"])
def test_a_held_task_is_blocked_not_busy(status):
    view = _classify(lane_task={"id": "t1", "title": "x", "status": status})
    assert view.state == sr.BLOCKED


def test_blocked_counts_as_occupied_capacity():
    """The lane is held; suggesting work onto it would be wrong."""
    assert sr.BLOCKED in sr.OCCUPIED_STATES


# -- progress is evidence-backed or null ------------------------------------

def test_progress_is_null_without_evidence():
    """Acceptance 4."""
    assert sr.progress_from_weights(None, None) == (None, sr.PROGRESS_NONE)
    assert sr.progress_from_weights(0, 0) == (None, sr.PROGRESS_NONE)
    assert sr.progress_from_requirements(0, 0) == (None, sr.PROGRESS_NONE)
    assert sr.progress_from_queue_counts({}) == (None, sr.PROGRESS_NONE)


def test_progress_is_deterministic_when_weights_support_it():
    assert sr.progress_from_weights(4, 1) == (25.0, sr.PROGRESS_TASK_WEIGHTS)
    assert sr.progress_from_weights(3, 3) == (100.0, sr.PROGRESS_TASK_WEIGHTS)


def test_progress_from_requirements_is_countable():
    assert sr.progress_from_requirements(2, 4) == (50.0, sr.PROGRESS_REQUIREMENTS)


def test_progress_never_exceeds_one_hundred():
    assert sr.progress_from_weights(2, 9)[0] == 100.0
    assert sr.progress_from_requirements(9, 2)[0] == 100.0


def test_progress_basis_always_names_its_source():
    percent, basis = sr.progress_from_queue_counts({"COMPLETED": 1, "RUNNING": 1})
    assert (percent, basis) == (50.0, sr.PROGRESS_QUEUE_TASKS)


# -- node rollup and the denominator ----------------------------------------

def _views(*states):
    return [sr.SessionView(f"s{i}", "n", state) for i, state in enumerate(states)]


def test_utilization_denominator_excludes_what_cannot_be_dispatched_to():
    """Acceptance 5. OFFLINE/UNAVAILABLE/STALE/UNKNOWN are not idle capacity."""
    node = sr.roll_up_node("n", _views(sr.BUSY, sr.IDLE, sr.OFFLINE, sr.UNKNOWN, sr.STALE))
    assert node.utilization_denominator == 2
    assert node.utilization_percent == 50.0
    assert node.total_visible_sessions == 5


def test_running_manual_counts_as_busy_for_utilization():
    node = sr.roll_up_node("n", _views(sr.RUNNING_MANUAL, sr.IDLE))
    assert node.utilization_percent == 50.0


def test_utilization_is_null_when_there_is_no_eligible_capacity():
    node = sr.roll_up_node("n", _views(sr.OFFLINE, sr.OFFLINE))
    assert node.utilization_percent is None
    assert node.utilization_denominator == 0


def test_the_denominator_definition_travels_with_the_number():
    assert "OFFLINE" in sr.roll_up_node("n", _views(sr.IDLE)).to_dict()["denominator_definition"]


# -- optimization rules ------------------------------------------------------

def test_backlog_plus_idle_capacity_is_flagged():
    """Acceptance 6."""
    nodes = [sr.roll_up_node("dell", _views(sr.IDLE, sr.IDLE))]
    kinds = [f.kind for f in sr.suggest(nodes, ready_backlog=3)]
    assert sr.UNDERUTILIZED_WITH_BACKLOG in kinds


def test_idle_capacity_with_no_backlog_is_not_a_fault():
    nodes = [sr.roll_up_node("dell", _views(sr.IDLE))]
    findings = {f.kind: f for f in sr.suggest(nodes, ready_backlog=0)}
    assert sr.UNDERUTILIZED_WITH_BACKLOG not in findings
    assert "not a scheduling fault" in findings[sr.IDLE_NODE_WITH_NO_BACKLOG].detail


def test_backlog_with_no_capacity_says_so():
    nodes = [sr.roll_up_node("win", _views(sr.BUSY, sr.BUSY))]
    kinds = [f.kind for f in sr.suggest(nodes, ready_backlog=5)]
    assert sr.ALL_CAPACITY_BUSY in kinds


def test_cross_node_imbalance_is_flagged():
    """Acceptance 7."""
    hot = sr.roll_up_node("win", _views(sr.BUSY, sr.BUSY, sr.BUSY, sr.BUSY))
    quiet = sr.roll_up_node("hp", _views(sr.IDLE, sr.IDLE))
    findings = [f for f in sr.suggest([hot, quiet], ready_backlog=2,
                                      backlog_titles=["backend api refactor"])
                if f.kind == sr.LOAD_IMBALANCE]
    assert findings
    assert "win" in findings[0].nodes and "hp" in findings[0].nodes


# -- OS compatibility --------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Fix WPF desktop viewer overlap", "registry key for installer",
    "Windows service restart", "build the MSI", "PowerShell bootstrap",
])
def test_windows_only_work_is_recognised(title):
    """Acceptance 8."""
    assert sr.task_requires_windows(title) is True


def test_a_windows_only_task_is_not_suggested_for_a_linux_node():
    hot = sr.roll_up_node("win", _views(*([sr.BUSY] * 4)), node={"platform": "windows"})
    quiet = sr.roll_up_node("hp", _views(sr.IDLE, sr.IDLE), node={"platform": "linux"})
    findings = [f for f in sr.suggest([hot, quiet], ready_backlog=1,
                                      backlog_titles=["Fix WPF installer registry key"])
                if f.kind == sr.LOAD_IMBALANCE]
    assert not findings, "a WPF/registry/installer task must never be proposed for Linux"


def test_a_generic_backend_task_is_a_candidate_for_linux():
    """Acceptance 9."""
    hot = sr.roll_up_node("win", _views(*([sr.BUSY] * 4)), node={"platform": "windows"})
    quiet = sr.roll_up_node("hp", _views(sr.IDLE, sr.IDLE), node={"platform": "linux"})
    findings = [f for f in sr.suggest([hot, quiet], ready_backlog=1,
                                      backlog_titles=["backend api tests"])
                if f.kind == sr.LOAD_IMBALANCE]
    assert findings
    assert "candidate task(s)" in findings[0].detail


def test_an_unclassifiable_task_is_marked_low_confidence_not_definitive():
    hot = sr.roll_up_node("win", _views(*([sr.BUSY] * 4)), node={"platform": "windows"})
    quiet = sr.roll_up_node("hp", _views(sr.IDLE, sr.IDLE), node={"platform": "linux"})
    findings = [f for f in sr.suggest([hot, quiet], ready_backlog=1,
                                      backlog_titles=["do the thing"])
                if f.kind == sr.LOAD_IMBALANCE]
    assert findings and findings[0].confidence == "low"
    assert "candidate only" in findings[0].detail


def test_suggestions_never_describe_an_action_taken():
    """V1 suggests; it must never read as though something was moved."""
    nodes = [sr.roll_up_node("dell", _views(sr.IDLE))]
    for finding in sr.suggest(nodes, ready_backlog=1):
        assert "moved" not in finding.detail.lower()
        assert "reassigned" not in finding.detail.lower()


# -- serialisation -----------------------------------------------------------

def test_views_are_json_serialisable():
    import json
    json.dumps(_classify().to_dict())
    json.dumps(sr.roll_up_node("n", _views(sr.IDLE)).to_dict())
    json.dumps([f.to_dict() for f in sr.suggest([sr.roll_up_node("n", _views(sr.IDLE))],
                                                ready_backlog=1)])


def test_no_raw_prompt_or_output_is_carried():
    """Acceptance 19. Titles and status only -- never pane content."""
    view = _classify(lane_task={"id": "t", "title": "x", "status": "RUNNING",
                                "prompt": "SECRET PROMPT TEXT"})
    payload = view.to_dict()
    assert "prompt" not in payload
    assert "SECRET" not in json.dumps(payload)
    assert payload["task_title"] == "x"
