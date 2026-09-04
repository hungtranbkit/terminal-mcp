"""Kill/Reopen session lifecycle (core.py's terminal_kill_session/
terminal_reopen_session/terminal_list_killed_sessions, killed_sessions.py).

Kill is destructive (real tmux kill-session + process tree) and requires
a server-enforced confirm_name match; on success it captures real,
observed pane state (never a guess) and saves it as reopen metadata.
Reopen recreates a NEW session/process from that metadata via the
existing, unmodified terminal_create_session -- never a resurrection of
the killed process's own memory/state.
"""
from __future__ import annotations

import subprocess
import time

import pytest

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.core import TerminalService


def _config(tmp_path, *, protected=("terminal-mcp",)) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True),
        allowed_session_patterns=("lifecycle-*",),
        max_capture_lines=200,
        default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("lifecycle-*",)),
        session_lifecycle=SessionLifecycleConfig(
            enabled=True, allowed_cwd_roots=(str(tmp_path),), protected_sessions=protected,
            launch_commands=(("claude", "claude"), ("codex", "codex")), create_ready_timeout_seconds=5,
        ),
    )


def _create_and_settle(service: TerminalService, name: str, agent_type: str, cwd) -> dict:
    result = service.terminal_create_session(name, agent_type, str(cwd))
    assert result.get("state") == "READY", result
    # Real settle time -- pane_current_command/pane_current_path briefly
    # still reflect tmux's own just-forked state immediately after
    # new-session, before the pane's actual shell/launcher has fully
    # execed -- a real Kill in practice always happens well after this
    # window (the session has been alive/used), this just reproduces that.
    time.sleep(0.4)
    return result


@pytest.fixture
def cleanup_tmux():
    created: list[str] = []
    yield created.append
    for name in created:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False, capture_output=True)


def test_kill_disposable_session_frees_tmux_and_saves_metadata(tmp_path, cleanup_tmux):
    config = _config(tmp_path)
    service = TerminalService(config, killed_sessions=_isolated_killed_store(tmp_path))
    name = "lifecycle-smoke-kill-1"
    cleanup_tmux(name)
    _create_and_settle(service, name, "shell", tmp_path)
    assert service.tmux.get_session(name) is not None

    result = service.terminal_kill_session(name, name)
    assert result["deleted"] is True
    assert result["reopen_metadata"]["metadata_complete"] is True
    assert result["reopen_metadata"]["agent_type"] == "shell"
    assert result["reopen_metadata"]["working_directory"] == str(tmp_path)

    # Process/tmux actually gone -- the whole point of Kill.
    assert service.tmux.get_session(name) is None


def test_kill_requires_confirm_name_match(tmp_path, cleanup_tmux):
    config = _config(tmp_path)
    service = TerminalService(config, killed_sessions=_isolated_killed_store(tmp_path))
    name = "lifecycle-smoke-kill-confirm"
    cleanup_tmux(name)
    _create_and_settle(service, name, "shell", tmp_path)

    result = service.terminal_kill_session(name, "not-the-right-name")
    assert result == {"error": "CONFIRMATION_MISMATCH", "session": name}
    # Refused before touching tmux at all.
    assert service.tmux.get_session(name) is not None


def test_kill_refuses_protected_session_even_with_correct_confirm(tmp_path, cleanup_tmux):
    config = _config(tmp_path, protected=("lifecycle-protected-sim",))
    service = TerminalService(config, killed_sessions=_isolated_killed_store(tmp_path))
    name = "lifecycle-protected-sim"
    cleanup_tmux(name)
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "bash"], check=True)
    time.sleep(0.2)

    result = service.terminal_kill_session(name, name)  # confirm_name matches exactly
    assert result == {"error": "SESSION_PROTECTED", "session": name}
    assert service.tmux.get_session(name) is not None
    assert service.killed_sessions.get(name) is None  # never captured metadata for a refused kill


