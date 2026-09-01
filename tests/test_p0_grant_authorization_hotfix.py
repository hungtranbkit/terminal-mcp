"""P0 HOTFIX: unify dynamic session grants with all MCP read/input paths.

Root cause (see core.py's _read_authorized_with_grant/_input_authorized_
with_grant docstrings for the full detail): terminal_list_sessions/
dashboard_list_sessions computed "statically allowed OR granted" for
their own DISPLAY fields, but every actual read/input tool
(terminal_status/terminal_capture/terminal_tail/terminal_input_context/
terminal_bind/terminal_send_text/terminal_send_keys, and Supervisor's
session-kind watch()) gated on session_allowed/input_session_allowed
alone via TerminalService._guard/_input_guard -- a session authorized
only via an active dashboard grant (never in the static config.yaml
whitelist, the entire point of a grant) reported read_allowed=true/
input_allowed=true in discovery while every actual operation still
returned ACCESS_DENIED. This is the live promptflow bug reported against
the running production service.

This file exercises the exact reported shape (static allowed=false +
read_granted=true + input_granted=true) against every affected path, plus
every safety invariant that must survive the fix unchanged: read-only
grants can't send, input can't bypass the global switch, denylisted
sessions stay denied despite a grant, a revoked grant fails closed
immediately, and a session recreated under the same name never inherits
unsafe input authorization.
"""
from __future__ import annotations

import time

from terminal_mcp.audit import AuditStore
from terminal_mcp.bindings import BindingStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, SupervisorConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.supervisor import SupervisorService, SupervisorStore
from terminal_mcp.supervisor2 import build_supervisor_v2


def _config(*, terminal_input=True, denied_session_patterns=()) -> AppConfig:
    # "promptflow"-shaped: NOT in either static whitelist, exactly the
    # real reported case (allowed=false in both directions).
    return AppConfig(
        PermissionsConfig(True, terminal_input), ("test-*", "agent-*"), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("test-*",),
                          denied_session_patterns=denied_session_patterns),
    )


def _service(tmp_path, **config_overrides) -> TerminalService:
    return TerminalService(
        _config(**config_overrides),
        grants=SessionGrantStore(tmp_path / "grants.db"),
        audit=AuditStore(tmp_path / "audit.db"),
        bindings=BindingStore(tmp_path / "bindings.db"),
    )


