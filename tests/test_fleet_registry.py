"""Fleet Metadata Registry: merge rules, secrets that must not travel, and
the question the whole feature exists for -- can a node still answer when the
controller is gone.

The tests that matter here are the ones about DIVERGENCE: two nodes handed
the same facts in a different order must agree, a reconnect must not undo a
delete, and nothing that looks like a credential may cross a wire.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from terminal_mcp.fleet_projection import (SshTargetFacts, classify_transport,
                                           parse_ssh_config, project_sessions,
                                           project_ssh_targets, ssh_target_id,
                                           ssh_targets_from_config)
from terminal_mcp.fleet_registry import (CRED_MISSING, CRED_PRESENT, KIND_NODE,
                                         KIND_PROJECT, KIND_SESSION, KIND_SSH_TARGET, FleetObject,
                                         FleetRegistryStore, SecretLeak, content_hash,
                                         scrub_payload, wins)
from terminal_mcp.fleet_service import FAIL, PASS, WARN, FleetService
from terminal_mcp.fleet_sync import FleetSyncService


@pytest.fixture
def store(tmp_path):
    return FleetRegistryStore(tmp_path / "fleet.db", local_node_id="m910")


def _node(store, node_id, **payload):
    return store.publish(KIND_NODE, f"node:{node_id}",
                         {"node_id": node_id, **payload}, owner_node=node_id)


# -- merge, idempotency, tombstones ------------------------------------------

def test_republishing_unchanged_content_does_not_mint_a_revision(store):
    """The projectors run on every poll. If an unchanged fact bumped its
    revision, the whole fleet would re-sync data that did not move."""
    first = _node(store, "hp-linux", display_name="hp")
    again = _node(store, "hp-linux", display_name="hp")
    assert (first.revision, again.revision) == (1, 1)
    assert _node(store, "hp-linux", display_name="hp2").revision == 2


def test_merging_the_same_batch_twice_changes_nothing(store):
    _node(store, "hp-linux", display_name="hp")
    batch = [obj.as_dict() for obj in store.export()]
    first = store.merge(batch, source_node="hp-linux")
    second = store.merge(batch, source_node="hp-linux")
    assert first["applied"] == 0 or second["applied"] == 0
    assert second["applied"] == 0, "a replayed batch must be a no-op"


def test_a_reconnecting_peer_cannot_resurrect_a_deleted_object(store):
    """The reconnect case, stated plainly: a peer that was offline when the
    delete happened comes back holding the OLD live version and pushes it."""
    live = _node(store, "hp-linux", display_name="hp")
    store.retire(KIND_NODE, "node:hp-linux")
    result = store.merge([live.as_dict()], source_node="hp-linux")
    assert result["applied"] == 0
    assert store.get(KIND_NODE, "node:hp-linux").deleted is True


def test_a_tombstone_beats_a_live_record_at_the_same_revision():
    """Concurrent delete and edit. Whoever merges first must not decide it:
    both nodes have to land on deleted."""
    now = "2026-09-12T00:00:00+00:00"
    live = FleetObject(kind=KIND_NODE, object_id="node:a", owner_node="a", revision=7,
                       updated_at=now, source_node="a", payload={"x": 1})
    dead = FleetObject(kind=KIND_NODE, object_id="node:a", owner_node="a", revision=7,
                       updated_at=now, source_node="b", deleted=True, deleted_at=now)
    assert wins(dead, live) is True
    assert wins(live, dead) is False


def test_two_nodes_given_the_same_versions_in_either_order_agree(tmp_path):
    """The convergence property. If this fails the fleet silently splits."""
    now = "2026-09-12T00:00:00+00:00"
    later = "2026-09-12T00:00:05+00:00"
    versions = [
        FleetObject(KIND_NODE, "node:a", "a", 3, now, "a", payload={"v": "three"}),
        FleetObject(KIND_NODE, "node:a", "a", 4, later, "b", payload={"v": "four"}),
        FleetObject(KIND_NODE, "node:a", "a", 4, now, "c", payload={"v": "other-four"}),
    ]
    left = FleetRegistryStore(tmp_path / "l.db", local_node_id="l")
    right = FleetRegistryStore(tmp_path / "r.db", local_node_id="r")
    left.merge(versions)
    right.merge(list(reversed(versions)))
    assert left.get(KIND_NODE, "node:a").payload == right.get(KIND_NODE, "node:a").payload


def test_only_the_owner_may_publish_an_object(store):
    """Single-writer-per-object is what lets the conflict policy stay this
    simple. Two writers would need a merge function that invents a winner."""
    _node(store, "hp-linux", display_name="hp")
    with pytest.raises(ValueError, match="owned by"):
        store.publish(KIND_NODE, "node:hp-linux", {"node_id": "hp-linux"},
                      owner_node="dell-linux")


def test_a_malformed_object_does_not_strand_the_rest_of_the_batch(store):
    result = store.merge([
        {"kind": "node", "object_id": "node:a", "owner_node": "a", "revision": 1,
         "payload": {"ok": True}},
        {"kind": "nonsense", "object_id": "x", "owner_node": "a"},
        {"kind": "node", "object_id": "node:b", "owner_node": "b", "revision": 1,
         "payload": {"ok": True}},
    ])
    assert result["applied"] == 2
    assert result["rejected"] == 1


def test_export_carries_tombstones_or_deletes_never_propagate(store):
    _node(store, "hp-linux")
    store.retire(KIND_NODE, "node:hp-linux")
    exported = store.export()
    assert any(obj.deleted for obj in exported), (
        "a peer that never receives tombstones hands the object straight back")


# -- secrets ------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"auth_token": "abc"},
    {"password": "hunter2"},
    {"ssh": {"private_key": "x"}},
    {"targets": [{"passphrase": "x"}]},
    {"api_key": "x"},
    {"bootstrap_secret": "x"},
])
def test_a_payload_naming_a_secret_is_refused_not_stripped(payload):
    """Refusal rather than a silent strip: a dropped field is a bug that
    ships, an exception is a bug that fails this test."""
    with pytest.raises(SecretLeak):
        scrub_payload(payload)


def test_private_key_material_is_caught_even_under_an_innocent_field_name():
    with pytest.raises(SecretLeak, match="private key material"):
        scrub_payload({"description": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc"})


@pytest.mark.parametrize("payload", [
    {"auth_token_ref": "TERMINAL_MCP_NODE_TOKEN_HP"},
    {"secret_ref": "file:/home/x/.local/state/tokens/hp"},
    {"token_file": "/var/lib/x/token"},
    {"credential_status": "MISSING_CREDENTIAL"},
    {"host_key_fingerprint": "SHA256:abcdef"},
])
def test_references_and_fingerprints_are_allowed_through(payload):
    """Pointing AT a secret is the supported pattern -- it is how a peer
    learns "the credential lives there" without the credential moving."""
    assert scrub_payload(dict(payload)) == payload


def test_a_peer_cannot_push_a_credential_onto_this_node(store):
    result = store.merge([{"kind": "ssh_target", "object_id": "ssh:x", "owner_node": "evil",
                           "revision": 99, "payload": {"password": "hunter2"}}])
    assert result["applied"] == 0 and result["rejected"] == 1
    assert store.get(KIND_SSH_TARGET, "ssh:x") is None


def test_the_ssh_projector_never_emits_a_key_path_or_its_contents(tmp_path):
    config = tmp_path / "config"
    config.write_text(
        "Host secret-box\n  HostName 10.0.0.9\n  User root\n"
        "  IdentityFile ~/.ssh/id_ed25519\n  Port 2222\n")
    targets = ssh_targets_from_config(config)
    assert len(targets) == 1
    payload = targets[0].payload()
    flat = json.dumps(payload)
    assert "id_ed25519" not in flat, "an IdentityFile path must not replicate"
    assert payload["host"] == "10.0.0.9" and payload["port"] == 2222
    # Existence-only: the key does not exist here, so the node reports that it
    # cannot use this target rather than asking anyone for the key.
    assert payload["credential_status"] == CRED_MISSING


# -- SSH identity, dedupe, transport -----------------------------------------

def test_a_wildcard_host_block_is_not_a_machine(tmp_path):
    config = tmp_path / "config"
    config.write_text("Host *\n  ServerAliveInterval 30\nHost real\n  HostName 10.0.0.1\n")
    assert [t.alias for t in ssh_targets_from_config(config)] == ["real"]


def test_one_machine_reached_three_ways_is_one_target(store):
    """LAN, tailnet and tunnel routes to the same box share a host key. Three
    rows would be three machines to an operator, which is wrong."""
    fingerprint = "SHA256:same-machine"
    targets = [
        SshTargetFacts(alias="dell-lan", host="192.168.1.132", port=22, username="dell",
                       transport="lan", host_key_fingerprint=fingerprint),
        SshTargetFacts(alias="dell-ts", host="100.81.85.120", port=22, username="dell",
                       transport="tailscale", host_key_fingerprint=fingerprint),
        SshTargetFacts(alias="dell-tunnel", host="dell.example.com", port=22, username="dell",
                       transport="tunnel", host_key_fingerprint=fingerprint),
    ]
    summary = project_ssh_targets(store, targets, local_node_id="m910")
    assert summary == {"published": 1, "deduped": 2, "skipped_foreign": 0}
    published = store.list(kind=KIND_SSH_TARGET)[0]
    assert set(published.payload["aliases"]) == {"dell-lan", "dell-ts", "dell-tunnel"}


def test_a_target_without_a_fingerprint_says_so_rather_than_pretending(store):
    targets = [SshTargetFacts(alias="a", host="10.0.0.1", port=22, username="x",
                              transport="lan")]
    project_ssh_targets(store, targets, local_node_id="m910")
    payload = store.list(kind=KIND_SSH_TARGET)[0].payload
    assert payload["id_basis"] == "address"


def test_identity_survives_the_address_changing():
    """A node that moves from LAN to tailnet is the same machine."""
    before = ssh_target_id(host_key_fingerprint="SHA256:x", host="192.168.1.132",
                           port=22, username="dell")
    after = ssh_target_id(host_key_fingerprint="SHA256:x", host="100.81.85.120",
                          port=22, username="dell")
    assert before == after


@pytest.mark.parametrize("host,expected", [
    ("100.81.85.120", "tailscale"),
    ("m910.tail281985.ts.net", "tailscale"),
    ("192.168.1.132", "lan"),
    ("10.0.0.5", "lan"),
    ("example.com", "unknown"),
])
def test_transport_is_read_off_the_address_not_the_alias(host, expected):
    assert classify_transport(host) == expected


def test_a_proxy_jump_makes_it_a_tunnel():
    assert classify_transport("192.168.1.9", proxy="bastion") == "tunnel"


# -- sessions -----------------------------------------------------------------

class _Record:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_a_session_keeps_its_identity_across_a_rename(store):
    record = _Record(node_id="hp-linux", stable_session_id="uuid-1", session_name="old",
                     status="ACTIVE", last_known_state="RUNNING")
    project_sessions(store, [record])
    record.session_name = "new"
    project_sessions(store, [record])
    objects = store.list(kind=KIND_SESSION)
    assert len(objects) == 1, "a rename must not create a second session"
    assert objects[0].payload["session_name"] == "new"


def test_a_purged_session_becomes_a_tombstone_not_a_disappearance(store):
    record = _Record(node_id="hp-linux", stable_session_id="uuid-1", session_name="s",
                     status="ACTIVE")
    project_sessions(store, [record])
    record.status = "DELETED"
    project_sessions(store, [record])
    assert store.get(KIND_SESSION, "session:hp-linux:uuid-1").deleted is True


def test_a_legacy_local_record_is_never_published_as_a_fleet_object(store):
    """The phantom this whole cleanup exists to stop. `local` resolves to a
    different machine on every node that reads it, so an object owned by it is
    unresolvable fleet-wide -- and a controller with legacy registry rows
    re-seeded exactly those objects on every refresh cycle, quietly undoing the
    ownership migration that had just removed them."""
    legacy = _Record(node_id="local", stable_session_id="uuid-legacy",
                     session_name="hp-codex1", status="ACTIVE")
    canonical = _Record(node_id="hp-linux", stable_session_id="uuid-canonical",
                        session_name="hp-codex1", status="ACTIVE")

    written = project_sessions(store, [legacy, canonical], local_node_id="hp-linux")

    assert written == 1, "only the canonical record may be published"
    assert store.get(KIND_SESSION, "session:local:uuid-legacy") is None
    assert store.get(KIND_SESSION, "session:hp-linux:uuid-canonical") is not None
    assert [obj.owner_node for obj in store.list(kind=KIND_SESSION)] == ["hp-linux"]


def test_a_legacy_local_record_is_not_reattributed_to_this_node(store):
    """Skipping, not claiming. `refresh_local` feeds this projector the
    controller's FLEET-wide listing, so a record saying `local` may belong to an
    un-migrated PEER -- taking it would move another machine's session onto this
    one on the strength of a label that means "unknown"."""
    legacy = _Record(node_id="local", stable_session_id="uuid-legacy",
                     session_name="peer-session", status="ACTIVE")

    project_sessions(store, [legacy], local_node_id="hp-linux")

    assert store.list(kind=KIND_SESSION) == []
    assert store.get(KIND_SESSION, "session:hp-linux:uuid-legacy") is None,         "the record must not be re-keyed onto this node either"


def test_a_retired_legacy_record_can_still_tombstone_its_fleet_object(store):
    """The guard must not strand what it is cleaning up: once
    retire_legacy_local_duplicates tombstones the registry row, that DELETED
    record has to be able to retire the phantom object, or it freezes in place
    forever instead of going away."""
    legacy = _Record(node_id="local", stable_session_id="uuid-legacy",
                     session_name="hp-codex1", status="ACTIVE")
    project_sessions(store, [legacy])  # published before the canonical id existed
    assert store.get(KIND_SESSION, "session:local:uuid-legacy").deleted is False

    legacy.status = "DELETED"
    project_sessions(store, [legacy], local_node_id="hp-linux")

    assert store.get(KIND_SESSION, "session:local:uuid-legacy").deleted is True


def test_a_node_with_no_canonical_id_still_projects_its_own_sessions(store):
    """A deployment that never named its controller has `local` as its real,
    working id. Nothing about it may change."""
    record = _Record(node_id="local", stable_session_id="uuid-1", session_name="s",
                     status="ACTIVE")

    assert project_sessions(store, [record], local_node_id="local") == 1
    assert project_sessions(store, [record]) == 1  # caller that passes nothing
    assert store.get(KIND_SESSION, "session:local:uuid-1") is not None


def test_no_pane_text_or_prompt_is_projected(store):
    """The sync runs between machines with different operators. It answers
    "what exists and where", never "what was typed"."""
    record = _Record(node_id="hp-linux", stable_session_id="uuid-1", session_name="s",
                     status="ACTIVE", last_output="SECRET PANE TEXT",
                     launch_command="claude --dangerously-skip-permissions")
    project_sessions(store, [record])
    flat = json.dumps(store.get(KIND_SESSION, "session:hp-linux:uuid-1").payload)
    assert "SECRET PANE TEXT" not in flat
    assert "dangerously" not in flat


def test_legacy_local_session_projection_keeps_canonical_owner_after_adoption(tmp_path):
    """Regression for the HP fleet-loop traceback seen on 2026-09-19.

    The registry migration has already adopted this machine's old
    ``owner_node="local"`` rows as ``hp-linux``. A still-legacy session
    registry record must not try to take that object back as owner ``local``.
    """
    path = tmp_path / "fleet.db"
    legacy = FleetRegistryStore(path, local_node_id="local")
    record = _Record(node_id="local", stable_session_id="uuid-legacy",
                     session_name="legacy", status="ACTIVE")
    project_sessions(legacy, [record])
    assert legacy.get(KIND_SESSION, "session:local:uuid-legacy").owner_node == "local"

    canonical = FleetRegistryStore(path, local_node_id="hp-linux")
    adopted = canonical.get(KIND_SESSION, "session:local:uuid-legacy")
    assert adopted.owner_node == "hp-linux"

    assert project_sessions(canonical, [record], local_node_id="hp-linux") == 1
    after = canonical.get(KIND_SESSION, "session:local:uuid-legacy")
    assert after.owner_node == "hp-linux"
    assert after.object_id == "session:local:uuid-legacy", \
        "keeping the legacy id avoids creating a duplicate session"


def test_controller_session_projection_skips_foreign_sessions_and_deletes(tmp_path):
    """A fleet-wide listing is read-only evidence for sessions owned by peers."""
    store = FleetRegistryStore(tmp_path / "fleet.db", local_node_id="hp-linux")
    foreign = _Record(node_id="dell-linux", stable_session_id="remote-1",
                      session_name="remote", status="ACTIVE")
    assert project_sessions(store, [foreign], local_node_id="hp-linux") == 0
    assert store.get(KIND_SESSION, "session:dell-linux:remote-1") is None

    store.publish(KIND_SESSION, "session:dell-linux:remote-1",
                  {"stable_session_id": "remote-1", "node_id": "dell-linux",
                   "session_name": "remote"}, owner_node="dell-linux")
    foreign.status = "DELETED"
    assert project_sessions(store, [foreign], local_node_id="hp-linux") == 0
    assert store.get(KIND_SESSION, "session:dell-linux:remote-1").deleted is False


def test_fleet_refresh_handles_legacy_local_session_without_projector_error(tmp_path):
    path = tmp_path / "fleet.db"
    legacy = FleetRegistryStore(path, local_node_id="local")
    record = _Record(node_id="local", stable_session_id="uuid-legacy",
                     session_name="legacy", status="ACTIVE")
    project_sessions(legacy, [record])

    store = FleetRegistryStore(path, local_node_id="hp-linux")
    service = FleetService(store, local_node_id="hp-linux")
    summary = service.refresh_local(sessions=[record], include_ssh_config=False, network={})

    assert summary["sessions"] == 1
    assert "sessions" not in summary.get("errors", {})
    assert store.get(KIND_SESSION, "session:local:uuid-legacy").owner_node == "hp-linux"

# -- sync ---------------------------------------------------------------------

def _pair(tmp_path):
    a = FleetRegistryStore(tmp_path / "a.db", local_node_id="hp-linux")
    b = FleetRegistryStore(tmp_path / "b.db", local_node_id="dell-linux")
    return (FleetSyncService(a, local_node_id="hp-linux"),
            FleetSyncService(b, local_node_id="dell-linux"))


def _wire(responder, source):
    def send(*, peer_node, endpoint, objects, since):
        return responder.handle_exchange({"objects": objects, "since": since},
                                         source_node=source)
    return send


def test_two_nodes_converge_without_a_controller(tmp_path):
    """The M910-off premise: this exchange has no controller in it at all."""
    left, right = _pair(tmp_path)
    left.store.publish(KIND_NODE, "node:hp-linux", {"node_id": "hp-linux"},
                       owner_node="hp-linux")
    right.store.publish(KIND_NODE, "node:dell-linux", {"node_id": "dell-linux"},
                        owner_node="dell-linux")
    left.exchange("dell-linux", transport=_wire(right, "hp-linux"))
    assert {o.object_id for o in left.store.list()} == {"node:hp-linux", "node:dell-linux"}
    assert {o.object_id for o in right.store.list()} == {"node:hp-linux", "node:dell-linux"}


def test_an_unreachable_peer_is_recorded_not_raised(tmp_path):
    """A peer being down is a normal fleet state. The local copy still
    answers, which is the entire reason it exists."""
    left, _ = _pair(tmp_path)

    def broken(**_kwargs):
        raise OSError("connection refused")

    result = left.exchange("dell-linux", transport=broken)
    assert result.ok is False and "connection refused" in result.error
    assert left.store.peers()[0]["last_error"]


def test_a_delete_propagates_across_a_reconnect(tmp_path):
    """The full reconnect story end to end: delete while the peer is away,
    then let it come back still holding the live version."""
    left, right = _pair(tmp_path)
    left.store.publish(KIND_NODE, "node:hp-linux", {"node_id": "hp-linux"},
                       owner_node="hp-linux")
    left.exchange("dell-linux", transport=_wire(right, "hp-linux"))
    left.store.retire(KIND_NODE, "node:hp-linux")
    left.exchange("dell-linux", transport=_wire(right, "hp-linux"))
    assert right.store.get(KIND_NODE, "node:hp-linux").deleted is True
    # And the peer pushing its stale live copy back does not undo it.
    right.exchange("hp-linux", transport=_wire(left, "dell-linux"))
    assert left.store.get(KIND_NODE, "node:hp-linux").deleted is True


# -- offline view and readiness ----------------------------------------------

def test_the_fleet_is_readable_with_no_network_at_all(tmp_path, monkeypatch):
    """Reads must not be able to block on a peer -- so this test makes any
    network call an error and then reads the whole fleet."""
    store = FleetRegistryStore(tmp_path / "f.db", local_node_id="dell-linux")
    store.merge([
        FleetObject(KIND_NODE, "node:m910", "m910", 4, "2026-09-12T00:00:00+00:00",
                    "m910", payload={"node_id": "m910", "display_name": "m910",
                                     "endpoint": "http://m910:8766"}),
        FleetObject(KIND_SSH_TARGET, "ssh:fp:x", "m910", 2, "2026-09-12T00:00:00+00:00",
                    "m910", payload={"alias": "m910", "host": "100.117.214.87",
                                     "transport": "tailscale", "preferred_order": 10}),
    ])
    service = FleetService(store, local_node_id="dell-linux")

    import urllib.request

    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline_view must not touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    view = service.offline_view()
    assert view["served_from"] == "local_cache"
    assert [n["node_id"] for n in view["nodes"]] == ["m910"]
    assert view["ssh_targets"][0]["host"] == "100.117.214.87"


def test_stale_metadata_is_labelled_rather_than_quietly_served(tmp_path):
    store = FleetRegistryStore(tmp_path / "f.db", local_node_id="a")
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    store.merge([FleetObject(KIND_NODE, "node:b", "b", 1, old, "b",
                             payload={"node_id": "b"})])
    node = FleetService(store, local_node_id="a").offline_view()["nodes"][0]
    assert node["metadata_stale"] is True
    assert node["metadata_age_seconds"] > 3600


def test_readiness_fails_on_a_fingerprint_mismatch_and_only_warns_on_a_missing_key(tmp_path):
    """A changed host key can mean impersonation; a missing credential means
    someone has to log in. Collapsing them to one severity trains an operator
    to ignore both."""
    store = FleetRegistryStore(tmp_path / "f.db", local_node_id="a")
    store.publish(KIND_NODE, "node:a", {"node_id": "a", "contract_version": 1})
    store.publish(KIND_SSH_TARGET, "ssh:fp:1",
                  {"alias": "dell", "host_key_fingerprint": "SHA256:new",
                   "credential_status": CRED_MISSING, "transport": "lan"})
    service = FleetService(store, local_node_id="a")

    warn_only = service.readiness()
    by_name = {c["check"]: c for c in warn_only["checks"]}
    assert by_name["ssh_credentials"]["status"] == WARN
    assert by_name["ssh_fingerprint_mismatch"]["status"] == PASS
    assert warn_only["status"] == WARN

    mismatched = service.readiness(known_fingerprints={"dell": "SHA256:old"})
    by_name = {c["check"]: c for c in mismatched["checks"]}
    assert by_name["ssh_fingerprint_mismatch"]["status"] == FAIL
    assert mismatched["status"] == FAIL


def test_readiness_warns_when_nodes_speak_different_contract_generations(tmp_path):
    store = FleetRegistryStore(tmp_path / "f.db", local_node_id="a")
    store.publish(KIND_NODE, "node:a", {"node_id": "a", "contract_version": 1})
    store.merge([FleetObject(KIND_NODE, "node:b", "b", 1,
                             datetime.now(timezone.utc).isoformat(), "b",
                             payload={"node_id": "b", "contract_version": 0})])
    checks = {c["check"]: c for c in FleetService(store, local_node_id="a").readiness()["checks"]}
    assert checks["contract_version_drift"]["status"] == WARN
    assert checks["contract_version_drift"]["evidence"]["versions"] == {"a": 1, "b": 0}


def test_a_node_with_no_registry_row_still_announces_its_own_addresses(tmp_path):
    """The bare node-agent case. It has no node_registry rows at all, so
    without this the one machine that most needs to say how it can be reached
    would be the only one that never did -- and a peer looking for a survivor
    after the controller is gone would find no address for it.
    """
    store = FleetRegistryStore(tmp_path / "f.db", local_node_id="hp-linux")
    service = FleetService(store, local_node_id="hp-linux")
    service.refresh_local(nodes=[], sessions=[], connections=[], include_ssh_config=False,
                          network={"lan_ip": "192.168.1.50",
                                   "tailscale_ip": "100.64.0.9",
                                   "tailscale_hostname": "hp.ts.net"})
    node = store.get(KIND_NODE, "node:hp-linux")
    assert node is not None and node.owner_node == "hp-linux"
    assert node.payload["lan_ip"] == "192.168.1.50"
    assert node.payload["tailscale_ip"] == "100.64.0.9"


def test_a_route_nobody_has_ever_verified_is_not_reported_as_fresh(tmp_path):
    """Found on the real fleet: every SSH route came from ~/.ssh/config, none
    had ever been verified, and the check passed anyway -- including for a
    node whose configured address is known to be dead."""
    store = FleetRegistryStore(tmp_path / "f.db", local_node_id="a")
    store.publish(KIND_NODE, "node:a", {"node_id": "a", "contract_version": 1})
    store.publish(KIND_SSH_TARGET, "ssh:addr:1",
                  {"alias": "dell-linux", "host": "192.168.1.132", "transport": "lan",
                   "credential_status": CRED_PRESENT, "last_verified_at": None})
    checks = {c["check"]: c for c in FleetService(store, local_node_id="a").readiness()["checks"]}
    assert checks["stale_routes"]["status"] == WARN
    assert checks["stale_routes"]["evidence"]["never_verified"] == ["dell-linux"]


@pytest.mark.parametrize("payload", [
    {"auth_ok": True},
    {"auth_status": "NEEDS_AUTH"},
    {"deploy_prereqs_ok": False},
])
def test_a_boolean_outcome_about_a_secret_is_not_a_secret(payload):
    """`auth_ok` is True or False. It says whether a credential worked, and
    carries none -- so the guard must not refuse it, while still refusing
    everything that could hold a value."""
    assert scrub_payload(dict(payload)) == payload


@pytest.mark.parametrize("payload", [
    {"auth": "Bearer abc"},
    {"auth_token": "abc"},
    {"authorization": "Basic xyz"},
])
def test_widening_the_suffix_allowlist_did_not_open_the_door(payload):
    with pytest.raises(SecretLeak):
        scrub_payload(payload)


# -- legacy 'local' node identity --------------------------------------------
#
# Every one of these reproduces a real production failure on the HP canonical
# controller (audited 2026-09-19). Before the fix its fleet_registry.db held
# 3112 rows still owned by the placeholder id "local" -- including 9 `project`
# rows that had arrived FROM dell-linux/dell-5530 -- and `fleet_loop` logged
#   ValueError: project:project:d94eeca841acfd35dae0 is owned by 'local',
#   not 'hp-linux' -- only its owner may publish it
# on every single cycle, which stopped projects, ssh targets AND the node's
# own identity row from being projected at all.


def _project_record(node_id, repo_root, *, branch="main", session="s", remote=None):
    return _Record(node_id=node_id, repo_root=repo_root, git_remote=remote,
                   git_branch=branch, session_name=session,
                   stable_session_id=f"uuid-{node_id}-{session}", status="ACTIVE")


def test_a_peers_legacy_local_rows_are_resolved_to_that_peer_not_to_us(tmp_path):
    """The ownership-theft case -- the reason a blanket rewrite is wrong.

    A row that arrived from dell-5530 while claiming owner "local" belongs to
    dell-5530. Rewriting every "local" row to whoever is reading would have
    handed this controller 2343 of another machine's sessions.
    """
    path = tmp_path / "fleet.db"
    store = FleetRegistryStore(path, local_node_id="hp-linux")
    store.merge([FleetObject(kind=KIND_SESSION, object_id="session:local:remote-1",
                             owner_node="local", revision=3,
                             updated_at="2026-09-18T00:00:00+00:00",
                             source_node="dell-5530", payload={"session_name": "w"})],
                source_node="dell-5530")
    obj = store.get(KIND_SESSION, "session:local:remote-1")
    assert obj.owner_node == "dell-5530", "a peer's row must stay the peer's"
    assert obj.owner_node != "hp-linux"


def test_our_own_legacy_local_rows_are_adopted_under_our_canonical_id(tmp_path):
    path = tmp_path / "fleet.db"
    # The historical shape: this machine published its own facts while it
    # still called itself "local", so owner AND source are the placeholder.
    legacy = FleetRegistryStore(path, local_node_id="local")
    legacy.publish(KIND_SESSION, "session:local:mine-1", {"session_name": "hp"},
                   owner_node="local")
    assert legacy.get(KIND_SESSION, "session:local:mine-1").owner_node == "local"

    # Reopening with the canonical id is what a deploy actually does.
    store = FleetRegistryStore(path, local_node_id="hp-linux")
    assert store.get(KIND_SESSION, "session:local:mine-1").owner_node == "hp-linux"


def test_adoption_is_idempotent_and_a_no_op_without_a_canonical_id(tmp_path):
    path = tmp_path / "fleet.db"
    legacy = FleetRegistryStore(path, local_node_id="local")
    legacy.publish(KIND_SESSION, "session:local:mine-1", {"session_name": "hp"},
                   owner_node="local")
    # A node that still has no canonical id must not "adopt" anything.
    assert FleetRegistryStore(path, local_node_id="local").adopt_legacy_local_ownership() == 0
    assert FleetRegistryStore(path, local_node_id="").adopt_legacy_local_ownership() == 0

    store = FleetRegistryStore(path, local_node_id="hp-linux")
    assert store.adopt_legacy_local_ownership() == 0, "first open already adopted it"
    revision = store.get(KIND_SESSION, "session:local:mine-1").revision
    FleetRegistryStore(path, local_node_id="hp-linux")
    assert store.get(KIND_SESSION, "session:local:mine-1").revision == revision, \
        "re-running must not mint a revision and re-sync the whole fleet"


def test_adoption_never_takes_a_row_a_peer_pushed_under_its_own_local_id(tmp_path):
    """The two halves must not fight: a peer's row is pinned by source_node
    BEFORE adoption can see it, so a restart cannot steal it later."""
    path = tmp_path / "fleet.db"
    store = FleetRegistryStore(path, local_node_id="hp-linux")
    store.merge([FleetObject(kind=KIND_SESSION, object_id="session:local:peer-1",
                             owner_node="local", revision=1,
                             updated_at="2026-09-18T00:00:00+00:00",
                             source_node="dell-linux", payload={"session_name": "d"})],
                source_node="dell-linux")
    reopened = FleetRegistryStore(path, local_node_id="hp-linux")
    assert reopened.get(KIND_SESSION, "session:local:peer-1").owner_node == "dell-linux"


# -- the single-writer rule the projectors have to respect --------------------

def test_a_project_owned_by_another_node_is_skipped_not_raised(tmp_path):
    """The exact production traceback: refresh_local passes the controller's
    FLEET-wide session listing, so this node derives a project for a repo that
    only ever existed on dell-linux."""
    from terminal_mcp.fleet_projection import project_projects

    store = FleetRegistryStore(tmp_path / "fleet.db", local_node_id="hp-linux")
    dell_only = _project_record("dell-linux", "/home/dell/workspace/novaretail-web")
    project_projects(store, [dell_only], local_node_id="dell-linux")   # dell got there first
    before = store.get(KIND_PROJECT, store.list(kind=KIND_PROJECT)[0].object_id)
    assert before.owner_node == "dell-linux"

    written = project_projects(store, [dell_only], local_node_id="hp-linux")

    assert written == 0, "we must not claim another node's project"
    after = store.get(KIND_PROJECT, before.object_id)
    assert after.owner_node == "dell-linux", "ownership must not move"
    assert after.revision == before.revision, "and no revision may be minted"


def test_our_own_projects_are_still_published_alongside_a_foreign_one(tmp_path):
    """Skipping the foreign project must not stop the rest of the batch --
    that silent all-or-nothing behaviour was the actual outage."""
    from terminal_mcp.fleet_projection import project_projects

    store = FleetRegistryStore(tmp_path / "fleet.db", local_node_id="hp-linux")
    foreign = _project_record("dell-linux", "/home/dell/workspace/novaretail-web")
    project_projects(store, [foreign], local_node_id="dell-linux")

    mine = _project_record("hp-linux", "/home/kimex/workspace/terminal-mcp", session="hp")
    written = project_projects(store, [foreign, mine], local_node_id="hp-linux")

    assert written == 1, "our own project must still be published"
    owners = {o.owner_node for o in store.list(kind=KIND_PROJECT)}
    assert owners == {"dell-linux", "hp-linux"}


def test_an_ssh_target_another_node_observed_first_is_skipped_not_raised(tmp_path):
    """Same rule, and the reason it is tested separately: an ssh target is
    keyed on the HOST, so any two nodes that can reach one box collide."""
    store = FleetRegistryStore(tmp_path / "fleet.db", local_node_id="hp-linux")
    target = SshTargetFacts(alias="box", host="192.168.1.50", port=22, username="kimex",
                            transport="lan", host_key_fingerprint="SHA256:abc")
    first = project_ssh_targets(store, [target], local_node_id="dell-linux")
    assert first["published"] == 1

    second = project_ssh_targets(store, [target], local_node_id="hp-linux")

    assert second["skipped_foreign"] == 1
    assert second["published"] == 0
    key = store.list(kind=KIND_SSH_TARGET)[0].object_id
    assert store.get(KIND_SSH_TARGET, key).owner_node == "dell-linux"


def test_one_failing_projector_does_not_silently_drop_the_later_ones(tmp_path):
    """The outage's real blast radius: `projects` raising took ssh targets and
    this node's OWN identity row with it, on every cycle, reported as a single
    'local refresh failed' line."""
    store = FleetRegistryStore(tmp_path / "fleet.db", local_node_id="hp-linux")
    service = FleetService(store, local_node_id="hp-linux")

    import terminal_mcp.fleet_service as fs

    def _boom(*_args, **_kwargs):
        raise ValueError("project:x is owned by 'local', not 'hp-linux'")

    original = fs.project_projects
    fs.project_projects = _boom
    try:
        summary = service.refresh_local(nodes=[], sessions=[], connections=[],
                                        include_ssh_config=False,
                                        network={"node_id": "hp-linux", "lan_ip": "10.0.0.1"})
    finally:
        fs.project_projects = original

    assert "errors" in summary and "projects" in summary["errors"], \
        "the failure must be reported, not swallowed"
    assert summary["ssh"] is not None, "ssh must still have been projected"
    identity = store.get(KIND_NODE, "node:hp-linux")
    assert identity is not None, "this node's own identity row must still be published"
    assert identity.payload["lan_ip"] == "10.0.0.1"


def test_refresh_local_does_not_re_seed_a_phantom_local_owner(tmp_path):
    """End to end through the real call path, because that is where the
    regression actually happened: the ownership migration cleared every
    `local`-owned row, and then the very next refresh cycle put five of them
    straight back, because the controller still holds legacy registry rows and
    the projector published them verbatim."""
    store = FleetRegistryStore(tmp_path / "fleet.db", local_node_id="hp-linux")
    service = FleetService(store, local_node_id="hp-linux")
    legacy = _Record(node_id="local", stable_session_id="uuid-legacy",
                     session_name="hp-codex1", status="ACTIVE")
    canonical = _Record(node_id="hp-linux", stable_session_id="uuid-canonical",
                        session_name="hp-codex1", status="ACTIVE")

    for _ in range(3):  # three cycles, exactly like the loop does
        summary = service.refresh_local(nodes=[], sessions=[legacy, canonical],
                                        connections=[], include_ssh_config=False,
                                        network={"node_id": "hp-linux"})

    assert "errors" not in summary, "the refresh must be clean, not merely quiet"
    assert store.list(kind=KIND_SESSION, owner_node="local") == []
    assert [obj.object_id for obj in store.list(kind=KIND_SESSION)] == \
        ["session:hp-linux:uuid-canonical"]
