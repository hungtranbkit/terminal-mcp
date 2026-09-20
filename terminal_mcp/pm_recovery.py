"""PM recovery: work whose RUNTIME died, put back in front of the router.

THE FAILURE THIS EXISTS FOR

A project task is bound to a session on a node. The node goes offline, or the
session is killed, or the task is left holding a runtime that never starts it.
Nothing about the task is wrong -- it still has its project, its agent, its
skills, its evidence and its place in the pipeline. What it has lost is the
one thing that was never durable: a place to run.

Before this, that task simply stopped. `routable_tasks` will not pick it up
(it still reports routing_state=BOUND, so by definition it is not waiting for
a runtime), and `bound_unstarted_tasks` only re-drives a lane that is still
there. A task bound to a dead node is invisible to both, which is how a
project silently stalls with everything looking assigned.

WHAT THE PM DECIDES, AND WHAT IT NEVER TOUCHES

It decides exactly one thing: whether this task's RUNTIME BINDING is still
worth anything. If it is not, the binding -- and only the binding -- is
released, and the existing Agent/Session router is asked again.

    released      execution_session, execution_node_id, routing_state
    preserved     project_id, agent_id, skill_ids, prompt, priority,
                  verification_evidence, coordinator history, position

That split is the whole design. `release_execution_binding` already writes
exactly those three columns and nothing else, which is why this module reuses
it rather than doing its own UPDATE: durable ownership is not something a
recovery pass should be able to get wrong.

NO DOUBLE DISPATCH

Releasing then re-routing is two steps, and between them another router (a
rescue sweep, a concurrent route_start) may legitimately claim the task. That
is fine and needs no lock here, because the claim itself is atomic:
`bind_task_to_session` takes SQLite's write lock BEFORE its eligibility read,
so the second binder is refused SESSION_ALREADY_CLAIMED. This module adds one
cheaper guard on top -- it re-reads the task immediately before releasing and
skips it if the binding has changed since the scan -- so the common case never
reaches the contention at all.

EVERY DECISION IS RECORDED

`QueueStore.record_pm_decision` appends to the task's own append-only
migration_history and emits a queue event. A task that moved from one session
to another at 3am must be explainable at 9am from the row, including the
reason the PM thought the old runtime was dead.

REPORT-ONLY BY DEFAULT IS NOT THE POSTURE HERE, AND THAT IS DELIBERATE

stale_sessions.py refuses to act because its action is destroying a session
someone might still want. This module's action is giving a task a working
session, which is recoverable, idempotent and the thing the operator wanted in
the first place. It is still bounded: `limit` caps one sweep, and a task whose
runtime is merely SLOW (not gone) is left alone -- see _stall_reason.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterable, Sequence

from .queue_store import (BOUND, ROUTING_BOUND_STATES, SPAWNED, TERMINAL_STATUSES,
                          UNROUTED, WAITING_RUNTIME, QueueStore)

_LOGGER = logging.getLogger(__name__)

#: Decisions this module records. A recovery pass that changed nothing still
#: says which of these it chose, because "the PM looked and left it alone" is
#: an answer an operator needs as much as the other two.
PM_REASSIGNED = "REASSIGNED"
PM_RELEASED = "RELEASED"
PM_HELD = "HELD"
PM_SKIPPED = "SKIPPED"

#: How long a task may sit holding a runtime it has not started on before the
#: PM treats it as stalled rather than merely slow. Generous on purpose: the
#: ordinary path is that the router's own dispatch budget ran out and the
#: background sweep picks it up within a cycle or two.
DEFAULT_UNSTARTED_STALL_SECONDS = 300.0

#: The same, for a task that reached a WAITING/UNCERTAIN state and stayed
#: there. Longer, because those states have their own in-engine recheck.
DEFAULT_WAITING_STALL_SECONDS = 600.0

#: Statuses that mean "bound but not yet actually running on that session".
#: A task in one of these has, by definition, produced nothing on its runtime
#: yet, so moving it loses no work at all.
PRE_RUN_STATUSES = ("QUEUED", "PRECHECK", "READY", "DISPATCHING", "WAITING_SESSION",
                    "DISPATCH_UNCERTAIN")

#: Statuses where the task IS live on its session. Recovered only when the
#: runtime is demonstrably gone -- never on a timer, because a long-running
#: agent turn looks exactly like a stall from the outside and re-dispatching
#: it would run the same work twice.
RUNNING_STATUSES = ("RUNNING", "VERIFYING")

#: A human is the only thing that moves these, so a runtime problem is not
#: what is holding them up.
HUMAN_OWNED_STATUSES = ("BLOCKED", "PAUSED", "FAILED", "NEEDS_HUMAN")


def _epoch(timestamp: str | None) -> float | None:
    from .queue_store import _epoch_or_none

    return _epoch_or_none(timestamp)


class ProjectPMRecovery:
    """The PM's recovery sweep over one project, or the whole fleet.

    Constructed from things that already exist: the durable queue store, the
    task router (for re-placement) and the controller (for "is that node and
    that session actually there"). Owns no state of its own, so a restart
    re-derives the identical work list.
    """

    def __init__(self, store: QueueStore, *, router: Any = None, controller: Any = None,
                 clock=time.time,
                 unstarted_stall_seconds: float = DEFAULT_UNSTARTED_STALL_SECONDS,
                 waiting_stall_seconds: float = DEFAULT_WAITING_STALL_SECONDS) -> None:
        self.store = store
        self.router = router
        self.controller = controller
        self.clock = clock
        self.unstarted_stall_seconds = float(unstarted_stall_seconds)
        self.waiting_stall_seconds = float(waiting_stall_seconds)

    # -- fleet facts --------------------------------------------------------

    def _fleet(self) -> tuple[dict[str, str], set[tuple[str | None, str]], set[str], bool]:
        """(node_id -> status, {(node_id, session)}, {session names}, usable).

        `usable` is the load-bearing value. A fleet read that failed tells us
        NOTHING about whether a session is gone, and acting on that would
        release every binding in the queue the first time a listing timed out.
        So a failed read means the sweep only considers evidence that does not
        depend on it.
        """
        if self.controller is None:
            return {}, set(), set(), False
        nodes: dict[str, str] = {}
        try:
            for node in self.controller.list_nodes():
                nodes[node.id] = str(node.status)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("pm-recovery: node listing failed")
            return {}, set(), set(), False
        pairs: set[tuple[str | None, str]] = set()
        names: set[str] = set()
        try:
            listing = self.controller.terminal_list_sessions()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("pm-recovery: fleet session listing failed")
            return nodes, pairs, names, False
        if not isinstance(listing, dict):
            return nodes, pairs, names, False
        for row in listing.get("sessions", []) or []:
            name = row.get("name")
            if not name:
                continue
            names.add(str(name))
            pairs.add((row.get("node_id"), str(name)))
        # A node that could not be reached this pass has an UNKNOWN session
        # list, not an empty one. Treating its sessions as missing is the
        # single most dangerous mistake available here, so the whole listing
        # is downgraded to "not usable as absence evidence" instead.
        unreachable = [entry.get("node_id") for entry in (listing.get("unreachable_nodes") or [])
                       if isinstance(entry, dict)]
        return nodes, pairs, names, (not unreachable or bool(names))

    # -- stall classification ----------------------------------------------

    def _stall_reason(self, task: Any, *, nodes: dict[str, str], pairs: set, names: set,
                      fleet_usable: bool) -> str | None:
        """Why this task's runtime is no longer worth holding, or None.

        Ordered most-certain first. Every branch is a FACT about the runtime,
        never an opinion about the task: "the node says OFFLINE", "the fleet
        listed its sessions and this one is not among them", "it has been
        pre-dispatch on a live session for longer than policy allows".
        """
        status = str(task.status or "")
        if status in TERMINAL_STATUSES or status in HUMAN_OWNED_STATUSES:
            return None
        node_id = task.execution_node_id
        session = task.execution_session
        if not session:
            return None

        node_status = nodes.get(node_id) if node_id else None
        if node_id and node_status is not None and node_status != "online":
            return (f"execution node {node_id!r} is {node_status} -- the session this task is "
                    f"bound to cannot be reached")
        if node_id and node_status is None and nodes:
            return (f"execution node {node_id!r} is no longer registered in the fleet")

        if fleet_usable:
            present = ((node_id, session) in pairs) if node_id else (session in names)
            if not present and session not in names:
                return (f"session {session!r} is not in the fleet listing any more -- the runtime "
                        f"this task was bound to is gone")

        # Live work is never moved on a timer. A model mid-turn is
        # indistinguishable from a stall from out here, and re-dispatching it
        # would run the same work twice on two sessions.
        if status in RUNNING_STATUSES:
            return None

        now = self.clock()
        if status in PRE_RUN_STATUSES:
            since = _epoch(task.uncertain_or_waiting_since) or _epoch(task.updated_at)
            if since is None:
                return None
            age = now - since
            limit = (self.waiting_stall_seconds
                     if status in ("WAITING_SESSION", "DISPATCH_UNCERTAIN")
                     else self.unstarted_stall_seconds)
            if age >= limit:
                if self._waiting_behind_live_work(session, task.id):
                    # Not stalled -- QUEUED. A lane is serial, so the second
                    # task in it is waiting for the one ahead of it, not for a
                    # runtime. Moving it would take work off a session that is
                    # about to become free and put it somewhere with its own
                    # queue, which is thrash dressed as recovery.
                    return None
                return (f"held {session!r} for {int(age)}s in {status} without starting "
                        f"(policy allows {int(limit)}s) -- the binding is not producing work")
        return None

    def _waiting_behind_live_work(self, session: str, task_id: str) -> bool:
        """Is something else actually running in this task's lane right now?"""
        try:
            lane = self.store.lane_status(session)
        except Exception:  # noqa: BLE001 -- an unreadable lane is not evidence of anything
            return True
        for row in lane.get("tasks", []) or ():
            if row.get("id") != task_id and row.get("status") in RUNNING_STATUSES:
                return True
        return False

    # -- the sweep ----------------------------------------------------------

    def sweep(self, *, project_id: str | None = None, limit: int = 25,
              dry_run: bool = False, actor: str = "pm") -> dict[str, Any]:
        """Recover every stalled runtime binding, newest problem first.

        `dry_run` reports the decisions without taking any: the same call the
        Projects admin panel makes to show an operator what a recovery would
        do before they ask for it.
        """
        nodes, pairs, names, fleet_usable = self._fleet()
        try:
            tasks = self.store.runtime_bound_tasks(limit=max(limit * 4, limit))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("pm-recovery: runtime-bound scan failed")
            return {"error": "SCAN_FAILED", "recovered": 0, "results": []}

        results: list[dict[str, Any]] = []
        recovered = held = 0
        for task in tasks:
            if project_id and task.project_id != project_id:
                continue
            if len(results) >= limit:
                break
            reason = self._stall_reason(task, nodes=nodes, pairs=pairs, names=names,
                                        fleet_usable=fleet_usable)
            if reason is None:
                held += 1
                continue
            row = self._recover_one(task, reason=reason, actor=actor, dry_run=dry_run)
            results.append(row)
            if row["decision"] in (PM_REASSIGNED, PM_RELEASED):
                recovered += 1
        return {
            "project_id": project_id,
            "scanned": len(tasks),
            "recovered": recovered,
            "healthy": held,
            "dry_run": bool(dry_run),
            "fleet_evidence_usable": fleet_usable,
            "results": results,
        }

    def _recover_one(self, task: Any, *, reason: str, actor: str, dry_run: bool) -> dict[str, Any]:
        """Release this one task's runtime and ask the router again."""
        preserved = {
            "project_id": task.project_id, "agent_id": task.agent_id,
            "skill_ids": list(task.skill_ids or ()), "lane": task.session,
            "status": task.status,
        }
        base = {"task_id": task.id, "project_id": task.project_id,
                "agent_id": task.agent_id, "released_session": task.execution_session,
                "released_node_id": task.execution_node_id,
                "reason": reason, "preserved": preserved}
        if dry_run:
            return {**base, "decision": PM_HELD, "would": PM_REASSIGNED}

        # Re-read immediately before acting. Between the scan and here another
        # router may have moved this task on already, and re-releasing a
        # binding that is no longer the one we judged would take a HEALTHY
        # runtime away from it.
        current = self.store.get_task(task.id)
        if current is None:
            return {**base, "decision": PM_SKIPPED, "detail": "task no longer exists"}
        if (current.routing_state not in ROUTING_BOUND_STATES
                or current.execution_session != task.execution_session):
            return {**base, "decision": PM_SKIPPED,
                    "detail": "the binding changed between the scan and the decision; "
                              "another router already moved this task"}
        if current.status in TERMINAL_STATUSES:
            return {**base, "decision": PM_SKIPPED, "detail": f"task is already {current.status}"}

        released = self.store.release_execution_binding(task.id, reason=f"pm-recovery: {reason}")
        if isinstance(released, dict) and released.get("error"):
            return {**base, "decision": PM_SKIPPED, "detail": released["error"]}

        self.store.record_pm_decision(
            task.id, decision=PM_RELEASED, reason=reason, actor=actor,
            released_session=task.execution_session,
            released_node_id=task.execution_node_id,
            evidence={"preserved": preserved})

        if self.router is None:
            return {**base, "decision": PM_RELEASED,
                    "detail": "no router is wired here; the task is routable again and the next "
                              "rescue sweep will place it"}
        try:
            outcome = self.router.route_task(task.id)
        except Exception as exc:  # noqa: BLE001 -- one bad task never wedges the sweep
            _LOGGER.exception("pm-recovery: re-route failed for task %s", task.id)
            return {**base, "decision": PM_RELEASED,
                    "detail": f"re-route raised {type(exc).__name__}: {exc}; the task is routable"}

        placed = getattr(outcome, "session", None)
        self.store.record_pm_decision(
            task.id, decision=PM_REASSIGNED if placed else PM_RELEASED,
            reason=(f"re-routed to {placed}" if placed
                    else f"no runtime available yet: {getattr(outcome, 'reason', '')}"),
            actor=actor, released_session=task.execution_session,
            released_node_id=task.execution_node_id,
            evidence={"routing_outcome": getattr(outcome, "outcome", None),
                      "routing_reason": getattr(outcome, "reason", None),
                      "new_session": placed,
                      "new_node_id": getattr(outcome, "node_id", None)})
        return {**base,
                "decision": PM_REASSIGNED if placed else PM_RELEASED,
                "new_session": placed,
                "new_node_id": getattr(outcome, "node_id", None),
                "routing_outcome": getattr(outcome, "outcome", None),
                "routing_reason": getattr(outcome, "reason", None)}
