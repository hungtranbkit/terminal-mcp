from __future__ import annotations

from types import SimpleNamespace

from terminal_mcp import context_pack
from terminal_mcp.graphify_bridge import GraphifyQuery


class _Spec:
    likely_module = "auth"
    relevant_modules = ()


class _NoMap:
    def __init__(self, root):
        self.root = root

    def exists(self):
        return False


class _FakeGraph:
    def __init__(self, root):
        self.root = root

    def query(self, question, **kwargs):
        assert "module auth" in question
        return GraphifyQuery(
            True,
            text="AuthService calls SessionStore; routes enter via auth_api.py.",
            graph_path="/tmp/graphify-out/graph.json",
        )


class _BrokenGraph:
    def __init__(self, root):
        pass

    def query(self, question, **kwargs):
        raise RuntimeError("optional graph unavailable")


def test_graphify_can_fill_a_module_missing_from_knowledge_map(tmp_path, monkeypatch):
    monkeypatch.setattr(context_pack, "GraphifyBridge", _FakeGraph)

    result = context_pack.load_task_knowledge(_Spec(), knowledge=_NoMap(tmp_path))

    assert result.loaded == 1
    assert result.usable is True
    assert result.unknown == ()
    assert result.packs[0].module == "auth"
    assert "SessionStore" in result.packs[0].graph_context
    assert "GRAPH CONTEXT:" in result.render()


def test_graphify_failure_does_not_break_old_context_pack_path(tmp_path, monkeypatch):
    monkeypatch.setattr(context_pack, "GraphifyBridge", _BrokenGraph)

    result = context_pack.load_task_knowledge(_Spec(), knowledge=_NoMap(tmp_path))

    assert result.loaded == 0
    assert result.usable is False
    assert result.unknown == ("auth",)
    assert any("not in the knowledge map or Graphify" in gap for gap in result.gaps)


def test_context_pack_render_remains_bounded_with_graph_context():
    pack = context_pack.ContextPack(
        module="auth",
        summary="authentication module",
        files=("auth.py",),
        graph_context="g" * (context_pack.MAX_GRAPH_CONTEXT_CHARS + 1000),
    )

    rendered = pack.render()

    assert len(rendered) <= context_pack.MAX_PACK_CHARS
    assert "MODULE auth" in rendered
