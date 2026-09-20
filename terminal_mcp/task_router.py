"""Task -> profile -> eligible runtime -> dispatch. Queue only as a last resort.

THE PRODUCTION BUG THIS REPLACES

Durable task f807b3fb3d31438281d2cb4c8c6aea5a was cancelled after sitting
QUEUED while a compatible session sat IDLE the whole time. Nothing was broken
in the sense of throwing an error. The architecture simply had no step that
asked "is there a runtime that could be running this right now?" -- `session`
was chosen by whoever created the task, and if that choice was wrong, absent,
or pointed at a lane whose auto-dispatch opt-in was off, the task waited
forever and no screen could say why.

Three things had to change together, and all three live here:

  1. A task no longer has to name a session. `route_start` takes the work and
     finds the runtime, which is what makes generic and agent-owned work
     dispatchable at all.
  2. Queueing became an outcome that must be JUSTIFIED. Every path that leaves
     a task queued writes WAITING_RUNTIME plus the per-candidate rejection
     reasons, so "unexplained QUEUED" is no longer a representable state.
  3. A restart-safe reconcile re-asks the question on a cadence. A routing
     decision made when the fleet was full is wrong ten seconds later when a
     session goes idle, and nothing was re-deciding it.

WHAT THIS MODULE DELIBERATELY DOES NOT DO

It does not send anything. Dispatch is `QueueEngine.tick`, the existing path
with its coordinator gate, its idempotency key and its delivery-state
machinery. It does not invent state: the claim is `QueueStore.
bind_task_to_session`, the same BEGIN IMMEDIATE discipline `claim_next_task`
already uses. It does not choose blindly: session_matcher.py owns the hard
rejects and the arithmetic. This module is the sequencing between those, and
the honest recording of what happened.

EXPLICIT TARGETS ARE NEVER SECOND-GUESSED. `start(target=session)` is hard
affinity: a caller who named a session gets that session or a clear refusal,
never a silent reroute to somewhere the router liked better. Routing is what
happens when nobody named one.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from . import session_matcher as sm
from . import task_profile
from .compact_tools import (
    MAX_START_TICKS, START_NEEDS_HUMAN_STATUSES, START_SERVER_PENDING_STATUSES,
    START_UNDERWAY_STATUSES,
)
from .node_models import NODE_ONLINE
from .queue_store import (
    BOUND, QUEUED, SPAWNED, TERMINAL_STATUSES, UNROUTED, WAITING_RUNTIME, QueueStore,
)
from .session_matcher import SessionCandidate
from .task_profile import TaskProfile

_LOGGER = logging.getLogger(__name__)

#: A live probe is only worth doing if there is real time to do it in; below
#: this the router ranks on durable state instead of spending the dispatch
#: phase's clock.
MIN_PROBE_SECONDS = 0.2
#: No single candidate probe may take more than this, however much budget is
#: left -- three fast probes tell a router more than one slow one.
MAX_PROBE_SECONDS = 2.0

#: Outcomes of one routing attempt. Deliberately a small, closed vocabulary --
#: every one of these is shown to a human, so a new one is a UI decision, not
#: an implementation detail.
ROUTED = "ROUTED"
"""Bound to an existing session and handed to the engine."""
SPAWNED_RUNTIME = "SPAWNED"
"""Nothing eligible existed, so a compatible session was created for it."""
DEFERRED = "DEFERRED"
"""No runtime, and spawning was not possible or not allowed. The task stays
queued with WAITING_RUNTIME and the full rejection list."""
ALREADY_BOUND = "ALREADY_BOUND"
"""Someone else got there first, or this task already had a runtime. Not an
error -- an idempotent no-op, which is what a retried route should be."""
NOT_ROUTABLE = "NOT_ROUTABLE"
"""The task itself cannot be routed right now (settled, mid-dispatch, or
explicitly pinned to a session)."""


@dataclass
class RoutingOutcome:
    outcome: str
    task_id: str
    session: str | None = None
    node_id: str | None = None
    score: int | None = None
    reason: str = ""
    routing_state: str = UNROUTED
    dispatched: bool = False
    """TRUE ONLY WHEN THE ENGINE ACTUALLY ADVANCED THE TASK. Found live
    (hp-linux, 5cecd87): this was set from "tick() did not raise", so a task
    the engine had declined to claim came back dispatched=True while sitting
    QUEUED -- a receipt that lies in exactly the direction that makes an
    orchestrator stop watching."""
    dispatch_ticks: int = 0
    dispatch_detail: str | None = None
    budget_exhausted: bool = False
    """The CALLER's wait ran out, not the engine refusing. The distinction
    decides whether the binding is kept (the engine is mid-flight and the
    server carries on) or handed back (the engine will not take it)."""
    task_state: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome, "task_id": self.task_id, "session": self.session,
            "node_id": self.node_id, "score": self.score, "reason": self.reason,
            "routing_state": self.routing_state, "dispatched": self.dispatched,
            "dispatch_ticks": self.dispatch_ticks, "dispatch_detail": self.dispatch_detail,
            "budget_exhausted": self.budget_exhausted,
            "task_state": self.task_state, "routing_evidence": self.evidence,
        }


def _candidate_skills(record: Any) -> tuple[str, ...]:
    """Skills a session advertises, from the tags the registry already stores.

    `skill:<name>` tags, not a new column: a session's capabilities are an
    operator-facing label today, and inventing a parallel field would mean two
    places to keep in agreement for a Phase A that does not yet have a Skill
    Registry to be the authority."""
    tags = tuple(getattr(record, "tags", ()) or ())
    return tuple(tag.split(":", 1)[1] for tag in tags if tag.startswith("skill:"))


class TaskRouter:
    """Sequences profile -> match -> claim -> dispatch over the existing stores.

    Every collaborator is injected and every one of them is something that
    already existed. The router owns no state of its own beyond a short
    candidate cache, so a restart loses nothing that matters.
    """

    def __init__(self, store: QueueStore, *, controller: Any = None, queue: Any = None,
                 engine: Any = None, session_registry: Any = None,
                 config: Any = None, clock: Callable[[], float] = time.monotonic,
                 capacity_check: Callable[[Any], str | None] | None = None) -> None:
        self.store = store
        self.controller = controller
        self.queue = queue
        self.engine = engine
        self.session_registry = session_registry
        self.config = config
        self.clock = clock
        # Phase B: an owner-level admission rule, injected rather than
        # imported, so the router keeps knowing nothing about agents. Returns
        # a reason to defer, or None. It is consulted on EVERY routing path --
        # submission and rescue alike -- because a limit that only held on the
        # submission path would be silently bypassed by the next reconcile.
        self.capacity_check = capacity_check
        # Set for the duration of one route so the match and dispatch phases
        # share a single wall-clock ceiling. Never read across calls.
        self._deadline: float | None = None
        self._cache: tuple[float, list[SessionCandidate]] | None = None
        self._cache_ttl_seconds = 2.0
        # The last fleet snapshot that actually came back, kept separately
        # from the TTL cache above. When a fresh collect is refused by its own
        # budget or comes back empty because every node timed out, routing on
        # a snapshot from thirty seconds ago and saying so beats routing on
        # nothing at all -- an empty candidate list is indistinguishable from
        # "the fleet is empty" and defers every task in the queue.
        self._last_snapshot: tuple[float, list[SessionCandidate]] | None = None
        # One routing decision at a time in this process. The store's
        # BEGIN IMMEDIATE already makes the CLAIM safe across processes; this
        # only stops one process from spending N concurrent fleet listings to
        # reach the same answer, and keeps spawn decisions serialized so two
        # simultaneous routes cannot both decide the fleet needs one more
        # session.
        self._lock = threading.RLock()

    # -- policy ------------------------------------------------------------

    @property
    def _policy(self) -> Any:
        return getattr(self.config, "router", None)

    def _policy_value(self, name: str, default: Any) -> Any:
        policy = self._policy
        if policy is None:
            return default
        value = getattr(policy, name, None)
        return default if value is None else value

    @property
    def enabled(self) -> bool:
        return bool(self._policy_value("enabled", True))

    @property
    def spawn_enabled(self) -> bool:
        return bool(self._policy_value("spawn_enabled", False))

    @property
    def rescue_enabled(self) -> bool:
        return bool(self._policy_value("rescue_enabled", True))

    # -- candidate collection ----------------------------------------------

    @property
    def snapshot_budget_seconds(self) -> float:
        """How long ONE routing decision may spend looking at the fleet.

        Deliberately a fraction of the whole dispatch budget, not all of it.
        Found live on hp-linux (2026-09-20): project_start returned in 12.5s
        having burned its entire 12s budget on a serial fleet listing, so
        `dispatch_ticks` was 0 and the task came back TASK_ACCEPTED/QUEUED
        rather than started. Looking at the fleet is a means; starting the
        task is the end, and the end gets most of the clock."""
        return float(self._policy_value("snapshot_budget_seconds", 4.0))

    @property
    def stale_snapshot_max_age_seconds(self) -> float:
        """How old a fallback fleet snapshot may be before it is not evidence.

        Routing on a minute-old view risks picking a session that has since
        gone busy -- which the live probe in `rank` re-checks before we commit,
        and which `bind_task_to_session` refuses outright if another task got
        there first. Routing on NO view risks deferring the entire queue
        because one node was slow, which nothing downstream corrects."""
        return float(self._policy_value("stale_snapshot_max_age_seconds", 60.0))

    def candidates(self, *, refresh: bool = False,
                   budget_seconds: float | None = None) -> list[SessionCandidate]:
        """Every session in the fleet, described in routing terms.

        Cached for a couple of seconds. A rescue cycle routing eight backlog
        tasks must not perform eight identical fleet listings, and two seconds
        is short enough that a session going busy is noticed on the next cycle
        while the live probe in `rank` re-verifies whichever one we actually
        pick anyway.

        `budget_seconds` bounds the underlying fleet fan-out. When the fresh
        collect comes back empty AND a recent snapshot exists, the snapshot
        wins: see stale_snapshot_max_age_seconds."""
        if not refresh and self._cache is not None:
            cached_at, rows = self._cache
            if self.clock() - cached_at < self._cache_ttl_seconds:
                return rows
        rows = self._collect(budget_seconds=budget_seconds)
        now = self.clock()
        if not rows and self._last_snapshot is not None:
            taken_at, previous = self._last_snapshot
            if previous and now - taken_at < self.stale_snapshot_max_age_seconds:
                _LOGGER.warning(
                    "task-router: fleet listing returned nothing; routing on the "
                    "%.1fs-old snapshot of %d session(s) instead of deferring everything",
                    now - taken_at, len(previous))
                self._cache = (now, previous)
                return previous
        self._cache = (now, rows)
        if rows:
            self._last_snapshot = (now, rows)
        return rows

    def invalidate(self) -> None:
        self._cache = None

    def _collect(self, *, budget_seconds: float | None = None) -> list[SessionCandidate]:
        if self.controller is None:
            return []
        try:
            listing = self._list_sessions_bounded(budget_seconds)
        except Exception:  # noqa: BLE001 -- a fleet listing glitch must defer, never crash the loop
            _LOGGER.exception("task-router: fleet session listing failed")
            return []
        nodes = {}
        try:
            # terminal_list_sessions has already evaluated node health under the
            # same budget; this call is served from NodeHealthService's own
            # probe cache and costs no further network round trips.
            nodes = {node.id: node for node in self.controller.list_nodes()}
        except Exception:  # noqa: BLE001
            _LOGGER.exception("task-router: node listing failed; scoring without node health")

        records: dict[tuple[str | None, str], Any] = {}
        if self.session_registry is not None:
            try:
                for record in self.session_registry.list():
                    records[(record.node_id, record.session_name)] = record
            except Exception:  # noqa: BLE001
                _LOGGER.exception("task-router: session registry read failed")

        # ONE durable read for both derived views. These were two calls to
        # list_all_lanes(), which walks every lane and every task in it; on a
        # fleet with 78 sessions that is the same full scan performed twice
        # for one routing decision.
        load, claims = self._lane_load_and_claims()
        local_node_id = getattr(self.controller, "local_node_id", None)

        candidates: list[SessionCandidate] = []
        for row in listing.get("sessions", []) or []:
            name = row.get("name")
            if not name:
                continue
            node_id = row.get("node_id")
            node = nodes.get(node_id)
            record = records.get((node_id, name))
            active, queued = load.get(name, (0, 0))
            candidates.append(SessionCandidate(
                session=name,
                node_id=node_id,
                node_name=row.get("node_name") or (node.display_name if node else None),
                node_online=(node.status == NODE_ONLINE) if node else True,
                node_draining=bool(node.draining) if node else False,
                node_capacity=node.capacity_status if node else None,
                node_agent_types=tuple(node.agent_types) if node else (),
                is_local_node=bool(local_node_id) and node_id == local_node_id,
                runtime=getattr(record, "agent_type", None),
                state=getattr(record, "last_known_state", None),
                state_probed=False,
                # `effective_input` is the canonical "can this be typed into
                # right now" field -- the same one every send path checks.
                # Falling back to True when a node's build predates it keeps an
                # older node usable; the actual send still enforces its own gate.
                input_allowed=bool(row.get("effective_input", row.get("input_allowed", True))),
                stale_identity_pin=bool(row.get("stale_identity_pin")),
                registry_status=getattr(record, "status", None),
                cwd=getattr(record, "cwd", None),
                worktree_path=getattr(record, "worktree_path", None) or getattr(record, "repo_root", None),
                worktree_exists=None,
                repo=(getattr(record, "git_remote", None) or getattr(record, "repo_root", None)),
                branch=getattr(record, "git_branch", None),
                dirty=None,
                context_percent=None,
                project=None,
                project_id=None,
                agent_id=None,
                skills=_candidate_skills(record),
                bindings=tuple(getattr(record, "binding_names", ()) or ()),
                active_tasks=active,
                queued_tasks=queued,
                claimed_by_task=claims.get(name),
            ))
        # Worktree existence is resolved once, here, rather than inside the
        # scoring loop: it touches the filesystem, and scoring must stay pure.
        return [self._with_worktree(candidate) for candidate in candidates]

    @staticmethod
    def _with_worktree(candidate: SessionCandidate) -> SessionCandidate:
        exists = sm.default_worktree_probe(candidate)
        if exists is candidate.worktree_exists:
            return candidate
        return SessionCandidate(**{**candidate.__dict__, "worktree_exists": exists})

    def _list_sessions_bounded(self, budget_seconds: float | None) -> dict[str, Any]:
        """The fleet listing, passing a budget through when the controller
        understands one. A controller old enough not to (a test double, an
        older embedded build) still works exactly as it did."""
        if budget_seconds is None:
            return self.controller.terminal_list_sessions()
        try:
            return self.controller.terminal_list_sessions(budget_seconds=budget_seconds)
        except TypeError:
            return self.controller.terminal_list_sessions()

    def _lane_load_and_claims(self) -> tuple[dict[str, tuple[int, int]], dict[str, str]]:
        """session -> (active, queued), and session -> task_id holding it.

        ONE pass over the durable lanes for both. They were separate reads of
        the same full scan, which on a real fleet is the most expensive purely
        local thing a routing decision does.

        A claimed session is never offered to a second task -- that is the
        cheap, read-side half of the double-dispatch guard whose authoritative
        half is `bind_task_to_session`'s own BEGIN IMMEDIATE.
        """
        load: dict[str, tuple[int, int]] = {}
        claims: dict[str, str] = {}
        try:
            lanes = self.store.list_all_lanes()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("task-router: lane load read failed")
            return load, claims
        for lane in lanes:
            tasks = lane.get("tasks", [])
            active = queued = 0
            for task in tasks:
                status = task["status"]
                if status in QueueStore._ACTIVE_STATUSES:
                    active += 1
                elif status == QUEUED:
                    queued += 1
                held = task.get("execution_session")
                if (held and task.get("routing_state") in (BOUND, SPAWNED)
                        and status not in TERMINAL_STATUSES):
                    claims[held] = task["id"]
            load[lane["session"]] = (active, queued)
        return load, claims

    def _lane_load(self) -> dict[str, tuple[int, int]]:
        """session -> (active, queued), from the durable queue only."""
        return self._lane_load_and_claims()[0]

    def _session_claims(self) -> dict[str, str]:
        """session -> task_id currently holding it, so a claimed session is
        never offered to a second task."""
        return self._lane_load_and_claims()[1]

    def _probe(self, candidate: SessionCandidate) -> SessionCandidate:
        """Re-read one candidate's live state before we commit to it.

        The registry's last-known state is good enough to RANK with and not
        good enough to DISPATCH on: a session that went WAITING_INPUT since the
        last reconcile looks perfectly idle in durable state, and that is
        precisely the session a router must not pick."""
        if self.controller is None:
            return candidate
        try:
            status = self._status_bounded(candidate.session)
        except Exception:  # noqa: BLE001 -- an unreadable session is not a usable one
            return SessionCandidate(**{**candidate.__dict__,
                                       "state": "UNKNOWN", "state_probed": True,
                                       "node_online": False})
        if not isinstance(status, dict) or "error" in status:
            return SessionCandidate(**{**candidate.__dict__,
                                       "state": "UNKNOWN", "state_probed": True,
                                       "node_online": False})
        updates: dict[str, Any] = {"state_probed": True}
        if status.get("state"):
            updates["state"] = str(status["state"]).upper()
        if status.get("input_required"):
            updates["state"] = "WAITING_INPUT"
        if status.get("cwd"):
            updates["cwd"] = status["cwd"]
        resource = status.get("resource")
        if isinstance(resource, dict):
            context = resource.get("context")
            if isinstance(context, dict) and context.get("percent") is not None:
                updates["context_percent"] = float(context["percent"])
            git = resource.get("git")
            if isinstance(git, dict):
                if git.get("repo"):
                    updates["repo"] = git["repo"]
                if git.get("branch"):
                    updates["branch"] = git["branch"]
                if git.get("dirty") is not None:
                    updates["dirty"] = bool(git["dirty"])
        refreshed = SessionCandidate(**{**candidate.__dict__, **updates})
        return self._with_worktree(refreshed)

    def _status_bounded(self, session: str) -> dict[str, Any]:
        """One live status read, inside whatever is left of the route budget.

        `terminal_status` is unbounded: it resolves the session across the
        fleet and then waits on a node with no ceiling of its own. Three of
        those is how a probe_limit of 3 ate a 12s budget. The controller
        already has a bounded form for exactly this reason (the wait/resume
        tools use it); the router now uses it too, and falls back to the plain
        call only for a controller that does not offer one."""
        bounded = getattr(self.controller, "terminal_status_bounded", None)
        if bounded is None or self._deadline is None:
            return self.controller.terminal_status(session)
        remaining = self._deadline - self.clock()
        if remaining <= MIN_PROBE_SECONDS:
            # No time left to verify: report it as unverifiable rather than
            # spending budget the dispatch phase needs. An unprobed candidate
            # is ranked on its durable state, which is what happens for every
            # candidate past probe_limit anyway.
            return {"error": "STATUS_PROBE_TIMEOUT", "session": session}
        return bounded(session, min(MAX_PROBE_SECONDS, remaining))

    # -- routing -----------------------------------------------------------

    def profile_for(self, task: Any) -> TaskProfile:
        return task_profile.analyze(task)

    def route_task(self, task_id: str, *, allow_spawn: bool | None = None,
                   dispatch: bool = True) -> RoutingOutcome:
        """Find a runtime for one durable task, claim it, and start it.

        Returns an outcome in every case -- a task that cannot be routed is a
        recorded fact, never an exception, because the caller is usually a
        background sweep that must carry on to the next task."""
        with self._lock:
            return self._route_locked(task_id, allow_spawn=allow_spawn, dispatch=dispatch)

    def _route_locked(self, task_id: str, *, allow_spawn: bool | None,
                      dispatch: bool) -> RoutingOutcome:
        task = self.store.get_task(task_id)
        if task is None:
            return RoutingOutcome(outcome=NOT_ROUTABLE, task_id=task_id, reason="task not found")
        if task.status in TERMINAL_STATUSES:
            return RoutingOutcome(outcome=NOT_ROUTABLE, task_id=task_id, task_state=task.status,
                                  reason=f"task is already {task.status}")
        if task.routing_state in (BOUND, SPAWNED) and task.execution_session:
            return RoutingOutcome(outcome=ALREADY_BOUND, task_id=task_id,
                                  session=task.execution_session, node_id=task.execution_node_id,
                                  routing_state=task.routing_state, task_state=task.status,
                                  reason=f"already bound to {task.execution_session}")
        if (task.metadata or {}).get("pinned_session"):
            # Hard affinity, declared at creation. The router records that it
            # looked and deliberately did nothing, rather than silently
            # rerouting work someone pinned on purpose.
            return RoutingOutcome(outcome=NOT_ROUTABLE, task_id=task_id, task_state=task.status,
                                  session=task.session, reason="task is pinned to an explicit session")
        if not self.enabled:
            return RoutingOutcome(outcome=DEFERRED, task_id=task_id, task_state=task.status,
                                  routing_state=UNROUTED, reason="router is disabled by policy")

        if self.capacity_check is not None:
            try:
                blocked = self.capacity_check(task)
            except Exception:  # noqa: BLE001 -- an admission-check glitch must not strand a task
                _LOGGER.exception("task-router: capacity check failed for task %s", task_id)
                blocked = None
            if blocked:
                evidence = {"reason": blocked, "capacity_blocked": True,
                            "candidates_considered": 0}
                self.store.record_routing_deferral(task_id, evidence=evidence)
                return RoutingOutcome(outcome=DEFERRED, task_id=task_id, task_state=task.status,
                                      routing_state=WAITING_RUNTIME, reason=blocked,
                                      evidence=evidence)

        profile = self.profile_for(task)
        # ONE wall-clock budget for the whole synchronous route, not one per
        # phase. Found live (hp-linux): a 12s dispatch budget still produced a
        # 42s call, because eight live probes across a 68-session fleet had
        # already spent thirty seconds before the first tick.
        budget = float(self._policy_value("dispatch_budget_seconds", 12.0))
        route_deadline = self.clock() + budget
        self._deadline = route_deadline
        # The fleet snapshot gets a SLICE of the budget, never all of it.
        # Whatever it does not use is left for ranking and, crucially, for the
        # dispatch ticks that actually start the task.
        snapshot_budget = max(0.5, min(self.snapshot_budget_seconds, budget * 0.5))
        snapshot_started = self.clock()
        fleet = self.candidates(budget_seconds=snapshot_budget)
        snapshot_seconds = self.clock() - snapshot_started
        match = sm.rank(profile, fleet, probe=self._probe,
                        probe_limit=int(self._policy_value("probe_limit", 3)),
                        deadline=lambda: self.clock() >= route_deadline)
        evidence: dict[str, Any] = {
            "profile": profile.as_dict(),
            "decided_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "candidates_considered": match.considered,
            "fleet_snapshot_seconds": round(snapshot_seconds, 3),
            "fleet_snapshot_budget_seconds": round(snapshot_budget, 3),
            **match.as_dict(),
        }

        if match.chosen is not None:
            chosen = match.chosen
            evidence["reason"] = self._reason_for(chosen)
            evidence["score"] = chosen.score
            bound = self.store.bind_task_to_session(
                task_id, chosen.candidate.session, node_id=chosen.candidate.node_id,
                routing_state=BOUND, evidence=evidence,
                agent_id=profile.agent_id or None,
                skill_ids=list(profile.skill_ids) or None)
            if "error" in bound:
                # Lost the race, or the task moved on. Both are correct
                # outcomes of a concurrent system, not failures -- the winner
                # already has it, and inventing a second binding is the exact
                # double-dispatch this design exists to prevent.
                self.invalidate()
                return RoutingOutcome(outcome=ALREADY_BOUND, task_id=task_id,
                                      session=chosen.candidate.session,
                                      task_state=task.status, reason=bound["error"],
                                      evidence=evidence)
            self.invalidate()
            outcome = RoutingOutcome(
                outcome=ROUTED, task_id=task_id, session=chosen.candidate.session,
                node_id=chosen.candidate.node_id, score=chosen.score,
                reason=evidence["reason"], routing_state=BOUND,
                task_state=bound.get("status"), evidence=evidence)
            if dispatch and not self._dispatch(outcome) and self._should_release(outcome):
                return self._release_and_defer(outcome, evidence)
            return outcome

        spawn_allowed = self.spawn_enabled if allow_spawn is None else allow_spawn
        if spawn_allowed:
            spawned = self._spawn_for(profile, evidence)
            if spawned is not None:
                session_name, node_id = spawned
                evidence["reason"] = (f"no eligible session among {match.considered} candidates; "
                                      f"spawned {session_name} on {node_id}")
                bound = self.store.bind_task_to_session(
                    task_id, session_name, node_id=node_id, routing_state=SPAWNED,
                    evidence=evidence, agent_id=profile.agent_id or None,
                    skill_ids=list(profile.skill_ids) or None)
                self.invalidate()
                if "error" not in bound:
                    outcome = RoutingOutcome(
                        outcome=SPAWNED_RUNTIME, task_id=task_id, session=session_name,
                        node_id=node_id, reason=evidence["reason"], routing_state=SPAWNED,
                        task_state=bound.get("status"), evidence=evidence)
                    if dispatch and not self._dispatch(outcome) and self._should_release(outcome):
                        # A session the router itself just created that still
                        # will not start the task is a real problem, but the
                        # honest answer is the same: say so and re-match.
                        return self._release_and_defer(outcome, evidence)
                    return outcome

        evidence["reason"] = self._deferral_reason(match, spawn_allowed)
        self.store.record_routing_deferral(task_id, evidence=evidence)
        return RoutingOutcome(outcome=DEFERRED, task_id=task_id, task_state=task.status,
                              routing_state=WAITING_RUNTIME, reason=evidence["reason"],
                              evidence=evidence)

    @staticmethod
    def _reason_for(chosen: sm.ScoredCandidate) -> str:
        head = f"score {chosen.score}"
        if chosen.reasons:
            return f"{head}: " + "; ".join(chosen.reasons)
        return f"{head}: no positive signals, but nothing disqualified it either"

    @staticmethod
    def _deferral_reason(match: sm.MatchResult, spawn_allowed: bool) -> str:
        """Why this task is still queued, in one sentence an operator can act on."""
        if match.considered == 0:
            return ("no sessions visible in the fleet"
                    + ("" if spawn_allowed else "; spawning is disabled by policy"))
        counts: dict[str, int] = {}
        for row in match.ranked:
            if row.eligible:
                continue
            key = row.rejected or "SCORE_BELOW_THRESHOLD"
            counts[key] = counts.get(key, 0) + 1
        summary = ", ".join(f"{count}x {reason}" for reason, count in
                            sorted(counts.items(), key=lambda item: -item[1]))
        tail = "" if spawn_allowed else "; spawning a new runtime is disabled by policy"
        if match.unverified:
            # Never say "nothing was eligible" when we simply stopped looking.
            return (f"stopped after verifying {len(match.ranked) - match.unverified} of "
                    f"{match.considered} candidates with {match.unverified} still eligible but "
                    f"unverified (probe budget); raise router.probe_limit to look further"
                    f"{tail}")
        return f"no eligible session among {match.considered} candidates ({summary}){tail}"

    def _dispatch(self, outcome: RoutingOutcome) -> bool:
        """Drive the bound lane until the task is genuinely under way.

        NOT ONE TICK. QueueEngine.tick makes at most ONE transition per call by
        design (claim -> coordinator review -> dispatch), so a single tick
        leaves a task at PRECHECK at best -- and if the engine declines to
        claim at all (the LLM governor refusing admission, a paused lane, an
        unmet dependency) it leaves it exactly where it was. Binding and
        ticking once and calling that "dispatched" is what produced the live
        BOUND-but-QUEUED failure.

        So this drives the same bounded sequence `turn(action="start")` already
        uses, with the same constants, and stops at the first settled state.
        This is a bound on how long the ROUTER waits, never on the task, which
        is durable from the moment it was persisted.

        Driving the lane's ticks here does NOT touch `auto_dispatch_enabled`:
        that flag governs the background QueueLoop's own lane sweep and stays
        exactly as the operator left it. Every tick still passes through the
        coordinator gate and the admission governor, so nothing here bypasses
        a policy -- it only stops the router from walking away from a task it
        just claimed a session for.

        Returns True when the task really is under way.
        """
        if self.engine is None or not outcome.session:
            outcome.dispatch_detail = "no queue engine is wired on this server"
            return False
        settled = START_UNDERWAY_STATUSES | START_NEEDS_HUMAN_STATUSES | START_SERVER_PENDING_STATUSES
        detail: str | None = None
        budget = float(self._policy_value("dispatch_budget_seconds", 12.0))
        # The matching phase may already have spent part of the budget; a
        # deadline handed down from there is honoured, so the two phases share
        # one ceiling instead of each getting a full one.
        deadline = self._deadline if self._deadline is not None else self.clock() + budget
        for _ in range(MAX_START_TICKS):
            if self.clock() >= deadline:
                # A submission must never become a long poll. The task is
                # durable and bound; the queue loop and the rescue sweep carry
                # it from here, so the only thing this cutoff ends is how long
                # the caller waits -- not the work.
                outcome.budget_exhausted = True
                detail = (f"dispatch budget of {budget:g}s exhausted after "
                          f"{outcome.dispatch_ticks} tick(s); the server keeps advancing it")
                break
            task = self.store.get_task(outcome.task_id)
            if task is None:
                break
            outcome.task_state = task.status
            if task.status in settled:
                break
            try:
                result = self.engine.tick(outcome.session)
            except Exception:  # noqa: BLE001 -- the binding is durable; a tick failure is recoverable
                _LOGGER.exception("task-router: dispatch tick failed for %r", outcome.session)
                detail = detail or "queue engine tick raised"
                break
            outcome.dispatch_ticks += 1
            # The engine's own words for why it could not move: an admission
            # refusal, a paused lane, a coordinator verdict. Never invented here.
            if getattr(result, "detail", None):
                detail = str(result.detail)
        task = self.store.get_task(outcome.task_id)
        if task is not None:
            outcome.task_state = task.status
            if task.last_error and not detail:
                detail = str(task.last_error)
        outcome.dispatched = outcome.task_state in START_UNDERWAY_STATUSES
        outcome.dispatch_detail = detail
        if outcome.task_state in settled:
            # The task reached a real resting state, so the clock running out
            # is not what happened to it. Found live: a task the coordinator
            # had PAUSED came back saying "the server is still driving it",
            # which is the same class of lie as the old dispatched=true.
            outcome.budget_exhausted = False
            if outcome.task_state in START_NEEDS_HUMAN_STATUSES:
                detail = self._settled_reason(outcome.task_id) or detail
                outcome.dispatch_detail = detail
        _LOGGER.info("task-router: task=%s session=%s ticks=%s state=%s dispatched=%s detail=%s",
                     outcome.task_id, outcome.session, outcome.dispatch_ticks,
                     outcome.task_state, outcome.dispatched, detail)
        return outcome.dispatched

    @staticmethod
    def _should_release(outcome: RoutingOutcome) -> bool:
        """Hand the runtime back, or keep it? Three different situations.

        RELEASE when the engine simply would not claim the task -- it is still
        QUEUED, the session is doing nothing for it, and some other runtime
        might.

        KEEP when the caller's clock ran out: the engine is mid-flight and
        taking its session away would strand real work.

        KEEP when the task is stopped for a HUMAN (the coordinator gate
        refusing, a paused lane, a failed attempt). Re-matching would be
        pointless -- the gate refused on grounds another session in the same
        repo would hit too -- and it would replace an answerable "this is
        blocked, here is why" with a task drifting between sessions.
        """
        if outcome.budget_exhausted:
            return False
        if outcome.task_state in START_NEEDS_HUMAN_STATUSES:
            return False
        return True

    def _settled_reason(self, task_id: str) -> str | None:
        """Why a stopped task stopped, in the words of whatever stopped it --
        the coordinator's decision or the task's own last error, never a guess
        assembled from the status name."""
        task = self.store.get_task(task_id)
        if task is None:
            return None
        if task.coordinator_reason:
            return str(task.coordinator_reason)
        decision = task.coordinator_decision or {}
        if isinstance(decision, dict) and decision.get("reason"):
            return str(decision["reason"])
        return str(task.last_error) if task.last_error else None

    def _release_and_defer(self, outcome: RoutingOutcome, evidence: dict[str, Any]) -> RoutingOutcome:
        """The session was eligible but the engine would not start the task.

        Hand the runtime back and say so. Leaving it BOUND would park the task
        in a lane nothing is going to advance while holding a session other
        tasks could have used -- the precise shape of the original production
        failure, just with a routing decision attached to it."""
        self.store.release_execution_binding(
            outcome.task_id, reason=outcome.dispatch_detail or "engine did not advance the task")
        held = outcome.session
        evidence = dict(evidence)
        evidence["reason"] = (
            f"bound {held} but the queue engine did not start the task"
            + (f": {outcome.dispatch_detail}" if outcome.dispatch_detail else "")
            + "; the binding was released so the task can be re-matched")
        evidence["undispatchable_session"] = held
        self.store.record_routing_deferral(outcome.task_id, evidence=evidence)
        self.invalidate()
        return RoutingOutcome(
            outcome=DEFERRED, task_id=outcome.task_id, task_state=outcome.task_state,
            routing_state=WAITING_RUNTIME, reason=evidence["reason"],
            dispatch_ticks=outcome.dispatch_ticks, dispatch_detail=outcome.dispatch_detail,
            evidence=evidence)

    # -- spawning ----------------------------------------------------------

    def _spawn_for(self, profile: TaskProfile, evidence: dict[str, Any]) -> tuple[str, str] | None:
        """Create one compatible session, or None if policy/capacity says no.

        The duplicate-spawn guard is the name. It is derived from the task id,
        so a retried route asks for a session that already exists and gets
        SESSION_ALREADY_EXISTS back -- which is a success for our purposes, not
        a reason to create a second one."""
        if self.controller is None or not profile.task_id:
            return None
        limit = int(self._policy_value("max_spawned_sessions", 4))
        existing = [candidate for candidate in self.candidates()
                    if candidate.session.startswith("tmcp-router-")]
        if len(existing) >= limit:
            evidence["spawn_blocked"] = f"already {len(existing)} router-spawned sessions (limit {limit})"
            return None
        name = f"tmcp-router-{profile.task_id[:12]}"
        runtime = profile.preferred_runtime or str(self._policy_value("default_runtime", "shell"))
        cwd = profile.workspace or None
        try:
            result = self.controller.terminal_create_session(
                name, runtime, cwd, node="auto", requested_by="task-router")
        except Exception:  # noqa: BLE001
            _LOGGER.exception("task-router: spawn failed for %r", name)
            evidence["spawn_blocked"] = "spawn raised"
            return None
        if isinstance(result, dict) and result.get("error") == "SESSION_ALREADY_EXISTS":
            self.invalidate()
            return name, result.get("node_id") or ""
        if not isinstance(result, dict) or "error" in result:
            evidence["spawn_blocked"] = (result or {}).get("error", "spawn failed")
            return None
        self.invalidate()
        return name, result.get("node_id") or ""

    # -- queue rescue ------------------------------------------------------

    def rescue_once(self, *, limit: int = 20) -> dict[str, Any]:
        """Re-ask "could this run now?" for every task that has no runtime.

        THIS IS THE FIX, not a nicety. A routing decision is only correct for
        the instant it was made: a task deferred while the fleet was busy must
        be reconsidered when a session goes idle, and before this existed
        nothing ever did. Restart-safe because every input is a durable row --
        a controller that has just come up re-derives the identical work list.

        One task's failure never stops the sweep. A bad row must not be able to
        wedge the queue for every other task, which is the same isolation
        QueueLoop already gives its per-lane ticks.
        """
        if not self.rescue_enabled or not self.enabled:
            return {"enabled": False, "routed": 0, "deferred": 0, "results": []}
        self.invalidate()
        results: list[dict[str, Any]] = []
        routed = deferred = 0
        try:
            tasks = self.store.routable_tasks(limit=limit)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("task-router: routable-task scan failed")
            return {"enabled": True, "routed": 0, "deferred": 0, "error": "SCAN_FAILED", "results": []}
        for task in tasks:
            try:
                outcome = self.route_task(task.id)
            except Exception as exc:  # noqa: BLE001 -- one bad row must not wedge the sweep
                _LOGGER.exception("task-router: rescue failed for task %s", task.id)
                results.append({"task_id": task.id, "outcome": "ERROR",
                                "reason": f"{type(exc).__name__}: {exc}"})
                continue
            if outcome.outcome in (ROUTED, SPAWNED_RUNTIME):
                routed += 1
            elif outcome.outcome == DEFERRED:
                deferred += 1
            results.append(outcome.as_dict())
        advanced = self._advance_bound_tasks()
        return {"enabled": True, "scanned": len(tasks), "routed": routed,
                "deferred": deferred, "advanced": len(advanced), "results": results,
                "advanced_results": advanced}

    def _advance_bound_tasks(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """Finish what a time-boxed route started.

        The router drives a lane it has just bound, but stops on a wall clock
        so a submission never becomes a long poll. When the clock wins the
        task is BOUND and pre-dispatch, and if its lane never opted into
        auto-dispatch the background sweep will not touch it either -- so this
        step picks those up and keeps ticking them. Same release policy as the
        synchronous path, so a task the engine will not claim still gets its
        runtime handed back rather than holding it forever."""
        advanced: list[dict[str, Any]] = []
        try:
            pending = self.store.bound_unstarted_tasks(limit=limit)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("task-router: bound-task scan failed")
            return advanced
        for task in pending:
            outcome = RoutingOutcome(
                outcome=ROUTED, task_id=task.id, session=task.execution_session,
                node_id=task.execution_node_id, routing_state=task.routing_state or BOUND,
                task_state=task.status, reason=(task.routing_evidence or {}).get("reason", ""))
            try:
                self._dispatch(outcome)
                if self._should_release(outcome):
                    self._release_and_defer(outcome, dict(task.routing_evidence or {}))
            except Exception as exc:  # noqa: BLE001 -- one task must not wedge the sweep
                _LOGGER.exception("task-router: advancing bound task %s failed", task.id)
                advanced.append({"task_id": task.id, "outcome": "ERROR",
                                 "reason": f"{type(exc).__name__}: {exc}"})
                continue
            advanced.append(outcome.as_dict())
        return advanced

    def on_session_idle(self, session: str) -> dict[str, Any]:
        """Hook: a session just became free, so re-run the sweep now.

        Cheaper and far more responsive than waiting for the next periodic
        reconcile, and it is the difference between a task starting in
        milliseconds and starting in ten seconds."""
        self.invalidate()
        _LOGGER.debug("task-router: session %r reported idle; re-running rescue", session)
        return self.rescue_once()

    # -- one-call entry point ----------------------------------------------

    def route_start(self, prompt: str, *, title: str | None = None, priority: int = 0,
                    metadata: dict[str, Any] | None = None, request_key: str | None = None,
                    project: str | None = None, agent_id: str | None = None,
                    skill_ids: Sequence[str] | None = None,
                    target: str | None = None) -> dict[str, Any]:
        """Persist the work, then find it a runtime -- in one call, no target.

        Persist FIRST, exactly like every other creation path in this project:
        the durable row exists before any routing is attempted, so a crash
        between the two loses a routing decision (cheap, recomputed on the next
        rescue) and never the task itself.

        `target` is accepted and honoured as HARD AFFINITY: naming a session
        means that session, and the router will not reroute it. It exists here
        only so one action can serve both callers; the routing is what happens
        when it is absent.
        """
        if self.queue is None:
            return {"status": "FAILED", "error": "ACTION_UNAVAILABLE",
                    "detail": "route_start is not wired on this server"}
        if not prompt or not str(prompt).strip():
            return {"status": "FAILED", "error": "TEXT_REQUIRED"}

        full_metadata = dict(metadata or {})
        if agent_id:
            full_metadata.setdefault("agent_id", agent_id)
        if skill_ids:
            full_metadata.setdefault("skill_ids", list(skill_ids))
        if target:
            full_metadata["pinned_session"] = target
        else:
            # The router chose this task's placement, so the router may choose
            # again -- see QueueStore.routable_tasks for why that permission
            # has to be recorded rather than assumed for every queued task.
            full_metadata[self.store.ROUTER_OWNED_METADATA_KEY] = True

        created = self.queue.create_task(
            title or "", prompt, session=target, project=project,
            metadata=full_metadata, request_key=request_key)
        if not isinstance(created, dict) or created.get("error") or not created.get("task_id"):
            return {"status": "FAILED", "action": "route_start", "result": created}
        task_id = created["task_id"]
        if agent_id or skill_ids:
            self.store.set_agent_binding(task_id, agent_id=agent_id,
                                         skill_ids=list(skill_ids) if skill_ids else None)

        if target:
            # Hard affinity: bind to exactly what the caller named, with no
            # eligibility opinion of our own, and dispatch there.
            evidence = {"reason": f"explicit target {target}: hard affinity, no routing performed",
                        "explicit_target": True}
            bound = self.store.bind_task_to_session(task_id, target, evidence=evidence)
            outcome = RoutingOutcome(
                outcome=ROUTED if "error" not in bound else ALREADY_BOUND,
                task_id=task_id, session=target, reason=evidence["reason"],
                routing_state=BOUND, task_state=bound.get("status"), evidence=evidence)
            if "error" not in bound:
                # Hard affinity: the caller named this session, so a failure to
                # start is reported, never "fixed" by moving the task somewhere
                # the caller did not ask for.
                self._dispatch(outcome)
            return self._receipt(outcome, created, explicit_target=True)

        outcome = self.route_task(task_id)
        return self._receipt(outcome, created, explicit_target=False)

    @staticmethod
    def _receipt(outcome: RoutingOutcome, created: dict[str, Any], *,
                 explicit_target: bool) -> dict[str, Any]:
        """The anti-polling receipt, with the routing decision in it.

        `poll: False` in every case, same contract as `action="start"`: the
        task is durable, the server advances it, and a caller that has a
        task_id learns nothing by asking again. When the answer is "still
        queued", the receipt says WHY rather than leaving the caller to guess.
        """
        dispatched = outcome.dispatched
        receipt: dict[str, Any] = {
            "status": "TASK_STARTED" if dispatched else "TASK_ACCEPTED",
            "action": "route_start",
            "task_id": outcome.task_id,
            "session": outcome.session,
            "node_id": outcome.node_id,
            "routing_outcome": outcome.outcome,
            "routing_state": outcome.routing_state,
            "routing_reason": outcome.reason,
            "score": outcome.score,
            "task_state": outcome.task_state,
            "dispatched": dispatched,
            "dispatch_ticks": outcome.dispatch_ticks,
            "dispatch_detail": outcome.dispatch_detail,
            "budget_exhausted": outcome.budget_exhausted,
            "explicit_target": explicit_target,
            "deduplicated": bool(created.get("deduplicated")),
            "request_key": created.get("request_key"),
            "poll": False,
        }
        if outcome.outcome == DEFERRED:
            receipt["needs_human"] = False
            receipt["next_action"] = "none"
            receipt["rejected_candidates"] = (outcome.evidence or {}).get("rejected", [])
            receipt["guidance"] = (
                "no runtime was eligible, so the task is durably queued as WAITING_RUNTIME. "
                "The server re-runs the match every rescue cycle and will start it as soon as a "
                "compatible session frees up -- do not poll and do not re-send")
        elif outcome.task_state in START_NEEDS_HUMAN_STATUSES:
            # Stopped, and only a person can move it. Saying anything else
            # here is how an orchestrator silently drops a task.
            receipt["needs_human"] = True
            receipt["next_action"] = "resolve"
            receipt["blocked_reason"] = outcome.dispatch_detail
            receipt["guidance"] = (
                f"this task is NOT running: it is {outcome.task_state}"
                + (f" -- {outcome.dispatch_detail}" if outcome.dispatch_detail else "")
                + ". Polling will not change that; resolve the blocker. The task is already "
                  "durably queued under this task_id, so do not re-send the prompt")
        elif outcome.budget_exhausted:
            receipt["needs_human"] = False
            receipt["next_action"] = "none"
            receipt["guidance"] = (
                f"the task is durably bound to {outcome.session} and the server is still "
                f"driving it; this call returned rather than hold the connection open "
                f"({outcome.dispatch_detail}) -- do not poll and do not re-send")
        elif not dispatched:
            # Bound, but the engine did not start it. Saying "started" here is
            # how an orchestrator drops a task, so the receipt says the
            # opposite plainly and names the blocker the engine reported.
            receipt["needs_human"] = False
            receipt["next_action"] = "none"
            receipt["guidance"] = (
                f"the task is durably queued on {outcome.session} but the queue engine has not "
                f"started it yet"
                + (f" ({outcome.dispatch_detail})" if outcome.dispatch_detail else "")
                + " -- the server keeps advancing it under this task_id; do not poll and do "
                  "not re-send")
        else:
            receipt["needs_human"] = False
            receipt["next_action"] = "none"
            receipt["guidance"] = (
                "work is started and tracked server-side under this task_id -- do not poll; "
                "use action=task with this task_id only if the user explicitly asks to check")
        return receipt
