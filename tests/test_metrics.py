"""Final audit pass item #12: the internal metrics registry (metrics.py)
itself -- thread safety of the counter store, the fixed/complete counter
set, and record_delivery_outcome's classification of a response dict.
"""
from __future__ import annotations

import threading

from terminal_mcp import metrics
from terminal_mcp.metrics import COUNTER_NAMES, MetricsRegistry, record_delivery_outcome


def test_snapshot_reports_every_known_counter_at_zero_by_default():
    registry = MetricsRegistry()
    snapshot = registry.snapshot()
    assert set(snapshot) == set(COUNTER_NAMES)
    assert all(value == 0 for value in snapshot.values())


def test_increment_updates_only_the_named_counter():
    registry = MetricsRegistry()
    registry.increment("delivery.text_sent")
    registry.increment("delivery.text_sent")
    registry.increment("delivery.error", amount=3)
    snapshot = registry.snapshot()
    assert snapshot["delivery.text_sent"] == 2
    assert snapshot["delivery.error"] == 3
    assert all(v == 0 for k, v in snapshot.items() if k not in ("delivery.text_sent", "delivery.error"))


def test_snapshot_returns_a_copy_not_a_live_view():
    registry = MetricsRegistry()
    snapshot = registry.snapshot()
    registry.increment("delivery.text_sent")
    assert snapshot["delivery.text_sent"] == 0  # the earlier snapshot must not have mutated


def test_concurrent_increments_are_not_lost():
    registry = MetricsRegistry()
    threads_count, increments_per_thread = 16, 200

    def _worker():
        for _ in range(increments_per_thread):
            registry.increment("delivery.text_sent")

    threads = [threading.Thread(target=_worker) for _ in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert registry.snapshot()["delivery.text_sent"] == threads_count * increments_per_thread


def test_module_level_increment_and_snapshot_share_one_registry(monkeypatch):
    fresh = MetricsRegistry()
    monkeypatch.setattr(metrics, "REGISTRY", fresh)
    metrics.increment("delivery.blocked")
    assert metrics.snapshot()["delivery.blocked"] == 1


def test_record_delivery_outcome_counts_identity_mismatch_by_either_error_code(monkeypatch):
    for error_code in ("IDENTITY_CHANGED_MID_SEND", "IDENTITY_MISMATCH"):
        fresh = MetricsRegistry()
        monkeypatch.setattr(metrics, "REGISTRY", fresh)
        record_delivery_outcome({"error": error_code})
        assert fresh.snapshot()["delivery.identity_mismatch"] == 1


def test_record_delivery_outcome_counts_pane_in_copy_mode_and_pane_busy(monkeypatch):
    fresh = MetricsRegistry()
    monkeypatch.setattr(metrics, "REGISTRY", fresh)
    record_delivery_outcome({"error": "PANE_IN_COPY_MODE"})
    record_delivery_outcome({"error": "PANE_BUSY"})
    snapshot = metrics.snapshot()
    assert snapshot["delivery.pane_in_copy_mode"] == 1
    assert snapshot["delivery.pane_busy"] == 1


def test_record_delivery_outcome_counts_each_delivery_state_exactly_once(monkeypatch):
    fresh = MetricsRegistry()
    monkeypatch.setattr(metrics, "REGISTRY", fresh)
    states = {
        "TEXT_SENT": "delivery.text_sent",
        "SUBMIT_CONFIRMED": "delivery.submit_confirmed",
        "DELIVERY_UNKNOWN": "delivery.delivery_unknown",
        "BLOCKED": "delivery.blocked",
        "ERROR": "delivery.error",
    }
    for delivery_state, counter_name in states.items():
        record_delivery_outcome({"delivery_state": delivery_state})
    snapshot = metrics.snapshot()
    for counter_name in states.values():
        assert snapshot[counter_name] == 1


def test_record_delivery_outcome_counts_recovery_attempted_independently_of_state(monkeypatch):
    fresh = MetricsRegistry()
    monkeypatch.setattr(metrics, "REGISTRY", fresh)
    record_delivery_outcome({"delivery_state": "SUBMIT_CONFIRMED", "recovery_attempted": True})
    snapshot = metrics.snapshot()
    assert snapshot["delivery.submit_confirmed"] == 1
    assert snapshot["delivery.recovery_attempted"] == 1


def test_record_delivery_outcome_ignores_an_unrelated_or_empty_response(monkeypatch):
    fresh = MetricsRegistry()
    monkeypatch.setattr(metrics, "REGISTRY", fresh)
    record_delivery_outcome({})
    record_delivery_outcome({"error": "SOME_OTHER_ERROR", "delivery_state": None})
    assert metrics.snapshot() == dict.fromkeys(COUNTER_NAMES, 0)
