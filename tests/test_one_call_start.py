"""TMCP-CALLED-TOOL-SPAM-002: one client call per user request.

The failure this pins: `QueueEngine.tick()` makes at most ONE state
transition per call, and before `action="start"` the only things that ever
called it were the client (one MCP call per transition -- the "Called tool"
wall) or `QueueLoop`, which sits behind two deliberately OFF-by-default
gates. So a normal "give this session work" request cost the client an
enqueue call plus one call per transition plus a polling loop to watch it.

What these tests actually prove, in the words of the task:

  1. ONE client call starts long work -- `turn(action="start", ...)` returns
     a durable task_id with the task already dispatched, having spent zero
     further client calls.
  2. SERVER-SIDE state advances without more client calls -- the follower
     carries the task to completion on its own, and the client call count
     stays at exactly one the whole time.
  3. The client is told not to poll, in the receipt itself.
  4. Existing actions are unchanged.

Everything is driven through fakes rather than a real tmux session: what is
under test is the call-count contract and the state machine around it, not
the pane layer (tests/test_send_reliability.py owns that).
"""
from __future__ import annotations

import threading
import time

import pytest

from terminal_mcp.compact_tools import (MAX_START_TICKS, TURN_ACTION_ALIASES,
                                        TURN_ACTIONS, CompactTerminalTools)
from terminal_mcp.queue_task_follower import StartedTaskFollower


class FakeLane:
    """A minimal stand-in for one queue lane's durable state machine.

    Mirrors the real vocabulary and the real "one transition per tick"
    rule, which is the property that made client-driven dispatch expensive.
    """

    SEQUENCE = ["QUEUED", "PRECHECK", "READY", "DISPATCHING", "RUNNING", "VERIFYING", "COMPLETED"]

    def __init__(self, *, stall_at: str | None = None) -> None:
        self.tasks: dict[str, dict] = {}
        self.ticks = 0
        self.client_calls = 0
        self.stall_at = stall_at
        self._counter = 0
        self._lock = threading.Lock()

    # -- handlers wired into CompactTerminalTools ------------------------

    def enqueue_task(self, session, prompt, *, title=None, priority=0,
                     metadata=None, request_key=None):
        self.client_calls += 1
        for task in self.tasks.values():
            if request_key and task["request_key"] == request_key:
                return {"status": "TASK_ACCEPTED", "task_id": task["id"],
                        "session": task["session"], "queue_position": 0, "deduplicated": True,
                        "request_key": request_key}
        self._counter += 1
        task_id = f"task-{self._counter}"
        self.tasks[task_id] = {"id": task_id, "session": session, "prompt": prompt,
                               "status": "QUEUED", "request_key": request_key}
        return {"status": "TASK_ACCEPTED", "task_id": task_id, "session": session,
                "queue_position": len(self.tasks) - 1, "deduplicated": False,
                "request_key": request_key}

    def task_status(self, task_id):
        task = self.tasks.get(task_id)
        return {"task": dict(task)} if task else {"error": "TASK_NOT_FOUND"}

    def tick(self, session):
        """One transition for the lane's active task, exactly like the real
        engine. Never more than one -- that is the whole point."""
        with self._lock:
            self.ticks += 1
            for task in self.tasks.values():
                if task["session"] != session:
                    continue
                status = task["status"]
                if status in ("COMPLETED", "BLOCKED", "NEEDS_HUMAN", "FAILED"):
                    continue
                if status == self.stall_at:
                    return {"session": session, "action": "NO_OP", "task_id": task["id"]}
                task["status"] = self.SEQUENCE[self.SEQUENCE.index(status) + 1]
                return {"session": session, "action": task["status"], "task_id": task["id"]}
        return {"session": session, "action": "IDLE"}


def _tools(lane: FakeLane, follower: StartedTaskFollower | None = None) -> CompactTerminalTools:
    handlers = {
        "enqueue_task": lane.enqueue_task,
        "task_status": lane.task_status,
        "dispatch_tick": lane.tick,
    }
    if follower is not None:
        handlers["follow_task"] = follower.follow
    return CompactTerminalTools(terminal=object(), controller=object(), handlers=handlers)


# ---------------------------------------------------------------------------
# 1. One client call starts long work
# ---------------------------------------------------------------------------


