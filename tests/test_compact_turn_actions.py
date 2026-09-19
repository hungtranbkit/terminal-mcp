"""terminal_turn as THE single surface: every retired tool as an action.

The compact ChatGPT catalog advertises one tool (chatgpt_sidecar.CATALOG), so
anything a normal orchestration step needs has to be reachable here. These
tests assert two things that together are the whole point:

  1. each action routes to the SAME implementation the standalone tool used --
     asserted by injecting a recording handler and checking the exact call, so
     a future refactor cannot quietly grow a second, weaker path;
  2. a missing/invalid argument is an honest refusal, never a silent no-op.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from terminal_mcp.compact_tools import (TURN_ACTION_ALIASES, TURN_ACTIONS,
                                        TURN_HANDLER_ACTIONS, TURN_PANE_ACTIONS,
                                        CompactTerminalTools)
from terminal_mcp.run_journal import RunJournalStore


class Recorder:
    """One stand-in for every injected handler; records how it was called."""

    def __init__(self, result=None):
        self.calls: list[tuple[tuple, dict]] = []
        self.result = {"ok": True} if result is None else result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class FakeController:
    def terminal_status(self, session):
        return {"session": session, "state": "IDLE", "input_required": False,
                "reason": "idle", "exists": True}

    def terminal_tail(self, session, lines):
        return {"session": session, "output": "READY", "truncated": False}

    def terminal_send_text(self, session, text, press_enter, dry_run, **kwargs):
        return {"session": session, "delivery_state": "SUBMIT_CONFIRMED",
                "enter_sent": True, "correlation_id": "corr-1"}


class FakeTerminal:
    def __init__(self, controller):
        self.controller = controller

    def terminal_get_binding(self, binding):
        return {"error": "BINDING_NOT_FOUND", "binding": binding}


def _tools(handlers=None):
    controller = FakeController()
    journal = RunJournalStore(Path(tempfile.mkdtemp()) / "run-journal.db")
    return CompactTerminalTools(FakeTerminal(controller), controller,
                                run_journal=journal, handlers=handlers or {})


# ---------------------------------------------------------------------------
# The vocabulary itself
# ---------------------------------------------------------------------------

def test_the_action_vocabulary_covers_every_retired_tool():
    for action in ("inspect", "send", "send_wait", "wait", "resume",
                   "list_sessions", "list_nodes", "create_session",
                   "delete_session", "enqueue_task", "task_status",
                   "task_batch_status"):
        assert action in TURN_ACTIONS, action
    assert set(TURN_ACTIONS) == set(TURN_PANE_ACTIONS) | set(TURN_HANDLER_ACTIONS)


def test_an_unknown_action_is_refused_and_names_what_is_allowed():
    result = _tools().turn(action="frobnicate")
    assert result["status"] == "FAILED"
    assert result["error"] == "INVALID_ACTION"
    # A model that guessed wrong must be able to recover from the error alone.
    assert "list_sessions" in result["allowed"]
    assert result["aliases"]["list"] == "list_sessions"


@pytest.mark.parametrize("alias,canonical", sorted(TURN_ACTION_ALIASES.items()))
def test_every_alias_resolves_to_its_canonical_action(alias, canonical):
    handler = Recorder()
    tools = _tools({TURN_HANDLER_ACTIONS[canonical]: handler})
    result = tools.turn(action=alias, target="s1", text="do it", task_id="t1",
                        task_ids=["t1"])
    assert result["action"] == canonical, f"{alias} must resolve to {canonical}"
    assert len(handler.calls) == 1


def test_action_matching_is_forgiving_about_case_and_dashes():
    handler = Recorder()
    tools = _tools({"list_sessions": handler})
    for spelling in ("list_sessions", "LIST_SESSIONS", "list-sessions", "  list  "):
        assert tools.turn(action=spelling)["action"] == "list_sessions"
    assert len(handler.calls) == 4


# ---------------------------------------------------------------------------
# Routing: the same implementation, with the right arguments
# ---------------------------------------------------------------------------

def test_list_sessions_and_list_nodes_take_no_arguments():
    sessions, nodes = Recorder({"sessions": []}), Recorder([{"node_id": "hp"}])
    tools = _tools({"list_sessions": sessions, "list_nodes": nodes})
    assert tools.turn(action="list")["status"] == "OK"
    assert sessions.calls == [((), {})]
    result = tools.turn(action="nodes")
    assert result["status"] == "OK"
    assert result["result"] == [{"node_id": "hp"}]


def test_create_session_passes_every_lifecycle_argument_through():
    create = Recorder({"session": "agent-new", "state": "READY"})
    tools = _tools({"create_session": create})
    result = tools.turn(action="create", target="agent-new", agent_type="claude",
                        working_directory="/home/kimex/work", initial_prompt="hi",
                        grant_mode="read", binding="primary", node="hp-linux")
    assert result["status"] == "OK"
    args, kwargs = create.calls[0]
    assert args == ("agent-new",)
    assert kwargs == {"agent_type": "claude", "working_directory": "/home/kimex/work",
                      "initial_prompt": "hi", "grant_mode": "read",
                      "binding": "primary", "node": "hp-linux"}


def test_delete_session_uses_target_as_the_name():
    delete = Recorder({"deleted": True})
    assert _tools({"delete_session": delete}).turn(
        action="delete", target=" agent-old ")["status"] == "OK"
    assert delete.calls == [(("agent-old",), {})]


def test_enqueue_task_uses_target_as_session_and_text_as_prompt():
    enqueue = Recorder({"status": "TASK_ACCEPTED", "task_id": "task-1"})
    tools = _tools({"enqueue_task": enqueue})
    result = tools.turn(action="enqueue", target="claude-1", text="run the suite",
                        title="suite", priority=3, metadata={"lane": "ci"},
                        request_key="req-1")
    assert result["status"] == "OK"
    assert result["result"]["task_id"] == "task-1"
    args, kwargs = enqueue.calls[0]
    assert args == ("claude-1", "run the suite")
    assert kwargs == {"title": "suite", "priority": 3,
                      "metadata": {"lane": "ci"}, "request_key": "req-1"}


def test_task_status_and_batch_status_route_their_ids():
    one, many = Recorder({"task": {"id": "t1"}}), Recorder({"tasks": []})
    tools = _tools({"task_status": one, "task_batch_status": many})
    assert tools.turn(action="task", task_id=" t1 ")["status"] == "OK"
    assert one.calls == [(("t1",), {})]
    assert tools.turn(action="tasks", task_ids=["t1", "t2"])["status"] == "OK"
    assert many.calls == [((["t1", "t2"],), {})]


def test_a_handler_error_is_reported_as_failed_not_ok():
    tools = _tools({"create_session": Recorder({"error": "SESSION_ALREADY_EXISTS"})})
    result = tools.turn(action="create", target="taken")
    assert result["status"] == "FAILED"
    assert result["result"]["error"] == "SESSION_ALREADY_EXISTS"


# ---------------------------------------------------------------------------
# Refusals: never a silent no-op
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action,kwargs,error", [
    ("create", {}, "TARGET_REQUIRED"),
    ("delete", {"target": "  "}, "TARGET_REQUIRED"),
    ("enqueue", {"text": "x"}, "TARGET_REQUIRED"),
    ("enqueue", {"target": "s1"}, "TEXT_REQUIRED"),
    ("task", {}, "TASK_ID_REQUIRED"),
    ("tasks", {}, "TASK_IDS_REQUIRED"),
])
def test_a_missing_argument_is_refused_before_any_handler_runs(action, kwargs, error):
    handlers = {key: Recorder() for key in TURN_HANDLER_ACTIONS.values()}
    result = _tools(handlers).turn(action=action, **kwargs)
    assert result["status"] == "FAILED"
    assert result["error"] == error
    assert all(not handler.calls for handler in handlers.values()), \
        "a refused call must never reach the implementation"


def test_an_unwired_action_says_so_instead_of_pretending_to_work():
    result = _tools().turn(action="enqueue", target="s1", text="go")
    assert result["status"] == "FAILED"
    assert result["error"] == "ACTION_UNAVAILABLE"
    assert result["action"] == "enqueue_task"


# ---------------------------------------------------------------------------
# The pane actions still behave exactly as before
# ---------------------------------------------------------------------------

def test_inspect_is_batch_inspect_for_one_call():
    result = _tools().turn(action="inspect", targets=["s1", "s2"], tail_lines=5)
    assert result["status"] == "OK"
    assert result["result"]["count"] == 2
    assert [row["target"] for row in result["result"]["targets"]] == ["s1", "s2"]


def test_send_still_goes_through_the_guarded_path():
    result = _tools().turn(action="send", target="s1", text="go")
    assert result["status"] == "SUBMIT_CONFIRMED"
    assert result["result"]["correlation_id"] == "corr-1"


def test_pane_actions_do_not_need_any_handler_wired():
    """They are implemented in this module; only the routed verbs are injected."""
    tools = _tools()
    assert tools.turn(action="inspect", target="s1")["status"] == "OK"
    assert tools.turn(action="wait", target="s1", desired_states=["IDLE"],
                      timeout=1)["status"] in {"MATCHED", "PENDING", "FAILED"}


# ---------------------------------------------------------------------------
# The wiring itself. Without this, a rename upstream leaves terminal_turn's
# routed actions silently ACTION_UNAVAILABLE on the real server -- which on a
# one-tool surface means the whole capability is gone.
# ---------------------------------------------------------------------------

def test_turn_handler_map_covers_every_routed_action():
    """The map build_mcp wires from. Keyword-only and exhaustive, so adding an
    action to TURN_HANDLER_ACTIONS without wiring it fails here rather than
    silently returning ACTION_UNAVAILABLE on the live server."""
    from terminal_mcp.mcp_app import turn_handler_map

    def stub():
        return None

    mapping = turn_handler_map(
        list_sessions=stub, list_nodes=stub, create_session=stub,
        delete_session=stub, enqueue_task=stub, task_status=stub,
        task_batch_status=stub)
    assert set(mapping) == set(TURN_HANDLER_ACTIONS.values())
    assert all(callable(value) for value in mapping.values())


def test_the_real_server_routes_a_turn_action_to_a_real_handler():
    """End-to-end through the actual registered tool: the answer must come
    from the real implementation (INVALID_TASK_IDS), never the router's
    ACTION_UNAVAILABLE. An empty id list is chosen because it is refused by
    the handler itself and therefore touches no state."""
    import asyncio

    from terminal_mcp import mcp_app
    server = mcp_app.build_mcp()
    result = asyncio.run(server.call_tool(
        "terminal_turn", {"action": "task_batch_status", "task_ids": []}))
    payload = result[1] if isinstance(result, tuple) else result
    text = str(payload)
    assert "ACTION_UNAVAILABLE" not in text, "the action reached no implementation"
    assert "INVALID_TASK_IDS" in text


def test_the_advertised_turn_schema_exposes_every_routed_argument():
    """A one-tool surface is only usable if the arguments the retired tools
    took are declared on terminal_turn itself."""
    import asyncio

    from terminal_mcp import mcp_app
    tools = {tool.name: tool for tool in asyncio.run(mcp_app.build_mcp().list_tools())}
    schema = tools["terminal_turn"].input_schema["properties"]
    for argument in ("action", "target", "targets", "text", "desired_states",
                     "resume_token", "agent_type", "working_directory",
                     "initial_prompt", "grant_mode", "binding", "node",
                     "title", "priority", "metadata", "request_key",
                     "task_id", "task_ids"):
        assert argument in schema, f"terminal_turn must declare {argument}"
