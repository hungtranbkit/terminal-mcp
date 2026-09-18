"""P0.3 Worker capability registry -- the tool/runtime axis.

The contract: a capability means "this binary actually resolves on THIS
node right now", never "an operator declared it". That is the same rule
agent_availability.py exists to enforce for launchers, and it is why
capabilities are a SEPARATE field from `labels` (operator tags).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from terminal_mcp import host_metrics
from terminal_mcp.capability_probe import (DEFAULT_CAPABILITY_PROBES, clear_cache,
                                           probe_capabilities)
from terminal_mcp.node_models import node_to_dict
from terminal_mcp.node_registry import NodeRegistry

PROD_NODES_DB = Path.home() / ".local/state/terminal-mcp/nodes.db"


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def registry(tmp_path):
    return NodeRegistry(tmp_path / "nodes.db")


def _beat(registry, node_id, capabilities):
    registry.register(node_id, display_name=node_id, hostname="h", endpoint="local")
    return registry.heartbeat(node_id, metrics=host_metrics.collect(workspace_path="/"),
                              tmux_session_count=0, agent_counts={}, agent_types=("shell",),
                              agent_version="1", labels=(), capabilities=capabilities)


# ------------------------------------------------------------------ probe
def test_probe_reports_only_binaries_that_resolve():
    found = probe_capabilities(use_cache=False)
    for name in found:
        binary = dict(DEFAULT_CAPABILITY_PROBES).get(name, name)
        assert shutil.which(binary) is not None, f"{name} reported but {binary} does not resolve"


def test_probe_omits_a_binary_that_does_not_exist():
    out = probe_capabilities(use_cache=False, probes=[("definitely-absent", "no-such-binary-xyz")])
    assert out == ()


def test_probe_reports_one_that_does_exist():
    assert probe_capabilities(use_cache=False, probes=[("sh", "sh")]) == ("sh",)


def test_empty_is_a_legitimate_answer_not_unknown():
    assert probe_capabilities(use_cache=False, probes=[("nope", "no-such-binary-xyz")]) == ()


def test_probe_never_executes_the_tool(monkeypatch):
    """Detection must stay shutil.which-only: a heartbeat every 20s on
    every node must never shell out, and a hanging tool must never be
    able to stall the heartbeat loop."""
    import subprocess

    def explode(*a, **k):
        raise AssertionError("capability probe executed a subprocess")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "check_output", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    probe_capabilities(use_cache=False)


def test_cache_avoids_rewalking_path(monkeypatch):
    calls = {"n": 0}
    real = shutil.which

    def counted(binary, *a, **k):
        calls["n"] += 1
        return real(binary, *a, **k)

    monkeypatch.setattr("terminal_mcp.launcher_resolution.shutil.which", counted)
    clear_cache()
    probe_capabilities()
    first = calls["n"]
    probe_capabilities()
    assert calls["n"] == first, "cached probe re-walked PATH"


def test_env_can_extend_but_never_fabricate(monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_CAPABILITY_PROBES", "madeup=no-such-binary-xyz,shell2=sh")
    out = probe_capabilities(use_cache=False)
    assert "madeup" not in out, "an operator-declared capability appeared without the binary"
    assert "shell2" in out


# --------------------------------------------------------------- registry
def test_capabilities_round_trip(registry):
    node = _beat(registry, "n1", ("git", "playwright"))
    assert node.capabilities == ("git", "playwright")
    assert registry.get("n1").capabilities == ("git", "playwright")
    assert node_to_dict(registry.get("n1"))["capabilities"] == ["git", "playwright"]


def test_capabilities_are_separate_from_labels(registry):
    """Conflating probed capability with operator tags is the bug this
    design avoids -- they must not share a field."""
    node = _beat(registry, "n1", ("git",))
    assert node.labels == () and node.capabilities == ("git",)


def test_selection_uses_AND_semantics(registry):
    _beat(registry, "linux-box", ("git", "node", "playwright"))
    _beat(registry, "win-box", ("git", "dotnet"))
    ids = lambda req: sorted(n.id for n in registry.nodes_with_capabilities(req))
    assert ids(("playwright",)) == ["linux-box"]
    assert ids(("dotnet",)) == ["win-box"]
    assert ids(("git", "node")) == ["linux-box"]
    assert ids(("git",)) == ["linux-box", "win-box"]
    assert ids(("playwright", "dotnet")) == []          # no node has both


def test_empty_requirement_matches_everything(registry):
    _beat(registry, "a", ("git",))
    _beat(registry, "b", ())
    assert sorted(n.id for n in registry.nodes_with_capabilities(())) == ["a", "b"]


def test_a_node_reporting_nothing_is_never_assumed_capable(registry):
    """An older agent that does not send capabilities must match only the
    empty requirement -- never be treated as able to do the work."""
    _beat(registry, "old-agent", ())
    assert registry.get("old-agent").capabilities == ()
    assert registry.nodes_with_capabilities(("playwright",)) == []


def test_offline_nodes_excluded_by_default(registry, monkeypatch):
    _beat(registry, "n1", ("git",))
    assert [n.id for n in registry.nodes_with_capabilities(("git",))] == ["n1"]
    # online_only=False must still find it regardless of status derivation
    assert [n.id for n in registry.nodes_with_capabilities(("git",), online_only=False)] == ["n1"]


# -------------------------------------------------------------- migration
def test_column_added_to_a_fresh_registry(tmp_path):
    import sqlite3
    registry = NodeRegistry(tmp_path / "n.db")
    cols = {r[1] for r in sqlite3.connect(registry.path).execute("PRAGMA table_info(nodes)")}
    assert "capabilities" in cols


@pytest.mark.skipif(not PROD_NODES_DB.exists(), reason="no real nodes.db on this host")
def test_migrates_a_copy_of_the_REAL_production_registry(tmp_path):
    import shutil as sh
    import sqlite3
    copy = tmp_path / "prod-nodes.db"
    sh.copy(PROD_NODES_DB, copy)
    before = sqlite3.connect(copy)
    node_count = before.execute("select count(*) from nodes").fetchone()[0]
    before.close()

    # capture what the source DB actually holds BEFORE migrating, so the
    # assertion is "migration preserves and never invents", not "production
    # happens to be empty" -- the latter was true only until the fleet was
    # redeployed and started reporting real capabilities.
    src = sqlite3.connect(copy)
    has_column = "capabilities" in {r[1] for r in src.execute("PRAGMA table_info(nodes)")}
    original = ({r[0]: r[1] for r in src.execute("select id, capabilities from nodes")}
                if has_column else {})
    src.close()

    registry = NodeRegistry(copy)

    after = sqlite3.connect(copy)
    assert after.execute("select count(*) from nodes").fetchone()[0] == node_count
    assert "capabilities" in {r[1] for r in after.execute("PRAGMA table_info(nodes)")}
    for node in registry.list():
        if has_column:
            # preserved exactly -- migration must not rewrite real data
            assert list(node.capabilities) == json.loads(original[node.id] or "[]")
        else:
            # a pre-existing DB without the column defaults to empty,
            # never to a guessed capability
            assert node.capabilities == ()
