from __future__ import annotations

import json
import sqlite3

import pytest

from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.orchestrator_checkpoint import (
    OrchestratorCheckpointError,
    OrchestratorCheckpointStore,
    default_orchestrator_db_path,
)
from terminal_mcp.project_service import ProjectService
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore

PROJECT = "git:github.com/acme/chat-recovery"
FAKE_KEY = "sk-or-FAKEFAKEFAKEFAKEFAKEFAKE"


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def call(server, tool, **kwargs):
    result = await server.call_tool(tool, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def test_default_path_honours_env(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.db"
    monkeypatch.setenv("TERMINAL_MCP_ORCHESTRATOR_DB", str(explicit))
    assert default_orchestrator_db_path() == explicit


def test_append_latest_list_project_isolation_and_restart(tmp_path):
    path = tmp_path / "orchestrator.db"
    store = OrchestratorCheckpointStore(path)
    first = store.checkpoint(PROJECT, "first", next_actions=["A"])
    second = store.checkpoint(PROJECT, "second", next_actions=["B"])
    store.checkpoint("other", "not ours")

    assert first["id"] != second["id"]
    assert store.recover(PROJECT)["summary"] == "second"
    history = store.list(PROJECT, limit=10)
    assert history["total"] == 2
    assert [item["summary"] for item in history["items"]] == ["second", "first"]

    reopened = OrchestratorCheckpointStore(path)
    assert reopened.recover(PROJECT)["id"] == second["id"]
    assert reopened.recover("other")["summary"] == "not ours"


def test_recursive_redaction_happens_before_sqlite_write(tmp_path):
    path = tmp_path / "orchestrator.db"
    store = OrchestratorCheckpointStore(path)
    saved = store.checkpoint(
        PROJECT,
        f"summary {FAKE_KEY}",
        decisions=[f"never persist {FAKE_KEY}"],
        active_tasks=[{"task_id": "T1", "detail": {"token": FAKE_KEY}}],
        blockers=[{"message": f"Authorization: Bearer {FAKE_KEY}"}],
    )
    serialized = json.dumps(saved, ensure_ascii=False)
    assert FAKE_KEY not in serialized
    assert "<REDACTED>" in serialized

    raw = path.read_bytes()
    wal = path.with_name(path.name + "-wal")
    if wal.exists():
        raw += wal.read_bytes()
    assert FAKE_KEY.encode() not in raw


def test_validation_and_missing_checkpoint_are_stable(tmp_path):
    store = OrchestratorCheckpointStore(tmp_path / "orchestrator.db")
    with pytest.raises(OrchestratorCheckpointError) as invalid:
        store.checkpoint("   ", "x")
    assert invalid.value.code == "INVALID_PROJECT_ID"

    with pytest.raises(OrchestratorCheckpointError) as missing:
        store.recover(PROJECT)
    assert missing.value.code == "CHECKPOINT_NOT_FOUND"

    with pytest.raises(OrchestratorCheckpointError) as bad_list:
        store.checkpoint(PROJECT, "x", decisions="not-a-list")
    assert bad_list.value.code == "INVALID_CHECKPOINT"


@pytest.mark.anyio
async def test_mcp_tools_checkpoint_recover_live_status_and_do_not_dispatch(tmp_path):
    store = OrchestratorCheckpointStore(tmp_path / "orchestrator.db")
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    server = build_mcp(
        queue=queue,
        chat_checkpoints=store,
        default_optional_services=False,
    )

    saved = await call(
        server,
        "terminal_chat_checkpoint",
        project_id=PROJECT,
        summary="delegated lanes",
        current_goal="finish UI",
        active_tasks=[{"task_id": "UI-1", "session": "coder-01", "status": "RUNNING"}],
        next_actions=["inspect", "merge", "deploy"],
    )
    assert saved["summary"] == "delegated lanes"
    assert queue.store.project_task_counts(PROJECT) == {}

    recovered = await call(server, "terminal_chat_recover", project_id=PROJECT)
    assert recovered["id"] == saved["id"]
    assert recovered["live_project_status"]["project_id"] == PROJECT
    assert queue.store.project_task_counts(PROJECT) == {}

    listed = await call(server, "terminal_chat_checkpoint_list", project_id=PROJECT, limit=5)
    assert listed["total"] == 1 and listed["items"][0]["id"] == saved["id"]


@pytest.mark.anyio
async def test_recover_survives_live_project_projection_failure(tmp_path, monkeypatch):
    store = OrchestratorCheckpointStore(tmp_path / "orchestrator.db")
    store.checkpoint(PROJECT, "durable state")
    queue = QueueService(QueueStore(tmp_path / "queue.db"))

    def broken_status(self, project_id):
        raise RuntimeError("internal path and credential-like detail must not escape")

    monkeypatch.setattr(ProjectService, "status", broken_status)
    server = build_mcp(
        queue=queue,
        chat_checkpoints=store,
        default_optional_services=False,
    )
    recovered = await call(server, "terminal_chat_recover", project_id=PROJECT)
    assert recovered["summary"] == "durable state"
    assert recovered["live_project_status"]["error"] == "PROJECT_STATUS_UNAVAILABLE"
    assert "credential" not in json.dumps(recovered["live_project_status"]).lower()


@pytest.mark.anyio
async def test_tool_descriptions_teach_checkpoint_and_new_chat_recovery(tmp_path):
    store = OrchestratorCheckpointStore(tmp_path / "orchestrator.db")
    server = build_mcp(chat_checkpoints=store, default_optional_services=False)
    tools = {tool.name: tool for tool in await server.list_tools()}
    assert {"terminal_chat_checkpoint", "terminal_chat_recover", "terminal_chat_checkpoint_list"} <= set(tools)
    checkpoint_doc = tools["terminal_chat_checkpoint"].description.lower()
    recover_doc = tools["terminal_chat_recover"].description.lower()
    assert "before" in checkpoint_doc and "delegation" in checkpoint_doc and "merge/deploy" in checkpoint_doc
    assert "new chat" in recover_doc and "first" in recover_doc


@pytest.mark.anyio
async def test_bare_build_registers_chat_recovery_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("TERMINAL_MCP_ORCHESTRATOR_DB", str(tmp_path / "orchestrator.db"))
    server = build_mcp()
    names = {tool.name for tool in await server.list_tools()}
    assert {"terminal_chat_checkpoint", "terminal_chat_recover", "terminal_chat_checkpoint_list"} <= names


@pytest.mark.anyio
async def test_mcp_returns_stable_error_for_missing_checkpoint(tmp_path):
    store = OrchestratorCheckpointStore(tmp_path / "orchestrator.db")
    server = build_mcp(chat_checkpoints=store, default_optional_services=False)
    result = await call(server, "terminal_chat_recover", project_id=PROJECT)
    assert result["error"] == "CHECKPOINT_NOT_FOUND"
