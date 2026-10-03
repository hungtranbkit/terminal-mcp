"""Node-local mirror of the per-session current task label.

Production evidence (2026-10-03): the Dell host runs its own controller/UI
(node_id "local") AND a node-agent registered as "dell-linux" with the HP
controller. A label written only into the controller that took the request
never reached Dell's /app/live. These tests pin the mirror: every label
write/clear is also pushed to the node that owns the session, stored there
under "local", while the central controller copy stays as it was.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from terminal_mcp.audit import AuditStore
from terminal_mcp.compact_tools import CompactTerminalTools
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.controller import ControllerService
from terminal_mcp.core import TerminalService
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.live_sessions import LiveSessionMonitor
from terminal_mcp.node_agent import build_node_agent
from terminal_mcp.node_client import LocalNodeClient, NodeClientError, RemoteNodeClient
from terminal_mcp.host_metrics import NodeMetrics
from terminal_mcp.node_registry import NodeRegistry
from terminal_mcp.run_journal import RunJournalStore
from terminal_mcp.task_labels import NODE_LOCAL_LABEL_ID, TaskLabeler

TOKEN = "node-sync-token"
IDLE = {"exists": True, "state": "IDLE", "reason": "shell prompt is back"}
CONFIRMED = {"delivery_state": "SUBMIT_CONFIRMED", "press_enter": True, "enter_sent": True}


def _config() -> AppConfig:
    return AppConfig(PermissionsConfig(True, True), ("*",), 50, 20,
                     InputPolicyConfig(allowed_session_patterns=("*",)))


def _terminal(tmp_path, name: str) -> TerminalService:
    return TerminalService(_config(), audit=AuditStore(tmp_path / f"{name}-audit.db"),
                           grants=SessionGrantStore(tmp_path / f"{name}-grants.db"))


class FakeLocalClient(LocalNodeClient):
    """Real set/clear_task_label (writes TerminalService.audit); listing and
    sends are faked so no tmux is involved."""

    def __init__(self, terminal, sessions):
        super().__init__(terminal)
        self.sessions = sessions

    def list_sessions(self, *, timeout_seconds=None):
        return {"sessions": [{"name": name} for name in self.sessions]}

    def send_text(self, session, text, press_enter=False, dry_run=False, **kwargs):
        return {"session": session, **CONFIRMED}


class AgentBackedClient(RemoteNodeClient):
    """RemoteNodeClient whose HTTP goes to a real in-process node agent
    (build_node_agent + TestClient). Session listing and sends are faked."""

    def __init__(self, http: TestClient, sessions):
        super().__init__("http://agent", TOKEN)
        self.http = http
        self.sessions = sessions
        self.label_calls: list[tuple[str, str]] = []
        self.label_timeouts: list[float | None] = []

    def _request(self, method, path, *, params=None, body=None, timeout_seconds=None):
        if path.endswith("/task-label"):
            self.label_calls.append((method, path))
            self.label_timeouts.append(timeout_seconds)
        response = self.http.request(method, path, content=json.dumps(body) if body is not None else None,
                                     headers={"Authorization": f"Bearer {self._token}",
                                              "Content-Type": "application/json"})
        if response.status_code != 200:
            raise NodeClientError(f"{method} {path} -> HTTP {response.status_code}",
                                  http_status=response.status_code)
        return response.json()

    def list_sessions(self, *, timeout_seconds=None):
        return {"sessions": [{"name": name} for name in self.sessions]}

    def send_text(self, session, text, press_enter=False, dry_run=False, **kwargs):
        return {"session": session, **CONFIRMED}

    def health(self):
        return {"status": "ok"}

    def metrics(self):
        return {}


def _online(controller: ControllerService, node_id: str, client) -> None:
    controller.registry.register(node_id, display_name=node_id, hostname=f"{node_id}-host",
                                 endpoint=f"http://{node_id}")
    controller._clients[node_id] = client
    controller.registry.heartbeat(
        node_id,
        metrics=NodeMetrics(cpu_percent=5.0, load1=0.1, load5=0.1, load15=0.1, cpu_count=4,
                            ram_total_bytes=8_000_000_000, ram_used_bytes=1_000_000_000, ram_percent=12.5,
                            swap_total_bytes=0, swap_used_bytes=0, swap_percent=0.0,
                            disk_total_bytes=100_000_000_000, disk_used_bytes=1_000_000_000,
                            disk_free_bytes=99_000_000_000, disk_percent=1.0),
        tmux_session_count=0, agent_counts={}, agent_types=("shell", "claude"), agent_version=None, labels=(),
    )


@pytest.fixture
def fleet(tmp_path):
    """HP-like controller (local id "hp-test") + a Dell-like remote node
    agent registered as "dell-linux" with its OWN audit DB."""
    central = _terminal(tmp_path, "central")
    local_client = FakeLocalClient(central, ["hp-worker"])
    controller = ControllerService(NodeRegistry(tmp_path / "nodes.db"), local_client=local_client,
                                   local_node_id="hp-test", local_workspace_root=str(tmp_path))
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={},
                                       agent_types=("shell",), agent_version="0")
    node_terminal = _terminal(tmp_path, "dell")
    agent = TestClient(build_node_agent(node_id="dell-linux", terminal=node_terminal, token=TOKEN))
    remote = AgentBackedClient(agent, ["promptflow-main"])
    _online(controller, "dell-linux", remote)
    # The compact layer only needs status/tail from the controller here.
    controller.terminal_status = lambda session: {**IDLE, "session": session}
    controller.terminal_status_bounded = lambda session, timeout_seconds: {**IDLE, "session": session}
    compact = CompactTerminalTools(SimpleNamespace(terminal_get_binding=lambda b: {"error": "X"}),
                                   controller, run_journal=RunJournalStore(tmp_path / "journal.db"))
    compact.task_labels = TaskLabeler(central.audit, local_node_id="hp-test", node_sync=controller)
    return SimpleNamespace(controller=controller, compact=compact, central=central.audit,
                           node=node_terminal.audit, remote=remote, agent=agent, local=local_client)


# -- send / send_wait -----------------------------------------------------------

def test_remote_titled_send_writes_central_copy_and_node_local_copy(fleet):
    result = fleet.compact.turn(action="send", target="promptflow-main", text="go",
                                title="Fix login CSRF")
    assert result["status"] == "SUBMIT_CONFIRMED"
    # Central copy keyed by the REMOTE node id, exactly as before.
    assert fleet.central.get_task_label("dell-linux", "promptflow-main")["summary"] == "Fix login CSRF"
    # Node-local copy, keyed "local", in the node's own AuditStore.
    assert fleet.remote.label_calls == [("POST", "/v1/sessions/promptflow-main/task-label")]
    node_row = fleet.node.get_task_label(NODE_LOCAL_LABEL_ID, "promptflow-main")
    assert node_row["summary"] == "Fix login CSRF" and node_row["source"] == "title"
    # No configured-id alias row on the node.
    assert fleet.node.get_task_label("dell-linux", "promptflow-main") is None
    # Bounded sync timeout, not the 10s default.
    assert fleet.remote.label_timeouts[-1] == 3.0


def test_qualified_remote_target_routes_to_owning_node(fleet):
    fleet.compact.turn(action="send", target="dell-linux/promptflow-main", text="go", title="Qualified")
    assert fleet.node.get_task_label("local", "promptflow-main")["summary"] == "Qualified"


def test_local_send_writes_node_local_label(fleet):
    fleet.compact.turn(action="send", target="hp-worker", text="go", title="Local work")
    assert fleet.central.get_task_label("hp-test", "hp-worker")["summary"] == "Local work"
    # LocalNodeClient mirror -> same process's audit, under "local".
    assert fleet.central.get_task_label("local", "hp-worker")["summary"] == "Local work"
    assert fleet.remote.label_calls == []


def test_second_titled_assignment_replaces_first_on_node(fleet):
    fleet.compact.turn(action="send", target="promptflow-main", text="go", title="First task")
    fleet.compact.turn(action="send_wait", target="promptflow-main", text="go on",
                       metadata={"task_summary": "Second task"}, timeout=2, poll_interval=1)
    assert fleet.node.get_task_label("local", "promptflow-main")["summary"] == "Second task"
    assert len(fleet.node.task_label_index()) == 1


def test_continuation_does_not_sync_or_overwrite(fleet):
    fleet.compact.turn(action="send", target="promptflow-main", text="go", title="Build the release")
    calls = len(fleet.remote.label_calls)
    for text in ("y", "continue", "ok go ahead",
                 "Now also refactor the billing module to use the new pricing API everywhere"):
        fleet.compact.turn(action="send", target="promptflow-main", text=text)
    assert len(fleet.remote.label_calls) == calls
    assert fleet.node.get_task_label("local", "promptflow-main")["summary"] == "Build the release"


# -- failure isolation ------------------------------------------------------------

@pytest.mark.parametrize("failure", [NodeClientError("HTTP 404"), RuntimeError("boom")])
def test_node_sync_failure_is_swallowed_and_send_succeeds(fleet, failure):
    def broken(*_a, **_k):
        raise failure
    fleet.remote.set_task_label = broken
    result = fleet.compact.turn(action="send", target="promptflow-main", text="go", title="Still sent")
    assert result["status"] == "SUBMIT_CONFIRMED"
    assert result["current_task"]["summary"] == "Still sent"
    assert fleet.central.get_task_label("dell-linux", "promptflow-main")["summary"] == "Still sent"


def test_offline_node_sync_reports_error_without_raising(fleet, tmp_path):
    fleet.controller.registry.register("m910", display_name="m910", hostname="m910", endpoint="http://m910")
    fleet.controller._clients["m910"] = fleet.remote  # never heartbeated -> not ONLINE
    result = fleet.controller.terminal_set_task_label("m910/x", summary="S")
    assert result["error"] == "NODE_UNREACHABLE"
    assert fleet.remote.label_calls == []


def test_old_agent_without_endpoint_is_swallowed(fleet):
    fleet.remote._request = lambda *a, **k: (_ for _ in ()).throw(NodeClientError("HTTP 404", http_status=404))
    result = fleet.controller.terminal_set_task_label("dell-linux/promptflow-main", summary="S")
    assert result["error"] == "NODE_UNREACHABLE" and "404" in result["detail"]


def test_node_sync_object_that_raises_never_breaks_labeler(tmp_path):
    class Exploding:
        def terminal_set_task_label(self, *a, **k):
            raise RuntimeError("down")

        def terminal_clear_task_label(self, *a, **k):
            raise RuntimeError("down")

    audit = AuditStore(tmp_path / "a.db")
    labeler = TaskLabeler(audit, local_node_id="local", node_sync=Exploding())
    assert labeler.on_send(node_id="n", session="s", text="go", title="T")["summary"] == "T"
    labeler.clear("n", "s")
    assert audit.get_task_label("n", "s") is None


# -- create_session / supervise / clear -----------------------------------------

def test_compact_create_session_title_syncs_after_successful_create(fleet):
    fleet.compact.handlers["create_session"] = lambda name, **kwargs: {
        "session": name, "state": "READY", "node_id": "dell-linux"}
    fleet.remote.sessions.append("new-1")
    result = fleet.compact.turn(action="create_session", target="new-1", initial_prompt="long prompt",
                                title="Port the parser")
    assert result["status"] == "OK"
    assert fleet.node.get_task_label("local", "new-1")["summary"] == "Port the parser"


def test_failed_create_does_not_sync(fleet):
    fleet.compact.handlers["create_session"] = lambda name, **kwargs: {"error": "SESSION_ALREADY_EXISTS"}
    fleet.compact.turn(action="create_session", target="new-2", title="Nope")
    assert fleet.remote.label_calls == []


def test_native_create_initial_prompt_syncs_to_owning_node(tmp_path):
    from terminal_mcp.mcp_app import build_mcp

    config = AppConfig(PermissionsConfig(True, True), ("test-*",), 50, 20,
                       InputPolicyConfig(allowed_session_patterns=("test-*",)))
    service = TerminalService(config, audit=AuditStore(tmp_path / "audit.db"),
                              grants=SessionGrantStore(tmp_path / "grants.db"))
    server = build_mcp(service)
    controller = server.compact_tools.controller
    synced: list[tuple] = []
    controller.terminal_create_session = lambda name, *a, **k: {
        "session": name, "state": "READY", "node_id": "dell-linux"}
    controller.terminal_set_task_label = lambda target, **k: synced.append(("set", target, k["summary"])) or {}
    controller.terminal_clear_task_label = lambda target: synced.append(("clear", target)) or {}
    create = server.compact_tools.handlers["create_session"]
    create("test-new", initial_prompt="# Task\nAdd retry to the uploader. More text.")
    assert synced == [("set", "dell-linux/test-new", "Add retry to the uploader.")]
    create("test-new")  # empty recreate clears the node copy too
    assert synced[-1] == ("clear", "dell-linux/test-new")


def test_supervise_syncs_bare_target_resolved_by_controller(fleet):
    class Supervisor:
        def start(self, target, **kwargs):
            return {"status": "RUNNING", "task_id": "dt_9"}

    fleet.compact.direct_tasks = Supervisor()
    fleet.compact.turn(action="supervise", target="promptflow-main",
                       text="Migrate the orders table to the v3 schema")
    row = fleet.node.get_task_label("local", "promptflow-main")
    assert row["source"] == "supervised" and row["task_id"] == "dt_9"


def test_clear_mirrors_to_node(fleet):
    fleet.compact.turn(action="send", target="promptflow-main", text="go", title="T")
    fleet.compact.task_labels.clear("dell-linux", "promptflow-main")
    assert fleet.node.get_task_label("local", "promptflow-main") is None
    assert fleet.central.get_task_label("dell-linux", "promptflow-main") is None


# -- node-agent endpoint --------------------------------------------------------

def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_endpoint_requires_auth(fleet):
    response = fleet.agent.post("/v1/sessions/s1/task-label", json={"summary": "x"})
    assert response.status_code == 401
    assert fleet.agent.delete("/v1/sessions/s1/task-label").status_code == 401
    bad = fleet.agent.post("/v1/sessions/s1/task-label", json={"summary": "x"},
                           headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 401
    assert fleet.node.task_label_index() == {}


@pytest.mark.parametrize("body,error", [
    ({}, "INVALID_SUMMARY"),
    ({"summary": "   "}, "INVALID_SUMMARY"),
    ({"summary": "x" * 2001}, "INVALID_SUMMARY"),
    ({"summary": ["list"]}, "INVALID_SUMMARY"),
    ({"summary": "ok", "source": "anything"}, "INVALID_SOURCE"),
    ({"summary": "ok", "task_id": "t" * 201}, "INVALID_TASK_ID"),
    ({"summary": "ok", "request_key": {"a": 1}}, "INVALID_REQUEST_KEY"),
])
def test_endpoint_validates_fields(fleet, body, error):
    response = fleet.agent.post("/v1/sessions/s1/task-label", json=body, headers=_auth())
    assert response.status_code == 200
    assert response.json()["error"] == error
    assert fleet.node.task_label_index() == {}


def test_endpoint_rejects_bad_session_name_and_non_object_json(fleet):
    response = fleet.agent.post("/v1/sessions/bad;name/task-label", json={"summary": "x"}, headers=_auth())
    assert response.json()["error"] == "INVALID_SESSION_NAME"
    response = fleet.agent.post("/v1/sessions/s1/task-label", content="[1]", headers=_auth())
    assert response.status_code == 400


def test_endpoint_replaces_bounds_redacts_and_ignores_extra_fields(fleet):
    first = fleet.agent.post("/v1/sessions/s1/task-label", headers=_auth(), json={
        "summary": "First", "task_id": "t1", "node_id": "evil", "updated_at": "1999"}).json()
    assert first["status"] == "OK" and first["node_id"] == "dell-linux"
    fleet.agent.post("/v1/sessions/s1/task-label", headers=_auth(), json={
        "summary": "Second " + "word " * 100, "source": "durable_task", "request_key": "rk"})
    rows = fleet.node.task_label_index()
    assert list(rows) == [("local", "s1")]
    row = rows[("local", "s1")]
    assert row["summary"].startswith("Second") and len(row["summary"]) <= 140
    assert row["source"] == "durable_task" and row["task_id"] is None and row["request_key"] == "rk"
    assert row["updated_at"] != "1999"
    secret = fleet.agent.post("/v1/sessions/s2/task-label", headers=_auth(), json={
        "summary": "deploy with token sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}).json()
    assert "AAAAAAAAAAAAAAAA" not in secret["label"]["summary"]


def test_endpoint_delete_clears(fleet):
    fleet.agent.post("/v1/sessions/s1/task-label", headers=_auth(), json={"summary": "x"})
    result = fleet.agent.delete("/v1/sessions/s1/task-label", headers=_auth()).json()
    assert result["removed"] == 1
    assert fleet.node.get_task_label("local", "s1") is None


def test_node_agent_delete_session_clears_label(fleet, monkeypatch):
    fleet.agent.post("/v1/sessions/s1/task-label", headers=_auth(), json={"summary": "x"})
    fleet.agent.post("/v1/sessions/s2/task-label", headers=_auth(), json={"summary": "kept"})
    monkeypatch.setattr(TerminalService, "terminal_delete_session",
                        lambda self, name, **k: {"deleted": True, "session": name})
    fleet.agent.request("DELETE", "/v1/sessions/s1", json={"confirm": True}, headers=_auth())
    assert fleet.node.get_task_label("local", "s1") is None
    monkeypatch.setattr(TerminalService, "terminal_delete_session",
                        lambda self, name, **k: {"error": "SESSION_PROTECTED"})
    fleet.agent.request("DELETE", "/v1/sessions/s2", json={"confirm": True}, headers=_auth())
    assert fleet.node.get_task_label("local", "s2")["summary"] == "kept"


def test_node_agent_kill_session_clears_label(fleet, monkeypatch):
    fleet.agent.post("/v1/sessions/s1/task-label", headers=_auth(), json={"summary": "x"})
    monkeypatch.setattr(TerminalService, "terminal_kill_session",
                        lambda self, name, confirm_name, **k: {"killed": True, "session": name})
    fleet.agent.post("/v1/sessions/s1/kill", json={"confirm_name": "s1"}, headers=_auth())
    assert fleet.node.get_task_label("local", "s1") is None


# -- Live Session Monitor on the node -------------------------------------------

class NodeMonitorController:
    def __init__(self, local_node_id):
        self.local_node_id = local_node_id

    def terminal_list_sessions(self):
        return {"sessions": [{"name": "promptflow-main", "node_id": self.local_node_id,
                              "node_name": self.local_node_id, "effective_read": True}],
                "unreachable_nodes": []}

    def terminal_status(self, target):
        return {**IDLE, "last_output": "$ ", "session": target}


@pytest.mark.parametrize("ui_node_id", ["local", "dell-canonical"])
def test_node_local_monitor_sees_synced_label(fleet, ui_node_id):
    fleet.compact.turn(action="send", target="promptflow-main", text="go", title="Synced from HP")
    monitor = LiveSessionMonitor(NodeMonitorController(ui_node_id), audit=fleet.node, ttl_seconds=0)
    entry = next(s for s in monitor.snapshot()["sessions"] if s["session"] == "promptflow-main")
    assert entry["current_task"]["summary"] == "Synced from HP"


def test_newer_node_mirror_beats_older_canonical_row(fleet):
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    fleet.node.set_task_label(node_id="dell-canonical", session="promptflow-main",
                              summary="Stale own label", source="title")
    with fleet.node._connection() as connection:
        connection.execute("UPDATE session_task_labels SET updated_at = ? WHERE node_id = ?",
                           (old, "dell-canonical"))
    fleet.compact.turn(action="send", target="promptflow-main", text="go", title="Fresh from HP")
    monitor = LiveSessionMonitor(NodeMonitorController("dell-canonical"), audit=fleet.node, ttl_seconds=0)
    entry = next(s for s in monitor.snapshot()["sessions"] if s["session"] == "promptflow-main")
    assert entry["current_task"]["summary"] == "Fresh from HP"
