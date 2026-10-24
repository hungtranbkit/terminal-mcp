import sqlite3
import threading
import time

from terminal_mcp.controller import _fanout
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


def test_single_node_obeys_deadline():
    release = threading.Event()
    try:
        start = time.monotonic()
        result = _fanout([('p0-single', lambda: release.wait(.5))], budget_seconds=.03, label='p0')
        assert time.monotonic() - start < .2
        assert not result['p0-single'][0]
    finally:
        release.set()


def test_repeated_timeouts_do_not_multiply_node_calls():
    release = threading.Event()
    calls = []
    def slow():
        calls.append(1)
        release.wait(1)
    try:
        for _ in range(4):
            _fanout([('p0-slow', slow), ('p0-fast', lambda: 1)], budget_seconds=.02, label='p0-repeat')
        assert len(calls) == 1
    finally:
        release.set()


def test_pending_counts_does_not_wait_for_sqlite_writer(tmp_path):
    store = QueueStore(tmp_path / 'queue.db')
    store.append_tasks('lane-a', [{'prompt': 'keep this work'}])
    with sqlite3.connect(store.path) as setup:
        setup.execute('PRAGMA journal_mode=WAL')
    with sqlite3.connect(store.path) as writer:
        writer.execute('BEGIN IMMEDIATE')
        start = time.monotonic()
        # Minimal service shell: this operation must depend only on the store.
        service = object.__new__(QueueService)
        service.store = store
        assert service.pending_counts() == {'lane-a': 1}
        assert time.monotonic() - start < .2
        writer.rollback()


def test_lane_inventory_is_read_only_under_writer(tmp_path):
    store = QueueStore(tmp_path / 'queue.db')
    store.append_tasks('lane-a', [{'prompt': 'preserve'}])
    expected = store.list_all_lanes()
    with sqlite3.connect(store.path) as setup:
        setup.execute('PRAGMA journal_mode=WAL')
    with sqlite3.connect(store.path) as writer:
        writer.execute('BEGIN IMMEDIATE')
        assert store.list_all_lanes() == expected
        writer.rollback()


def test_fleet_rejects_excess_without_queue_and_recovers():
    from terminal_mcp.read_fanout import ReadFanout
    gate = ReadFanout(limit=2, per_node=1)
    release = threading.Event()
    try:
        first = gate.submit('scope', 'sessions', 'a', lambda: release.wait(1))
        gate.submit('scope', 'sessions', 'b', lambda: release.wait(1))
        import pytest
        with pytest.raises(TimeoutError):
            gate.submit('scope', 'sessions', 'c', lambda: 1)
        with pytest.raises(TimeoutError):
            gate.submit('scope', 'health', 'a', lambda: 1)
        assert gate.snapshot()['queued'] == 0
        release.set()
        first.result(timeout=1)
        assert gate.submit('scope', 'sessions', 'a', lambda: 'ok').result(timeout=1) == 'ok'
    finally:
        release.set()
        gate._executor.shutdown(wait=True)
