"""Persistent Session Registry wired into TerminalService/core.py and the
dashboard's own routes -- session_registry.py's own store is tested in
isolation in tests/test_session_registry.py; this file is the "does it
actually get populated/reconciled/reopened for real, through a real
tmux session" integration layer, matching task item 12/13's own required
scenarios.
"""
from __future__ import annotations

import subprocess
import time

import pytest
from starlette.testclient import TestClient

from terminal_mcp.audit import AuditStore
from terminal_mcp.bindings import BindingStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.killed_sessions import KilledSessionStore
from terminal_mcp.lease import PaneLeaseStore
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.session_registry import SessionRegistryStore


def _config(tmp_path, roots=None) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True),
        allowed_session_patterns=("reg-*",),
        max_capture_lines=200,
        default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("reg-*",)),
        session_lifecycle=SessionLifecycleConfig(
            enabled=True, allowed_cwd_roots=(str(tmp_path),) if roots is None else roots,
            protected_sessions=("terminal-mcp",),
            launch_commands=(("claude", "true"), ("codex", "true")),
        ),
    )


def _service(tmp_path, roots=None) -> TerminalService:
    # Every store isolated to tmp_path -- see the multi-node grant fix's
    # own regression finding (this session's earlier work) for why a
    # bare TerminalService(config) with no overrides is a real, live
    # production-state leak risk, not a hypothetical one.
    return TerminalService(
        _config(tmp_path, roots=roots),
        bindings=BindingStore(tmp_path / "bindings.db"),
        audit=AuditStore(tmp_path / "audit.db"),
        grants=SessionGrantStore(tmp_path / "grants.db"),
        leases=PaneLeaseStore(tmp_path / "leases.db"),
        killed_sessions=KilledSessionStore(tmp_path / "killed_sessions.db"),
        session_registry=SessionRegistryStore(tmp_path / "session_registry.db"),
    )


def _tmux(*args, check=True):
    return subprocess.run(["tmux", *args], check=check, capture_output=True, text=True, timeout=10)


@pytest.fixture
def tmux_cleanup():
    created: list[str] = []
    yield created
    for name in created:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False, capture_output=True)


# -- reconcile: create -> list picks it up ------------------------------------

def test_create_session_reconciles_into_an_active_registry_record(tmp_path, tmux_cleanup):
    service = _service(tmp_path)
    tmux_cleanup.append("reg-create1")
    result = service.terminal_create_session("reg-create1", "shell", str(tmp_path))
    time.sleep(0.3)  # let tmux's own pane_current_path/command tracking settle
    assert result["state"] == "READY"
    listing = service.terminal_registry_list()
    record = next(r for r in listing["records"] if r["session_name"] == "reg-create1")
    assert record["status"] == "ACTIVE"
    assert record["backend_type"] == "tmux"
    assert record["cwd"] == str(tmp_path)


# -- kill keeps the record (task item 12: "kill keeps record") ---------------

def test_kill_session_keeps_registry_record_as_killed(tmp_path, tmux_cleanup):
    service = _service(tmp_path)
    service.terminal_create_session("reg-kill1", "shell", str(tmp_path))
    time.sleep(0.3)  # let tmux's own pane_current_path/command tracking settle
    result = service.terminal_kill_session("reg-kill1", "reg-kill1", requested_by="tester")
    assert "error" not in result
    record = service.terminal_registry_get("reg-kill1")
    assert record["status"] == "KILLED"
    assert record["recoverable"] is True  # shell + no agent_type issue -> always reopenable
    assert record["cwd"] == str(tmp_path)


# -- tmux disappears out-of-band keeps the record as MISSING -----------------

def test_session_vanishing_out_of_band_is_reconciled_to_missing(tmp_path, tmux_cleanup):
    service = _service(tmp_path)
    service.terminal_create_session("reg-vanish1", "shell", str(tmp_path))
    time.sleep(0.3)  # let tmux's own pane_current_path/command tracking settle
    service.terminal_registry_list()  # first reconcile pass -- record now ACTIVE
    # Out-of-band kill -- NEVER through terminal_kill_session, simulating a
    # tmux-server restart or an operator running `tmux kill-session`
    # directly (exactly the real mesflow/promptflow incident this whole
    # feature exists to make recoverable-from).
    _tmux("kill-session", "-t", "reg-vanish1", check=False)
    listing = service.terminal_registry_list()
    record = next(r for r in listing["records"] if r["session_name"] == "reg-vanish1")
    assert record["status"] == "MISSING"
    assert record["cwd"] == str(tmp_path)  # metadata survives, only the runtime is gone
    assert record["recoverable"] is True


# -- reopen uses exact stored cwd/agent_type ----------------------------------

