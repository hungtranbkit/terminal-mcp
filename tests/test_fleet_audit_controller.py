"""controller.terminal_audit_fleet -- the scatter-gather half.

Uses a REAL TerminalService + REAL AuditStore for the local node (so the
local half of a fleet read goes through the genuine store, query and limit
validation, not a mock) plus a FakeNodeClient for "remote" nodes, matching
tests/test_controller.py's own posture.

ISOLATION: TerminalService defaults every store to this host's REAL
~/.local/state/terminal-mcp/*.db. `audit` MUST be passed explicitly here
-- without it these tests would both read this machine's real production
audit rows (making every assertion non-deterministic) and append their own
rows to it. The same trap tests/test_controller.py documents hitting with
grants.db and session_registry.db.
"""
from __future__ import annotations


from terminal_mcp.audit import AuditStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.controller import ControllerService
from terminal_mcp.core import TerminalService
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.host_metrics import NodeMetrics
from terminal_mcp.node_client import LocalNodeClient, NodeClientError
from terminal_mcp.node_registry import NodeRegistry
from terminal_mcp.session_registry import SessionRegistryStore

_METRICS = NodeMetrics(cpu_percent=5.0, load1=0.1, load5=0.1, load15=0.1, cpu_count=4,
                       ram_total_bytes=8_000_000_000, ram_used_bytes=1_000_000_000, ram_percent=12.5,
                       swap_total_bytes=0, swap_used_bytes=0, swap_percent=0.0,
                       disk_total_bytes=100_000_000_000, disk_used_bytes=1_000_000_000,
                       disk_free_bytes=99_000_000_000, disk_percent=1.0)


class FakeAuditNodeClient:
    """A 'remote' node that serves canned audit rows. Honours `limit` and
    `at_or_before` the way a real node's AuditStore.list does, so paging
    behaviour is exercised rather than assumed."""

    def __init__(self, rows: list[dict] | None = None, *, broken: bool = False, error: str | None = None):
        self.rows = rows or []
        self.broken = broken
        self.error = error
        self.calls: list[dict] = []

    def audit_list(self, *, limit=50, binding=None, session=None, at_or_before=None):
        self.calls.append({"limit": limit, "binding": binding, "session": session,
                           "at_or_before": at_or_before})
        if self.broken:
            raise NodeClientError("connection refused")
        if self.error is not None:
            return {"error": self.error}
        rows = [row for row in self.rows if at_or_before is None or row["timestamp"] <= at_or_before]
        rows.sort(key=lambda row: (row["timestamp"], row["id"]), reverse=True)
        return {"events": rows[:limit]}


def _config(tmp_path) -> AppConfig:
    return AppConfig(permissions=PermissionsConfig(True, True), allowed_session_patterns=("ctrl-*",),
                     max_capture_lines=200, default_tail_lines=50,
                     input_policy=InputPolicyConfig(allowed_session_patterns=("ctrl-*",)))


def _controller(tmp_path):
    service = TerminalService(_config(tmp_path),
                              audit=AuditStore(tmp_path / "audit.db"),
                              grants=SessionGrantStore(tmp_path / "grants.db"),
                              session_registry=SessionRegistryStore(tmp_path / "session_registry.db"))
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(service),
                                   local_workspace_root=str(tmp_path))
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={},
                                       agent_types=("shell",), agent_version=None)
    return controller, service


def _add_remote(controller, node_id, client, *, online=True):
    controller.registry.register(node_id, display_name=node_id, hostname=f"{node_id}-host",
                                 endpoint=f"http://{node_id}")
    controller._clients[node_id] = client
    if online:
        controller.registry.heartbeat(node_id, metrics=_METRICS, tmux_session_count=0, agent_counts={},
                                      agent_types=("shell",), agent_version=None, labels=())


def _remote_row(row_id, timestamp, **overrides):
    # A remote node reports its OWN rows as node_id "local" -- its own
    # point of view. The aggregation must overwrite that.
    row = {"id": row_id, "timestamp": timestamp, "action": "terminal_send_text", "session": "remote-lane",
           "result": "SENT", "text_sha256": "deadbeef", "preview": "remote work", "text_length": 11,
           "source_transport": "mcp", "server_version": "1.0.0", "node_id": "local"}
    row.update(overrides)
    return row


