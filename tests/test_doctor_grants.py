"""`terminal-mcp-doctor grants` -- fleet-wide granted-but-ineffective-input
diagnostic (task: "P0 HOTFIX REMOTE PERMISSION FLAP" item 8's own
"invariant/assertion/doctor check"). Same tmp_path/env isolation pattern
as test_doctor_nodes.py -- never touches the real production registry.

This file also stands as the regression test for a real bug this
feature's own first live run caught: `from .node_registry import
NodeRegistry, NODE_ONLINE` raised ImportError (NODE_ONLINE actually
lives in node_models.py) -- every test below exercises doctor.main
end-to-end, so a broken import at cmd_grants' own module level fails
loudly here, not silently in production only when a real node happens
to be offline."""
from __future__ import annotations

import json
import subprocess

import pytest
import yaml

from terminal_mcp import doctor
from terminal_mcp.grants import SessionGrantStore


def _write_config(tmp_path) -> str:
    raw = {
        "permissions": {"terminal_read": True, "terminal_input": True},
        "allowed_session_patterns": ["test-*"],
        "session_lifecycle": {"enabled": True, "allowed_cwd_roots": [str(tmp_path)]},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return str(path)


@pytest.fixture
def tmux_cleanup():
    created: list[str] = []
    yield created
    for name in created:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False, capture_output=True)


def test_clean_fleet_reports_nothing_flagged(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TERMINAL_MCP_NODE_REGISTRY_DB", str(tmp_path / "nodes.db"))
    monkeypatch.setenv("TERMINAL_MCP_GRANTS_DB", str(tmp_path / "grants.db"))
    config_path = _write_config(tmp_path)
    code = doctor.main(["grants", "--json", "--config", config_path])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["flagged"] == []
    assert result["node_errors"] == {}


def test_stale_identity_pin_is_flagged_but_explained_and_exits_zero(tmp_path, monkeypatch, capsys, tmux_cleanup):
    monkeypatch.setenv("TERMINAL_MCP_NODE_REGISTRY_DB", str(tmp_path / "nodes.db"))
    grants_path = tmp_path / "grants.db"
    monkeypatch.setenv("TERMINAL_MCP_GRANTS_DB", str(grants_path))
    config_path = _write_config(tmp_path)

    name = "test-doctor-grants-stale1"
    tmux_cleanup.append(name)
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", str(tmp_path), "bash -lc 'sleep 30'"], check=True)

    # A real active grant, deliberately pinned to an identity that does
    # NOT match this session's real one -- same mechanism as the live
    # openclaw910 incident this whole feature was built to diagnose.
    store = SessionGrantStore(grants_path)
    store.set_read(name, True, granted_by="tester")
    store.set_input(name, True, granted_by="tester",
                    pinned_session_id="$999", pinned_pane_id="%999", pinned_created_epoch=1)

    code = doctor.main(["grants", "--json", "--config", config_path])
    result = json.loads(capsys.readouterr().out)
    row = next(r for r in result["flagged"] if r["session"] == name)
    assert row["node_id"] == "local"
    assert row["reason"] == "IDENTITY_MISMATCH"
    assert row["explained"] is True
    assert code == 0  # an explained, expected state -- never a failure exit


def test_human_output_labels_stale_pin_as_re_grant_to_fix(tmp_path, monkeypatch, capsys, tmux_cleanup):
    monkeypatch.setenv("TERMINAL_MCP_NODE_REGISTRY_DB", str(tmp_path / "nodes.db"))
    grants_path = tmp_path / "grants.db"
    monkeypatch.setenv("TERMINAL_MCP_GRANTS_DB", str(grants_path))
    config_path = _write_config(tmp_path)

    name = "test-doctor-grants-stale2"
    tmux_cleanup.append(name)
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", str(tmp_path), "bash -lc 'sleep 30'"], check=True)
    store = SessionGrantStore(grants_path)
    store.set_read(name, True, granted_by="tester")
    store.set_input(name, True, granted_by="tester",
                    pinned_session_id="$999", pinned_pane_id="%999", pinned_created_epoch=1)

    code = doctor.main(["grants", "--config", config_path])
    out = capsys.readouterr().out
    assert code == 0
    assert "re-grant to fix" in out
    assert name in out