def test_terminal_mcp_itself_cannot_be_accidentally_killed(tmp_path):
    # Uses the REAL default protected_sessions (always includes
    # "terminal-mcp" -- see AskChatGptConfig/SessionLifecycleConfig
    # __post_init__ precedent) -- never touches the real live session
    # since this refuses before any tmux call, exactly like
    # test_delete_protected_session_is_refused in test_session_lifecycle.py.
    config = _config(tmp_path, protected=())  # operator config omits it -- still always protected
    service = TerminalService(config, killed_sessions=_isolated_killed_store(tmp_path))
    result = service.terminal_kill_session("terminal-mcp", "terminal-mcp")
    assert result == {"error": "SESSION_PROTECTED", "session": "terminal-mcp"}


def test_kill_already_gone_is_idempotent_and_saves_no_metadata(tmp_path):
    config = _config(tmp_path)
    service = TerminalService(config, killed_sessions=_isolated_killed_store(tmp_path))
    result = service.terminal_kill_session("lifecycle-never-existed", "lifecycle-never-existed")
    assert result["deleted"] is False
    assert result["action"] == "already_gone"
    assert result["reopen_metadata"] is None


# -- Reopen -----------------------------------------------------------------

def test_reopen_from_saved_metadata_recreates_same_name_cwd_agent(tmp_path, cleanup_tmux):
    config = _config(tmp_path)
    service = TerminalService(config, killed_sessions=_isolated_killed_store(tmp_path))
    name = "lifecycle-smoke-reopen-1"
    cleanup_tmux(name)
    _create_and_settle(service, name, "shell", tmp_path)
    killed = service.terminal_kill_session(name, name)
    assert killed["reopen_metadata"]["metadata_complete"] is True

    reopened = service.terminal_reopen_session(name)
    assert reopened["state"] == "READY"
    assert reopened["agent_type"] == "shell"
    assert reopened["cwd"] == str(tmp_path)
    assert reopened["reopened_from_metadata"] is True
    assert service.tmux.get_session(name) is not None


def test_reopen_is_a_new_process_not_a_resurrection(tmp_path, cleanup_tmux):
    config = _config(tmp_path)
    service = TerminalService(config, killed_sessions=_isolated_killed_store(tmp_path))
    name = "lifecycle-smoke-reopen-identity"
    cleanup_tmux(name)
    _create_and_settle(service, name, "shell", tmp_path)
    before = service.tmux.get_session(name)
    service.terminal_kill_session(name, name)
    service.terminal_reopen_session(name)
    after = service.tmux.get_session(name)
    assert after is not None
    assert after.session_id != before.session_id  # tmux's own never-reused-while-alive id -- proves "new", not "restored"
    assert after.pane_pid != before.pane_pid


def test_reopen_clears_metadata_after_success(tmp_path, cleanup_tmux):
    config = _config(tmp_path)
    service = TerminalService(config, killed_sessions=_isolated_killed_store(tmp_path))
    name = "lifecycle-smoke-reopen-clear"
    cleanup_tmux(name)
    _create_and_settle(service, name, "shell", tmp_path)
    service.terminal_kill_session(name, name)
    assert service.killed_sessions.get(name) is not None
    service.terminal_reopen_session(name)
    assert service.killed_sessions.get(name) is None


def test_reopen_without_metadata_and_no_override_fails_clearly_not_a_guess(tmp_path):
    config = _config(tmp_path)
    service = TerminalService(config, killed_sessions=_isolated_killed_store(tmp_path))
    result = service.terminal_reopen_session("lifecycle-never-killed")
    assert result == {"error": "REOPEN_METADATA_INCOMPLETE", "session": "lifecycle-never-killed",
                      "missing": ["agent_type"]}


def test_reopen_with_agent_type_but_no_cwd_reports_missing_cwd(tmp_path):
    config = _config(tmp_path)
    service = TerminalService(config, killed_sessions=_isolated_killed_store(tmp_path))
    result = service.terminal_reopen_session("lifecycle-partial", agent_type="claude")
    assert result == {"error": "REOPEN_METADATA_INCOMPLETE", "session": "lifecycle-partial",
                      "missing": ["working_directory"]}


