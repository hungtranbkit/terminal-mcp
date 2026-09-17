"""Why is a worker idle while READY work exists?

blg_8d65afc1b38b. Observed in production 2026-09-14: the supervisor loop
polling every 20s with `watch_count=4, enabled_watch_count=0`, two stalled
workers, `hp1` untouched for ~35h, and `linux1`/`hp3-work` sitting at an
empty prompt having finished their work -- while READY backlog items
existed. Capacity was there, work was there, and nothing connected them.

This module is the part of that fix which can be reasoned about on its
own: no I/O, no tmux, no database. It answers three questions from facts a
caller has already gathered.

  1. What state is this worker really in?          classify_worker
  2. May a disabled watch be brought back?          watch_recovery_action
  3. Is the refill invariant currently violated?    evaluate_refill

Keeping it pure is what makes the interesting cases testable at all --
"stale-looking but a child process is genuinely running", "prompt empty
for 12s so not idle YET", "watch died on max_iterations but the target is
alive" -- none of which are comfortable to stage against a live tmux.

The deliberate bias throughout: **never reclaim on one weak signal**. A
worker at an empty prompt might be between turns. A quiet pane might be a
compile. Reclaiming a worker that is actually working destroys someone's
in-progress task, which is far worse than leaving a slot idle for another
minute.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# -- worker states ---------------------------------------------------------
# Wider than worker_registry's IDLE/BUSY/OFFLINE, which cannot express the
# difference between "quiet because it finished" and "quiet because it
# died" -- and that difference is the whole problem here.

RUNNING = "RUNNING"                 # producing output, or a child process is alive
IDLE_READY = "IDLE_READY"           # empty agent prompt, debounce elapsed: dispatchable
WAITING_INPUT = "WAITING_INPUT"     # blocked on a human (permission prompt, question)
STALLED = "STALLED"                 # confidently stuck; reclaimable under policy
BLOCKED = "BLOCKED"                 # cannot proceed for a declared reason
OFFLINE = "OFFLINE"                 # node unreachable / session gone
RESERVED = "RESERVED"               # deliberately held (hotfix lane, manual exclusion)
UNKNOWN = "UNKNOWN"                 # not yet classified -- must never be permanent

WORKER_STATES = (RUNNING, IDLE_READY, WAITING_INPUT, STALLED, BLOCKED, OFFLINE, RESERVED, UNKNOWN)

# States a scheduler may place new work on.
DISPATCHABLE_STATES = (IDLE_READY,)

# -- non-dispatch reason codes --------------------------------------------
# Persisted so "nothing happened" is always explainable after the fact.
# An empty reason is not allowed anywhere a dispatch was skipped.

NO_READY_WORK = "NO_READY_WORK"
NO_IDLE_WORKER = "NO_IDLE_WORKER"
DEPENDENCY_BLOCKED = "DEPENDENCY_BLOCKED"
CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
RESERVED_FOR_HOTFIX = "RESERVED_FOR_HOTFIX"
NODE_OFFLINE = "NODE_OFFLINE"
CONCURRENCY_LIMIT = "CONCURRENCY_LIMIT"
WORKTREE_CONFLICT = "WORKTREE_CONFLICT"
PERMISSION_DENIED = "PERMISSION_DENIED"
DISPATCH_DISABLED = "DISPATCH_DISABLED"
NON_DISPATCH_REASONS = (
    NO_READY_WORK, NO_IDLE_WORKER, DEPENDENCY_BLOCKED, CAPABILITY_MISMATCH,
    RESERVED_FOR_HOTFIX, NODE_OFFLINE, CONCURRENCY_LIMIT, WORKTREE_CONFLICT,
    PERMISSION_DENIED, DISPATCH_DISABLED,
)

# -- watch disable reasons -------------------------------------------------
# The production bug in one line: supervisor.py disables watches for seven
# reasons and calls set_enabled(..., True) from NOWHERE, so every one of
# them is permanent. A watch that hit max_iterations while its worker kept
# running never comes back, and the worker leaves scheduling visibility
# for good. Splitting the reasons is what lets reconciliation bring the
# recoverable ones back without ever overriding a human.

RECOVERABLE_DISABLE_REASONS = frozenset({
    "max_iterations_exceeded",      # a poll ceiling, not a verdict about the worker
    "target_missing",               # sessions come back (restart, re-create, rename)
    "same_failure_limit_exceeded",  # the failure may have been fixed since
    "access_denied_or_error",       # a grant may have been added since
    # Fleet reasons. A node that is down, restarting or being updated is the
    # most ordinary recoverable condition there is -- and the one that
    # previously cost the most, because a watch disabled while its node was
    # briefly unreachable never came back after the node did.
    "node_unreachable",
    "node_not_found",               # the node may be re-registered
    "ambiguous_target",             # one of the colliding sessions may end
})

# Never re-enabled automatically. A person said no; that stands until a
# person says otherwise.
INTENTIONAL_DISABLE_REASONS = frozenset({
    "manual_unwatch",
    "autonomous_completion_blocked_no_verifier",
    "autonomous_verification_failed",
})


# Controller/status error code -> the disable reason to record for it.
# Anything unlisted falls back to access_denied_or_error, which is itself
# recoverable, so an unknown error can never make a watch permanently dead --
# the failure mode this whole module exists to remove.
_STATUS_ERROR_DISABLE_REASONS = {
    "NODE_UNREACHABLE": "node_unreachable",
    "NODE_OFFLINE": "node_unreachable",
    "NODE_NOT_FOUND": "node_not_found",
    "SESSION_NOT_FOUND": "target_missing",
    "AMBIGUOUS_SESSION": "ambiguous_target",
}


def disable_reason_for_status_error(error: str | None) -> str:
    """Which disable reason describes this status error?

    The distinction that matters: "your node is not answering right now" and
    "you are not allowed to read this session" both used to be recorded as
    access_denied_or_error, so reconciliation could not tell a transient fleet
    outage from a revoked permission. Both are recoverable, but only one is
    expected to resolve on its own, and an operator reading
    `supervisor_status` deserves to see which one they have.

    An unrecognised error deliberately maps to a RECOVERABLE reason rather than
    an unknown one: a watch must never become permanently invisible because the
    fleet grew an error code this table has not met yet.
    """
    code = (error or "").strip().upper()
    if code in _STATUS_ERROR_DISABLE_REASONS:
        return _STATUS_ERROR_DISABLE_REASONS[code]
    for known, reason in _STATUS_ERROR_DISABLE_REASONS.items():
        if code.startswith(known):
            return reason
    return "access_denied_or_error"


@dataclass(frozen=True)
class WorkerSignals:
    """What a caller has observed about one worker. Every field is
    optional because the callers differ in what they can cheaply see, and
    a missing signal must weaken confidence rather than be read as zero."""
    session: str
    node_online: bool = True
    session_exists: bool = True
    seconds_since_output_change: float | None = None
    seconds_since_tmux_activity: float | None = None
    agent_prompt_empty: bool | None = None
    child_process_running: bool | None = None
    pending_shell_command: bool | None = None
    awaiting_human_input: bool = False
    reserved: bool = False
    blocked_reason: str | None = None
    output_hash: str | None = None


@dataclass(frozen=True)
class WorkerVerdict:
    state: str
    reason: str
    confidence: str                     # "high" | "low" -- reclaim requires high
    signals_considered: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "reason": self.reason, "confidence": self.confidence,
                "signals_considered": list(self.signals_considered)}


@dataclass(frozen=True)
class SchedulerThresholds:
    """Every number the classifier uses, in one place, all configurable --
    a hard-coded threshold here is a support burden on somebody else's
    fleet."""
    idle_debounce_seconds: float = 45.0      # 30-60s band from the task
    stale_after_seconds: float = 1800.0      # 30m of no progress -> suspicious
    hard_stale_after_seconds: float = 21600.0  # 6h -> stalled even with weak counter-signals
    refill_sla_seconds: float = 60.0
    unknown_probe_limit: int = 3


def classify_worker(signals: WorkerSignals,
                    thresholds: SchedulerThresholds | None = None) -> WorkerVerdict:
    """One worker's real state. Order matters: the cheapest and most
    certain facts are checked first, and every branch says which signals
    it used so a surprising verdict can be argued with."""
    thresholds = thresholds or SchedulerThresholds()
    used: list[str] = []

    if not signals.node_online:
        return WorkerVerdict(OFFLINE, "node is not reachable", "high", ("node_online",))
    if not signals.session_exists:
        return WorkerVerdict(OFFLINE, "session no longer exists", "high", ("session_exists",))
    if signals.reserved:
        return WorkerVerdict(RESERVED, "held out of scheduling deliberately", "high", ("reserved",))
    if signals.blocked_reason:
        return WorkerVerdict(BLOCKED, signals.blocked_reason, "high", ("blocked_reason",))
    if signals.awaiting_human_input:
        return WorkerVerdict(WAITING_INPUT, "waiting on a human answer", "high", ("awaiting_human_input",))

    # A live child process outranks every quietness signal. This is the
    # "compile that prints nothing for twenty minutes" case, and getting
    # it wrong means killing real work.
    if signals.child_process_running:
        used.append("child_process_running")
        quiet = signals.seconds_since_output_change
        if quiet is not None and quiet >= thresholds.hard_stale_after_seconds:
            used.append("seconds_since_output_change")
            return WorkerVerdict(
                STALLED,
                f"a child process is running but nothing has changed for {int(quiet)}s, "
                f"past the hard ceiling of {int(thresholds.hard_stale_after_seconds)}s",
                "high", tuple(used))
        return WorkerVerdict(RUNNING, "a child process is running", "high", tuple(used))

    if signals.pending_shell_command:
        used.append("pending_shell_command")
        return WorkerVerdict(RUNNING, "a shell command is pending", "high", tuple(used))

    # Empty agent prompt + debounce = dispatchable. The debounce is what
    # stops a worker being handed new work in the gap between two turns of
    # the one it is already doing.
    if signals.agent_prompt_empty:
        used.append("agent_prompt_empty")
        quiet = signals.seconds_since_output_change
        if quiet is None:
            return WorkerVerdict(UNKNOWN, "prompt looks empty but there is no activity signal to "
                                          "confirm it settled", "low", tuple(used))
        used.append("seconds_since_output_change")
        if quiet < thresholds.idle_debounce_seconds:
            return WorkerVerdict(
                RUNNING,
                f"prompt is empty but only for {int(quiet)}s "
                f"(debounce {int(thresholds.idle_debounce_seconds)}s) -- may be between turns",
                "high", tuple(used))
        return WorkerVerdict(IDLE_READY,
                             f"empty prompt, quiet for {int(quiet)}s", "high", tuple(used))

    quiet = signals.seconds_since_output_change
    activity = signals.seconds_since_tmux_activity
    if quiet is None and activity is None:
        return WorkerVerdict(UNKNOWN, "no activity signal available", "low", ())

    worst = max(x for x in (quiet, activity) if x is not None)
    used.extend(name for name, value in (("seconds_since_output_change", quiet),
                                         ("seconds_since_tmux_activity", activity)) if value is not None)
    if worst >= thresholds.hard_stale_after_seconds:
        return WorkerVerdict(STALLED,
                             f"no progress for {int(worst)}s, past the hard ceiling of "
                             f"{int(thresholds.hard_stale_after_seconds)}s", "high", tuple(used))
    if worst >= thresholds.stale_after_seconds:
        # Suspicious, but the prompt state is unknown -- so this is exactly
        # the case where a probe is owed before anything is reclaimed.
        return WorkerVerdict(UNKNOWN,
                             f"no progress for {int(worst)}s and the prompt state is unknown -- probe before "
                             f"reclaiming", "low", tuple(used))
    return WorkerVerdict(RUNNING, f"active within the last {int(worst)}s", "high", tuple(used))


def may_reclaim(verdict: WorkerVerdict) -> tuple[bool, str]:
    """Reclaim needs STALLED *and* high confidence. A low-confidence
    verdict means the classifier is telling you to probe, not to act."""
    if verdict.state != STALLED:
        return False, f"state is {verdict.state}, not {STALLED}"
    if verdict.confidence != "high":
        return False, "confidence is low -- probe before reclaiming"
    return True, verdict.reason


# -- watch reconciliation --------------------------------------------------

@dataclass(frozen=True)
class WatchRecovery:
    should_reenable: bool
    reason: str
    retry_after_seconds: float = 0.0


def watch_recovery_action(*, disabled_reason: str | None, target_alive: bool,
                          attempts: int = 0, seconds_since_disabled: float = 0.0,
                          base_backoff_seconds: float = 60.0,
                          max_attempts: int = 6) -> WatchRecovery:
    """May this disabled watch be brought back?

    The bug this exists for: every disable in supervisor.py is permanent,
    because nothing ever re-enables one. A worker whose watch hit
    max_iterations keeps working and keeps being invisible to scheduling
    forever after.

    Rules, in order:
      * a human's `manual_unwatch` is never overridden here
      * a dead target is not re-enabled -- there is nothing to watch yet,
        and reconciliation will look again next pass
      * exponential backoff, so a target that is alive but immediately
        re-fails does not spin
      * a bounded attempt count, after which it stays down and says so
    """
    if disabled_reason is None:
        return WatchRecovery(False, "watch is not disabled")
    if disabled_reason in INTENTIONAL_DISABLE_REASONS:
        return WatchRecovery(False, f"{disabled_reason} is a deliberate exclusion, not a fault")
    if disabled_reason not in RECOVERABLE_DISABLE_REASONS:
        return WatchRecovery(False, f"{disabled_reason} is not a known recoverable reason")
    if not target_alive:
        return WatchRecovery(False, "target is still missing")
    if attempts >= max_attempts:
        return WatchRecovery(False, f"already retried {attempts} times; leaving it disabled")
    wait = base_backoff_seconds * (2 ** attempts)
    if seconds_since_disabled < wait:
        return WatchRecovery(False, f"backing off, {int(wait - seconds_since_disabled)}s remaining", wait)
    return WatchRecovery(True, f"target is alive again and {disabled_reason} is recoverable")


# -- the refill invariant --------------------------------------------------

@dataclass(frozen=True)
class RefillDecision:
    """`dispatchable` pairs are what a caller should act on. `reason` is
    never empty when nothing is dispatchable -- "nothing happened and I
    cannot say why" is the failure mode this whole module exists to
    prevent."""
    dispatchable: tuple[tuple[str, str], ...] = ()   # (worker session, task id)
    reason: str | None = None
    ready_count: int = 0
    idle_count: int = 0
    blocked_reasons: dict[str, str] = field(default_factory=dict)

    @property
    def invariant_violated(self) -> bool:
        """READY work AND an idle worker AND nothing dispatched. True here
        means somebody owes an explanation."""
        return self.ready_count > 0 and self.idle_count > 0 and not self.dispatchable

    def to_dict(self) -> dict[str, Any]:
        return {"dispatchable": [list(pair) for pair in self.dispatchable],
                "reason": self.reason, "ready_count": self.ready_count,
                "idle_count": self.idle_count, "blocked_reasons": dict(self.blocked_reasons),
                "invariant_violated": self.invariant_violated}


def evaluate_refill(*, workers: dict[str, WorkerVerdict], ready_tasks: list[dict[str, Any]],
                    compatible: Any = None, dispatch_enabled: bool = True,
                    max_dispatch: int | None = None) -> RefillDecision:
    """Pair idle workers with READY tasks, or say why not.

    `compatible(session, task) -> (bool, reason_code)` is injected: capability
    matching, worktree conflicts, permissions and reservations all live in
    the caller's world, and a pure function has no business guessing at
    them. Refusing without a reason code is not possible -- the callback
    must supply one.

    One task per worker and one worker per task within a pass, so a single
    refill can never double-book either side. Cross-pass safety is the
    lease's job, not this function's.
    """
    idle = [session for session, verdict in sorted(workers.items()) if verdict.state in DISPATCHABLE_STATES]
    ready_count = len(ready_tasks)
    idle_count = len(idle)

    if not dispatch_enabled:
        return RefillDecision(reason=DISPATCH_DISABLED, ready_count=ready_count, idle_count=idle_count)
    if ready_count == 0:
        return RefillDecision(reason=NO_READY_WORK, ready_count=0, idle_count=idle_count)
    if idle_count == 0:
        return RefillDecision(reason=NO_IDLE_WORKER, ready_count=ready_count, idle_count=0)

    pairs: list[tuple[str, str]] = []
    blocked: dict[str, str] = {}
    taken: set[str] = set()
    limit = max_dispatch if max_dispatch is not None else len(idle)

    for session in idle:
        if len(pairs) >= limit:
            blocked.setdefault(session, CONCURRENCY_LIMIT)
            continue
        placed = False
        for task in ready_tasks:
            task_id = str(task.get("id") or task.get("task_id") or "")
            if not task_id or task_id in taken:
                continue
            if compatible is None:
                ok, why = True, None
            else:
                ok, why = compatible(session, task)
            if ok:
                pairs.append((session, task_id))
                taken.add(task_id)
                placed = True
                break
            blocked[session] = why or CAPABILITY_MISMATCH
        if not placed:
            blocked.setdefault(session, CAPABILITY_MISMATCH)

    reason = None if pairs else (
        # Every idle worker was refused; surface the reason they agreed on
        # rather than a generic one.
        next(iter(set(blocked.values()))) if len(set(blocked.values())) == 1
        else CAPABILITY_MISMATCH if blocked else NO_READY_WORK
    )
    return RefillDecision(dispatchable=tuple(pairs), reason=reason,
                          ready_count=ready_count, idle_count=idle_count, blocked_reasons=blocked)


def utilization_snapshot(workers: dict[str, WorkerVerdict], *, ready_count: int,
                         last_dispatch_at: str | None = None,
                         idle_with_ready_since: str | None = None,
                         reclaimed_count: int = 0) -> dict[str, Any]:
    """The numbers that answer "why is the fleet idle" without reading a
    log. Counts every state, not only the interesting ones, so a worker
    can never vanish from the accounting."""
    counts = {state: 0 for state in WORKER_STATES}
    for verdict in workers.values():
        counts[verdict.state] = counts.get(verdict.state, 0) + 1
    usable = len(workers) - counts[OFFLINE]
    busy = counts[RUNNING] + counts[WAITING_INPUT]
    return {
        "total_workers": len(workers),
        "usable_workers": usable,
        "counts": counts,
        "ready_tasks": ready_count,
        "utilization_percent": round(100.0 * busy / usable, 1) if usable else 0.0,
        "idle_with_ready_work": counts[IDLE_READY] > 0 and ready_count > 0,
        "idle_with_ready_since": idle_with_ready_since,
        "last_dispatch_at": last_dispatch_at,
        "reclaimed_stalled_count": reclaimed_count,
        "per_worker": {session: verdict.to_dict() for session, verdict in sorted(workers.items())},
    }
