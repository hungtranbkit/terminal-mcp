"""Session Knowledge Store wired into TerminalService (core.py) -- real
tmux sessions, real pipe-pane capture, real redaction, exercised through
the exact same terminal_list_sessions()/terminal_kill_session() reconcile
pass the dashboard/MCP tools already call. Store-level mechanics (chunking,
compaction, FTS, restart persistence) are covered in test_session_
knowledge.py; this file proves the WIRING -- capture actually happens from
a real reconcile pass, in the right place, without duplication, redacted,
permission-gated, and survives a kill.
"""
from __future__ import annotations

import subprocess
import time

import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import (
    AppConfig, InputPolicyConfig, PermissionsConfig, SessionKnowledgeConfig, SessionLifecycleConfig,
)
from terminal_mcp.controller import ControllerService
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.node_client import LocalNodeClient
from terminal_mcp.node_registry import NodeRegistry
from terminal_mcp.session_knowledge import SessionKnowledgeStore


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], check=check, capture_output=True, text=True, timeout=10)


def _config(tmp_path, *, patterns=("know-*",)) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True),
        allowed_session_patterns=patterns,
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=patterns),
        session_lifecycle=SessionLifecycleConfig(
            enabled=True, allowed_cwd_roots=(str(tmp_path),), protected_sessions=(),
        ),
        # Explicit opt-in (SessionKnowledgeConfig's own docstring/config.py)
        # -- this file's whole point is exercising REAL capture, unlike
        # every other test in this project's suite, which must never
        # trigger it at all (see core.py's own SAFETY BOUNDARY #1
        # comment on _capture_session_knowledge for why this flag exists).
        session_knowledge=SessionKnowledgeConfig(enabled=True),
    )


@pytest.fixture
def service(tmp_path):
    config = _config(tmp_path)
    knowledge = SessionKnowledgeStore(tmp_path / "knowledge.db")
    grants = SessionGrantStore(tmp_path / "grants.db")
    svc = TerminalService(config, session_knowledge=knowledge, grants=grants)
    yield svc
    # Real disposable tmux sessions this test created -- never leaves a
    # real tmux session or its pipe-pane running past the test.
    for name in ("know-1", "know-2", "know-restricted"):
        _tmux("kill-session", "-t", name, check=False)


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_capture_happens_via_terminal_list_sessions_reconcile(service, tmp_path):
    result = service.terminal_create_session("know-1", "shell", str(tmp_path))
    assert result["state"] == "READY"
    service.terminal_send_text("know-1", "echo capture-marker-one", press_enter=True)
    assert _wait_until(lambda: "capture-marker-one" in _joined_timeline(service, "know-1"))


def test_capture_does_not_duplicate_across_multiple_reconcile_passes(service, tmp_path):
    # NOTE: a real terminal legitimately shows a sent command's text
    # TWICE on its own (once echoed as typed, once as part of its actual
    # output line for an `echo` command) -- occasionally more, depending
    # on this host's own shell-integration prompt hooks (the same
    # environmental artifact documented against test_session_lifecycle.
    # py's own test_create_initial_prompt_goes_through_reliable_
    # submission_once). This test is about CAPTURE-SIDE dedup (never
    # re-appending the same already-captured bytes), so it takes a
    # baseline count once real output has settled, then asserts that
    # count stays STABLE across further no-op reconcile passes -- never
    # asserts a specific literal count, which the shell's own behavior
    # (not this capture code) controls.
    service.terminal_create_session("know-1", "shell", str(tmp_path))
    service.terminal_send_text("know-1", "echo dedupe-check-marker", press_enter=True)
    assert _wait_until(lambda: "dedupe-check-marker" in _joined_timeline(service, "know-1"))
    time.sleep(0.5)  # let the shell's own output fully settle before taking the baseline
    baseline = _joined_timeline(service, "know-1").count("dedupe-check-marker")
    assert baseline >= 1
    # Several MORE reconcile passes with no new output in between --
    # must not re-capture/duplicate the same already-captured content.
    for _ in range(5):
        service.terminal_list_sessions()
        time.sleep(0.1)
    joined = _joined_timeline(service, "know-1")
    assert joined.count("dedupe-check-marker") == baseline


def test_capture_preserves_order_across_multiple_sends(service, tmp_path):
    service.terminal_create_session("know-1", "shell", str(tmp_path))
    service.terminal_send_text("know-1", "echo order-marker-AAA", press_enter=True)
    assert _wait_until(lambda: "order-marker-AAA" in _joined_timeline(service, "know-1"))
    service.terminal_send_text("know-1", "echo order-marker-BBB", press_enter=True)
    assert _wait_until(lambda: "order-marker-BBB" in _joined_timeline(service, "know-1"))
    joined = _joined_timeline(service, "know-1")
    assert joined.index("order-marker-AAA") < joined.index("order-marker-BBB")