def _record_local(service, count, *, session="ctrl-local"):
    for index in range(count):
        service.audit.record(action="terminal_send_text", session=session, result="SENT",
                             text=f"local task {index}")


def _uids(page):
    return [row["audit_uid"] for row in page["events"]]


# -- local-only deployment (backward compatibility) -----------------------

def test_a_single_node_deployment_gets_its_own_rows_with_provenance(tmp_path):
    """No special-casing: the local node is registered like any other, so
    a deployment with no remote nodes takes the same path."""
    controller, service = _controller(tmp_path)
    _record_local(service, 3)

    page = controller.terminal_audit_fleet(limit=10)

    assert page["complete"] is True
    assert len(page["events"]) == 3
    assert {row["node_id"] for row in page["events"]} == {"local"}
    assert _uids(page) == ["local:3", "local:2", "local:1"]
    assert [node["node_id"] for node in page["nodes"]] == ["local"]
    assert page["nodes"][0]["ok"] is True and page["nodes"][0]["rows"] == 3
    assert page["node_errors"] == {}


def test_an_empty_local_audit_is_an_empty_complete_page(tmp_path):
    controller, _service = _controller(tmp_path)
    page = controller.terminal_audit_fleet(limit=10)
    assert page["events"] == []
    assert page["complete"] is True
    assert page["next_cursor"] is None


def test_the_local_rows_match_what_the_local_tool_returns(tmp_path):
    """The aggregation must not become a second, drifting copy of the
    local read -- same rows, same order, plus provenance."""
    controller, service = _controller(tmp_path)
    _record_local(service, 4)

    local = service.terminal_list_input_audit(10)["events"]
    fleet = controller.terminal_audit_fleet(limit=10)["events"]

    assert [row["id"] for row in local] == [row["node_row_id"] for row in fleet]
    assert [row["preview"] for row in local] == [row["preview"] for row in fleet]


# -- two or more nodes ----------------------------------------------------

def test_two_nodes_merge_into_one_ordered_page_with_real_node_ids(tmp_path):
    controller, service = _controller(tmp_path)
    _record_local(service, 1)  # local:1, newest wall-clock (recorded now)
    _add_remote(controller, "dell-5530", FakeAuditNodeClient([
        _remote_row(7, "2020-01-01T00:00:02"), _remote_row(6, "2020-01-01T00:00:01"),
    ]))

    page = controller.terminal_audit_fleet(limit=10)

    assert page["complete"] is True
    assert _uids(page) == ["local:1", "dell-5530:7", "dell-5530:6"]
    # The remote node reported node_id "local" for its own rows; that must
    # have been overwritten with the controller's id for it.
    assert [row["node_id"] for row in page["events"]] == ["local", "dell-5530", "dell-5530"]


def test_the_same_row_id_on_two_nodes_stays_two_rows(tmp_path):
    """The backlog item's core problem: input_audit.id is per-node, so id
    41 exists on every node and means something different on each."""
    controller, _service = _controller(tmp_path)
    _add_remote(controller, "m910", FakeAuditNodeClient([_remote_row(41, "2020-01-01T00:00:02")]))
    _add_remote(controller, "dell-5530", FakeAuditNodeClient([_remote_row(41, "2020-01-01T00:00:01")]))

    page = controller.terminal_audit_fleet(limit=10)

    assert _uids(page) == ["m910:41", "dell-5530:41"]
    assert len(page["events"]) == 2


def test_each_node_is_asked_for_the_full_limit_not_a_share_of_it(tmp_path):
    """The merge cannot know in advance which node holds the newest rows,
    so under-asking would silently truncate a busy node."""
    controller, _service = _controller(tmp_path)
    client = FakeAuditNodeClient([_remote_row(1, "2020-01-01T00:00:01")])
    _add_remote(controller, "m910", client)

    controller.terminal_audit_fleet(limit=25)

    assert client.calls[0]["limit"] == 25


# -- offline / unreachable nodes -----------------------------------------

