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
    # P0-7/P0-8: prose completion evidence alone is only a
    # COMPLETION_CANDIDATE now, never directly "DONE" -- see status.py's
    # SUPERVISOR_STATES docstring for why (VERIFIED_DONE requires a later
    # poll to corroborate a quiet window, which this single-poll test
    # deliberately does not wait for).
    session = tmux_session_factory("test-sup-done", "bash -lc 'printf \"FINAL REPORT\\nall good\\n\"; sleep 20'")
    time.sleep(0.3)
    svc = _service(tmp_path)
    svc.watch(session=session)
    result = svc.run_once()
    assert result["events"][0]["state"] == "COMPLETION_CANDIDATE"
    assert result["events"][0]["event_type"] == "completion_candidate"


def _marker(task_id: str, attempt, nonce: str, *, status: str = "completion_candidate") -> str:
    return (
        "###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 "
        f"task_id={task_id} attempt={attempt} status={status} "
        f"summary_sha256=deadbeef1234 nonce={nonce}###"
    )


def _print_into_pane(session: str, text: str) -> None:
    # Raw tmux send-keys, deliberately bypassing terminal_send_text's
    # permission/audit layer -- these tests simulate the agent itself
    # printing a marker into its own pane, not the supervisor sending
    # anything, so the input-permission path (covered elsewhere) is
    # irrelevant here. A plain `bash sleep` target is canonical-tty, so
    # (unlike test_send_reliability.py's raw-tty targets) no settle gap
    # is needed between the literal text and Enter.
    import subprocess

    subprocess.run(["tmux", "send-keys", "-t", session, "-l", "--", f"printf '%s\\n' '{text}'"], check=True)
    subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], check=True)


