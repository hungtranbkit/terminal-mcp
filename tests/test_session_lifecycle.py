"""Session lifecycle: create/detach/delete real tmux sessions.

Covers the dashboard's "Tạo session"/"Tách"/"Xóa session" controls and the
terminal_create_session/_detach_session/_delete_session MCP tools -- both
surfaces share the exact same TerminalService methods (see core.py's
"Session lifecycle" section and lifecycle.py), so route-level tests here
also exercise the MCP-tool-facing code path.
"""
from __future__ import annotations

import subprocess

import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.mcp_app import build_mcp


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], check=check, capture_output=True, text=True, timeout=10)


def _lifecycle_config(tmp_path, *, enabled: bool = True, protected=("terminal-mcp",),
                      launch_commands=(("claude", "claude"), ("codex", "codex")),
                      timeout: float = 5.0, roots=None) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True),
        allowed_session_patterns=("lifecycle-*", "claude-lc-*", "codex-lc-*"),
        max_capture_lines=200,
        default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("lifecycle-*", "claude-lc-*", "codex-lc-*")),
        session_lifecycle=SessionLifecycleConfig(
            enabled=enabled,
            allowed_cwd_roots=(str(tmp_path),) if roots is None else roots,
            protected_sessions=protected,
            launch_commands=launch_commands,
            create_ready_timeout_seconds=timeout,
        ),
    )


@pytest.fixture
def lifecycle_session_factory():
    """Tracks names created *through the service* (not pre-created via
    tmux directly, unlike tmux_session_factory) and guarantees teardown
    even if a test's own delete/detach assertions fail first."""
    created: list[str] = []

    def track(name: str) -> str:
        created.append(name)
        return name

    yield track
    for name in created:
        _tmux("kill-session", "-t", name, check=False)


# -- create -------------------------------------------------------------

def test_create_shell_session_succeeds(tmp_path, lifecycle_session_factory):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    name = lifecycle_session_factory("lifecycle-shell-1")
    result = service.terminal_create_session(name, "shell")
    assert "error" not in result
    assert result["state"] == "READY"  # a shell has nothing to wait for
    assert result["agent_type"] == "shell"
    assert service.tmux.get_session(name) is not None
    # Creating never auto-grants -- caller gets a session, nothing more.
    assert result["grant"] is None


def test_create_duplicate_name_fails_explicitly(tmp_path, lifecycle_session_factory):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    name = lifecycle_session_factory("lifecycle-dup")
    first = service.terminal_create_session(name, "shell")
    assert "error" not in first
    second = service.terminal_create_session(name, "shell")
    assert second["error"] == "SESSION_ALREADY_EXISTS"
    # The pre-existing session must be completely untouched -- never
    # attached/overwritten by the failed duplicate attempt.
    info = service.tmux.get_session(name)
    assert info is not None and info.session_id == first["session_id"]


@pytest.mark.parametrize("bad_name", [
    "../etc/passwd", "rm -rf /", "foo; rm -rf /", "$(whoami)", "foo`id`",
    "-x", ".hidden", "a" * 200, "", "foo/bar", "foo bar",
])
def test_create_rejects_invalid_or_injection_like_names(tmp_path, bad_name):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    result = service.terminal_create_session(bad_name, "shell")
    assert result["error"] in ("INVALID_SESSION_NAME", "SENSITIVE_SESSION_NOT_CREATABLE")
    assert service.tmux.get_session(bad_name) is None


def test_create_rejects_cwd_outside_allowed_roots(tmp_path, lifecycle_session_factory):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    name = lifecycle_session_factory("lifecycle-badcwd")
    result = service.terminal_create_session(name, "shell", str(outside))
    assert result["error"] == "CWD_NOT_ALLOWED"
    assert service.tmux.get_session(name) is None


def test_create_rejects_nonexistent_cwd(tmp_path, lifecycle_session_factory):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    name = lifecycle_session_factory("lifecycle-nocwd")
    result = service.terminal_create_session(name, "shell", str(tmp_path / "does-not-exist"))
    assert result["error"] == "CWD_NOT_FOUND"


