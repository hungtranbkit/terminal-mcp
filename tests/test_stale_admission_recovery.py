from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from terminal_mcp.config import LLMGovernorConfig
from terminal_mcp.queue_engine import QueueEngine
from terminal_mcp.queue_loop import QueueLoop
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.request_governor import RequestGovernor
from tests.test_queue_engine import FakeOps, _always_ready_gate


def setup_case(tmp_path, state="RUNNING", age=3600):
    store = QueueStore(tmp_path / "queue.db")
    ops = FakeOps()
    old_id = store.append_tasks("old-lane", [{"prompt": "old work"}])[0]
    store.transition_task(old_id, "DISPATCHING", event_type="DISPATCHING")
    store.transition_task(old_id, "RUNNING", event_type="RUNNING")
    if state == "VERIFYING":
        store.transition_task(old_id, state, event_type=state)
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE queue_tasks SET updated_at=?,lease_expires_at=? WHERE id=?", (stamp, stamp, old_id))
    governor = RequestGovernor(LLMGovernorConfig(global_max_concurrency=1), store)
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate(), governor=governor)
    return store, ops, governor, engine, old_id


@pytest.mark.parametrize("state", ["RUNNING", "VERIFYING"])
def test_missing_old_session_releases_slot_even_when_auto_dispatch_is_off(tmp_path, state, caplog):
    store, ops, governor, engine, old_id = setup_case(tmp_path, state)
    ops.set_status("old-lane", {"error": "SESSION_LOCATION_UNKNOWN"})
    next_id = store.append_tasks("new-lane", [{"prompt": "new useful work"}])[0]
    store.set_auto_dispatch("new-lane", True)
    ops.set_status("new-lane", {"state": "IDLE", "node_id": "local", "cwd": "/repo/new"})
    loop = QueueLoop(engine)
    loop.run_one_cycle()
    assert store.get_task(old_id).status == "WAITING_SESSION"
    assert store.lane_status("old-lane")["auto_dispatch_enabled"] is False
    assert store.get_task(next_id).status == "PRECHECK"
    loop.run_one_cycle()
    loop.run_one_cycle()
    assert store.get_task(next_id).status == "RUNNING"
    assert governor.status()["global_reserved"] == 1
    assert all(send["session"] == "new-lane" for send in ops.sent)
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


def test_idle_old_verification_releases_slot_without_fabricating_completion(tmp_path, monkeypatch):
    store, ops, governor, engine, old_id = setup_case(tmp_path, "VERIFYING")
    ops.set_status("old-lane", {"state": "IDLE"})
    monkeypatch.setattr("terminal_mcp.queue_engine.time.monotonic", lambda: 0.0)
    engine.reconcile_stale_active_tasks()
    assert store.get_task(old_id).status == "VERIFYING"
    monkeypatch.setattr("terminal_mcp.queue_engine.time.monotonic", lambda: 901.0)
    engine.reconcile_stale_active_tasks()
    task = store.get_task(old_id)
    assert task.status == "BLOCKED"
    assert "STALE_ACTIVE_TIMEOUT" in task.last_error
    assert governor.status()["global_reserved"] == 0
    assert not ops.sent


@pytest.mark.parametrize("age,response", [
    (60, {"error": "SESSION_LOCATION_UNKNOWN"}),
    (86400, {"state": "RUNNING"}),
    (86400, {"error": "INTERNAL_ERROR"}),
    (86400, {"state": "UNKNOWN"}),
])
def test_young_live_or_uncertain_tasks_are_not_reset(tmp_path, age, response):
    store, ops, governor, engine, old_id = setup_case(tmp_path, age=age)
    ops.set_status("old-lane", response)
    QueueLoop(engine).run_one_cycle()
    assert store.get_task(old_id).status == "RUNNING"
    assert governor.status()["global_reserved"] == 1


