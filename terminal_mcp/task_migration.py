"""Task Migration / Load Balancing (task: "bổ sung Task Migration / Load
Balancing vào Queue/Coordinator" -- when one coding session's backlog
grows too large, runs slow, goes offline, or gets blocked, move its
still-QUEUED work to a healthier session in the SAME project, without
losing the task or its history).

Layering: queue_store.py owns the actual mutation (reassign_task) and
its race/restart safety (BEGIN IMMEDIATE + re-check, same pattern as
claim_next_task) -- this module is pure POLICY on top of it: computing
load, deciding what a "balanced" plan looks like, and enforcing the
eligibility/role/project/cooldown rules item 6/9/11 ask for. Same split
as coordinator.py (policy) vs queue_store.py (storage/mutation).

ROLE/PROJECT BOUNDARIES (item 11) are satisfied STRUCTURALLY, not by
any check in this module: Integration Agent work lives entirely in
integration_store.py's own Handoff table, a completely different store
this module never touches -- there is no code path here that could
ever move a coding QueueTask into "being" an Integration Agent handoff
or vice versa. Cross-PROJECT leakage is prevented by plan_rebalance
only ever grouping sessions that share the exact same
queue_lanes.project value (set via QueueStore.set_lane_project) --
None only matches None, so two unconfigured lanes are never silently
treated as the same project.
"""
from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from typing import Any

from .queue_store import TaskAlreadyClaimedError, QueueStore, QueueTask

DEFAULT_IMBALANCE_THRESHOLD = 2
"""plan_rebalance only proposes a move when the most-loaded eligible
session has at least this many more QUEUED tasks than the least-loaded
one -- avoids proposing/making a move for a 1-task difference that
would just ping-pong back next round."""

DEFAULT_COOLDOWN_SECONDS = 300.0
"""item 5's own hysteresis: a session touched by an APPLIED rebalance
(as either source or destination) is excluded from being a SOURCE again
until this many seconds have passed -- the actual anti-ping-pong
mechanism. Never blocks a MANUAL task_reassign (item 4) -- only auto-
rebalance planning respects this."""


def _age_seconds(iso_timestamp: str) -> float:
    # calendar.timegm, not time.mktime -- these timestamps are always
    # UTC; time.mktime wrongly assumes local time (a real bug found and
    # fixed in this same task -- see queue_store.py's identical fix).
    return time.time() - calendar.timegm(time.strptime(iso_timestamp, "%Y-%m-%dT%H:%M:%SZ"))


@dataclass(frozen=True)
class SessionLoad:
    session: str
    online: bool
    queued_depth: int
    oldest_queued_age_seconds: float
    has_active_task: bool
    current_task_runtime_seconds: float | None
    in_cooldown: bool

    def to_dict(self) -> dict[str, Any]:
        return {"session": self.session, "online": self.online, "queued_depth": self.queued_depth,
               "oldest_queued_age_seconds": self.oldest_queued_age_seconds,
               "has_active_task": self.has_active_task,
               "current_task_runtime_seconds": self.current_task_runtime_seconds, "in_cooldown": self.in_cooldown}


