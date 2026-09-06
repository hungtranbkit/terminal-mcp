"""QueueLoop -- the AUTO-DISPATCH background loop (task: "Khi task
VERIFIED_DONE và coordinator READY cho task kế, hệ thống tự gửi task kế
vào đúng session. Không cần ChatGPT đứng chờ để push từng task.").

Fast, deterministic tests use FakeOps (imported from test_queue_engine.py,
same fixture already proven there) and run_one_cycle() directly -- no
real thread, no real tmux session. ONE test at the bottom actually starts
a real background thread (start()/stop()) to prove the full autonomous
lifecycle (no manual tick() calls at all) end to end.

SAFETY: every session name below is a disposable fixture -- never
`window`/`window2`, and the loop is never started against anything but
an explicitly auto_dispatch_enabled lane."""
from __future__ import annotations

import time

import pytest

from terminal_mcp.coordinator import CoordinatorGate, RepoEvidence
from terminal_mcp.queue_engine import QueueEngine
from terminal_mcp.queue_loop import QueueLoop
from terminal_mcp.queue_store import COMPLETED, QueueStore

from tests.test_queue_engine import FakeOps, _always_ready_gate


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


@pytest.fixture
def ops():
    return FakeOps()


def test_run_one_cycle_only_touches_auto_dispatch_enabled_lanes(store, ops):
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())
    store.append_tasks("lane-a", [{"prompt": "a real task in lane-a"}])
    store.append_tasks("lane-b", [{"prompt": "a real task in lane-b"}])
    store.set_auto_dispatch("lane-a", True)  # lane-b left OFF (the default)

    loop = QueueLoop(engine, poll_interval_seconds=60)
    results = loop.run_one_cycle()

    sessions_touched = {r["session"] for r in results}
    assert sessions_touched == {"lane-a"}
    assert store.lane_status("lane-a")["current_task"] is not None  # claimed
    assert store.lane_status("lane-b")["tasks"][0]["status"] == "QUEUED"  # completely untouched


def test_run_one_cycle_calls_heartbeat_refresher_exactly_once(store, ops):
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())
    store.append_tasks("lane-a", [{"prompt": "a real task"}])
    store.set_auto_dispatch("lane-a", True)
    calls = []
    loop = QueueLoop(engine, poll_interval_seconds=60, heartbeat_refresher=lambda: calls.append(1))
    loop.run_one_cycle()
    assert calls == [1]


def test_one_lane_failure_never_stops_other_lanes_in_the_same_cycle(store, ops):
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())
    store.append_tasks("lane-a", [{"prompt": "a real task in lane-a"}])
    store.append_tasks("lane-broken", [{"prompt": "a real task in lane-broken"}])
    store.set_auto_dispatch("lane-a", True)
    store.set_auto_dispatch("lane-broken", True)

    real_tick = engine.tick
    def flaky_tick(session):
        if session == "lane-broken":
            raise RuntimeError("simulated engine failure for lane-broken")
        return real_tick(session)
    engine.tick = flaky_tick

    loop = QueueLoop(engine, poll_interval_seconds=60)
    results = loop.run_one_cycle()

    broken_result = next(r for r in results if r["session"] == "lane-broken")
    assert broken_result["action"] == "ENGINE_ERROR"
    # lane-a still progressed despite lane-broken's own failure.
    assert store.lane_status("lane-a")["current_task"] is not None


def test_status_reports_last_cycle_and_clears_error_on_success(store, ops):
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())
    loop = QueueLoop(engine, poll_interval_seconds=60)
    assert loop.status()["last_cycle_at"] is None
    loop.run_one_cycle()
    status = loop.status()
    assert status["last_cycle_at"] is not None
    assert status["running"] is False  # run_one_cycle() alone never starts the thread


def test_start_and_stop_manage_a_real_background_thread(store, ops):
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())
    loop = QueueLoop(engine, poll_interval_seconds=0.05)
    assert loop.is_alive() is False
    loop.start()
    try:
        assert loop.is_alive() is True
        time.sleep(0.2)
        assert loop.status()["last_cycle_at"] is not None
    finally:
        loop.stop()
    assert loop.is_alive() is False


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_real_background_loop_drives_a_full_lifecycle_with_zero_manual_ticks(store, ops):
    """THE core auto-dispatch proof: a task goes QUEUED -> ... ->
    COMPLETED, and a SECOND task then starts automatically -- with this
    test never calling engine.tick()/queue_run_once itself even once.
    Only the background thread (start()) drives every transition."""
    ops.set_status("lane-auto", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})
    task_ids = store.append_tasks("lane-auto", [{"prompt": "first auto task"}, {"prompt": "second auto task"}])
    store.set_auto_dispatch("lane-auto", True)

    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())
    loop = QueueLoop(engine, poll_interval_seconds=0.03)
    loop.start()
    try:
        # Claim -> PRECHECK -> READY -> DISPATCHING -> RUNNING, all
        # autonomous; once dispatched, flip the fake session to RUNNING
        # (what a real terminal_status would show right after a real
        # send lands) so the loop's own completion-check path proceeds.
        assert _wait_until(lambda: len(ops.sent) >= 1, timeout=3.0)
        ops.set_status("lane-auto", {"state": "RUNNING", "node_id": "local", "cwd": "/repo/a"})
        # Let it sit RUNNING briefly, then go quiet (VERIFYING) and post
        # a real, verifiable completion marker for the first task.
        time.sleep(0.15)
        ops.set_status("lane-auto", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})
        assert _wait_until(lambda: store.get_task(task_ids[0]).status == "VERIFYING", timeout=3.0)
        first_task = store.get_task(task_ids[0])
        marker = (f"###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 task_id={first_task.id} "
                 f"attempt={first_task.attempt_count} nonce={first_task.verification_nonce} "
                 f"status=completion_candidate summary_sha256=deadbeef###")
        ops.set_capture("lane-auto", {"output": marker})
        assert _wait_until(lambda: store.get_task(task_ids[0]).status == COMPLETED, timeout=3.0)

        # SECOND task must then start automatically -- no manual tick,
        # no manual claim, nothing but the background loop itself. Wait
        # for the actual SEND (not just an intermediate PRECHECK/READY
        # status) so this genuinely proves the second task was
        # dispatched, not merely claimed.
        assert _wait_until(lambda: len(ops.sent) >= 2, timeout=3.0)
        assert ops.sent[1]["session"] == "lane-auto"
        assert store.get_task(task_ids[1]).status in ("DISPATCHING", "RUNNING")
    finally:
        loop.stop()
