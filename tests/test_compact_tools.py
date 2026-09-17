import base64
import inspect
import json

from terminal_mcp.compact_tools import (
    MAX_RESUME_TOKEN_CHARS,
    MAX_TOTAL_TAIL_CHARS,
    MAX_WAIT_SECONDS,
    CompactTerminalTools,
)


class FakeController:
    def __init__(self):
        self.statuses = {}
        self.tails = {}
        self.send_result = {}
        self.send_calls = []

    def terminal_status(self, session):
        values = self.statuses.get(session)
        if values is None:
            return {"error": "SESSION_NOT_FOUND", "session": session, "reason": "missing"}
        if isinstance(values, list):
            return values.pop(0) if len(values) > 1 else values[0]
        return values

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


def service():
    controller = FakeController()
    terminal = FakeTerminal(controller)
    return CompactTerminalTools(terminal, controller), terminal, controller


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
    assert result["polls"] == 2
    assert result["tail"] == "PROMPT"


def test_wait_for_state_deadline_is_pending_and_resumable():
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
    assert result["polls"] == 4
    assert result["elapsed_seconds"] == 3
    assert result["target"] == "worker"
    assert result["desired_states"] == ["WAITING_INPUT"]
    assert result["task_id"] is None
    assert result["next_action"]
    assert result["resume_token"]


def test_wait_defaults_and_hard_maximum():
    assert MAX_WAIT_SECONDS == 45
    assert inspect.signature(CompactTerminalTools.wait_for_state).parameters["timeout"].default == 30
    compact, _terminal, _controller = service()
    assert compact.wait_for_state("worker", ["DONE"], timeout=45.01) == {
        "status": "FAILED", "error": "INVALID_TIMEOUT", "allowed": "0 < timeout <= 45"
    }


def test_resume_token_roundtrip_reaches_matched_and_contains_no_observation_text():
    compact, _terminal, controller = service()
    clock = FakeClock()
    compact.monotonic = clock.monotonic
    compact.sleep = clock.sleep
    controller.statuses["worker"] = {
        "session": "worker", "state": "RUNNING", "reason": "secret prompt text",
        "current_task": {"id": "task-7"},
    }
    controller.tails["worker"] = "sensitive full tail"
    pending = compact.wait_for_state("worker", ["DONE"], timeout=1)
    raw = base64.urlsafe_b64decode(pending["resume_token"] + "=" * (-len(pending["resume_token"]) % 4))
    payload = json.loads(raw)
    assert payload == {"v": 1, "target": "worker", "desired_states": ["DONE"], "tail_lines": 20}
    assert b"sensitive full tail" not in raw
    assert b"secret prompt text" not in raw
    assert pending["task_id"] == "task-7"

    controller.statuses["worker"] = {"session": "worker", "state": "DONE", "reason": "complete"}
    matched = compact.resume_wait(pending["resume_token"])
    assert matched["status"] == "MATCHED"
    assert matched["target"] == "worker"


def test_resume_token_rejects_malformed_oversized_and_long_timeout():
    compact, _terminal, controller = service()
    assert compact.resume_wait("not-base64!")["error"] == "INVALID_RESUME_TOKEN"
    assert compact.resume_wait("a" * (MAX_RESUME_TOKEN_CHARS + 1))["error"] == "INVALID_RESUME_TOKEN"
    controller.statuses["worker"] = {"session": "worker", "state": "RUNNING"}
    controller.tails["worker"] = "tail"
    pending = compact.wait_for_state("worker", ["DONE"], timeout=0.01, poll_interval=1)
    assert compact.resume_wait(pending["resume_token"], timeout=46) == {
        "status": "FAILED", "error": "INVALID_TIMEOUT", "allowed": "0 < timeout <= 45"
    }


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
