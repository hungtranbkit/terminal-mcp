"""Issue #52 regressions: no false ACK, no lost task, honest status.

Each test reproduces one observed (2026-10-12) or required failure shape:
false ACK with the prompt still in the composer, a boot hooks menu, a clone
whose context stayed at 0, duplicate/idempotent dispatch, controller restart
mid-send, node/controller unreachable, late completion, and a new client
reading the same task_id.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from terminal_mcp import core
from terminal_mcp.compact_tools import CompactTerminalTools
from terminal_mcp.composer import COMPOSER_DRAFT, COMPOSER_EMPTY, COMPOSER_UNKNOWN, ComposerSnapshot
from terminal_mcp.direct_task import (ACCEPT_TIMEOUT_SECONDS, MAX_RECOVERY_ATTEMPTS,
                                      DirectTaskStore, DirectTaskSupervisor, agent_of,
                                      composer_holds, echoed_then_answered)
from terminal_mcp.queue_policy import queue_disabled_response
from terminal_mcp.run_journal import RunJournalStore
from terminal_mcp.status import classify_status

from tests.test_direct_task_lifecycle import Clock, FakePane, _Controller, _Terminal

HOOKS_MENU = """╭─────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.154.0)              │
╰─────────────────────────────────────────╯
  Hooks need review
  1 hook is new or changed.
  Hooks can run outside the sandbox after you trust them.