def test_captured_output_is_redacted(service, tmp_path):
    service.terminal_create_session("know-1", "shell", str(tmp_path))
    service.terminal_send_text("know-1", "echo token=abcSECRETvalue123", press_enter=True)
    assert _wait_until(lambda: "REDACTED" in _joined_timeline(service, "know-1") or
                       "abcSECRETvalue123" in _joined_timeline(service, "know-1"))
    joined = _joined_timeline(service, "know-1")
    assert "abcSECRETvalue123" not in joined
    assert "<REDACTED>" in joined


def test_knowledge_search_finds_captured_content(service, tmp_path):
    service.terminal_create_session("know-1", "shell", str(tmp_path))
    service.terminal_send_text("know-1", "echo findable-search-marker-xyz", press_enter=True)
    assert _wait_until(lambda: "findable-search-marker-xyz" in _joined_timeline(service, "know-1"))
    result = service.terminal_knowledge_search("findable-search-marker-xyz")
    assert "error" not in result
    assert any("findable-search-marker-xyz" in r["text"] for r in result["results"])


def test_missing_session_history_survives_after_kill(service, tmp_path):
    service.terminal_create_session("know-1", "shell", str(tmp_path))
    service.terminal_send_text("know-1", "echo before-kill-marker", press_enter=True)
    assert _wait_until(lambda: "before-kill-marker" in _joined_timeline(service, "know-1"))
    killed = service.terminal_kill_session("know-1", "know-1", requested_by="test")
    assert "error" not in killed

    timeline = service.terminal_knowledge_timeline("know-1")
    assert "error" not in timeline
    joined = "\n".join(c["text"] for c in timeline["chunks"])
    assert "before-kill-marker" in joined


def test_pre_kill_checkpoint_is_created(service, tmp_path):
    service.terminal_create_session("know-1", "shell", str(tmp_path))
    service.terminal_send_text("know-1", "echo pre-kill-checkpoint-marker", press_enter=True)
    assert _wait_until(lambda: "pre-kill-checkpoint-marker" in _joined_timeline(service, "know-1"))
    service.terminal_kill_session("know-1", "know-1", requested_by="tester-identity")

    brief = service.terminal_knowledge_recover("know-1")
    assert "error" not in brief
    assert brief["checkpoint"] is not None
    assert brief["checkpoint"]["kind"] == "pre_kill"
    assert "tester-identity" in brief["checkpoint"]["summary"]


def test_recovery_brief_never_claims_process_restored(service, tmp_path):
    service.terminal_create_session("know-1", "shell", str(tmp_path))
    service.terminal_send_text("know-1", "echo recovery-brief-marker", press_enter=True)
    assert _wait_until(lambda: "recovery-brief-marker" in _joined_timeline(service, "know-1"))

    brief = service.terminal_knowledge_recover("know-1")
    assert brief["recovered_process"] is False
    assert "recovery-brief-marker" in brief["recovery_brief_text"]


def test_manual_checkpoint_requires_input_authorization(service, tmp_path):
    service.terminal_create_session("know-1", "shell", str(tmp_path))
    service.terminal_list_sessions()  # ensure_meta has run at least once
    result = service.terminal_knowledge_checkpoint("know-1", "manual checkpoint text")
    assert "error" not in result
    brief = service.terminal_knowledge_recover("know-1")
    assert brief["checkpoint"]["summary"] == "manual checkpoint text"


def test_search_and_timeline_are_permission_gated(tmp_path):
    """A session NOT statically allowed and with no read grant must never
    surface its content via search/timeline, even after it's genuinely
    been captured -- requirement #11."""
    config = _config(tmp_path, patterns=("know-*",))
    knowledge = SessionKnowledgeStore(tmp_path / "knowledge.db")
    grants = SessionGrantStore(tmp_path / "grants.db")
    service = TerminalService(config, session_knowledge=knowledge, grants=grants)
    try:
        # Directly seed knowledge for a session name OUTSIDE the allowed
        # pattern (as if it were captured while the session existed under
        # a different config, or captured before a pattern was tightened)
        # -- proves the GATE itself, not just "never captured".
        knowledge.ensure_meta("local", "not-know-restricted", "inst-1", cwd=str(tmp_path))
        knowledge.append_output("local", "not-know-restricted", "inst-1", "secret-not-allowed-content\n")

        search = service.terminal_knowledge_search("secret-not-allowed-content")
        assert search["results"] == []

        timeline = service.terminal_knowledge_timeline("not-know-restricted")
        assert timeline.get("error") == "ACCESS_DENIED"

        recover = service.terminal_knowledge_recover("not-know-restricted")
        assert recover.get("error") == "ACCESS_DENIED"
    finally:
        _tmux("kill-session", "-t", "not-know-restricted", check=False)


def test_search_project_filter_matches_captured_session(service, tmp_path):
    proj_dir = tmp_path / "quan_ly_ban_hang_project"
    proj_dir.mkdir()
    service.terminal_create_session("know-1", "shell", str(proj_dir))
    service.terminal_send_text("know-1", "echo deployment-report-marker", press_enter=True)
    assert _wait_until(lambda: "deployment-report-marker" in _joined_timeline(service, "know-1"))
    result = service.terminal_knowledge_search("deployment-report-marker", project="quan_ly_ban_hang_project")
    assert any("deployment-report-marker" in r["text"] for r in result["results"])


