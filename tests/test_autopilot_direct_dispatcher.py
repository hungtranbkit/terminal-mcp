"""The production adapter: autopilot -> DirectTaskDispatcher -> the REAL
DirectTaskSupervisor (fake pane only). Proves the live path keeps its own
contracts when driven by the autopilot: a submit receipt is not RUNNING, an
IDLE turn is continued rather than completed, only an explicit marker is DONE,
and a re-dispatch with the same key is deduplicated rather than resent."""
from __future__ import annotations

from terminal_mcp import autopilot as ap
from terminal_mcp.direct_task import DirectTaskStore, DirectTaskSupervisor
from terminal_mcp.queue_store import COMPLETED, DISPATCHING, RUNNING, VERIFYING

from .test_autopilot import PROJECT, World
from .test_direct_task_lifecycle import Clock, FakePane


def _sup(tmp_path, pane, clock):
    return DirectTaskSupervisor(DirectTaskStore(tmp_path / "direct.db"), send=pane.send,
                                status=pane.status, tail=pane.tail, clock=clock, settle_seconds=5)


def _drive(sup, clock, ticks):
    for _ in range(ticks):
        clock.now += 3.0
        sup.tick()


def test_live_adapter_runs_a_then_b_only_on_explicit_done(tmp_path):
    clock = Clock()
    pane = FakePane(busy_ticks=1)
    sup = _sup(tmp_path, pane, clock)
    w = World(tmp_path)
    w.dispatcher = ap.DirectTaskDispatcher(sup, max_continuations=3)
    a = w.add("ba-work-1", "A")
    b = w.add("ba-work-1", "B", depends_on=[a])

    w.autopilot().tick(PROJECT)
    assert w.status(a) == DISPATCHING and len(pane.sent) == 1   # receipt only

    _drive(sup, clock, 2)                     # agent shows a spinner: observed execution
    w.autopilot().tick(PROJECT)
    assert w.status(a) == RUNNING

    _drive(sup, clock, 6)                     # turn ends IDLE -> continuation, never DONE
    w.autopilot().tick(PROJECT)
    assert w.status(a) == RUNNING and len(pane.sent) >= 2

    dt_id = sup.store.by_idempotency_key(w.store.get_task(a).dispatch_idempotency_key)["task_id"]
    pane.output.append(f"● TMCP-DONE:{dt_id}")
    _drive(sup, clock, 2)
    w.autopilot().tick(PROJECT)
    assert w.status(a) == VERIFYING

    w.qa(a)
    assert w.status(a) == COMPLETED
    w.autopilot().tick(PROJECT)
    assert w.status(b) == DISPATCHING


def test_live_adapter_dedups_same_key_and_maps_unknown_key(tmp_path):
    clock = Clock()
    pane = FakePane()
    sup = _sup(tmp_path, pane, clock)
    adapter = ap.DirectTaskDispatcher(sup)
    assert adapter.observe(idempotency_key="autopilot:x:never").state == ap.NOT_FOUND
    first = adapter.dispatch(target="s", text="Implement A.", idempotency_key="autopilot:x:1")
    second = adapter.dispatch(target="s", text="Implement A.", idempotency_key="autopilot:x:1")
    assert first.state == ap.PENDING and second.state == ap.PENDING
    assert len(pane.sent) == 1


def test_live_adapter_refuses_busy_target_without_sending(tmp_path):
    clock = Clock()
    pane = FakePane()
    sup = _sup(tmp_path, pane, clock)
    sup.start("s", text="someone else's supervised task", idempotency_key="other")
    adapter = ap.DirectTaskDispatcher(sup)
    result = adapter.dispatch(target="s", text="Implement A.", idempotency_key="autopilot:x:1")
    assert result.state == ap.REFUSED and len(pane.sent) == 1
    assert adapter.observe(idempotency_key="autopilot:x:1").state == ap.NOT_FOUND
