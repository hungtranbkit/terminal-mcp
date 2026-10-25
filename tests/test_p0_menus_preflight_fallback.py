"""P0 #95 / #80 / #93: menus that hung sessions, bare-name preflight timeouts,
and the single caller-named fallback dispatch.

Fixtures: `claude_autocompact_prompt_80col.txt` is a live dell capture
(2026-10-26; the other session's conversation above the dialog trimmed).
The `*.synth.txt` fixtures are rebuilt from the strings in the codex-cli
0.162 binary ("Codex just got an upgrade. Introducing", "Try new model",
"Use existing model", " for lower credit usage?", "Keep current model") and
Claude Code's usage-limit menu; their layout is approximate, their wording
is not.
"""
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from terminal_mcp import adapters
from terminal_mcp.audit import AuditStore
from terminal_mcp.compact_tools import _fallback_safe
from terminal_mcp.controller import ControllerService
from terminal_mcp.status import classify_status, detect_waiting_input
from tests.test_codex_startup_navigation import MenuBackend, configured, launch
from tests.test_compact_tools import service

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


# -- menus are named, never answered ------------------------------------------

@pytest.mark.parametrize("name,kind", [
    ("claude_autocompact_prompt_80col.txt", "CLAUDE_AUTOCOMPACT_PROMPT"),
    ("claude_rate_limit_menu_80col.synth.txt", "CLAUDE_RATE_LIMIT_MENU"),
    ("codex_0162_model_migration_80col.synth.txt", "CODEX_MODEL_MIGRATION"),
    ("codex_0162_rate_limit_switch_80col.synth.txt", "CODEX_RATE_LIMIT_SWITCH"),
    ("codex_0162_update_menu_80col.txt", "CODEX_UPDATE_MENU"),
])
def test_seen_menus_are_named_with_an_owner_action(name, kind):
    prompt = adapters.classify_input_prompt(fixture(name).splitlines())
    assert prompt["kind"] == kind
    assert prompt["next_action"] == adapters.INPUT_PROMPT_ACTIONS[kind]
    waiting, reason = detect_waiting_input(fixture(name))
    assert waiting and reason.startswith(kind)


def test_live_claude_autocompact_dialog_no_longer_reads_running():
    # Before: no detector matched, and a claude pane redrawn within 60s fell
    # through to the activity timer -> RUNNING, so dispatch waited silently.
    info = SimpleNamespace(pane_current_command="claude", pane_dead=False,
                           activity_epoch=int(time.time()))
    state, waiting, reason = classify_status(info, fixture("claude_autocompact_prompt_80col.txt"))
    assert (state, waiting) == ("WAITING_INPUT", True)
    assert reason.startswith("CLAUDE_AUTOCOMPACT_PROMPT")


def test_menu_words_in_old_scrollback_do_not_block_a_fresh_composer():
    old = fixture("codex_0162_rate_limit_switch_80col.synth.txt").splitlines()
    fresh = old + ["• Done."] + [f"line {n}" for n in range(20)] + ["› Ask Codex to do anything"]
    assert adapters.classify_input_prompt(fresh) is None


def test_usage_limit_notice_is_bottom_only():
    notice = ["■ You've hit your usage limit. Upgrade to Pro, or try again at 3:04 PM.",
              "", "› Ask Codex to do anything", "  gpt-6 · 40% left"]
    assert adapters.usage_limit_notice(notice).startswith("You've hit your usage limit")
    lifted = notice + [f"• worked line {n}" for n in range(12)] + ["› Ask Codex to do anything"]
    assert adapters.usage_limit_notice(lifted) is None
    assert adapters.usage_limit_notice(["The docs say: you've hit your usage limit when..."]) is None


# -- startup: skip update automatically, never the model/rate-limit choice ----

