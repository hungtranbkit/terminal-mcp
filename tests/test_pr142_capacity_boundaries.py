from types import SimpleNamespace

import pytest

from terminal_mcp.controller import ControllerService
from terminal_mcp.node_registry import NodeRegistry
from terminal_mcp.node_registry import valid_nonnegative_integer
from terminal_mcp.node_models import CAPACITY_HEALTHY, NODE_ONLINE
from terminal_mcp.scheduler import choose_node


def _node(node_id="candidate", *, disk=100 * 1024**3, sessions=0):
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
    def __init__(self, *, volume=True):
        self.calls = 0
        self.volume = volume

    def repo_evidence(self, cwd):
        if not self.volume:
            return {"cwd": cwd, "exists": False, "readable": False, "error": "PATH_NOT_FOUND"}
        return {"cwd": cwd, "exists": True, "readable": True, "device": 1,
                "statvfs_free_bytes": 10 * 1024**3, "statvfs_free_inodes": 1000}

    def resolve_cwd(self, cwd=None):
        return {"cwd": cwd or "/workspace/task", "resolved": True}

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


@pytest.mark.parametrize("value", [None, False, -1, 0.5, float("inf")])
def test_invalid_quantitative_capacity_is_not_eligible(value):
    node = _node(sessions=value)
    result = choose_node([node])
    assert result.node_id is None


def test_target_cwd_volume_is_probed_before_create_and_recorded():
    client = Client()
    ctl = _controller([_node()], {"candidate": client})
    result = ctl.terminal_create_session("volume-bound", node="auto", cwd="/workspace/task")
    assert result["node_id"] == "candidate"
    assert client.calls == 1
    assert result["capacity_snapshot"]["target_volume"]["device"] == 1


def test_explicit_target_fails_closed_when_volume_probe_is_not_allowed():
    client = Client(volume=False)
    ctl = _controller([_node()], {"candidate": client})
    result = ctl.terminal_create_session("volume-denied", node="candidate", cwd="/workspace/task")
    assert result["error"] == "TARGET_VOLUME_UNVERIFIED"
    assert client.calls == 0


def test_omitted_cwd_resolves_and_probes_default_before_create():
    client = Client()
    ctl = _controller([_node()], {"candidate": client})
    result = ctl.terminal_create_session("volume-default", node="candidate")
    assert result["node_id"] == "candidate"
    assert result["capacity_snapshot"]["target_volume"]["cwd"] == "/workspace/task"
    assert client.calls == 1


def test_omitted_cwd_unknown_default_fails_closed():
    client = Client(volume=False)
    ctl = _controller([_node()], {"candidate": client})
    result = ctl.terminal_create_session("volume-default-denied", node="candidate")
    assert result["error"] == "TARGET_VOLUME_UNVERIFIED"
    assert client.calls == 0


def test_naive_heartbeat_is_offline_not_an_exception(tmp_path):
    registry = NodeRegistry(tmp_path / "nodes.sqlite")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    assert registry._derive_status(now.replace(tzinfo=None).isoformat(), now) != NODE_ONLINE
    assert valid_nonnegative_integer(0)
    assert not valid_nonnegative_integer(False)
