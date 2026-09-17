"""Redundant SSH paths: the acceptance cases, stated as tests.

The one bug worth preventing here is claiming 2/2 when the second path hops
through the first. Most of what follows is that claim, attacked from a
different angle each time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from terminal_mcp.deployment_redundancy import (DEGRADED, FAIL, PATH_DEPENDENT,
                                                PATH_NEEDS_AUTH, PATH_NODE_OFFLINE,
                                                PATH_READY, PATH_UNREACHABLE,
                                                PATH_UNVERIFIED, READY, DeploymentPath,
                                                DeploymentTarget, choose_deploy_node,
                                                deploy_lease_key, evaluate_target,
                                                jump_hosts, path_depends_on_peer)

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(minutes=5)).isoformat()


def _target(**kwargs):
    fields = {"target_id": "mesflow-vps", "display_name": "MESFlow VPS",
              "project_id": "mesflow", "min_independent_paths": 2,
              "primary_node": "dell-linux", "backup_nodes": ("hp-linux",)}
    fields.update(kwargs)
    return DeploymentTarget(**fields)


def _path(node, *, ok=True, auth=True, proxy=None, group=None, probe=FRESH,
          latency=20.0, prereqs=True, role="backup"):
    return DeploymentPath(
        target_id="mesflow-vps", node_id=node, ssh_alias=f"{node}-vps",
        host="100.64.0.9", port=22, username="deploy", transport="tailscale",
        proxy_jump=proxy, independence_group=group,
        host_key_fingerprint="SHA256:target", public_key_id=f"SHA256:{node}-pub",
        capabilities=("ssh", "deploy"), last_probe_at=probe, last_probe_ok=ok,
        last_probe_reason=None if ok else "connection refused",
        latency_ms=latency, auth_ok=auth, deploy_prereqs_ok=prereqs, role=role)


# -- the acceptance matrix ----------------------------------------------------

def test_both_nodes_healthy_is_two_of_two_ready():
    result = evaluate_target(_target(), [_path("dell-linux", role="primary"),
                                         _path("hp-linux")], now=NOW)
    assert result["status"] == READY
    assert result["redundancy"]["label"] == "2/2"
    assert result["deploy_available"] is True
    assert result["warnings"] == []


def test_primary_offline_but_backup_independent_is_degraded_and_still_deployable():
    """The case that must not be reported as FAIL. Calling a working target
    FAIL is how someone holds a release they could have shipped."""
    result = evaluate_target(_target(), [_path("dell-linux", role="primary"),
                                         _path("hp-linux")],
                             node_online={"dell-linux": False, "hp-linux": True}, now=NOW)
    assert result["status"] == DEGRADED
    assert result["redundancy"]["label"] == "1/2"
    assert result["deploy_available"] is True
    states = {p["node_id"]: p["state"] for p in result["paths"]}
    assert states["dell-linux"] == PATH_NODE_OFFLINE
    assert states["hp-linux"] == PATH_READY


def test_backup_offline_leaves_the_primary_deploying():
    result = evaluate_target(_target(), [_path("dell-linux", role="primary"),
                                         _path("hp-linux")],
                             node_online={"dell-linux": True, "hp-linux": False}, now=NOW)
    assert result["status"] == DEGRADED
    assert result["deploy_available"] is True
    assert choose_deploy_node(result)["node_id"] == "dell-linux"


def test_both_offline_is_fail():
    result = evaluate_target(_target(), [_path("dell-linux"), _path("hp-linux")],
                             node_online={"dell-linux": False, "hp-linux": False}, now=NOW)
    assert result["status"] == FAIL
    assert result["redundancy"]["label"] == "0/2"
    assert result["deploy_available"] is False


def test_a_path_that_jumps_through_the_peer_is_not_a_second_path():
    """The headline case. Both nodes work, both would deploy today, and it is
    still one path -- the day dell-linux dies, hp-linux dies with it."""
    result = evaluate_target(
        _target(),
        [_path("dell-linux", role="primary"), _path("hp-linux", proxy="dell-linux")],
        now=NOW)
    assert result["redundancy"]["label"] == "1/2"
    assert result["status"] == DEGRADED
    assert result["deploy_available"] is True, "it still deploys -- it just is not redundant"
    hp = [p for p in result["paths"] if p["node_id"] == "hp-linux"][0]
    assert hp["state"] == PATH_DEPENDENT
    assert hp["depends_on"] == "dell-linux"
    assert hp["counts_toward_redundancy"] is False
    assert any("one path wearing two names" in w for w in result["warnings"])


def test_a_jump_host_written_as_an_address_is_still_the_peer():
    """A config saying `ProxyJump 192.168.1.132` routes through dell-linux
    just as surely as one saying `ProxyJump dell-linux`. Comparing ids alone
    would miss it, which is the quiet version of the headline bug."""
    result = evaluate_target(
        _target(),
        [_path("dell-linux"), _path("hp-linux", proxy="dell@192.168.1.132:22")],
        node_aliases={"dell-linux": {"192.168.1.132", "dell-linux.ts.net"}}, now=NOW)
    assert result["redundancy"]["label"] == "1/2"
    hp = [p for p in result["paths"] if p["node_id"] == "hp-linux"][0]
    assert hp["depends_on"] == "dell-linux"


def test_a_node_that_cannot_authenticate_is_degraded_and_named():
    result = evaluate_target(_target(), [_path("dell-linux", role="primary"),
                                         _path("hp-linux", auth=False)], now=NOW)
    assert result["status"] == DEGRADED
    assert result["redundancy"]["label"] == "1/2"
    hp = [p for p in result["paths"] if p["node_id"] == "hp-linux"][0]
    assert hp["state"] == PATH_NEEDS_AUTH
    assert "PUBLIC key" in hp["reason"], "the fix is to install a public key, not copy a private one"


def test_the_dispatcher_picks_the_backup_when_the_primary_is_dead():
    result = evaluate_target(_target(), [_path("dell-linux", role="primary"),
                                         _path("hp-linux")],
                             node_online={"dell-linux": False, "hp-linux": True}, now=NOW)
    choice = choose_deploy_node(result)
    assert choice["node_id"] == "hp-linux"
    assert choice["role"] == "backup"
    assert choice["deploy_available"] is True
    # The reason must say the primary was skipped, not silently substitute.
    assert "primary unavailable" in choice["reason"]
    assert [s["node_id"] for s in choice["skipped"]] == ["dell-linux"]


def test_the_dispatcher_prefers_the_primary_when_both_work():
    result = evaluate_target(_target(), [_path("dell-linux", role="primary", latency=90.0),
                                         _path("hp-linux", latency=5.0)], now=NOW)
    choice = choose_deploy_node(result)
    assert choice["node_id"] == "dell-linux", "preference beats latency; evidence beats preference"
    assert choice["role"] == "primary"


def test_nothing_deployable_reports_why_each_path_was_rejected():
    result = evaluate_target(_target(), [_path("dell-linux", ok=False),
                                         _path("hp-linux", ok=False)], now=NOW)
    choice = choose_deploy_node(result)
    assert choice["node_id"] is None and choice["deploy_available"] is False
    assert {c["node_id"] for c in choice["considered"]} == {"dell-linux", "hp-linux"}


# -- evidence discipline ------------------------------------------------------

def test_a_configured_but_never_probed_path_counts_for_nothing():
    """A config file is a plan, not a route."""
    result = evaluate_target(_target(), [_path("dell-linux"),
                                         _path("hp-linux", ok=None, probe=None)], now=NOW)
    assert result["redundancy"]["label"] == "1/2"
    hp = [p for p in result["paths"] if p["node_id"] == "hp-linux"][0]
    assert hp["state"] == PATH_UNVERIFIED
    assert any("never proven" in w for w in result["warnings"])


def test_a_stale_success_stops_counting():
    """A route proven last month is a story about last month."""
    old = (NOW - timedelta(days=3)).isoformat()
    result = evaluate_target(_target(), [_path("dell-linux"),
                                         _path("hp-linux", probe=old)], now=NOW)
    assert result["redundancy"]["label"] == "1/2"
    hp = [p for p in result["paths"] if p["node_id"] == "hp-linux"][0]
    assert hp["state"] == PATH_UNVERIFIED
    assert "stale" in hp["reason"]


def test_two_paths_behind_one_gateway_are_flagged_even_while_both_work():
    """Shared fate is only actionable while everything is still green."""
    result = evaluate_target(_target(), [_path("dell-linux", group="office-gw"),
                                         _path("hp-linux", group="office-gw")], now=NOW)
    assert result["status"] == READY, "both work today"
    assert result["shared_fate"] == [{"independence_group": "office-gw",
                                      "nodes": ["dell-linux", "hp-linux"]}]
    assert any("fail together" in w for w in result["warnings"])


def test_an_unreachable_path_reports_the_probe_reason():
    result = evaluate_target(_target(), [_path("dell-linux"), _path("hp-linux", ok=False)],
                             now=NOW)
    hp = [p for p in result["paths"] if p["node_id"] == "hp-linux"][0]
    assert hp["state"] == PATH_UNREACHABLE
    assert hp["reason"] == "connection refused"


def test_a_higher_requirement_is_honoured():
    """min_independent_paths is a requirement the operator sets, not a
    description of what happens to exist."""
    result = evaluate_target(_target(min_independent_paths=3),
                             [_path("a"), _path("b")], now=NOW)
    assert result["redundancy"] == {"achieved": 2, "required": 3, "label": "2/3"}
    assert result["status"] == DEGRADED


# -- jump parsing -------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("dell-linux", ["dell-linux"]),
    ("dell@192.168.1.132:22", ["192.168.1.132"]),
    ("bastion,dell-linux", ["bastion", "dell-linux"]),
    ("ssh -W %h:%p dell-linux", ["dell-linux"]),
    (None, []),
    ("", []),
])
def test_every_hop_is_found_not_just_the_first(value, expected):
    """Missing a second-hop peer would be the worst possible way to be wrong
    about independence."""
    assert jump_hosts(value) == expected


def test_a_jump_through_a_non_peer_does_not_break_pair_independence():
    """A corporate bastion is a dependency, but it is not the OTHER
    management node -- the pair is still two paths."""
    path = DeploymentPath(target_id="t", node_id="hp-linux", proxy_jump="corp-bastion")
    assert path_depends_on_peer(path, ["dell-linux", "hp-linux"]) is None


# -- lease --------------------------------------------------------------------

def test_the_deploy_lease_is_keyed_on_the_target_not_the_node():
    """Keying on the node would let two dispatchers each take their own
    node's lease and both deploy -- the exact double-execution this
    prevents."""
    assert deploy_lease_key("mesflow-vps") == deploy_lease_key("mesflow-vps")
    assert deploy_lease_key("mesflow-vps") != deploy_lease_key("other-vps")
    assert "dell-linux" not in deploy_lease_key("mesflow-vps")
