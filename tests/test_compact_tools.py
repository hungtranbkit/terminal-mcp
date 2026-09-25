import inspect
import tempfile
from pathlib import Path

from terminal_mcp.compact_tools import (
    DEFAULT_WAIT_SECONDS,
    MAX_SEND_WAIT_SECONDS,
    MAX_TOTAL_TAIL_CHARS,
    SYNC_WAIT_BUDGET_SECONDS,
    CompactTerminalTools,
)
from terminal_mcp.run_journal import RunJournalStore


class FakeController:
    def __init__(self):
        self.statuses = {}
        self.tails = {}
        self.send_result = {}
        self.send_calls = []
        self.status_calls = 0
        self.bounded_status_timeouts = []

    def terminal_status(self, session):
        self.status_calls += 1
        values = self.statuses.get(session)
        if values is None:
            return {"error": "SESSION_NOT_FOUND", "session": session, "reason": "missing"}
        if isinstance(values, list):
            return values.pop(0) if len(values) > 1 else values[0]
        return values

    def terminal_status_bounded(self, session, timeout_seconds):
        self.bounded_status_timeouts.append(timeout_seconds)
        return self.terminal_status(session)

    def terminal_tail(self, session, lines):
        value = self.tails.get(session)
        if value is None:
            return {"error": "SESSION_NOT_FOUND", "session": session}
        return {"session": session, "output": value, "lines_requested": lines, "truncated": False}

    def terminal_send_text(self, session, text, press_enter, dry_run, **kwargs):
        self.send_calls.append((session, text, press_enter, dry_run, kwargs))
        return {"session": session, **self.send_result}


class FakeTerminal:
    def __init__(self, controller):
        self.controller = controller
        self.bindings = {}
        self.send_result = {}
        self.send_calls = []

    def terminal_get_binding(self, binding):
        if binding not in self.bindings:
            return {"error": "BINDING_NOT_FOUND", "binding": binding}
        return {"binding": binding, "session": self.bindings[binding]}

    def terminal_status_bound(self, binding):
        if binding not in self.bindings:
            return {"error": "BINDING_NOT_FOUND", "binding": binding}
        return {"binding": binding, **self.controller.terminal_status(self.bindings[binding])}

    def terminal_tail_bound(self, binding, lines):
        if binding not in self.bindings:
            return {"error": "BINDING_NOT_FOUND", "binding": binding}
        return {"binding": binding, **self.controller.terminal_tail(self.bindings[binding], lines)}

    def terminal_send_bound(self, binding, text, press_enter, dry_run, idempotency_key):
        self.send_calls.append((binding, text, press_enter, dry_run, idempotency_key))
        return {"binding": binding, "session": self.bindings.get(binding), **self.send_result}


def service(journal_path=None):
    controller = FakeController()
    terminal = FakeTerminal(controller)
    temporary = None
    if journal_path is None:
        temporary = tempfile.TemporaryDirectory()
        journal_path = Path(temporary.name) / "run-journal.db"
    journal = RunJournalStore(journal_path)
    compact = CompactTerminalTools(terminal, controller, run_journal=journal)
    compact._test_temporary_directory = temporary
    return compact, terminal, controller


def test_batch_inspect_mixed_success_missing_and_binding():
    compact, terminal, controller = service()
    terminal.bindings["primary"] = "worker-1"
    controller.statuses["worker-1"] = {
        "session": "worker-1", "state": "WAITING_INPUT", "input_required": True,
        "reason": "prompt", "exists": True,
    }
    controller.tails["worker-1"] = "READY"
    result = compact.batch_inspect(["binding:primary", "missing"], tail_lines=10)
    assert result["count"] == 2
    assert result["targets"][0]["target_type"] == "binding"
    assert result["targets"][0]["state"] == "WAITING_INPUT"
    assert result["targets"][0]["tail"] == "READY"
    assert result["targets"][1]["error"] == "SESSION_NOT_FOUND"


