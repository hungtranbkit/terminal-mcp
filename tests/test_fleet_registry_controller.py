"""Fleet-aware registry reads through the REAL ControllerService
(task blg_84f09bbc1798) -- the fan-out wiring, not the merge arithmetic
(that is tests/test_fleet_registry.py).

Uses a real TerminalService + real SessionRegistryStore for the local
node, and a registry-only fake for the "remote" one, same posture as
tests/test_controller.py. Every store is a tmp_path fixture -- no real
~/.local/state/terminal-mcp/*.db is touched.
"""
from __future__ import annotations

from typing import Any

import pytest

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.controller import ControllerService
from terminal_mcp.core import TerminalService
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.host_metrics import NodeMetrics
from terminal_mcp.node_client import LocalNodeClient, NodeClientError
from terminal_mcp.node_registry import NodeRegistry
from terminal_mcp.session_registry import SessionRegistryStore


def _config(tmp_path) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True),
        allowed_session_patterns=("fleet-*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("fleet-*",)),
        session_lifecycle=SessionLifecycleConfig(
            enabled=True, allowed_cwd_roots=(str(tmp_path),), protected_sessions=(),
            launch_commands=(("claude", "true"),)),
    )


class RegistryOnlyNodeClient:
    """A remote node that can answer /v1/registry and nothing else --
    exactly the surface the fleet read actually uses."""

    def __init__(self, records: list[dict[str, Any]] | None = None, *, broken: bool = False,
                 error: str | None = None) -> None:
        self.records = records or []
        self.broken = broken
        self.error = error
        self.calls: list[bool] = []

    def registry_list(self, *, recoverable_only: bool = False) -> dict[str, Any]:
        self.calls.append(recoverable_only)
        if self.broken:
            raise NodeClientError("simulated transport failure")
        if self.error:
            return {"error": self.error}
        records = [r for r in self.records if not recoverable_only or r.get("recoverable")]
        return {"records": records}


def _metrics() -> NodeMetrics:
    return NodeMetrics(cpu_percent=5.0, load1=0.1, load5=0.1, load15=0.1, cpu_count=4,
                       ram_total_bytes=8_000_000_000, ram_used_bytes=1_000_000_000, ram_percent=12.5,
                       swap_total_bytes=0, swap_used_bytes=0, swap_percent=0.0,
                       disk_total_bytes=100_000_000_000, disk_used_bytes=1_000_000_000,
                       disk_free_bytes=99_000_000_000, disk_percent=1.0)


@pytest.fixture
def rig(tmp_path):
    registry_store = SessionRegistryStore(tmp_path / "session_registry.db")
    service = TerminalService(_config(tmp_path),
                              grants=SessionGrantStore(tmp_path / "grants.db"),
                              session_registry=registry_store)
    controller = ControllerService(NodeRegistry(tmp_path / "nodes.db"),
                                   local_client=LocalNodeClient(service),
                                   local_workspace_root=str(tmp_path))
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=(),
                                       agent_version=None)
    return {"controller": controller, "service": service, "registry": registry_store}


def _attach_remote(controller, node_id, client, *, heartbeat=True, display_name=None):
    controller.registry.register(node_id, display_name=display_name or node_id,
                                 hostname=f"{node_id}-host", endpoint=f"http://{node_id}")
    controller._clients[node_id] = client
    if heartbeat:
        controller.registry.heartbeat(node_id, metrics=_metrics(), tmux_session_count=1,
                                      agent_counts={}, agent_types=("shell",), agent_version=None,
                                      labels=())
    return client


def _mine(records, prefix="fleet-"):
    """Only this test's own sessions.

    terminal_registry_list() reconciles against the REAL tmux server on
    the host before reading, so whatever sessions genuinely exist here
    (and leftovers from other suites) land in even an isolated registry
    db. Asserting on an exact full listing would make these tests depend
    on the machine they run on; filtering to our own prefix is the honest
    fix, not a weaker assertion."""
    return [r for r in records if str(r.get("session_name", "")).startswith(prefix)]