class OneShotUpdateBackend(MenuBackend):
    """Update menu that may survive Escape (a CLI that re-asks)."""

    def __init__(self, sticky):
        super().__init__("update", 0)
        self.sticky = sticky

    def capture_lines(self, *args, **kwargs):
        if self.ready:
            return ["› Ask Codex to do anything", "codex default"]
        return fixture("codex_0162_update_menu_80col.txt").splitlines()

    def send_keys(self, name, keys):
        assert keys == ["Escape"], "only Skip (Escape) is ever sent to the update menu"
        self.keys.extend(keys)
        self.ready = not self.sticky


def no_general_input(tmp_path):
    config = configured(tmp_path)
    return replace(config,
                   permissions=replace(config.permissions, terminal_input=False, allow_send_keys=False),
                   input_policy=replace(config.input_policy, allow_keys=()),
                   session_access=replace(config.session_access, default_input=False))


def test_update_menu_is_skipped_without_a_general_input_grant(tmp_path, monkeypatch):
    backend = OneShotUpdateBackend(sticky=False)
    result = launch(tmp_path, monkeypatch, backend, no_general_input(tmp_path))
    assert result["state"] == "READY"
    assert backend.keys == ["Escape"]


def test_update_menu_that_survives_skip_is_reported_not_retried(tmp_path, monkeypatch):
    backend = OneShotUpdateBackend(sticky=True)
    result = launch(tmp_path, monkeypatch, backend, no_general_input(tmp_path))
    assert result["state"] == "BLOCKED"
    assert result["blocked_reason"] == "CODEX_UPDATE_MENU"
    assert "check_for_update_on_startup" in result["next_action"]
    assert backend.keys == ["Escape"]


def test_update_skip_still_respects_the_session_deny_floor(tmp_path, monkeypatch):
    config = configured(tmp_path)
    config = replace(config, input_policy=replace(config.input_policy,
                                                  denied_session_patterns=("codex-nav-*",)))
    backend = OneShotUpdateBackend(sticky=False)
    assert launch(tmp_path, monkeypatch, backend, config)["state"] == "BLOCKED"
    assert backend.keys == []


@pytest.mark.parametrize("name,reason", [
    ("codex_0162_model_migration_80col.synth.txt", "CODEX_MODEL_MIGRATION"),
    ("codex_0162_rate_limit_switch_80col.synth.txt", "CODEX_RATE_LIMIT_SWITCH"),
])
def test_model_and_rate_limit_menus_block_startup_without_keys(tmp_path, monkeypatch, name, reason):
    class Backend(MenuBackend):
        def capture_lines(self, *args, **kwargs):
            return fixture(name).splitlines()

    backend = Backend("menu", 0)
    result = launch(tmp_path, monkeypatch, backend)  # even WITH a general input grant
    assert result["state"] == "BLOCKED"
    assert result["blocked_reason"] == reason
    assert "never" in result["next_action"]
    assert backend.keys == []


# -- direct start preflight ---------------------------------------------------

IDLE = {"state": "IDLE", "agent_type": "codex", "exists": True}
RUNNING = {"state": "RUNNING", "agent_type": "codex", "exists": True}
CONFIRMED = {"delivery_state": "SUBMIT_CONFIRMED", "enter_sent": True, "agent_type": "codex",
             "enter_count": 1, "submission_id": "sub-1"}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_ENABLE_QUEUE", "1")
    tools, terminal, controller = service(tmp_path / "journal.db")
    terminal.audit = AuditStore(tmp_path / "audit.db")
    clock = [0.0]
    tools.monotonic = lambda: clock[0]
    tools.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    tools.handlers["enqueue_task"] = lambda *a, **kw: pytest.fail("direct start must never enqueue")
    tools.handlers["legacy_task_by_request_key"] = lambda key: None
    sends = {}

    def send(session, text, press_enter, dry_run, **kwargs):
        controller.send_calls.append((session, text, press_enter, dry_run, kwargs))
        return {"session": session, **sends.get(session, CONFIRMED)}

    controller.terminal_send_text = send
    controller.sends = sends
    return tools, controller