def _granted_session(tmp_path, tmux_session_factory, name: str, *, command: str | None = None,
                     grant_input: bool = True, terminal_input: bool = True) -> tuple[TerminalService, str]:
    session = tmux_session_factory(name, command or "bash -lc 'read v; echo GOT=$v; sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path, terminal_input=terminal_input)
    assert service.grant_session_read(session, True, granted_by="hotfix-test").get("read_enabled") is True
    if grant_input:
        result = service.grant_session_input(session, True, granted_by="hotfix-test")
        assert result.get("input_enabled") is True
    return service, session


# ---------------------------------------------------------------------------
# Exact reproduction of the reported live bug
# ---------------------------------------------------------------------------


def test_exact_reproduction_allowed_false_read_and_input_granted_true(tmp_path, tmux_session_factory):
    service, session = _granted_session(tmp_path, tmux_session_factory, "newsession-promptflow-repro")

    listed = {s["name"]: s for s in service.terminal_list_sessions()["sessions"]}[session]
    assert listed["allowed"] is False
    assert listed["read_allowed"] is True
    assert listed["read_granted"] is True
    assert listed["input_allowed"] is True
    assert listed["input_granted"] is True

    assert "error" not in service.terminal_status(session)
    assert "error" not in service.terminal_capture(session)
    assert "error" not in service.terminal_tail(session)
    assert "error" not in service.terminal_input_context(session=session)
    bound = service.terminal_bind("promptflow-repro-bind", session, read_enabled=True, input_enabled=True)
    assert "error" not in bound
    dry = service.terminal_send_text(session, "hello", press_enter=True, dry_run=True)
    assert dry.get("would_send") is True
    assert "error" not in dry


# ---------------------------------------------------------------------------
# list_sessions reports effective permissions consistently
# ---------------------------------------------------------------------------


def test_list_sessions_effective_fields_match_read_input_allowed(tmp_path, tmux_session_factory):
    service, session = _granted_session(tmp_path, tmux_session_factory, "newsession-effective-consistency")
    listed = {s["name"]: s for s in service.terminal_list_sessions()["sessions"]}[session]
    assert listed["effective_read"] == listed["read_allowed"] == True  # noqa: E712
    assert listed["effective_input"] == listed["input_allowed"] == True  # noqa: E712

    dash_listed = {s["name"]: s for s in service.dashboard_list_sessions()["sessions"]}[session]
    assert dash_listed["effective_read"] is True
    assert dash_listed["effective_input"] is True
    assert dash_listed["allowed"] is False


def test_ungranted_session_reports_and_behaves_consistently_denied(tmp_path, tmux_session_factory):
    session = tmux_session_factory("newsession-never-granted", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path)
    listed = {s["name"]: s for s in service.terminal_list_sessions()["sessions"]}[session]
    assert listed["read_allowed"] is False
    assert listed["input_allowed"] is False
    assert listed["effective_read"] is False
    assert listed["effective_input"] is False
    assert service.terminal_status(session)["error"] == "ACCESS_DENIED"
    assert service.terminal_send_text(session, "x")["error"] == "ACCESS_DENIED"


# ---------------------------------------------------------------------------
# status/capture/tail/input_context succeed according to the read grant
# ---------------------------------------------------------------------------


def test_read_only_grant_permits_all_read_tools(tmp_path, tmux_session_factory):
    service, session = _granted_session(tmp_path, tmux_session_factory, "newsession-read-tools",
                                        grant_input=False)
    assert "error" not in service.terminal_status(session)
    assert "error" not in service.terminal_capture(session)
    assert "error" not in service.terminal_tail(session)
    ctx = service.terminal_input_context(session=session)
    assert "error" not in ctx
    assert ctx["effective_input"] is False  # read grant alone -- see below


# ---------------------------------------------------------------------------
# bind succeeds only with appropriate grant(s)
# ---------------------------------------------------------------------------


def test_bind_succeeds_with_read_grant_binds_readable_not_sendable(tmp_path, tmux_session_factory):
    service, session = _granted_session(tmp_path, tmux_session_factory, "newsession-bind-read-only",
                                        grant_input=False)
    bound = service.terminal_bind("bind-read-only", session, read_enabled=True, input_enabled=True)
    assert "error" not in bound
    assert "error" not in service.terminal_tail_bound("bind-read-only")
    # The binding's own input_enabled flag was accepted (unaffected stored
    # intent, exactly as for a statically-allowed session), but an actual
    # send still requires the underlying grant to include input too.
    sent = service.terminal_send_bound("bind-read-only", "x", press_enter=True)
    assert sent["error"] == "ACCESS_DENIED"


def test_bind_denied_for_a_session_with_no_grant_at_all(tmp_path, tmux_session_factory):
    session = tmux_session_factory("newsession-bind-denied", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path)
    assert service.terminal_bind("bind-denied", session)["error"] == "ACCESS_DENIED"


# ---------------------------------------------------------------------------
# send_text dry_run WOULD_SEND / actual send (disposable session only)
# ---------------------------------------------------------------------------


def test_send_text_dry_run_would_send_when_input_granted(tmp_path, tmux_session_factory):
    service, session = _granted_session(tmp_path, tmux_session_factory, "newsession-dryrun")
    result = service.terminal_send_text(session, "hello", press_enter=True, dry_run=True)
    assert result["would_send"] is True
    assert result["dry_run"] is True
    assert "error" not in result


def test_real_send_only_against_a_disposable_granted_session(tmp_path, tmux_session_factory):
    # Never promptflow or any other real/attached production session --
    # this is a fresh, disposable tmux session created and destroyed only
    # for this test.
    service, session = _granted_session(tmp_path, tmux_session_factory, "newsession-real-send")
    result = service.terminal_send_text(session, "hotfix-test-value", press_enter=True)
    assert result.get("sent") is True
    assert result.get("delivery_state") == "SUBMIT_CONFIRMED"
    time.sleep(0.3)
    assert "GOT=hotfix-test-value" in service.terminal_tail(session)["output"]


# ---------------------------------------------------------------------------
# Read grant does not imply input
# ---------------------------------------------------------------------------


def test_read_only_grant_cannot_send(tmp_path, tmux_session_factory):
    service, session = _granted_session(tmp_path, tmux_session_factory, "newsession-read-only-no-send",
                                        grant_input=False)
    assert service.terminal_send_text(session, "x")["error"] == "ACCESS_DENIED"
    assert service.terminal_input_context(session=session)["effective_input"] is False


# ---------------------------------------------------------------------------
# Input grant cannot bypass the global terminal_input switch
# ---------------------------------------------------------------------------


def test_input_grant_cannot_bypass_global_terminal_input_disabled(tmp_path, tmux_session_factory):
    # An input grant issued while the global switch is on must NOT let a
    # later-flipped-off global switch be bypassed -- the global gate is
    # checked independently of, and in addition to, the grant.
    session = tmux_session_factory("newsession-global-off", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    grants_path = tmp_path / "grants.db"
    bindings_path = tmp_path / "bindings.db"
    service_on = TerminalService(_config(terminal_input=True), grants=SessionGrantStore(grants_path),
                                 audit=AuditStore(tmp_path / "audit.db"), bindings=BindingStore(bindings_path))
    assert service_on.grant_session_read(session, True, granted_by="t").get("read_enabled") is True
    assert service_on.grant_session_input(session, True, granted_by="t").get("input_enabled") is True

    service_off = TerminalService(_config(terminal_input=False), grants=SessionGrantStore(grants_path),
                                  audit=AuditStore(tmp_path / "audit.db"), bindings=BindingStore(bindings_path))
    result = service_off.terminal_send_text(session, "x")
    assert result["error"] == "INPUT_DISABLED"
    listed = {s["name"]: s for s in service_off.terminal_list_sessions()["sessions"]}[session]
    assert listed["input_allowed"] is False  # global gate reflected in discovery too
    assert listed["input_granted"] is True  # the grant itself is still active, just globally gated


# ---------------------------------------------------------------------------
# Denylisted session remains denied despite grants
# ---------------------------------------------------------------------------


def test_denylisted_session_stays_denied_even_with_a_grant(tmp_path, tmux_session_factory):
    # A name with no SENSITIVE_SESSION_WORDS collision (that is a
    # separate, already-covered gate -- see grant_session_read/_input's
    # own SENSITIVE_SESSION_NOT_GRANTABLE checks) -- isolates the
    # input_policy.denied_session_patterns floor specifically.
    session = tmux_session_factory("newsession-quarantined-target", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path, denied_session_patterns=("newsession-quarantined-*",))
    # grant_session_input itself already refuses a denylisted target...
    assert service.grant_session_read(session, True, granted_by="t").get("read_enabled") is True
    result = service.grant_session_input(session, True, granted_by="t")
    assert result.get("error") == "ACCESS_DENIED"
    # ...and even if a grant row existed anyway (defense in depth -- the
    # canonical decision itself also checks the deny floor, not just the
    # grant-creation path), input must still be refused.
    assert service.terminal_send_text(session, "x")["error"] in ("ACCESS_DENIED", "GRANT_REQUIRED")
    listed = {s["name"]: s for s in service.terminal_list_sessions()["sessions"]}[session]
    assert listed["input_allowed"] is False


# ---------------------------------------------------------------------------
# Revoked grant denied immediately
# ---------------------------------------------------------------------------


def test_revoked_input_grant_fails_closed_immediately(tmp_path, tmux_session_factory):
    service, session = _granted_session(tmp_path, tmux_session_factory, "newsession-revoke-input")
    assert "error" not in service.terminal_send_text(session, "x", dry_run=True)
    revoked = service.grant_session_input(session, False, granted_by="t")
    assert revoked["input_enabled"] is False
    assert service.terminal_send_text(session, "x")["error"] == "ACCESS_DENIED"
    # Read remains -- revoking input alone never touches read.
    assert "error" not in service.terminal_status(session)


def test_revoked_read_grant_also_revokes_input_and_fails_both_closed(tmp_path, tmux_session_factory):
    service, session = _granted_session(tmp_path, tmux_session_factory, "newsession-revoke-read")
    service.grant_session_read(session, False, granted_by="t")
    assert service.terminal_status(session)["error"] == "ACCESS_DENIED"
    assert service.terminal_send_text(session, "x")["error"] == "ACCESS_DENIED"
    listed = {s["name"]: s for s in service.terminal_list_sessions()["sessions"]}[session]
    assert listed["read_allowed"] is False
    assert listed["input_allowed"] is False


# ---------------------------------------------------------------------------
# Recreated same-name session/pane identity safety
# ---------------------------------------------------------------------------


def test_recreated_same_name_session_does_not_inherit_input_grant(tmp_path, tmux_session_factory):
    import subprocess

    service, session = _granted_session(tmp_path, tmux_session_factory, "newsession-recreate-identity")
    assert "error" not in service.terminal_send_text(session, "x", dry_run=True)

    subprocess.run(["tmux", "kill-session", "-t", session], check=True)
    time.sleep(0.2)
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "bash -lc 'sleep 20'"], check=True)
    time.sleep(0.3)

    result = service.terminal_send_text(session, "x", dry_run=True)
    assert result["error"] == "IDENTITY_MISMATCH"
    # Read is unaffected by identity pinning (read grants aren't pinned to
    # a specific pane the way input grants are) -- still works for the
    # new pane under the same name.
    assert "error" not in service.terminal_status(session)
    subprocess.run(["tmux", "kill-session", "-t", session], check=False)


