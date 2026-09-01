"""P0 Part C: independent completion verification. Prose/marker evidence
alone must never promote an *autonomous* (Supervisor v2 approved_auto_
continue, with v2 globally enabled) watch to VERIFIED_DONE -- it requires
a real, independently-run verifier (verifier.py) to pass first. A non-
autonomous watch (the default, and every watch that predates this
feature) is completely unaffected -- see test_supervisor.py's existing
quiet-window/nonce promotion tests, still green, unchanged."""
from __future__ import annotations

import subprocess
import sys
import time

from terminal_mcp.audit import AuditStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, SupervisorConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.supervisor import SupervisorService, SupervisorStore
from terminal_mcp.supervisor2 import build_supervisor_v2

PASS_CMD = [sys.executable, "-c", "exit(0)"]
FAIL_CMD = [sys.executable, "-c", "exit(1)"]


def _config(**overrides) -> AppConfig:
    overrides.setdefault("v2_enabled", True)
    overrides.setdefault("completion_verify_quiet_seconds", 2)
    return AppConfig(
        PermissionsConfig(True, True), ("test-*", "agent-*"), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
        supervisor=SupervisorConfig(**overrides),
    )


def _v2(tmp_path, **overrides):
    terminal = TerminalService(_config(**overrides), audit=AuditStore(tmp_path / "audit.db"))
    store = SupervisorStore(tmp_path / "supervisor.db")
    svc = SupervisorService(terminal, store)
    return build_supervisor_v2(svc), svc


def _done_session(tmux_session_factory, name: str) -> str:
    return tmux_session_factory(name, "bash -lc 'printf \"FINAL REPORT\\ndone\\n\"; sleep 30'")


def _make_autonomous(v2, svc, session: str):
    svc.watch(session=session)
    events = svc.run_once()["events"]
    v2.set_policy(session=session, policy_mode="approved_auto_continue", approved_template="ack")
    return events


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "a.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


# ---------------------------------------------------------------------------
# C.1/C.4: prose alone never yields VERIFIED_DONE for an autonomous watch
# ---------------------------------------------------------------------------


def test_prose_alone_never_yields_verified_done_for_autonomous_watch_no_verifier(tmp_path, tmux_session_factory):
    session = _done_session(tmux_session_factory, "test-cv-noverifier")
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    _make_autonomous(v2, svc, session)
    svc.run_once()  # first sees COMPLETION_CANDIDATE
    time.sleep(3)
    svc.run_once()  # quiet window elapses -- would have promoted pre-Part-C
    watch = svc.store.get_watch(f"session:{session}")
    assert watch["state"] == "BLOCKED"
    last_event = svc.store.list_events(target=session, limit=1)[0]
    assert "verifier policy" in last_event["reason"]
    assert svc.get_completion_token(session=session)  # watch is still alive, just blocked, not deleted


def test_non_autonomous_watch_unaffected_still_promotes_on_quiet_window(tmp_path, tmux_session_factory):
    session = _done_session(tmux_session_factory, "test-cv-nonauto")
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    svc.watch(session=session)  # observe_only default -- never autonomous
    svc.run_once()
    time.sleep(3)
    svc.run_once()
    watch = svc.store.get_watch(f"session:{session}")
    assert watch["state"] == "VERIFIED_DONE"  # completely unchanged, pre-existing behavior


# ---------------------------------------------------------------------------
# C.3/C.4: real verifier evidence actually gates the outcome
# ---------------------------------------------------------------------------


def test_passing_verifier_promotes_autonomous_watch_to_verified_done(tmp_path, tmux_session_factory):
    session = _done_session(tmux_session_factory, "test-cv-pass")
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    _make_autonomous(v2, svc, session)
    svc.set_verifier_policy(session=session, test_command=PASS_CMD)
    svc.run_once()
    time.sleep(3)
    events = svc.run_once()["events"]
    watch = svc.store.get_watch(f"session:{session}")
    assert watch["state"] == "VERIFIED_DONE"
    assert watch["last_verifier_pass"] == 1
    assert any(e["event_type"] == "verifying" for e in svc.store.list_events(target=session, limit=20))
    assert any(e["event_type"] == "verified_done" for e in svc.store.list_events(target=session, limit=20))


def test_failed_test_command_rejects_promotion_to_failed_state(tmp_path, tmux_session_factory):
    session = _done_session(tmux_session_factory, "test-cv-failtest")
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    _make_autonomous(v2, svc, session)
    svc.set_verifier_policy(session=session, test_command=FAIL_CMD)
    svc.run_once()
    time.sleep(3)
    svc.run_once()
    watch = svc.store.get_watch(f"session:{session}")
    assert watch["state"] == "FAILED"
    assert watch["last_verifier_pass"] == 0