def _remote_record(name, *, status="ACTIVE", recoverable=False, **extra):
    record = {"node_id": "local", "session_name": name, "status": status, "node_name": None,
              "last_seen_at": "2026-09-14T10:00:00", "cwd": None, "recoverable": recoverable,
              "read_granted": False, "input_granted": False, "key": f"local/{name}"}
    record.update(extra)
    return record


# -- local-only ----------------------------------------------------------

def test_local_only_controller_reads_its_own_registry(rig):
    rig["registry"].upsert_seen("local", "fleet-alpha", backend_type="tmux", cwd="/tmp/a")
    result = rig["controller"].registry_list_fleet()
    mine = _mine(result["records"])
    assert [r["session_name"] for r in mine] == ["fleet-alpha"]
    assert mine[0]["source"] == "local"
    assert result["unavailable_nodes"] == []
    assert result["scope"] == "fleet"


def test_local_node_is_read_exactly_once(rig):
    """The controller registers its OWN node in `_clients`, so a naive
    fan-out would read the local registry twice -- once directly and once
    routed through its own client -- and report every local session
    twice.

    The collector prevents that rather than producing duplicates and
    cleaning them up afterwards: the local node is skipped in the client
    loop, so `deduped` is legitimately 0 here. (The merge layer's own
    dedupe still exists for genuine cross-source duplicates and is unit-
    tested directly in tests/test_fleet_registry.py.)"""
    rig["registry"].upsert_seen("local", "fleet-alpha", backend_type="tmux")
    result = rig["controller"].registry_list_fleet()
    mine = _mine(result["records"])
    assert len(mine) == 1, "the same row must not appear twice"
    assert mine[0]["source"] == "local"
    local_summaries = [n for n in result["nodes"] if n["node_id"] == rig["controller"].local_node_id]
    assert len(local_summaries) == 1, "the local node must appear as exactly one source"


# -- remote-only / merged ------------------------------------------------

def test_remote_records_appear_with_authoritative_node_identity(rig):
    _attach_remote(rig["controller"], "dell-linux",
                   RegistryOnlyNodeClient([_remote_record("fleet-gamma")]),
                   display_name="dell-linux (Linux)")
    result = rig["controller"].registry_list_fleet()
    gamma = next(r for r in result["records"] if r["session_name"] == "fleet-gamma")
    assert gamma["node_id"] == "dell-linux"
    assert gamma["node_name"] == "dell-linux (Linux)"
    assert gamma["source_node_id"] == "local", "the raw row value is preserved"
    assert gamma["source"] == "fleet"


def test_local_records_come_first_then_remote(rig):
    rig["registry"].upsert_seen("local", "fleet-alpha", backend_type="tmux")
    _attach_remote(rig["controller"], "dell-linux", RegistryOnlyNodeClient([_remote_record("fleet-gamma")]))
    names = [r["session_name"] for r in rig["controller"].registry_list_fleet()["records"]]
    assert names.index("fleet-alpha") < names.index("fleet-gamma")


def test_same_session_name_on_two_nodes_both_survive(rig):
    rig["registry"].upsert_seen("local", "fleet-work", backend_type="tmux")
    _attach_remote(rig["controller"], "dell-linux", RegistryOnlyNodeClient([_remote_record("fleet-work")]))
    result = rig["controller"].registry_list_fleet()
    work = [r for r in result["records"] if r["session_name"] == "fleet-work"]
    assert len(work) == 2
    assert {r["node_id"] for r in work} == {"local", "dell-linux"}


# -- offline / unreachable ----------------------------------------------

def test_unreachable_node_is_reported_not_silently_empty(rig):
    rig["registry"].upsert_seen("local", "fleet-alpha", backend_type="tmux")
    _attach_remote(rig["controller"], "broken", RegistryOnlyNodeClient(broken=True))
    result = rig["controller"].registry_list_fleet()
    assert [r["session_name"] for r in _mine(result["records"])] == ["fleet-alpha"]
    assert [n["node_id"] for n in result["unavailable_nodes"]] == ["broken"]
    assert "simulated transport failure" in result["unavailable_nodes"][0]["error"]