def test_registry_reopen_recreates_with_exact_saved_cwd_and_agent(tmp_path, tmux_cleanup):
    service = _service(tmp_path)
    tmux_cleanup.append("reg-reopen1")
    project_dir = tmp_path / "myproj"
    project_dir.mkdir()
    service.terminal_create_session("reg-reopen1", "shell", str(project_dir))
    time.sleep(0.3)  # let tmux's own pane_current_path/command tracking settle
    service.terminal_registry_list()
    _tmux("kill-session", "-t", "reg-reopen1", check=False)
    service.terminal_registry_list()
    assert service.terminal_registry_get("reg-reopen1")["status"] == "MISSING"

    result = service.terminal_registry_reopen("reg-reopen1")
    assert "error" not in result
    assert result["recreated_from_registry"] is True
    time.sleep(0.3)  # let tmux's own pane_current_path tracking settle post-reopen
    info = service.tmux.get_session("reg-reopen1")
    assert info is not None
    assert info.pane_current_path == str(project_dir)
    assert service.terminal_registry_get("reg-reopen1")["status"] == "ACTIVE"


def test_registry_reopen_without_enough_metadata_is_refused(tmp_path, tmux_cleanup):
    service = _service(tmp_path)
    # upsert_manual with agent_type "codex" and no cwd -- not shell, so
    # incomplete (matches killed_sessions.py's own "shell needs no cwd"
    # exception exactly).
    service.session_registry.upsert_manual("local", "reg-incomplete1", status="MISSING", agent_type="codex")
    result = service.terminal_registry_reopen("reg-incomplete1")
    assert result["error"] == "REOPEN_METADATA_INCOMPLETE"
    assert "cwd" in result["missing"]


def test_registry_reopen_of_unknown_record_is_not_found(tmp_path):
    service = _service(tmp_path)
    result = service.terminal_registry_reopen("reg-ghost-never-existed")
    assert result["error"] == "REGISTRY_RECORD_NOT_FOUND"


# -- permanent delete is a separate action from Kill --------------------------

def test_purge_refuses_an_active_session(tmp_path, tmux_cleanup):
    service = _service(tmp_path)
    tmux_cleanup.append("reg-active1")
    service.terminal_create_session("reg-active1", "shell", str(tmp_path))
    time.sleep(0.3)  # let tmux's own pane_current_path/command tracking settle
    service.terminal_registry_list()
    result = service.terminal_registry_purge("reg-active1", purged_by="tester")
    assert result["error"] == "SESSION_STILL_ACTIVE"
    # Kill (a completely separate action) does NOT purge the registry row --
    # it stays, just as KILLED, exactly like killed_sessions.py's own
    # record does.
    service.terminal_kill_session("reg-active1", "reg-active1")
    assert service.terminal_registry_get("reg-active1")["status"] == "KILLED"


def test_purge_after_kill_removes_it_from_the_recoverable_list(tmp_path, tmux_cleanup):
    service = _service(tmp_path)
    service.terminal_create_session("reg-purge1", "shell", str(tmp_path))
    time.sleep(0.3)  # let tmux's own pane_current_path/command tracking settle
    service.terminal_kill_session("reg-purge1", "reg-purge1")
    result = service.terminal_registry_purge("reg-purge1", purged_by="operator@example.com")
    assert result == {"session": "reg-purge1", "purged": True}
    record = service.terminal_registry_get("reg-purge1")
    assert record["status"] == "DELETED"
    assert record["recoverable"] is False
    recoverable = service.terminal_registry_list(recoverable_only=True)["records"]
    assert "reg-purge1" not in {r["session_name"] for r in recoverable}


# -- backfill: a session created OUTSIDE terminal-mcp (raw tmux) -------------

def test_session_created_outside_terminal_mcp_is_backfilled_on_next_reconcile(tmp_path, tmux_cleanup):
    service = _service(tmp_path)
    tmux_cleanup.append("reg-external1")
    project_dir = tmp_path / "external-proj"
    project_dir.mkdir()
    # A real session this project never created -- e.g. an operator's own
    # `tmux new-session` directly on the host (task item 8's own scenario).
    _tmux("new-session", "-d", "-s", "reg-external1", "-c", str(project_dir), "bash")
    time.sleep(0.3)  # let tmux's own pane_current_path/command tracking settle
    listing = service.terminal_registry_list()
    record = next(r for r in listing["records"] if r["session_name"] == "reg-external1")
    assert record["status"] == "ACTIVE"
    assert record["cwd"] == str(project_dir)
    assert record["agent_type"] == "shell"  # classified from pane_current_command ("bash")
    assert record["metadata_complete"] is True


# -- search by project path/repo/name (task item 9) --------------------------

