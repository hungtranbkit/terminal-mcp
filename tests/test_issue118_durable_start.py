"""Issue #118: `action=start` must hand back a DURABLE task_id.

Live evidence (2026-11-17, controller 56ccb14, TERMINAL_MCP_ENABLE_QUEUE=0):
`start` on a Codex pane returned TASK_STARTED and on any other pane
SUBMIT_CONFIRMED -- both with task_id=null and no persisted row, because with
the legacy queue retired `start` was either a bare direct send or translated
into `send`. The durable DirectTaskSupervisor (dt_ ids) existed but `start`
never used it.

Contract pinned here, with production wiring (`make_direct_task_supervisor`):
persist BEFORE send, idle-guarded send only, dedup by request key (also
across a restart), the permission/idle preflight refuses without persisting
or sending, and a node-side guard refusal is terminal -- never a later resend.
"""
from __future__ import annotations

import pytest

from terminal_mcp.audit import AuditStore
from terminal_mcp.compact_tools import make_direct_task_supervisor
from terminal_mcp.direct_task import DirectTaskStore
from tests.test_compact_tools import service

IDLE = {"state": "IDLE", "agent_type": "codex", "exists": True}
RUNNING = {"state": "RUNNING", "agent_type": "codex", "exists": True}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def build(tmp_path, store_name="direct.db"):
    tools, terminal, controller = service(tmp_path / "journal.db")
    terminal.audit = AuditStore(tmp_path / "audit.db")
    clock = Clock()
    tools.monotonic = clock
    tools.sleep = clock.sleep
    tools.handlers["legacy_task_by_request_key"] = lambda key: None
    tools.handlers["enqueue_task"] = lambda *a, **kw: pytest.fail("start must never enqueue")
    tools.direct_tasks = make_direct_task_supervisor(
        tools, DirectTaskStore(tmp_path / store_name), accept_wait_seconds=0,
        clock=clock, sleep=clock.sleep)
    return tools, controller, clock


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_ENABLE_QUEUE", "0")   # production setting
    tools, controller, clock = build(tmp_path)
    controller.statuses["codex"] = [IDLE]
    controller.tails["codex"] = "› "
    controller.send_result = {"delivery_state": "SUBMIT_CONFIRMED", "enter_sent": True,
                              "agent_type": "codex", "enter_count": 1, "submission_id": "sub-1"}
    return tools, controller, clock


def start(tools, text="inert canary task", key="req-1"):
    return tools.turn(action="start", target="codex", text=text, timeout=1, request_key=key)


def test_start_returns_durable_task_id_persisted_before_send(rig):
    tools, controller, _ = rig
    seen_rows = []
    original = controller.terminal_send_text

    def send(*args, **kwargs):
        seen_rows.append(tools.direct_tasks.store.by_idempotency_key("turn-start:req-1"))
        return original(*args, **kwargs)

    controller.terminal_send_text = send
    result = start(tools)
    assert result["status"] == "TASK_ACCEPTED"
    assert result["mode"] == "supervised"
    assert result["task_id"].startswith("dt_")
    assert result["task_state"] == "EXECUTION_ACCEPTED"
    assert result["dispatched"] is True and result["client_polling"] is False
    # Persist first: the row existed when the (single) send happened.
    assert seen_rows and seen_rows[0]["task_id"] == result["task_id"]
    assert len(controller.send_calls) == 1
    # Only the atomic node-side idle guard is used, never the bare /send.
    assert controller.send_calls[0][4].get("require_idle") is True


def test_lifecycle_running_verified_then_done_by_marker(rig):
    tools, controller, clock = rig
    result = start(tools)
    task_id = result["task_id"]
    sent_text = controller.send_calls[0][1]
    assert "inert canary task" in sent_text and task_id in sent_text
    controller.statuses["codex"] = [RUNNING]
    controller.tails["codex"] = f"› {sent_text}\n• Working (3s)"
    clock.now += 30
    running = tools.direct_tasks.get(task_id)["task"]
    assert running["state"] == "RUNNING" and running["running_confirmed"] is True
    controller.statuses["codex"] = [IDLE]
    controller.tails["codex"] = f"› {sent_text}\nok\nTMCP-DONE:{task_id}\n› "
    clock.now += 30
    done = tools.direct_tasks.get(task_id)["task"]
    assert done["state"] == "DONE" and done["business_complete"] is True
    assert len(controller.send_calls) == 1


def test_replay_same_key_is_deduplicated_without_second_send(rig):
    tools, controller, _ = rig
    first = start(tools)
    controller.statuses["codex"] = [RUNNING]     # replay must not depend on idleness
    again = start(tools)
    assert again["task_id"] == first["task_id"]
    assert again["deduplicated"] is True
    assert len(controller.send_calls) == 1


def test_same_key_different_text_is_a_conflict_not_a_send(rig):
    tools, controller, _ = rig
    start(tools)
    other = start(tools, text="a different prompt")
    assert other["status"] == "FAILED" and other["error"] == "IDEMPOTENCY_CONFLICT"
    assert len(controller.send_calls) == 1