def test_node_returning_an_error_payload_is_treated_as_unavailable(rig):
    _attach_remote(rig["controller"], "sad", RegistryOnlyNodeClient(error="NODE_UNREACHABLE"))
    result = rig["controller"].registry_list_fleet()
    assert result["unavailable_nodes"][0]["error"] == "NODE_UNREACHABLE"
    assert result["counts"]["nodes_reporting"] >= 1  # the local node still reported


def test_offline_node_records_are_marked_stale_and_not_active(rig):
    """No heartbeat => derived status OFFLINE, but the client still
    answers: its rows are real, and must not read as ACTIVE."""
    _attach_remote(rig["controller"], "sleepy",
                   RegistryOnlyNodeClient([_remote_record("fleet-sleepy", status="ACTIVE")]),
                   heartbeat=False)
    result = rig["controller"].registry_list_fleet()
    sleepy = next(r for r in result["records"] if r["session_name"] == "fleet-sleepy")
    assert sleepy["status"] == "ACTIVE"
    assert sleepy["effective_status"] == "UNKNOWN"
    assert sleepy["stale"] is True and sleepy["node_online"] is False


def test_include_offline_false_skips_but_still_reports_the_node(rig):
    _attach_remote(rig["controller"], "sleepy",
                   RegistryOnlyNodeClient([_remote_record("fleet-sleepy")]), heartbeat=False)
    result = rig["controller"].registry_list_fleet(include_offline=False)
    assert all(r["session_name"] != "fleet-sleepy" for r in result["records"])
    assert result["unavailable_nodes"][0]["error"] == "SKIPPED_OFFLINE_NODE"


def test_node_ids_filter_restricts_the_fan_out(rig):
    client = _attach_remote(rig["controller"], "dell-linux",
                            RegistryOnlyNodeClient([_remote_record("fleet-gamma")]))
    other = _attach_remote(rig["controller"], "other", RegistryOnlyNodeClient([_remote_record("fleet-x")]))
    result = rig["controller"].registry_list_fleet(node_ids=("dell-linux",))
    assert [r["session_name"] for r in result["records"]] == ["fleet-gamma"]
    assert client.calls and other.calls == [], "a filtered-out node is never even asked"


# -- recoverable_only / permissions --------------------------------------

def test_recoverable_only_is_forwarded_to_every_node(rig):
    client = _attach_remote(rig["controller"], "dell-linux", RegistryOnlyNodeClient([
        _remote_record("fleet-live", status="ACTIVE", recoverable=False),
        _remote_record("fleet-gone", status="MISSING", recoverable=True)]))
    result = rig["controller"].registry_list_fleet(recoverable_only=True)
    assert client.calls == [True]
    assert [r["session_name"] for r in result["records"] if r["source"] == "fleet"] == ["fleet-gone"]


def test_remote_grant_flags_are_never_upgraded(rig):
    _attach_remote(rig["controller"], "dell-linux", RegistryOnlyNodeClient([
        _remote_record("fleet-denied", read_granted=False, input_granted=False)]))
    record = next(r for r in rig["controller"].registry_list_fleet()["records"]
                  if r["session_name"] == "fleet-denied")
    assert record["read_granted"] is False and record["input_granted"] is False


# -- search --------------------------------------------------------------

def test_fleet_search_finds_a_remote_session_by_project_path(rig):
    _attach_remote(rig["controller"], "dell-linux", RegistryOnlyNodeClient([
        _remote_record("quan_ly_ban_hang", cwd="/home/k/offline-pos")]))
    result = rig["controller"].registry_search_fleet("ban hang")
    assert [r["session_name"] for r in result["records"]] == ["quan_ly_ban_hang"]
    assert result["records"][0]["node_id"] == "dell-linux"
    assert result["counts"]["matched"] == 1


