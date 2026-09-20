"""Rotating and revoking a node token end to end (blg_a3cc401d8275).

node_credentials.py's own tests cover the store. This file covers the
three acceptance criteria, which are about the OPERATION rather than the
data structure:

  AC1 a token can be rotated without hand-editing files on both sides
  AC2 revocation takes effect without a full node restart
  AC3 the old token is provably rejected afterwards

So the tests here drive the real HTTP routes and the real agent-side
credential holder, and the assertions that matter are the ones about
what is NOT there: no plaintext in the audit log, no plaintext in any
operator-facing response, and no window in which a node that is doing
everything right gets a 401.
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from terminal_mcp import node_credentials
from terminal_mcp.audit import AuditStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.connection_store import ConnectionStore
from terminal_mcp.controller import ControllerService
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import node_token_env_var, register_dashboard
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.node_agent import AgentCredential, _collect_rotated_token
from terminal_mcp.node_client import LocalNodeClient, RemoteNodeClient
from terminal_mcp.replay_guard import HeartbeatReplayGuard, HEADER_NONCE, HEADER_TIMESTAMP, MISSING_HEADERS, REPLAYED_NONCE
from terminal_mcp.node_credentials import NodeCredentialStore
from terminal_mcp.node_registry import NodeRegistry
from terminal_mcp.token_rotation import DELIVERED, NONE, PENDING, TokenRotationService

NODE = "win-rot"
ORIGINAL = "a" * 64


def _config() -> AppConfig:
    return AppConfig(PermissionsConfig(True, True), ("test-*",), 50, 20,
                     InputPolicyConfig(allowed_session_patterns=("test-*",)))


@pytest.fixture
def applied():
    """Records what the controller was told to present outbound, which is
    the half of a rotation that no unit test of the store can see."""
    return []


@pytest.fixture
def service(tmp_path, applied):
    credentials = NodeCredentialStore(tmp_path / "credentials.db")
    connections = ConnectionStore(tmp_path / "connections.db")
    audit = AuditStore(tmp_path / "audit.db")
    return TokenRotationService(credentials, connection_store=connections, audit=audit,
                                apply_outbound=lambda node_id, token: applied.append((node_id, token)),
                                state_dir=tmp_path / "pending")


# ---------------------------------------------------------------------------
# AC1: rotation as one operation, both sides
# ---------------------------------------------------------------------------

def test_a_rotation_is_staged_not_applied_until_the_node_has_it(service, applied):
    service.adopt_existing(NODE, ORIGINAL)
    result = service.rotate(NODE)

    assert result["rotated"] is True
    assert result["pending_state"] == PENDING
    # The controller has NOT switched its outbound copy: doing that before
    # the node has the new token is the exact window of broken
    # controller->node calls this two-phase handoff exists to remove.
    assert applied == []
    # And the node's existing token still authenticates, so nothing it is
    # doing right now breaks.
    assert service.credentials.verify(NODE, ORIGINAL).accepted


def test_the_node_collects_its_replacement_over_its_own_authenticated_channel(service):
    service.adopt_existing(NODE, ORIGINAL)
    service.rotate(NODE)

    collected = service.collect(NODE)
    assert collected["ok"] is True
    fresh = collected["token"]
    assert fresh != ORIGINAL
    assert node_credentials.token_fingerprint(fresh) == collected["token_id"]
    # Both tokens work in this window -- the node may still be using
    # either while it swaps them over.
    assert service.credentials.verify(NODE, fresh).accepted
    assert service.credentials.verify(NODE, ORIGINAL).accepted
    assert service.status(NODE)["pending_state"] == DELIVERED


def test_confirmation_moves_the_controller_over_and_kills_the_old_token(service, applied):
    service.adopt_existing(NODE, ORIGINAL)
    service.rotate(NODE)
    fresh = service.collect(NODE)["token"]

    assert service.confirm(NODE, node_credentials.token_fingerprint(fresh)) is True

    # AC1's other half: the controller's outbound copy moved by itself.
    assert applied == [(NODE, fresh)]
    # AC3: the old token is not merely expiring, it is revoked.
    old = service.credentials.verify(NODE, ORIGINAL)
    assert old.accepted is False
    assert old.verdict == node_credentials.REVOKED
    assert service.status(NODE)["pending_state"] == NONE


def test_confirming_a_token_that_is_not_the_staged_one_changes_nothing(service, applied):
    service.adopt_existing(NODE, ORIGINAL)
    service.rotate(NODE)

    assert service.confirm(NODE, node_credentials.token_fingerprint(ORIGINAL)) is False
    assert service.confirm(NODE, "deadbeefdead") is False
    assert service.confirm(NODE, None) is False
    assert applied == []
    assert service.status(NODE)["pending_state"] == PENDING


def test_a_node_that_is_offline_for_the_whole_window_is_not_locked_out(service):
    """The reason rotation defaulted to open-ended grace.

    A fixed window punishes the node for being unreachable -- which is
    precisely when an operator is most likely to be rotating."""
    service.adopt_existing(NODE, ORIGINAL)
    service.rotate(NODE)  # no grace_seconds: valid until confirmed

    expired = service.credentials.expire_grace()
    assert expired == 0
    assert service.credentials.verify(NODE, ORIGINAL).accepted

    fresh = service.collect(NODE)["token"]
    assert service.confirm(NODE, node_credentials.token_fingerprint(fresh)) is True


def test_an_operator_can_still_demand_a_hard_deadline(service):
    service.adopt_existing(NODE, ORIGINAL)
    service.rotate(NODE, grace_seconds=0)
    # Immediate cutover: for a token believed compromised, there is no
    # window at all.
    assert service.credentials.verify(NODE, ORIGINAL).verdict == node_credentials.REVOKED


# ---------------------------------------------------------------------------
# idempotence
# ---------------------------------------------------------------------------

def test_rotating_twice_does_not_strand_the_token_the_node_is_holding(service):
    service.adopt_existing(NODE, ORIGINAL)
    first = service.rotate(NODE)
    second = service.rotate(NODE)

    assert second["rotated"] is False
    assert second["pending_rotation"]["token_id"] == first["pending_rotation"]["token_id"]
    # The token the node actually has is still good -- a second rotation
    # would have pushed it out of grace.
    assert service.credentials.verify(NODE, ORIGINAL).accepted


def test_force_is_the_deliberate_way_to_replace_an_in_flight_rotation(service):
    service.adopt_existing(NODE, ORIGINAL)
    first = service.rotate(NODE)["pending_rotation"]["token_id"]
    second = service.rotate(NODE, force=True)
    assert second["rotated"] is True
    assert second["pending_rotation"]["token_id"] != first


def test_concurrent_rotations_stage_exactly_one_replacement(service):
    service.adopt_existing(NODE, ORIGINAL)
    start = threading.Barrier(6)

    def _rotate():
        start.wait()
        return service.rotate(NODE)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = [future.result() for future in [pool.submit(_rotate) for _ in range(6)]]

    assert sum(1 for result in results if result.get("rotated")) == 1
    active = [record for record in service.credentials.list_for(NODE)
              if record.status == node_credentials.ACTIVE]
    assert len(active) == 1


def test_collect_is_repeatable_until_confirmed(service):
    """A node that crashed between receiving and persisting must be able
    to ask again -- otherwise the recovery is a site visit."""
    service.adopt_existing(NODE, ORIGINAL)
    service.rotate(NODE)
    assert service.collect(NODE)["token"] == service.collect(NODE)["token"]
    service.confirm(NODE, service.collect(NODE)["token_id"])
    assert service.collect(NODE) == {"ok": False, "error": "NO_PENDING_ROTATION"}


def test_adopting_an_already_managed_node_changes_nothing(service):
    first = service.adopt_existing(NODE, ORIGINAL)
    second = service.adopt_existing(NODE, ORIGINAL)
    assert first["already_managed"] is False
    assert second["already_managed"] is True
    assert first["active_token_id"] == second["active_token_id"]


def test_adopting_reads_the_token_the_controller_already_holds(tmp_path, service):
    """Backward compatibility, with no operator input: a node enrolled
    before this feature is adopted from the credential already on disk."""
    connections = service.connection_store
    token_file = connections.write_token("legacy-node", "legacy-token-value")
    connections.save("legacy-node", transport_type="agent_token",
                     endpoint="http://10.0.0.9:8790", token_file=token_file)

    result = service.adopt_existing("legacy-node")
    assert result["ok"] is True
    assert service.credentials.verify("legacy-node", "legacy-token-value").accepted


def test_adopting_a_node_with_no_token_at_all_is_refused_not_invented(service):
    assert service.adopt_existing("ghost-node")["error"] == "NO_EXISTING_TOKEN"


# ---------------------------------------------------------------------------
# revocation
# ---------------------------------------------------------------------------

def test_revoking_a_node_also_stops_the_controller_using_it(service, applied):
    service.adopt_existing(NODE, ORIGINAL)
    service.rotate(NODE)

    result = service.revoke(NODE, reason="laptop_stolen")
    assert result["ok"] is True
    # Inbound: refused, with its own verdict.
    assert service.credentials.verify(NODE, ORIGINAL).verdict == node_credentials.REVOKED
    # Outbound: the controller was told to stop presenting it. A
    # revocation that leaves the controller still commanding the node
    # with the refused credential is a revocation in name only.
    assert applied == [(NODE, None)]
    # And the staged replacement is discarded, not left collectable.
    assert service.status(NODE)["pending_state"] == NONE
    assert service.collect(NODE)["error"] == "NO_PENDING_ROTATION"


def test_revoking_twice_is_the_same_as_revoking_once(service):
    service.adopt_existing(NODE, ORIGINAL)
    first = service.revoke(NODE)
    second = service.revoke(NODE)
    assert second["ok"] is True
    assert {row["token_id"] for row in first["revoked"]} == {row["token_id"] for row in second["revoked"]}
    assert service.credentials.verify(NODE, ORIGINAL).verdict == node_credentials.REVOKED


def test_one_leaked_old_token_can_be_killed_without_touching_the_live_one(service):
    service.adopt_existing(NODE, ORIGINAL)
    service.rotate(NODE)
    fresh = service.collect(NODE)["token"]
    service.confirm(NODE, node_credentials.token_fingerprint(fresh))
    service.rotate(NODE)
    newest = service.collect(NODE)["token"]

    service.revoke(NODE, token_id=node_credentials.token_fingerprint(fresh), reason="leaked_in_a_log")
    assert service.credentials.verify(NODE, fresh).verdict == node_credentials.REVOKED
    assert service.credentials.verify(NODE, newest).accepted


def test_rotating_an_unmanaged_node_is_refused_rather_than_guessed(service):
    assert service.rotate("never-seen")["error"] == "NODE_NOT_MANAGED"
    assert service.revoke("never-seen")["error"] == "NODE_NOT_MANAGED"


# ---------------------------------------------------------------------------
# nothing leaks
# ---------------------------------------------------------------------------

def test_no_audit_row_contains_a_token(tmp_path, service):
    service.adopt_existing(NODE, ORIGINAL)
    service.rotate(NODE)
    fresh = service.collect(NODE)["token"]
    service.confirm(NODE, node_credentials.token_fingerprint(fresh))
    service.rotate(NODE)
    newer = service.collect(NODE)["token"]
    service.revoke(NODE, reason="end of test")

    rows = service.audit.search(limit=200)["events"]
    blob = json.dumps(rows)
    assert ORIGINAL not in blob
    assert fresh not in blob
    assert newer not in blob
    # The whole file, not just what search() returns.
    raw = (tmp_path / "audit.db").read_bytes()
    for secret in (ORIGINAL, fresh, newer):
        assert secret.encode() not in raw
    # It is still a usable audit trail: every action is attributed and
    # names the credential by fingerprint.
    actions = {row["action"] for row in rows}
    assert {"node_token_adopt", "node_token_rotate", "node_token_deliver",
            "node_token_rotation_confirmed", "node_token_revoke"} <= actions
    assert all(row["reason"].startswith("token_id=") for row in rows if row["action"].startswith("node_token"))


def test_operator_facing_status_carries_no_secret(service):
    service.adopt_existing(NODE, ORIGINAL)
    service.rotate(NODE)
    fresh = service.collect(NODE)["token"]
    blob = json.dumps(service.status(NODE))
    assert ORIGINAL not in blob and fresh not in blob
    assert service.status(NODE)["active_token_id"] == node_credentials.token_fingerprint(fresh)


def test_the_staged_secret_is_0600_and_deleted_once_confirmed(tmp_path, service):
    service.adopt_existing(NODE, ORIGINAL)
    service.rotate(NODE)
    staged = tmp_path / "pending" / f"{NODE}.json"
    assert staged.exists()
    assert oct(staged.stat().st_mode)[-3:] == "600"
    fresh = service.collect(NODE)["token"]
    service.confirm(NODE, node_credentials.token_fingerprint(fresh))
    assert not staged.exists()


def test_a_tampered_staged_file_is_refused_not_handed_out(tmp_path, service):
    service.adopt_existing(NODE, ORIGINAL)
    service.rotate(NODE)
    staged = tmp_path / "pending" / f"{NODE}.json"
    record = json.loads(staged.read_text())
    record["token"] = "something-else-entirely"
    staged.write_text(json.dumps(record))
    # The fingerprint no longer matches the announced token_id, so the
    # node would be handed a credential the controller does not accept.
    assert service.collect(NODE)["error"] == "PENDING_ROTATION_UNREADABLE"


# ---------------------------------------------------------------------------
# the node's own half: AgentCredential (AC2 -- no restart)
# ---------------------------------------------------------------------------

def test_an_agent_swaps_its_token_in_place_and_keeps_accepting_the_old_one():
    credential = AgentCredential("first-token")
    assert credential.accepts("first-token")

    assert credential.adopt("second-token") is True
    # The new one works immediately -- no restart, which is the whole
    # point: a restart here costs every live session on that node.
    assert credential.accepts("second-token")
    # And the controller is still presenting the old one until it sees a
    # heartbeat signed with the new one, so that must not 401.
    assert credential.accepts("first-token")


def test_adopting_the_same_token_twice_does_not_shift_the_grace_window():
    credential = AgentCredential("first-token")
    credential.adopt("second-token")
    assert credential.adopt("second-token") is False
    assert credential.accepts("first-token"), "a duplicate hint must not evict the real previous token"


def test_the_previous_token_stops_being_accepted_once_its_window_closes():
    credential = AgentCredential("first-token", previous_grace_seconds=0)
    credential.adopt("second-token")
    assert credential.accepts("second-token")
    assert not credential.accepts("first-token")


def test_an_adopted_token_survives_the_next_agent_restart(tmp_path):
    token_file = tmp_path / "node.token"
    token_file.write_text("first-token")
    credential = AgentCredential("first-token", token_file=str(token_file))
    credential.adopt("second-token")
    assert token_file.read_text() == "second-token"
    assert oct(token_file.stat().st_mode)[-3:] == "600"
    # What a restarted agent would read.
    assert AgentCredential(token_file.read_text()).accepts("second-token")


def test_an_agent_refuses_a_delivered_token_that_is_not_the_announced_one(monkeypatch):
    """Adopting the wrong string locks the agent out until someone drives
    to the machine, so a mismatch is discarded rather than trusted."""
    credential = AgentCredential("first-token")

    class _Response:
        def read(self):
            return json.dumps({"token": "a-different-token"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("terminal_mcp.node_agent.urllib.request.urlopen", lambda *a, **k: _Response())
    adopted = _collect_rotated_token(controller_url="http://ctl", node_id=NODE, credential=credential,
                                     hint={"available": True, "token_id": "0123456789ab"})
    assert adopted is False
    assert credential.current == "first-token"


# ---------------------------------------------------------------------------
# the routes, end to end
# ---------------------------------------------------------------------------

def _client(tmp_path, credentials, heartbeat_replay=None):
    service = TerminalService(_config(), grants=SessionGrantStore(tmp_path / "grants.db"),
                              audit=AuditStore(tmp_path / "audit.db"))
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(service),
                                   local_workspace_root=str(tmp_path))
    connections = ConnectionStore(tmp_path / "connections.db")
    mcp = build_mcp(service)
    register_dashboard(mcp, service, controller=controller, connection_store=connections,
                       credentials=credentials, heartbeat_replay=heartbeat_replay)
    client = TestClient(mcp.streamable_http_app(), headers={"Origin": "http://testserver"})
    return client, controller


def test_a_full_rotation_over_the_real_routes_never_401s_the_node(tmp_path, monkeypatch):
    """AC1 + AC2 + AC3 in one pass, through HTTP, as a node would do it."""
    credentials = NodeCredentialStore(tmp_path / "credentials.db")
    client, controller = _client(tmp_path, credentials)
    controller.register_remote_node(NODE, display_name=NODE, hostname="win-rot",
                                    endpoint="http://10.0.0.5:8790", token=ORIGINAL)
    monkeypatch.setenv(node_token_env_var(NODE), ORIGINAL)
    old_headers = {"Authorization": f"Bearer {ORIGINAL}"}
    beat = {"metrics": {}, "tmux_session_count": 0}

    assert client.post(f"/dashboard/api/nodes/{NODE}/token/adopt").status_code == 200
    assert client.post(f"/dashboard/api/nodes/{NODE}/heartbeat", json=beat,
                       headers=old_headers).status_code == 200

    rotate = client.post(f"/dashboard/api/nodes/{NODE}/token/rotate", json={})
    assert rotate.status_code == 200 and rotate.json()["rotated"] is True
    staged_id = rotate.json()["pending_rotation"]["token_id"]
    assert ORIGINAL not in rotate.text, "an operator must never be handed a token"

    # The node has not noticed yet: its heartbeat still works, and now
    # carries the hint that tells it to collect.
    still_ok = client.post(f"/dashboard/api/nodes/{NODE}/heartbeat", json=beat, headers=old_headers)
    assert still_ok.status_code == 200
    assert still_ok.json()["token_refresh"] == {
        "available": True, "token_id": staged_id,
        "collect_path": f"/dashboard/api/nodes/{NODE}/token/refresh"}

    collected = client.post(f"/dashboard/api/nodes/{NODE}/token/refresh", headers=old_headers)
    assert collected.status_code == 200
    fresh = collected.json()["token"]

    # The node's next heartbeat, signed with the new token, completes it.
    replay_headers = {"Authorization": f"Bearer {fresh}", HEADER_TIMESTAMP: str(time.time()), HEADER_NONCE: "abcdefghijklmnop"}
    confirmed = client.post(f"/dashboard/api/nodes/{NODE}/heartbeat", json=beat, headers=replay_headers)
    assert confirmed.status_code == 200
    assert "token_refresh" not in confirmed.json()
    replayed = client.post(f"/dashboard/api/nodes/{NODE}/heartbeat", json=beat, headers=replay_headers)
    assert replayed.status_code == 409
    assert replayed.json()["error"] == "REPLAY_REJECTED"
    assert replayed.json()["verdict"] == REPLAYED_NONCE

    # AC3, at the route: the old token is refused, by name.
    refused = client.post(f"/dashboard/api/nodes/{NODE}/heartbeat", json=beat, headers=old_headers)
    assert refused.status_code == 401
    assert refused.json()["verdict"] == node_credentials.REVOKED
    assert fresh not in refused.text and ORIGINAL not in refused.text

    # AC1's "both sides": the controller moved its own copy with no
    # operator editing anything.
    assert os.environ[node_token_env_var(NODE)] == fresh
    client_for_node = controller.client_for(NODE)
    assert isinstance(client_for_node, RemoteNodeClient)
    assert client_for_node._token == fresh


def test_a_node_cannot_collect_another_nodes_token(tmp_path, monkeypatch):
    credentials = NodeCredentialStore(tmp_path / "credentials.db")
    client, controller = _client(tmp_path, credentials)
    credentials.adopt(NODE, ORIGINAL)
    credentials.adopt("other-node", "b" * 64)
    monkeypatch.setenv(node_token_env_var(NODE), ORIGINAL)
    client.post(f"/dashboard/api/nodes/{NODE}/token/rotate", json={})

    stolen = client.post(f"/dashboard/api/nodes/{NODE}/token/refresh",
                         headers={"Authorization": "Bearer " + "b" * 64})
    assert stolen.status_code == 401
    assert "token" not in stolen.json()


def test_an_unauthenticated_caller_cannot_collect_a_staged_token(tmp_path):
    credentials = NodeCredentialStore(tmp_path / "credentials.db")
    client, controller = _client(tmp_path, credentials)
    credentials.adopt(NODE, ORIGINAL)
    client.post(f"/dashboard/api/nodes/{NODE}/token/rotate", json={})
    assert client.post(f"/dashboard/api/nodes/{NODE}/token/refresh").status_code == 401


def test_a_revoked_node_is_refused_immediately_with_no_restart(tmp_path, monkeypatch):
    """AC2 at the route: revocation is enforced on the controller, so it
    takes effect on the node's very next request -- the node process is
    never touched, let alone restarted."""
    credentials = NodeCredentialStore(tmp_path / "credentials.db")
    client, controller = _client(tmp_path, credentials)
    controller.register_remote_node(NODE, display_name=NODE, hostname="h",
                                    endpoint="http://10.0.0.5:8790", token=ORIGINAL)
    monkeypatch.setenv(node_token_env_var(NODE), ORIGINAL)
    headers = {"Authorization": f"Bearer {ORIGINAL}"}
    beat = {"metrics": {}, "tmux_session_count": 0}
    client.post(f"/dashboard/api/nodes/{NODE}/token/adopt")
    assert client.post(f"/dashboard/api/nodes/{NODE}/heartbeat", json=beat, headers=headers).status_code == 200

    revoked = client.post(f"/dashboard/api/nodes/{NODE}/token/revoke", json={"reason": "stolen"})
    assert revoked.status_code == 200

    refused = client.post(f"/dashboard/api/nodes/{NODE}/heartbeat", json=beat, headers=headers)
    assert refused.status_code == 401
    assert refused.json()["verdict"] == node_credentials.REVOKED
    # Outbound too: this controller no longer presents the dead credential.
    assert controller.client_for(NODE)._token == ""
    assert node_token_env_var(NODE) not in os.environ


def test_token_status_is_readable_and_shows_no_secret(tmp_path):
    credentials = NodeCredentialStore(tmp_path / "credentials.db")
    client, _ = _client(tmp_path, credentials)
    credentials.adopt(NODE, ORIGINAL)
    response = client.get(f"/dashboard/api/nodes/{NODE}/token")
    assert response.status_code == 200
    assert ORIGINAL not in response.text
    assert response.json()["active_token_id"] == node_credentials.token_fingerprint(ORIGINAL)


def test_a_legacy_node_keeps_working_and_is_never_offered_a_rotation(tmp_path, monkeypatch):
    """No flag day: a node that has never been adopted authenticates
    exactly as it did before, and its heartbeat carries no hint."""
    credentials = NodeCredentialStore(tmp_path / "credentials.db")
    client, _ = _client(tmp_path, credentials)
    monkeypatch.setenv(node_token_env_var("legacy"), "legacy-secret")
    response = client.post("/dashboard/api/nodes/legacy/heartbeat",
                           json={"metrics": {}, "tmux_session_count": 0},
                           headers={"Authorization": "Bearer legacy-secret"})
    assert response.status_code in (200, 404)  # 404 only because it is not registered
    assert "token_refresh" not in response.json()
    assert client.post("/dashboard/api/nodes/legacy/token/rotate", json={}).json()["error"] == "NODE_NOT_MANAGED"
