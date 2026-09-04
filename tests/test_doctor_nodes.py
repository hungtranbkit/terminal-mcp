"""`terminal-mcp-doctor nodes` -- read-only multi-node fleet diagnostics
(task item 13). Every test here isolates both the config file AND the
node registry DB path (TERMINAL_MCP_NODE_REGISTRY_DB) to tmp_path -- this
CLI's whole point is to read the real default registry when a real
operator runs it, so a test that didn't override that would pollute the
real production ~/.local/state/terminal-mcp/nodes.db (see controller.py's
build_default_controller docstring for the exact incident this class of
mistake already caused once in this feature's own development)."""
from __future__ import annotations

import json

import yaml

from terminal_mcp import doctor


def _write_config(tmp_path, **nodes_overrides) -> str:
    raw = {
        "permissions": {"terminal_read": True},
        "allowed_session_patterns": ["test-*"],
        "session_lifecycle": {"enabled": True, "allowed_cwd_roots": [str(tmp_path)]},
    }
    if nodes_overrides:
        raw["nodes"] = nodes_overrides
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return str(path)


def test_local_only_reports_online_healthy_exit_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TERMINAL_MCP_NODE_REGISTRY_DB", str(tmp_path / "nodes.db"))
    config_path = _write_config(tmp_path)
    code = doctor.main(["nodes", "--json", "--config", config_path])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["id"] == "local"
    assert result["nodes"][0]["status"] == "online"
    assert result["skipped_remote_nodes"] == []


def test_human_output_lists_local_node(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TERMINAL_MCP_NODE_REGISTRY_DB", str(tmp_path / "nodes.db"))
    config_path = _write_config(tmp_path)
    code = doctor.main(["nodes", "--config", config_path])
    assert code == 0
    out = capsys.readouterr().out
    assert "local" in out
    assert "status=online" in out


def test_remote_node_missing_token_env_reported_skipped_and_nonzero_exit(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TERMINAL_MCP_NODE_REGISTRY_DB", str(tmp_path / "nodes.db"))
    monkeypatch.delenv("TERMINAL_MCP_NODE_TOKEN_M910", raising=False)
    config_path = _write_config(tmp_path, remote=[{
        "node_id": "m910", "endpoint": "http://192.168.1.50:8790", "token_env": "TERMINAL_MCP_NODE_TOKEN_M910",
    }])
    code = doctor.main(["nodes", "--json", "--config", config_path])
    assert code == 1  # onboarding incomplete -- honestly reported, not silently ignored
    result = json.loads(capsys.readouterr().out)
    assert len(result["skipped_remote_nodes"]) == 1
    assert result["skipped_remote_nodes"][0]["node_id"] == "m910"
    assert "TERMINAL_MCP_NODE_TOKEN_M910" in result["skipped_remote_nodes"][0]["reason"]
    # Only the local node was actually registered -- the remote entry
    # never silently became a phantom "online" row.
    assert [n["id"] for n in result["nodes"]] == ["local"]


def test_remote_node_with_token_but_unreachable_endpoint_reports_test_connection_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TERMINAL_MCP_NODE_REGISTRY_DB", str(tmp_path / "nodes.db"))
    monkeypatch.setenv("TERMINAL_MCP_NODE_TOKEN_M910", "some-token")
    config_path = _write_config(tmp_path, remote=[{
        "node_id": "m910", "endpoint": "http://127.0.0.1:1", "token_env": "TERMINAL_MCP_NODE_TOKEN_M910",
        "timeout_seconds": 1.0,
    }])
    code = doctor.main(["nodes", "--json", "--config", config_path])
    assert code == 1  # m910 registered but offline (never heartbeated) -> not fully healthy
    result = json.loads(capsys.readouterr().out)
    by_id = {n["id"]: n for n in result["nodes"]}
    assert "m910" in by_id
    assert by_id["m910"]["status"] == "offline"  # registered, but no heartbeat has ever arrived
    assert by_id["m910"]["test_connection"]["ok"] is False  # real, live connection attempt, refused


def test_local_node_never_gets_a_test_connection_probe(tmp_path, monkeypatch, capsys):
    # test_connection is meaningless (and, per controller.py, hardcoded
    # ok=True/latency 0) for the local node -- confirms doctor doesn't
    # even attempt/report one for it, only for remote entries.
    monkeypatch.setenv("TERMINAL_MCP_NODE_REGISTRY_DB", str(tmp_path / "nodes.db"))
    config_path = _write_config(tmp_path)
    doctor.main(["nodes", "--json", "--config", config_path])
    result = json.loads(capsys.readouterr().out)
    assert "test_connection" not in result["nodes"][0]


def test_local_node_capability_report_includes_platform_and_backend(tmp_path, monkeypatch, capsys):
    # Task's own explicit capability report field list -- available via
    # the CLI, not only the dashboard.
    monkeypatch.setenv("TERMINAL_MCP_NODE_REGISTRY_DB", str(tmp_path / "nodes.db"))
    config_path = _write_config(tmp_path)
    doctor.main(["nodes", "--json", "--config", config_path])
    row = json.loads(capsys.readouterr().out)["nodes"][0]
    assert row["platform"] == "linux"
    assert row["session_backend"] == "tmux"
    assert row["wsl_available"] is False
    assert isinstance(row["shell_capabilities"], list)
    assert isinstance(row["claude_available"], bool)
    assert isinstance(row["codex_available"], bool)


def test_human_output_shows_os_and_backend(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TERMINAL_MCP_NODE_REGISTRY_DB", str(tmp_path / "nodes.db"))
    config_path = _write_config(tmp_path)
    doctor.main(["nodes", "--config", config_path])
    out = capsys.readouterr().out
    assert "[linux/tmux]" in out