› 1. Review hooks
  2. Trust all and continue
  3. Continue without trusting (hooks won't run)
  Press enter to confirm or esc to go back"""


def _sup(tmp_path, pane, clock, **kwargs):
    kwargs.setdefault("activate", pane.activate)
    return DirectTaskSupervisor(DirectTaskStore(tmp_path / "direct.db"), send=pane.send,
                                status=pane.status, tail=pane.tail, clock=clock,
                                settle_seconds=5, **kwargs)


def _run(sup, clock, ticks, step=3.0):
    for _ in range(ticks):
        clock.now += step
        sup.tick()


def _task(sup, task_id):
    return sup.get(task_id, refresh=False)["task"]


# -- false ACK / stuck composer ------------------------------------------------

def test_submit_confirmed_with_prompt_still_in_composer_is_never_running(tmp_path):
    clock = Clock()
    pane = FakePane()
    pane.swallow = True  # adapter ack, but the agent never got the prompt
    sup = _sup(tmp_path, pane, clock, activate=None)
    started = sup.start("s", text="fix the bug")
    task = started["task"]
    assert started["status"] == "OK" and task["state"] == "DISPATCHING"
    assert task["receipt"] == "SUBMIT_CONFIRMED" and task["running_confirmed"] is False
    _run(sup, clock, 10)
    task = _task(sup, task["task_id"])
    assert task["state"] == "STUCK_COMPOSER" and task["running_confirmed"] is False
    assert "Never resend" in task["next_action"]
    assert len(pane.sent) == 1, "a stuck composer is never re-typed"


def test_stuck_composer_recovery_is_enter_only_and_bounded(tmp_path):
    clock = Clock()
    pane = FakePane()
    pane.swallow = True
    sup = _sup(tmp_path, pane, clock)
    task_id = sup.start("s", text="fix the bug")["task"]["task_id"]
    _run(sup, clock, 40)
    task = _task(sup, task_id)
    assert task["state"] == "STUCK_COMPOSER"
    assert len(pane.activations) == MAX_RECOVERY_ATTEMPTS
    assert len(pane.sent) == 1
    assert "exhausted" in task["next_action"]


def test_stuck_composer_recovers_to_running_after_enter(tmp_path):
    clock = Clock()
    pane = FakePane(busy_ticks=3)
    pane.swallow = True
    sup = _sup(tmp_path, pane, clock)
    task_id = sup.start("s", text="fix the bug")["task"]["task_id"]
    _run(sup, clock, 2)
    assert _task(sup, task_id)["state"] in ("DISPATCHING", "STUCK_COMPOSER")
    pane.swallow = False  # the next Enter goes through
    _run(sup, clock, 6)
    task = _task(sup, task_id)
    assert 1 <= len(pane.activations) <= MAX_RECOVERY_ATTEMPTS
    assert task["state"] == "RUNNING" and task["receipt"] == "EXECUTION_ACCEPTED"
    assert len(pane.sent) == 1


def test_clone_context_zero_no_execution_evidence_is_uncertain_not_resent(tmp_path):
    """Send says confirmed, composer empty, but the agent never starts."""
    clock = Clock()
    pane = FakePane(busy_ticks=0)
    pane.output.append("● previous turn output")
    original_send = pane.send

    def lost(target, text, key):
        result = original_send(target, text, key)
        pane.output.pop()  # nothing reached the transcript
        return result

    sup = DirectTaskSupervisor(DirectTaskStore(tmp_path / "direct.db"), send=lost, status=pane.status,
                               tail=pane.tail, activate=pane.activate, clock=clock,
                               settle_seconds=5)
    task_id = sup.start("s", text="do work")["task"]["task_id"]
    _run(sup, clock, int(ACCEPT_TIMEOUT_SECONDS / 3) + 2)
    task = _task(sup, task_id)
    assert task["state"] == "DISPATCH_UNCERTAIN" and task["wait_reason"] == "NO_EXECUTION_EVIDENCE"
    assert len(pane.sent) == 1 and not pane.activations
    # Late evidence still promotes it: the agent starts after all.
    pane.output.append(f"> {pane.sent[0][0]}")
    pane.busy = 2
    _run(sup, clock, 1)
    assert _task(sup, task_id)["state"] == "RUNNING"


def test_short_turn_answer_after_echo_counts_as_acceptance(tmp_path):
    clock = Clock()
    pane = FakePane(busy_ticks=0)
    sup = _sup(tmp_path, pane, clock)
    task_id = sup.start("s", text="quick question")["task"]["task_id"]
    pane.output.append("● Here is the answer")
    _run(sup, clock, 1)
    task = _task(sup, task_id)
    assert task["state"] == "RUNNING" and task["running_confirmed"] is True


# -- boot hooks menu / mode -------------------------------------------------------

def test_boot_hooks_menu_blocks_dispatch_and_mode_is_agent(tmp_path):
    clock = Clock()
    pane = FakePane(agent="codex")
    pane.waiting_input = True
    original = pane.status

    def status(target):
        observed = original(target)
        if observed.get("state") == "WAITING_INPUT":
            observed["reason"] = ("recent prompt matched 'press enter' at bottom offset 0; "
                                  "current command is 'codex'")
        return observed

    sup = DirectTaskSupervisor(DirectTaskStore(tmp_path / "direct.db"), send=pane.send, status=status,
                               tail=pane.tail, activate=pane.activate, clock=clock,
                               settle_seconds=5)
    started = sup.start("codex-pane", text="implement it")
    task = started["task"]
    assert task["state"] == "WAITING_INPUT"
    assert task["wait_reason"] == "SESSION_INPUT_BEFORE_DISPATCH"
    assert task["mode"] == "agent" and task["agent"] == "codex"
    assert task["dispatches"] == 0 and pane.sent == [], "never type into a hooks menu"
    _run(sup, clock, 10)
    assert pane.sent == [] and not pane.activations
    pane.waiting_input = False  # a human answered the menu
    _run(sup, clock, 2)
    task = _task(sup, task["task_id"])
    assert len(pane.sent) == 1 and task["state"] in ("DISPATCHING", "RUNNING")


def test_classifier_names_the_agent_on_waiting_input_and_sees_hooks_menu():
    session = SimpleNamespace(pane_dead=False, activity_epoch=0, pane_current_command="codex")
    state, waiting, reason = classify_status(session, HOOKS_MENU, now=10)
    assert state == "WAITING_INPUT" and waiting
    assert "current command is 'codex'" in reason
    assert agent_of({"state": state, "reason": reason}) == "codex"


def test_agent_of_prefers_structured_fields():
    assert agent_of({"resource": {"agent": "claude"}, "reason": "x"}) == "claude"
    assert agent_of({"reason": "shell prompt; current command is 'bash'"}) == "shell"
    assert agent_of({"reason": "no signal"}) is None


# -- idempotency / restart / new client ---------------------------------------

def test_duplicate_supervise_with_same_key_returns_same_task_even_when_finished(tmp_path):
    clock = Clock()
    pane = FakePane()
    sup = _sup(tmp_path, pane, clock)
    first = sup.start("s", text="goal", idempotency_key="chat-42:task-1")
    task_id = first["task"]["task_id"]
    sup.complete(task_id)
    again = sup.start("s", text="goal", idempotency_key="chat-42:task-1")
    assert again["deduplicated"] is True and again["task"]["task_id"] == task_id
    assert again["task"]["state"] == "DONE"
    assert len(pane.sent) == 1


def test_crash_between_send_intent_and_result_never_retypes(tmp_path):
    clock = Clock()
    pane = FakePane(busy_ticks=3)

    def crashing_send(target, text, key):
        pane.send(target, text, key)  # it DID reach the pane...
        raise ConnectionError("controller died mid-call")  # ...but the result is lost

    sup = DirectTaskSupervisor(DirectTaskStore(tmp_path / "direct.db"), send=crashing_send,
                               status=pane.status, tail=pane.tail, clock=clock, settle_seconds=5)
    task_id = sup.start("s", text="goal")["task"]["task_id"]
    assert _task(sup, task_id)["receipt"] == "DISPATCH_UNCERTAIN"
    restarted = _sup(tmp_path, pane, clock)
    _run(restarted, clock, 1)
    task = _task(restarted, task_id)
    assert task["state"] == "RUNNING" and len(pane.sent) == 1


def test_restart_with_only_the_persisted_intent_verifies_by_observation(tmp_path):
    clock = Clock()
    pane = FakePane(busy_ticks=3)
    store = DirectTaskStore(tmp_path / "direct.db")
    sup = DirectTaskSupervisor(store, send=pane.send, status=pane.status, tail=pane.tail,
                               clock=clock, settle_seconds=5)
    task_id = sup.start("s", text="goal")["task"]["task_id"]
    # Emulate a process killed after the INPUT_SENT write, before the result.
    store.update(task_id, receipt="INPUT_SENT", dispatches=0, next_step=0,
                 pending_advance='{"next_step": 1}', input_sent_at=clock.now)
    restarted = _sup(tmp_path, pane, clock)
    _run(restarted, clock, 2)
    task = _task(restarted, task_id)
    assert len(pane.sent) == 1, "a lost send result is verified, never re-sent"
    assert task["state"] == "RUNNING" and task["steps_dispatched"] == 1


def test_status_from_a_new_client_reads_the_same_task(tmp_path):
    clock = Clock()
    pane = FakePane()
    sup = _sup(tmp_path, pane, clock)
    task_id = sup.start("s", text="goal", idempotency_key="k-new-client")["task"]["task_id"]
    other_process = _sup(tmp_path, pane, clock)
    by_id = other_process.get(task_id)
    by_key = other_process.get_by_key("k-new-client")
    assert by_id["task"]["task_id"] == by_key["task"]["task_id"] == task_id
    assert other_process.get_by_key("unknown")["error"] == "UNKNOWN_IDEMPOTENCY_KEY"


# -- node / controller unavailable ------------------------------------------------

def test_node_unreachable_backs_off_and_does_not_fail_or_resend(tmp_path):
    clock = Clock()
    pane = FakePane(busy_ticks=50)
    sup = _sup(tmp_path, pane, clock)
    task_id = sup.start("s", text="goal")["task"]["task_id"]
    _run(sup, clock, 1)
    original = pane.status
    pane.status = lambda target: {"error": "NODE_UNREACHABLE", "reason": "tunnel down"}
    sup._status = pane.status
    _run(sup, clock, 60, step=10)
    task = _task(sup, task_id)
    assert task["completed_at"] is None, "a 10-minute node outage is not a failed task"
    assert task["last_error_hop"] == "node" and len(pane.sent) == 1
    sup._status = original
    _run(sup, clock, 30, step=10)
    task = _task(sup, task_id)
    assert task["last_error_hop"] is None and task["state"] == "RUNNING"


def test_missing_session_fails_fast_with_session_hop(tmp_path):
    clock = Clock()
    pane = FakePane(busy_ticks=50)
    sup = _sup(tmp_path, pane, clock)
    task_id = sup.start("s", text="goal")["task"]["task_id"]
    pane.exists = False
    _run(sup, clock, 60, step=30)
    task = _task(sup, task_id)
    assert task["state"] == "FAILED" and task["last_error_hop"] == "session"


# -- completion ---------------------------------------------------------------------

def test_late_completion_marker_upgrades_a_limit_failure(tmp_path):
    clock = Clock()
    pane = FakePane()
    sup = _sup(tmp_path, pane, clock)
    task_id = sup.start("s", text="never finishes", max_continuations=1)["task"]["task_id"]
    _run(sup, clock, 60)
    assert _task(sup, task_id)["state"] == "FAILED"
    pane.output.append(f"● TMCP-DONE:{task_id}")
    task = sup.get(task_id)["task"]
    assert task["state"] == "DONE" and task["late_completion"] is True
    assert task["business_complete"] is True


def test_watcher_heartbeat_is_reported(tmp_path):
    clock = Clock()
    pane = FakePane()
    sup = _sup(tmp_path, pane, clock)
    task_id = sup.start("s", text="goal")["task"]["task_id"]
    assert _task(sup, task_id)["watcher"]["alive"] is False
    _run(sup, clock, 1)
    assert _task(sup, task_id)["watcher"]["alive"] is True
    clock.now += 600
    assert _task(sup, task_id)["watcher"]["alive"] is False


# -- composer helpers --------------------------------------------------------------

def test_composer_helpers_distinguish_live_composer_from_transcript_echo():
    needle = "dt_0123456789abcdef"
    stuck = f"● earlier\n❯ do the thing [Terminal MCP supervised task {needle}] ..."
    submitted = f"> do the thing [task {needle}]\n● Working on it\n❯"
    assert composer_holds(stuck, needle)
    assert not composer_holds(submitted, needle)
    assert echoed_then_answered(submitted, needle)
    assert not echoed_then_answered(stuck, needle)
    assert composer_holds("❯ [Pasted text #1 +40 lines]", needle)


def test_core_veto_when_composer_still_holds_prompt_after_enter():
    draft = ComposerSnapshot(COMPOSER_DRAFT, draft_text="fix the offline sync bug")
    assert core._composer_still_holds_prompt(draft, draft, "fix the offline sync bug please")
    assert core._composer_still_holds_prompt(
        ComposerSnapshot(COMPOSER_UNKNOWN),
        ComposerSnapshot(COMPOSER_DRAFT, draft_text="[Pasted text #2 +30 lines]"), "long prompt")
    assert not core._composer_still_holds_prompt(draft, ComposerSnapshot(COMPOSER_EMPTY), "x")
    assert not core._composer_still_holds_prompt(draft, ComposerSnapshot(COMPOSER_UNKNOWN), "x")


class _BootTmux:
    def __init__(self, frames, command="codex"):
        self.frames = list(frames)
        self.command = command

    def get_session(self, name):
        return SimpleNamespace(pane_current_command=self.command)

    def capture_lines(self, name, lines):
        frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        return frame.splitlines()


@pytest.fixture
def fast_boot(monkeypatch):
    monkeypatch.setattr(core, "AGENT_BOOT_READY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(core, "AGENT_BOOT_READY_POLL_SECONDS", 0.0)
    monkeypatch.setattr(core.time, "sleep", lambda _s: None)


def test_create_session_initial_prompt_waits_out_boot_and_refuses_hooks_menu(fast_boot):
    loading = "│ model:     loading   /model to change │\n› Ask Codex to do anything"
    fake = SimpleNamespace(tmux=_BootTmux([loading, loading, HOOKS_MENU]))
    result = core.TerminalService._await_agent_composer_ready(fake, "s")
    assert result["error"] == "BOOT_DIALOG_WAITING_INPUT" and result["status"] == "NOT_SENT"
    assert "never auto-trust" in result["next_action"]


def test_create_session_initial_prompt_proceeds_on_settled_composer(fast_boot):
    ready = "│ model: gpt-5 │\n› Ask Codex to do anything\n  ? for shortcuts"
    fake = SimpleNamespace(tmux=_BootTmux([ready]))
    assert core.TerminalService._await_agent_composer_ready(fake, "s") is None
    shell = SimpleNamespace(tmux=_BootTmux(["$ "], command="bash"))
    assert core.TerminalService._await_agent_composer_ready(shell, "s") is None


def test_create_session_initial_prompt_not_ready_is_reported_not_typed(fast_boot):
    booting = "│ model:     loading   /model to change │"
    fake = SimpleNamespace(tmux=_BootTmux([booting]))
    result = core.TerminalService._await_agent_composer_ready(fake, "s")
    assert result["error"] == "AGENT_NOT_READY"


# -- compact surface ----------------------------------------------------------------

def _tools(tmp_path, pane, clock):
    tools = CompactTerminalTools(_Terminal(), _Controller(pane),
                                 run_journal=RunJournalStore(tmp_path / "j.db"),
                                 sleep=lambda _s: None)
    tools.direct_tasks = DirectTaskSupervisor(
        DirectTaskStore(tmp_path / "direct.db"),
        send=lambda t, text, key: tools.send_task(t, text, idempotency_key=key),
        status=lambda t: tools._status(t), tail=lambda t, n: tools._tail(t, n),
        clock=clock, settle_seconds=5)
    return tools


def test_queue_start_is_refused_with_supervise_next_calls(tmp_path, monkeypatch):
    monkeypatch.delenv("TERMINAL_MCP_ENABLE_QUEUE", raising=False)
    tools = _tools(tmp_path, FakePane(), Clock())
    result = tools.turn(action="start", target="s", text="do it")
    assert result["error"] == "QUEUE_DISABLED_USE_DIRECT_SESSION"
    assert result["task_created"] is False
    assert [call["action"] for call in result["next_calls"]] == [
        "create_session", "supervise", "supervise_status"]
    assert queue_disabled_response()["next_action"] == "create_session_then_supervise"


def test_supervise_status_by_idempotency_key_and_label_task_id(tmp_path):
    clock = Clock()
    tools = _tools(tmp_path, FakePane(), clock)
    started = tools.turn(action="supervise", target="s", text="goal", idempotency_key="k1")
    task_id = started["task"]["task_id"]
    by_key = tools.turn(action="supervise_status", idempotency_key="k1")
    assert by_key["task"]["task_id"] == task_id
    missing = tools.turn(action="supervise_status")
    assert missing["error"] == "TASK_ID_REQUIRED" and "idempotency_key" in missing["accepted"]


def test_inspect_tail_lines_over_max_is_clamped_not_rejected(tmp_path):
    tools = _tools(tmp_path, FakePane(), Clock())
    result = tools.turn(action="inspect", target="s", tail_lines=100)
    assert result["status"] == "OK"
    assert result["result"]["tail_lines_clamped"] == {"requested": 100, "used": 20, "max": 20}
    assert tools.turn(action="inspect", target="s", tail_lines="x")["status"] == "FAILED"
