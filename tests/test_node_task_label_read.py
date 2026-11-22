"""Authenticated read-only task labels: no task cross-session or prompt leakage."""
from __future__ import annotations

import time

from starlette.testclient import TestClient

from terminal_mcp.audit import AuditStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.node_agent import build_node_agent
from terminal_mcp.node_client import LocalNodeClient, RemoteNodeClient


TOKEN = "unit-test-bearer-token"


def _client(tmp_path):
    config = AppConfig(PermissionsConfig(True, True), ("*",), 50, 20,
                       InputPolicyConfig(allowed_session_patterns=("*",)))
    terminal = TerminalService(config, audit=AuditStore(tmp_path / "audit.db"))
    live = {"exists": True, "allowed": True, "state": "IDLE"}
    born = [time.time() - 60]
    terminal.terminal_status = lambda name: dict(live) if name == "agent-worker" else {
        "exists": False, "allowed": False}
    terminal.terminal_list_sessions = lambda: {"sessions": [{
        "name": "agent-worker", "created": born[0], "effective_read": live["allowed"]}]}
    app = build_node_agent(node_id="test", terminal=terminal, token=TOKEN)
    return TestClient(app), terminal, live, born


def _url():
    return "/v1/sessions/agent-worker/task-label"


def test_auth_and_exact_read_only_title(tmp_path):
    client, terminal, _, _ = _client(tmp_path)
    terminal.audit.set_task_label(node_id="local", session="agent-worker",
                                  summary="Verify new OTA firmware", source="title",
                                  task_id="task_421")
    before = terminal.audit.get_task_label("local", "agent-worker")
    assert client.get(_url()).status_code == 401
    assert client.get(_url(), headers={"Authorization": "Bearer bad"}).status_code == 401
    res = client.get(_url(), headers={"Authorization": "Bearer " + TOKEN})
    assert res.status_code == 200
    label = res.json()["label"]
    assert label["summary"] == "Verify new OTA firmware"
    assert label["source"] == "title" and label["task_id"] == "task_421"
    assert "request_key" not in label
    assert terminal.audit.get_task_label("local", "agent-worker") == before


def test_prompt_derived_and_unknown_name_are_not_readable(tmp_path):
    client, terminal, _, _ = _client(tmp_path)
    terminal.audit.set_task_label(node_id="local", session="agent-worker",
                                  summary="Sensitive prompt about customer account", source="prompt_fallback")
    auth = {"Authorization": "Bearer " + TOKEN}
    assert client.get(_url(), headers=auth).json()["label"] is None
    assert client.get("/v1/sessions/not-the-owner/task-label", headers=auth).json()["label"] is None


def test_denial_and_recreated_same_named_session_fail_closed(tmp_path):
    client, terminal, live, born = _client(tmp_path)
    terminal.audit.set_task_label(node_id="local", session="agent-worker",
                                  summary="Maintain view-only monitor", source="title")
    auth = {"Authorization": "Bearer " + TOKEN}
    live["allowed"] = False
    assert client.get(_url(), headers=auth).json()["label"] is None
    live["allowed"] = True
    live["exists"] = False
    assert client.get(_url(), headers=auth).json()["label"] is None
    live["exists"] = True
    born[0] = time.time() + 20
    assert client.get(_url(), headers=auth).json()["label"] is None


def test_remote_client_reads_bearer_authenticated_task_label(tmp_path):
    client, terminal, _, _ = _client(tmp_path)
    terminal.audit.set_task_label(node_id="local", session="agent-worker",
                                  summary="CrommaCode status verification", source="title")
    remote = RemoteNodeClient("http://agent.test", TOKEN)
    seen = []
    def request(method, path, *, params=None, body=None, timeout_seconds=None):
        seen.append((method, path, timeout_seconds))
        response = client.request(method, path,
                                  headers={"Authorization": "Bearer " + TOKEN})
        assert response.status_code == 200
        return response.json()
    remote._request = request
    result = remote.get_task_label("agent-worker", timeout_seconds=2.0)
    assert result["label"]["summary"] == "CrommaCode status verification"
    assert seen == [("GET", _url(), 2.0)]


def test_iso_created_times_from_live_node_listing(tmp_path):
    from datetime import datetime, timedelta, timezone
    client, terminal, _, born = _client(tmp_path)
    terminal.audit.set_task_label(node_id="local", session="agent-worker",
                                  summary="Verify project observability", source="title")
    headers = {"Authorization": "Bearer " + TOKEN}
    born[0] = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    assert client.get(_url(), headers=headers).json()["label"]["summary"] == "Verify project observability"
    born[0] = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    assert client.get(_url(), headers=headers).json()["label"] is None