def test_an_offline_node_yields_a_partial_page_and_is_named(tmp_path):
    controller, service = _controller(tmp_path)
    _record_local(service, 2)
    _add_remote(controller, "sleepy", FakeAuditNodeClient([_remote_row(9, "2020-01-01T00:00:09")]),
                online=False)

    page = controller.terminal_audit_fleet(limit=10)

    # The reachable node's rows are still returned in full.
    assert _uids(page) == ["local:2", "local:1"]
    # ...but the answer is explicitly flagged as missing rows, and says
    # WHICH node is missing.
    assert page["complete"] is False
    assert page["partial"] is True
    assert "sleepy" in page["node_errors"]
    sleepy = next(node for node in page["nodes"] if node["node_id"] == "sleepy")
    assert sleepy["ok"] is False and sleepy["rows"] == 0


def test_an_unreachable_node_never_fails_the_whole_read(tmp_path):
    controller, service = _controller(tmp_path)
    _record_local(service, 1)
    _add_remote(controller, "broken", FakeAuditNodeClient(broken=True))

    page = controller.terminal_audit_fleet(limit=10)

    assert _uids(page) == ["local:1"]
    assert page["complete"] is False
    assert "connection refused" in page["node_errors"]["broken"]


def test_a_node_that_answers_with_an_error_payload_is_treated_as_failed(tmp_path):
    controller, service = _controller(tmp_path)
    _record_local(service, 1)
    _add_remote(controller, "grumpy", FakeAuditNodeClient(error="INVALID_LIMIT"))

    page = controller.terminal_audit_fleet(limit=10)

    assert page["complete"] is False
    assert page["node_errors"]["grumpy"] == "INVALID_LIMIT"
    assert _uids(page) == ["local:1"]


def test_a_registered_node_with_no_client_is_reported_not_skipped_silently(tmp_path):
    controller, _service = _controller(tmp_path)
    controller.registry.register("ghost", display_name="ghost", hostname="ghost", endpoint="http://ghost")
    controller.registry.heartbeat("ghost", metrics=_METRICS, tmux_session_count=0, agent_counts={},
                                  agent_types=("shell",), agent_version=None, labels=())

    page = controller.terminal_audit_fleet(limit=10)

    assert page["complete"] is False
    assert page["node_errors"]["ghost"] == "no_client"


def test_every_node_reports_a_fetched_at_so_freshness_is_visible(tmp_path):
    controller, _service = _controller(tmp_path)
    _add_remote(controller, "m910", FakeAuditNodeClient([_remote_row(1, "2020-01-01T00:00:01")]))
    _add_remote(controller, "down", FakeAuditNodeClient(), online=False)

    page = controller.terminal_audit_fleet(limit=10)

    assert all(node["fetched_at"] for node in page["nodes"])


# -- pagination across nodes ---------------------------------------------

def test_paging_across_two_nodes_returns_every_row_exactly_once(tmp_path):
    controller, _service = _controller(tmp_path)
    _add_remote(controller, "aaa", FakeAuditNodeClient(
        [_remote_row(i, f"2020-01-01T00:00:{i:02d}") for i in range(1, 6)]))
    _add_remote(controller, "bbb", FakeAuditNodeClient(
        [_remote_row(i, f"2020-01-01T00:00:{i:02d}") for i in range(1, 6)]))

    seen, cursor, guard = [], None, 0
    while guard < 20:
        page = controller.terminal_audit_fleet(limit=3, cursor=cursor)
        seen.extend(_uids(page))
        cursor = page["next_cursor"]
        guard += 1
        if cursor is None:
            break

    assert len(seen) == 10
    assert len(set(seen)) == 10
    assert seen[0] == "aaa:5"  # newest timestamp, lowest node_id wins the tie


def test_a_cursor_is_passed_down_as_a_timestamp_filter(tmp_path):
    """Each node prunes its own side of the scan rather than shipping
    everything back for the controller to discard."""
    controller, _service = _controller(tmp_path)
    client = FakeAuditNodeClient([_remote_row(i, f"2020-01-01T00:00:{i:02d}") for i in range(1, 6)])
    _add_remote(controller, "aaa", client)

    first = controller.terminal_audit_fleet(limit=2)
    controller.terminal_audit_fleet(limit=2, cursor=first["next_cursor"])

    assert client.calls[0]["at_or_before"] is None
    assert client.calls[1]["at_or_before"] == "2020-01-01T00:00:04"


