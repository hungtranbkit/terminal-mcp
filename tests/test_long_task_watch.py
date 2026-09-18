from terminal_mcp.queue_store import QueueStore
from terminal_mcp.queue_engine import QueueEngine


def test_long_task_watch_is_durable_and_idempotent(tmp_path):
    path = tmp_path / "queue.db"
    store = QueueStore(path)
    first = store.ensure_long_task_watch("t1", "s1", "r1", "lane-a", expected_minutes=45)
    assert first["state"] == "EXECUTION_START_PENDING"
    assert store.ensure_long_task_watch("t1", "s1", "r1", "lane-a")["task_id"] == "t1"
    store.update_long_task_watch("t1", state="WATCHING", execution_started_at="now", resume_count=1)
    restarted = QueueStore(path).long_task_watch("t1")
    assert restarted["state"] == "WATCHING"
    assert restarted["resume_count"] == 1


def test_long_task_metadata_requires_explicit_signal(tmp_path):
    store = QueueStore(tmp_path / "queue.db")
    task_id = store.set_tasks("lane-a", [{"prompt": "x", "metadata": {"expected_minutes": 45}}])[0]
    task = store.get_task(task_id)
    assert QueueEngine._long_task_metadata(task) == (True, 45)
    task_id = store.set_tasks("lane-b", [{"prompt": "x", "metadata": {}}])[0]
    assert QueueEngine._long_task_metadata(store.get_task(task_id)) == (False, None)


def test_watch_terminal_states_and_isolation(tmp_path):
    store = QueueStore(tmp_path / "queue.db")
    store.ensure_long_task_watch("t1", "s1", "r1", "lane-a")
    store.ensure_long_task_watch("t2", "s2", "r2", "lane-b")
    store.update_long_task_watch("t1", state="DONE", first_checkpoint_at="cp")
    assert [w["task_id"] for w in store.list_long_task_watches(active_only=True)] == ["t2"]
    assert store.long_task_watch("t1")["first_checkpoint_at"] == "cp"
