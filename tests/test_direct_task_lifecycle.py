"""Regression: a turn ending (IDLE/composer) is not the task ending.

Reproduces the 2026-09-30 incident shapes:
  * command finishes -> composer -> pane observed IDLE while the task is
    incomplete; the controller must resume it and only finish on an explicit
    DONE;
  * every send_wait/wait left a PENDING wait_* continuation behind (521 live),
    which accumulated and permanently blocked delete_session.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from terminal_mcp.compact_tools import CompactTerminalTools
from terminal_mcp.direct_task import (DirectTaskStore, DirectTaskSupervisor,
                                      completion_instruction, continuation_prompt,
                                      find_marker)
from terminal_mcp.run_journal import RunJournalStore
from terminal_mcp.session_deletion import deletion_preflight


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


class FakePane:
    """A pane whose agent works for `busy_ticks` observations per prompt.

    `script` decides what each completed turn prints; the default is an
    unfinished turn that just returns to the composer.
    """

    def __init__(self, *, agent="claude", busy_ticks=2, script=None):
        self.agent = agent
        self.busy_ticks = busy_ticks
        self.script = script or (lambda n, text: "turn finished, more work remains")
        self.sent: list[tuple[str, str]] = []
        self.output: list[str] = []
        self.busy = 0
        self.waiting_input = False
        self.exists = True
        self.send_status = "SUBMIT_CONFIRMED"

    def send(self, target, text, key):
        if self.send_status != "SUBMIT_CONFIRMED":
            return {"status": self.send_status, "reason": "guard refused"}
        self.sent.append((text, key))
        self.output.append(f"> {text}")  # the composer echoes the prompt
        self.busy = self.busy_ticks
        return {"status": "SUBMIT_CONFIRMED"}

    def status(self, target):
        if not self.exists:
            return {"error": "SESSION_NOT_FOUND"}
        if self.waiting_input:
            return {"state": "WAITING_INPUT", "reason": "permission prompt"}
        if self.busy > 0:
            self.busy -= 1
            if self.busy == 0:
                self.output.append(self.script(len(self.sent), self.sent[-1][0]))
            return {"state": "RUNNING", "reason": f"{self.agent} spinner"}
        return {"state": "IDLE", "reason": f"{self.agent} is back at its composer; current command is {self.agent!r}"}

    def tail(self, target, lines):
        return {"output": "\n".join(self.output[-lines:])}


def _supervisor(tmp_path, pane, clock, **kwargs):
    return DirectTaskSupervisor(DirectTaskStore(tmp_path / "direct.db"), send=pane.send,
                                status=pane.status, tail=pane.tail, clock=clock,
                                settle_seconds=5, **kwargs)


def _run(sup, clock, ticks, step=3.0):
    for _ in range(ticks):
        clock.now += step
        sup.tick()


def test_idle_after_turn_is_not_done_and_controller_resumes_until_explicit_done(tmp_path):
    clock = Clock()
    # Turns 1 and 2 end at the composer unfinished; turn 3 prints the marker.
    pane = FakePane(script=lambda n, text: (
        f"● All checks pass.\n● TMCP-DONE:{TASK['id']}" if n == 3 else "● Ran tests; 2 still failing"))
    sup = _supervisor(tmp_path, pane, clock)
    started = sup.start("pos-hotfix", text="Fix the offline POS sync bug and make tests pass")
    TASK["id"] = started["task"]["task_id"]
    assert started["status"] == "OK" and started["task"]["mode"] == "agent"
    assert len(pane.sent) == 1

    _run(sup, clock, 5)  # turn 1 ends -> pane IDLE, settle elapses
    task = sup.get(TASK["id"], refresh=False)["task"]
    assert task["state"] == "RUNNING" and not task["business_complete"]
    assert len(pane.sent) == 2, "controller must re-dispatch after a normal turn end"
    assert "continuation 1/6" in pane.sent[1][0]

    _run(sup, clock, 30)
    task = sup.get(TASK["id"], refresh=False)["task"]
    assert task["state"] == "DONE" and task["business_complete"] is True
    assert len(pane.sent) == 3
    _run(sup, clock, 10)
    assert len(pane.sent) == 3, "no dispatch after explicit DONE"
    # idempotency keys are unique per dispatch
    assert len({key for _text, key in pane.sent}) == 3


TASK: dict[str, str] = {}


def test_prompt_echo_never_completes_its_own_task():
    task_id = "dt_0123456789abcdef"
    echoed = "\n".join([f"> goal {completion_instruction(task_id)}",
                        f"> {continuation_prompt(task_id, 1, 6)}"])
    assert find_marker(echoed, task_id) is None
    assert find_marker("$ printf 'TMCP-%s:%s\\n' DONE " + task_id, task_id) is None
    assert find_marker(f"⏺ TMCP-DONE:{task_id}", task_id) == ("DONE", "")
    assert find_marker(f"• TMCP-BLOCKED:{task_id} need prod creds", task_id) == ("BLOCKED", "need prod creds")
    assert find_marker(f"TMCP-DONE:{task_id}ff", task_id) is None


def test_continuation_limit_stops_without_spinning(tmp_path):
    clock = Clock()
    pane = FakePane()
    sup = _supervisor(tmp_path, pane, clock)
    task_id = sup.start("s", text="never finishes", max_continuations=2)["task"]["task_id"]
    _run(sup, clock, 60)
    task = sup.get(task_id, refresh=False)["task"]
    assert task["state"] == "FAILED" and "CONTINUATION_LIMIT_REACHED" in task["reason"]
    assert len(pane.sent) == 3  # initial + 2 continuations
    _run(sup, clock, 20)
    assert len(pane.sent) == 3


def test_waiting_input_pauses_then_resumes(tmp_path):
    clock = Clock()
    pane = FakePane()
    sup = _supervisor(tmp_path, pane, clock)
    task_id = sup.start("s", text="goal")["task"]["task_id"]
    pane.waiting_input = True
    _run(sup, clock, 20)
    task = sup.get(task_id, refresh=False)["task"]
    assert task["state"] == "WAITING_INPUT" and task["wait_reason"] == "SESSION_INPUT"
    assert len(pane.sent) == 1, "never auto-answer or continue over an input prompt"
    pane.waiting_input = False
    _run(sup, clock, 6)
    assert sup.get(task_id, refresh=False)["task"]["state"] == "RUNNING"
    assert len(pane.sent) == 2


def test_blocked_marker_ends_task(tmp_path):
    clock = Clock()
    ids = {}
    pane = FakePane(script=lambda n, text: f"● TMCP-BLOCKED:{ids['id']} missing DB password")
    sup = _supervisor(tmp_path, pane, clock)
    ids["id"] = sup.start("s", text="deploy")["task"]["task_id"]
    _run(sup, clock, 10)
    task = sup.get(ids["id"], refresh=False)["task"]
    assert task["state"] == "BLOCKED" and task["reason"] == "missing DB password"
    assert len(pane.sent) == 1


def test_shell_steps_run_sequentially_and_end_only_on_explicit_completion(tmp_path):
    clock = Clock()
    ids = {}

    def script(n, text):
        return f"TMCP-DONE:{ids['id']}" if "printf" in text else f"step {n} ok"

    pane = FakePane(agent="bash", busy_ticks=1, script=script)
    sup = _supervisor(tmp_path, pane, clock)
    started = sup.start("sh", steps=["echo one", "echo two", "echo three"])
    ids["id"] = started["task"]["task_id"]
    assert started["task"]["mode"] == "shell"
    assert pane.sent[0][0] == "echo one", "shell steps are sent verbatim"
    _run(sup, clock, 20)
    assert [t for t, _ in pane.sent] == ["echo one", "echo two", "echo three"]
    task = sup.get(ids["id"], refresh=False)["task"]
    assert task["state"] == "WAITING_INPUT" and task["wait_reason"] == "AWAITING_EXPLICIT_COMPLETION"
    assert not task["business_complete"], "running out of steps is not DONE"
    done = sup.complete(ids["id"])
    assert done["task"]["state"] == "DONE"


def test_send_failures_are_capped(tmp_path):
    clock = Clock()
    pane = FakePane()
    sup = _supervisor(tmp_path, pane, clock)
    task_id = sup.start("s", text="goal")["task"]["task_id"]
    pane.send_status = "BLOCKED"
    _run(sup, clock, 60)
    task = sup.get(task_id, refresh=False)["task"]
    assert task["state"] == "BLOCKED" and "dispatch not confirmed" in task["reason"]


def test_supervision_survives_controller_restart(tmp_path):
    clock = Clock()
    pane = FakePane()
    sup = _supervisor(tmp_path, pane, clock)
    task_id = sup.start("s", text="goal")["task"]["task_id"]
    restarted = _supervisor(tmp_path, pane, clock)
    _run(restarted, clock, 5)
    assert len(pane.sent) == 2
    assert restarted.get(task_id, refresh=False)["task"]["continuations"] == 1


def test_one_active_task_per_target_and_idempotent_start(tmp_path):
    clock = Clock()
    pane = FakePane()
    sup = _supervisor(tmp_path, pane, clock)
    first = sup.start("s", text="goal", idempotency_key="k1")
    again = sup.start("s", text="goal", idempotency_key="k1")
    assert again["deduplicated"] and again["task"]["task_id"] == first["task"]["task_id"]
    assert sup.start("s", text="other")["error"] == "TARGET_HAS_ACTIVE_SUPERVISED_TASK"
    assert len(pane.sent) == 1


# -- wait continuations ---------------------------------------------------------

def _wait(journal, target):
    return journal.start_wait(target=target, target_type="session",
                              desired_states=["IDLE", "WAITING_INPUT"], tail_lines=20,
                              requested_timeout_seconds=20)


def test_new_wait_supersedes_old_pending_waits_so_they_cannot_accumulate(tmp_path):
    journal = RunJournalStore(tmp_path / "journal.db")
    tokens = [_wait(journal, "pos")["resume_token"] for _ in range(50)]
    _wait(journal, "other")
    assert journal.pending_wait_count("pos") == 1
    assert journal.pending_wait_count() == 2
    old = journal.get_wait(tokens[0])
    assert old["status"] == "SUPERSEDED" and old["completed_at"] is not None
    # a late observation cannot resurrect a finalized wait
    again = journal.record_wait_observation(tokens[0], status="PENDING", last_observed_state="RUNNING",
                                            input_required=False, reason=None, polls=1, waited_ms=10)
    assert again["status"] == "SUPERSEDED"


def test_stale_waits_are_reaped_and_never_block_delete(tmp_path):
    journal = RunJournalStore(tmp_path / "journal.db")
    token = _wait(journal, "pos")["resume_token"]
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with journal._connection() as connection:
        connection.execute("UPDATE wait_continuations SET updated_at = ?", (old,))
    assert journal.reap_stale_waits() == 1
    assert journal.get_wait(token)["status"] == "EXPIRED"
    assert journal.active_for_session("pos") == []


def test_delete_preflight_cancels_orphan_waits_and_blocks_only_live_supervision(tmp_path):
    journal = RunJournalStore(tmp_path / "journal.db")
    # Legacy shape: hundreds of PENDING waits created before supersession.
    for _ in range(300):
        _wait(journal, "pos")
    with journal._connection() as connection:
        connection.execute("UPDATE wait_continuations SET status = 'PENDING', completed_at = NULL")
        connection.execute("UPDATE journal_runs SET completed_at = NULL")
    clock = Clock()
    pane = FakePane()
    sup = _supervisor(tmp_path, pane, clock)
    task_id = sup.start("pos", text="goal")["task"]["task_id"]

    blocked = deletion_preflight("pos", queue=None, run_journal=journal, direct_tasks=sup)
    assert blocked["error"] == "SESSION_HAS_ACTIVE_SUPERVISED_TASK"
    assert blocked["references"]["active_runs"] == [], "passive waits are not active runs"

    sup.cancel(task_id)
    ok = deletion_preflight("pos", queue=None, run_journal=journal, direct_tasks=sup)
    assert ok["ok"] is True and ok["references"]["cancelled_waits"] == 300
    assert journal.pending_wait_count("pos") == 0
    assert journal.active_session_index() == {}


# -- compact surface --------------------------------------------------------------

class _Controller:
    def __init__(self, pane):
        self.pane = pane

    def terminal_status(self, session):
        return {"session": session, "exists": True, "input_required": False, **self.pane.status(session)}

    def terminal_tail(self, session, lines):
        return {"session": session, "truncated": False, **self.pane.tail(session, lines)}

    def terminal_send_text(self, session, text, press_enter, dry_run, **kwargs):
        self.pane.send(session, text, kwargs.get("idempotency_key"))
        return {"session": session, "delivery_state": "SUBMIT_CONFIRMED", "enter_sent": True,
                "press_enter": True}


class _Terminal:
    def terminal_get_binding(self, binding):
        return {"error": "BINDING_NOT_FOUND"}


def test_compact_wait_matching_idle_reports_task_not_complete(tmp_path):
    pane = FakePane(busy_ticks=0)
    controller = _Controller(pane)
    tools = CompactTerminalTools(_Terminal(), controller,
                                 run_journal=RunJournalStore(tmp_path / "j.db"),
                                 sleep=lambda _s: None)
    clock = Clock()
    tools.direct_tasks = DirectTaskSupervisor(
        DirectTaskStore(tmp_path / "d.db"),
        send=lambda t, text, key: tools.send_task(t, text, idempotency_key=key),
        status=lambda t: tools._status(t), tail=lambda t, n: tools._tail(t, n),
        clock=clock, settle_seconds=5)
    started = tools.turn(action="supervise", target="pos", text="long task")
    assert started["status"] == "OK" and started["action"] == "supervise"
    task_id = started["task"]["task_id"]

    waited = tools.turn(action="wait", target="pos", timeout=2)
    assert waited["status"] == "MATCHED"
    assert waited["business_complete"] is False
    assert waited["supervised_task"]["task_id"] == task_id

    for action in ("supervise_status",):
        assert tools.turn(action=action, task_id=task_id)["task"]["state"] == "RUNNING"
    cancelled = tools.turn(action="supervise_cancel", task_id=task_id)
    assert cancelled["task"]["state"] == "CANCELLED"
    assert "supervised_task" not in tools.turn(action="wait", target="pos", timeout=2)
