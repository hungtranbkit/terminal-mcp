"""RecoveryEngine -- REAL, live, disposable end-to-end proof (task: "Auto
Recovery cho session sau reboot/crash/node-agent restart", item 10's own
required acceptance: "session chết/restart node -> logical session cũ
được RECOVERED... không duplicate process; ... trường hợp thiếu
conversation_id fail-safe, không giả resume").

Real local tmux backend, real TerminalService, real ControllerService
(LocalNodeClient), real SessionRegistryStore/PaneLeaseStore -- the ONLY
thing not exercised here is a real resumable Claude conversation (that
underlying mechanism -- terminal_registry_reopen's own --resume/verify
-- already has its own real, live-proven E2E, see docs/REQUIREMENTS.md's
"Conversation-continuity recovery" Feature Details entry; re-running a
REAL claude process here would add real API cost/flakiness to prove a
layer this engine does not itself implement). What IS proven for real
here: process death -> reconciliation -> a real new process, same
session name, same stable_session_id, real exactly-once locking under a
genuine race, and the missing-conversation_id DEGRADED fail-safe.

SAFETY: every session here is a disposable tmp_path/tmux fixture --
never `window`/`window2`/`wtest`/`win3`/`win4`."""
from __future__ import annotations

import subprocess
import threading
import time

import pytest

from terminal_mcp.audit import AuditStore
from terminal_mcp.bindings import BindingStore
from terminal_mcp.config import AppConfig, AutoRecoveryConfig, InputPolicyConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.controller import build_default_controller
from terminal_mcp.core import RECOVERY_STATE_DEGRADED
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.killed_sessions import KilledSessionStore
from terminal_mcp.lease import PaneLeaseStore
from terminal_mcp.recovery_engine import RecoveryEngine
from terminal_mcp.session_registry import SessionRegistryStore


def _config(tmp_path) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("recovery-*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("recovery-*",)),
        session_lifecycle=SessionLifecycleConfig(
            enabled=True, allowed_cwd_roots=(str(tmp_path),),
            protected_sessions=("terminal-mcp",),
        ),
    )


def _tmux(*args, check=True):
    return subprocess.run(["tmux", *args], check=check, capture_output=True, text=True, timeout=10)


@pytest.fixture
def tmux_cleanup():
    created: list[str] = []
    yield created
    for name in created:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False, capture_output=True)


@pytest.fixture
def rig(tmp_path, tmux_cleanup):
    from terminal_mcp.core import TerminalService
    service = TerminalService(
        _config(tmp_path), bindings=BindingStore(tmp_path / "bindings.db"), audit=AuditStore(tmp_path / "audit.db"),
        grants=SessionGrantStore(tmp_path / "grants.db"), leases=PaneLeaseStore(tmp_path / "leases.db"),
        killed_sessions=KilledSessionStore(tmp_path / "killed_sessions.db"),
        session_registry=SessionRegistryStore(tmp_path / "session_registry.db"),
    )
    controller = build_default_controller(service)
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=(), agent_version=None)
    lease_store = PaneLeaseStore(tmp_path / "recovery_leases.db")
    engine = RecoveryEngine(service.session_registry, controller, lease_store,
                            AutoRecoveryConfig(enabled=True))
    return {"service": service, "controller": controller, "engine": engine, "cleanup": tmux_cleanup}


def test_real_process_death_reconcile_and_recovery_same_logical_session(rig):
    """The core acceptance scenario: a real tmux session dies out-of-
    band (simulating a crash/node restart) -> a real reconcile pass
    marks it MISSING -> the engine spawns a real NEW process under the
    SAME session name, same cwd, same stable_session_id -- proving this
    is recognized as the SAME logical session recovering, not an
    unrelated fresh one."""
    service = rig["service"]
    engine = rig["engine"]
    rig["cleanup"].append("recovery-a")

    created = service.terminal_create_session("recovery-a", "shell", str(service.config.session_lifecycle.allowed_cwd_roots[0]))
    assert created["state"] == "READY"
    time.sleep(0.3)
    service.terminal_list_sessions()  # first reconcile -- ACTIVE, real stable_session_id assigned
    original_record = service.session_registry.get("local", "recovery-a")
    original_pid = created["pane_id"]

    # Real, out-of-band process death -- NEVER through terminal_kill_
    # session (that would mark KILLED, a completely different, already-
    # covered case) -- this is what a real crash/node-agent restart
    # looks like.
    _tmux("kill-session", "-t", "recovery-a", check=False)
    listing = service.terminal_registry_list()
    record = next(r for r in listing["records"] if r["session_name"] == "recovery-a")
    assert record["status"] == "MISSING"

    result = engine.recover_session("local", "recovery-a")
    assert "error" not in result, result
    assert result["recovery_state"] == RECOVERY_STATE_DEGRADED  # shell -> never resumable, honest fail-safe

    # Genuinely a NEW real process (recreated_from_registry), but the
    # SAME logical session: same name, same cwd, same stable_session_id
    # (never regenerated -- see session_registry.py's own migration
    # docstring for why).
    assert result["recreated_from_registry"] is True
    new_record = service.session_registry.get("local", "recovery-a")
    assert new_record.status == "ACTIVE"
    assert new_record.stable_session_id == original_record.stable_session_id
    assert new_record.cwd == original_record.cwd
    info = service.tmux.get_session("recovery-a")
    assert info is not None and not info.pane_dead
    assert info.pane_id != original_pid  # genuinely a new process, never a resurrection


