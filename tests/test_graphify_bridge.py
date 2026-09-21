from __future__ import annotations

import subprocess

from terminal_mcp.graphify_bridge import GraphifyBridge, module_question


def _graph(tmp_path):
    path = tmp_path / "graphify-out" / "graph.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"nodes":[],"edges":[]}', encoding="utf-8")
    return path


def test_query_is_bounded_and_uses_existing_graph(tmp_path, monkeypatch):
    graph_path = _graph(tmp_path)
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, stdout="x" * 500, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    bridge = GraphifyBridge(tmp_path, executable="/opt/bin/graphify")
    result = bridge.query("show auth dependencies", budget=321, max_chars=240)

    assert result.useful is True
    assert result.graph_path == str(graph_path)
    assert seen["args"] == [
        "/opt/bin/graphify", "query", "show auth dependencies",
        "--graph", str(graph_path), "--budget", "321",
    ]
    assert seen["kwargs"]["cwd"] == str(tmp_path.resolve())
    assert len(result.text) <= 240
    assert result.text.endswith("[graph truncated]")


def test_missing_graph_fails_open_without_spawning(tmp_path, monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("query must not spawn when no graph exists")

    monkeypatch.setattr(subprocess, "run", must_not_run)
    bridge = GraphifyBridge(tmp_path, executable="/opt/bin/graphify")
    result = bridge.query("where is auth?")

    assert result.available is False
    assert result.useful is False
    assert "not built" in (result.reason or "")


def test_disabled_graphify_fails_open(tmp_path, monkeypatch):
    _graph(tmp_path)
    monkeypatch.setenv("TERMINAL_MCP_GRAPHIFY", "off")

    bridge = GraphifyBridge(tmp_path, executable="/opt/bin/graphify")
    result = bridge.query("where is auth?")

    assert result.available is False
    assert "disabled" in (result.reason or "").lower()


def test_sync_is_explicit_and_code_only(tmp_path):
    bridge = GraphifyBridge(tmp_path, executable="graphify")

    assert bridge.sync_command(update=True) == ["graphify", "update", "."]
    assert bridge.sync_command(update=False, code_only=True) == [
        "graphify", "extract", ".", "--code-only",
    ]


def test_module_question_is_small_and_path_aware():
    text = module_question("auth", paths=("a.py", "b.py", "c.py", "d.py", "e.py"))

    assert "module auth" in text
    assert "a.py" in text and "d.py" in text
    assert "e.py" not in text