# -- payload / auth posture ----------------------------------------------

def test_no_raw_prompt_text_is_introduced_by_aggregation(tmp_path):
    """Audit rows never stored raw prompt text; aggregation must not add
    a field that does. The preview is already redacted and truncated by
    audit.sanitized_preview before it is ever written."""
    controller, service = _controller(tmp_path)
    service.audit.record(action="terminal_send_text", session="ctrl-local", result="SENT",
                         text="deploy with token=hunter2 please")

    page = controller.terminal_audit_fleet(limit=10)
    row = page["events"][0]

    assert "text" not in row
    assert "hunter2" not in row["preview"]
    assert row["text_sha256"]
    assert set(row) - set(service.terminal_list_input_audit(1)["events"][0]) == {
        "node_row_id", "audit_uid"}
    assert row["node_id"] == controller.local_node_id


def test_a_session_filter_is_forwarded_to_every_node(tmp_path):
    controller, service = _controller(tmp_path)
    _record_local(service, 1, session="ctrl-a")
    _record_local(service, 1, session="ctrl-b")
    client = FakeAuditNodeClient([_remote_row(1, "2020-01-01T00:00:01")])
    _add_remote(controller, "m910", client)

    page = controller.terminal_audit_fleet(limit=10, session="ctrl-a")

    assert client.calls[0]["session"] == "ctrl-a"
    assert [row["session"] for row in page["events"]] == ["ctrl-a", "remote-lane"]


# -- node-agent transport: auth is preserved, payload is unchanged --------
#
# The fleet read is only as safe as the route it fans out to. These use a
# TerminalService with an ISOLATED audit store for the same reason the
# rest of this file does -- tests/test_node_agent.py's own fixture leaves
# `audit` defaulted to this host's real production audit.db, which would
# make a route that READS audit rows both non-deterministic and a way to
# print real production rows into test output.

_AGENT_TOKEN = "fleet-audit-test-token"


def _agent(tmp_path):
    from starlette.testclient import TestClient
    from terminal_mcp.node_agent import build_node_agent

    service = TerminalService(_config(tmp_path),
                              audit=AuditStore(tmp_path / "agent-audit.db"),
                              grants=SessionGrantStore(tmp_path / "agent-grants.db"),
                              session_registry=SessionRegistryStore(tmp_path / "agent-registry.db"))
    app = build_node_agent(node_id="test-node", terminal=service, token=_AGENT_TOKEN,
                           workspace_root=str(tmp_path))
    client = TestClient(app)
    return client, service


def test_the_audit_route_refuses_an_unauthenticated_read(tmp_path):
    """Aggregation must not become a way around a node's own auth: the
    fleet read goes through the SAME bearer-token gate every other node
    route uses."""
    client, service = _agent(tmp_path)
    _record_local(service, 1)

    assert client.get("/v1/audit").status_code == 401
    assert client.get("/v1/audit", headers={"Authorization": "Bearer wrong-token"}).status_code == 401


def test_the_audit_route_serves_sanitized_rows_to_an_authenticated_caller(tmp_path):
    client, service = _agent(tmp_path)
    service.audit.record(action="terminal_send_text", session="ctrl-local", result="SENT",
                         text="rotate with token=hunter2")

    response = client.get("/v1/audit", params={"limit": 10},
                          headers={"Authorization": f"Bearer {_AGENT_TOKEN}"})

    assert response.status_code == 200
    rows = response.json()["events"]
    assert len(rows) == 1
    assert "text" not in rows[0]
    assert "hunter2" not in rows[0]["preview"]


def test_the_audit_route_honours_the_paging_filter(tmp_path):
    client, service = _agent(tmp_path)
    _record_local(service, 3)
    headers = {"Authorization": f"Bearer {_AGENT_TOKEN}"}

    everything = client.get("/v1/audit", params={"limit": 10}, headers=headers).json()["events"]
    boundary = everything[1]["timestamp"]
    filtered = client.get("/v1/audit", params={"limit": 10, "at_or_before": boundary},
                          headers=headers).json()["events"]

    assert len(everything) == 3
    assert all(row["timestamp"] <= boundary for row in filtered)
    assert len(filtered) < len(everything)