def test_one_start_call_persists_dispatches_and_returns_a_durable_task_id():
    lane = FakeLane()
    result = _tools(lane).turn(action="start", target="claude-work", text="implement the thing")

    assert result["status"] == "TASK_STARTED"
    assert result["task_id"] == "task-1"
    assert result["dispatched"] is True
    assert result["task_state"] in ("DISPATCHING", "RUNNING")
    # THE contract: the client paid for exactly one enqueue; every queue
    # transition it used to drive itself happened inside this one call.
    assert lane.client_calls == 1
    assert lane.ticks >= 3, "start must actually drive the lane, not just enqueue"


def test_start_is_durable_before_it_dispatches():
    """Persist-before-dispatch: even if no tick ever runs, the task exists."""
    lane = FakeLane()
    tools = CompactTerminalTools(terminal=object(), controller=object(),
                                 handlers={"enqueue_task": lane.enqueue_task,
                                           "task_status": lane.task_status})
    result = tools.turn(action="start", target="claude-work", text="implement the thing")
    assert result["task_id"] == "task-1"
    assert lane.tasks["task-1"]["status"] == "QUEUED"  # durably recorded, not lost
    assert result["dispatched"] is False
    assert result["status"] == "TASK_ACCEPTED"


def test_start_never_spends_an_unbounded_number_of_ticks_on_the_caller():
    """A lane that refuses to move must not hold the call open forever."""
    lane = FakeLane(stall_at="QUEUED")
    result = _tools(lane).turn(action="start", target="stuck", text="hello")
    assert lane.ticks <= MAX_START_TICKS
    assert result["task_id"] == "task-1"          # still durable
    assert result["dispatched"] is False          # and honestly reported
    assert result["status"] == "TASK_ACCEPTED"


def test_start_deduplicates_on_request_key_like_enqueue_does():
    lane = FakeLane()
    tools = _tools(lane)
    first = tools.turn(action="start", target="claude-work", text="x", request_key="rk-1")
    second = tools.turn(action="start", target="claude-work", text="x", request_key="rk-1")
    assert second["task_id"] == first["task_id"]
    assert second["deduplicated"] is True
    assert len(lane.tasks) == 1


# ---------------------------------------------------------------------------
# 2. Server-side state advances with NO further client calls
# ---------------------------------------------------------------------------


def test_server_side_follower_carries_the_task_with_zero_further_client_calls():
    lane = FakeLane()
    follower = StartedTaskFollower(lane.tick, lane.task_status,
                                   poll_interval_seconds=0.01, ttl_seconds=5)
    result = _tools(lane, follower).turn(action="start", target="claude-work", text="long work")

    assert result["server_side_progress"]["following"] is True
    calls_after_start = lane.client_calls

    deadline = time.monotonic() + 5
    while lane.tasks["task-1"]["status"] != "COMPLETED" and time.monotonic() < deadline:
        time.sleep(0.02)

    # THE proof: the task reached COMPLETED and the client never called again.
    assert lane.tasks["task-1"]["status"] == "COMPLETED"
    assert lane.client_calls == calls_after_start == 1


def test_follower_stops_at_a_settled_task_and_never_claims_the_next_one():
    """It is a follow-through for ONE task, not a second auto-dispatcher --
    the OFF-by-default lane gates queue_loop.py owns stay meaningful."""
    lane = FakeLane()
    lane.enqueue_task("s", "first")
    lane.client_calls = 0
    lane.tasks["task-1"]["status"] = "COMPLETED"
    lane.enqueue_task("s", "second (must stay untouched)")

    follower = StartedTaskFollower(lane.tick, lane.task_status,
                                   poll_interval_seconds=0.01, ttl_seconds=5)
    assert follower.run_until_settled("s", "task-1") == "SETTLED"
    assert lane.ticks == 0                            # nothing to do, nothing done
    assert lane.tasks["task-2"]["status"] == "QUEUED"  # the backlog is not dispatched


def test_follower_gives_up_at_its_ttl_rather_than_running_forever():
    lane = FakeLane(stall_at="RUNNING")
    lane.enqueue_task("s", "stalls")
    follower = StartedTaskFollower(lane.tick, lane.task_status,
                                   poll_interval_seconds=0.01, ttl_seconds=0.15)
    assert follower.run_until_settled("s", "task-1") == "TTL_EXPIRED"


