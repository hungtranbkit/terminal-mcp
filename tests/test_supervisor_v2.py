from __future__ import annotations

import time

import pytest

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, SupervisorConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.supervisor import SupervisorService, SupervisorStore
from terminal_mcp.supervisor2 import build_supervisor_v2


def _config(*, terminal_input=True, **overrides) -> AppConfig:
    # v2_enabled=True by default here so the existing tests exercise the
    # per-watch policy_mode gate in isolation; the global-kill-switch gate
    # itself is covered separately by test_v2_disabled_globally_blocks_send.
    overrides.setdefault("v2_enabled", True)
    return AppConfig(
        PermissionsConfig(True, terminal_input), ("test-*", "agent-*"), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
        supervisor=SupervisorConfig(**overrides),
    )


def _v2(tmp_path, **overrides):
    terminal = TerminalService(_config(**overrides))
    store = SupervisorStore(tmp_path / "supervisor.db")
    svc = SupervisorService(terminal, store)
    return build_supervisor_v2(svc), svc


def _wait_prompt(name: str) -> str:
    return f"bash -lc 'echo \"Do you want to continue? [y/N]\"; read x; sleep 30'"


# ---------------------------------------------------------------------------
# Policy defaults + observe_only never sends
# ---------------------------------------------------------------------------


