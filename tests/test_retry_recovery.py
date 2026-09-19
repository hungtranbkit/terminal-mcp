"""TMCP-RETRY-CONTEXT-002: a retry continues work instead of restarting it.

The production failure these are written against: retrying a long Claude/Codex
task effectively began a fresh agent turn, so an hour of reasoning was
discarded and the work restarted from zero. `queue_engine.build_dispatch_text`
embedded `task.prompt` verbatim on every attempt, which is what made a retry
indistinguishable from a new task.
"""
from __future__ import annotations

import pytest

from terminal_mcp.retry_recovery import (
    MODES_THAT_REPLAY_PROMPT,
    RECOVER_LIVE_CONVERSATION,
    RECOVERY_RESTART,
    RESUME_FROM_CHECKPOINT,
    RESUME_NATIVE_CONVERSATION,
    RETRY_MODES,
    RecoveryCapsule,
    RetryContext,
    duplicate_retry_is_noop,
    plan_retry,
)

CAPSULE = RecoveryCapsule(
    completed_steps=("audited classify_status", "wrote the marker patterns"),
    next_step="wire the markers into classify_status",
    files_changed=("terminal_mcp/status.py",),
    branch="fix/lane", commit="abc1234", worktree="/home/kimex/workspace/lane",
    tests_run=("tests/test_status.py",), test_results="31 passed",
    last_decision="esc to interrupt is the only verified busy marker",
)


def _ctx(**overrides) -> RetryContext:
    base = dict(task_id="task-abc", session="agent-1", request_key="rk-1", attempt=2,
                agent_type="claude")
    base.update(overrides)
    return RetryContext(**base)


# -- (a) live session retry keeps the same session and conversation ----------

def test_live_session_and_process_continues_the_same_conversation():
    plan = plan_retry(_ctx(session_alive=True, agent_process_alive=True))
    assert plan.mode == RECOVER_LIVE_CONVERSATION
    assert plan.recreate_session is False      # (b) never a new session
    assert plan.relaunch_agent is False        # (b) never a relaunch
    assert plan.replays_prompt is False        # (g) never the original prompt
    assert plan.preserves_conversation is True


# -- (b) no recovery mode ever destroys history -----------------------------

@pytest.mark.parametrize("ctx", [
    _ctx(session_alive=True, agent_process_alive=True),
    _ctx(session_alive=True, conversation_id="conv-1"),
    _ctx(session_alive=False, conversation_id="conv-1"),
    _ctx(session_alive=False, capsule=CAPSULE),
    _ctx(session_alive=False),
], ids=["live", "native-resume", "recreate-and-resume", "checkpoint", "restart"])
def test_no_mode_ever_clears_history(ctx):
    # /clear has no place in any recovery path. Asserted as a field on every
    # mode rather than trusted, which is the point of stating it as a field.
    assert plan_retry(ctx).clears_history is False


# -- (c) process loss with a known conversation id resumes natively ---------

def test_session_alive_but_agent_died_resumes_native_conversation():
    plan = plan_retry(_ctx(session_alive=True, agent_process_alive=False, conversation_id="conv-1"))
    assert plan.mode == RESUME_NATIVE_CONVERSATION
    assert plan.relaunch_agent is True
    assert plan.recreate_session is False      # the session survived; do not touch it
    assert plan.resume_conversation_id == "conv-1"
    assert plan.replays_prompt is False
    assert plan.preserves_conversation is True


def test_session_gone_still_resumes_the_conversation_rather_than_restarting():
    # A lost session is not a reason to lose the conversation too.
    plan = plan_retry(_ctx(session_alive=False, conversation_id="conv-1"))
    assert plan.mode == RESUME_NATIVE_CONVERSATION
    assert plan.recreate_session is True
    assert plan.resume_conversation_id == "conv-1"
    assert plan.replays_prompt is False


