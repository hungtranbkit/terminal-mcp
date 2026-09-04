"""dashboard.py's session lifecycle routes (create/detach/delete/kill/
reopen), now routed through ControllerService instead of TerminalService
directly (task's own multi-node Create Session UX work) -- covers every
item 12 scenario: auto scheduler, explicit remote node create, offline-
node rejection, no-silent-fallback, node metadata persistence in both the
create response and /dashboard/api/sessions' own list, Windows-node
selection (platform tagging), and backward compatibility (no `node` in
the request body behaves exactly as before multi-node routing existed).

Uses a REAL TerminalService + real tmux session for the local node (so
permission/session semantics are genuine, matching test_controller.py's
own convention) plus a lightweight FakeNodeClient standing in for a
"remote" node -- same fixture shape as test_controller.py, duplicated
here (not imported) since these two files are exercising different
layers (controller.py's own routing vs. dashboard.py's HTTP surface on
top of it) and dashboard.py's own register_dashboard signature needs a
full app/TestClient, not just a bare ControllerService.
"""
from __future__ import annotations

import subprocess
from typing import Any

import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import AppConfig, DashboardConfig, InputPolicyConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.controller import ControllerService
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.host_metrics import NodeMetrics
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.node_client import LocalNodeClient, NodeClientError
from terminal_mcp.node_registry import NodeRegistry


def _config(tmp_path) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("mn-*",),
        input_policy=InputPolicyConfig(allowed_session_patterns=("mn-*",)),
        dashboard=DashboardConfig(web_terminal_enabled=False),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),), protected_sessions=()),
    )


def _client(tmp_path):
    config = _config(tmp_path)
    service = TerminalService(config)
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(service), local_workspace_root=str(tmp_path))
    server = build_mcp(service)
    register_dashboard(server, service, controller=controller)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})
    return client, controller, service


class FakeNodeClient:
    def __init__(self, *, broken: bool = False, platform: str = "linux") -> None:
        self.broken = broken
        self.platform = platform
        self.calls: list[tuple[str, str]] = []
        self._sessions: dict[str, dict[str, Any]] = {}
        self._killed: list[dict[str, Any]] = []

    def list_sessions(self) -> dict[str, Any]:
        if self.broken:
            raise NodeClientError("simulated transport failure")
        rows = []
        for name, meta in self._sessions.items():
            rows.append({"name": name, "allowed": True, "attached": False, "windows": 1,
                        "created": "2026-01-01T00:00:00+00:00", "activity": "2026-01-01T00:00:00+00:00",
                        "read_allowed": True, "read_granted": False, "input_allowed": True, "input_granted": False,
                        "effective_read": True, "effective_input": True, **meta})
        return {"sessions": rows}

    def status(self, session: str) -> dict[str, Any]:
        self.calls.append(("status", session))
        if self.broken:
            raise NodeClientError("simulated transport failure")
        if session not in self._sessions:
            return {"error": "SESSION_NOT_FOUND"}
        return {"session": session, "state": "RUNNING", "reason": "ok"}

    def tail(self, session, lines=None, *, ansi=False):
        return {"session": session, "output": {"lines": []}}

    def capture(self, session, start_line=None):
        return {"session": session, "lines": []}

    def send_text(self, *a, **k):
        return {"error": "NOT_IMPLEMENTED_IN_FAKE"}

    def send_keys(self, *a, **k):
        return {"error": "NOT_IMPLEMENTED_IN_FAKE"}

    def input_context(self, *a, **k):
        return {}

    def create_session(self, name, agent_type="shell", cwd=None, *, initial_prompt=None,
                       grant_mode="none", binding=None, requested_by=None) -> dict[str, Any]:
        self.calls.append(("create_session", name))
        if self.broken:
            raise NodeClientError("simulated transport failure")
        self._sessions[name] = {"agent_type": agent_type}
        return {"session": name, "state": "READY", "agent_type": agent_type}

    def detach_session(self, name):
        self.calls.append(("detach_session", name))
        return {"session": name, "detached": True}

    def delete_session(self, name):
        self.calls.append(("delete_session", name))
        self._sessions.pop(name, None)
        return {"session": name, "deleted": True}

    def kill_session(self, name, confirm_name, *, requested_by=None):
        self.calls.append(("kill_session", name))
        if self.broken:
            raise NodeClientError("simulated transport failure")
        if name in self._sessions:
            self._killed.append({"name": name, "agent_type": self._sessions[name].get("agent_type", "shell"),
                                 "working_directory": None, "metadata_complete": True})
            del self._sessions[name]
        return {"session": name, "killed": True}

    def reopen_session(self, name, *, agent_type=None, cwd=None, grant_mode="none", requested_by=None):
        self.calls.append(("reopen_session", name))
        self._killed = [r for r in self._killed if r.get("name") != name]
        self._sessions[name] = {"agent_type": agent_type or "shell"}
        return {"session": name, "state": "READY", "agent_type": agent_type or "shell"}

    def list_killed_sessions(self):
        return {"killed_sessions": list(self._killed)}

    def health(self):
        return {"status": "ok"}

    def metrics(self):
        return {}


