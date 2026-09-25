import pytest

from terminal_mcp.queue_store import QueueStore


def test_fast_agent_checkpoint_delta_survives_store_restart(tmp_path):
    db = tmp_path / "queue.db"
    store = QueueStore(db)
    (task_id,) = store.set_tasks("worker", [{"prompt": "do the work",
                                                "metadata": {"fast_agent_mode": True}}])

    saved = store.record_fast_agent_checkpoint(task_id, {
        "goal": "do the work", "findings": ["bug is in parser"],
        "files_read": ["src/parser.py"], "changed_files": [], "tests": [],
        "failures": [], "next_action": "add regression test",
        "repo": {"path": "/repo", "branch": "feature", "commit": "abc"},
    })
    store = QueueStore(db)
    row = store.get_task(task_id).metadata["fast_agent_checkpoint"]
    assert row == saved
    assert row["task_id"] == task_id and row["version"] == 1

    store.record_fast_agent_checkpoint(task_id, {"tests": ["pytest tests/test_parser.py"]})
    row = QueueStore(db).get_task(task_id).metadata["fast_agent_checkpoint"]
    assert row["version"] == 2
    assert row["findings"] == ["bug is in parser"]
    assert row["tests"] == ["pytest tests/test_parser.py"]


def test_fast_agent_checkpoint_rejects_unbounded_unknown_fields(tmp_path):
    store = QueueStore(tmp_path / "queue.db")
    (task_id,) = store.set_tasks("worker", [{"prompt": "do the work"}])
    with pytest.raises(ValueError, match="unsupported checkpoint fields"):
        store.record_fast_agent_checkpoint(task_id, {"full_logs": "not allowed"})


def test_terminal_turn_task_checkpoint_action_routes_a_bounded_delta():
    from terminal_mcp.compact_tools import CompactTerminalTools

    calls = []
    tools = CompactTerminalTools(object(), object(), handlers={
        "task_checkpoint": lambda task_id, checkpoint: calls.append((task_id, checkpoint)) or checkpoint,
    })
    result = tools.turn(action="task_checkpoint", task_id="task-a",
                        args={"checkpoint": {"next_action": "run focused test"}})
    assert result["status"] == "OK"
    assert calls == [("task-a", {"next_action": "run focused test"})]