def test_fleet_search_finds_local_and_remote_together(rig):
    rig["registry"].upsert_seen("local", "fleet-pos-local", backend_type="tmux", cwd="/home/k/offline-pos")
    _attach_remote(rig["controller"], "dell-linux", RegistryOnlyNodeClient([
        _remote_record("fleet-pos-remote", cwd="/home/k/offline-pos")]))
    names = [r["session_name"] for r in rig["controller"].registry_search_fleet("offline-pos")["records"]]
    assert set(names) == {"fleet-pos-local", "fleet-pos-remote"}


def test_empty_search_returns_nothing_and_asks_no_node(rig):
    client = _attach_remote(rig["controller"], "dell-linux", RegistryOnlyNodeClient([_remote_record("x")]))
    result = rig["controller"].registry_search_fleet("   ")
    assert result["records"] == [] and result["total"] == 0
    assert client.calls == [], "an empty query must not trigger a fan-out"


def test_fleet_search_reports_unavailable_nodes_too(rig):
    """A search that could not see a node must say so -- otherwise "not
    found" is indistinguishable from "not looked at"."""
    _attach_remote(rig["controller"], "broken", RegistryOnlyNodeClient(broken=True))
    result = rig["controller"].registry_search_fleet("anything")
    assert [n["node_id"] for n in result["unavailable_nodes"]] == ["broken"]


# -- pagination ----------------------------------------------------------

def test_fleet_list_paginates(rig):
    _attach_remote(rig["controller"], "dell-linux", RegistryOnlyNodeClient(
        [_remote_record(f"fleet-s{i}") for i in range(12)]))
    first = rig["controller"].registry_list_fleet(limit=5)
    assert len(first["records"]) == 5 and first["has_more"] is True
    second = rig["controller"].registry_list_fleet(limit=5, cursor=first["next_cursor"])
    assert first["records"][0]["session_name"] != second["records"][0]["session_name"]
    assert first["total"] == second["total"]


# -- backward compatibility ----------------------------------------------

def test_local_registry_list_is_unchanged_by_this_feature(rig):
    """TerminalService.terminal_registry_list -- what /v1/registry and
    the dashboard call -- must stay local-only and keep its exact shape.
    Making it fleet-aware would make every node fan out to every other
    node on every controller poll."""
    rig["registry"].upsert_seen("local", "fleet-alpha", backend_type="tmux")
    _attach_remote(rig["controller"], "dell-linux", RegistryOnlyNodeClient([_remote_record("fleet-gamma")]))
    local = rig["service"].terminal_registry_list()
    assert set(local.keys()) == {"records"}, "no new top-level key on the local shape"
    mine = _mine(local["records"])
    assert [r["session_name"] for r in mine] == ["fleet-alpha"]
    assert all(r["session_name"] != "fleet-gamma" for r in local["records"]), "still local-only"
    assert mine[0]["node_id"] == "local", "raw node_id is untouched locally"


def test_local_registry_search_is_unchanged_by_this_feature(rig):
    rig["registry"].upsert_seen("local", "fleet-alpha", backend_type="tmux", cwd="/home/k/offline-pos")
    _attach_remote(rig["controller"], "dell-linux", RegistryOnlyNodeClient([
        _remote_record("fleet-gamma", cwd="/home/k/offline-pos")]))
    local = rig["service"].terminal_registry_search("offline-pos")
    assert set(local.keys()) == {"records"}
    assert [r["session_name"] for r in _mine(local["records"])] == ["fleet-alpha"]
    assert all(r["session_name"] != "fleet-gamma" for r in local["records"]), "still local-only"


def test_every_pre_existing_record_field_survives_the_fleet_read(rig):
    rig["registry"].upsert_seen("local", "fleet-alpha", backend_type="tmux", cwd="/tmp/a")
    local_record = _mine(rig["service"].terminal_registry_list()["records"])[0]
    fleet_record = next(r for r in rig["controller"].registry_list_fleet()["records"]
                        if r["session_name"] == "fleet-alpha")
    for key, value in local_record.items():
        if key in ("node_id", "node_name"):
            continue
        assert fleet_record[key] == value, key
    assert set(local_record) - set(fleet_record) == set(), "no field was dropped"


# -- MCP tool surface ----------------------------------------------------
# The tools are where the default actually changes, so the local/fleet
# contract is pinned here rather than only at the controller.

