"""controller.py: routing, session-location resolution/ambiguity, fleet-wide
views, and the Phase A/B "local node behaves exactly like today" guarantee
(task item 11). Uses a REAL TerminalService + real tmux sessions for the
local node (so permission enforcement/session semantics are the genuine
ones, not a mock), plus a lightweight FakeNodeClient standing in for a
"remote" node so ambiguous/offline/unreachable routing can be tested
without needing a real second host or even a real subprocess.
"""
from __future__ import annotations

from typing import Any

import pytest

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.controller import ControllerService
from terminal_mcp.core import TerminalService
from terminal_mcp.node_client import LocalNodeClient, NodeClientError
from terminal_mcp.node_models import CAPACITY_HEALTHY
from terminal_mcp.node_registry import NodeRegistry
from terminal_mcp.host_metrics import NodeMetrics


def _config(tmp_path, *, read=True, input=True) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(read, input),
        allowed_session_patterns=("ctrl-*",),
        max_capture_lines=200,
        default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("ctrl-*",)),
        session_lifecycle=SessionLifecycleConfig(
            enabled=True, allowed_cwd_roots=(str(tmp_path),), protected_sessions=(),
            launch_commands=(("claude", "true"),),
        ),
    )


def _controller(tmp_path, *, read=True, input=True) -> tuple[ControllerService, TerminalService]:
    # grants/audit/bindings/leases/killed_sessions/session_registry all
    # default to this host's REAL ~/.local/state/terminal-mcp/*.db when
    # not given explicitly (TerminalService.__init__) -- isolated here so
    # this test suite's own grant-read/grant-input/create/kill/reopen
    # calls (test_remote_session_grant_reaches_the_remote_node_not_local
    # and friends) can never leak a test session name into real
    # production state. Found live: a prior run of this exact suite had
    # left "ctrl-grant-local"/"ctrl-amb-grant" sitting in the real
    # grants.db. session_registry.db was found the same way while adding
    # the fleet-wide watchdog session-drop tests: without this, every
    # prior test's own mark_missing()-detected drop events (real,
    # accumulated across every past run of this whole file) leaked
    # straight into terminal_watchdog_session_events_fleet()'s results.
    from terminal_mcp.grants import SessionGrantStore
    from terminal_mcp.session_registry import SessionRegistryStore
    service = TerminalService(_config(tmp_path, read=read, input=input),
                              grants=SessionGrantStore(tmp_path / "grants.db"),
                              session_registry=SessionRegistryStore(tmp_path / "session_registry.db"))
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(service),
                                   local_workspace_root=str(tmp_path))
    return controller, service


def _heartbeat_local(controller: ControllerService, *, sessions: int = 0) -> None:
    controller.refresh_local_heartbeat(tmux_session_count=sessions, agent_counts={},
                                       agent_types=("shell", "claude"), agent_version="0.13.0")