def test_registry_search_finds_a_lost_session_by_project_path(tmp_path, tmux_cleanup):
    service = _service(tmp_path)
    project_dir = tmp_path / "quan_ly_ban_hang_proj"
    project_dir.mkdir()
    service.session_registry.upsert_manual("local", "quan_ly_ban_hang", status="MISSING",
                                           cwd=str(project_dir), agent_type="claude", backfill_project=False)
    by_name = service.terminal_registry_search("quan_ly_ban_hang")
    assert {r["session_name"] for r in by_name["records"]} == {"quan_ly_ban_hang"}
    by_path = service.terminal_registry_search(str(project_dir))
    assert {r["session_name"] for r in by_path["records"]} == {"quan_ly_ban_hang"}
    empty = service.terminal_registry_search("")
    assert empty["records"] == []


# -- restart persistence ------------------------------------------------------

def test_registry_records_survive_a_fresh_terminalservice_same_paths(tmp_path, tmux_cleanup):
    service1 = _service(tmp_path)
    tmux_cleanup.append("reg-restart1")
    service1.terminal_create_session("reg-restart1", "shell", str(tmp_path))
    time.sleep(0.3)  # let tmux's own pane_current_path/command tracking settle
    service1.terminal_kill_session("reg-restart1", "reg-restart1")
    # A brand new TerminalService (simulating a process restart) pointed
    # at the SAME store files.
    service2 = TerminalService(
        _config(tmp_path),
        bindings=BindingStore(tmp_path / "bindings.db"), audit=AuditStore(tmp_path / "audit.db"),
        grants=SessionGrantStore(tmp_path / "grants.db"), leases=PaneLeaseStore(tmp_path / "leases.db"),
        killed_sessions=KilledSessionStore(tmp_path / "killed_sessions.db"),
        session_registry=SessionRegistryStore(tmp_path / "session_registry.db"),
    )
    record = service2.terminal_registry_get("reg-restart1")
    assert record["status"] == "KILLED"
    assert record["cwd"] == str(tmp_path)


# -- grant snapshot is informational, kept in sync ----------------------------

def test_grant_read_input_are_reflected_in_the_registry_snapshot(tmp_path, tmux_cleanup):
    service = _service(tmp_path)
    tmux_cleanup.append("reg-grant1")
    service.terminal_create_session("reg-grant1", "shell", str(tmp_path))
    time.sleep(0.3)  # let tmux's own pane_current_path/command tracking settle
    service.terminal_registry_list()
    service.grant_session_read("reg-grant1", True)
    service.grant_session_input("reg-grant1", True)
    record = service.terminal_registry_get("reg-grant1")
    assert record["read_granted"] is True and record["input_granted"] is True


# -- dashboard HTTP routes ------------------------------------------------------

def test_dashboard_registry_routes_list_reopen_purge(tmp_path, tmux_cleanup):
    service = _service(tmp_path)
    tmux_cleanup.append("reg-http1")
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})

    service.terminal_create_session("reg-http1", "shell", str(tmp_path))
    time.sleep(0.3)  # let tmux's own pane_current_path/command tracking settle
    service.terminal_kill_session("reg-http1", "reg-http1")

    listing = client.get("/dashboard/api/registry")
    assert listing.status_code == 200
    names = {r["session_name"] for r in listing.json()["records"]}
    assert "reg-http1" in names

    search = client.get("/dashboard/api/registry/search", params={"q": "reg-http1"})
    assert search.status_code == 200
    assert {r["session_name"] for r in search.json()["records"]} == {"reg-http1"}

    reopened = client.post("/dashboard/api/registry/reopen", json={"session_name": "reg-http1"})
    assert reopened.status_code == 200
    assert reopened.json().get("error") is None

    purge_while_active = client.post("/dashboard/api/registry/purge", json={"session_name": "reg-http1"})
    assert purge_while_active.json()["error"] == "SESSION_STILL_ACTIVE"

    service.terminal_kill_session("reg-http1", "reg-http1")
    purged = client.post("/dashboard/api/registry/purge", json={"session_name": "reg-http1"})
    assert purged.status_code == 200
    assert purged.json() == {"session": "reg-http1", "purged": True}


def test_dashboard_registry_mutation_routes_require_origin_csrf_defense(tmp_path):
    service = _service(tmp_path)
    server = build_mcp(service)
    register_dashboard(server, service)
    no_origin_client = TestClient(server.streamable_http_app())
    r1 = no_origin_client.post("/dashboard/api/registry/reopen", json={"session_name": "x"})
    assert r1.status_code == 403
    r2 = no_origin_client.post("/dashboard/api/registry/purge", json={"session_name": "x"})
    assert r2.status_code == 403
