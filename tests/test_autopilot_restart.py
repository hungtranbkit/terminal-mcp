"""Restart safety of the bounded repair step (issue #106).

Scheduling a repair is two durable writes: the queue-owned repair record
(`repairs`, `repair_of_job`) and retry_task (FAILED -> QUEUED). A process
death between them must neither lose the repair reference (a resend with no
pointer to the failed QA job) nor hand out an uncounted repair (a crash
loop that bypasses max_repair_attempts). Whichever write happens first, a
restarted coordinator must count the repair exactly once.
"""
from __future__ import annotations

import pytest

from terminal_mcp import autopilot as ap
from terminal_mcp.queue_store import FAILED, QUEUED

from .test_autopilot import PROJECT, SimulatedCrash, World

REPAIR_STEPS = ("retry_task", "set_internal_metadata")


def _crash_after_first_repair_write(store):
    """Let the first durable write of the repair step commit, then die."""
    originals = {name: getattr(store, name) for name in REPAIR_STEPS}

    def wrap(name):
        def call(*args, **kwargs):
            result = originals[name](*args, **kwargs)
            if name == "retry_task" or kwargs.get("event_type") == "AUTOPILOT_REPAIR_SCHEDULED":
                for restore, fn in originals.items():
                    setattr(store, restore, fn)
                raise SimulatedCrash()
            return result
        return call

    for name in REPAIR_STEPS:
        setattr(store, name, wrap(name))


def _to_first_qa_failure(w):
    a = w.add("ba-work-1", "A")
    w.autopilot().tick(PROJECT)
    w.dispatcher.set_state("ba-work-1", ap.DONE)
    w.autopilot().tick(PROJECT)
    job = w.qa(a, passed=False)
    assert w.status(a) == FAILED
    return a, job


def _restart(w, name):
    w.locks.force_release(PROJECT, ap.COORDINATOR_RESOURCE, actor="test", reason="died")
    return w.autopilot(name)


def test_crash_inside_repair_step_counts_the_repair_once_and_keeps_the_reference(tmp_path):
    w = World(tmp_path, max_repairs=1)
    a, job = _to_first_qa_failure(w)
    _crash_after_first_repair_write(w.store)
    with pytest.raises(SimulatedCrash):
        w.autopilot("hp-1").tick(PROJECT)

    restarted = _restart(w, "hp-1-restarted")
    restarted.tick(PROJECT)
    restarted.tick(PROJECT)
    meta = w.store.get_task(a).metadata["autopilot"]
    assert meta["repairs"] == 1 and meta["repair_of_job"] == job.id
    repair_sends = [text for target, text, _ in w.dispatcher.sends if job.id in text]
    assert len(repair_sends) == 1  # resent once, and that send names the failed QA job
    assert len(w.dispatcher.sends) == 2


def test_crash_loop_cannot_buy_repairs_past_the_budget(tmp_path):
    w = World(tmp_path, max_repairs=1)
    a, job = _to_first_qa_failure(w)
    _crash_after_first_repair_write(w.store)
    with pytest.raises(SimulatedCrash):
        w.autopilot("hp-1").tick(PROJECT)
    _restart(w, "hp-2").tick(PROJECT)

    # The one allowed repair fails QA again: the budget is spent.
    w.dispatcher.records[w.dispatcher.sends[-1][2]]["state"] = ap.DONE
    w.autopilot("hp-2").tick(PROJECT)
    w.qa(a, passed=False)
    _crash_after_first_repair_write(w.store)  # would crash if it tried another repair
    report = w.autopilot("hp-2").tick(PROJECT)
    assert w.status(a) == FAILED and report["repairs"] == []
    assert any(i["reason"] == ap.REASON_REPAIR_EXHAUSTED for i in report["owner_attention"])
    assert len(w.dispatcher.sends) == 2


def test_repair_record_written_but_retry_refused_is_reported_not_retried_blind(tmp_path):
    w = World(tmp_path, max_repairs=2)
    a, job = _to_first_qa_failure(w)

    def refuse(task_id, **kw):
        raise RuntimeError("harness run owns this task")

    w.store.retry_task = refuse
    report = w.autopilot().tick(PROJECT)
    assert w.status(a) == FAILED
    assert any(i["reason"] == ap.REASON_RETRY_REFUSED for i in report["owner_attention"])
    report = w.autopilot().tick(PROJECT)
    assert w.store.get_task(a).metadata["autopilot"].get("repairs", 0) <= 1
    assert len(w.dispatcher.sends) == 1


def test_retry_after_restart_is_dispatched_once(tmp_path):
    w = World(tmp_path, max_repairs=2)
    a, job = _to_first_qa_failure(w)
    report = w.autopilot().tick(PROJECT)
    assert report["repairs"] == [a]
    for name in ("hp-a", "hp-b"):
        _restart(w, name).tick(PROJECT)
    assert w.status(a) != QUEUED
    assert sum(1 for _, text, _ in w.dispatcher.sends if job.id in text) == 1