class FakeNodeClient:
    """Minimal NodeClient stand-in for a 'remote' node -- no HTTP, no
    subprocess, just enough surface for routing/ambiguity/offline tests."""

    def __init__(self, sessions: dict[str, dict[str, Any]] | None = None, *, broken: bool = False,
                killed: list[dict[str, Any]] | None = None) -> None:
        self._sessions = sessions or {}
        self.broken = broken
        self._killed = killed or []
        self.calls: list[tuple[str, str]] = []
        self.grants: dict[str, dict[str, bool]] = {}

    def list_sessions(self) -> dict[str, Any]:
        if self.broken:
            raise NodeClientError("simulated transport failure")
        return {"sessions": [{"name": name, **row} for name, row in self._sessions.items()]}

    def status(self, session: str) -> dict[str, Any]:
        self.calls.append(("status", session))
        if self.broken:
            raise NodeClientError("simulated transport failure")
        if session not in self._sessions:
            return {"error": "SESSION_NOT_FOUND"}
        return {"session": session, "status": "running"}

    def tail(self, session: str, lines=None, *, ansi=False) -> dict[str, Any]:
        self.calls.append(("tail", session))
        return {"session": session, "lines": []}

    def health(self) -> dict[str, Any]:
        return {"status": "ok"}

    def metrics(self) -> dict[str, Any]:
        return {}

    def list_killed_sessions(self) -> dict[str, Any]:
        if self.broken:
            raise NodeClientError("simulated transport failure")
        return {"killed_sessions": list(self._killed)}

    def create_session(self, name: str, agent_type: str = "shell", cwd: str | None = None, *,
                       initial_prompt=None, grant_mode="none", binding=None, requested_by=None,
                       show_on_desktop=False) -> dict[str, Any]:
        self.calls.append(("create_session", name))
        if self.broken:
            raise NodeClientError("simulated transport failure")
        self._sessions[name] = {"agent_type": agent_type, "cwd": cwd}
        return {"session": name, "state": "READY", "agent_type": agent_type, "cwd": cwd}

    def reopen_session(self, name: str, *, agent_type=None, cwd=None, grant_mode="none", requested_by=None) -> dict[str, Any]:
        self.calls.append(("reopen_session", name))
        if self.broken:
            raise NodeClientError("simulated transport failure")
        self._killed = [row for row in self._killed if row.get("name") != name]
        self._sessions[name] = {"agent_type": agent_type, "cwd": cwd}
        return {"session": name, "state": "READY", "agent_type": agent_type, "cwd": cwd}

    def grant_read(self, name: str, enabled: bool, *, granted_by: str | None = None) -> dict[str, Any]:
        self.calls.append(("grant_read", name))
        if self.broken:
            raise NodeClientError("simulated transport failure")
        if name not in self._sessions:
            return {"error": "SESSION_NOT_FOUND", "session": name}
        self.grants[name] = {"read_enabled": enabled, "input_enabled": self.grants.get(name, {}).get("input_enabled", False) and enabled}
        return {"session": name, **self.grants[name]}

    def grant_input(self, name: str, enabled: bool, *, granted_by: str | None = None) -> dict[str, Any]:
        self.calls.append(("grant_input", name))
        if self.broken:
            raise NodeClientError("simulated transport failure")
        existing = self.grants.get(name)
        if not existing or not existing.get("read_enabled"):
            return {"error": "READ_GRANT_REQUIRED", "session": name}
        existing["input_enabled"] = enabled
        return {"session": name, **existing}

    def knowledge_search(self, query: str, *, session_name=None, project=None, since=None, until=None,
                         limit: int = 20) -> dict[str, Any]:
        self.calls.append(("knowledge_search", query))
        if self.broken:
            raise NodeClientError("simulated transport failure")
        return {"query": query, "results": list(getattr(self, "knowledge_results", []))}

    def knowledge_timeline(self, session_name: str, *, since=None, until=None, limit: int = 200) -> dict[str, Any]:
        self.calls.append(("knowledge_timeline", session_name))
        if self.broken:
            raise NodeClientError("simulated transport failure")
        return {"session": session_name, "chunks": []}

    def knowledge_recover(self, session_name: str) -> dict[str, Any]:
        self.calls.append(("knowledge_recover", session_name))
        if self.broken:
            raise NodeClientError("simulated transport failure")
        return {"session": session_name, "recovered_process": False, "checkpoint": None, "recent_chunks": []}

    def knowledge_checkpoint(self, session_name: str, summary: str) -> dict[str, Any]:
        self.calls.append(("knowledge_checkpoint", session_name))
        if self.broken:
            raise NodeClientError("simulated transport failure")
        return {"session": session_name, "checkpoint_id": 1}

    def watchdog_session_events(self, *, unacknowledged_only: bool = False, limit: int = 50) -> dict[str, Any]:
        self.calls.append(("watchdog_session_events", ""))
        if self.broken:
            raise NodeClientError("simulated transport failure")
        return {"events": list(getattr(self, "drop_events", []))}

    def watchdog_acknowledge_session_event(self, event_id: int, *, by: str | None = None) -> dict[str, Any]:
        self.calls.append(("watchdog_acknowledge_session_event", str(event_id)))
        if self.broken:
            raise NodeClientError("simulated transport failure")
        for row in getattr(self, "drop_events", []):
            if row["id"] == event_id:
                row["acknowledged"] = True
                return {"event_id": event_id, "acknowledged": True}
        return {"error": "EVENT_NOT_FOUND", "event_id": event_id}


# -- Phase A/B backward compatibility: local-only behaves like plain TerminalService --

def test_local_only_create_tail_status_kill_reopen_roundtrip(tmp_path):
    controller, service = _controller(tmp_path)
    _heartbeat_local(controller)

    created = controller.terminal_create_session("ctrl-rt", "shell", str(tmp_path))
    try:
        assert created["node_id"] == "local"
        assert created["node_name"] == "Local"

        status = controller.terminal_status("ctrl-rt")
        assert status["node_id"] == "local"

        tail = controller.terminal_tail("ctrl-rt")
        assert tail["node_id"] == "local"

        killed = controller.terminal_kill_session("ctrl-rt", "ctrl-rt")
        assert killed.get("error") is None or "node_id" in killed

        reopened = controller.terminal_reopen_session("ctrl-rt")
        assert reopened["node_id"] == "local"
    finally:
        import subprocess
        subprocess.run(["tmux", "kill-session", "-t", "ctrl-rt"], check=False, capture_output=True)


def test_permission_denial_preserved_through_routing(tmp_path):
    # task item 12: routing must never WEAKEN TerminalService's own
    # permission enforcement -- a read-only-input config still refuses a
    # send when routed through the controller, exactly as it would direct.
    controller, service = _controller(tmp_path, input=False)
    _heartbeat_local(controller)
    controller.terminal_create_session("ctrl-perm", "shell", str(tmp_path))
    try:
        result = controller.terminal_send_text("ctrl-perm", "echo hi")
        assert result.get("error") in ("ACCESS_DENIED", "INPUT_DISABLED") or result.get("delivery_state") is None
        # Whatever the exact TerminalService error shape is, it must NOT
        # have actually delivered text -- compare against calling the
        # service directly for the ground truth.
        direct = service.terminal_send_text("ctrl-perm", "echo hi")
        assert result.get("error") == direct.get("error")
    finally:
        import subprocess
        subprocess.run(["tmux", "kill-session", "-t", "ctrl-perm"], check=False, capture_output=True)