def test_create_rejects_path_traversal_cwd(tmp_path, lifecycle_session_factory):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    config = _lifecycle_config(tmp_path, roots=(str(allowed_root),))
    service = TerminalService(config)
    name = lifecycle_session_factory("lifecycle-traversal")
    result = service.terminal_create_session(name, "shell", str(allowed_root / ".." / ".."))
    assert result["error"] == "CWD_NOT_ALLOWED"


def test_create_rejects_symlink_escape_cwd(tmp_path, lifecycle_session_factory):
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    escape_link = allowed_root / "escape"
    escape_link.symlink_to(outside, target_is_directory=True)
    config = _lifecycle_config(tmp_path, roots=(str(allowed_root),))
    service = TerminalService(config)
    name = lifecycle_session_factory("lifecycle-symlink")
    result = service.terminal_create_session(name, "shell", str(escape_link))
    assert result["error"] == "CWD_NOT_ALLOWED"
    assert service.tmux.get_session(name) is None


def test_create_rejects_unknown_agent_type(tmp_path, lifecycle_session_factory):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    name = lifecycle_session_factory("lifecycle-badtype")
    result = service.terminal_create_session(name, "bash -c whoami")
    assert result["error"] == "INVALID_AGENT_TYPE"
    assert service.tmux.get_session(name) is None


@pytest.mark.parametrize("agent_type,session_prefix", [("claude", "claude-lc-"), ("codex", "codex-lc-")])
def test_create_claude_and_codex_use_server_side_allowlisted_launcher(
    tmp_path, lifecycle_session_factory, agent_type, session_prefix,
):
    # Real, locally-installed claude/codex binaries (same ones the
    # production nail/promptflow/codex-main sessions run) -- agent_type
    # never becomes client-supplied command text, only a lookup key into
    # config.session_lifecycle.launch_commands.
    config = _lifecycle_config(tmp_path, timeout=8.0)
    service = TerminalService(config)
    name = lifecycle_session_factory(f"{session_prefix}1")
    result = service.terminal_create_session(name, agent_type)
    assert "error" not in result
    assert result["state"] in ("READY", "CREATED")
    info = service.tmux.get_session(name)
    assert info is not None


def test_create_launcher_never_accepts_raw_command_from_caller(tmp_path, lifecycle_session_factory):
    # There is no code path that lets agent_type (or any other create
    # parameter) become an arbitrary shell command -- only "shell",
    # "claude", "codex" are ever accepted, and the latter two always
    # resolve through config.session_lifecycle.launch_commands, never
    # through caller-supplied text.
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    from terminal_mcp.lifecycle import AGENT_TYPES
    assert AGENT_TYPES == ("shell", "claude", "codex")
    name = lifecycle_session_factory("lifecycle-noexec")
    result = service.terminal_create_session(name, "shell; touch /tmp/pwned")
    assert result["error"] == "INVALID_AGENT_TYPE"


def test_session_lifecycle_disabled_blocks_create(tmp_path):
    config = _lifecycle_config(tmp_path, enabled=False)
    service = TerminalService(config)
    result = service.terminal_create_session("lifecycle-disabled", "shell")
    assert result["error"] == "SESSION_LIFECYCLE_DISABLED"
    assert service.tmux.get_session("lifecycle-disabled") is None


def test_create_launch_fail_cleans_up_disposable_session(tmp_path, lifecycle_session_factory):
    # A launcher configured to a command that exits immediately -- tmux's
    # default remain-on-exit=off tears the session down with it, so this
    # exercises the "session already gone" branch of the FAILED path
    # (LAUNCH_FAILED, never left behind as a zombie session either way).
    config = _lifecycle_config(tmp_path, launch_commands=(("codex", "/bin/false"),))
    service = TerminalService(config)
    name = lifecycle_session_factory("lifecycle-launchfail")
    result = service.terminal_create_session(name, "codex")
    assert result["error"] == "LAUNCH_FAILED"
    assert result["state"] == "FAILED"
    assert service.tmux.get_session(name) is None


def test_create_grant_mode_none_never_grants(tmp_path, lifecycle_session_factory):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    name = lifecycle_session_factory("lifecycle-nogrant")
    result = service.terminal_create_session(name, "shell", grant_mode="none")
    assert result["grant"] is None
    assert service.grants.get(name) is None