def test_follower_is_idempotent_per_task_and_bounded_in_concurrency():
    lane = FakeLane(stall_at="RUNNING")
    lane.enqueue_task("s", "a")
    follower = StartedTaskFollower(lane.tick, lane.task_status,
                                   poll_interval_seconds=0.01, ttl_seconds=0.3,
                                   max_concurrent=1)
    assert follower.follow("s", "task-1")["reason"] == "FOLLOWING"
    assert follower.follow("s", "task-1")["reason"] == "ALREADY_FOLLOWING"
    lane.enqueue_task("s2", "b")
    refused = follower.follow("s2", "task-2")
    assert refused["following"] is False
    assert refused["reason"] == "FOLLOWER_CAPACITY"


def test_a_follower_that_cannot_read_the_store_stops_quietly():
    def boom(_task_id):
        raise RuntimeError("store is gone")

    follower = StartedTaskFollower(lambda _s: None, boom, poll_interval_seconds=0.01)
    assert follower.run_until_settled("s", "task-1") == "UNREADABLE"


# ---------------------------------------------------------------------------
# 3. The receipt tells the client not to poll
# ---------------------------------------------------------------------------


def test_the_start_receipt_states_the_no_polling_contract():
    lane = FakeLane()
    result = _tools(lane).turn(action="start", target="claude-work", text="work")
    assert result["poll"] is False
    assert result["next_action"] == "none"
    assert "do not" in result["guidance"]
    assert "task_id" in result["guidance"]


@pytest.mark.parametrize("phrase", ["GIVING A SESSION WORK IS ONE CALL", "After start, STOP"])
def test_the_server_policy_states_it_too(phrase: str):
    from terminal_mcp import orchestration_policy as op
    assert phrase in op.SERVER_INSTRUCTIONS


# ---------------------------------------------------------------------------
# 4. Existing actions stay compatible
# ---------------------------------------------------------------------------


def test_start_is_additive_and_every_previous_action_still_exists():
    for action in ("inspect", "send", "send_wait", "wait", "resume", "list_sessions",
                   "list_nodes", "create_session", "delete_session", "enqueue_task",
                   "task_status", "task_batch_status"):
        assert action in TURN_ACTIONS, action
    assert "start" in TURN_ACTIONS
    for alias in ("start_task", "dispatch", "run"):
        assert TURN_ACTION_ALIASES[alias] == "start"
    # The pre-existing aliases are untouched.
    assert TURN_ACTION_ALIASES["enqueue"] == "enqueue_task"
    assert TURN_ACTION_ALIASES["task"] == "task_status"


def test_enqueue_still_only_persists_and_never_dispatches():
    """`start` must not have changed what `enqueue` means -- a caller that
    wants backlog without dispatch still has exactly that."""
    lane = FakeLane()
    result = _tools(lane).turn(action="enqueue", target="claude-work", text="later")
    assert result["status"] == "OK"  # the pre-existing handler-turn envelope, unchanged
    assert result["result"]["status"] == "TASK_ACCEPTED"
    assert lane.ticks == 0
    assert lane.tasks["task-1"]["status"] == "QUEUED"


def test_start_requires_a_target_and_text_like_the_other_pane_actions():
    lane = FakeLane()
    tools = _tools(lane)
    assert tools.turn(action="start", text="x")["error"] == "TARGET_REQUIRED"
    assert tools.turn(action="start", target="s")["error"] == "TEXT_REQUIRED"
    assert lane.tasks == {}


def test_start_refuses_honestly_when_the_queue_is_not_wired():
    tools = CompactTerminalTools(terminal=object(), controller=object(), handlers={})
    result = tools.turn(action="start", target="s", text="x")
    assert result["status"] == "FAILED"
    assert result["error"] == "ACTION_UNAVAILABLE"