# ---------------------------------------------------------------------------
# Direct / bound / dashboard / Supervisor authorization parity
# ---------------------------------------------------------------------------


def test_direct_bound_and_granted_paths_agree_for_the_same_session(tmp_path, tmux_session_factory):
    service, session = _granted_session(tmp_path, tmux_session_factory, "newsession-parity")
    bound = service.terminal_bind("parity-bind", session, read_enabled=True, input_enabled=True)
    assert "error" not in bound

    direct = service.terminal_send_text(session, "y", dry_run=True)
    via_bound = service.terminal_send_bound("parity-bind", "y", dry_run=True)
    via_granted = service.terminal_send_text_granted(session, "y", dry_run=True)
    for result in (direct, via_bound, via_granted):
        assert result.get("would_send") is True, result


def test_supervisor_watch_accepts_a_granted_session(tmp_path, tmux_session_factory):
    service, session = _granted_session(tmp_path, tmux_session_factory, "newsession-supervisor-watch",
                                        grant_input=False)
    supervisor = SupervisorService(service, SupervisorStore(tmp_path / "supervisor.db"))
    result = supervisor.watch(session=session)
    assert "error" not in result
    assert result["watch_key"] == f"session:{session}"
    events = supervisor.run_once()["events"]
    assert events  # a real poll actually succeeded (read-authorized), not silently skipped


def test_supervisor_watch_still_denied_for_an_ungranted_session(tmp_path, tmux_session_factory):
    session = tmux_session_factory("newsession-supervisor-denied", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path)
    supervisor = SupervisorService(service, SupervisorStore(tmp_path / "supervisor.db"))
    assert supervisor.watch(session=session)["error"] == "ACCESS_DENIED"


def test_dashboard_and_direct_paths_agree_after_grant(tmp_path, tmux_session_factory):
    from starlette.testclient import TestClient

    from terminal_mcp.dashboard import register_dashboard
    from terminal_mcp.mcp_app import build_mcp

    session = tmux_session_factory("newsession-dashboard-parity", "bash -lc 'sleep 20'")
    time.sleep(0.2)
    service = _service(tmp_path)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})

    client.post("/dashboard/api/session/grant-read", json={"name": session, "enabled": True})
    client.post("/dashboard/api/session/grant-input", json={"name": session, "enabled": True})

    dash_detail = client.get("/dashboard/api/session", params={"name": session}).json()
    assert dash_detail["input_allowed"] is True
    assert "error" not in service.terminal_status(session)
    assert service.terminal_send_text(session, "x", dry_run=True).get("would_send") is True
