from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace

from terminal_mcp.controller import ControllerService
from terminal_mcp.node_registry import NodeRegistry


def make_controller(path, clients):
    ctl = ControllerService(NodeRegistry(path / "nodes.sqlite"))
    nodes = {key: SimpleNamespace(id=key, display_name=key, status="online") for key in clients}
    ctl._clients.update(clients)
    # Isolate session reads from health probing. Qualified writes still go
    # through the real controller resolve/route and fresh node-status call.
    ctl.list_nodes = lambda **kwargs: list(nodes.values())
    ctl.node_status = nodes.get
    return ctl


def test_overlapping_expired_listings_do_not_multiply_node_reads(tmp_path):
    lock = Lock()
    entered = Event()
    release = Event()
    calls = []

    class ControlledNode:
        def __init__(self, name):
            self.name = name

        def list_sessions(self, *, timeout_seconds=None):
            with lock:
                calls.append(self.name)
                if len(calls) >= 2:
                    entered.set()
            assert release.wait(3)
            return {"sessions": [{"name": self.name, "effective_read": True}]}

    ctl = make_controller(tmp_path, {name: ControlledNode(name) for name in ("a", "b")})
    with ThreadPoolExecutor(max_workers=2) as callers:
        first = callers.submit(ctl.terminal_list_sessions, budget_seconds=0.10)
        try:
            assert entered.wait(2)
            second = callers.submit(ctl.terminal_list_sessions, budget_seconds=0.10)
            for pending in (first, second):
                result = pending.result(timeout=2)
                assert len(result["unreachable_nodes"]) == 2
            with lock:
                assert sorted(calls) == ["a", "b"]
        finally:
            release.set()


def test_single_remote_budget_and_new_read_preserve_node_acl(tmp_path):
    entered = Event()
    release = Event()
    calls = 0

    class Node:
        def list_sessions(self, *, timeout_seconds=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                assert release.wait(3)
                return {"sessions": [{"name": "old", "effective_read": True}]}
            return {"sessions": [{"name": "fresh", "effective_read": False}]}

        def send_text(self, name, text, press_enter, dry_run, **kwargs):
            return {"error": "INPUT_NOT_ALLOWED", "session": name}

    ctl = make_controller(tmp_path, {"a": Node()})
    with ThreadPoolExecutor(max_workers=1) as callers:
        first = callers.submit(ctl.terminal_list_sessions, budget_seconds=0.10)
        try:
            assert entered.wait(2)
            expired = first.result(timeout=1)
            assert expired["sessions"] == []
            assert expired["unreachable_nodes"][0]["status"] == "timeout"
            assert ctl.terminal_send_text("a/restricted", "inert")["error"] == "INPUT_NOT_ALLOWED"
        finally:
            release.set()
    rows = ctl.terminal_list_sessions(budget_seconds=1)["sessions"]
    assert calls == 2
    assert len(rows) == 1 and rows[0]["name"] == "fresh" and rows[0]["allowed"] is False


def test_controller_budget_does_not_enqueue_unadmitted_rpc(tmp_path, monkeypatch):
    from terminal_mcp import controller

    monkeypatch.setattr(controller, "_MAX_FANOUT_WORKERS", 2)
    lock = Lock()
    release = Event()
    drained = Event()
    calls = []
    completed = 0

    class Node:
        def __init__(self, name):
            self.name = name

        def list_sessions(self, *, timeout_seconds=None):
            nonlocal completed
            with lock:
                calls.append(self.name)
            try:
                assert release.wait(3)
                return {"sessions": []}
            finally:
                with lock:
                    completed += 1
                    if completed == 2:
                        drained.set()

    ctl = make_controller(tmp_path, {name: Node(name) for name in ("a", "b", "c")})
    try:
        result = ctl.terminal_list_sessions(budget_seconds=0.10)
        assert len(result["unreachable_nodes"]) == 3
        assert any("admission deadline" in row["detail"] for row in result["unreachable_nodes"])
        with lock:
            assert sorted(calls) == ["a", "b"]
    finally:
        release.set()
    assert drained.wait(2)
    with lock:
        assert sorted(calls) == ["a", "b"]
