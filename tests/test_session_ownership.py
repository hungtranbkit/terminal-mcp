"""Durable session ownership: a created session never becomes "location unknown".

Live 2026-10-05 on dell-linux: create_session answered SUBMIT_CONFIRMED, and a
minute later inspect/send/delete for the same name answered
SESSION_LOCATION_UNKNOWN. The controller's only positive evidence was an
in-memory 20s cache; once it expired (or the controller restarted) a slow or
failed listing from the owning node erased the ownership. And when the owner
DID answer "gone", an unrelated offline node still turned that definite answer
into "location unknown".
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from terminal_mcp.controller import ControllerService
from terminal_mcp.node_client import NodeClientError
from terminal_mcp.node_registry import NodeRegistry
from terminal_mcp.session_ownership import ACTIVE, KILLED, MISSING, SessionOwnershipStore

from tests.test_controller import FakeNodeClient, _controller, _heartbeat_local, _register_fake_remote


class RemoteNode(FakeNodeClient):
    """A node whose tmux instances have identities, that can delete, and whose
    listing can be made to fail independently of routed calls."""

    def __init__(self) -> None:
        super().__init__({})
        self.listing_fails = False
        self._next_id = 1

    def list_sessions(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        if self.listing_fails:
            raise NodeClientError("simulated listing timeout")
        return super().list_sessions(timeout_seconds=timeout_seconds)

    def create_session(self, name, agent_type="shell", cwd=None, **kw) -> dict[str, Any]:
        created = int(time.time()) + self._next_id  # strictly increasing per instance
        session_id = f"${self._next_id}"
        self._next_id += 1
        self._sessions[name] = {"created": datetime.fromtimestamp(created, timezone.utc).isoformat(),
                                "session_id": session_id, "created_epoch": created}
        return {"session": name, "state": "READY", "session_id": session_id, "created_epoch": created}

    def send_text(self, session, text, press_enter=False, dry_run=False, **kw) -> dict[str, Any]:
        self.calls.append(("send_text", session))
        return {"session": session, "sent": True}

    def delete_session(self, name, *, confirm=False, requested_by=None) -> dict[str, Any]:
        self.calls.append(("delete_session", name))
        row = self._sessions.pop(name, None)
        if row is None:
            return {"session": name, "deleted": False, "action": "already_gone", "receipt": {"instance": None}}
        return {"session": name, "deleted": True, "action": "deleted",
                "receipt": {"instance": f"{row['session_id']}@{row['created_epoch']}", "tmux_kill": "killed"}}

    def snapshot(self) -> dict[str, Any]:
        """A listing taken now, to be replayed later (a slow fan-out)."""
        return {"sessions": [{"name": name, **row} for name, row in self._sessions.items()]}


def _fleet(tmp_path):
    controller, _service = _controller(tmp_path)
    _heartbeat_local(controller)
    remote = RemoteNode()
    _register_fake_remote(controller, "dell-linux", remote)
    return controller, remote


def _expire_cache(controller: ControllerService) -> None:
    controller._session_location_cache.clear()


def test_create_records_durable_ownership_before_success(tmp_path):
    controller, remote = _fleet(tmp_path)
    result = controller.terminal_create_session("canary-a", node="dell-linux")
    assert result["state"] == "READY" and result["ownership"]["state"] == ACTIVE
    stored = controller.ownership.get("canary-a", "dell-linux")
    assert stored.state == ACTIVE and stored.instance_id and stored.generation == 1


def test_create_then_cache_expiry_then_listing_timeout_still_routes(tmp_path):
    controller, remote = _fleet(tmp_path)
    controller.terminal_create_session("canary-a", node="dell-linux")
    _expire_cache(controller)
    remote.listing_fails = True  # one /v1/sessions timeout
    resolved = controller.resolve_session("canary-a")
    assert resolved.get("error") is None, resolved
    assert resolved["node_id"] == "dell-linux" and resolved["stale_location"] is True
    assert controller.terminal_status("canary-a")["status"] == "running"
    assert controller.terminal_send_text("canary-a", "echo hi")["sent"] is True
    # The failed listing changed nothing durable.
    assert controller.ownership.get("canary-a", "dell-linux").state == ACTIVE


def test_controller_restart_keeps_routing_without_any_cache(tmp_path):
    controller, remote = _fleet(tmp_path)
    controller.terminal_create_session("canary-a", node="dell-linux")
    restarted = ControllerService(NodeRegistry(tmp_path / "nodes.db"),
                                  local_client=controller._clients[controller.local_node_id],
                                  local_node_id=controller.local_node_id,
                                  local_workspace_root=str(tmp_path))
    restarted._clients["dell-linux"] = remote
    remote.listing_fails = True  # and the first probe after restart fails
    resolved = restarted.resolve_session("canary-a")
    assert resolved["node_id"] == "dell-linux" and resolved["known_owner"]["state"] == ACTIVE


def test_missed_heartbeat_reports_known_owner_unreachable_not_lost(tmp_path):
    from tests.test_controller import _age_last_heartbeat
    controller, remote = _fleet(tmp_path)
    controller.terminal_create_session("canary-a", node="dell-linux")
    _expire_cache(controller)
    _age_last_heartbeat(controller.registry, "dell-linux", 10_000)
    routed = controller.terminal_status("canary-a")
    assert routed["error"] == "NODE_UNREACHABLE" and routed["node_id"] == "dell-linux"
    assert routed["known_owner"]["state"] == ACTIVE
    assert controller.ownership.get("canary-a", "dell-linux").state == ACTIVE


def test_node_without_client_keeps_ownership(tmp_path):
    controller, remote = _fleet(tmp_path)
    controller.terminal_create_session("canary-a", node="dell-linux")
    _expire_cache(controller)
    del controller._clients["dell-linux"]
    routed = controller.terminal_status("canary-a")
    assert routed["error"] == "NODE_UNREACHABLE" and routed["node_id"] == "dell-linux"
    assert controller.ownership.get("canary-a", "dell-linux").state == ACTIVE


def test_only_the_owner_confirming_absence_marks_missing(tmp_path):
    controller, remote = _fleet(tmp_path)
    controller.registry.register("macbook", display_name="mac", hostname="m", endpoint="http://m")  # offline
    controller.terminal_create_session("canary-a", node="dell-linux")
    _expire_cache(controller)
    remote._sessions.pop("canary-a")  # the process exited on its own
    result = controller.resolve_session("canary-a")
    # A definite answer from the known owner, despite the unprobed macbook.
    assert result["error"] == "SESSION_RUNTIME_MISSING" and result["node_id"] == "dell-linux"
    row = controller.ownership.get("canary-a", "dell-linux")
    assert row.state == MISSING and row.missing_at is not None  # row kept, not deleted


def test_incomplete_probe_without_owner_evidence_never_claims_not_found(tmp_path):
    controller, remote = _fleet(tmp_path)
    remote.listing_fails = True
    result = controller.resolve_session("never-created")
    assert result["error"] == "SESSION_LOCATION_UNKNOWN"


def test_delete_tombstones_and_stale_inventory_cannot_resurrect(tmp_path):
    controller, remote = _fleet(tmp_path)
    controller.terminal_create_session("canary-b", node="dell-linux")
    before_delete = remote.snapshot()
    deleted = controller.terminal_delete_session("canary-b", confirm=True)
    assert deleted["receipt"]["controller_ownership"]["state"] == KILLED
    controller._reconcile_ownership("dell-linux", before_delete)  # slow fan-out lands late
    assert controller.ownership.get("canary-b", "dell-linux").state == KILLED
    _expire_cache(controller)
    result = controller.resolve_session("canary-b")
    assert result["error"] == "SESSION_DELETED" and result["node_id"] == "dell-linux"
    listed = controller.terminal_list_sessions()["sessions"]
    assert "canary-b" not in {row["name"] for row in listed}
    again = controller.terminal_delete_session("canary-b", confirm=True)  # idempotent
    assert again.get("action") == "already_gone"
    assert controller.ownership.get("canary-b", "dell-linux").state == KILLED


def test_same_name_recreation_is_a_new_generation_and_routes(tmp_path):
    controller, remote = _fleet(tmp_path)
    controller.terminal_create_session("canary-b", node="dell-linux")
    before_delete = remote.snapshot()
    controller.terminal_delete_session("canary-b", confirm=True)
    recreated = controller.terminal_create_session("canary-b", node="dell-linux")
    assert recreated["ownership"]["generation"] == 2
    controller._reconcile_ownership("dell-linux", before_delete)  # stale old inventory
    row = controller.ownership.get("canary-b", "dell-linux")
    assert row.state == ACTIVE and row.generation == 2
    _expire_cache(controller)
    assert controller.resolve_session("canary-b")["node_id"] == "dell-linux"


def test_multi_node_same_name_unprobeable_is_ambiguous_not_guessed(tmp_path):
    controller, remote = _fleet(tmp_path)
    other = RemoteNode()
    _register_fake_remote(controller, "m910", other)
    controller.ownership.record_created("dup", "dell-linux", instance_id="$1@1", created_epoch=1)
    controller.ownership.record_created("dup", "m910", instance_id="$1@2", created_epoch=2)
    remote.listing_fails = other.listing_fails = True
    result = controller.resolve_session("dup")
    assert result["error"] == "AMBIGUOUS_SESSION" and set(result["nodes"]) == {"dell-linux", "m910"}


def test_duplicate_guard_uses_durable_owner(tmp_path):
    controller, remote = _fleet(tmp_path)
    controller.terminal_create_session("canary-a", node="dell-linux")
    _expire_cache(controller)
    remote.listing_fails = True
    again = controller.terminal_create_session("canary-a", node="auto")
    assert again["error"] == "SESSION_ALREADY_EXISTS" and again["node_id"] == "dell-linux"


def test_list_sessions_marks_missing_only_for_nodes_that_answered(tmp_path):
    controller, remote = _fleet(tmp_path)
    controller.terminal_create_session("canary-a", node="dell-linux")
    remote.listing_fails = True
    controller.terminal_list_sessions()
    assert controller.ownership.get("canary-a", "dell-linux").state == ACTIVE
    remote.listing_fails = False
    remote._sessions.pop("canary-a")
    controller.terminal_list_sessions()
    assert controller.ownership.get("canary-a", "dell-linux").state == MISSING


def test_store_stale_rules(tmp_path):
    store = SessionOwnershipStore(tmp_path / "own.db")
    store.record_created("s", "n", instance_id="$1@100", created_epoch=100)
    store.tombstone("s", "n", instance_id="$1@100", created_epoch=100)
    store.reconcile_node("n", {"s": 100})  # the deleted instance
    assert store.get("s", "n").state == KILLED
    store.reconcile_node("n", {"s": 200})  # a newer instance created outside the controller
    row = store.get("s", "n")
    assert row.state == ACTIVE and row.generation == 2 and row.created_epoch == 200


def test_node_agent_restart_with_tmux_surviving_stays_routable(tmp_path):
    controller, remote = _fleet(tmp_path)
    controller.terminal_create_session("canary-a", node="dell-linux")
    restarted_agent = RemoteNode()  # a fresh agent process, same surviving tmux server
    restarted_agent._sessions = dict(remote._sessions)
    restarted_agent.listing_fails = True  # its first listing after restart is not ready yet
    controller._clients["dell-linux"] = restarted_agent
    _expire_cache(controller)
    assert controller.terminal_status("canary-a")["status"] == "running"
    restarted_agent.listing_fails = False
    _expire_cache(controller)
    resolved = controller.resolve_session("canary-a")
    assert resolved["node_id"] == "dell-linux" and "stale_location" not in resolved