def test_dirty_worktree_scope_violation_rejects_promotion(tmp_path, tmux_session_factory):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "a.txt").write_text("dirty\n")
    session = _done_session(tmux_session_factory, "test-cv-dirty")
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    _make_autonomous(v2, svc, session)
    svc.set_verifier_policy(session=session, worktree=str(repo), require_git_clean=True)
    svc.run_once()
    time.sleep(3)
    svc.run_once()
    watch = svc.store.get_watch(f"session:{session}")
    assert watch["state"] == "FAILED"


def test_commit_worktree_mismatch_rejects_promotion(tmp_path, tmux_session_factory):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    session = _done_session(tmux_session_factory, "test-cv-commitmismatch")
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    _make_autonomous(v2, svc, session)
    svc.set_verifier_policy(session=session, worktree=str(repo), require_commit_matches="0" * 40)
    svc.run_once()
    time.sleep(3)
    svc.run_once()
    watch = svc.store.get_watch(f"session:{session}")
    assert watch["state"] == "FAILED"


def test_matching_commit_and_clean_worktree_passes(tmp_path, tmux_session_factory):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                         text=True, check=True).stdout.strip()
    session = _done_session(tmux_session_factory, "test-cv-commitmatch")
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    _make_autonomous(v2, svc, session)
    svc.set_verifier_policy(session=session, worktree=str(repo), require_git_clean=True,
                            require_commit_matches=sha)
    svc.run_once()
    time.sleep(3)
    svc.run_once()
    watch = svc.store.get_watch(f"session:{session}")
    assert watch["state"] == "VERIFIED_DONE"
    result = __import__("json").loads(watch["last_verifier_result"])
    assert result["git"]["commit_sha"] == sha


# ---------------------------------------------------------------------------
# C.5: BLOCKED/FAILED actually halt further autonomous action-taking
# ---------------------------------------------------------------------------


def test_failed_verification_blocks_further_autonomous_actions(tmp_path, tmux_session_factory):
    session = _done_session(tmux_session_factory, "test-cv-blocks-policy")
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    _make_autonomous(v2, svc, session)
    svc.set_verifier_policy(session=session, test_command=FAIL_CMD)
    svc.run_once()
    time.sleep(3)
    svc.run_once()
    policy = v2.get_policy(session=session)
    assert policy["blocked_reason"] is not None
    assert "independent verifier failed" in policy["blocked_reason"]


def test_failed_watch_does_not_perpetually_reverify_every_poll(tmp_path, tmux_session_factory):
    # A FAILED/BLOCKED watch is disabled (not deleted) precisely so the
    # SAME still-"done"-looking pane output never re-arms COMPLETION_
    # CANDIDATE and re-runs the (real subprocess) verifier again on every
    # subsequent poll cycle, forever, for a condition that cannot resolve
    # itself without an operator.
    session = _done_session(tmux_session_factory, "test-cv-noreverify")
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    _make_autonomous(v2, svc, session)
    svc.set_verifier_policy(session=session, test_command=FAIL_CMD)
    svc.run_once()
    time.sleep(3)
    svc.run_once()
    watch = svc.store.get_watch(f"session:{session}")
    assert watch["state"] == "FAILED"
    assert watch["enabled"] == 0
    events_after_first_failure = len(svc.store.list_events(target=session, limit=50))

    for _ in range(3):
        svc.run_once()  # disabled -- run_once's own `if not row["enabled"]: continue` skips it
    watch = svc.store.get_watch(f"session:{session}")
    assert watch["state"] == "FAILED"  # unchanged
    assert len(svc.store.list_events(target=session, limit=50)) == events_after_first_failure  # no new events


def test_blocked_no_verifier_also_blocks_the_v2_policy(tmp_path, tmux_session_factory):
    session = _done_session(tmux_session_factory, "test-cv-blocks-policy-noverifier")
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    _make_autonomous(v2, svc, session)
    svc.run_once()
    time.sleep(3)
    svc.run_once()
    policy = v2.get_policy(session=session)
    assert policy["blocked_reason"] is not None


# ---------------------------------------------------------------------------
# C.5/C.6: restart during VERIFYING reconciles safely
# ---------------------------------------------------------------------------


def test_restart_during_verifying_reconciles_to_verified_done(tmp_path, tmux_session_factory):
    session = _done_session(tmux_session_factory, "test-cv-restart-pass")
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    _make_autonomous(v2, svc, session)
    svc.set_verifier_policy(session=session, test_command=PASS_CMD)
    key = f"session:{session}"
    # Simulate a crash exactly between set_verifying's durable write and
    # run_verifier ever completing -- a fresh SupervisorService opening the
    # same db (a real "process restart") must resolve it on its own next poll.
    svc.store.set_verifying(key, "2020-01-01T00:00:00+00:00")
    v2b, svcb = _v2(tmp_path)
    result = svcb.run_once()
    watch = svcb.store.get_watch(key)
    assert watch["state"] == "VERIFIED_DONE"
    assert any(e["event_type"] == "verified_done" for e in result["events"])