# ---------------------------------------------------------------------------
# 5. `send(long_task=True)` and `start` are ONE mechanism, not two
#
# `send(long_task=True)` landed on main while this was in flight. It crossed
# the persist boundary in one call and told the client not to poll -- but it
# stopped at enqueue, on the stated assumption that "the queue loop/watcher
# owns dispatch from here". That loop is OFF by default behind two gates
# (queue_loop.py), so on an ordinary deployment the task sat QUEUED and
# nothing ever started it. Both spellings now run the same start sequence.
# ---------------------------------------------------------------------------


def test_long_task_send_now_actually_dispatches_not_just_persists():
    lane = FakeLane()
    follower = StartedTaskFollower(lane.tick, lane.task_status,
                                   poll_interval_seconds=0.01, ttl_seconds=5)
    result = _tools(lane, follower).turn(action="send", target="worker",
                                         text="run the long task", long_task=True,
                                         request_key="long-task:1")

    # Everything the pre-existing long_task contract promised, unchanged.
    assert result["status"] == "TASK_ACCEPTED"
    assert result["mode"] == "durable_queue"
    assert result["receipt"]["task_id"] == "task-1"
    assert result["client_polling"] is False
    # ...plus the part that was missing: it is actually under way.
    assert result["dispatched"] is True
    assert result["task_state"] in ("DISPATCHING", "RUNNING")
    assert result["poll"] is False
    assert lane.client_calls == 1


def test_long_task_send_hands_the_task_to_the_same_server_side_follower():
    lane = FakeLane()
    follower = StartedTaskFollower(lane.tick, lane.task_status,
                                   poll_interval_seconds=0.01, ttl_seconds=5)
    _tools(lane, follower).turn(action="send", target="worker", text="long",
                                long_task=True)
    deadline = time.monotonic() + 5
    while lane.tasks["task-1"]["status"] != "COMPLETED" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert lane.tasks["task-1"]["status"] == "COMPLETED"
    assert lane.client_calls == 1


def test_a_plain_send_is_untouched_by_the_long_task_path():
    """Only long_task crosses into the queue; an ordinary send still goes
    straight down the guarded submit path and never enqueues anything."""
    lane = FakeLane()
    tools = _tools(lane)
    tools.send_task = lambda *a, **k: {"status": "SUBMIT_CONFIRMED"}  # type: ignore[assignment]
    result = tools.turn(action="send", target="worker", text="short")
    assert result["status"] == "SUBMIT_CONFIRMED"
    assert result.get("mode") is None
    assert lane.tasks == {}
    assert lane.ticks == 0


# ---------------------------------------------------------------------------
# 6. What the LIVE run on hp-linux found (2026-09-19, session
#    test-start-verify). `start` drove the lane correctly -- claim, then the
#    Coordinator gate -- and the gate refused: "session
#    'terminal-mcp-session-health' is already actively working in the same
#    repo/worktree". Two things were wrong with how that was reported:
#
#      a) the receipt still said "work is started and tracked server-side",
#         which is how an orchestrator silently drops a task;
#      b) `dispatched` was computed from a set that lumped BLOCKED in with
#         RUNNING, so a refused task could report dispatched=True.
#
#    Plus one waste: it spent all 6 ticks on a lane that answers PAUSED to
#    every one of them.
# ---------------------------------------------------------------------------


class GatedLane(FakeLane):
    """A lane whose coordinator gate refuses, exactly like the live one."""

    def __init__(self, *, refuse_with: str = "PAUSED",
                 reason: str = "session 'other' is already working in the same worktree") -> None:
        super().__init__()
        self.refuse_with = refuse_with
        self.reason = reason

    def tick(self, session):
        self.ticks += 1
        for task in self.tasks.values():
            if task["session"] != session or task["status"] == self.refuse_with:
                continue
            task["status"] = self.refuse_with
            task["coordinator_decision"] = {"status": "NEEDS_HUMAN", "reason": self.reason}
            return {"session": session, "action": self.refuse_with, "task_id": task["id"]}
        return {"session": session, "action": self.refuse_with}


@pytest.mark.parametrize("refused", ["PAUSED", "BLOCKED", "NEEDS_HUMAN", "FAILED"])
def test_a_gate_refusal_is_never_reported_as_dispatched_or_as_fine(refused: str):
    lane = GatedLane(refuse_with=refused)
    result = _tools(lane).turn(action="start", target="worker", text="work")

    assert result["dispatched"] is False, f"{refused} must never read as dispatched"
    assert result["status"] == "TASK_ACCEPTED"
    assert result["needs_human"] is True
    assert result["next_action"] == "resolve"
    assert "NOT running" in result["guidance"]
    assert refused in result["guidance"]
    # The caller is told not to re-send: the task is already durable.
    assert "do not" in result["guidance"] and "re-send" in result["guidance"]