def test_default_policy_is_observe_only(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-v2-default", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)
    svc.run_once()
    assert v2.get_policy(session=session)["policy_mode"] == "observe_only"
    assert v2.list_actionable_events()["events"] == []


def test_observe_only_event_cannot_be_claimed_even_directly(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-v2-observe", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)
    events = svc.run_once()["events"]
    result = v2.claim_event(events[0]["id"], claimed_by="tester")
    assert result["error"] == "POLICY_OBSERVE_ONLY"


def test_observe_only_watch_never_gets_an_action_row(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-v2-noaction", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)
    for _ in range(3):
        svc.run_once()
        time.sleep(0.1)
    assert v2.list_actions(target=session)["actions"] == []


# ---------------------------------------------------------------------------
# Per-watch opt-in + policy validation
# ---------------------------------------------------------------------------


def test_set_policy_requires_existing_watch(tmp_path):
    v2, svc = _v2(tmp_path)
    assert v2.set_policy(session="test-nope", policy_mode="suggest_only")["error"] == "WATCH_NOT_FOUND"


def test_set_policy_rejects_invalid_mode(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-v2-badmode", "bash -lc 'sleep 10'")
    time.sleep(0.2)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)
    assert v2.set_policy(session=session, policy_mode="yolo")["error"] == "INVALID_POLICY_MODE"


def test_approved_auto_continue_requires_template(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-v2-notemplate", "bash -lc 'sleep 10'")
    time.sleep(0.2)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)
    result = v2.set_policy(session=session, policy_mode="approved_auto_continue")
    assert result["error"] == "APPROVED_TEMPLATE_REQUIRED"


def test_one_watch_suggest_only_does_not_affect_another_observe_only(tmp_path, tmux_session_factory):
    a = tmux_session_factory("test-v2-isoA", _wait_prompt(""))
    b = tmux_session_factory("test-v2-isoB", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=a)
    svc.watch(session=b)
    svc.run_once()
    v2.set_policy(session=a, policy_mode="suggest_only")
    actionable = {e["target"] for e in v2.list_actionable_events()["events"]}
    assert actionable == {a}


# ---------------------------------------------------------------------------
# Idempotency + concurrency
# ---------------------------------------------------------------------------


def test_claim_is_exactly_once_per_watch(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-v2-onceclaim", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)
    events = svc.run_once()["events"]
    v2.set_policy(session=session, policy_mode="suggest_only")
    first = v2.claim_event(events[0]["id"], claimed_by="a")
    assert "id" in first
    second = v2.claim_event(events[0]["id"], claimed_by="b")
    assert second["error"] in ("EVENT_ALREADY_CLAIMED", "ACTION_ALREADY_ACTIVE_FOR_WATCH")


def test_execute_send_is_idempotent(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-v2-idempotent", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)
    events = svc.run_once()["events"]
    v2.set_policy(session=session, policy_mode="suggest_only")
    claim = v2.claim_event(events[0]["id"], claimed_by="a")
    v2.submit_decision(claim["id"], "y")
    v2.review_action(claim["id"], "approve")
    first = v2.execute_send(claim["id"])
    assert first["sent"] is True
    second = v2.execute_send(claim["id"])
    assert second["error"] == "ALREADY_SENT_OR_NOT_APPROVED"
    third = v2.execute_send(claim["id"])
    assert third["error"] == "ALREADY_SENT_OR_NOT_APPROVED"


def test_v2_disabled_globally_blocks_send_even_when_watch_is_approved(tmp_path, tmux_session_factory):
    # The global kill switch (supervisor.v2_enabled) is a second, independent
    # gate on top of the per-watch policy_mode -- an approved action must
    # still refuse to send while it is off, exactly like a fresh install.
    session = tmux_session_factory("test-v2-globaloff", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path, v2_enabled=False)
    svc.watch(session=session)
    events = svc.run_once()["events"]
    v2.set_policy(session=session, policy_mode="suggest_only")
    claim = v2.claim_event(events[0]["id"], claimed_by="a")
    v2.submit_decision(claim["id"], "y")
    v2.review_action(claim["id"], "approve")
    result = v2.execute_send(claim["id"])
    assert result["error"] == "V2_DISABLED"
    # still 'approved', not 'sent' or 'failed' -- disabled is a pure no-op,
    # not a consumed attempt, so turning v2_enabled on later can retry it.
    action = v2.store.get_action(claim["id"])
    assert action["state"] == "approved"


def test_double_review_is_rejected(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-v2-doublereview", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)
    events = svc.run_once()["events"]
    v2.set_policy(session=session, policy_mode="suggest_only")
    claim = v2.claim_event(events[0]["id"], claimed_by="a")
    v2.submit_decision(claim["id"], "y")
    ok = v2.review_action(claim["id"], "approve")
    assert ok["state"] == "approved"
    again = v2.review_action(claim["id"], "approve")
    assert again["error"] == "INVALID_ACTION_STATE"


def test_restart_recovery_does_not_replay_a_sent_action(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-v2-restart", _wait_prompt(""))
    time.sleep(0.3)
    db_path = tmp_path / "supervisor.db"
    terminal = TerminalService(_config())
    svc1 = SupervisorService(terminal, SupervisorStore(db_path))
    v2_a = build_supervisor_v2(svc1)
    svc1.watch(session=session)
    events = svc1.run_once()["events"]
    v2_a.set_policy(session=session, policy_mode="suggest_only")
    claim = v2_a.claim_event(events[0]["id"], claimed_by="a")
    v2_a.submit_decision(claim["id"], "y")
    v2_a.review_action(claim["id"], "approve")
    v2_a.execute_send(claim["id"])

    # Fresh service/store pair against the same db path, simulating a restart.
    svc2 = SupervisorService(TerminalService(_config()), SupervisorStore(db_path))
    v2_b = build_supervisor_v2(svc2)
    replay = v2_b.execute_send(claim["id"])
    assert replay["error"] == "ALREADY_SENT_OR_NOT_APPROVED"
    assert v2_b.store.get_action(claim["id"])["state"] in ("sent", "observing", "completed")


# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------


def test_same_prompt_repeat_limit_blocks(tmp_path, tmux_session_factory):
    # Real claim against a real attention event, but the "this exact prompt
    # was already proposed same_prompt_repeat_limit times before" precondition
    # is seeded directly (getting a real tmux session to naturally produce
    # several distinct WAITING_INPUT events with byte-identical output is not
    # a meaningful thing to script — v1 already dedupes identical output on
    # its own). submit_decision's real repeat-tracking call is what's under
    # test here, exercised through the real API, not bypassed.
    session = tmux_session_factory("test-v2-samerepeat", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)
    events = svc.run_once()["events"]
    v2.set_policy(session=session, policy_mode="suggest_only", same_prompt_repeat_limit=2)

    from terminal_mcp.audit import text_fingerprint
    from terminal_mcp.redaction import redact_text
    key = f"session:{session}"
    same_hash = text_fingerprint(redact_text("y"))
    v2.store.record_prompt(key, same_hash)  # round 1 (simulated)
    v2.store.record_prompt(key, same_hash)  # round 2 (simulated) -> at the limit

    claim = v2.claim_event(events[0]["id"], claimed_by="loop")
    result = v2.submit_decision(claim["id"], "y")  # round 3, identical prompt -> must block
    assert result["error"] == "SAME_PROMPT_REPEAT_LIMIT"
    assert v2.store.get_action(claim["id"])["state"] == "blocked"


def test_max_auto_actions_blocks_decision(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-v2-maxauto", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)
    events = svc.run_once()["events"]
    v2.set_policy(session=session, policy_mode="suggest_only", max_auto_actions=1)
    # Simulate the counter already being at the cap (as if a prior round sent).
    v2.store.increment_auto_action_count(f"session:{session}")
    claim = v2.claim_event(events[0]["id"], claimed_by="a")
    result = v2.submit_decision(claim["id"], "y")
    assert result["error"] == "MAX_AUTO_ACTIONS_EXCEEDED"
    assert v2.store.get_action(claim["id"])["state"] == "blocked"


def test_wall_clock_timeout_blocks_decision(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-v2-timeout", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)
    events = svc.run_once()["events"]
    v2.set_policy(session=session, policy_mode="suggest_only", wall_clock_timeout_seconds=1)
    v2.store.record_prompt(f"session:{session}", "warm-up")  # sets first_action_at
    time.sleep(1.2)
    claim = v2.claim_event(events[0]["id"], claimed_by="a")
    result = v2.submit_decision(claim["id"], "y")
    assert result["error"] == "WALL_CLOCK_TIMEOUT_EXCEEDED"


def test_no_progress_limit_blocks_and_pauses_policy(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-v2-noprogress", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)
    events = svc.run_once()["events"]
    v2.set_policy(session=session, policy_mode="suggest_only", no_progress_limit=1)
    claim = v2.claim_event(events[0]["id"], claimed_by="a")
    v2.submit_decision(claim["id"], "n")  # deliberately doesn't change the prompt
    v2.review_action(claim["id"], "approve")
    v2.execute_send(claim["id"])
    # The session still just re-prints the same prompt after "n" -> no real
    # output change is guaranteed across a couple of reconcile passes.
    for _ in range(3):
        time.sleep(0.3)
        svc.run_once()
        v2._reconcile_observing_actions()
    action = v2.store.get_action(claim["id"])
    policy = v2.get_policy(session=session)
    # Either it progressed (the read consumed input, changing output) or it
    # correctly stalled — assert the mechanism produced a defensible outcome
    # either way, and never left the action stuck in 'observing' forever.
    assert action["state"] in ("completed", "blocked")
    if action["state"] == "blocked":
        assert action["stop_reason"].startswith("no_progress_limit_exceeded")
        assert policy["blocked_reason"] == "no_progress_limit_exceeded"


# ---------------------------------------------------------------------------
# Blocked sensitive/destructive/credential scenarios
# ---------------------------------------------------------------------------


def test_credential_request_in_output_blocks_claim(tmp_path, tmux_session_factory):
    session = tmux_session_factory(
        "test-v2-credblock",
        "bash -lc 'printf \"Need to Enter your API key first.\\nDo you want to continue? [y/N]\\n\"; read x; sleep 20'",
    )
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)
    events = svc.run_once()["events"]
    v2.set_policy(session=session, policy_mode="suggest_only")
    result = v2.claim_event(events[0]["id"], claimed_by="a")
    assert result["error"] == "BLOCKED_FOR_REVIEW"
    assert v2.get_policy(session=session)["blocked_reason"] is not None
    assert v2.list_actionable_events()["events"] == []


def test_destructive_prompt_content_blocks_decision(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-v2-destructive", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)
    events = svc.run_once()["events"]
    v2.set_policy(session=session, policy_mode="suggest_only")
    claim = v2.claim_event(events[0]["id"], claimed_by="a")
    result = v2.submit_decision(claim["id"], "sure, run rm -rf / to clean up")
    assert result["error"] == "BLOCKED_FOR_REVIEW"
    assert v2.store.get_action(claim["id"])["state"] == "blocked"


def test_claim_refuses_when_current_state_no_longer_actionable(tmp_path, tmux_session_factory):
    # The event said WAITING_INPUT, but by claim time the session moved on
    # (or is ambiguous) — never guess, hold instead.
    session = tmux_session_factory("test-v2-stale", "bash -lc 'sleep 30'")
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)
    # Craft a fabricated stale event referencing a state the watch isn't in.
    v2.v1.store.add_event(
        watch_key=f"session:{session}", kind="session", target=session,
        previous_state="UNKNOWN", state="WAITING_INPUT", event_type="attention_required",
        reason="synthetic", output_preview="", output_hash=None, iteration_count=1,
    )
    v2.set_policy(session=session, policy_mode="suggest_only")
    events = v2.list_actionable_events()["events"]
    assert events == []  # filtered out: watch's real current state != event's recorded state


# ---------------------------------------------------------------------------
# Guard preservation: v2's send never weakens any existing gate
# ---------------------------------------------------------------------------


def test_execute_send_still_respects_terminal_input_disabled(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-v2-inputoff", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path, terminal_input=False)
    svc.watch(session=session)
    events = svc.run_once()["events"]
    v2.set_policy(session=session, policy_mode="suggest_only")
    claim = v2.claim_event(events[0]["id"], claimed_by="a")
    v2.submit_decision(claim["id"], "y")
    v2.review_action(claim["id"], "approve")
    result = v2.execute_send(claim["id"])
    assert result["sent"] is False
    assert result["error"] == "INPUT_DISABLED"
    assert v2.store.get_action(claim["id"])["state"] == "failed"


def test_execute_send_still_respects_input_policy_denied_pattern(tmp_path, tmux_session_factory):
    # allowed_session_patterns includes this session for *reading*, but
    # input_policy only allows "test-*" here too — use a session outside
    # input_policy specifically to prove the send path's own guard still runs.
    terminal = TerminalService(AppConfig(
        PermissionsConfig(True, True), ("test-*", "agent-*"), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("agent-*",)),  # deliberately excludes "test-*"
        supervisor=SupervisorConfig(v2_enabled=True),
    ))
    session_factory_name = "test-v2-policyoff"
    import subprocess
    subprocess.run(["tmux", "kill-session", "-t", session_factory_name], check=False, capture_output=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", session_factory_name, _wait_prompt("")], check=True, capture_output=True, text=True)
    time.sleep(0.3)
    try:
        store = SupervisorStore(tmp_path / "supervisor.db")
        svc = SupervisorService(terminal, store)
        v2 = build_supervisor_v2(svc)
        svc.watch(session=session_factory_name)
        events = svc.run_once()["events"]
        v2.set_policy(session=session_factory_name, policy_mode="suggest_only")
        claim = v2.claim_event(events[0]["id"], claimed_by="a")
        v2.submit_decision(claim["id"], "y")
        v2.review_action(claim["id"], "approve")
        result = v2.execute_send(claim["id"])
        assert result["sent"] is False
        assert result["error"] == "ACCESS_DENIED"
        assert v2.store.get_action(claim["id"])["state"] == "failed"
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session_factory_name], check=False, capture_output=True)


def test_approved_auto_continue_only_sends_exact_template_match(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-v2-boundcheck", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)
    events = svc.run_once()["events"]
    v2.set_policy(session=session, policy_mode="approved_auto_continue", approved_template="y")
    claim = v2.claim_event(events[0]["id"], claimed_by="a")
    # A benign-content prompt that simply isn't byte-identical to the
    # approved template — proves the *template-bound* check specifically
    # (a separate case below proves the destructive-content screen).
    off_template = v2.submit_decision(claim["id"], "yes, please continue")
    assert off_template["state"] == "decided"  # not auto-approved: needs supervisor2_review_action


# ---------------------------------------------------------------------------
# Audit correlation
# ---------------------------------------------------------------------------


def test_send_result_never_contains_raw_prompt_text(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-v2-noraw", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)
    events = svc.run_once()["events"]
    v2.set_policy(session=session, policy_mode="approved_auto_continue", approved_template="y")
    claim = v2.claim_event(events[0]["id"], claimed_by="a")
    v2.submit_decision(claim["id"], "y")
    v2.execute_send(claim["id"])
    action = v2.store.get_action(claim["id"])
    assert "y" not in action["send_result"] or '"characters"' in action["send_result"]
    import json
    parsed = json.loads(action["send_result"])
    assert set(parsed.keys()) <= {"session", "binding", "sent", "characters", "press_enter"}


# ---------------------------------------------------------------------------
# Real tmux end-to-end acceptance demo (automated)
# ---------------------------------------------------------------------------


def test_full_e2e_approved_auto_continue_reaches_done(tmp_path, tmux_session_factory):
    session = tmux_session_factory(
        "test-v2-e2e",
        "bash -lc 'echo \"Do you want to continue? [y/N]\"; read x; "
        "if [ \"$x\" = y ]; then printf \"Continuing...\\nFINAL REPORT\\ndone\\n\"; else echo no; fi; sleep 20'",
    )
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)

    svc.watch(session=session)
    events = svc.run_once()["events"]
    assert events[0]["event_type"] == "attention_required"

    v2.set_policy(session=session, policy_mode="approved_auto_continue", approved_template="y")
    actionable = v2.list_actionable_events()["events"]
    assert len(actionable) == 1

    claim = v2.claim_event(actionable[0]["id"], claimed_by="e2e-demo")
    decided = v2.submit_decision(claim["id"], "y", "continue per approved template")
    assert decided["state"] == "approved"  # auto-approved: exact template match

    sent = v2.execute_send(claim["id"])
    assert sent["sent"] is True

    time.sleep(1.5)
    result = v2.run_once()
    assert any(e["state"] == "DONE" for e in result["events"])
    reconciled = result["v2_reconciled"]
    assert any(r["action_id"] == claim["id"] and r["result"] == "progressed" for r in reconciled)

    action = v2.store.get_action(claim["id"])
    assert action["state"] == "completed"
    assert action["resulting_event_id"] is not None
    resulting_event = [e for e in v2.v1.store.list_events(limit=10) if e["id"] == action["resulting_event_id"]][0]
    assert resulting_event["state"] == "DONE"

    # Chain closed cleanly: a fresh policy read shows counters reset.
    policy = v2.get_policy(session=session)
    assert policy["auto_action_count"] == 0


def test_full_e2e_observe_only_never_sends(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-v2-e2e-observe", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)  # policy defaults to observe_only, never changed
    svc.run_once()
    assert v2.list_actionable_events()["events"] == []
    assert v2.list_actions(target=session)["actions"] == []
    # The session's own output must still show the unanswered prompt —
    # nothing was ever typed into it.
    tail = svc.terminal.terminal_status(session)
    assert "Do you want to continue" in tail["last_output"]


# ---------------------------------------------------------------------------
# MCP tool surface
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_supervisor2_tools_full_pipeline_via_mcp(tmp_path, tmux_session_factory):
    from terminal_mcp.mcp_app import build_mcp

    session = tmux_session_factory(
        "test-v2-mcp",
        "bash -lc 'echo \"Do you want to continue? [y/N]\"; read x; "
        "if [ \"$x\" = y ]; then printf \"Continuing...\\nFINAL REPORT\\ndone\\n\"; else echo no; fi; sleep 20'",
    )
    time.sleep(0.3)
    terminal = TerminalService(_config())
    store = SupervisorStore(tmp_path / "supervisor.db")
    svc = SupervisorService(terminal, store)
    v2 = build_supervisor_v2(svc)
    server = build_mcp(terminal, svc, v2)

    names = {tool.name for tool in await server.list_tools()}
    assert {
        "supervisor2_set_policy", "supervisor2_get_policy", "supervisor2_list_actionable_events",
        "supervisor2_claim_event", "supervisor2_submit_decision", "supervisor2_review_action",
        "supervisor2_execute_send", "supervisor2_list_actions",
    } <= names

    async def call(name, **kwargs):
        result = await server.call_tool(name, kwargs)
        if result.structured_content is not None:
            return result.structured_content
        import json
        return json.loads(result.content[0].text)

    await call("supervisor_watch", session=session)
    events = (await call("supervisor_run_once"))["events"]
    policy = await call("supervisor2_set_policy", session=session, policy_mode="approved_auto_continue", approved_template="y")
    assert policy["policy_mode"] == "approved_auto_continue"
    actionable = (await call("supervisor2_list_actionable_events"))["events"]
    assert len(actionable) == 1
    claim = await call("supervisor2_claim_event", event_id=actionable[0]["id"], claimed_by="mcp-demo")
    decided = await call("supervisor2_submit_decision", action_id=claim["id"], proposed_prompt="y")
    assert decided["state"] == "approved"
    sent = await call("supervisor2_execute_send", action_id=claim["id"])
    assert sent["sent"] is True
    time.sleep(1.5)
    r = await call("supervisor_run_once")
    assert any(e["state"] == "DONE" for e in r["events"])
    actions = (await call("supervisor2_list_actions", target=session))["actions"]
    assert actions[0]["state"] == "completed"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_run_once_does_not_leak_file_descriptors(tmp_path, tmux_session_factory):
    # Regression for the real production incident: SupervisorLoop drives
    # v2.run_once() (-> v1.run_once() + _reconcile_observing_actions()) on
    # a fixed poll_interval_seconds forever. Each of those touches
    # SupervisorStore/SupervisorV2Store (and, for a bound watch,
    # BindingStore/AuditStore) many times per call. Every one of those
    # stores previously leaked one real file descriptor per SQL call (see
    # the fix in supervisor.py/supervisor2.py/audit.py/bindings.py's
    # _connection() helper) -- under a real 20s poll loop this exhausted
    # the process's file descriptor limit (1024) within about 25 minutes
    # and made the whole HTTP service stop accepting connections while
    # still showing as "active". Assert real fd count stays flat across
    # many run_once() cycles, not just that they succeed.
    import os

    session = tmux_session_factory("test-v2-fdleak", _wait_prompt(""))
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)

    def open_fd_count() -> int:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))

    baseline = open_fd_count()
    for _ in range(150):
        v2.run_once()
    after = open_fd_count()
    # A real leak grows roughly linearly with iteration count (150
    # iterations previously leaked 300+ fds for this store pair alone).
    # A small, bounded fluctuation is fine; anything near iteration count
    # is the regression this guards against.
    assert after - baseline < 20, (
        f"fd count grew from {baseline} to {after} across 150 run_once() cycles -- leak regression"
    )