def test_dry_run_send_text_routed_through_controller_never_actually_sends(tmp_path):
    # Regression: dry_run was originally dropped by NodeClient/Controller's
    # send_text signature (only press_enter/idempotency_key/... were
    # threaded through) -- a caller passing dry_run=True through the
    # controller would have silently had real text delivered instead of
    # dry-run-previewed. Must match calling TerminalService directly.
    controller, service = _controller(tmp_path)
    _heartbeat_local(controller)
    controller.terminal_create_session("ctrl-dry", "shell", str(tmp_path))
    try:
        routed = controller.terminal_send_text("ctrl-dry", "echo should-not-appear", dry_run=True)
        assert routed.get("dry_run") is True or routed.get("sent") is False
        tail = controller.terminal_tail("ctrl-dry")
        output = "\n".join(tail.get("output", [])) if isinstance(tail.get("output"), list) else str(tail.get("output", ""))
        assert "should-not-appear" not in output
    finally:
        import subprocess
        subprocess.run(["tmux", "kill-session", "-t", "ctrl-dry"], check=False, capture_output=True)


def test_session_not_found_reported_cleanly(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    result = controller.terminal_status("ctrl-does-not-exist")
    assert result["error"] == "SESSION_NOT_FOUND"


# -- create_session: auto scheduling, explicit node, duplicates -----------

def test_create_auto_before_any_heartbeat_is_no_eligible_node(tmp_path):
    # The local node is registered at ControllerService construction time
    # but starts OFFLINE until its first heartbeat -- Auto placement must
    # not silently pick an offline node.
    controller, _service = _controller(tmp_path)
    result = controller.terminal_create_session("ctrl-early", "shell", str(tmp_path), node="auto")
    assert result["error"] == "NO_ELIGIBLE_NODE"


def test_create_explicit_unknown_node_is_node_not_found(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    result = controller.terminal_create_session("ctrl-x", "shell", str(tmp_path), node="ghost-node")
    assert result["error"] == "NODE_NOT_FOUND"


def test_create_auto_with_platform_requirement_no_match_is_no_eligible_node(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)  # local node is "linux"
    result = controller.terminal_create_session("ctrl-win", "shell", str(tmp_path), node="auto", platform="windows")
    assert result["error"] == "NO_ELIGIBLE_NODE"


def test_create_explicit_node_platform_mismatch_rejected(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)  # local node is "linux"
    result = controller.terminal_create_session("ctrl-win2", "shell", str(tmp_path), node="local", platform="windows")
    assert result["error"] == "PLATFORM_MISMATCH"
    assert result["node_id"] == "local"


def test_create_duplicate_name_across_controller_is_rejected(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    controller.terminal_create_session("ctrl-dup", "shell", str(tmp_path))
    try:
        result = controller.terminal_create_session("ctrl-dup", "shell", str(tmp_path))
        assert result["error"] == "SESSION_ALREADY_EXISTS"
        assert result["node_id"] == "local"
    finally:
        import subprocess
        subprocess.run(["tmux", "kill-session", "-t", "ctrl-dup"], check=False, capture_output=True)


# -- ambiguous session name across two nodes -------------------------------

def test_duplicate_session_name_on_two_nodes_is_ambiguous(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    controller.terminal_create_session("ctrl-amb", "shell", str(tmp_path))
    try:
        controller.registry.register("fake-remote", display_name="Fake", hostname="fake-host", endpoint="http://fake")
        fake = FakeNodeClient({"ctrl-amb": {}})
        controller._clients["fake-remote"] = fake
        controller.registry.heartbeat(
            "fake-remote",
            metrics=NodeMetrics(cpu_percent=5.0, load1=0.1, load5=0.1, load15=0.1, cpu_count=4,
                                ram_total_bytes=8_000_000_000, ram_used_bytes=1_000_000_000, ram_percent=12.5,
                                swap_total_bytes=0, swap_used_bytes=0, swap_percent=0.0,
                                disk_total_bytes=100_000_000_000, disk_used_bytes=1_000_000_000,
                                disk_free_bytes=99_000_000_000, disk_percent=1.0),
            tmux_session_count=1, agent_counts={}, agent_types=("shell",), agent_version=None, labels=(),
        )

        # create_session already cached "ctrl-amb" -> local (20s TTL) --
        # invalidate first so this exercises the actual re-probe/ambiguity
        # path rather than serving the (correct, but pre-existing) cached
        # answer from before the second node's session appeared.
        controller.invalidate_session_location("ctrl-amb")
        result = controller.terminal_status("ctrl-amb")
        assert result["error"] == "AMBIGUOUS_SESSION"
        assert set(result["nodes"]) == {"local", "fake-remote"}

        # A qualified name disambiguates cleanly, no guessing needed.
        qualified = controller.terminal_status("local/ctrl-amb")
        assert qualified["node_id"] == "local"
    finally:
        import subprocess
        subprocess.run(["tmux", "kill-session", "-t", "ctrl-amb"], check=False, capture_output=True)


def test_qualified_name_to_unregistered_node_is_node_not_found(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    result = controller.terminal_status("ghost-node/whatever")
    assert result["error"] == "NODE_NOT_FOUND"


# -- grant-read/grant-input routing (multi-node permission bug fix) -------
# Real bug found live: a Windows/remote-node session's "Xem + gửi" grant
# button did nothing, because the dashboard route called the LOCAL
# TerminalService's own grants store directly regardless of which node the
# session actually lived on. These are the routing-layer regression tests
# for the fix (controller.terminal_grant_session_read/_input, mirroring
# every other _route-based operation above).

def test_local_session_grant_routes_to_local_grants_store_unchanged(tmp_path):
    controller, service = _controller(tmp_path)
    _heartbeat_local(controller)
    controller.terminal_create_session("ctrl-grant-local", "shell", str(tmp_path))
    try:
        result = controller.terminal_grant_session_read("ctrl-grant-local", True)
        assert result.get("error") is None
        assert result["read_enabled"] is True
        assert result["node_id"] == "local"
        # Really landed in the local TerminalService's own grant store,
        # not just a routing-layer echo.
        assert service.grants.get("ctrl-grant-local").read_enabled is True
    finally:
        import subprocess
        subprocess.run(["tmux", "kill-session", "-t", "ctrl-grant-local"], check=False, capture_output=True)


def test_remote_session_grant_reaches_the_remote_node_not_local(tmp_path):
    # The exact scenario reported live: a session that exists ONLY on a
    # remote node (e.g. Windows session "window" on a worker node) -- the
    # local TerminalService has never heard of it.
    controller, service = _controller(tmp_path)
    _heartbeat_local(controller)
    controller.registry.register("worker", display_name="Worker", hostname="worker-host", endpoint="http://worker")
    fake = FakeNodeClient({"window": {}})
    controller._clients["worker"] = fake
    controller.registry.heartbeat(
        "worker", metrics=NodeMetrics(cpu_percent=5.0, load1=0.1, load5=0.1, load15=0.1, cpu_count=4,
                                     ram_total_bytes=8_000_000_000, ram_used_bytes=1_000_000_000, ram_percent=12.5,
                                     swap_total_bytes=0, swap_used_bytes=0, swap_percent=0.0,
                                     disk_total_bytes=100_000_000_000, disk_used_bytes=1_000_000_000,
                                     disk_free_bytes=99_000_000_000, disk_percent=1.0),
        tmux_session_count=1, agent_counts={}, agent_types=("shell",), agent_version=None, labels=(),
    )

    result = controller.terminal_grant_session_read("window", True, granted_by="op@example.com")
    assert result.get("error") is None
    assert result["node_id"] == "worker"
    assert fake.grants["window"]["read_enabled"] is True
    # Never silently applied to the local grants store instead.
    assert service.grants.get("window") is None

    result2 = controller.terminal_grant_session_input("window", True)
    assert result2.get("error") is None
    assert result2["input_enabled"] is True
    assert fake.grants["window"]["input_enabled"] is True
    assert ("grant_read", "window") in fake.calls
    assert ("grant_input", "window") in fake.calls


def test_grant_input_without_read_grant_required_error_from_remote_node(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    controller.registry.register("worker", display_name="Worker", hostname="worker-host", endpoint="http://worker")
    fake = FakeNodeClient({"window": {}})
    controller._clients["worker"] = fake
    controller.registry.heartbeat(
        "worker", metrics=NodeMetrics(cpu_percent=5.0, load1=0.1, load5=0.1, load15=0.1, cpu_count=4,
                                     ram_total_bytes=8_000_000_000, ram_used_bytes=1_000_000_000, ram_percent=12.5,
                                     swap_total_bytes=0, swap_used_bytes=0, swap_percent=0.0,
                                     disk_total_bytes=100_000_000_000, disk_used_bytes=1_000_000_000,
                                     disk_free_bytes=99_000_000_000, disk_percent=1.0),
        tmux_session_count=1, agent_counts={}, agent_types=("shell",), agent_version=None, labels=(),
    )
    result = controller.terminal_grant_session_input("worker/window", True)
    assert result["error"] == "READ_GRANT_REQUIRED"


def test_duplicate_session_name_grant_is_ambiguous_never_guessed(tmp_path):
    # Task item 3: "hai node có cùng session name không được grant nhầm
    # nhau" -- a bare, unqualified name that exists on two nodes must be
    # refused outright, exactly like every other routed operation, never
    # silently applied to whichever node happens to be resolved first.
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    controller.terminal_create_session("ctrl-amb-grant", "shell", str(tmp_path))
    try:
        controller.registry.register("fake-remote", display_name="Fake", hostname="fake-host", endpoint="http://fake")
        fake = FakeNodeClient({"ctrl-amb-grant": {}})
        controller._clients["fake-remote"] = fake
        controller.registry.heartbeat(
            "fake-remote", metrics=NodeMetrics(cpu_percent=5.0, load1=0.1, load5=0.1, load15=0.1, cpu_count=4,
                                              ram_total_bytes=8_000_000_000, ram_used_bytes=1_000_000_000, ram_percent=12.5,
                                              swap_total_bytes=0, swap_used_bytes=0, swap_percent=0.0,
                                              disk_total_bytes=100_000_000_000, disk_used_bytes=1_000_000_000,
                                              disk_free_bytes=99_000_000_000, disk_percent=1.0),
            tmux_session_count=1, agent_counts={}, agent_types=("shell",), agent_version=None, labels=(),
        )
        controller.invalidate_session_location("ctrl-amb-grant")
        result = controller.terminal_grant_session_read("ctrl-amb-grant", True)
        assert result["error"] == "AMBIGUOUS_SESSION"
        assert set(result["nodes"]) == {"local", "fake-remote"}

        # A qualified name disambiguates cleanly, same as every other route.
        qualified = controller.terminal_grant_session_read("local/ctrl-amb-grant", True)
        assert qualified.get("error") is None
        assert qualified["node_id"] == "local"
    finally:
        import subprocess
        subprocess.run(["tmux", "kill-session", "-t", "ctrl-amb-grant"], check=False, capture_output=True)


def test_qualified_name_to_node_with_no_client_is_node_unreachable(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    controller.registry.register("no-client-node", display_name="NoClient", hostname="h", endpoint="http://x")
    result = controller.terminal_status("no-client-node/whatever")
    assert result["error"] == "NODE_UNREACHABLE"


def test_remote_client_transport_failure_surfaces_as_node_unreachable(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    controller.registry.register("broken-node", display_name="Broken", hostname="h", endpoint="http://x")
    controller._clients["broken-node"] = FakeNodeClient(broken=True)
    result = controller.terminal_status("broken-node/anything")
    assert result["error"] == "NODE_UNREACHABLE"
    assert result["node_id"] == "broken-node"


# -- node offline behavior: never silently dropped -------------------------

def test_offline_node_reported_in_unreachable_not_silently_dropped(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    controller.terminal_create_session("ctrl-listed", "shell", str(tmp_path))
    try:
        # Register a second node but never heartbeat it -- it stays OFFLINE.
        controller.registry.register("dark-node", display_name="Dark", hostname="h", endpoint="http://x")
        controller._clients["dark-node"] = FakeNodeClient({"phantom": {}})

        result = controller.terminal_list_sessions()
        names = {row["name"] for row in result["sessions"]}
        assert "ctrl-listed" in names
        assert "phantom" not in names  # offline node's sessions never surfaced
        assert any(n["node_id"] == "dark-node" and n["status"] == "offline" for n in result["unreachable_nodes"])
    finally:
        import subprocess
        subprocess.run(["tmux", "kill-session", "-t", "ctrl-listed"], check=False, capture_output=True)


def test_fleet_list_sessions_merges_online_nodes_with_node_tags(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    controller.terminal_create_session("ctrl-merge", "shell", str(tmp_path))
    try:
        controller.registry.register("remote-ok", display_name="Remote OK", hostname="h", endpoint="http://x")
        fake = FakeNodeClient({"remote-sess": {}})
        controller._clients["remote-ok"] = fake
        controller.registry.heartbeat(
            "remote-ok",
            metrics=NodeMetrics(cpu_percent=5.0, load1=0.1, load5=0.1, load15=0.1, cpu_count=4,
                                ram_total_bytes=8_000_000_000, ram_used_bytes=1_000_000_000, ram_percent=12.5,
                                swap_total_bytes=0, swap_used_bytes=0, swap_percent=0.0,
                                disk_total_bytes=100_000_000_000, disk_used_bytes=1_000_000_000,
                                disk_free_bytes=99_000_000_000, disk_percent=1.0),
            tmux_session_count=1, agent_counts={}, agent_types=("shell",), agent_version=None, labels=(),
        )
        result = controller.terminal_list_sessions()
        by_name = {row["name"]: row for row in result["sessions"]}
        assert by_name["ctrl-merge"]["node_id"] == "local"
        assert by_name["remote-sess"]["node_id"] == "remote-ok"
        assert by_name["remote-sess"]["node_name"] == "Remote OK"
    finally:
        import subprocess
        subprocess.run(["tmux", "kill-session", "-t", "ctrl-merge"], check=False, capture_output=True)


# -- node management surface (dashboard/doctor) ----------------------------

def test_test_connection_local_is_always_ok_zero_latency(tmp_path):
    controller, _service = _controller(tmp_path)
    result = controller.test_connection("local")
    assert result == {"ok": True, "latency_ms": 0.0}


def test_test_connection_unknown_node(tmp_path):
    controller, _service = _controller(tmp_path)
    result = controller.test_connection("ghost")
    assert result["error"] == "NODE_NOT_FOUND"


def test_set_draining_roundtrip(tmp_path):
    controller, _service = _controller(tmp_path)
    result = controller.set_draining("local", True)
    assert result == {"node_id": "local", "draining": True}
    assert controller.node_status("local").draining is True
    assert controller.set_draining("ghost", True)["error"] == "NODE_NOT_FOUND"


def test_session_location_cache_hit_avoids_reprobe(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    controller.terminal_create_session("ctrl-cache", "shell", str(tmp_path))
    try:
        # First call resolves+caches (create_session already primed the
        # cache); register a second node whose FakeNodeClient would raise
        # if ever probed, to prove the cache is actually being used.
        controller.registry.register("must-not-probe", display_name="X", hostname="h", endpoint="http://x")

        class ExplodingClient(FakeNodeClient):
            def list_sessions(self):
                raise AssertionError("cache should have avoided probing this node")

        controller._clients["must-not-probe"] = ExplodingClient()
        controller.registry.heartbeat(
            "must-not-probe",
            metrics=NodeMetrics(cpu_percent=5.0, load1=0.1, load5=0.1, load15=0.1, cpu_count=4,
                                ram_total_bytes=8_000_000_000, ram_used_bytes=1_000_000_000, ram_percent=12.5,
                                swap_total_bytes=0, swap_used_bytes=0, swap_percent=0.0,
                                disk_total_bytes=100_000_000_000, disk_used_bytes=1_000_000_000,
                                disk_free_bytes=99_000_000_000, disk_percent=1.0),
            tmux_session_count=0, agent_counts={}, agent_types=("shell",), agent_version=None, labels=(),
        )
        result = controller.terminal_status("ctrl-cache")
        assert result["node_id"] == "local"
    finally:
        import subprocess
        subprocess.run(["tmux", "kill-session", "-t", "ctrl-cache"], check=False, capture_output=True)


def test_invalidate_session_location_forces_reprobe(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    controller.terminal_create_session("ctrl-inval", "shell", str(tmp_path))
    try:
        controller.invalidate_session_location("ctrl-inval")
        result = controller.terminal_status("ctrl-inval")
        assert result["node_id"] == "local"
    finally:
        import subprocess
        subprocess.run(["tmux", "kill-session", "-t", "ctrl-inval"], check=False, capture_output=True)


# ---------------------------------------------------------------------------
# terminal_reopen_session -- routes via each node's own killed-sessions
# list, NEVER live-session resolution (a killed session, by definition,
# is never in any node's live tmux listing -- see this method's own
# docstring in controller.py for the exact regression this fixes: the
# dashboard's create-session-UX multi-node work, task item 9).
# ---------------------------------------------------------------------------


def _register_fake_remote(controller: ControllerService, node_id: str, client: FakeNodeClient) -> None:
    controller.registry.register(node_id, display_name=node_id, hostname=f"{node_id}-host", endpoint=f"http://{node_id}")
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


def test_reopen_finds_the_right_remote_node_via_its_own_killed_sessions_list(tmp_path):
    # The general _route/resolve_session path would report SESSION_NOT_
    # FOUND here (the session is, correctly, in no node's LIVE listing --
    # it's killed) -- this must use the killed-sessions list instead.
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    remote = FakeNodeClient(killed=[{"name": "ctrl-remote-reopen", "agent_type": "shell",
                                     "working_directory": "/tmp", "metadata_complete": True}])
    _register_fake_remote(controller, "remote-a", remote)

    result = controller.terminal_reopen_session("ctrl-remote-reopen")
    assert result.get("error") is None, result
    assert result["node_id"] == "remote-a"
    assert ("reopen_session", "ctrl-remote-reopen") in remote.calls


def test_reopen_stale_location_cache_never_breaks_it(tmp_path):
    # Real regression this fix closes: an EXPIRED (or simply never-
    # populated) session-location cache entry must not matter at all --
    # reopen never consults that cache for resolution in the first place.
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    remote = FakeNodeClient(killed=[{"name": "ctrl-cache-gone", "agent_type": "shell",
                                     "working_directory": "/tmp", "metadata_complete": True}])
    _register_fake_remote(controller, "remote-b", remote)
    # Simulate a stale/never-set cache pointing nowhere useful for this name.
    controller.invalidate_session_location("ctrl-cache-gone")

    result = controller.terminal_reopen_session("ctrl-cache-gone")
    assert result["node_id"] == "remote-b"


def test_reopen_not_found_anywhere_reports_cleanly(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    result = controller.terminal_reopen_session("ctrl-nowhere")
    assert result["error"] == "SESSION_NOT_FOUND"


def test_reopen_explicit_node_moves_it_elsewhere_using_saved_metadata(tmp_path):
    # task item 9: "cho phép đổi node nếu user chọn Move/Reopen elsewhere"
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    origin = FakeNodeClient(killed=[{"name": "ctrl-move-reopen", "agent_type": "shell",
                                     "working_directory": "/tmp", "metadata_complete": True}])
    target = FakeNodeClient()
    _register_fake_remote(controller, "origin-node", origin)
    _register_fake_remote(controller, "target-node", target)

    result = controller.terminal_reopen_session("ctrl-move-reopen", node="target-node")
    assert result.get("error") is None, result
    assert result["node_id"] == "target-node"
    assert result["moved_from"] == "origin-node"
    assert ("create_session", "ctrl-move-reopen") in target.calls
    assert ("reopen_session", "ctrl-move-reopen") not in origin.calls  # never touched the origin's own reopen path


def test_reopen_explicit_node_same_as_origin_uses_reopen_not_create(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    origin = FakeNodeClient(killed=[{"name": "ctrl-same-node", "agent_type": "shell",
                                     "working_directory": "/tmp", "metadata_complete": True}])
    _register_fake_remote(controller, "origin-node", origin)

    result = controller.terminal_reopen_session("ctrl-same-node", node="origin-node")
    assert result["node_id"] == "origin-node"
    assert ("reopen_session", "ctrl-same-node") in origin.calls


def test_reopen_explicit_override_agent_type_and_cwd_used_over_saved_metadata(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    origin = FakeNodeClient(killed=[{"name": "ctrl-override", "agent_type": "shell",
                                     "working_directory": "/tmp/old", "metadata_complete": True}])
    target = FakeNodeClient()
    _register_fake_remote(controller, "origin-node2", origin)
    _register_fake_remote(controller, "target-node2", target)

    result = controller.terminal_reopen_session("ctrl-override", node="target-node2",
                                                 agent_type="claude", cwd="/tmp/new")
    assert result["agent_type"] == "claude"
    assert result["cwd"] == "/tmp/new"


# -- Session Knowledge Store fleet search (task item 10) --------------------


def test_knowledge_timeline_and_recover_route_to_the_session_own_node(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    remote = FakeNodeClient({"ctrl-know-1": {}})
    _register_fake_remote(controller, "know-node", remote)

    timeline = controller.terminal_knowledge_timeline("ctrl-know-1")
    assert timeline["node_id"] == "know-node"
    assert ("knowledge_timeline", "ctrl-know-1") in remote.calls

    recover = controller.terminal_knowledge_recover("ctrl-know-1")
    assert recover["node_id"] == "know-node"
    assert ("knowledge_recover", "ctrl-know-1") in remote.calls


def test_knowledge_checkpoint_routes_to_the_session_own_node(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    remote = FakeNodeClient({"ctrl-know-2": {}})
    _register_fake_remote(controller, "know-node2", remote)

    result = controller.terminal_knowledge_checkpoint("ctrl-know-2", "a manual note")
    assert result["node_id"] == "know-node2"
    assert ("knowledge_checkpoint", "ctrl-know-2") in remote.calls


def test_knowledge_search_fleet_merges_results_from_every_online_node(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    # Real bug regression: session_knowledge.py's own search() ALWAYS
    # includes a "node_id" field already -- always the string "local"
    # from that remote node's own private point of view (core.py's
    # REGISTRY_LOCAL_NODE_ID convention), never this controller's real
    # node_id for it. A fake result with no node_id key at all (as this
    # test used to write it) can't catch a setdefault()-instead-of-
    # overwrite bug, since setdefault happens to "work" when the key is
    # simply absent -- these fakes deliberately include the same wrong
    # "local" value real results always carry, so this test actually
    # exercises the overwrite.
    node_a = FakeNodeClient()
    node_a.knowledge_results = [{"session_name": "a-sess", "text": "hit on node a",
                                "captured_at": "2026-01-01T00:00:01Z", "node_id": "local"}]
    node_b = FakeNodeClient()
    node_b.knowledge_results = [{"session_name": "b-sess", "text": "hit on node b",
                                "captured_at": "2026-01-01T00:00:02Z", "node_id": "local"}]
    _register_fake_remote(controller, "search-node-a", node_a)
    _register_fake_remote(controller, "search-node-b", node_b)

    result = controller.terminal_knowledge_search_fleet("hit")
    names = {r["session_name"] for r in result["results"]}
    assert names == {"a-sess", "b-sess"}
    # Each result is tagged with the node it actually came from -- NOT
    # the "local" value it was faked with, matching what a real remote
    # node's own session_knowledge.py would have sent.
    by_session = {r["session_name"]: r["node_id"] for r in result["results"]}
    assert by_session["a-sess"] == "search-node-a"
    assert by_session["b-sess"] == "search-node-b"


def test_knowledge_search_fleet_tolerates_an_unreachable_node(tmp_path):
    """Requirement #10: a node being offline/unreachable never fails the
    WHOLE search or loses what other nodes have -- it just contributes
    zero results, reported separately in node_errors."""
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    healthy = FakeNodeClient()
    healthy.knowledge_results = [{"session_name": "healthy-sess", "text": "still findable", "captured_at": "2026-01-01T00:00:01Z"}]
    broken = FakeNodeClient(broken=True)
    _register_fake_remote(controller, "healthy-node", healthy)
    _register_fake_remote(controller, "broken-node", broken)

    result = controller.terminal_knowledge_search_fleet("findable")
    assert len(result["results"]) == 1
    assert result["results"][0]["session_name"] == "healthy-sess"
    assert "broken-node" in result["node_errors"]


def test_knowledge_search_fleet_skips_offline_nodes_entirely(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    remote = FakeNodeClient()
    remote.knowledge_results = [{"session_name": "offline-sess", "text": "should not be searched",
                                 "captured_at": "2026-01-01T00:00:01Z"}]
    controller.registry.register("offline-node", display_name="offline-node", hostname="offline-host",
                                 endpoint="http://offline-node")
    controller._clients["offline-node"] = remote
    # Deliberately never heartbeat()'d -- stays OFFLINE/unknown, never queried.

    result = controller.terminal_knowledge_search_fleet("should not be searched")
    assert result["results"] == []
    assert ("knowledge_search", "should not be searched") not in remote.calls


# -- watchdog: node online/offline transitions ---------------------------

def _age_last_heartbeat(registry: NodeRegistry, node_id: str, seconds_ago: float) -> None:
    """Directly backdates a node's own last_heartbeat_at in the real DB
    file -- the simplest way to make a REAL, current-time
    sync_status_transitions() call see that node as stale/offline,
    without needing to mock datetime.now() anywhere."""
    import sqlite3
    from datetime import datetime, timedelta, timezone
    then = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    conn = sqlite3.connect(registry.path)
    conn.execute("UPDATE nodes SET last_heartbeat_at = ? WHERE id = ?", (then, node_id))
    conn.commit()
    conn.close()


def test_list_nodes_detects_and_records_an_offline_transition(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    controller.list_nodes()  # baseline pass -- records "online" as last_known_status

    _age_last_heartbeat(controller.registry, "local", 300)  # past offline_after_seconds (180s default)
    controller.list_nodes()  # this call's own sync_status_transitions should detect it

    events = controller.terminal_watchdog_node_events()["events"]
    matching = [e for e in events if e["node_id"] == "local"]
    assert len(matching) == 1
    assert matching[0]["to_status"] == "offline"


def test_list_nodes_first_ever_heartbeat_is_never_a_watchdog_event(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    controller.list_nodes()  # the very first status read for this node
    assert controller.terminal_watchdog_node_events()["events"] == []


def test_watchdog_node_event_acknowledge(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    controller.list_nodes()
    _age_last_heartbeat(controller.registry, "local", 300)
    controller.list_nodes()
    event_id = controller.terminal_watchdog_node_events()["events"][0]["id"]

    result = controller.terminal_watchdog_acknowledge_node_event(event_id, by="tester")
    assert result["acknowledged"] is True
    assert controller.terminal_watchdog_node_events(unacknowledged_only=True)["events"] == []


def test_watchdog_node_event_acknowledge_unknown_id(tmp_path):
    controller, _service = _controller(tmp_path)
    result = controller.terminal_watchdog_acknowledge_node_event(99999)
    assert result["error"] == "EVENT_NOT_FOUND"


# -- watchdog: session-dropped-unexpectedly events, FLEET-WIDE ------------
# (task: "Mở rộng fleet-wide cho session drop" -- extends the earlier
# local-node-only pass. Mirrors the knowledge_search_fleet tests above:
# merges across online nodes, tags each row with the REAL node it came
# from (not whatever "local" value the node's own private DB row
# happened to carry), tolerates one unreachable node without losing
# another's events, and never queries an offline node at all.)

def test_watchdog_session_events_fleet_merges_and_tags_every_online_node(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    node_a = FakeNodeClient()
    node_a.drop_events = [{"id": 1, "node_id": "local", "session_name": "a-sess", "kind": "session_missing",
                          "detected_at": "2026-01-01T00:00:01Z", "detail": None, "acknowledged": False,
                          "recovered": False}]
    node_b = FakeNodeClient()
    node_b.drop_events = [{"id": 1, "node_id": "local", "session_name": "b-sess", "kind": "session_missing",
                          "detected_at": "2026-01-01T00:00:02Z", "detail": None, "acknowledged": False,
                          "recovered": False}]
    _register_fake_remote(controller, "drop-node-a", node_a)
    _register_fake_remote(controller, "drop-node-b", node_b)

    result = controller.terminal_watchdog_session_events_fleet()
    names = {e["session_name"] for e in result["events"]}
    assert names == {"a-sess", "b-sess"}
    # Each event is tagged with the node it actually came from -- NOT the
    # "local" value it was faked with (same bug class fixed in
    # terminal_knowledge_search_fleet: this must be a direct assignment,
    # never setdefault, or every remote node's own drop events would be
    # mislabeled "local").
    by_session = {e["session_name"]: e["node_id"] for e in result["events"]}
    assert by_session["a-sess"] == "drop-node-a"
    assert by_session["b-sess"] == "drop-node-b"


def test_watchdog_session_events_fleet_tolerates_an_unreachable_node(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    healthy = FakeNodeClient()
    healthy.drop_events = [{"id": 1, "node_id": "local", "session_name": "healthy-sess",
                           "kind": "session_missing", "detected_at": "2026-01-01T00:00:01Z",
                           "detail": None, "acknowledged": False, "recovered": False}]
    broken = FakeNodeClient(broken=True)
    _register_fake_remote(controller, "healthy-node", healthy)
    _register_fake_remote(controller, "broken-node", broken)

    result = controller.terminal_watchdog_session_events_fleet()
    assert len(result["events"]) == 1
    assert result["events"][0]["session_name"] == "healthy-sess"
    assert "broken-node" in result["node_errors"]


def test_watchdog_session_events_fleet_skips_offline_nodes_entirely(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    remote = FakeNodeClient()
    remote.drop_events = [{"id": 1, "node_id": "local", "session_name": "offline-sess",
                          "kind": "session_missing", "detected_at": "2026-01-01T00:00:01Z",
                          "detail": None, "acknowledged": False, "recovered": False}]
    controller.registry.register("offline-drop-node", display_name="offline-drop-node",
                                 hostname="offline-host", endpoint="http://offline-drop-node")
    controller._clients["offline-drop-node"] = remote
    # Deliberately never heartbeat()'d -- stays OFFLINE/unknown, never queried.

    result = controller.terminal_watchdog_session_events_fleet()
    assert result["events"] == []
    assert ("watchdog_session_events", "") not in remote.calls


def test_watchdog_acknowledge_session_event_routes_to_the_right_node(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    remote = FakeNodeClient()
    remote.drop_events = [{"id": 7, "node_id": "local", "session_name": "route-me",
                          "kind": "session_missing", "detected_at": "2026-01-01T00:00:01Z",
                          "detail": None, "acknowledged": False, "recovered": False}]
    _register_fake_remote(controller, "route-node", remote)

    result = controller.terminal_watchdog_acknowledge_session_event("route-node", 7, by="tester")
    assert result["acknowledged"] is True
    assert remote.drop_events[0]["acknowledged"] is True


def test_watchdog_acknowledge_session_event_unknown_node(tmp_path):
    controller, _service = _controller(tmp_path)
    result = controller.terminal_watchdog_acknowledge_session_event("no-such-node", 1)
    assert result["error"] == "NODE_NOT_FOUND"