def test_the_refusal_carries_the_gate_s_own_reason_never_an_invented_one():
    lane = GatedLane(reason="session 'health' is already actively working in the same worktree")
    result = _tools(lane).turn(action="start", target="worker", text="work")
    assert result["blocked_reason"] == "session 'health' is already actively working in the same worktree"
    assert result["blocked_reason"] in result["guidance"]


def test_start_stops_ticking_at_the_first_refusal_instead_of_burning_the_budget():
    """Live: dispatch_ticks=6 against a lane that answers PAUSED every time."""
    lane = GatedLane()
    result = _tools(lane).turn(action="start", target="worker", text="work")
    assert result["dispatch_ticks"] == 1, "one tick reached the gate; the rest taught nothing"
    assert lane.ticks == 1


def test_needs_rework_queued_task_settles_after_one_review_tick():
    """The durable queue may stay QUEUED, but compact start must settle the
    semantic NEEDS_REWORK refusal after one deterministic review."""
    lane = FakeLane()

    def rework_tick(session):
        lane.ticks += 1
        task = lane.tasks["task-1"]
        task["status"] = "QUEUED"
        task["coordinator_decision"] = {
            "status": "NEEDS_REWORK",
            "reason": "uncommitted changes present",
        }
        return {"session": session, "action": "QUEUED", "task_id": task["id"]}

    lane.tick = rework_tick
    result = _tools(lane).turn(action="start", target="worker", text="work")

    assert result["dispatch_ticks"] == 1
    assert result["task_state"] == "NEEDS_REWORK"
    assert result["dispatched"] is False
    assert result["needs_human"] is True
    assert result["next_action"] == "resolve"
    assert result["server_side_progress"]["following"] is False
    assert "uncommitted changes present" in result["guidance"]
    assert lane.ticks == 1


def test_a_refused_task_is_not_handed_to_the_follower():
    lane = GatedLane()
    follower = StartedTaskFollower(lane.tick, lane.task_status,
                                   poll_interval_seconds=0.01, ttl_seconds=5)
    result = _tools(lane, follower).turn(action="start", target="worker", text="work")
    assert result["server_side_progress"]["following"] is False
    assert result["server_side_progress"]["reason"] == "ALREADY_SETTLED"
    assert follower.active_task_ids() == ()


def test_the_follower_stops_on_a_lane_the_gate_paused():
    lane = GatedLane()
    lane.enqueue_task("s", "work")
    lane.tasks["task-1"]["status"] = "PAUSED"
    follower = StartedTaskFollower(lane.tick, lane.task_status,
                                   poll_interval_seconds=0.01, ttl_seconds=5)
    assert follower.run_until_settled("s", "task-1") == "SETTLED"
    assert lane.ticks == 0


def test_an_unreachable_target_is_the_server_s_retry_not_the_user_s_problem():
    lane = GatedLane(refuse_with="WAITING_SESSION")
    result = _tools(lane).turn(action="start", target="worker", text="work")
    assert result["dispatched"] is False
    assert result["needs_human"] is False
    assert result["next_action"] == "none"
    assert "server will retry" in result["guidance"]


def test_a_genuinely_running_task_still_reads_as_dispatched_and_needs_nobody():
    lane = FakeLane()
    result = _tools(lane).turn(action="start", target="worker", text="work")
    assert result["dispatched"] is True
    assert result["needs_human"] is False
    assert result["next_action"] == "none"
    assert "work is started" in result["guidance"]


def test_the_long_task_send_spelling_reports_a_refusal_the_same_way():
    lane = GatedLane()
    result = _tools(lane).turn(action="send", target="worker", text="work", long_task=True)
    assert result["mode"] == "durable_queue"       # pre-existing keys intact
    assert result["client_polling"] is False
    assert result["dispatched"] is False
    assert result["needs_human"] is True
    assert result["blocked_reason"] == lane.reason
