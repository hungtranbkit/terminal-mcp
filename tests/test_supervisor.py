from __future__ import annotations

import time

import pytest

from terminal_mcp.config import AppConfig, PermissionsConfig, SupervisorConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.supervisor import SupervisorService, SupervisorStore, watch_key


def _config(**overrides) -> AppConfig:
    supervisor = SupervisorConfig(**overrides)
    return AppConfig(PermissionsConfig(True, False), ("test-*", "agent-*"), 50, 20, supervisor=supervisor)


def _service(tmp_path, **supervisor_overrides) -> SupervisorService:
    terminal = TerminalService(_config(**supervisor_overrides))
    store = SupervisorStore(tmp_path / "supervisor.db")
    return SupervisorService(terminal, store)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_supervisor_config_defaults():
    cfg = SupervisorConfig()
    assert cfg.enabled is False
    assert cfg.poll_interval_seconds == 20
    assert cfg.idle_threshold_seconds == 45
    assert cfg.max_iterations == 20
    assert cfg.same_failure_limit == 2


def test_load_config_rejects_poll_interval_under_5_seconds(tmp_path):
    from terminal_mcp.config import load_config

    bad = tmp_path / "config.yaml"
    bad.write_text(
        "allowed_session_patterns: ['test-*']\nsupervisor:\n  enabled: true\n  poll_interval_seconds: 2\n"
    )
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        load_config(bad)


def test_load_config_parses_supervisor_block(tmp_path):
    from terminal_mcp.config import load_config

    good = tmp_path / "config.yaml"
    good.write_text(
        "allowed_session_patterns: ['test-*']\n"
        "supervisor:\n"
        "  enabled: true\n"
        "  poll_interval_seconds: 30\n"
        "  idle_threshold_seconds: 60\n"
        "  max_iterations: 5\n"
        "  same_failure_limit: 3\n"
        "  event_retention: 200\n"
        "  watched_session_patterns: ['claude-*']\n"
        "  watched_bindings: ['mesflow-dev']\n"
    )
    cfg = load_config(good)
    assert cfg.supervisor.enabled is True
    assert cfg.supervisor.poll_interval_seconds == 30
    assert cfg.supervisor.watched_session_patterns == ("claude-*",)
    assert cfg.supervisor.watched_bindings == ("mesflow-dev",)


def test_load_config_supervisor_disabled_by_default(tmp_path):
    from terminal_mcp.config import load_config

    minimal = tmp_path / "config.yaml"
    minimal.write_text("allowed_session_patterns: ['test-*']\n")
    assert load_config(minimal).supervisor.enabled is False


# ---------------------------------------------------------------------------
# Security: denied session guard, whitelist
# ---------------------------------------------------------------------------


def test_watch_refuses_denied_session(tmp_path):
    svc = _service(tmp_path)
    result = svc.watch(session="private-not-allowed")
    assert result["error"] == "ACCESS_DENIED"
    assert svc.list_watches()["watches"] == []


def test_watch_refuses_unknown_binding(tmp_path):
    svc = _service(tmp_path)
    result = svc.watch(binding="does-not-exist")
    assert result["error"] == "BINDING_NOT_FOUND"


def test_watch_requires_exactly_one_target(tmp_path):
    svc = _service(tmp_path)
    assert svc.watch()["error"] == "EXACTLY_ONE_TARGET_REQUIRED"
    assert svc.watch(binding="a", session="b")["error"] == "EXACTLY_ONE_TARGET_REQUIRED"
    assert svc.unwatch()["error"] == "EXACTLY_ONE_TARGET_REQUIRED"


def test_run_once_never_polls_a_denied_session_even_if_db_is_tampered(tmp_path):
    # Defense in depth: even if a watch row for a denied session existed
    # (should be impossible via watch()), _poll_one goes through
    # terminal_status(), which re-checks the whitelist independently.
    svc = _service(tmp_path)
    svc.store.upsert_watch("session", "private-not-allowed", source="manual")
    result = svc._poll_one(svc.store.get_watch(watch_key("session", "private-not-allowed")))
    assert result["event_type"] == "watch_target_missing"
    assert "ACCESS_DENIED" in result["reason"]


# ---------------------------------------------------------------------------
# Real-tmux state machine + event queue behavior
# ---------------------------------------------------------------------------