def start(tools, target="dell-linux/codex-a", key="req-1", **kwargs):
    return tools.turn(action="start", target=target, text="inert task", timeout=10,
                      request_key=key, **kwargs)


def test_bare_name_is_resolved_before_the_status_slice(rig):
    tools, controller = rig
    asked = []
    controller.resolve_session = lambda name, timeout_seconds=None: (
        asked.append(timeout_seconds) or {"node_id": "dell-linux", "session": name})
    controller.statuses["dell-linux/codex-a"] = [IDLE, RUNNING]
    result = start(tools, target="codex-a")
    assert result["status"] == "TASK_STARTED"
    assert result["resolved_target"] == "dell-linux/codex-a"
    assert [call[0] for call in controller.send_calls] == ["dell-linux/codex-a"]
    assert asked and asked[0] > 2.0, "resolution gets its own budget, not the 2s poll slice"


def test_resolution_timeout_is_reported_and_nothing_is_sent(rig):
    tools, controller = rig
    controller.resolve_session = lambda name, timeout_seconds=None: {
        "error": "RESOLUTION_TIMEOUT", "session": name}
    result = start(tools, target="codex-a")
    assert (result["status"], result["error"], result["dispatched"]) == (
        "FAILED", "STATUS_PROBE_TIMEOUT", False)
    assert result["hop"] == "controller resolution"
    assert controller.send_calls == []


def test_preflight_status_read_is_not_limited_to_the_poll_slice(rig):
    tools, controller = rig
    controller.statuses["dell-linux/codex-a"] = [IDLE, RUNNING]
    start(tools)
    assert controller.bounded_status_timeouts[0] > 2.0


@pytest.mark.parametrize("status,task_state,reason", [
    ({**IDLE, "state": "WAITING_INPUT", "input_required": True,
      "input_prompt": {"kind": "CODEX_RATE_LIMIT_SWITCH", "detail": "x",
                       "next_action": adapters.INPUT_PROMPT_ACTIONS["CODEX_RATE_LIMIT_SWITCH"]}},
     "WAITING_INPUT", "CODEX_RATE_LIMIT_SWITCH"),
    ({**IDLE, "blocked_reason": "AGENT_USAGE_LIMIT",
      "usage_limit": {"notice": "You've hit your usage limit.", "next_action": "elsewhere"}},
     "BLOCKED", "AGENT_USAGE_LIMIT"),
    (RUNNING, "RUNNING", "TARGET_BUSY"),
])
def test_not_idle_targets_say_why_with_one_action(rig, status, task_state, reason):
    tools, controller = rig
    controller.statuses["dell-linux/codex-a"] = status
    result = start(tools)
    assert result["error"] == "DIRECT_TARGET_NOT_IDLE"
    assert (result["task_state"], result["blocked_reason"]) == (task_state, reason)
    assert result["next_action"]
    assert controller.send_calls == []


# -- one caller-named fallback ------------------------------------------------

def test_one_fallback_when_primary_typed_nothing(rig):
    tools, controller = rig
    controller.statuses["dell-linux/codex-a"] = {**IDLE, "blocked_reason": "AGENT_USAGE_LIMIT"}
    controller.statuses["nec1/claude-b"] = [{**IDLE, "agent_type": "claude"},
                                            {**RUNNING, "agent_type": "claude"}]
    result = start(tools, args={"fallback_target": "nec1/claude-b"})
    assert result["status"] == "TASK_STARTED"
    assert result["fallback"]["primary_error"] == "DIRECT_TARGET_NOT_IDLE"
    assert result["primary"]["dispatched"] is False
    assert [call[0] for call in controller.send_calls] == ["nec1/claude-b"]
    # Replay with the same key returns the stored outcomes: no second send.
    assert start(tools, args={"fallback_target": "nec1/claude-b"})["status"] == "TASK_STARTED"
    assert len(controller.send_calls) == 1