def test_restart_during_verifying_reconciles_to_failed(tmp_path, tmux_session_factory):
    session = _done_session(tmux_session_factory, "test-cv-restart-fail")
    time.sleep(0.3)
    v2, svc = _v2(tmp_path)
    _make_autonomous(v2, svc, session)
    svc.set_verifier_policy(session=session, test_command=FAIL_CMD)
    key = f"session:{session}"
    svc.store.set_verifying(key, "2020-01-01T00:00:00+00:00")
    v2b, svcb = _v2(tmp_path)
    svcb.run_once()
    watch = svcb.store.get_watch(key)
    assert watch["state"] == "FAILED"


# ---------------------------------------------------------------------------
# C.6: replayed nonce rejected (unaffected by Part C, still enforced)
# ---------------------------------------------------------------------------


def test_replayed_nonce_never_reverifies_an_autonomous_watch(tmp_path, tmux_session_factory):
    session = tmux_session_factory(
        "test-cv-nonce-replay",
        "bash -lc 'read x; printf \"FINAL REPORT\\ndone\\n\"; sleep 30'",
    )
    time.sleep(0.3)
    v2, svc = _v2(tmp_path, completion_verify_quiet_seconds=3600)
    _make_autonomous(v2, svc, session)
    svc.set_verifier_policy(session=session, test_command=PASS_CMD)
    token = svc.get_completion_token(session=session)
    svc.terminal.terminal_send_text(session, "go", press_enter=True)
    time.sleep(0.5)
    marker = (
        "###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 "
        f"task_id={token['task_id']} attempt={token['attempt']} status=completion_candidate "
        f"summary_sha256=deadbeef1234 nonce={token['nonce']}###"
    )
    svc.terminal.terminal_send_text(session, marker, press_enter=True)
    time.sleep(0.5)
    svc.run_once()  # nonce-verified -- promotes via VERIFYING -> VERIFIED_DONE on this very poll
    watch = svc.store.get_watch(f"session:{session}")
    assert watch["state"] == "VERIFIED_DONE"
    # Replaying the exact same marker text again (pasted/scrolled back into
    # view) must never re-trigger verification a second time -- the watch
    # is already VERIFIED_DONE and the nonce is consumed.
    svc.terminal.terminal_send_text(session, marker, press_enter=True)
    time.sleep(0.5)
    events_before = len(svc.store.list_events(target=session, limit=50))
    svc.run_once()
    events_after = len(svc.store.list_events(target=session, limit=50))
    assert events_after == events_before  # no new verifying/verified_done event from the replay
    assert svc.store.get_watch(f"session:{session}")["state"] == "VERIFIED_DONE"


# ---------------------------------------------------------------------------
# C.6: stale verifier evidence after new output is not blindly reused
# ---------------------------------------------------------------------------


def test_a_new_attempt_after_failure_reruns_the_verifier_fresh_not_stale(tmp_path, tmux_session_factory):
    session = tmux_session_factory("test-cv-restale", "bash -lc 'read x; printf \"FINAL REPORT\\ndone\\n\"; sleep 30'")
    time.sleep(0.3)
    v2, svc = _v2(tmp_path, completion_verify_quiet_seconds=2)
    _make_autonomous(v2, svc, session)
    svc.set_verifier_policy(session=session, test_command=FAIL_CMD)
    svc.terminal.terminal_send_text(session, "go", press_enter=True)
    time.sleep(0.5)
    svc.run_once()
    time.sleep(3)
    svc.run_once()
    assert svc.store.get_watch(f"session:{session}")["state"] == "FAILED"

    # Operator fixes the underlying condition and reconfigures the
    # verifier to something that now genuinely passes, then re-watches
    # (a fresh attempt) -- the NEXT verification run must reflect the
    # CURRENT policy/state, never silently reuse the earlier FAILED result.
    svc.set_verifier_policy(session=session, test_command=PASS_CMD)
    svc.watch(session=session)  # re-enable: fresh attempt, fresh nonce
    svc.run_once()
    time.sleep(3)
    result = svc.run_once()
    watch = svc.store.get_watch(f"session:{session}")
    assert watch["state"] == "VERIFIED_DONE"
    assert watch["last_verifier_pass"] == 1  # fresh, not the stale earlier FAILED evidence
