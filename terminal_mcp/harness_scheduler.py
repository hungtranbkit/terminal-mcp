"""Which task next, on which session -- decided by arithmetic, not by a model.

WHY THERE IS NO PM MODEL IN THIS FILE

"What should we work on next?" reads like a judgement call and is not one.
Given a dependency graph, a set of finished runs and a concurrency limit, the
answer is determined: the ready tasks are the ones whose dependencies are
satisfied, and the best one to start is the one on the longest remaining path
to the milestone. That is a topological sort and a depth calculation. A model
asked the same question gives the same answer more slowly, less consistently,
and for money -- and it has to be re-asked every time anything changes, which
is how a scheduler becomes a polling loop with a bill attached.

So this module is pure functions over a frozen graph. It starts nothing,
stores nothing and calls nothing. `plan()` returns an ordering and a session
assignment; the caller decides what to do with them.

COST_FIRST IS THE DEFAULT, AND THE DEFAULT IS NOT "SLOW"

The three modes trade money for wall-clock:

* COST_FIRST keeps ONE Builder active per lane and one overall. Every task in
  a lane therefore lands in the same warm session, and each new task pays only
  for its own contract instead of re-loading the project. Sequential, cheapest
  per task, and on a dependency chain it is barely slower than the
  alternatives -- a chain cannot be parallelised anyway.
* BALANCED allows two lanes to advance at once. Independent lanes really are
  independent, so this buys real wall-clock with a real second session.
* SPEED_FIRST removes the lane cap and stops reusing sessions across tasks.
  Fastest, and it pays a full context load per task to get there.

The knob exists because the right answer differs by situation, and because a
knob makes the cost of "just run everything at once" visible as a number
rather than as a surprise at the end of the month.

SESSION REUSE ACROSS TASKS IS NOT THE SAME AS REUSE ACROSS ITERATIONS

Reuse across iterations (a revision going back to the builder that wrote the
code) is about the SAME work. Reuse across tasks is about the same PLACE: two
tasks in the same lane touch the same modules, so a session that already has
those files loaded can take the next contract without reloading anything.
That is only true while the tasks are adjacent in the plan and the lane has
not changed, which is exactly the condition `plan()` checks -- and only while
the session has room, which is the 85% ceiling shared with harness_policy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from . import harness_policy as policy

# -- scheduling modes ---------------------------------------------------------
COST_FIRST = "cost_first"
BALANCED = "balanced"
SPEED_FIRST = "speed_first"
SCHEDULING_MODES: tuple[str, ...] = (COST_FIRST, BALANCED, SPEED_FIRST)


@dataclass(frozen=True)
class SchedulerPolicy:
    """How much concurrency is allowed, and whether sessions carry over."""

    mode: str = COST_FIRST
    max_active_builders: int = 1
    max_active_per_lane: int = 1
    reuse_session_across_tasks: bool = True
    reuse_context_ceiling: float = policy.SESSION_REUSE_CONTEXT_CEILING
    #: Never more than one Planner for one task, and never a pool of them.
    #: Both are off in every mode: a planner pool multiplies cost by a
    #: constant to buy variance reduction nobody has measured.
    parallel_planners: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "max_active_builders": self.max_active_builders,
                "max_active_per_lane": self.max_active_per_lane,
                "reuse_session_across_tasks": self.reuse_session_across_tasks,
                "reuse_context_ceiling": self.reuse_context_ceiling,
                "parallel_planners": self.parallel_planners}


_POLICIES: dict[str, SchedulerPolicy] = {
    COST_FIRST: SchedulerPolicy(COST_FIRST, 1, 1, True),
    BALANCED: SchedulerPolicy(BALANCED, 2, 1, True),
    SPEED_FIRST: SchedulerPolicy(SPEED_FIRST, 4, 2, False),
}


def scheduler_policy(mode: str = COST_FIRST) -> SchedulerPolicy:
    if mode not in _POLICIES:
        raise ValueError(f"unknown scheduling mode {mode!r}; "
                         f"expected one of {', '.join(SCHEDULING_MODES)}")
    return _POLICIES[mode]


# -- the graph ----------------------------------------------------------------

@dataclass(frozen=True)
class TaskNode:
    """One node of the DEFINITION graph.

    `autonomous=False` is the important field. A task that needs an input only
    a person can supply is not "hard"; it is not startable at all, and the
    cheapest possible handling is to route it to a human BEFORE a Builder is
    paid to iterate against a bar it can never reach. Faking completion is the
    one outcome this field exists to make unreachable.
    """

    id: str
    lane: str = ""
    priority: str = "P2"
    depends_on: tuple[str, ...] = ()
    autonomous: bool = True
    not_autonomous_reason: str | None = None
    requested_mode: str | None = None
    #: An INFRASTRUCTURE prerequisite this machine does not satisfy: the
    #: repository is not here, or a toolchain the task's own checks invoke is
    #: absent or the wrong version.
    #:
    #: Deliberately a SEPARATE field from `autonomous`, because the two route
    #: to different places and conflating them would be wrong in both
    #: directions. A missing human asset is nobody's problem but a person's --
    #: it goes to the Human Decision Queue. A missing toolchain is not a
    #: decision at all; it is a fact about this host, and the same task on a
    #: machine that has the toolchain is perfectly autonomous. Putting an
    #: absent Node runtime in front of a human as a "decision" would be
    #: asking them to approve something rather than install it.
    #:
    #: What both share is the only thing that matters for cost: neither opens
    #: a run, so neither spends a Planner token finding out.
    infra_prerequisite: str | None = None

    @property
    def startable(self) -> bool:
        return self.autonomous and self.infra_prerequisite is None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "lane": self.lane, "priority": self.priority,
                "depends_on": list(self.depends_on), "autonomous": self.autonomous,
                "not_autonomous_reason": self.not_autonomous_reason,
                "infra_prerequisite": self.infra_prerequisite,
                "startable": self.startable,
                "requested_mode": self.requested_mode}


class DependencyCycle(ValueError):
    def __init__(self, cycle: Sequence[str]) -> None:
        self.cycle = list(cycle)
        super().__init__("dependency cycle: " + " -> ".join(cycle))


def closure(nodes: Mapping[str, TaskNode], target: str) -> list[str]:
    """Every task the target transitively needs, the target last.

    Depth-first with an explicit in-progress set, so a cycle is reported with
    the path that produced it rather than as a RecursionError three frames
    deep in something unrelated.
    """
    ordered: list[str] = []
    done: set[str] = set()
    path: list[str] = []
    in_path: set[str] = set()

    def walk(task_id: str) -> None:
        if task_id in done:
            return
        if task_id in in_path:
            raise DependencyCycle(path[path.index(task_id):] + [task_id])
        node = nodes.get(task_id)
        if node is None:
            # A dependency that is not in the graph is recorded as itself, so
            # the caller sees a named gap rather than a silently shorter plan.
            done.add(task_id)
            ordered.append(task_id)
            return
        path.append(task_id)
        in_path.add(task_id)
        for dependency in node.depends_on:
            walk(dependency)
        in_path.discard(task_id)
        path.pop()
        done.add(task_id)
        ordered.append(task_id)

    walk(target)
    return ordered


def depth_to(nodes: Mapping[str, TaskNode], target: str) -> dict[str, int]:
    """Longest remaining path from each task to the target -- the critical path.

    The task with the greatest depth is the one whose delay delays everything
    else, so starting it first is the scheduling decision, and it is
    arithmetic. Computed backwards over the closure, which is already in
    dependency order, so one pass suffices.
    """
    order = closure(nodes, target)
    depth: dict[str, int] = {}
    for task_id in reversed(order):
        if task_id == target:
            depth[task_id] = 0
            continue
        node = nodes.get(task_id)
        dependents = [other for other in order
                      if task_id in (nodes[other].depends_on if other in nodes else ())]
        depth[task_id] = 1 + max((depth.get(d, 0) for d in dependents), default=0)
    return depth


_PRIORITY_RANK: dict[str, int] = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def ready(nodes: Mapping[str, TaskNode], *, satisfied: Iterable[str],
          target: str) -> list[str]:
    """Tasks whose dependencies are all satisfied and which are not done.

    `satisfied` comes from the RUN store -- a dependency is met when its run
    reached a terminal-success stage -- never from the definition file. A
    definition file records what was planned; it is not, and must never
    become, the record of what happened.
    """
    finished = set(satisfied)
    out = []
    for task_id in closure(nodes, target):
        if task_id in finished:
            continue
        node = nodes.get(task_id)
        if node is None:
            continue
        if all(dependency in finished for dependency in node.depends_on):
            out.append(task_id)
    return out


@dataclass(frozen=True)
class Assignment:
    """One task, and the session it should run on."""

    task_id: str
    lane: str
    mode_hint: str | None = None
    reuse_session_from: str | None = None
    reason: str = ""

    @property
    def inherits_session(self) -> bool:
        return self.reuse_session_from is not None

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "lane": self.lane,
                "mode_hint": self.mode_hint,
                "reuse_session_from": self.reuse_session_from,
                "inherits_session": self.inherits_session, "reason": self.reason}


@dataclass(frozen=True)
class Plan:
    order: tuple[Assignment, ...]
    deferred: tuple[tuple[str, str], ...] = ()
    #: The subset of `deferred` that is an infrastructure gap rather than a
    #: human decision. Reported separately because the actions differ: one is
    #: "install this / use that machine", the other is "somebody decide".
    blocked_on_infra: tuple[tuple[str, str], ...] = ()
    scheduling: SchedulerPolicy = field(default_factory=scheduler_policy)

    def to_dict(self) -> dict[str, Any]:
        return {"scheduling": self.scheduling.to_dict(),
                "order": [a.to_dict() for a in self.order],
                "deferred": [{"task_id": t, "reason": r} for t, r in self.deferred],
                "blocked_on_infra": [{"task_id": t, "reason": r}
                                     for t, r in self.blocked_on_infra]}

    @property
    def session_reuses(self) -> int:
        return sum(1 for a in self.order if a.inherits_session)


def plan(nodes: Mapping[str, TaskNode], *, target: str,
         satisfied: Iterable[str] = (), mode: str = COST_FIRST,
         context_percent: Mapping[str, float] | None = None) -> Plan:
    """The whole ordering, in one deterministic pass.

    Selection rule, applied repeatedly to the set of ready tasks:
      1. greatest depth to the target  -- the critical path
      2. then the lane already holding the warm session, if reuse is allowed
         and that lane still has room -- this is what turns a lane into one
         session instead of five
      3. then declared priority, then task id -- so the answer never depends
         on dictionary ordering

    Rule 2 sits ABOVE priority on purpose in COST_FIRST: two P0 tasks in
    different lanes are equally urgent, and choosing the one that needs no
    context reload is free. It sits below depth because delaying the critical
    path to save a context load trades money for the schedule, which is the
    wrong direction even in COST_FIRST.
    """
    scheduling = scheduler_policy(mode)
    depths = depth_to(nodes, target)
    occupancy = dict(context_percent or {})
    finished = set(satisfied)
    assignments: list[Assignment] = []
    deferred: list[tuple[str, str]] = []
    blocked_on_infra: list[tuple[str, str]] = []
    lane_sessions: dict[str, str] = {}
    last_lane: str | None = None
    remaining = {task_id for task_id in closure(nodes, target)
                 if task_id not in finished}

    while remaining:
        candidates = [task_id for task_id in ready(nodes, satisfied=finished, target=target)
                      if task_id in remaining]
        if not candidates:
            for task_id in sorted(remaining):
                node = nodes.get(task_id)
                missing = [d for d in (node.depends_on if node else ())
                           if d not in finished]
                deferred.append((task_id, "unsatisfiable dependencies: "
                                          + ", ".join(missing) if missing
                                 else "not in the definition graph"))
            break

        blocked = [task_id for task_id in candidates
                   if not (nodes[task_id].startable if task_id in nodes else True)]
        for task_id in blocked:
            node = nodes[task_id]
            if node.infra_prerequisite:
                # A fact about this host, not a question for anybody. It is
                # deferred with the exact gap named so an operator can close
                # it, and NOT routed to the Human Decision Queue -- see
                # TaskNode.infra_prerequisite for why the distinction is
                # load-bearing rather than cosmetic.
                blocked_on_infra.append((task_id, node.infra_prerequisite))
                deferred.append((task_id, node.infra_prerequisite))
            else:
                deferred.append((task_id, node.not_autonomous_reason
                                 or "needs an input only a person can supply"))
            remaining.discard(task_id)
            # NOT added to `finished`: a task routed to a human is not done,
            # and anything depending on it stays unstartable. Marking it
            # satisfied here is exactly the "fake completion" this forbids.
        candidates = [t for t in candidates if t not in blocked]
        if not candidates:
            continue

        def warmth(task_id: str) -> int:
            """0 = the session we just used, 1 = a lane session still open,
            2 = a lane with no session yet.

            A lane's Builder session is not destroyed when the plan steps into
            another lane -- COST_FIRST runs one Builder at a time, so the
            other lane's session is merely idle. Treating "we were just here"
            as the only warm case would abandon a loaded session after a
            single interleave and pay to rebuild it later.
            """
            node = nodes[task_id]
            if not (scheduling.reuse_session_across_tasks and node.lane):
                return 2
            if occupancy.get(node.lane, 0.0) >= scheduling.reuse_context_ceiling:
                return 2
            if node.lane == last_lane and lane_sessions.get(node.lane):
                return 0
            return 1 if lane_sessions.get(node.lane) else 2

        def rank(task_id: str) -> tuple:
            node = nodes[task_id]
            return (-depths.get(task_id, 0), warmth(task_id),
                    _PRIORITY_RANK.get(node.priority, 9), task_id)

        chosen = sorted(candidates, key=rank)[0]
        node = nodes[chosen]
        reuse_from = None
        reason = "critical path"
        if warmth(chosen) < 2 and lane_sessions.get(node.lane):
            reuse_from = lane_sessions[node.lane]
            reason = (f"lane {node.lane} session opened for {reuse_from} is still "
                      f"loaded with this lane's modules; only the contract is sent")
        assignments.append(Assignment(task_id=chosen, lane=node.lane,
                                      mode_hint=node.requested_mode,
                                      reuse_session_from=reuse_from, reason=reason))
        lane_sessions[node.lane] = chosen
        last_lane = node.lane
        finished.add(chosen)
        remaining.discard(chosen)

    return Plan(order=tuple(assignments), deferred=tuple(deferred),
                blocked_on_infra=tuple(blocked_on_infra),
                scheduling=scheduling)


def concurrency_slots(plan_result: Plan) -> int:
    """How many Builders the plan ever wants at once. One, in COST_FIRST."""
    return plan_result.scheduling.max_active_builders
