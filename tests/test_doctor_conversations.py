"""`terminal-mcp-doctor conversations` -- fleet-wide agent-conversation-
collision diagnostic (task: "P0 AUDIT/RECOVERY -- window/window2
transcript collision").

REAL INCIDENT this exists to catch (see doctor.cmd_conversations' own
docstring for the full account): a manual recovery on dell-5530 resumed
BOTH `window` and `window2` with the exact same `claude --resume <uuid>`
transcript id, after their node-agent's own restart killed both. This
was confirmed live via a PEB-based command-line read of each session's
own foreground claude.exe child -- window and window2 were fully
independent at the process level (distinct ConPTY host pids, distinct
claude.exe pids) the whole time; only the conversation id they were
each launched with collided.

Same tmp_path/env isolation pattern as test_doctor_grants.py. Real tmux
sessions never populate resume_conversation_id (a known, disclosed gap
-- see models.py's own SessionInfo.resume_conversation_id docstring:
currently Windows-only), so the collision scenarios here monkeypatch
LocalNodeClient.list_sessions directly rather than trying to fake a
Windows-only signal through a real Linux tmux session -- this exercises
doctor.cmd_conversations' own collision-grouping/reporting logic
end-to-end (including its real config/registry/controller wiring, via
doctor.main) without depending on platform-specific process internals
this suite can't exercise for real."""
from __future__ import annotations

import json

import pytest
import yaml

from terminal_mcp import doctor
from terminal_mcp.node_client import LocalNodeClient


def _write_config(tmp_path) -> str:
    raw = {
        "permissions": {"terminal_read": True, "terminal_input": True},
        "allowed_session_patterns": ["test-*"],
        "session_lifecycle": {"enabled": True, "allowed_cwd_roots": [str(tmp_path)]},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return str(path)


def _fake_list_sessions(rows):
    def fn(self):
        return {"sessions": rows}
    return fn


def test_clean_fleet_reports_no_collisions(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TERMINAL_MCP_NODE_REGISTRY_DB", str(tmp_path / "nodes.db"))
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(LocalNodeClient, "list_sessions", _fake_list_sessions([
        {"name": "window", "resume_conversation_id": "cdbb5b70-b933-46c9-a8f1-dbe57572ea5d"},
        {"name": "window2", "resume_conversation_id": "660c7672-4a51-4d19-8766-f81063c6bd5a"},
    ]))

    code = doctor.main(["conversations", "--json", "--config", config_path])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["collisions"] == []
    assert result["node_errors"] == {}


def test_sessions_with_no_resume_id_are_never_flagged(tmp_path, monkeypatch, capsys):
    """A fresh, never-resumed conversation (resume_conversation_id=None,
    the common/expected case) must never be treated as a collision, even
    when several sessions all have it -- None means 'no signal', not
    'the same conversation'."""
    monkeypatch.setenv("TERMINAL_MCP_NODE_REGISTRY_DB", str(tmp_path / "nodes.db"))
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(LocalNodeClient, "list_sessions", _fake_list_sessions([
        {"name": "window", "resume_conversation_id": None},
        {"name": "window2", "resume_conversation_id": None},
        {"name": "window3", "resume_conversation_id": None},
    ]))

    code = doctor.main(["conversations", "--json", "--config", config_path])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["collisions"] == []


def test_two_sessions_sharing_one_conversation_is_flagged_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    """The exact real-world shape of the dell-5530 incident: window and
    window2 both resumed onto cdbb5b70-...."""
    monkeypatch.setenv("TERMINAL_MCP_NODE_REGISTRY_DB", str(tmp_path / "nodes.db"))
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(LocalNodeClient, "list_sessions", _fake_list_sessions([
        {"name": "window", "resume_conversation_id": "cdbb5b70-b933-46c9-a8f1-dbe57572ea5d"},
        {"name": "window2", "resume_conversation_id": "cdbb5b70-b933-46c9-a8f1-dbe57572ea5d"},
    ]))

    code = doctor.main(["conversations", "--json", "--config", config_path])
    result = json.loads(capsys.readouterr().out)
    assert code == 1
    assert len(result["collisions"]) == 1
    row = result["collisions"][0]
    assert row["node_id"] == "local"
    assert row["conversation_id"] == "cdbb5b70-b933-46c9-a8f1-dbe57572ea5d"
    assert sorted(row["sessions"]) == ["window", "window2"]


def test_three_sessions_sharing_one_conversation_lists_all_three(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TERMINAL_MCP_NODE_REGISTRY_DB", str(tmp_path / "nodes.db"))
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(LocalNodeClient, "list_sessions", _fake_list_sessions([
        {"name": "a", "resume_conversation_id": "shared-id"},
        {"name": "b", "resume_conversation_id": "shared-id"},
        {"name": "c", "resume_conversation_id": "shared-id"},
        {"name": "d", "resume_conversation_id": "different-id"},
    ]))

    code = doctor.main(["conversations", "--json", "--config", config_path])
    result = json.loads(capsys.readouterr().out)
    assert code == 1
    assert len(result["collisions"]) == 1
    assert sorted(result["collisions"][0]["sessions"]) == ["a", "b", "c"]


def test_human_output_names_both_colliding_sessions_and_warns_not_to_use_them(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TERMINAL_MCP_NODE_REGISTRY_DB", str(tmp_path / "nodes.db"))
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(LocalNodeClient, "list_sessions", _fake_list_sessions([
        {"name": "window", "resume_conversation_id": "cdbb5b70-b933-46c9-a8f1-dbe57572ea5d"},
        {"name": "window2", "resume_conversation_id": "cdbb5b70-b933-46c9-a8f1-dbe57572ea5d"},
    ]))

    code = doctor.main(["conversations", "--config", config_path])
    out = capsys.readouterr().out
    assert code == 1
    assert "window" in out and "window2" in out
    assert "INVESTIGATE" in out