def test_send_task_blocks_menu_without_second_send_or_enter():
    compact, _terminal, controller = service()
    controller.send_result = {
        "delivery_state": "BLOCKED", "error": "TARGET_AWAITING_APPROVAL",
        "enter_sent": False, "enter_count": 0, "correlation_id": "corr-blocked",
    }
    result = compact.send_task("claude-menu", "do work")
    assert result["status"] == "BLOCKED"
    assert result["correlation_id"] == "corr-blocked"
    assert len(controller.send_calls) == 1
    assert controller.send_calls[0][2] is True
    assert result["evidence"]["enter_count"] == 0


def test_send_task_claude_binding_never_duplicates_enter():
    compact, terminal, _controller = service()
    terminal.bindings["claude-primary"] = "claude-1"
    terminal.send_result = {
        "delivery_state": "SUBMIT_CONFIRMED", "submit_status": "SUBMIT_CONFIRMED",
        "agent_type": "claude", "enter_sent": True, "enter_count": 1,
        "correlation_id": "corr-claude", "submission_id": "sub-claude",
    }
    result = compact.send_task("claude-primary", "task")
    assert result["status"] == "SUBMIT_CONFIRMED"
    assert result["submission_id"] == "sub-claude"
    assert len(terminal.send_calls) == 1
    assert terminal.send_calls[0][2] is True
    assert result["evidence"]["enter_count"] == 1


def test_send_task_codex_confirm_path_is_concise():
    compact, _terminal, controller = service()
    controller.send_result = {
        "delivery_state": "SUBMIT_CONFIRMED", "submit_status": "SUBMIT_CONFIRMED",
        "agent_type": "codex", "enter_sent": True, "enter_count": 1,
        "attempts": 1, "correlation_id": "corr-codex",
        "output": "must not be copied into compact receipt",
    }
    result = compact.send_task("codex-1", "task")
    assert result["status"] == "SUBMIT_CONFIRMED"
    assert result["submission_id"] == "corr-codex"
    assert "output" not in result and "output" not in result["evidence"]


def test_send_task_public_wait_budget_is_at_most_twenty_seconds():
    assert MAX_SEND_WAIT_SECONDS == 20
    assert inspect.signature(CompactTerminalTools.send_task).parameters["timeout"].default == 20
    compact, _terminal, _controller = service()
    assert compact.send_task("worker", "task", timeout=21) == {
        "status": "FAILED", "error": "INVALID_TIMEOUT", "allowed": "0 < timeout <= 20"
    }


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_wait_for_state_success_returns_once_with_final_tail():
    compact, terminal, controller = service()
    clock = FakeClock()
    compact.monotonic = clock.monotonic
    compact.sleep = clock.sleep
    controller.statuses["worker"] = [
        {"session": "worker", "state": "RUNNING", "input_required": False, "reason": "busy"},
        {"session": "worker", "state": "WAITING_INPUT", "input_required": True, "reason": "ready"},
    ]
    controller.tails["worker"] = "PROMPT"
    result = compact.wait_for_state("worker", ["WAITING_INPUT"], timeout=10, poll_interval=1)
    assert result["status"] == "MATCHED"
    assert result["continuation_status"] == "COMPLETE"
    assert result["polls"] == 2
    assert result["tail"] == "PROMPT"
    assert result["resume_token"].startswith("wait_")
    assert result["checkpoint_id"]


def test_wait_for_state_timeout_is_pending_and_resumable():
    compact, terminal, controller = service()
    clock = FakeClock()
    compact.monotonic = clock.monotonic
    compact.sleep = clock.sleep
    controller.statuses["worker"] = {
        "session": "worker", "state": "RUNNING", "input_required": False, "reason": "busy"
    }
    controller.tails["worker"] = "still busy"
    result = compact.wait_for_state("worker", ["WAITING_INPUT"], timeout=3, poll_interval=1)
    assert result["status"] == "PENDING"
    assert result["polls"] == 3
    assert result["elapsed_seconds"] == 3
    assert result["waited_ms"] == 3000
    assert result["next_poll_after_ms"] == 1000
    assert result["desired_states"] == ["WAITING_INPUT"]
    assert result["last_observed_state"] == "RUNNING"