def test_create_grant_mode_read_send_grants_explicitly(tmp_path, lifecycle_session_factory):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    # A name outside the static whitelist -- proves the grant, not the
    # static allowlist, is what makes it readable/sendable afterward.
    name = lifecycle_session_factory("granted-disposable-1")
    result = service.terminal_create_session(name, "shell", grant_mode="read_send", requested_by="tester")
    assert "error" not in result
    assert result["grant"]["read"].get("read_enabled") is True
    assert result["grant"]["input"].get("input_enabled") is True
    grant = service.grants.get(name)
    assert grant is not None and grant.read_enabled and grant.input_enabled
    service.grants.set_read(name, False, granted_by="test-cleanup")  # never leave a stray active grant behind


def test_create_binding_option_creates_binding(tmp_path, lifecycle_session_factory):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    name = lifecycle_session_factory("lifecycle-bindtest")
    result = service.terminal_create_session(name, "shell", binding="lifecycle-binding-1")
    assert "error" not in result
    assert "error" not in result["binding"]
    stored = service.bindings.get("lifecycle-binding-1")
    assert stored is not None and stored.session == name
    service.bindings.delete("lifecycle-binding-1")


def test_create_binding_collision_fails_closed_never_remaps(tmp_path, tmux_session_factory, lifecycle_session_factory):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    first = tmux_session_factory("lifecycle-bindfirst")  # must already exist for terminal_bind to succeed
    second = lifecycle_session_factory("lifecycle-bindsecond")
    bound = service.terminal_bind("lifecycle-collide", first)
    assert "error" not in bound
    result = service.terminal_create_session(second, "shell", binding="lifecycle-collide")
    assert "error" not in result  # the session itself is still created successfully
    assert result["binding"]["error"] == "BINDING_EXISTS"
    stored = service.bindings.get("lifecycle-collide")
    assert stored is not None and stored.session == first  # never remapped to `second`
    service.bindings.delete("lifecycle-collide")


def test_create_initial_prompt_goes_through_reliable_submission_once(tmp_path, lifecycle_session_factory):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    name = lifecycle_session_factory("lifecycle-prompt-1")
    result = service.terminal_create_session(name, "shell", initial_prompt="echo hello-lifecycle", grant_mode="none")
    assert result["state"] == "READY"
    prompt_result = result["initial_prompt_result"]
    assert "error" not in prompt_result
    assert prompt_result.get("sent") is True
    # Sent exactly once -- the pane shows the echoed command exactly once,
    # not duplicated by any retry/second-send path.
    import time
    time.sleep(0.3)
    output = "\n".join(service.tmux.capture_lines(name, 50))
    assert output.count("echo hello-lifecycle") == 1


def test_create_initial_prompt_without_permission_reports_denied_not_silent(tmp_path, lifecycle_session_factory):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    name = lifecycle_session_factory("unwhitelisted-prompt-1")
    result = service.terminal_create_session(name, "shell", initial_prompt="echo should-not-send", grant_mode="none")
    assert result["state"] == "READY"
    assert result["initial_prompt_result"].get("error") == "ACCESS_DENIED"


# -- detach ---------------------------------------------------------------

def test_detach_attached_session_does_not_kill_it(tmp_path, tmux_session_factory):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    name = tmux_session_factory("lifecycle-detach-1")
    _tmux("set-option", "-t", name, "destroy-unattached", "off", check=False)
    result = service.terminal_detach_session(name)
    assert "error" not in result
    assert result["attached"] is False
    # Session/process still exist -- output/state preserved.
    assert service.tmux.get_session(name) is not None


def test_detach_already_detached_session_is_idempotent(tmp_path, tmux_session_factory):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    name = tmux_session_factory("lifecycle-detach-2")
    first = service.terminal_detach_session(name)
    second = service.terminal_detach_session(name)
    assert "error" not in first and "error" not in second
    assert second["attached"] is False
    assert service.tmux.get_session(name) is not None