@pytest.fixture
def mcp_rig(tmp_path):
    import json

    from terminal_mcp.mcp_app import build_mcp

    registry_store = SessionRegistryStore(tmp_path / "session_registry.db")
    service = TerminalService(_config(tmp_path),
                              grants=SessionGrantStore(tmp_path / "grants.db"),
                              session_registry=registry_store)
    controller = ControllerService(NodeRegistry(tmp_path / "nodes.db"),
                                   local_client=LocalNodeClient(service),
                                   local_workspace_root=str(tmp_path))
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=(),
                                       agent_version=None)
    server = build_mcp(service, controller=controller)

    async def call(name, **kwargs):
        result = await server.call_tool(name, kwargs)
        if result.structured_content is not None:
            return result.structured_content
        return json.loads(result.content[0].text)

    return {"call": call, "controller": controller, "registry": registry_store}


@pytest.mark.anyio
async def test_tool_scope_local_returns_the_exact_pre_fleet_shape(mcp_rig):
    """The compatibility escape hatch: scope="local" must return the
    old `{"records": [...]}` and nothing else."""
    mcp_rig["registry"].upsert_seen("local", "fleet-alpha", backend_type="tmux")
    _attach_remote(mcp_rig["controller"], "dell-linux",
                   RegistryOnlyNodeClient([_remote_record("fleet-gamma")]))
    result = await mcp_rig["call"]("terminal_registry_list", scope="local")
    assert set(result.keys()) == {"records"}
    assert all(r["session_name"] != "fleet-gamma" for r in result["records"])


@pytest.mark.anyio
async def test_tool_defaults_to_fleet_and_includes_remote_sessions(mcp_rig):
    mcp_rig["registry"].upsert_seen("local", "fleet-alpha", backend_type="tmux")
    _attach_remote(mcp_rig["controller"], "dell-linux",
                   RegistryOnlyNodeClient([_remote_record("fleet-gamma")]))
    result = await mcp_rig["call"]("terminal_registry_list")
    names = [r["session_name"] for r in _mine(result["records"])]
    assert "fleet-alpha" in names and "fleet-gamma" in names
    assert result["scope"] == "fleet"
    assert "nodes" in result and "unavailable_nodes" in result


@pytest.mark.anyio
async def test_tool_fleet_records_keep_every_local_field(mcp_rig):
    mcp_rig["registry"].upsert_seen("local", "fleet-alpha", backend_type="tmux", cwd="/tmp/a")
    local = await mcp_rig["call"]("terminal_registry_list", scope="local")
    fleet = await mcp_rig["call"]("terminal_registry_list")
    local_record = _mine(local["records"])[0]
    fleet_record = next(r for r in fleet["records"] if r["session_name"] == "fleet-alpha")
    assert set(local_record) - set(fleet_record) == set()


@pytest.mark.anyio
async def test_search_tool_scope_local_vs_fleet(mcp_rig):
    mcp_rig["registry"].upsert_seen("local", "fleet-pos", backend_type="tmux", cwd="/home/k/offline-pos")
    _attach_remote(mcp_rig["controller"], "dell-linux", RegistryOnlyNodeClient([
        _remote_record("fleet-pos-remote", cwd="/home/k/offline-pos")]))
    local = await mcp_rig["call"]("terminal_registry_search", query="offline-pos", scope="local")
    assert set(local.keys()) == {"records"}
    assert [r["session_name"] for r in _mine(local["records"])] == ["fleet-pos"]

    fleet = await mcp_rig["call"]("terminal_registry_search", query="offline-pos")
    assert {r["session_name"] for r in fleet["records"]} == {"fleet-pos", "fleet-pos-remote"}


@pytest.mark.anyio
async def test_search_tool_reports_a_node_it_could_not_reach(mcp_rig):
    _attach_remote(mcp_rig["controller"], "broken", RegistryOnlyNodeClient(broken=True))
    result = await mcp_rig["call"]("terminal_registry_search", query="anything")
    assert [n["node_id"] for n in result["unavailable_nodes"]] == ["broken"]
