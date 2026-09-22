from pathlib import Path

import pytest

from terminal_mcp.context_compiler import ContextBudgetError, compile_context
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.work_runtime import WorkRuntimeStore, WORK_RUNNING


def runtime(tmp_path: Path) -> WorkRuntimeStore:
    return WorkRuntimeStore(QueueStore(tmp_path / "queue.db"))


def test_context_compiler_dedupes_and_bounds_soft_lanes():
    packet = compile_context({
        "mandatory": ["Never deploy without approval.", "Never deploy without approval"],
        "experience": ["fix one", "fix two", "fix three"],
    }, budgets={"mandatory": 100, "experience": 12})
    assert packet.lanes["mandatory"] == ("Never deploy without approval.",)
    assert packet.stats["mandatory"]["duplicate_items"] == 1
    assert packet.stats["experience"]["dropped_items"] >= 1
    assert len(packet.fingerprint) == 64


def test_context_compiler_never_silently_drops_mandatory():
    with pytest.raises(ContextBudgetError):
        compile_context({"mandatory": ["hard rule"]}, budgets={"mandatory": 3})


def test_work_session_survives_provider_handoff(tmp_path):
    store = runtime(tmp_path)
    work = store.create("git:example/repo", "Implement knowledge", objective="finish feature")
    first = store.start_attempt(work.id, provider="openai", agent_type="codex",
                                terminal_session="codex-work", context_fingerprint="ctx-a")
    second = store.start_attempt(work.id, provider="anthropic", agent_type="claude",
                                 terminal_session="claude-work", context_fingerprint="ctx-b",
                                 handoff_reason="provider limit")
    current = store.get(work.id)
    attempts = store.attempts(work.id)
    assert current is not None and current.status == WORK_RUNNING
    assert current.current_attempt_id == second["id"]
    assert attempts[0]["id"] == first["id"] and attempts[0]["status"] == "HANDED_OFF"
    assert attempts[1]["status"] == "ACTIVE"


def test_verified_experience_requires_evidence_and_redacts(tmp_path):
    store = runtime(tmp_path)
    with pytest.raises(ValueError):
        store.add_verified_experience("p", kind="bugfix", title="x", content="x", evidence={})
    saved = store.add_verified_experience(
        "p", kind="bugfix", title="auth fix", content="OPENAI_API_KEY=secretvalue",
        evidence={"test": "passed", "Authorization": "Bearer abc"})
    assert "secretvalue" not in saved["content"]
    assert saved["evidence"]
