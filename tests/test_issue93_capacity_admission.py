from types import SimpleNamespace

from terminal_mcp.controller import ControllerService
from terminal_mcp.node_models import CAPACITY_HEALTHY, NODE_ONLINE


def _node(node_id, *, disk=100 * 1024**3, sessions=0):
    return SimpleNamespace(
        id=node_id, display_name=node_id, status=NODE_ONLINE, draining=False,
        last_heartbeat_at="2026-11-28T00:00:00+00:00", platform="linux",
        agent_types=("shell",), capabilities=("git",), capacity_status=CAPACITY_HEALTHY,
        overload_reasons=(), disk_free_bytes=disk, tmux_session_count=sessions,
        max_sessions=10, agent_version="0.14.0", labels=(),
        ram_percent=30.0, ram_percent_smoothed=30.0, cpu_percent=10.0,
        cpu_percent_smoothed=10.0,
    )


class Client:
    def __init__(self):
        self.calls = 0

    def create_session(self, *args, **kwargs):
        self.calls += 1
        return {"session": args[0], "state": "READY", "session_id": "$1",
                "created_epoch": "2026-11-28T00:00:00+00:00"}


def _controller(nodes, clients):
    ctl = ControllerService.__new__(ControllerService)
    ctl.local_node_id = "local"
    ctl._clients = clients
    ctl.resolve_session = lambda name: {"error": "SESSION_NOT_FOUND", "session": name}
    ctl.list_nodes = lambda: list(nodes)
    ctl.local_client = SimpleNamespace(terminal=None)
    ctl.registry = SimpleNamespace(get=lambda node_id: next((n for n in nodes if n.id == node_id), None))
    ctl.ownership = SimpleNamespace(record_created=lambda *a, **k: SimpleNamespace(public=lambda: {}))
    ctl._session_location_cache = {}
    return ctl


def test_controller_denies_unknown_capacity_before_remote_create():
    client = Client()
    ctl = _controller([_node("unknown", disk=None)], {"unknown": client})
    result = ctl.terminal_create_session("capacity-unknown", node="auto")
    assert result["error"] == "NO_ELIGIBLE_NODE"
    assert client.calls == 0
    assert "disk_free_bytes unknown" in result["excluded"][0][1]


def test_controller_falls_back_to_verified_capacity_node():
    unknown = _node("unknown", sessions=None)
    eligible = _node("eligible", sessions=1)
    client = Client()
    ctl = _controller([unknown, eligible], {"eligible": client})
    result = ctl.terminal_create_session("capacity-fallback", node="auto")
    assert result["node_id"] == "eligible"
    assert client.calls == 1