def test_detach_nonexistent_session_reports_not_found(tmp_path):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    result = service.terminal_detach_session("lifecycle-never-existed")
    assert result["error"] == "SESSION_NOT_FOUND"


# -- delete -----------------------------------------------------------------

def test_delete_terminates_exact_target_only(tmp_path, tmux_session_factory):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    victim = tmux_session_factory("lifecycle-victim")
    bystander = tmux_session_factory("lifecycle-bystander")
    result = service.terminal_delete_session(victim)
    assert result["deleted"] is True
    assert service.tmux.get_session(victim) is None
    assert service.tmux.get_session(bystander) is not None


def test_delete_missing_session_is_idempotent(tmp_path):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    result = service.terminal_delete_session("lifecycle-already-gone")
    assert "error" not in result
    assert result["deleted"] is False
    assert result["action"] == "already_gone"


def test_delete_protected_session_is_refused(tmp_path, tmux_session_factory):
    # A disposable session name, configured as protected here -- exercises
    # the exact same lifecycle.delete() refusal path the real, live
    # "terminal-mcp" controlling session relies on, without ever creating
    # or killing a session by that literal name (see tmux_session_factory's
    # own docstring in conftest.py for why: this project's actual
    # deployment runs its test suite in the SAME tmux server as real,
    # attended sessions, and a test that kill-session'd the literal name
    # "terminal-mcp" here once took down this project's own live session).
    name = "lifecycle-protected-sim"
    config = _lifecycle_config(tmp_path, protected=(name,))
    service = TerminalService(config)
    tmux_session_factory(name)
    result = service.terminal_delete_session(name)
    assert result["error"] == "SESSION_PROTECTED"
    assert service.tmux.get_session(name) is not None


def test_protected_set_always_includes_terminal_mcp_even_if_omitted(tmp_path):
    # SessionLifecycleConfig.__post_init__ (config.py) folds "terminal-mcp"
    # into protected_sessions unconditionally, so this holds regardless of
    # how the config was built -- exercised at both layers: the config
    # object itself, and terminal_delete_session's real refusal decision.
    # lifecycle.py's delete() checks the protected set BEFORE ever issuing
    # a tmux call, so asserting the refusal here needs no real "terminal-
    # mcp" tmux session to exist (this suite must never create or kill one
    # -- see test_delete_protected_session_is_refused above).
    config = _lifecycle_config(tmp_path, protected=())  # operator config omits it entirely
    assert "terminal-mcp" in config.session_lifecycle.protected_sessions
    service = TerminalService(config)
    result = service.terminal_delete_session("terminal-mcp")
    assert result["error"] == "SESSION_PROTECTED"


def test_delete_cleans_up_stale_binding_and_grant(tmp_path, tmux_session_factory):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    name = tmux_session_factory("lifecycle-cleanup-1")
    service.terminal_bind("lifecycle-cleanup-binding", name)
    service.grant_session_read(name, True)
    service.grant_session_input(name, True)
    assert service.bindings.get("lifecycle-cleanup-binding") is not None
    assert service.grants.get(name).read_enabled is True

    result = service.terminal_delete_session(name)
    assert result["deleted"] is True
    # Binding removed outright -- never left pointing at a vanished session.
    assert service.bindings.get("lifecycle-cleanup-binding") is None
    # Grant kept as a row (history) but marked disabled, not silently
    # left active for a same-named session created later.
    grant = service.grants.get(name)
    assert grant is not None
    assert grant.read_enabled is False and grant.input_enabled is False


def test_delete_disables_supervisor_watch_without_losing_history(tmp_path, tmux_session_factory):
    from terminal_mcp.supervisor import SupervisorService, SupervisorStore, watch_key
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    store = SupervisorStore(tmp_path / "supervisor.db")
    supervisor = SupervisorService(service, store)
    name = tmux_session_factory("lifecycle-cleanup-2")
    supervisor.watch(session=name)
    assert store.get_watch(watch_key("session", name)) is not None

    result = service.terminal_delete_session(name)
    assert result["deleted"] is True
    # Mirrors mcp_app.py/dashboard.py's own post-delete coordination call.
    supervisor.unwatch(session=name, delete=False)
    watches = supervisor.list_watches()["watches"]
    matching = [w for w in watches if w["target"] == name]
    assert matching and matching[0]["enabled"] is False  # disabled, not deleted -- history kept