def test_recovery_does_not_overwrite_concurrent_cancellation(tmp_path):
    store, ops, _, engine, old_id = setup_case(tmp_path)
    def cancel_during_probe(session):
        store.transition_task(old_id, "CANCELLED", event_type="CANCELLED")
        return {"error": "SESSION_NOT_FOUND"}
    ops.terminal_status = cancel_during_probe
    QueueLoop(engine).run_one_cycle()
    assert store.get_task(old_id).status == "CANCELLED"


def test_one_probe_exception_does_not_strand_other_old_tasks(tmp_path):
    store, ops, _, engine, old_id = setup_case(tmp_path)
    other = store.append_tasks("other-old", [{"prompt": "other task"}])[0]
    store.transition_task(other, "DISPATCHING", event_type="DISPATCHING")
    store.transition_task(other, "RUNNING", event_type="RUNNING")
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE queue_tasks SET updated_at=? WHERE id=?", (store.get_task(old_id).updated_at, other))
    def probe(session):
        if session == "old-lane":
            raise TimeoutError("temporary probe failure")
        return {"error": "SESSION_NOT_FOUND"}
    ops.terminal_status = probe
    QueueLoop(engine).run_one_cycle()
    assert store.get_task(old_id).status == "RUNNING"
    assert store.get_task(other).status == "WAITING_SESSION"


def test_timeout_configuration_controls_recovery(tmp_path):
    store, ops, _, _, old_id = setup_case(tmp_path, age=120)
    governor = RequestGovernor(LLMGovernorConfig(stale_active_timeout_seconds=60), store)
    engine = QueueEngine(store, ops, governor=governor)
    ops.set_status("old-lane", {"error": "SESSION_NOT_FOUND"})
    QueueLoop(engine).run_one_cycle()
    assert store.get_task(old_id).status == "WAITING_SESSION"


def test_stale_timeout_environment_is_validated(monkeypatch):
    from terminal_mcp.config import load_config
    monkeypatch.setenv("LLM_STALE_ACTIVE_TIMEOUT_SEC", "120")
    assert load_config("config.example.yaml").llm_governor.stale_active_timeout_seconds == 120
    monkeypatch.setenv("LLM_STALE_ACTIVE_TIMEOUT_SEC", "0")
    with pytest.raises(ValueError, match="LLM_STALE_ACTIVE_TIMEOUT_SEC"):
        load_config("config.example.yaml")


@pytest.mark.parametrize("response", [
    {"state": "RUNNING"}, {"state": "UNKNOWN"}, {"error": "INTERNAL_ERROR"},
    {"state": "IDLE", "last_output": "new progress"},
])
def test_live_uncertain_or_new_output_resets_inactivity(tmp_path, monkeypatch, response):
    store, ops, _, engine, task_id = setup_case(tmp_path)
    clock = [0.0]
    monkeypatch.setattr("terminal_mcp.queue_engine.time.monotonic", lambda: clock[0])
    ops.set_status("old-lane", {"state": "IDLE"})
    engine.reconcile_stale_active_tasks()
    assert store.get_task(task_id).status == "RUNNING"
    clock[0] = 899.0
    ops.set_status("old-lane", response)
    engine.reconcile_stale_active_tasks()
    clock[0] = 901.0
    ops.set_status("old-lane", {"state": "IDLE", "last_output": response.get("last_output", "")})
    engine.reconcile_stale_active_tasks()
    assert store.get_task(task_id).status == "RUNNING"