def test_public_default_and_large_requested_timeout_use_ten_second_slice():
    assert DEFAULT_WAIT_SECONDS == 20
    assert SYNC_WAIT_BUDGET_SECONDS == 10
    assert inspect.signature(CompactTerminalTools.wait_for_state).parameters["timeout"].default == 20
    compact, _terminal, controller = service()
    clock = FakeClock()
    compact.monotonic = clock.monotonic
    compact.sleep = clock.sleep
    controller.statuses["worker"] = {"session": "worker", "state": "RUNNING"}
    controller.tails["worker"] = "busy"

    result = compact.wait_for_state("worker", ["DONE"], timeout=900, poll_interval=7)

    assert result["status"] == "PENDING"
    assert result["requested_timeout_seconds"] == 900
    assert result["waited_ms"] == 10_000
    assert result["sync_wait_budget_ms"] == 10_000
    assert clock.now == 10
    assert controller.bounded_status_timeouts
    assert max(controller.bounded_status_timeouts) <= SYNC_WAIT_BUDGET_SECONDS
    assert min(controller.bounded_status_timeouts) > 0


def test_resume_does_not_send_and_repeated_complete_poll_is_idempotent(tmp_path):
    compact, terminal, controller = service(tmp_path / "journal.db")
    clock = FakeClock()
    compact.monotonic = clock.monotonic
    compact.sleep = clock.sleep
    controller.statuses["worker"] = {"session": "worker", "state": "RUNNING"}
    controller.tails["worker"] = "busy"
    pending = compact.wait_for_state("worker", ["DONE"], timeout=1)
    assert terminal.send_calls == [] and controller.send_calls == []

    controller.statuses["worker"] = {"session": "worker", "state": "DONE"}
    complete = compact.resume_wait(pending["resume_token"])
    calls_after_complete = controller.status_calls
    replay = compact.resume_wait(pending["resume_token"])

    assert complete["status"] == replay["status"] == "MATCHED"
    assert complete["checkpoint_id"] == replay["checkpoint_id"]
    assert controller.status_calls == calls_after_complete
    assert terminal.send_calls == [] and controller.send_calls == []


def test_wait_can_resume_from_fresh_store_and_retain_result(tmp_path):
    path = tmp_path / "journal.db"
    first, _terminal, controller = service(path)
    clock = FakeClock()
    first.monotonic = clock.monotonic
    first.sleep = clock.sleep
    controller.statuses["worker"] = {"session": "worker", "state": "RUNNING"}
    controller.tails["worker"] = "busy"
    pending = first.wait_for_state("worker", ["DONE"], timeout=1)

    reopened = CompactTerminalTools(first.terminal, controller, run_journal=RunJournalStore(path),
                                    monotonic=clock.monotonic, sleep=clock.sleep)
    controller.statuses["worker"] = {"session": "worker", "state": "DONE"}
    complete = reopened.resume_wait(pending["resume_token"])
    del controller.statuses["worker"]
    retained = CompactTerminalTools(first.terminal, controller, run_journal=RunJournalStore(path)) \
        .resume_wait(pending["resume_token"])

    assert complete["status"] == "MATCHED"
    assert retained["status"] == "MATCHED"
    assert retained["last_observed_state"] == "DONE"


def test_bad_unknown_and_expired_resume_tokens_fail_safely(tmp_path):
    compact, _terminal, _controller = service(tmp_path / "journal.db")
    assert compact.resume_wait("not-a-token") == {
        "status": "FAILED", "error": "INVALID_RESUME_TOKEN"
    }
    assert compact.resume_wait("wait_" + "0" * 32) == {
        "status": "FAILED", "error": "UNKNOWN_RESUME_TOKEN"
    }
    expired = compact.run_journal.start_wait(
        target="worker", target_type="session", desired_states=["DONE"],
        tail_lines=20, requested_timeout_seconds=900, ttl_seconds=-1,
    )
    assert compact.resume_wait(expired["resume_token"]) == {
        "status": "FAILED", "error": "EXPIRED_RESUME_TOKEN"
    }