def test_nonce_verified_marker_promotes_to_verified_done_on_the_very_first_poll(tmp_path, tmux_session_factory):
    # P0-7 phase 2: a marker whose task_id/attempt/nonce match the current,
    # unconsumed token is materially stronger evidence than prose alone --
    # it skips the ordinary quiet-window wait entirely and promotes on this
    # very poll, even with a long quiet window configured.
    session = tmux_session_factory("test-sup-nonce-ok", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    svc = _service(tmp_path, completion_verify_quiet_seconds=3600)
    svc.watch(session=session)
    key = watch_key("session", session)
    token = svc.get_completion_token(session=session)
    assert token["consumed"] is False
    assert token["task_id"] == key

    marker = _marker(token["task_id"], token["attempt"], token["nonce"])
    _print_into_pane(session, marker)
    time.sleep(0.3)

    result = svc.run_once()
    watch = svc.list_watches()["watches"][0]
    assert watch["state"] == "VERIFIED_DONE"
    assert result["events"][0]["event_type"] == "verified_done"
    assert "nonce-verified" in result["events"][0]["reason"]

    # The token is single-use: fetching it again shows consumed.
    assert svc.get_completion_token(session=session)["consumed"] is True


def test_marker_with_wrong_task_id_does_not_bypass_the_quiet_window(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-sup-nonce-wrongtask", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    svc = _service(tmp_path, completion_verify_quiet_seconds=3600)
    svc.watch(session=session)
    token = svc.get_completion_token(session=session)

    marker = _marker("some-other-watch-key", token["attempt"], token["nonce"])
    _print_into_pane(session, marker)
    time.sleep(0.3)

    result = svc.run_once()
    watch = svc.list_watches()["watches"][0]
    # Falls back to an ordinary (unverified) candidate -- not promoted, and
    # the token is left unconsumed since it never actually matched.
    assert watch["state"] == "COMPLETION_CANDIDATE"
    assert result["events"][0]["event_type"] == "completion_candidate"
    assert svc.get_completion_token(session=session)["consumed"] is False


def test_marker_with_wrong_attempt_does_not_bypass_the_quiet_window(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-sup-nonce-wrongattempt", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    svc = _service(tmp_path, completion_verify_quiet_seconds=3600)
    svc.watch(session=session)
    key = watch_key("session", session)
    token = svc.get_completion_token(session=session)

    # Same task_id/nonce, but a stale attempt number (e.g. echoed from
    # before an unwatch/rewatch bumped the attempt counter).
    marker = _marker(key, token["attempt"] + 1, token["nonce"])
    _print_into_pane(session, marker)
    time.sleep(0.3)

    result = svc.run_once()
    watch = svc.list_watches()["watches"][0]
    assert watch["state"] == "COMPLETION_CANDIDATE"
    assert result["events"][0]["event_type"] == "completion_candidate"
    assert svc.get_completion_token(session=session)["consumed"] is False


def test_replaying_a_consumed_nonce_after_rewatch_does_not_bypass_the_quiet_window(tmp_path, tmux_session_factory):
    # Full replay scenario: attempt 1's token gets legitimately consumed,
    # then the watch is disabled and re-enabled (a new attempt, per
    # upsert_watch's docstring) minting a fresh token. An agent (buggy, or
    # an adversarial pasted-back transcript) that echoes the OLD, already-
    # consumed attempt-1 marker again must never verify against attempt 2.
    session = tmux_session_factory("test-sup-nonce-replay", "bash -lc 'sleep 40'")
    time.sleep(0.2)
    svc = _service(tmp_path, completion_verify_quiet_seconds=3600)
    svc.watch(session=session)
    key = watch_key("session", session)
    old_token = svc.get_completion_token(session=session)

    svc.unwatch(session=session)
    svc.watch(session=session)  # re-watch: new attempt, fresh nonce
    new_token = svc.get_completion_token(session=session)
    assert new_token["attempt"] == old_token["attempt"] + 1
    assert new_token["nonce"] != old_token["nonce"]

    replayed_marker = _marker(key, old_token["attempt"], old_token["nonce"])
    _print_into_pane(session, replayed_marker)
    time.sleep(0.3)

    result = svc.run_once()
    watch = svc.list_watches()["watches"][0]
    assert watch["state"] == "COMPLETION_CANDIDATE"
    assert result["events"][0]["event_type"] == "completion_candidate"
    # The new attempt's token is untouched by the replay attempt.
    assert svc.get_completion_token(session=session)["consumed"] is False


def test_completion_token_and_attempt_persist_across_a_fresh_store_handle(tmp_path, tmux_session_factory):
    # Restart persistence: a brand new SupervisorStore/Service pair opened
    # against the same db path must see the same nonce/attempt/consumed
    # state the first one wrote -- nonce delivery survives a service
    # restart exactly like watches/events already do.
    session = tmux_session_factory("test-sup-nonce-restart", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    svc = _service(tmp_path)
    svc.watch(session=session)
    token_before = svc.get_completion_token(session=session)

    reopened_store = SupervisorStore(svc.store.path)
    reopened = SupervisorService(svc.terminal, reopened_store)
    token_after = reopened.get_completion_token(session=session)
    assert token_after == token_before


def test_get_completion_token_requires_exactly_one_target(tmp_path):
    svc = _service(tmp_path)
    assert svc.get_completion_token()["error"] == "EXACTLY_ONE_TARGET_REQUIRED"
    assert svc.get_completion_token(binding="a", session="b")["error"] == "EXACTLY_ONE_TARGET_REQUIRED"


def test_get_completion_token_unknown_watch_is_reported_not_guessed(tmp_path):
    svc = _service(tmp_path)
    result = svc.get_completion_token(session="test-sup-never-watched")
    assert result["error"] == "WATCH_NOT_FOUND"


# ---------------------------------------------------------------------------
# P0-7/P0-8 phase 3: trusted verifier hooks. Never executes anything --
# purely reads structured evidence markers the agent already printed into
# its own pane, bound to the same nonce/attempt as the completion token.
# ---------------------------------------------------------------------------


def _evidence(kind: str, task_id: str, attempt, nonce: str, status: str) -> str:
    return (
        "###TERMINAL_MCP_EVIDENCE protocol=terminal-mcp-evidence/v1 "
        f"kind={kind} task_id={task_id} attempt={attempt} nonce={nonce} "
        f"status={status} summary=see-pane###"
    )


def test_watch_rejects_unknown_verifier_kind(tmp_path):
    svc = _service(tmp_path)
    result = svc.watch(session="test-sup-badverifier", required_verifiers=["not_a_real_kind"])
    assert result["error"] == "UNKNOWN_VERIFIER_KIND"
    assert result["unknown"] == ["not_a_real_kind"]
    assert svc.list_watches()["watches"] == []  # rejected before any row was written


def test_watch_with_no_required_verifiers_is_the_default_and_unaffected(tmp_path, tmux_session_factory):
    session = tmux_session_factory(
        "test-sup-verifier-default", "bash -lc 'printf \"FINAL REPORT\\nall good\\n\"; sleep 20'"
    )
    time.sleep(0.2)
    svc = _service(tmp_path, completion_verify_quiet_seconds=1)
    watch = svc.watch(session=session)
    assert watch["required_verifiers"] == []
    svc.run_once()
    time.sleep(1.2)
    svc.run_once()
    # Unaffected: promotes via the ordinary quiet-window path exactly like
    # before phase 3 existed, since no required_verifiers is configured.
    assert svc.list_watches()["watches"][0]["state"] == "VERIFIED_DONE"


def test_required_verifier_missing_blocks_promotion_even_after_quiet_window(tmp_path, tmux_session_factory):
    session = tmux_session_factory(
        "test-sup-verifier-missing", "bash -lc 'printf \"FINAL REPORT\\nall good\\n\"; sleep 20'"
    )
    time.sleep(0.2)
    svc = _service(tmp_path, completion_verify_quiet_seconds=1)
    watch = svc.watch(session=session, required_verifiers=["tests"])
    assert watch["required_verifiers"] == ["tests"]

    first = svc.run_once()
    assert first["events"][0]["state"] == "COMPLETION_CANDIDATE"
    time.sleep(1.2)
    second = svc.run_once()
    # Would ordinarily promote now (quiet window elapsed, no regression) --
    # blocked because the required "tests" evidence was never printed.
    assert second["events"] == []
    assert svc.list_watches()["watches"][0]["state"] == "COMPLETION_CANDIDATE"


def test_required_verifier_evidence_failing_blocks_promotion(tmp_path, tmux_session_factory):
    # Prose/quiet-window path (not the nonce fast-path): printing the
    # evidence marker is itself new pane output, so it (re-)arms a fresh
    # quiet window over the combined completion+evidence snapshot -- the
    # SAME snapshot must then hold quiet a second time before the required-
    # verifier check is even reached, exactly like any other candidate
    # snapshot. Two full run_once()/quiet-window cycles is the real
    # sequence, not an artifact of this test.
    session = tmux_session_factory(
        "test-sup-verifier-fail",
        "bash -lc 'printf \"FINAL REPORT\\nall good\\n\"; read marker; printf \"%s\\n\" \"$marker\"; sleep 20'",
    )
    time.sleep(0.2)
    svc = _service(tmp_path, completion_verify_quiet_seconds=1)
    svc.watch(session=session, required_verifiers=["tests"])
    token = svc.get_completion_token(session=session)
    svc.run_once()
    time.sleep(1.2)

    marker = _evidence("tests", token["task_id"], token["attempt"], token["nonce"], "fail")
    _print_into_pane(session, marker)
    time.sleep(0.3)
    svc.run_once()  # re-arms on the new (evidence-included) snapshot
    time.sleep(1.2)

    result = svc.run_once()
    assert result["events"] == []  # a failing verifier is not a re-alert-worthy transition
    assert svc.list_watches()["watches"][0]["state"] == "COMPLETION_CANDIDATE"
    # The completion nonce was never consumed by this -- still available.
    assert svc.get_completion_token(session=session)["consumed"] is False
    assert svc.get_completion_token(session=session)["nonce"] == token["nonce"]


def test_required_verifier_evidence_passing_allows_promotion(tmp_path, tmux_session_factory):
    session = tmux_session_factory(
        "test-sup-verifier-pass",
        "bash -lc 'printf \"FINAL REPORT\\nall good\\n\"; read marker; printf \"%s\\n\" \"$marker\"; sleep 20'",
    )
    time.sleep(0.2)
    svc = _service(tmp_path, completion_verify_quiet_seconds=1)
    svc.watch(session=session, required_verifiers=["tests"])
    token = svc.get_completion_token(session=session)
    svc.run_once()
    time.sleep(1.2)

    marker = _evidence("tests", token["task_id"], token["attempt"], token["nonce"], "pass")
    _print_into_pane(session, marker)
    time.sleep(0.3)
    svc.run_once()  # re-arms on the new (evidence-included) snapshot
    time.sleep(1.2)

    result = svc.run_once()
    assert result["events"][0]["event_type"] == "verified_done"
    assert svc.list_watches()["watches"][0]["state"] == "VERIFIED_DONE"


def test_required_verifier_gates_the_nonce_fast_path_too(tmp_path, tmux_session_factory):
    # A nonce-verified completion marker alone is not enough when a
    # verifier is required -- it must not consume the nonce or promote
    # until the required evidence also shows up (bound to the same,
    # still-unconsumed token).
    session = tmux_session_factory("test-sup-verifier-noncegate", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    svc = _service(tmp_path, completion_verify_quiet_seconds=3600)
    svc.watch(session=session, required_verifiers=["tests"])
    token = svc.get_completion_token(session=session)

    completion_marker = _marker(token["task_id"], token["attempt"], token["nonce"])
    _print_into_pane(session, completion_marker)
    time.sleep(0.3)

    result = svc.run_once()
    assert result["events"][0]["event_type"] == "completion_candidate"
    assert svc.list_watches()["watches"][0]["state"] == "COMPLETION_CANDIDATE"
    assert svc.get_completion_token(session=session)["consumed"] is False  # not spent by the failed attempt

    evidence_marker = _evidence("tests", token["task_id"], token["attempt"], token["nonce"], "pass")
    _print_into_pane(session, evidence_marker)
    time.sleep(0.3)

    result2 = svc.run_once()
    assert result2["events"][0]["event_type"] == "verified_done"
    assert svc.list_watches()["watches"][0]["state"] == "VERIFIED_DONE"
    assert svc.get_completion_token(session=session)["consumed"] is True


def test_required_verifiers_are_sticky_on_rewatch_unless_explicitly_changed(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-sup-verifier-sticky", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    svc = _service(tmp_path)
    svc.watch(session=session, required_verifiers=["tests", "git_status"])
    svc.unwatch(session=session)

    resumed = svc.watch(session=session)  # no required_verifiers passed -- sticky
    assert sorted(resumed["required_verifiers"]) == ["git_status", "tests"]

    cleared = svc.watch(session=session, required_verifiers=[])  # explicit clear
    assert cleared["required_verifiers"] == []


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
