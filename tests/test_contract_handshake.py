"""Protocol generation, so a version mismatch degrades loudly.

`AGENT_GENERATION` is a random token minted per process -- it answers "did
this agent restart?" and nothing else. Two nodes running completely different
code report two random strings that look exactly as different as two restarts
of one build. There was no way for a controller to know whether a node spoke
the same protocol before routing work to it.

Not academic: audited 2026-09-11, dell-linux ran a commit the controller had
never seen, still deciding access by the retired session-name whitelist, and
nothing anywhere reported a mismatch.
"""
from __future__ import annotations

import json

from terminal_mcp import contract
from terminal_mcp.node_models import node_to_dict
from terminal_mcp.node_registry import NodeRegistry
from terminal_mcp.host_metrics import NodeMetrics


def test_describe_does_not_collide_with_probed_tool_capabilities():
    """The heartbeat body already carries `capabilities` for PROBED tool
    features (git/docker/python). The first wiring of this reused that name
    and silently clobbered the probed list -- two different questions must not
    share a field."""
    payload = contract.describe()
    assert set(payload) == {"contract_version", "contract_capabilities"}
    assert "capabilities" not in payload


def test_a_peer_reporting_nothing_is_legacy_never_compatible():
    result = contract.compatibility(None)
    assert result["status"] == "degraded"
    assert result["peer_contract_version"] == contract.LEGACY_CONTRACT_VERSION
    assert result["missing_capabilities"] == sorted(contract.CAPABILITIES)


def test_same_version_is_ok():
    result = contract.compatibility(contract.CONTRACT_VERSION, contract.CAPABILITIES)
    assert result["status"] == "ok"
    assert result["missing_capabilities"] == []


def test_an_older_peer_degrades_and_names_what_is_missing():
    """Routable, but the caller is told exactly which capabilities it may not
    assume -- that is the case that used to be invisible."""
    result = contract.compatibility(0, [contract.CAP_KEY_SENDS])
    assert result["status"] == "degraded"
    assert contract.CAP_GRANT_ONLY_ACCESS in result["missing_capabilities"]
    assert contract.CAP_KEY_SENDS not in result["missing_capabilities"]


def test_a_newer_peer_is_refused_not_guessed_at():
    """A peer speaking a contract this build does not implement may be relying
    on semantics that do not exist here; guessing is how a wrong route becomes
    a wrong write."""
    result = contract.compatibility(contract.CONTRACT_VERSION + 1)
    assert result["status"] == "refused"
    assert str(contract.CONTRACT_VERSION) in result["reason"]


def test_unknown_capabilities_from_a_newer_peer_are_ignored_not_fatal():
    """Capabilities are additive: a newer node must be able to talk to an
    older controller."""
    result = contract.compatibility(contract.CONTRACT_VERSION,
                                    list(contract.CAPABILITIES) + ["some_future_feature"])
    assert result["status"] == "ok"


# -- persistence -------------------------------------------------------------

def _metrics() -> NodeMetrics:
    return NodeMetrics(cpu_percent=1.0, load1=0.1, load5=0.1, load15=0.1, cpu_count=4,
                       ram_total_bytes=1, ram_used_bytes=1, ram_percent=1.0,
                       swap_total_bytes=1, swap_used_bytes=0, swap_percent=0.0,
                       disk_total_bytes=1, disk_used_bytes=1, disk_free_bytes=0, disk_percent=1.0)


def _heartbeat(registry, node_id, **kwargs):
    registry.register(node_id, display_name=node_id, hostname="h", endpoint="http://x:8790")
    return registry.heartbeat(node_id, metrics=_metrics(), tmux_session_count=0,
                              agent_counts={}, agent_types=("shell",), agent_version="0.12.0",
                              labels=(), **kwargs)


def test_contract_is_persisted_and_serialized_separately_from_tool_capabilities(tmp_path):
    registry = NodeRegistry(tmp_path / "nodes.db")
    node = _heartbeat(registry, "n1",
                      capabilities=("git", "docker"),
                      contract_version=contract.CONTRACT_VERSION,
                      contract_capabilities=tuple(contract.CAPABILITIES))
    assert node.contract_version == contract.CONTRACT_VERSION
    assert set(node.contract_capabilities) == set(contract.CAPABILITIES)
    # The probed list survived intact -- the collision this guards against.
    assert set(node.capabilities) == {"git", "docker"}

    payload = node_to_dict(node)
    assert payload["contract_version"] == contract.CONTRACT_VERSION
    assert sorted(payload["contract_capabilities"]) == sorted(contract.CAPABILITIES)
    assert sorted(payload["capabilities"]) == ["docker", "git"]


def test_a_node_that_reports_no_contract_is_recorded_as_legacy(tmp_path):
    """An older agent that knows nothing about this simply stays at 0, rather
    than inheriting whatever the row happened to hold before."""
    registry = NodeRegistry(tmp_path / "nodes.db")
    node = _heartbeat(registry, "old-node", capabilities=("git",))
    assert node.contract_version == contract.LEGACY_CONTRACT_VERSION
    assert node.contract_capabilities == ()
    assert contract.compatibility(node.contract_version, node.contract_capabilities)["status"] == "degraded"


def test_existing_rows_migrate_without_the_columns(tmp_path):
    """Backward compatible: a registry created before these columns existed
    opens and answers, it does not fail."""
    path = tmp_path / "nodes.db"
    NodeRegistry(path)  # creates with the columns
    reopened = NodeRegistry(path)  # second open must be a no-op migration
    reopened.register("n2", display_name="n2", hostname="h", endpoint="http://y:8790")
    assert reopened.get("n2").contract_version == 0
