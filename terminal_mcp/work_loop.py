"""The Work coordinator tick.

WHAT THIS DOES NOT DO

It does not dispatch. `queue_engine.py` already owns that, with the guarded
transactional send, the idempotency key and the reconciliation that stops a
prompt being sent twice. Adding a second dispatcher would reintroduce exactly
the race that machinery exists to prevent.

What this loop owns is the layer above: keeping a work RUN's state honest
against what the queue actually did, opening and clearing the gates, and --
the one thing with teeth -- deciding which lanes are allowed to run
automatically at all.

THE ONE PRIVILEGED ACTION

`auto_dispatch_enabled` defaults to False on every queue lane. That is the
existing safety posture and this loop does not weaken it: it turns
auto-dispatch ON only for a lane that is a `-work` session, belongs to an
active work run, and passes eligibility. It never enables it anywhere else,
and a test asserts that directly, because this is the single place where a
bug would put an autonomous agent in front of a human's terminal.

Turning it OFF is unconditional -- a paused or finished run stops its lane
regardless of anything else, since refusing to stop is a far worse failure
than refusing to start.

BOUNDED, LIKE ITS SIBLINGS

Same daemon-thread-with-a-stop-Event shape as MaintenanceLoop, RecoveryLoop
and FleetSyncLoop. Each tick does a fixed amount of work over the active
runs; nothing here is unbounded, retried tightly, or able to starve the
threads the rest of the process shares.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import work_store as ws
from .work_eligibility import evaluate as evaluate_eligibility, is_work_session

_LOGGER = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 20
DEFAULT_MAX_RUNS_PER_TICK = 25


@dataclass
class WorkLoopConfig:
    # OFF by default, unlike the fleet refresh loop. That difference is
    # deliberate: the fleet loop only reads and re-projects local data, while
    # this one can cause an agent to be handed work. A capability that acts
    # on its own starts disabled.
    enabled: bool = False
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    max_runs_per_tick: int = DEFAULT_MAX_RUNS_PER_TICK
    # Auto-enabling a `-work` lane's dispatch is the privileged action; a
    # deployment can keep the coordinator's bookkeeping while leaving the
    # actual enabling to a human.
    auto_enable_dispatch: bool = True


class WorkCoordinatorLoop:
    def __init__(self, *, service: Any, config: WorkLoopConfig,
                 evidence: Callable[[], dict[str, Any]] | None = None) -> None:
        self.service = service
        self._config = config
        # Sessions/nodes/statuses the eligibility gate needs. Injected because
        # the caller has already listed them for its own reasons and a
        # second fan-out per tick would be pure cost.
        self._evidence = evidence or (lambda: {"sessions": [], "nodes": {}, "statuses": {}})
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._ticks = 0
        self._last_tick_at: float | None = None
        self._last_error: str | None = None
        self._last_result: dict[str, Any] = {}

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if not self._config.enabled:
            _LOGGER.info("work coordinator disabled by config")
            return
        self._thread = threading.Thread(target=self._run, name="terminal-mcp-work",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 -- a dead loop is worse than a bad tick
                _LOGGER.exception("work coordinator: tick failed")
            self._stop_event.wait(self._config.interval_seconds)

    # -- one tick -------------------------------------------------------------

    def tick(self) -> dict[str, Any]:
        """One bounded pass over the active runs. Never raises."""
        self._ticks += 1
        result: dict[str, Any] = {"tick": self._ticks, "runs": [], "errors": [],
                                  "lanes_enabled": [], "lanes_disabled": []}
        try:
            evidence = self._evidence()
        except Exception as exc:  # noqa: BLE001
            evidence = {"sessions": [], "nodes": {}, "statuses": {}}
            result["errors"].append(f"evidence:{type(exc).__name__}")

        try:
            runs = self.service.store.list_runs(include_terminal=False,
                                                limit=self._config.max_runs_per_tick)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"list_runs:{type(exc).__name__}")
            runs = []

        for run in runs:
            try:
                result["runs"].append(self._advance(run, evidence, result))
            except Exception as exc:  # noqa: BLE001 -- one bad run never stops the rest
                result["errors"].append(f"{run.work_id}:{type(exc).__name__}")
                _LOGGER.exception("work coordinator: run %s failed", run.work_id)

        self._last_result = result
        self._last_tick_at = time.time()
        self._last_error = result["errors"][0] if result["errors"] else None
        return result

    def _advance(self, run: ws.WorkRun, evidence: dict[str, Any],
                 result: dict[str, Any]) -> dict[str, Any]:
        """Reconcile one run against what the queue actually did."""
        report: dict[str, Any] = {"work_id": run.work_id, "state": run.state,
                                  "actions": []}
        contract = self.service.evaluate_contract(run.work_id)
        report["progress"] = contract.get("progress")

        should_run = run.state in (ws.READY, ws.RUNNING, ws.VERIFYING)
        verdict = self._lane_verdict(run, evidence)
        report["eligibility"] = verdict

        # The privileged action, and the only one that can hand an agent work.
        if should_run and self._config.auto_enable_dispatch and verdict.get("eligible"):
            if self._set_dispatch(run, True):
                result["lanes_enabled"].append(run.lane)
                report["actions"].append("dispatch_enabled")
        elif not should_run:
            # Unconditional: a paused or finished run stops its lane
            # regardless of anything else. Refusing to stop is far worse than
            # refusing to start.
            if self._set_dispatch(run, False):
                result["lanes_disabled"].append(run.lane)
                report["actions"].append("dispatch_disabled")

        # READY -> RUNNING once the queue actually has work moving.
        if run.state == ws.READY and (contract.get("progress") or {}).get("total_tasks"):
            self._try_transition(run.work_id, ws.RUNNING, actor="coordinator")
            report["actions"].append("running")

        # The contract decides completion -- never a worker's own claim.
        if contract.get("satisfied") and run.state in (ws.RUNNING, ws.VERIFYING):
            if self._try_transition(run.work_id, ws.COMPLETE, actor="coordinator"):
                self.service.store.record_event(
                    run.work_id, kind="contract_satisfied",
                    summary="every required task complete, nothing blocked, no gate open",
                    actor="coordinator")
                self._set_dispatch(run, False)
                report["actions"].append("complete")

        # A blocked task surfaces as a blocked RUN rather than silently
        # stalling: a run that is going nowhere must say so.
        blocked = [u for u in contract.get("unmet", []) if u["reason"] == "TASK_BLOCKED"]
        if blocked and run.state == ws.RUNNING:
            if self._try_transition(run.work_id, ws.BLOCKED, actor="coordinator",
                                    reason=f"{len(blocked)} task(s) blocked"):
                report["actions"].append("blocked")

        report["state"] = (self.service.store.get_run(run.work_id) or run).state
        return report

    def _lane_verdict(self, run: ws.WorkRun, evidence: dict[str, Any]) -> dict[str, Any]:
        if not run.lane:
            return {"eligible": False, "reason": "NO_LANE", "detail": "run has no lane"}
        sessions = {s.get("name"): s for s in (evidence.get("sessions") or [])}
        row = sessions.get(run.lane, {})
        node_id = row.get("node_id")
        return evaluate_eligibility(
            run.lane,
            status=(evidence.get("statuses") or {}).get(run.lane, {"exists": True}
                                                        if row else None),
            node=(evidence.get("nodes") or {}).get(node_id) if node_id else None,
            input_allowed=row.get("input_allowed"),
            input_denied_reason=row.get("input_denied_reason")).as_dict()

    def _set_dispatch(self, run: ws.WorkRun, enabled: bool) -> bool:
        """Enable/disable automatic dispatch on a run's lane.

        The `is_work_session` check is repeated here rather than trusted to
        the caller. This is the one function in the whole runtime that can
        cause an autonomous agent to be handed a human's terminal, and a
        second check costs nothing next to that.
        """
        queue = getattr(self.service, "queue", None)
        if queue is None or not run.lane:
            return False
        if enabled and not is_work_session(run.lane):
            _LOGGER.error("work coordinator: refusing to auto-enable dispatch on %r, "
                          "which is not a -work session", run.lane)
            return False
        setter = getattr(queue, "set_auto_dispatch", None)
        if setter is None:
            return False
        try:
            current = queue.status(run.lane)
            if bool(current.get("auto_dispatch_enabled")) == enabled:
                return False
            setter(run.lane, enabled)
        except Exception:  # noqa: BLE001 -- never fatal to a tick
            _LOGGER.exception("work coordinator: could not set dispatch on %s", run.lane)
            return False
        self.service.store.record_event(
            run.work_id, kind="dispatch_toggled",
            summary=f"auto-dispatch {'enabled' if enabled else 'disabled'} on {run.lane}",
            actor="coordinator")
        return True

    def _try_transition(self, work_id: str, state: str, *, actor: str,
                        reason: str | None = None) -> bool:
        try:
            self.service.store.transition_run(work_id, state, actor=actor, reason=reason)
            return True
        except ws.WorkError:
            # An invalid edge here means another actor moved the run first.
            # That is normal concurrency, not an error worth logging loudly.
            return False

    # -- introspection ---------------------------------------------------------

    def status(self) -> dict[str, Any]:
        age = (time.time() - self._last_tick_at) if self._last_tick_at else None
        return {"enabled": self._config.enabled, "running": self.is_alive(),
                "interval_seconds": self._config.interval_seconds,
                "auto_enable_dispatch": self._config.auto_enable_dispatch,
                "ticks": self._ticks, "last_tick_at": self._last_tick_at,
                "age_seconds": round(age, 1) if age is not None else None,
                "last_error": self._last_error, "last_result": self._last_result}