def test_wait_response_redacts_credentials_and_persists_no_tail(tmp_path):
    compact, _terminal, controller = service(tmp_path / "journal.db")
    clock = FakeClock()
    compact.monotonic = clock.monotonic
    compact.sleep = clock.sleep
    controller.statuses["worker"] = {
        "session": "worker", "state": "RUNNING",
        "reason": "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuv",
    }
    controller.tails["worker"] = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
    result = compact.wait_for_state("worker", ["DONE"], timeout=1)
    serialized = str(result)
    assert "abcdefghijklmnopqrstuv" not in serialized
    assert "abcdefghijklmnopqrstuvwxyz" not in serialized
    reopened = RunJournalStore(tmp_path / "journal.db").get_wait(result["resume_token"])
    assert "tail" not in reopened
    assert "abcdefghijklmnopqrstuv" not in str(reopened)


def test_persistence_failure_prevents_observation():
    class BrokenJournal:
        def start_wait(self, **_kwargs):
            raise OSError("disk unavailable")

    controller = FakeController()
    terminal = FakeTerminal(controller)
    controller.statuses["worker"] = {"session": "worker", "state": "RUNNING"}
    compact = CompactTerminalTools(terminal, controller, run_journal=BrokenJournal())
    result = compact.wait_for_state("worker", ["DONE"], timeout=900)
    assert result["error"] == "CONTINUATION_PERSIST_FAILED"
    assert controller.status_calls == 0


def test_batch_output_has_strict_tail_budget_and_truncation_metadata():
    compact, terminal, controller = service()
    targets = [f"worker-{index}" for index in range(25)]
    for target in targets:
        controller.statuses[target] = {
            "session": target, "state": "RUNNING", "input_required": False, "reason": "busy"
        }
        controller.tails[target] = "x" * 5_000
    result = compact.batch_inspect(targets)
    assert result["response_truncated"] is True
    assert sum(len(row["tail"]) for row in result["targets"]) <= MAX_TOTAL_TAIL_CHARS
    assert all(row["tail_truncated"] for row in result["targets"])


def test_batch_rejects_oversized_target_without_echoing_it():
    compact, _terminal, _controller = service()
    oversized = "x" * 100_000
    result = compact.batch_inspect([oversized])
    assert result == {"error": "INVALID_TARGETS", "max_targets": 25}
    assert oversized not in str(result)


def test_turn_inspect_wraps_batch_in_one_logical_result():
    compact, _terminal, controller = service()
    controller.statuses["worker"] = {
        "session": "worker", "state": "IDLE", "input_required": False, "reason": "ready"
    }
    controller.tails["worker"] = "READY"

    result = compact.turn(action="inspect", target="worker", tail_lines=5)

    assert result["status"] == "OK"
    assert result["action"] == "inspect"
    assert result["result"]["count"] == 1
    assert result["result"]["targets"][0]["tail"] == "READY"


def test_compact_inspect_v2_projects_useful_fields_and_keeps_legacy_row_keys():
    compact, _terminal, controller = service()
    controller.statuses["worker"] = {
        "session": "worker", "state": "IDLE", "input_required": False,
        "reason": "", "exists": True, "cwd": "/repo", "node_id": "node-a",
        "last_activity_s": 12, "resource": {"git": {"branch": "main", "dirty": False}},
        "recovery_state": None, "last_output": "",
    }
    controller.tails["worker"] = ""

    result = compact.turn(action="inspect", target="worker")

    assert result["result"]["version"] == 2
    row = result["result"]["targets"][0]
    assert row["session"] == "worker"
    assert row["state"] == "IDLE"
    assert row["node"] == "node-a"
    assert row["cwd"] == "/repo"
    assert row["branch"] == "main"
    assert row["dirty"] is False
    assert row["last_activity_s"] == 12
    assert row["reason"] == ""
    assert row["resource"]["git"]["branch"] == "main"
    assert row["tail"] == ""
    assert row["tail_truncated"] is False