@pytest.mark.parametrize("state", ["RUNNING", "VERIFYING"])
def test_valid_completion_marker_survives_idle_timeout(tmp_path, monkeypatch, state):
    store, ops, _, engine, task_id = setup_case(tmp_path, state)
    nonce = store.ensure_verification_nonce(task_id)
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE queue_tasks SET updated_at='2026-01-01T00:00:00Z' WHERE id=?", (task_id,))
    task = store.get_task(task_id)
    ops.set_status("old-lane", {"state": "IDLE"})
    ops.set_capture("old-lane", {"output": (
        f"###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 task_id={task_id} "
        f"attempt={task.attempt_count} nonce={nonce} status=completion_candidate summary_sha256=deadbeef###"
    )})
    monkeypatch.setattr("terminal_mcp.queue_engine.time.monotonic", lambda: 0.0)
    engine.reconcile_stale_active_tasks()
    monkeypatch.setattr("terminal_mcp.queue_engine.time.monotonic", lambda: 901.0)
    engine.reconcile_stale_active_tasks()
    assert store.get_task(task_id).status == ("COMPLETED" if state == "VERIFYING" else state)
    assert not ops.sent


def test_active_external_verifier_is_preserved(tmp_path, monkeypatch):
    from terminal_mcp.verify_queue import VerifyQueue
    store, ops, _, engine, task_id = setup_case(tmp_path, "VERIFYING")
    verify = VerifyQueue(store)
    verify.ensure_verify_job(store.get_task(task_id))
    job = verify.claim_next(verifier="reviewer", lease_seconds=3600)
    verify.start(job.id, job.claim_token)
    ops.set_status("old-lane", {"state": "IDLE"})
    monkeypatch.setattr("terminal_mcp.queue_engine.time.monotonic", lambda: 0.0)
    engine.reconcile_stale_active_tasks()
    monkeypatch.setattr("terminal_mcp.queue_engine.time.monotonic", lambda: 901.0)
    engine.reconcile_stale_active_tasks()
    assert store.get_task(task_id).status == "VERIFYING"


def test_verifier_claim_race_blocks_missing_session_recovery(tmp_path):
    from terminal_mcp.verify_queue import VerifyQueue
    store, ops, _, engine, task_id = setup_case(tmp_path, "VERIFYING")
    verify = VerifyQueue(store)
    verify.ensure_verify_job(store.get_task(task_id))
    def claim_during_probe(session):
        verify.claim_next(verifier="reviewer", lease_seconds=3600)
        return {"error": "SESSION_NOT_FOUND"}
    ops.terminal_status = claim_during_probe
    engine.reconcile_stale_active_tasks()
    assert store.get_task(task_id).status == "VERIFYING"


def test_old_running_task_finishes_normally_on_enabled_lane(tmp_path):
    store, ops, _, engine, task_id = setup_case(tmp_path)
    nonce = store.ensure_verification_nonce(task_id)
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE queue_tasks SET updated_at='2026-01-01T00:00:00Z' WHERE id=?", (task_id,))
    task = store.get_task(task_id)
    store.set_auto_dispatch("old-lane", True)
    ops.set_status("old-lane", {"state": "IDLE"})
    ops.set_capture("old-lane", {"output": (
        f"###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 task_id={task_id} "
        f"attempt={task.attempt_count} nonce={nonce} status=completion_candidate summary_sha256=deadbeef###"
    )})
    loop = QueueLoop(engine)
    loop.run_one_cycle()
    assert store.get_task(task_id).status == "VERIFYING"
    loop.run_one_cycle()
    assert store.get_task(task_id).status == "COMPLETED"
    assert not ops.sent


def test_expired_verifier_lease_does_not_strand_missing_session(tmp_path):
    from terminal_mcp.verify_queue import VerifyQueue
    store, ops, _, engine, task_id = setup_case(tmp_path, "VERIFYING")
    verify = VerifyQueue(store)
    verify.ensure_verify_job(store.get_task(task_id))
    job = verify.claim_next(verifier="reviewer")
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE verify_jobs SET lease_expires_at='2026-01-01T00:00:00Z' WHERE id=?", (job.id,))
    ops.set_status("old-lane", {"error": "SESSION_NOT_FOUND"})
    engine.reconcile_stale_active_tasks()
    assert store.get_task(task_id).status == "WAITING_SESSION"