def test_reopen_shell_needs_no_cwd_override(tmp_path):
    config = _config(tmp_path)
    service = TerminalService(config, killed_sessions=_isolated_killed_store(tmp_path))
    result = service.terminal_reopen_session("lifecycle-shell-only", agent_type="shell")
    assert result.get("state") == "READY"
    subprocess.run(["tmux", "kill-session", "-t", "lifecycle-shell-only"], check=False, capture_output=True)


def test_reopen_with_explicit_override_bypasses_incomplete_saved_metadata(tmp_path, cleanup_tmux):
    # Simulate a "legacy"/unmanaged session: created directly via raw tmux
    # (never through terminal_create_session), running a command that
    # matches no known launcher and isn't a plain shell -- pane_current_
    # command classification finds nothing, so Kill saves INCOMPLETE
    # metadata (this is exactly the "unknown/legacy session" scenario).
    config = _config(tmp_path)
    service = TerminalService(config, killed_sessions=_isolated_killed_store(tmp_path))
    name = "lifecycle-legacy-unmanaged"
    cleanup_tmux(name)
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", str(tmp_path), "sleep 300"], check=True)
    time.sleep(0.3)

    killed = service.terminal_kill_session(name, name)
    assert killed["deleted"] is True
    assert killed["reopen_metadata"]["metadata_complete"] is False
    assert killed["reopen_metadata"]["agent_type"] is None  # "sleep" matches no launcher, not a shell either

    # Reopen with NO override fails clearly, naming what's missing.
    blind = service.terminal_reopen_session(name)
    assert blind["error"] == "REOPEN_METADATA_INCOMPLETE"
    assert blind["missing"] == ["agent_type"]

    # Caller explicitly picks a safe agent/cwd instead -- never guessed.
    explicit = service.terminal_reopen_session(name, agent_type="shell", cwd=str(tmp_path))
    assert explicit.get("state") == "READY"
    assert explicit["reopened_from_metadata"] is False  # override was used, not the saved (incomplete) record


# -- listing / self-healing --------------------------------------------------

def test_list_killed_sessions_shows_entry_until_reopened(tmp_path, cleanup_tmux):
    config = _config(tmp_path)
    service = TerminalService(config, killed_sessions=_isolated_killed_store(tmp_path))
    name = "lifecycle-smoke-list-1"
    cleanup_tmux(name)
    _create_and_settle(service, name, "shell", tmp_path)
    service.terminal_kill_session(name, name)

    listed = service.terminal_list_killed_sessions()
    assert any(entry["name"] == name for entry in listed["killed_sessions"])

    service.terminal_reopen_session(name)
    listed_after = service.terminal_list_killed_sessions()
    assert not any(entry["name"] == name for entry in listed_after["killed_sessions"])


def test_list_killed_sessions_self_heals_when_name_reused_outside_reopen(tmp_path, cleanup_tmux):
    config = _config(tmp_path)
    service = TerminalService(config, killed_sessions=_isolated_killed_store(tmp_path))
    name = "lifecycle-smoke-reused"
    cleanup_tmux(name)
    _create_and_settle(service, name, "shell", tmp_path)
    service.terminal_kill_session(name, name)
    assert service.killed_sessions.get(name) is not None

    # A session by this name exists again, but NOT via terminal_reopen_
    # session (e.g. created directly, or via terminal_create_session
    # again) -- the stale killed_sessions record must never keep showing
    # up as "still reopenable".
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "bash"], check=True)
    time.sleep(0.2)

    listed = service.terminal_list_killed_sessions()
    assert not any(entry["name"] == name for entry in listed["killed_sessions"])
    assert service.killed_sessions.get(name) is None


def _isolated_killed_store(tmp_path):
    from terminal_mcp.killed_sessions import KilledSessionStore
    return KilledSessionStore(tmp_path / "killed_sessions.db")


# -- dashboard routes: auth/CSRF/origin regression ---------------------------

def _dashboard_client(config: AppConfig):
    from starlette.testclient import TestClient
    from terminal_mcp.dashboard import register_dashboard
    from terminal_mcp.mcp_app import build_mcp
    service = TerminalService(config, killed_sessions=_isolated_killed_store_for(config))
    server = build_mcp(service)
    register_dashboard(server, service)
    return TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"}), service