def test_transition_running_to_waiting_input_emits_attention_event(tmp_path, tmux_session_factory):
    session = tmux_session_factory(
        "test-sup-wait", "bash -lc 'echo \"Do you want to continue? [y/N]\"; read x; sleep 20'"
    )
    time.sleep(0.3)
    svc = _service(tmp_path)
    svc.watch(session=session)

    first = svc.run_once()
    assert len(first["events"]) == 1
    event = first["events"][0]
    assert event["state"] == "WAITING_INPUT"
    assert event["event_type"] == "attention_required"
    assert event["previous_state"] == "UNKNOWN"
    assert "hi" not in event  # sanity: no stray extra keys leaking

    # Dedupe: identical state/output on the next poll emits nothing.
    second = svc.run_once()
    assert second["events"] == []


def test_transition_to_done_on_explicit_completion_marker(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-sup-done", "bash -lc 'printf \"FINAL REPORT\\nall good\\n\"; sleep 20'")
    time.sleep(0.3)
    svc = _service(tmp_path)
    svc.watch(session=session)
    result = svc.run_once()
    assert result["events"][0]["state"] == "DONE"
    assert result["events"][0]["event_type"] == "completed"


def test_ordinary_silence_never_produces_false_done(tmp_path, tmux_session_factory):
    # A quiet, unremarkable idle shell must never be classified DONE just
    # because nothing is happening — only explicit completion evidence does.
    session = tmux_session_factory("test-sup-quiet", "bash -lc 'sleep 20'")
    time.sleep(0.3)
    svc = _service(tmp_path, idle_threshold_seconds=3600)  # long threshold: won't tip into IDLE during this test
    svc.watch(session=session)
    result = svc.run_once()
    for event in result["events"]:
        assert event["state"] != "DONE"
    watch = svc.list_watches()["watches"][0]
    assert watch["state"] in ("UNKNOWN", "RUNNING", "IDLE")


def test_idle_threshold_marks_quiet_running_session_idle(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-sup-idle", "bash -lc 'sleep 20'")
    time.sleep(0.3)
    svc = _service(tmp_path, idle_threshold_seconds=1)
    svc.watch(session=session)
    svc.run_once()
    time.sleep(1.2)
    result = svc.run_once()
    watch = svc.list_watches()["watches"][0]
    assert watch["state"] == "IDLE"


def test_error_detection_from_real_traceback(tmp_path, tmux_session_factory):
    session = tmux_session_factory(
        "test-sup-traceback",
        "bash -lc 'printf \"Traceback (most recent call last):\\n  File x.py\\nValueError: bad\\n\"; sleep 20'",
    )
    time.sleep(0.3)
    svc = _service(tmp_path)
    svc.watch(session=session)
    result = svc.run_once()
    assert result["events"][0]["state"] == "ERROR"
    assert result["events"][0]["event_type"] == "error_detected"


def test_same_failure_limit_stops_watch_and_emits_stalled(tmp_path, tmux_session_factory):
    session = tmux_session_factory(
        "test-sup-repeat-err",
        "bash -lc 'printf \"Traceback (most recent call last):\\nValueError: x\\n\"; sleep 60'",
    )
    time.sleep(0.3)
    svc = _service(tmp_path, same_failure_limit=2)
    svc.watch(session=session)

    events_by_poll = [svc.run_once()["events"] for _ in range(4)]
    types = [e[0]["event_type"] if e else None for e in events_by_poll]
    assert types == ["error_detected", None, "stalled", None]
    watch = svc.list_watches()["watches"][0]
    assert watch["enabled"] is False
    assert watch["disabled_reason"] == "same_failure_limit_exceeded"


def test_max_iterations_stops_watch_and_emits_stalled(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-sup-maxiter", "bash -lc 'sleep 60'")
    time.sleep(0.3)
    svc = _service(tmp_path, max_iterations=2, idle_threshold_seconds=3600)
    svc.watch(session=session)

    svc.run_once()
    result = svc.run_once()
    watch = svc.list_watches()["watches"][0]
    assert watch["iteration_count"] == 2
    assert watch["enabled"] is False
    assert watch["disabled_reason"] == "max_iterations_exceeded"
    assert any(e["event_type"] == "stalled" for e in result["events"])


def test_max_iterations_does_not_stop_an_actively_progressing_watch(tmp_path, tmux_session_factory):
    # Reliability cleanup: a watch whose output keeps changing (real
    # ongoing work) must not be disabled merely because the raw poll count
    # is high -- only once it *also* goes quiet.
    session = tmux_session_factory(
        "test-sup-maxiter-progress",
        "bash -lc 'for i in $(seq 1 30); do echo step-$i; sleep 0.3; done; sleep 60'",
    )
    time.sleep(0.2)
    svc = _service(tmp_path, max_iterations=2, idle_threshold_seconds=3600)
    svc.watch(session=session)

    # Poll well past max_iterations=2 while the fixture is still actively
    # printing new lines -- must stay enabled the whole time.
    for _ in range(4):
        svc.run_once()
        time.sleep(0.3)
    watch = svc.list_watches()["watches"][0]
    assert watch["enabled"] is True
    assert watch["iteration_count"] > 2  # raw poll count really did exceed the ceiling
    assert watch["disabled_reason"] is None

    # Once it goes quiet (the loop finishes, falls into the trailing sleep
    # 60), the next poll(s) with no new output do stop it.
    for _ in range(3):
        svc.run_once()
        result = svc.run_once()
    watch = svc.list_watches()["watches"][0]
    assert watch["enabled"] is False
    assert watch["disabled_reason"] == "max_iterations_exceeded"


def test_missing_session_emits_watch_target_missing_and_disables(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-sup-vanish", "bash -lc 'sleep 3'")
    time.sleep(0.2)
    svc = _service(tmp_path)
    svc.watch(session=session)
    svc.run_once()  # first poll while it exists

    import subprocess
    subprocess.run(["tmux", "kill-session", "-t", session], check=True, capture_output=True, text=True, timeout=10)

    result = svc.run_once()
    assert result["events"][0]["event_type"] == "watch_target_missing"
    watch = svc.list_watches()["watches"][0]
    assert watch["enabled"] is False
    assert watch["disabled_reason"] == "target_missing"


def test_unwatch_then_rewatch_resumes_disabled_watch(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-sup-resume", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    svc = _service(tmp_path)
    svc.watch(session=session)
    svc.unwatch(session=session)
    assert svc.list_watches()["watches"][0]["enabled"] is False

    svc.watch(session=session)  # explicit resume
    assert svc.list_watches()["watches"][0]["enabled"] is True

    svc.unwatch(session=session, delete=True)
    assert svc.list_watches()["watches"] == []


# ---------------------------------------------------------------------------
# Redaction before persistence
# ---------------------------------------------------------------------------


def test_redaction_before_persistence(tmp_path, tmux_session_factory):
    session = tmux_session_factory(
        "test-sup-apikey",  # avoid "secret"/"password"/etc. — those trigger a separate exact-whitelist-only rule
        "bash -lc 'printf \"OPENAI_API_KEY=sk-livesecretvalue1234567890\\nDo you want to continue? [y/N]\\n\"; read x; sleep 20'",
    )
    time.sleep(0.3)
    svc = _service(tmp_path)
    svc.watch(session=session)
    result = svc.run_once()
    event = result["events"][0]
    assert "sk-livesecretvalue1234567890" not in event["output_preview"]
    assert "<REDACTED>" in event["output_preview"]
    # And the persisted row on disk carries the same redacted preview, not
    # raw output — reread through a fresh store handle at the same path.
    reopened = SupervisorStore(svc.store.path)
    stored = reopened.list_events(limit=1)[0]
    assert "sk-livesecretvalue1234567890" not in stored["output_preview"]


# ---------------------------------------------------------------------------
# Acknowledge flow + persistence across "restart"
# ---------------------------------------------------------------------------


def test_ack_event_flow(tmp_path, tmux_session_factory):
    session = tmux_session_factory(
        "test-sup-ack", "bash -lc 'echo \"Do you want to continue? [y/N]\"; read x; sleep 20'"
    )
    time.sleep(0.3)
    svc = _service(tmp_path)
    svc.watch(session=session)
    event = svc.run_once()["events"][0]
    assert event["acknowledged_at"] is None

    assert svc.ack_event(999999)["error"] == "EVENT_NOT_FOUND_OR_ALREADY_ACKNOWLEDGED"

    acked = svc.ack_event(event["id"])
    assert acked["acknowledged"] is True
    assert acked["event"]["acknowledged_at"] is not None

    # Acking twice is a no-op error, not a crash or a second timestamp.
    assert svc.ack_event(event["id"])["error"] == "EVENT_NOT_FOUND_OR_ALREADY_ACKNOWLEDGED"

    unacked = svc.list_events(unacknowledged_only=True)["events"]
    assert all(e["id"] != event["id"] for e in unacked)


def test_events_and_watches_persist_across_a_fresh_store_handle(tmp_path, tmux_session_factory):
    # Simulates a service restart: a brand new SupervisorStore/Service pair
    # opened against the same db path must see everything the first one wrote.
    session = tmux_session_factory(
        "test-sup-persist", "bash -lc 'echo \"Do you want to continue? [y/N]\"; read x; sleep 20'"
    )
    time.sleep(0.3)
    db_path = tmp_path / "supervisor.db"
    terminal = TerminalService(_config())
    svc1 = SupervisorService(terminal, SupervisorStore(db_path))
    svc1.watch(session=session)
    svc1.run_once()

    svc2 = SupervisorService(TerminalService(_config()), SupervisorStore(db_path))
    assert svc2.list_watches()["watches"][0]["target"] == session
    assert len(svc2.list_events()["events"]) == 1


def test_event_retention_pruning(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-sup-retain", "bash -lc 'sleep 30'")
    time.sleep(0.2)
    svc = _service(tmp_path, event_retention=2, idle_threshold_seconds=1)
    svc.watch(session=session)
    for _ in range(3):
        svc.run_once()
        time.sleep(1.1)
    events = svc.store.list_events(limit=100)
    assert len(events) <= 2


# ---------------------------------------------------------------------------
# Config-driven watch discovery (patterns / bindings)
# ---------------------------------------------------------------------------


def test_config_watched_session_pattern_is_auto_discovered(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-sup-pattern", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    svc = _service(tmp_path, watched_session_patterns=("test-sup-pattern*",))
    svc.run_once()
    watches = svc.list_watches()["watches"]
    assert any(w["target"] == session and w["source"] == "config_pattern" for w in watches)


def test_config_watched_binding_is_auto_discovered(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-sup-boundtarget", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    terminal = TerminalService(_config(watched_bindings=("sup-demo",)))
    from terminal_mcp.bindings import BindingStore

    terminal.bindings = BindingStore(tmp_path / "bindings.db")
    terminal.bindings.put("sup-demo", session)
    svc = SupervisorService(terminal, SupervisorStore(tmp_path / "supervisor.db"))
    svc.run_once()
    watches = svc.list_watches()["watches"]
    assert any(w["kind"] == "binding" and w["target"] == "sup-demo" for w in watches)


# ---------------------------------------------------------------------------
# MCP tool surface
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_supervisor_tools_registered_and_functional(tmp_path, tmux_session_factory):
    from terminal_mcp.mcp_app import build_mcp

    session = tmux_session_factory(
        "test-sup-tool", "bash -lc 'echo \"Do you want to continue? [y/N]\"; read x; sleep 20'"
    )
    time.sleep(0.3)
    terminal = TerminalService(_config())
    svc = SupervisorService(terminal, SupervisorStore(tmp_path / "supervisor.db"))
    server = build_mcp(terminal, svc)

    names = {tool.name for tool in await server.list_tools()}
    assert {
        "supervisor_watch", "supervisor_unwatch", "supervisor_list_watches",
        "supervisor_status", "supervisor_list_events", "supervisor_ack_event", "supervisor_run_once",
    } <= names

    async def call(name, **kwargs):
        result = await server.call_tool(name, kwargs)
        if result.structured_content is not None:
            return result.structured_content
        import json
        return json.loads(result.content[0].text)

    watched = await call("supervisor_watch", session=session)
    assert watched["created"] is True
    ran = await call("supervisor_run_once")
    assert ran["events"] and ran["events"][0]["event_type"] == "attention_required"
    status = await call("supervisor_status")
    assert status["watch_count"] == 1
    listed = await call("supervisor_list_events")
    assert len(listed["events"]) == 1
    acked = await call("supervisor_ack_event", id=listed["events"][0]["id"])
    assert acked["acknowledged"] is True



# ---------------------------------------------------------------------------
# Reliability cleanup: the background loop never dies silently
# ---------------------------------------------------------------------------


def test_background_loop_survives_a_poll_exception_and_records_it(tmp_path):
    from terminal_mcp.supervisor import SupervisorLoop, _LAST_POLL_ERROR

    class ExplodingService:
        config = SupervisorConfig(poll_interval_seconds=5)

        def run_once(self):
            raise RuntimeError("synthetic poll failure for this test")

    _LAST_POLL_ERROR[0] = None
    loop = SupervisorLoop(ExplodingService())
    loop.start()
    try:
        deadline = time.monotonic() + 3
        while _LAST_POLL_ERROR[0] is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _LAST_POLL_ERROR[0] is not None
        assert "RuntimeError" in _LAST_POLL_ERROR[0]["error"]
        assert "synthetic poll failure" in _LAST_POLL_ERROR[0]["error"]
        # The thread is still alive -- one bad cycle never kills the loop.
        assert loop.is_alive()
    finally:
        loop.stop()
        _LAST_POLL_ERROR[0] = None


@pytest.fixture
def anyio_backend():
    return "asyncio"