def test_turn_send_wait_sends_once_then_creates_one_durable_wait():
    compact, _terminal, controller = service()
    clock = FakeClock()
    compact.monotonic = clock.monotonic
    compact.sleep = clock.sleep
    controller.send_result = {
        "delivery_state": "SUBMIT_CONFIRMED", "submit_status": "SUBMIT_CONFIRMED",
        "agent_type": "codex", "enter_sent": True, "enter_count": 1,
        "attempts": 1, "correlation_id": "corr-turn",
    }
    controller.statuses["worker"] = {"session": "worker", "state": "IDLE"}
    controller.tails["worker"] = "done"

    result = compact.turn(
        action="send_wait", target="worker", text="do work",
        desired_states=["IDLE"], timeout=5, idempotency_key="turn:1",
    )

    assert result["status"] == "MATCHED"
    assert result["send"]["status"] == "SUBMIT_CONFIRMED"
    assert result["wait"]["status"] == "MATCHED"
    assert len(controller.send_calls) == 1
    assert result["wait"]["resume_token"].startswith("wait_")


def test_turn_long_task_is_one_call_durable_receipt_without_client_polling():
    compact, _terminal, controller = service()
    calls = []

    def enqueue(session, prompt, **kwargs):
        calls.append((session, prompt, kwargs))
        return {
            "status": "TASK_ACCEPTED",
            "task_id": "task-long-1",
            "session": session,
            "queue_position": 0,
            "request_key": kwargs["request_key"],
        }

    compact.handlers["enqueue_task"] = enqueue
    result = compact.turn(
        action="send", target="worker", text="run the long task",
        long_task=True, request_key="long-task:1",
    )

    assert result["status"] == "TASK_ACCEPTED"
    assert result["mode"] == "durable_queue"
    assert result["receipt"]["task_id"] == "task-long-1"
    assert result["client_polling"] is False
    assert calls == [("worker", "run the long task", {
        "title": None, "priority": 0, "metadata": {"long_task": True},
        "request_key": "long-task:1",
    })]
    # A durable handoff does not inspect, wait, resume, or inject text in the
    # same client call. The queue loop/server watcher owns the next state.
    assert controller.status_calls == 0
    assert controller.send_calls == []


def test_turn_resume_never_sends_or_creates_new_wait():
    compact, _terminal, controller = service()
    clock = FakeClock()
    compact.monotonic = clock.monotonic
    compact.sleep = clock.sleep
    controller.statuses["worker"] = {"session": "worker", "state": "RUNNING"}
    controller.tails["worker"] = "busy"
    pending = compact.turn(
        action="wait", target="worker", desired_states=["DONE"],
        timeout=1, poll_interval=1,
    )
    token = pending["result"]["resume_token"]
    sends_before = len(controller.send_calls)

    controller.statuses["worker"] = {"session": "worker", "state": "DONE"}
    resumed = compact.turn(action="resume", resume_token=token, timeout=5)

    assert resumed["status"] == "MATCHED"
    assert resumed["result"]["resume_token"] == token
    assert len(controller.send_calls) == sends_before


def test_turn_rejects_unknown_action_without_touching_terminal():
    compact, _terminal, controller = service()
    result = compact.turn(action="do_everything_magically", target="worker")
    assert result["error"] == "INVALID_ACTION"
    assert controller.status_calls == 0
    assert controller.send_calls == []