def test_dedup_survives_controller_restart(rig, tmp_path):
    tools, controller, _ = rig
    first = start(tools)
    # A new process: fresh tools + supervisor over the SAME durable files.
    tools2, controller2, _ = build(tmp_path)
    controller2.statuses["codex"] = [RUNNING]
    controller2.tails["codex"] = ""
    again = start(tools2)
    assert again["task_id"] == first["task_id"] and again["deduplicated"] is True
    assert controller2.send_calls == []
    assert tools2.direct_tasks.get(first["task_id"])["status"] == "OK"


@pytest.mark.parametrize("status", [
    {"error": "SESSION_NOT_FOUND", "reason": "missing"},
    {"state": "IDLE", "agent_type": "codex", "exists": True, "allowed": False},
])
def test_permission_or_missing_target_refuses_without_persisting_or_sending(rig, status):
    tools, controller, _ = rig
    controller.statuses["codex"] = [status]
    result = start(tools)
    assert result["status"] == "FAILED"
    assert result.get("task_id") is None
    assert tools.direct_tasks.store.active() == []
    assert controller.send_calls == []


@pytest.mark.parametrize("state", ["RUNNING", "WAITING_INPUT"])
def test_busy_target_refuses_without_queueing(rig, state):
    tools, controller, _ = rig
    controller.statuses["codex"] = [{"state": state, "agent_type": "codex", "exists": True}]
    result = start(tools)
    assert result["error"] == "DIRECT_TARGET_NOT_IDLE"
    assert tools.direct_tasks.store.active() == []
    assert controller.send_calls == []


@pytest.mark.parametrize("refusal", [
    {"delivery_state": "BLOCKED", "error": "TARGET_NOT_IDLE", "reason": "TARGET_NOT_IDLE"},
    {"error": "IDLE_GUARD_UNSUPPORTED", "sent": False, "enter_sent": False},
])
def test_node_guard_refusal_is_terminal_and_never_resent(rig, refusal):
    tools, controller, clock = rig
    controller.send_result = refusal
    result = start(tools)
    assert result["status"] == "BLOCKED"
    assert result["task_id"].startswith("dt_")
    assert result["task_state"] == "BLOCKED"
    clock.now += 120
    tools.direct_tasks.tick()
    assert len(controller.send_calls) == 1


def test_queue_opt_in_keeps_legacy_paths(rig, monkeypatch):
    tools, controller, _ = rig
    monkeypatch.setenv("TERMINAL_MCP_ENABLE_QUEUE", "1")
    controller.statuses["codex"] = [IDLE, RUNNING]
    result = start(tools)
    assert result["mode"] == "direct" and result.get("task_id") is None
    assert tools.direct_tasks.store.active() == []


@pytest.mark.parametrize("agent_type", ["codex", "shell"])
def test_start_without_supervisor_fails_closed_without_sending(rig, agent_type):
    tools, controller, _ = rig
    tools.direct_tasks = None  # e.g. DirectTaskStore initialization failed
    controller.statuses["codex"] = [{**IDLE, "agent_type": agent_type}]
    result = start(tools)
    assert result["status"] == "FAILED"
    assert result["error"] == "DURABLE_SUPERVISOR_UNAVAILABLE"
    assert result["dispatched"] is False
    assert result.get("task_id") is None
    assert controller.send_calls == []


def test_durable_start_keeps_explicit_idempotency_key_precedence(rig):
    tools, controller, _ = rig
    result = tools.turn(action="start", target="codex", text="inert",
                        timeout=1, request_key="legacy", idempotency_key="explicit")
    assert tools.direct_tasks.store.by_idempotency_key("turn-start:explicit")["task_id"] == result["task_id"]
    assert tools.direct_tasks.store.by_idempotency_key("turn-start:legacy") is None
    replay = tools.turn(action="start", target="codex", text="inert",
                        timeout=1, request_key="changed", idempotency_key="explicit")
    assert replay["task_id"] == result["task_id"] and replay["deduplicated"] is True
    assert len(controller.send_calls) == 1


@pytest.mark.parametrize("restart", [False, True])
def test_placeholder_request_replay_preserves_task_id_without_resend(rig, tmp_path, restart):
    tools, controller, _ = rig
    text = "printf 'TMCP-%s:%s\\n' DONE {TMCP_TASK_ID}"
    first = start(tools, text=text)
    assert first["task_id"].startswith("dt_")
    assert "{TMCP_TASK_ID}" not in controller.send_calls[0][1]
    if restart:
        tools, replay_controller, _ = build(tmp_path)
    else:
        replay_controller = controller
    replay_controller.statuses["codex"] = [RUNNING]
    replay_controller.tails["codex"] = ""
    again = start(tools, text=text)
    assert again["task_id"] == first["task_id"]
    assert again["deduplicated"] is True
    assert len(controller.send_calls) == 1
    if restart:
        assert replay_controller.send_calls == []
    conflict = start(tools, text=text + " different")
    assert conflict["error"] == "IDEMPOTENCY_CONFLICT"
    assert len(controller.send_calls) == 1