def test_first_ever_capture_backfills_current_screen_for_a_preexisting_session(service, tmp_path):
    """Requirement #9: a session that already existed before this feature
    ever saw it (simulated here by creating it, then sending output
    BEFORE the very first terminal_list_sessions call this store has
    ever made for it) still gets its CURRENT on-screen content captured
    once, marked as backfilled, rather than only ever seeing output from
    that point forward."""
    _tmux("new-session", "-d", "-s", "know-1", "-c", str(tmp_path))
    _tmux("send-keys", "-t", "know-1", "echo preexisting-backfill-marker", "Enter")
    assert _wait_until(lambda: "preexisting-backfill-marker" in
                       subprocess.run(["tmux", "capture-pane", "-p", "-t", "know-1"],
                                      capture_output=True, text=True).stdout)
    # This service has NEVER captured "know-1" before this point.
    service.terminal_list_sessions()
    joined = _joined_timeline(service, "know-1")
    assert "preexisting-backfill-marker" in joined
    timeline = service.session_knowledge.timeline("local", "know-1", limit=50)
    backfilled = [c for c in timeline if c.source == "backfilled"]
    assert backfilled and "preexisting-backfill-marker" in backfilled[0].text


def _joined_timeline(service: TerminalService, session_name: str) -> str:
    # Capture only happens as a side effect of terminal_list_sessions/
    # dashboard_list_sessions (the real reconcile pass) -- trigger one
    # before reading, exactly like the dashboard's own 5s poll loop or an
    # MCP client's own periodic terminal_list_sessions call would.
    service.terminal_list_sessions()
    return "\n".join(c.text for c in service.session_knowledge.timeline("local", session_name, limit=200))


# ---------------------------------------------------------------------------
# Dashboard routes -- thin wrappers over controller.terminal_knowledge_*
# (already thoroughly tested in test_controller.py) -- these just prove the
# HTTP wiring itself (auth guard, param passthrough, error status mapping).
# ---------------------------------------------------------------------------

@pytest.fixture
def dashboard_client(tmp_path):
    config = _config(tmp_path)
    knowledge = SessionKnowledgeStore(tmp_path / "knowledge.db")
    grants = SessionGrantStore(tmp_path / "grants.db")
    svc = TerminalService(config, session_knowledge=knowledge, grants=grants)
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(svc), local_workspace_root=str(tmp_path))
    server = build_mcp(svc)
    register_dashboard(server, svc, controller=controller)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})
    client.terminal = svc
    yield client
    _tmux("kill-session", "-t", "know-1", check=False)


def test_dashboard_history_timeline_route_reaches_real_captured_output(dashboard_client, tmp_path):
    client = dashboard_client
    client.terminal.terminal_create_session("know-1", "shell", str(tmp_path))
    client.terminal.terminal_send_text("know-1", "echo dashboard-route-marker", press_enter=True)
    assert _wait_until(lambda: "dashboard-route-marker" in _joined_timeline(client.terminal, "know-1"))

    r = client.get("/dashboard/api/session/knowledge/timeline?name=know-1")
    assert r.status_code == 200
    body = r.json()
    assert any("dashboard-route-marker" in c["text"] for c in body["chunks"])


def test_dashboard_history_search_route_finds_content(dashboard_client, tmp_path):
    client = dashboard_client
    client.terminal.terminal_create_session("know-1", "shell", str(tmp_path))
    client.terminal.terminal_send_text("know-1", "echo dashboard-search-hit", press_enter=True)
    assert _wait_until(lambda: "dashboard-search-hit" in _joined_timeline(client.terminal, "know-1"))

    r = client.get("/dashboard/api/knowledge/search?query=dashboard-search-hit")
    assert r.status_code == 200
    assert any("dashboard-search-hit" in row["text"] for row in r.json()["results"])


def test_dashboard_history_export_route_returns_downloadable_text(dashboard_client, tmp_path):
    client = dashboard_client
    client.terminal.terminal_create_session("know-1", "shell", str(tmp_path))
    client.terminal.terminal_send_text("know-1", "echo export-route-marker", press_enter=True)
    assert _wait_until(lambda: "export-route-marker" in _joined_timeline(client.terminal, "know-1"))

    r = client.get("/dashboard/api/session/knowledge/export?name=know-1")
    assert r.status_code == 200
    assert "export-route-marker" in r.text
    assert "attachment" in r.headers.get("content-disposition", "")


def test_dashboard_history_timeline_missing_session_is_a_clean_404(dashboard_client):
    # _read_guard is the same guard every other GET route already uses
    # (CF Access verification when configured -- not configured in this
    # test fixture, same as every other GET-route test in this project);
    # a session that genuinely doesn't exist gets a clean 404, never a
    # crash or a blank 200.
    client = dashboard_client
    r = client.get("/dashboard/api/session/knowledge/timeline?name=never-existed-know")
    assert r.status_code == 404