def _register_fake_remote(controller: ControllerService, node_id: str, client: FakeNodeClient,
                          *, agent_types=("shell",), platform="linux") -> None:
    controller.registry.register(node_id, display_name=node_id, hostname=f"{node_id}-host", endpoint=f"http://{node_id}")
    controller._clients[node_id] = client
    controller.registry.heartbeat(
        node_id,
        metrics=NodeMetrics(cpu_percent=5.0, load1=0.1, load5=0.1, load15=0.1, cpu_count=4,
                            ram_total_bytes=8_000_000_000, ram_used_bytes=1_000_000_000, ram_percent=12.5,
                            swap_total_bytes=0, swap_used_bytes=0, swap_percent=0.0,
                            disk_total_bytes=100_000_000_000, disk_used_bytes=1_000_000_000,
                            disk_free_bytes=99_000_000_000, disk_percent=1.0),
        tmux_session_count=0, agent_counts={}, agent_types=agent_types, agent_version=None, labels=(),
        platform=platform, session_backend="tmux" if platform == "linux" else "windows_pty",
    )


def _heartbeat_local(controller: ControllerService) -> None:
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=("shell", "claude"),
                                       agent_version="0.13.0")


def _kill_tmux(name: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", name], check=False, capture_output=True)


# ---------------------------------------------------------------------------
# Backward compatibility -- no `node` in the request body.
# ---------------------------------------------------------------------------


def test_create_without_node_field_defaults_to_auto_and_lands_local(tmp_path):
    client, controller, _service = _client(tmp_path)
    _heartbeat_local(controller)
    try:
        r = client.post("/dashboard/api/session/create", json={"name": "mn-bwcompat", "agent_type": "shell"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["node_id"] == "local"
    finally:
        _kill_tmux("mn-bwcompat")


def test_create_before_any_heartbeat_still_works_local_heartbeat_refreshed_first(tmp_path):
    # Real bug fixed while wiring this: a fresh client hitting /session/
    # create FIRST (no prior /nodes or /sessions poll) must not get
    # NO_ELIGIBLE_NODE just because the local node never heartbeated yet.
    client, controller, _service = _client(tmp_path)
    try:
        r = client.post("/dashboard/api/session/create", json={"name": "mn-firstcall", "agent_type": "shell"})
        assert r.status_code == 200, r.text
        assert r.json()["node_id"] == "local"
    finally:
        _kill_tmux("mn-firstcall")


# ---------------------------------------------------------------------------
# Explicit node selection -- auto, remote, offline rejection, no fallback.
# ---------------------------------------------------------------------------


def test_create_explicit_node_auto_uses_scheduler(tmp_path):
    client, controller, _service = _client(tmp_path)
    _heartbeat_local(controller)
    try:
        r = client.post("/dashboard/api/session/create", json={"name": "mn-auto", "agent_type": "shell", "node": "auto"})
        assert r.status_code == 200, r.text
        assert r.json()["node_id"] == "local"
    finally:
        _kill_tmux("mn-auto")


def test_create_explicit_remote_node_id_actually_routes_there(tmp_path):
    client, controller, _service = _client(tmp_path)
    _heartbeat_local(controller)
    remote = FakeNodeClient()
    _register_fake_remote(controller, "remote-x", remote)

    r = client.post("/dashboard/api/session/create", json={"name": "mn-remote", "agent_type": "shell", "node": "remote-x"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["node_id"] == "remote-x"
    assert ("create_session", "mn-remote") in remote.calls


def test_create_explicit_offline_node_rejected_not_silent_fallback(tmp_path):
    # Item 5: "không silently fallback sang local nếu node lỗi. Fail rõ ràng."
    client, controller, _service = _client(tmp_path)
    _heartbeat_local(controller)
    # "offline-node" is registered but NEVER heartbeated -- stays offline.
    controller.registry.register("offline-node", display_name="offline-node", hostname="x", endpoint="http://x")
    controller._clients["offline-node"] = FakeNodeClient()

    r = client.post("/dashboard/api/session/create", json={"name": "mn-shouldnotexist", "agent_type": "shell", "node": "offline-node"})
    assert r.status_code == 502  # NODE_UNREACHABLE -- fails BEFORE ever attempting the network call
    assert r.json()["error"] == "NODE_UNREACHABLE"
    # The real guarantee this test protects: it must NEVER have silently
    # created anything on local instead.
    listing = client.post("/dashboard/api/session/create", json={"name": "mn-shouldnotexist", "agent_type": "shell", "node": "auto"})
    # If the offline-node attempt had silently created it on local, THIS
    # auto-create of the SAME name would now fail with SESSION_ALREADY_EXISTS.
    assert listing.status_code == 200, listing.text
    _kill_tmux("mn-shouldnotexist")


def test_create_explicit_unreachable_node_client_never_falls_back_to_local(tmp_path):
    client, controller, _service = _client(tmp_path)
    _heartbeat_local(controller)
    broken = FakeNodeClient(broken=True)
    _register_fake_remote(controller, "broken-remote", broken)

    r = client.post("/dashboard/api/session/create", json={"name": "mn-broken", "agent_type": "shell", "node": "broken-remote"})
    assert r.status_code == 502
    assert r.json()["error"] == "NODE_UNREACHABLE"
    # Never silently created locally under the same name.
    r2 = client.post("/dashboard/api/session/create", json={"name": "mn-broken", "agent_type": "shell", "node": "local"})
    assert r2.status_code == 200, r2.text
    _kill_tmux("mn-broken")


# ---------------------------------------------------------------------------
# Windows node platform tagging.
# ---------------------------------------------------------------------------


def test_create_on_windows_node_reports_platform(tmp_path):
    client, controller, _service = _client(tmp_path)
    _heartbeat_local(controller)
    win = FakeNodeClient(platform="windows")
    _register_fake_remote(controller, "win-node", win, agent_types=("shell", "claude"), platform="windows")

    r = client.post("/dashboard/api/session/create", json={"name": "mn-win", "agent_type": "claude", "node": "win-node"})
    assert r.status_code == 200, r.text
    assert r.json()["node_id"] == "win-node"

    nodes_response = client.get("/dashboard/api/nodes")
    win_row = next(n for n in nodes_response.json()["nodes"] if n["id"] == "win-node")
    assert win_row["platform"] == "windows"
    assert win_row["claude_available"] is True


# ---------------------------------------------------------------------------
# Node metadata persistence -- create response AND /dashboard/api/sessions.
# ---------------------------------------------------------------------------


def test_session_list_shows_node_label_for_remote_session(tmp_path):
    client, controller, _service = _client(tmp_path)
    _heartbeat_local(controller)
    remote = FakeNodeClient()
    _register_fake_remote(controller, "labeled-node", remote)
    client.post("/dashboard/api/session/create", json={"name": "mn-labeled", "agent_type": "shell", "node": "labeled-node"})

    r = client.get("/dashboard/api/sessions")
    assert r.status_code == 200, r.text
    rows = r.json()["sessions"]
    row = next(row for row in rows if row["name"] == "mn-labeled")
    assert row["node_id"] == "labeled-node"
    assert row["node_name"] == "labeled-node"


def test_session_list_local_rows_still_tagged_local(tmp_path):
    client, controller, _service = _client(tmp_path)
    _heartbeat_local(controller)
    try:
        client.post("/dashboard/api/session/create", json={"name": "mn-localtag", "agent_type": "shell"})
        r = client.get("/dashboard/api/sessions")
        row = next(row for row in r.json()["sessions"] if row["name"] == "mn-localtag")
        assert row["node_id"] == "local"
    finally:
        _kill_tmux("mn-localtag")


# ---------------------------------------------------------------------------
# Kill/detach/delete route through the RIGHT node (task item 10).
# ---------------------------------------------------------------------------


def test_kill_routes_to_the_session_own_node_not_local(tmp_path):
    client, controller, _service = _client(tmp_path)
    _heartbeat_local(controller)
    remote = FakeNodeClient()
    _register_fake_remote(controller, "kill-node", remote)
    client.post("/dashboard/api/session/create", json={"name": "mn-killme", "agent_type": "shell", "node": "kill-node"})

    r = client.post("/dashboard/api/session/kill", json={"name": "mn-killme", "confirm_name": "mn-killme"})
    assert r.status_code == 200, r.text
    assert ("kill_session", "mn-killme") in remote.calls


def test_detach_routes_to_the_session_own_node(tmp_path):
    client, controller, _service = _client(tmp_path)
    _heartbeat_local(controller)
    remote = FakeNodeClient()
    _register_fake_remote(controller, "detach-node", remote)
    client.post("/dashboard/api/session/create", json={"name": "mn-detachme", "agent_type": "shell", "node": "detach-node"})

    r = client.post("/dashboard/api/session/detach", json={"name": "mn-detachme"})
    assert r.status_code == 200, r.text
    assert ("detach_session", "mn-detachme") in remote.calls


# ---------------------------------------------------------------------------
# Reopen -- default same-node, explicit move elsewhere (task item 9).
# ---------------------------------------------------------------------------


def test_reopen_default_stays_on_same_remote_node(tmp_path):
    client, controller, _service = _client(tmp_path)
    _heartbeat_local(controller)
    remote = FakeNodeClient()
    _register_fake_remote(controller, "reopen-node", remote)
    client.post("/dashboard/api/session/create", json={"name": "mn-reopenme", "agent_type": "shell", "node": "reopen-node"})
    client.post("/dashboard/api/session/kill", json={"name": "mn-reopenme", "confirm_name": "mn-reopenme"})

    r = client.post("/dashboard/api/session/reopen", json={"name": "mn-reopenme"})
    assert r.status_code == 200, r.text
    assert r.json()["node_id"] == "reopen-node"


def test_reopen_explicit_node_moves_it(tmp_path):
    client, controller, _service = _client(tmp_path)
    _heartbeat_local(controller)
    origin = FakeNodeClient()
    target = FakeNodeClient()
    _register_fake_remote(controller, "origin-x", origin)
    _register_fake_remote(controller, "target-x", target)
    client.post("/dashboard/api/session/create", json={"name": "mn-move", "agent_type": "shell", "node": "origin-x"})
    client.post("/dashboard/api/session/kill", json={"name": "mn-move", "confirm_name": "mn-move"})

    r = client.post("/dashboard/api/session/reopen", json={"name": "mn-move", "node": "target-x"})
    assert r.status_code == 200, r.text
    assert r.json()["node_id"] == "target-x"
    assert r.json()["moved_from"] == "origin-x"


# ---------------------------------------------------------------------------
# grant_mode on create -- real usability gap reported live: the dashboard's
# Create Session form never requested a grant, so a session created there
# always started completely unreadable/un-sendable, forcing a separate
# grant round-trip afterward for the common case of wanting to use the
# session you just made. Still opt-in/explicit (defaults to "none",
# unchanged behavior) -- these tests use "mn-nogrant" (matches this file's
# own statically-whitelisted "mn-*" pattern -- already readable regardless
# of any grant) only for the "none"/default case, and a name OUTSIDE that
# pattern for "read"/"read_send" so the grant's actual effect is the only
# thing making it readable/sendable, not the static whitelist.
# ---------------------------------------------------------------------------


def test_create_default_grant_mode_is_none_unchanged_from_before(tmp_path):
    client, controller, service = _client(tmp_path)
    _heartbeat_local(controller)
    try:
        r = client.post("/dashboard/api/session/create", json={"name": "mn-nogrant", "agent_type": "shell"})
        assert r.status_code == 200, r.text
        assert service.grants.get("mn-nogrant") is None
    finally:
        _kill_tmux("mn-nogrant")


def test_create_grant_mode_read_grants_read_only_not_input(tmp_path):
    client, controller, service = _client(tmp_path)
    _heartbeat_local(controller)
    try:
        r = client.post("/dashboard/api/session/create",
                        json={"name": "not-mn-prefixed-read", "agent_type": "shell", "grant_mode": "read"})
        assert r.status_code == 200, r.text
        grant = service.grants.get("not-mn-prefixed-read")
        assert grant is not None
        assert grant.read_enabled is True
        assert grant.input_enabled is False
    finally:
        _kill_tmux("not-mn-prefixed-read")


def test_create_grant_mode_read_send_grants_both(tmp_path):
    client, controller, service = _client(tmp_path)
    _heartbeat_local(controller)
    try:
        r = client.post("/dashboard/api/session/create",
                        json={"name": "not-mn-prefixed-readsend", "agent_type": "shell", "grant_mode": "read_send"})
        assert r.status_code == 200, r.text
        grant = service.grants.get("not-mn-prefixed-readsend")
        assert grant is not None
        assert grant.read_enabled is True
        assert grant.input_enabled is True
    finally:
        _kill_tmux("not-mn-prefixed-readsend")


def test_create_grant_mode_invalid_value_rejected(tmp_path):
    client, controller, _service = _client(tmp_path)
    _heartbeat_local(controller)
    r = client.post("/dashboard/api/session/create",
                    json={"name": "mn-badgrant", "agent_type": "shell", "grant_mode": "everything"})
    assert r.status_code == 400
    assert r.json()["error"] == "INVALID_GRANT_MODE"