# -- dashboard route-level: auth/CSRF, refresh, shared service -------------

def _dashboard_client(config: AppConfig) -> tuple[TestClient, TerminalService]:
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    return TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"}), service


def test_dashboard_create_route_rejects_cross_origin_request(tmp_path):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "https://evil.example.com"})
    response = client.post("/dashboard/api/session/create", json={"name": "lifecycle-csrf", "agent_type": "shell"})
    assert response.status_code == 403
    assert response.json()["error"] == "ORIGIN_NOT_ALLOWED"
    assert service.tmux.get_session("lifecycle-csrf") is None


def test_dashboard_lifecycle_routes_require_cloudflare_access_when_configured():
    from terminal_mcp.config import DashboardConfig
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
    response = client.post("/dashboard/api/session/create", json={"name": "lifecycle-cf", "agent_type": "shell"})
    assert response.status_code == 403
    assert response.json()["error"] == "CLOUDFLARE_ACCESS_VERIFICATION_FAILED"


def test_dashboard_create_detach_delete_round_trip_refreshes_list(tmp_path, lifecycle_session_factory):
    config = _lifecycle_config(tmp_path)
    client, service = _dashboard_client(config)
    name = lifecycle_session_factory("lifecycle-roundtrip-1")

    created = client.post("/dashboard/api/session/create", json={"name": name, "agent_type": "shell"})
    assert created.status_code == 200
    listed = client.get("/dashboard/api/sessions").json()
    assert any(row["name"] == name for row in listed["sessions"])
    assert listed["session_lifecycle_enabled"] is True
    assert "terminal-mcp" in listed["protected_sessions"]

    detached = client.post("/dashboard/api/session/detach", json={"name": name})
    assert detached.status_code == 200
    assert detached.json()["attached"] is False
    assert service.tmux.get_session(name) is not None  # still there after detach

    deleted = client.post("/dashboard/api/session/delete", json={"name": name})
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    listed_after = client.get("/dashboard/api/sessions").json()
    assert not any(row["name"] == name for row in listed_after["sessions"])


def test_dashboard_delete_route_refuses_protected_session(tmp_path, tmux_session_factory):
    name = "lifecycle-protected-dashboard-sim"  # see test_delete_protected_session_is_refused
    config = _lifecycle_config(tmp_path, protected=(name,))
    client, service = _dashboard_client(config)
    tmux_session_factory(name)
    response = client.post("/dashboard/api/session/delete", json={"name": name})
    assert response.status_code == 403
    assert response.json()["error"] == "SESSION_PROTECTED"
    assert service.tmux.get_session(name) is not None


def test_mcp_tool_and_dashboard_route_share_the_same_service(tmp_path, lifecycle_session_factory):
    # The dashboard route below and mcp_app.py's terminal_create_session
    # tool both do nothing but call TerminalService.terminal_create_session
    # -- proven here by creating through the dashboard HTTP route, then
    # immediately observing the effect through the exact same service
    # instance's own method (what the MCP tool wrapper calls directly).
    config = _lifecycle_config(tmp_path)
    client, service = _dashboard_client(config)
    name = lifecycle_session_factory("lifecycle-shared-svc")
    response = client.post("/dashboard/api/session/create", json={"name": name, "agent_type": "shell"})
    assert response.status_code == 200
    direct = service.terminal_create_session(name, "shell")  # same session, via the "MCP" call shape
    assert direct["error"] == "SESSION_ALREADY_EXISTS"  # proves it's the same underlying tmux/service state


@pytest.mark.anyio
async def test_mcp_tools_registered_for_session_lifecycle(tmp_path):
    config = _lifecycle_config(tmp_path)
    service = TerminalService(config)
    server = build_mcp(service)
    tool_names = {tool.name for tool in await server.list_tools()}
    for name in ("terminal_create_session", "terminal_detach_session", "terminal_delete_session"):
        assert name in tool_names


@pytest.fixture
def anyio_backend():
    return "asyncio"