def test_conversation_id_on_a_non_resumable_agent_is_not_treated_as_resumable():
    # A shell has no conversation to resume even if an id was once recorded.
    plan = plan_retry(_ctx(session_alive=True, agent_process_alive=False,
                           agent_type="shell", conversation_id="conv-1", capsule=CAPSULE))
    assert plan.mode == RESUME_FROM_CHECKPOINT


# -- (e) checkpoint fallback preserves completed work ----------------------

def test_checkpoint_fallback_carries_completed_work_into_the_continuation():
    plan = plan_retry(_ctx(session_alive=False, conversation_id=None, capsule=CAPSULE))
    assert plan.mode == RESUME_FROM_CHECKPOINT
    assert plan.replays_prompt is False
    text = plan.continuation_text
    assert "audited classify_status" in text          # completed steps survive
    assert "wire the markers into classify_status" in text  # next step survives
    assert "fix/lane" in text and "abc1234" in text   # branch/commit survive
    assert "tests/test_status.py" in text and "31 passed" in text  # test evidence survives
    assert "do not redo" in text


def test_capsule_holding_only_a_conversation_id_is_not_usable_as_a_checkpoint():
    # That id belongs to native resume. Treating it as checkpoint evidence
    # would claim a recovery this mode cannot actually perform.
    thin = RecoveryCapsule(conversation_id="conv-1")
    assert thin.is_usable is False
    plan = plan_retry(_ctx(session_alive=False, agent_type="shell", capsule=thin))
    assert plan.mode == RECOVERY_RESTART


def test_capsule_from_mapping_tolerates_partial_and_unknown_keys():
    capsule = RecoveryCapsule.from_mapping({
        "completed_steps": "did one thing", "next_step": " keep going ",
        "tests_run": ["a", "", "b"], "something_new_a_future_writer_added": 1,
    })
    assert capsule is not None
    assert capsule.completed_steps == ("did one thing",)
    assert capsule.next_step == "keep going"
    assert capsule.tests_run == ("a", "b")
    assert capsule.is_usable is True
    assert RecoveryCapsule.from_mapping(None) is None


# -- (g)/(h) prompt replay, and RECOVERY_RESTART as the genuine last resort --

def test_recovery_restart_only_when_every_preserving_path_is_unavailable():
    plan = plan_retry(_ctx(session_alive=False, agent_process_alive=False,
                           conversation_id=None, capsule=None))
    assert plan.mode == RECOVERY_RESTART
    assert plan.replays_prompt is True      # the ONLY mode allowed to
    assert plan.continuation_text == ""     # it replays instead of continuing
    assert plan.preserves_conversation is False


def test_only_recovery_restart_replays_the_prompt():
    assert MODES_THAT_REPLAY_PROMPT == {RECOVERY_RESTART}
    for ctx in (_ctx(session_alive=True, agent_process_alive=True),
                _ctx(session_alive=True, conversation_id="c"),
                _ctx(session_alive=False, conversation_id="c"),
                _ctx(session_alive=False, capsule=CAPSULE)):
        assert plan_retry(ctx).replays_prompt is False


def test_escalation_order_is_strict_and_earlier_modes_win():
    # Every precondition satisfied at once: the cheapest, most context-
    # preserving mode must win, not whichever check happens to run last.
    rich = _ctx(session_alive=True, agent_process_alive=True, conversation_id="conv-1",
                capsule=CAPSULE)
    assert plan_retry(rich).mode == RETRY_MODES[0] == RECOVER_LIVE_CONVERSATION
    # Drop one precondition at a time and watch it escalate exactly one step.
    assert plan_retry(_ctx(session_alive=True, agent_process_alive=False,
                           conversation_id="conv-1", capsule=CAPSULE)).mode == RETRY_MODES[1]
    assert plan_retry(_ctx(session_alive=True, agent_process_alive=False,
                           agent_type="shell", capsule=CAPSULE)).mode == RETRY_MODES[2]
    assert plan_retry(_ctx(session_alive=True, agent_process_alive=False,
                           agent_type="shell")).mode == RETRY_MODES[3]


# -- continuation text never re-states the request --------------------------

