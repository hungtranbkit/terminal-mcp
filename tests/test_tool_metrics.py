"""Dem loi goi tool: cai duy nhat no khong duoc phep lam la lam hong tool."""
from __future__ import annotations

import inspect
from typing import get_type_hints

import pytest

from terminal_mcp import tool_metrics


class FakeServer:
    def __init__(self) -> None:
        self.registered: dict[str, object] = {}

    def tool(self, *a, **kw):
        def deco(fn):
            self.registered[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def wired(tmp_path):
    server = FakeServer()
    store = tool_metrics.ToolMetricsStore(tmp_path / "tool_calls.db")
    tool_metrics.instrument(server, store)
    return server, store


def test_wrapper_preserves_the_signature_fastmcp_builds_its_schema_from(wired):
    # The whole hook is one wrap of server.tool instead of editing 280
    # registrations, so the wrapper MUST stay transparent to introspection --
    # FastMCP derives each tool's JSON schema from the signature and
    # annotations, and a lost annotation is a silently broken tool.
    server, _ = wired

    @server.tool()
    def terminal_demo(session: str, lines: int = 20) -> dict:
        """Mo ta goc."""
        return {"session": session, "lines": lines}

    fn = server.registered["terminal_demo"]
    assert fn.__name__ == "terminal_demo"
    assert fn.__doc__ == "Mo ta goc."
    sig = inspect.signature(fn)
    assert list(sig.parameters) == ["session", "lines"]
    assert sig.parameters["lines"].default == 20
    # Annotation phai GIAI DUOC ve kieu that -- day moi la thu FastMCP dung.
    # (So chuoi chu ky la sai: module test co `from __future__ import
    # annotations`, nen chung hien ra duoi dang chuoi.)
    hints = get_type_hints(fn)
    assert hints["session"] is str and hints["lines"] is int and hints["return"] is dict


def test_a_call_is_counted_with_its_session(wired):
    server, store = wired

    @server.tool()
    def terminal_tail(session: str, lines: int = 200) -> dict:
        return {"ok": True}

    server.registered["terminal_tail"](session="nova-claude-x", lines=5)
    rows = store.summary(3600)
    assert rows == [{"tool": "terminal_tail", "calls": 1, "errors": 0,
                     "avg_ms": rows[0]["avg_ms"]}]


def test_a_raising_tool_still_raises_and_is_counted_as_an_error(wired):
    server, store = wired

    @server.tool()
    def terminal_boom(session: str) -> dict:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        server.registered["terminal_boom"](session="s1")
    assert store.summary(3600)[0]["errors"] == 1


def test_a_broken_store_never_breaks_the_tool_call(wired, tmp_path):
    # Measurement is never worth a failed tool call: if the database is
    # unwritable the tool must still return its real result.
    server, store = wired

    def explode(*a, **kw):
        raise OSError("disk gone")

    store.record = explode

    @server.tool()
    def terminal_ok(session: str) -> str:
        return "ket qua that"

    assert server.registered["terminal_ok"](session="s1") == "ket qua that"


def test_session_is_read_from_target_when_there_is_no_session_argument(wired):
    server, store = wired

    @server.tool()
    def terminal_turn(action: str, target: str | None = None) -> dict:
        return {}

    server.registered["terminal_turn"](action="inspect", target="nova-claude-y")
    with store._connect() as con:
        assert con.execute("select session from tool_calls").fetchone()[0] == "nova-claude-y"


def test_prune_keeps_only_the_most_recent_rows(wired):
    _, store = wired
    for i in range(20):
        store.record(f"t{i}", None, True, 1.0)
    store.prune(keep_rows=5)
    with store._connect() as con:
        assert con.execute("select count(*) from tool_calls").fetchone()[0] == 5
