from concurrent.futures import ThreadPoolExecutor

from terminal_mcp.queue_store import DISPATCHING, QueueStore


def _dispatching(tmp_path):
    store = QueueStore(tmp_path / "queue.db")
    task_id = store.set_tasks("synthetic-lane", [{"prompt": "synthetic A"}])[0]
    store.transition_task(task_id, "PRECHECK", event_type="TEST_PRECHECK")
    store.transition_task(task_id, "READY", event_type="TEST_READY")
    store.transition_task(task_id, DISPATCHING, event_type="TEST_DISPATCH",
                          extra_fields={"dispatch_idempotency_key": "key-a"})
    return store, task_id


def test_queue_worker_claim_is_atomic_and_idempotent(tmp_path):
    store, task_id = _dispatching(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda n: store.claim_cli_worker(
            task_id, dispatch_key="key-a", worker_id=f"w{n}", pid=1000 + n), range(8)))
    saved = store.get_task(task_id).metadata["cli_worker"]
    assert all(r == saved for r in receipts)
    assert saved["state"] == "RUNNING" and saved["pid"] > 0
    assert store.claim_cli_worker(task_id, dispatch_key="key-a", worker_id="restart", pid=2000) == saved
    assert store.get_task(task_id).status == DISPATCHING  # worker receipt alone is never DONE


def test_queue_worker_receipt_is_fenced_to_claiming_worker(tmp_path):
    store, task_id = _dispatching(tmp_path)
    store.claim_cli_worker(task_id, dispatch_key="key-a", worker_id="worker-a", pid=101)
    assert store.update_cli_worker(task_id, worker_id="worker-b",
                                   changes={"state": "BLOCKED", "blocker": "lost lease"}) is None
    updated = store.update_cli_worker(task_id, worker_id="worker-a",
                                      changes={"heartbeat_at": 1234.0, "lease_expires_at": 1300.0})
    assert updated["heartbeat_at"] == 1234.0
    assert store.claim_cli_worker(task_id, dispatch_key="wrong-key", worker_id="worker-c", pid=102) is None


def test_cli_worker_cannot_claim_task_after_autopilot_moved_it(tmp_path):
    store, task_id = _dispatching(tmp_path)
    store.transition_task(task_id, "BLOCKED", event_type="TEST_BLOCKED", reason="synthetic blocker")
    assert store.claim_cli_worker(task_id, dispatch_key="key-a", worker_id="late", pid=55) is None