def _isolated_killed_store_for(config):
    import tempfile
    from pathlib import Path
    from terminal_mcp.killed_sessions import KilledSessionStore
    # A fresh isolated store per client -- fine for these route-shape-only
    # tests, which never rely on data written by another client.
    return KilledSessionStore(Path(tempfile.mkdtemp()) / "killed_sessions.db")


def test_dashboard_kill_route_rejects_cross_origin_request(tmp_path):
    from starlette.testclient import TestClient
    from terminal_mcp.dashboard import register_dashboard
    from terminal_mcp.mcp_app import build_mcp
    config = _config(tmp_path)
    service = TerminalService(config, killed_sessions=_isolated_killed_store(tmp_path))
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "https://evil.example.com"})
    response = client.post("/dashboard/api/session/kill", json={"name": "lifecycle-x", "confirm_name": "lifecycle-x"})
    assert response.status_code == 403
    assert response.json()["error"] == "ORIGIN_NOT_ALLOWED"


def test_dashboard_kill_route_requires_cloudflare_access_when_configured():
    from starlette.testclient import TestClient
    from terminal_mcp.config import DashboardConfig
    from terminal_mcp.dashboard import register_dashboard
    from terminal_mcp.mcp_app import build_mcp
    config = AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("lifecycle-*",),
        max_capture_lines=50, default_tail_lines=20,
        dashboard=DashboardConfig(cloudflare_access_team_domain="team.cloudflareaccess.com",
                                  cloudflare_access_audience="aud"),
        session_lifecycle=SessionLifecycleConfig(enabled=True),
    )
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})
    response = client.post("/dashboard/api/session/kill", json={"name": "lifecycle-x", "confirm_name": "lifecycle-x"})
    assert response.status_code == 403
    assert response.json()["error"] == "CLOUDFLARE_ACCESS_VERIFICATION_FAILED"


def test_dashboard_kill_route_refuses_protected_session(tmp_path):
    name = "lifecycle-protected-dashboard-kill"
    config = _config(tmp_path, protected=(name,))
    client, service = _dashboard_client(config)
    import subprocess, time
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "bash"], check=True)
    time.sleep(0.2)
    try:
        response = client.post("/dashboard/api/session/kill", json={"name": name, "confirm_name": name})
        assert response.status_code == 403
        assert response.json()["error"] == "SESSION_PROTECTED"
        assert service.tmux.get_session(name) is not None
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False, capture_output=True)


def test_dashboard_kill_route_requires_matching_confirm_name(tmp_path, cleanup_tmux):
    config = _config(tmp_path)
    client, service = _dashboard_client(config)
    name = "lifecycle-dashboard-confirm"
    cleanup_tmux(name)
    _create_and_settle(service, name, "shell", tmp_path)
    response = client.post("/dashboard/api/session/kill", json={"name": name, "confirm_name": "typo"})
    assert response.status_code == 400
    assert response.json()["error"] == "CONFIRMATION_MISMATCH"
    assert service.tmux.get_session(name) is not None


def test_dashboard_kill_then_reopen_round_trip(tmp_path, cleanup_tmux):
    config = _config(tmp_path)
    client, service = _dashboard_client(config)
    name = "lifecycle-dashboard-roundtrip"
    cleanup_tmux(name)
    _create_and_settle(service, name, "shell", tmp_path)

    killed = client.post("/dashboard/api/session/kill", json={"name": name, "confirm_name": name})
    assert killed.status_code == 200
    assert killed.json()["reopen_metadata"]["metadata_complete"] is True

    listed = client.get("/dashboard/api/killed-sessions")
    assert any(e["name"] == name for e in listed.json()["killed_sessions"])

    reopened = client.post("/dashboard/api/session/reopen", json={"name": name})
    assert reopened.status_code == 200
    assert reopened.json()["state"] == "READY"

    listed_after = client.get("/dashboard/api/killed-sessions")
    assert not any(e["name"] == name for e in listed_after.json()["killed_sessions"])