def test_submit_confirmed_but_not_running_is_never_fallen_back_or_completed(rig):
    tools, controller = rig
    controller.statuses["dell-linux/codex-a"] = IDLE  # accepted, never seen RUNNING
    controller.statuses["nec1/codex-b"] = [IDLE, RUNNING]
    result = start(tools, args={"fallback_target": "nec1/codex-b"})
    assert result["error"] == "DIRECT_START_NOT_RUNNING"
    assert result["submitted"] is True and result["status"] != "DONE"
    assert "fallback" not in result
    assert [call[0] for call in controller.send_calls] == ["dell-linux/codex-a"]


def test_guard_refusal_before_typing_allows_fallback_but_typed_text_does_not(rig):
    tools, controller = rig
    controller.statuses["dell-linux/codex-a"] = IDLE
    controller.statuses["nec1/codex-b"] = [IDLE, RUNNING]
    controller.sends["dell-linux/codex-a"] = {"error": "IDLE_GUARD_UNSUPPORTED", "enter_sent": False,
                                              "reason": "IDLE_GUARD_UNSUPPORTED"}
    result = start(tools, args={"fallback_target": "nec1/codex-b"})
    assert result["status"] == "TASK_STARTED" and result["fallback"]["attempted"]
    typed = {"error": "DIRECT_SUBMIT_NOT_ACCEPTED", "dispatched": False,
             "send": {"reason": "SUBMIT_UNCONFIRMED", "evidence": {"enter_sent": False}}}
    assert _fallback_safe(typed) is False


def test_fallback_runs_at_most_once_and_reports_both_blockers(rig):
    tools, controller = rig
    controller.statuses["dell-linux/codex-a"] = RUNNING
    controller.statuses["nec1/codex-b"] = {**IDLE, "state": "WAITING_INPUT", "input_required": True}
    result = start(tools, args={"fallback_target": "nec1/codex-b"})
    assert result["error"] == "DIRECT_TARGET_NOT_IDLE" and result["task_state"] == "RUNNING"
    assert result["fallback"]["task_state"] == "WAITING_INPUT"
    assert controller.send_calls == []


def test_fallback_to_the_same_target_is_refused(rig):
    tools, controller = rig
    controller.statuses["dell-linux/codex-a"] = RUNNING
    result = start(tools, args={"fallback_target": "dell-linux/codex-a"})
    assert result["fallback"]["attempted"] is False
    assert controller.send_calls == []


# -- controller: bounded resolution asks nodes in parallel -------------------

def test_bounded_resolution_is_not_serialised_behind_a_slow_node():
    controller = ControllerService.__new__(ControllerService)
    release = threading.Event()

    class Client:
        def __init__(self, names, slow=False):
            self.names, self.slow = names, slow

        def list_sessions(self, timeout_seconds=None):
            if self.slow:
                release.wait(timeout_seconds)
            return {"sessions": [{"name": name} for name in self.names]}

    nodes = [SimpleNamespace(id=f"slow{n}", status="online") for n in range(3)]
    nodes.append(SimpleNamespace(id="dell-linux", status="online"))
    controller._clients = {f"slow{n}": Client([], slow=True) for n in range(3)}
    controller._clients["dell-linux"] = Client(["codex-a"])
    controller._session_location_cache = {}
    controller._rename_aliases = {}
    controller.session_cache_ttl_seconds = 30
    controller.list_nodes = lambda: nodes
    controller._reconcile_ownership = lambda node_id, listing: None
    try:
        began = time.monotonic()
        result = controller.resolve_session("codex-a", timeout_seconds=1.0)
        elapsed = time.monotonic() - began
    finally:
        release.set()
    assert elapsed < 2.0, "three slow nodes must not be waited for one after another"
    # Serially, the first slow node used the whole slice -> RESOLUTION_TIMEOUT.
    assert result == {"node_id": "dell-linux", "session": "codex-a"}