def test_continuation_never_contains_the_original_prompt_and_names_the_task():
    for ctx in (_ctx(session_alive=True, agent_process_alive=True),
                _ctx(session_alive=True, conversation_id="c"),
                _ctx(session_alive=False, capsule=CAPSULE)):
        plan = plan_retry(ctx)
        assert "task_id=task-abc" in plan.continuation_text
        assert "attempt=2" in plan.continuation_text
        assert "do not start over" in plan.continuation_text


def test_native_resume_continuation_asks_the_agent_to_confirm_context_survived():
    # Resuming a conversation is not proof the context came back, so the
    # continuation asks rather than assumes -- and tells the agent to say so
    # instead of silently restarting.
    plan = plan_retry(_ctx(session_alive=True, conversation_id="conv-1"))
    assert "Confirm you can still see the earlier turns" in plan.continuation_text
    assert "instead of restarting" in plan.continuation_text


# -- identity preservation across every mode -------------------------------

@pytest.mark.parametrize("ctx", [
    _ctx(session_alive=True, agent_process_alive=True, capsule=CAPSULE),
    _ctx(session_alive=True, conversation_id="conv-1", capsule=CAPSULE),
    _ctx(session_alive=False, capsule=CAPSULE),
    _ctx(session_alive=False, agent_type="shell"),
], ids=["live", "native", "checkpoint", "restart"])
def test_every_mode_preserves_the_logical_task_identity(ctx):
    preserved = plan_retry(ctx).preserved
    assert preserved["task_id"] == "task-abc"
    assert preserved["request_key"] == "rk-1"
    assert preserved["session"] == "agent-1"
    assert preserved["attempt"] == 2


def test_branch_worktree_and_evidence_are_recovered_from_the_capsule():
    preserved = plan_retry(_ctx(session_alive=False, capsule=CAPSULE)).preserved
    assert preserved["branch"] == "fix/lane"
    assert preserved["worktree"] == "/home/kimex/workspace/lane"
    assert preserved["completed_steps"] == list(CAPSULE.completed_steps)
    assert preserved["tests_run"] == list(CAPSULE.tests_run)
    assert preserved["files_changed"] == list(CAPSULE.files_changed)


def test_explicit_context_wins_over_the_capsule_for_branch_and_worktree():
    # The live facts describe where work is happening NOW; a capsule can be
    # older than the task's current branch.
    preserved = plan_retry(_ctx(session_alive=False, capsule=CAPSULE,
                                branch="fix/newer", worktree="/tmp/newer")).preserved
    assert preserved["branch"] == "fix/newer"
    assert preserved["worktree"] == "/tmp/newer"


# -- (f) duplicate retry does not spawn a second owner ---------------------

def test_duplicate_retry_of_an_owned_task_is_a_noop():
    is_noop, reason = duplicate_retry_is_noop(
        task_id="task-abc", request_key="rk-1",
        active_owner={"task_id": "task-abc", "owner": "queue-engine"})
    assert is_noop is True
    assert "already owned by queue-engine" in reason


def test_duplicate_retry_detected_by_request_key_when_the_task_id_differs():
    # A caller holding only the request_key must still be reconciled, which is
    # what stops one logical request becoming two tasks on two agents.
    is_noop, reason = duplicate_retry_is_noop(
        task_id="task-new", request_key="rk-1",
        active_owner={"task_id": "task-abc", "request_key": "rk-1"})
    assert is_noop is True
    assert "already in flight as task task-abc" in reason


def test_retry_with_no_active_owner_proceeds():
    is_noop, _reason = duplicate_retry_is_noop(task_id="task-abc", request_key="rk-1",
                                               active_owner=None)
    assert is_noop is False


def test_a_different_logical_task_is_not_a_duplicate():
    is_noop, _reason = duplicate_retry_is_noop(
        task_id="task-abc", request_key="rk-1",
        active_owner={"task_id": "other", "request_key": "rk-other"})
    assert is_noop is False