def test_no_duplicate_process_under_a_genuine_concurrent_race(rig):
    """Item 5's own core requirement: nhiều reconnect/retry không được
    spawn duplicate agent/session. Two threads call recover_session for
    the EXACT SAME missing session at the same moment -- exactly one
    real spawn must happen, the other must cleanly lose the race."""
    service = rig["service"]
    engine = rig["engine"]
    rig["cleanup"].append("recovery-race")
    service.terminal_create_session("recovery-race", "shell", str(service.config.session_lifecycle.allowed_cwd_roots[0]))
    time.sleep(0.3)
    service.terminal_list_sessions()
    _tmux("kill-session", "-t", "recovery-race", check=False)
    service.terminal_list_sessions()  # -> MISSING

    results: list[dict] = []
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait(timeout=5)
        results.append(engine.recover_session("local", "recovery-race"))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 2
    successes = [r for r in results if "error" not in r]
    in_progress = [r for r in results if r.get("error") == "RECOVERY_IN_PROGRESS"]
    # Exactly one real recovery happened; the other cleanly lost the
    # race (a real, live-reproduced outcome, not asserted from theory).
    assert len(successes) == 1, results
    assert len(in_progress) == 1, results


def test_missing_conversation_id_is_an_honest_degraded_recovery_never_a_fake_resume(rig):
    """Item 6/10's own explicit fail-safe requirement: a session with no
    conversation_id on record must NEVER be reported as a genuine
    resumed conversation -- DEGRADED, with a real, honest detail
    string, every time."""
    service = rig["service"]
    engine = rig["engine"]
    rig["cleanup"].append("recovery-nocid")
    service.terminal_create_session("recovery-nocid", "shell", str(service.config.session_lifecycle.allowed_cwd_roots[0]))
    time.sleep(0.3)
    service.terminal_list_sessions()
    record = service.session_registry.get("local", "recovery-nocid")
    assert record.conversation_id is None  # shell never gets one -- confirms the real precondition
    assert record.resumable is False

    _tmux("kill-session", "-t", "recovery-nocid", check=False)
    service.terminal_list_sessions()

    result = engine.recover_session("local", "recovery-nocid")
    assert result["recovery_state"] == RECOVERY_STATE_DEGRADED
    new_record = service.session_registry.get("local", "recovery-nocid")
    assert new_record.recovery_state == RECOVERY_STATE_DEGRADED
    assert "no conversation_id" in new_record.recovery_detail


def test_reconcile_node_end_to_end_through_the_real_registry_list_route(rig):
    """The full automatic path, through controller.registry_list (the
    real fleet-aware read this engine's own reconcile_node depends on)
    -- not just recover_session called directly."""
    service = rig["service"]
    engine = rig["engine"]
    rig["cleanup"].append("recovery-reconcile")
    service.terminal_create_session("recovery-reconcile", "shell",
                                    str(service.config.session_lifecycle.allowed_cwd_roots[0]))
    time.sleep(0.3)
    service.terminal_list_sessions()
    _tmux("kill-session", "-t", "recovery-reconcile", check=False)
    service.terminal_list_sessions()

    results = engine.reconcile_node("local")
    assert any(r.get("session") == "recovery-reconcile" and "error" not in r for r in results), results
    assert service.session_registry.get("local", "recovery-reconcile").status == "ACTIVE"