class TaskMigrationPlanner:
    def __init__(self, store: QueueStore, ops: Any, *, cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS) -> None:
        self.store = store
        self.ops = ops
        self.cooldown_seconds = cooldown_seconds

    def compute_load(self, session: str) -> SessionLoad:
        metrics = self.store.metrics(session)
        lane = self.store.lane_status(session)
        active = lane["current_task"]
        try:
            status_response = self.ops.terminal_status(session)
            online = not status_response.get("error")
        except Exception:  # noqa: BLE001 -- an unreachable node/session just means "offline", not a crash
            online = False
        runtime = None
        if active is not None and active["status"] == "RUNNING" and active.get("started_at"):
            runtime = _age_seconds(active["started_at"])
        last_rebalance_at = lane.get("last_rebalance_at")
        in_cooldown = bool(last_rebalance_at) and _age_seconds(last_rebalance_at) < self.cooldown_seconds
        return SessionLoad(
            session=session, online=online, queued_depth=metrics["queued_depth"],
            oldest_queued_age_seconds=metrics["oldest_queued_age_seconds"], has_active_task=active is not None,
            current_task_runtime_seconds=runtime, in_cooldown=in_cooldown,
        )

    def eligible_destination(self, task: QueueTask, destination_session: str) -> tuple[bool, str]:
        """item 6's own eligibility gate. Fail-closed: any unreadable
        status is INELIGIBLE, never assumed fine. Branch/worktree/node
        compatibility reuses the SAME expected_cwd/expected_node_id
        metadata fields coordinator.py's own CoordinatorGate already
        checks at review time (task item's own "đúng project/worktree/
        branch compatibility") -- a task with neither field declared is
        compatible with any online destination."""
        try:
            status_response = self.ops.terminal_status(destination_session)
        except Exception as exc:  # noqa: BLE001
            return False, f"could not read destination session status: {exc}"
        if status_response.get("error"):
            return False, f"destination session unreachable: {status_response['error']}"
        expected_cwd = (task.metadata or {}).get("expected_cwd")
        if expected_cwd and status_response.get("cwd") and \
                str(status_response["cwd"]).rstrip("/\\") != str(expected_cwd).rstrip("/\\"):
            return False, f"destination cwd does not match this task's expected worktree ({expected_cwd})"
        expected_node_id = (task.metadata or {}).get("expected_node_id")
        if expected_node_id and status_response.get("node_id") and status_response["node_id"] != expected_node_id:
            return False, (f"destination is on node {status_response.get('node_id')!r}, "
                          f"task expects {expected_node_id!r}")
        return True, "eligible"

    def plan_rebalance(self, project: str | None, sessions: list[str], *,
                       imbalance_threshold: int = DEFAULT_IMBALANCE_THRESHOLD,
                       respect_cooldown: bool = True) -> list[dict[str, Any]]:
        """Greedy plan: repeatedly move ONE QUEUED task from the most-
        loaded eligible session to the least-loaded eligible one, re-
        evaluating load after each simulated move, until the gap drops
        below imbalance_threshold or no more eligible tasks/sessions
        remain. `sessions` is the CALLER-supplied candidate set (task
        item 11: "không leak task sang project khác") -- this method
        additionally filters to only those whose own queue_lanes.project
        matches `project` exactly (None only matches None), as a second,
        structural safety net even if a caller's own candidate list was
        built carelessly."""
        same_project = [s for s in sessions if self.store.lane_status(s)["project"] == project]
        loads = {s: self.compute_load(s) for s in same_project}
        # A VIRTUAL per-session queue of still-unplanned QUEUED tasks --
        # this is what a real bug in an earlier version of this method
        # got wrong: re-querying the STORE for "the busiest session's
        # next QUEUED task" on every loop iteration always returns the
        # SAME task (nothing has actually moved yet, since a plan is
        # pure simulation), so a naive re-query would plan the SAME
        # task moving multiple times instead of picking a DIFFERENT one
        # each round. Popping from this in-memory list instead (and
        # appending onto the destination's own virtual list, so it can
        # later become a source in a longer chain) is what makes each
        # planned move refer to a genuinely different task."""
        virtual_queued: dict[str, list[dict[str, Any]]] = {
            s: [t for t in self.store.lane_status(s)["tasks"] if t["status"] == "QUEUED"] for s in same_project
        }
        plan: list[dict[str, Any]] = []
        # A session in cooldown is never a SOURCE (the actual ping-pong
        # risk); it may still be a valid DESTINATION.
        while True:
            sources = [s for s, load in loads.items()
                      if load.online and load.queued_depth > 0 and not (respect_cooldown and load.in_cooldown)]
            destinations = [s for s, load in loads.items() if load.online]
            if not sources or not destinations:
                break
            busiest = max(sources, key=lambda s: loads[s].queued_depth)
            quietest = min(destinations, key=lambda s: loads[s].queued_depth)
            if busiest == quietest or loads[busiest].queued_depth - loads[quietest].queued_depth < imbalance_threshold:
                break
            candidates = virtual_queued.get(busiest, [])
            if not candidates:
                break
            # Oldest QUEUED task first (position ASC is already the
            # lane's own stored order) -- never the newest, so a just-
            # enqueued task isn't the first thing yanked away. Walk the
            # virtual list looking for the first candidate that's
            # actually eligible for `quietest`; an ineligible one is
            # left in place (it may still be eligible for a DIFFERENT
            # destination on a later iteration) rather than reordered.
            task_row = None
            task = None
            for candidate_row in candidates:
                candidate_task = self.store.get_task(candidate_row["id"])
                ok, _reason = self.eligible_destination(candidate_task, quietest)
                if ok:
                    task_row, task = candidate_row, candidate_task
                    break
            if task_row is None:
                # Nothing in this source is eligible for this
                # destination -- this (busiest, quietest) pairing is
                # infeasible; stop rather than loop forever on it.
                break
            candidates.remove(task_row)
            virtual_queued.setdefault(quietest, []).append(task_row)
            plan.append({
                "task_id": task.id, "from_session": busiest, "to_session": quietest,
                "reason": f"rebalance: {busiest} had {loads[busiest].queued_depth} queued, "
                         f"{quietest} had {loads[quietest].queued_depth}",
            })
            # Simulate the move for the NEXT iteration's own load view.
            loads[busiest] = SessionLoad(busiest, loads[busiest].online, loads[busiest].queued_depth - 1,
                                        loads[busiest].oldest_queued_age_seconds, loads[busiest].has_active_task,
                                        loads[busiest].current_task_runtime_seconds, loads[busiest].in_cooldown)
            loads[quietest] = SessionLoad(quietest, loads[quietest].online, loads[quietest].queued_depth + 1,
                                         loads[quietest].oldest_queued_age_seconds, loads[quietest].has_active_task,
                                         loads[quietest].current_task_runtime_seconds, loads[quietest].in_cooldown)
        return plan

    def apply_plan(self, plan: list[dict[str, Any]], *, actor: str) -> list[dict[str, Any]]:
        """Applies a plan produced by plan_rebalance (or a hand-built
        one) task-by-task, via queue_store.py's own race-safe
        reassign_task. A task claimed by a dispatcher between planning
        and applying fails clean (TASK_ALREADY_CLAIMED) for THAT one
        move only -- every other move in the plan still applies."""
        results = []
        touched_sessions = set()
        for move in plan:
            try:
                self.store.reassign_task(move["task_id"], move["to_session"], reason=move["reason"], actor=actor)
                results.append({"task_id": move["task_id"], "status": "MIGRATED", "to_session": move["to_session"]})
                touched_sessions.add(move["from_session"])
                touched_sessions.add(move["to_session"])
            except (KeyError, TaskAlreadyClaimedError) as exc:
                results.append({"task_id": move["task_id"], "status": "FAILED", "reason": str(exc)})
        for session in touched_sessions:
            self.store.mark_rebalanced(session)
        return results