def test_send_task_rejects_contradictory_submit_confirmed_receipt():
    compact, _terminal, controller = service()
    controller.send_result = {
        "delivery_state": "SUBMIT_CONFIRMED",
        "submit_status": "SUBMIT_CONFIRMED",
        "press_enter": True,
        "enter_sent": False,
        "enter_count": 0,
        "correlation_id": "corr-bad-confirm",
    }
    result = compact.send_task("codex-idle", "do work")
    assert result["status"] == "FAILED"
    assert "enter_sent=False" in result["reason"]
    assert result["evidence"]["enter_sent"] is False


def test_send_wait_shares_budget_with_submission_and_resume_never_resends():
    compact, _, controller = service()
    clock = FakeClock()
    compact.monotonic, compact.sleep = clock.monotonic, clock.sleep
    original = controller.terminal_send_text
    controller.send_result = {'delivery_state': 'SUBMIT_CONFIRMED', 'enter_sent': True}
    def slow_send(*args, **kwargs):
        clock.sleep(8)
        return original(*args, **kwargs)
    controller.terminal_send_text = slow_send
    controller.statuses['worker'] = {'state': 'RUNNING'}
    result = compact.turn(action='send_wait', target='worker', text='sleep 60', timeout=10)
    assert result['status'] == 'PENDING'
    assert clock.now <= 10
    controller.statuses['worker'] = {'state': 'IDLE'}
    resumed = compact.resume_wait(result['wait']['resume_token'], timeout=1)
    assert resumed['status'] == 'MATCHED'
    assert len(controller.send_calls) == 1


def test_slow_binding_status_returns_pending_without_cancelling_read():
    import threading
    import time
    compact, terminal, _ = service()
    release = threading.Event()
    def blocked_status(binding):
        release.wait(2)
        return {'state': 'RUNNING'}
    terminal.terminal_status_bound = blocked_status
    try:
        started = time.monotonic()
        result = compact.wait_for_state('binding:primary', ['IDLE'], timeout=0.05)
        assert time.monotonic() - started < 0.5
        assert result['status'] == 'PENDING'
        assert result['resume_token']
    finally:
        release.set()


def test_matched_state_does_not_block_on_slow_final_tail():
    import threading
    import time
    compact, _, controller = service()
    controller.statuses['worker'] = {'state': 'IDLE'}
    release = threading.Event()
    def blocked_tail(*args):
        release.wait(2)
        return {'output': 'done'}
    controller.terminal_tail = blocked_tail
    try:
        started = time.monotonic()
        result = compact.wait_for_state('worker', ['IDLE'], timeout=0.05)
        assert time.monotonic() - started < 0.5
        assert result['status'] == 'MATCHED'
        assert result['tail_unavailable'] is True
    finally:
        release.set()


def test_slow_submission_exhausts_wait_budget_without_extra_probe():
    compact, _, controller = service()
    clock = FakeClock()
    compact.monotonic, compact.sleep = clock.monotonic, clock.sleep
    controller.send_result = {'delivery_state': 'SUBMIT_CONFIRMED'}
    original = controller.terminal_send_text
    def slow_send(*args, **kwargs):
        clock.sleep(12)
        return original(*args, **kwargs)
    controller.terminal_send_text = slow_send
    result = compact.turn(action='send_wait', target='worker', text='sleep 60', timeout=10)
    assert result['status'] == 'PENDING'
    assert result['wait']['resume_token']
    assert controller.status_calls == 0
    assert clock.now == 12


def test_stalled_reads_are_single_flight_and_capacity_bounded():
    import threading
    compact, _, _ = service()
    release = threading.Event()
    calls = []
    def read():
        calls.append(1)
        release.wait(5)
        return {'state': 'IDLE'}
    try:
        for _ in range(3):
            assert compact._bounded_read(('status', 'same'), read, 0.002)['error'] == 'STATUS_PROBE_TIMEOUT'
        assert len(calls) == 1
        for i in range(40):
            compact._bounded_read(('status', i), read, 0.002)
        assert len(compact._reads) == 32
        assert len(calls) == 32
    finally:
        release.set()
