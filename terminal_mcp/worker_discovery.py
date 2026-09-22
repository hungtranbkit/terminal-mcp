"""Phase 2 -- automatic worker declaration and work-conserving dispatch
(backlog `blg_orch_no_workers_declared`).

WHAT THE EVIDENCE SHOWED. `pm_service._candidates()` builds its candidate list
from exactly one source: `pm_store.list_capabilities()`. That table has **zero
production rows**, and the only thing that ever writes it is a human calling
`terminal_pm_upsert_capability`. So `pm_router.route_task` returns
NO_ELIGIBLE_WORKER for every task, always -- not because routing is wrong, but
because it is asked to choose from an empty set.

Measured on this host, 2026-09-14: `session_records` holds 5 ACTIVE sessions,
four of them `agent_type=claude` with `input_granted=1`, while
`capability_profiles` holds 0 rows. Four usable workers were sitting idle and
invisible. Phase 1's reconciliation cannot fix this, for the reason phase 1
itself reported: reconciliation repairs a worker that is *represented* and has
gone wrong, and these workers were never represented at all.

WHY DISCOVERY IS SEPARATE FROM ROUTING. This module decides only "should this
session be represented as a worker at all", and writes a capability profile if
so. It never chooses a worker for a task, never scores, never sends anything to
a session. `pm_router` keeps deciding routing and `hard_gate_failure` keeps
being the only eligibility gate, so capability/node matching is untouched: a
discovered profile is an ordinary candidate that must still pass every existing
constraint. Discovery widens the candidate set; it does not weaken the filter.

DECLARED vs DETECTED IS PRESERVED (worker_registry.py's rule). Everything this
module writes is *detected* -- derived from the session registry and the node
registry, both of which observe the real runtime. It never invents a skill from
a display name, and it never overwrites a human's declaration: the write goes
through `PMStore.upsert_discovered_capability`, which refuses to touch any row
not marked `source=auto`.

WHAT IT DELIBERATELY WILL NOT DECLARE. Auto-discovery assigns `role=WORKER` and
nothing else. It will not auto-declare VERIFIER, INTEGRATOR or DEPLOYER: those
are trust positions, and "this tmux session exists and accepts input" is not
evidence that it may verify or deploy anything. The backlog item's third
acceptance criterion (a non-zero VERIFIER count) therefore still needs an
explicit human declaration or a separate probe -- see the module's report in
`docs/` and the backlog note rather than reading a VERIFIER count into this.

SAFETY POSTURE. Off by default (`WorkerDiscoveryPolicy.enabled=False`), pure
decision logic separated from I/O, every refusal typed and counted, and no
session is ever declared on partial information -- an unknown is a refusal, not
a default. `evaluate_session` is a pure function over a plain dict so that every
rule below is testable without a database, a node, or a tmux server.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .pm_store import SOURCE_AUTO
from .worker_registry import ROLE_WORKER

_LOGGER = logging.getLogger(__name__)

#: Session registry status that means "this session is really there now".
#: Every other status (MISSING/OFFLINE/KILLED/DELETED) is a refusal.
STATUS_ACTIVE = "ACTIVE"

# -- refusal reasons ---------------------------------------------------------
# A closed set, because "why was this session not declared" is the question an
# operator asks when capacity looks idle, and a free-text answer cannot be
# counted. Each is also a test name below.
SKIP_NOT_ACTIVE = "SESSION_NOT_ACTIVE"
SKIP_NO_INPUT_GRANT = "NO_INPUT_GRANT"
SKIP_NO_READ_GRANT = "NO_READ_GRANT"
SKIP_AGENT_TYPE_NOT_ELIGIBLE = "AGENT_TYPE_NOT_ELIGIBLE"
SKIP_NODE_OFFLINE = "NODE_OFFLINE"
SKIP_STALE_LAST_SEEN = "STALE_LAST_SEEN"
SKIP_RECOVERY_IN_PROGRESS = "RECOVERY_IN_PROGRESS"
SKIP_MISSING_IDENTITY = "MISSING_IDENTITY"
SKIP_DISCOVERY_DISABLED = "DISCOVERY_DISABLED"

ALL_SKIP_REASONS: tuple[str, ...] = (
    SKIP_NOT_ACTIVE, SKIP_NO_INPUT_GRANT, SKIP_NO_READ_GRANT,
    SKIP_AGENT_TYPE_NOT_ELIGIBLE, SKIP_NODE_OFFLINE, SKIP_STALE_LAST_SEEN,
    SKIP_RECOVERY_IN_PROGRESS, SKIP_MISSING_IDENTITY, SKIP_DISCOVERY_DISABLED,
)

#: Agent types that can actually be handed a task. A `shell` session is a real,
#: live, input-granted session that is NOT an agent -- sending it a task prompt
#: would type prose into a bash prompt. Measured on this host: `test-http-secure`
#: is exactly that row, ACTIVE with input_granted=0 and agent_type=shell, and it
#: is the reason this is an allow-list rather than a deny-list.
DEFAULT_ELIGIBLE_AGENT_TYPES: tuple[str, ...] = ("claude", "codex")

#: A session whose last_seen_at is older than this is not trusted as live even
#: if its stored status still says ACTIVE -- the registry is updated by polling,
#: so a crashed poller leaves stale ACTIVE rows behind.
DEFAULT_MAX_LAST_SEEN_AGE_SECONDS = 900.0


@dataclass(frozen=True)
class WorkerDiscoveryPolicy:
    """Off by default. Turning discovery on is a deliberate, reviewable act,
    exactly like `queue_lanes.auto_dispatch_enabled` -- this project does not
    switch on automatic behaviour against a live fleet by shipping it."""

    enabled: bool = False
    eligible_agent_types: tuple[str, ...] = DEFAULT_ELIGIBLE_AGENT_TYPES
    max_last_seen_age_seconds: float = DEFAULT_MAX_LAST_SEEN_AGE_SECONDS
    require_input_grant: bool = True
    require_read_grant: bool = True
    #: Auto-declared profiles get a WIP limit so that discovering N workers can
    #: never turn into N unbounded queues. None would mean unbounded.
    max_queued: int | None = 1


DEFAULT_POLICY = WorkerDiscoveryPolicy()


@dataclass(frozen=True)
class DiscoveryDecision:
    """`declare=True` carries `fields` (the kwargs for a capability profile);
    `declare=False` carries `reason`, always one of ALL_SKIP_REASONS."""

    declare: bool
    reason: str = ""
    fields: dict[str, Any] = field(default_factory=dict)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_seconds(value: Any, now: datetime) -> float | None:
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    return (now - parsed).total_seconds()


def derive_skills(agent_type: str | None, node_tools: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Detected skills only, each tagged with where it came from.

    The agent type is observed by the session registry (the launcher that was
    actually run), and node tools are probed by `capability_probe.py`. Both are
    evidence. Nothing here is inferred from a session's name.
    """
    skills: list[dict[str, Any]] = []
    if agent_type:
        skills.append({"name": str(agent_type).casefold(), "confidence": 1.0, "source": "session_registry"})
    for tool in node_tools:
        name = str(tool).casefold()
        if name and name != (agent_type or "").casefold():
            skills.append({"name": name, "confidence": 1.0, "source": "node_probe"})
    return skills


def evaluate_session(record: dict[str, Any], *, node_online: bool = True,
                     node_os: str | None = None, node_tools: Sequence[str] = (),
                     now: datetime | None = None,
                     policy: WorkerDiscoveryPolicy = DEFAULT_POLICY) -> DiscoveryDecision:
    """Pure. `record` is a plain dict shaped like `SessionRecord` -- the real
    caller passes `dataclasses.asdict(session_record)`, a test passes a literal.

    Every refusal below is a deliberate one; in particular an *absent* signal is
    a refusal rather than a default, because the cost of wrongly declaring a
    worker is a task dispatched into a session that cannot run it.
    """
    if not policy.enabled:
        return DiscoveryDecision(False, SKIP_DISCOVERY_DISABLED)

    node_id = record.get("node_id")
    session = record.get("session_name") or record.get("session")
    if not node_id or not session:
        # Without both halves of the composite identity there is nothing to
        # key a profile on, and guessing either one would produce a profile
        # that routes work to the wrong machine.
        return DiscoveryDecision(False, SKIP_MISSING_IDENTITY)

    if record.get("status") != STATUS_ACTIVE:
        return DiscoveryDecision(False, SKIP_NOT_ACTIVE)

    if not node_online:
        return DiscoveryDecision(False, SKIP_NODE_OFFLINE)

    # A session mid-recovery is by definition not in a known-good state; it may
    # be about to be restarted under it. Phase 1 owns that lane -- discovery
    # stays out of it rather than racing it.
    recovery_state = record.get("recovery_state")
    if recovery_state and str(recovery_state).upper() not in ("", "NONE", "IDLE", "OK", "RECOVERED"):
        return DiscoveryDecision(False, SKIP_RECOVERY_IN_PROGRESS)

    agent_type = record.get("agent_type")
    eligible = {t.casefold() for t in policy.eligible_agent_types}
    if not agent_type or str(agent_type).casefold() not in eligible:
        return DiscoveryDecision(False, SKIP_AGENT_TYPE_NOT_ELIGIBLE)

    if policy.require_input_grant and not record.get("input_granted"):
        # Goal 5, and the one that matters most: a session we may not type into
        # must never be handed a task. `input_granted` is the same effective
        # permission the real send path enforces, so this cannot drift from it.
        return DiscoveryDecision(False, SKIP_NO_INPUT_GRANT)
    if policy.require_read_grant and not record.get("read_granted"):
        # Without read permission the dispatcher could send but never observe
        # the result -- submission confirmation would be unverifiable.
        return DiscoveryDecision(False, SKIP_NO_READ_GRANT)

    now = now or datetime.now(timezone.utc)
    age = _age_seconds(record.get("last_seen_at"), now)
    if age is None or age > policy.max_last_seen_age_seconds:
        # Unparseable timestamp counts as stale, not as fresh.
        return DiscoveryDecision(False, SKIP_STALE_LAST_SEEN)

    return DiscoveryDecision(True, fields={
        "os": node_os or record.get("backend_type") or None,
        "runtime_tools": [str(t).casefold() for t in node_tools],
        "project_affinity": record.get("repo_root") or record.get("git_remote") or None,
        # WORKER only -- never an auto-declared VERIFIER/INTEGRATOR/DEPLOYER.
        # See this module's docstring.
        "role": ROLE_WORKER,
        "skills": derive_skills(agent_type, node_tools),
        "max_queued": policy.max_queued,
        "permissions_note": f"auto-discovered from session_registry ({agent_type})",
    })


# -- watch coverage ----------------------------------------------------------
# A declared worker that nothing watches is still invisible to the supervisor,
# so discovery also ensures a watch exists. The rule is CREATE-ONLY, and the
# reason is specific rather than stylistic.
#
# `SupervisorStore.upsert_watch` on an EXISTING row does three things besides
# creating it: it sets `enabled = 1, disabled_reason = NULL`, it re-pins the
# session/pane identity, and it mints a fresh completion nonce while bumping
# `completion_attempt`. All three are correct for a human re-issuing
# `supervisor_watch` -- that is the explicit "treat whatever answers to this
# name now as correct" action. All three are wrong for an unattended loop:
#
#   * it would silently re-enable watches an operator or phase-1 recovery
#     deliberately disabled, every tick, forever;
#   * it would re-pin identity continuously, destroying the P0-2 guarantee
#     that a watch is bound to the session it was pinned to;
#   * it would invalidate the outstanding completion nonce on every pass,
#     breaking exactly the submission/completion-confirmation semantics this
#     work is required to preserve.
#
# So discovery reads the existing watch keys first and only creates what is
# genuinely absent. An existing watch -- enabled or not -- is reported and left
# completely alone.
WATCH_CREATED = "WATCH_CREATED"
WATCH_EXISTS = "WATCH_EXISTS"
WATCH_REFUSED = "WATCH_REFUSED"

#: Watch states meaning the worker is not going to progress by itself. Idle
#: capacity in one of these is NOT available capacity, and it is not silently
#: idle either -- it is surfaced as actionable (requirement 6).
STALLED_WATCH_STATES: tuple[str, ...] = ("WAITING_INPUT", "ERROR", "BLOCKED", "FAILED")

#: Watch states meaning the worker is genuinely free to take work.
FREE_WATCH_STATES: tuple[str, ...] = ("IDLE", "UNKNOWN", "VERIFIED_DONE")


def classify_watch_states(watches: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Split watched targets into free / stalled / busy, so that "the fleet
    looks idle" can always be answered with which kind of idle it is.

    A stalled worker (WAITING_INPUT above all) is the failure this exists to
    make visible: it holds capacity, it will never finish on its own, and
    without this it is indistinguishable from a worker that is merely between
    tasks. Each stalled entry carries the state and how long it has been in it,
    which is what makes it actionable rather than merely reported.
    """
    free: list[str] = []
    stalled: list[dict[str, Any]] = []
    busy: list[str] = []
    for watch in watches:
        target = str(watch.get("target") or watch.get("session") or "?")
        state = str(watch.get("state") or "UNKNOWN").upper()
        if not watch.get("enabled", True):
            continue  # a disabled watch is phase-1/operator territory, not capacity
        if state in STALLED_WATCH_STATES:
            stalled.append({"target": target, "state": state,
                            "since": watch.get("state_since"),
                            "action": _stall_action(state)})
        elif state in FREE_WATCH_STATES:
            free.append(target)
        else:
            busy.append(target)
    return {"free": sorted(free), "busy": sorted(busy),
            "stalled": sorted(stalled, key=lambda s: s["target"]),
            "stalled_count": len(stalled)}


def _stall_action(state: str) -> str:
    """What a human or the recovery engine should actually do. Deliberately
    names the existing mechanism rather than inventing a new one -- recovery
    already exists (`recovery_engine.py`) and phase 1 owns it."""
    if state == "WAITING_INPUT":
        return "answer or clear the prompt (supervisor attention_required); worker cannot self-advance"
    if state == "ERROR":
        return "inspect the pane and reset; candidate for recovery_engine"
    if state == "BLOCKED":
        return "verification blocked -- resolve the blocker or re-queue the task"
    return "verification failed -- requeue or reassign the task"


@dataclass
class ReconcileReport:
    """What one bounded reconcile pass actually did. `skipped` is counted by
    reason so that "capacity looks idle" is always answerable."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped_declared: list[str] = field(default_factory=list)
    skipped: dict[str, list[str]] = field(default_factory=dict)
    considered: int = 0
    watches: dict[str, list[str]] = field(default_factory=dict)

    def skip(self, key: str, reason: str) -> None:
        self.skipped.setdefault(reason, []).append(key)

    def watch(self, session: str, outcome: str) -> None:
        self.watches.setdefault(outcome, []).append(session)

    def to_dict(self) -> dict[str, Any]:
        return {
            "considered": self.considered,
            "created": sorted(self.created),
            "updated": sorted(self.updated),
            "skipped_declared": sorted(self.skipped_declared),
            "skipped": {reason: sorted(keys) for reason, keys in sorted(self.skipped.items())},
            "skipped_counts": {reason: len(keys) for reason, keys in sorted(self.skipped.items())},
            "declared_total": len(self.created) + len(self.updated),
            "watches": {outcome: sorted(s) for outcome, s in sorted(self.watches.items())},
            "watch_counts": {outcome: len(s) for outcome, s in sorted(self.watches.items())},
        }


class WorkerDiscoveryService:
    """Composes the session registry, the node registry and the PM store. Owns
    no state of its own -- the same posture as `worker_registry.py`, and for
    the same reason: a fourth store would immediately start drifting from the
    three that already hold this information correctly.

    `reconcile()` is bounded (one pass over the registry's ACTIVE rows),
    idempotent (keyed on `(node_id, session)`, and a second identical pass
    produces no change), and safe to run concurrently with itself: every write
    goes through `upsert_discovered_capability`, which re-reads the stored row
    inside the write and refuses anything it does not own, so two racing
    reconcilers converge on the same rows rather than fighting over them.
    """

    def __init__(self, session_registry: Any, pm_store: Any, *, controller: Any | None = None,
                 supervisor: Any | None = None,
                 policy: WorkerDiscoveryPolicy = DEFAULT_POLICY) -> None:
        self.session_registry = session_registry
        self.pm_store = pm_store
        self.controller = controller
        self.supervisor = supervisor
        self.policy = policy

    # -- watches ------------------------------------------------------------

    def _existing_watch_targets(self) -> set[str]:
        """One query per pass, not one per worker."""
        if self.supervisor is None:
            return set()
        try:
            listing = self.supervisor.list_watches()
        except Exception:  # noqa: BLE001 -- an unreadable supervisor must not break declaration
            return set()
        rows = listing.get("watches", listing) if isinstance(listing, dict) else listing
        targets = set()
        for row in rows or ():
            if str(row.get("kind", "session")) == "session":
                targets.add(str(row.get("target") or row.get("session") or ""))
        return targets

    def _ensure_watch(self, session: str, existing: set[str], report: ReconcileReport) -> None:
        """CREATE-ONLY -- see the WATCH_* block above for why re-upserting an
        existing watch on every tick would re-enable disabled watches, re-pin
        identity, and invalidate the outstanding completion nonce."""
        if self.supervisor is None:
            return
        if session in existing:
            report.watch(session, WATCH_EXISTS)
            return
        try:
            result = self.supervisor.watch(session=session, source="auto-discovery")
        except Exception as exc:  # noqa: BLE001 -- one bad session never aborts the pass
            report.watch(session, WATCH_REFUSED)
            _LOGGER.warning("worker-discovery: watch(%r) failed: %s", session, exc)
            return
        if isinstance(result, dict) and result.get("error"):
            # ACCESS_DENIED is the expected, correct refusal for a session
            # outside the read whitelist -- recorded, never retried blindly.
            report.watch(session, WATCH_REFUSED)
            return
        report.watch(session, WATCH_CREATED)
        existing.add(session)

    # -- node facts ---------------------------------------------------------

    def _node_facts(self) -> dict[str, dict[str, Any]]:
        """`{node_id: {"online", "os", "tools"}}`. Best-effort: a controller
        that cannot be reached yields an empty map, and callers then treat a
        node as online (matching `pm_service._node_online`'s own posture --
        never hard-fail routing on a registry hiccup alone)."""
        if self.controller is None:
            return {}
        try:
            nodes = self.controller.list_nodes()
        except Exception:  # noqa: BLE001 -- best-effort enrichment, never blocks discovery
            return {}
        facts: dict[str, dict[str, Any]] = {}
        for node in nodes:
            node_id = getattr(node, "id", None) or getattr(node, "node_id", None)
            if not node_id:
                continue
            tools = getattr(node, "runtime_tools", None) or getattr(node, "labels", None) or ()
            facts[str(node_id)] = {
                "online": getattr(node, "status", None) != "offline",
                "os": getattr(node, "platform", None) or getattr(node, "os", None),
                "tools": tuple(str(t) for t in tools) if isinstance(tools, (list, tuple, set)) else (),
            }
        return facts

    # -- reconcile ----------------------------------------------------------

    def reconcile(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Declare every eligible live session, idempotently. Returns a
        `ReconcileReport` dict. Writes nothing and sends nothing when the
        policy is disabled -- the report still explains why."""
        report = ReconcileReport()
        now = now or datetime.now(timezone.utc)
        facts = self._node_facts() if self.policy.enabled else {}
        existing_watches = self._existing_watch_targets() if self.policy.enabled else set()

        for record in self._active_records():
            report.considered += 1
            node_id = record.get("node_id") or "?"
            session = record.get("session_name") or record.get("session") or "?"
            key = f"{node_id}/{session}"
            node = facts.get(str(node_id), {})
            decision = evaluate_session(
                record,
                node_online=bool(node.get("online", True)),
                node_os=node.get("os"),
                node_tools=node.get("tools", ()),
                now=now, policy=self.policy,
            )
            if not decision.declare:
                report.skip(key, decision.reason)
                continue
            outcome, _profile = self.pm_store.upsert_discovered_capability(
                node_id, session, **decision.fields)
            if outcome == self.pm_store.DISCOVERY_CREATED:
                report.created.append(key)
            elif outcome == self.pm_store.DISCOVERY_UPDATED:
                report.updated.append(key)
            else:
                report.skipped_declared.append(key)
            # A declared worker nothing watches is still invisible to the
            # supervisor, so the watch is ensured for every eligible session --
            # including one whose profile a human already declared.
            self._ensure_watch(str(session), existing_watches, report)
        return report.to_dict()

    # -- work-conserving dispatch -------------------------------------------

    def dispatch(self, *, pm_service: Any, tasks: Sequence[dict[str, Any]],
                 reserve_workers: int = 0, apply: bool = False,
                 now: datetime | None = None) -> dict[str, Any]:
        """Declare, then make sure no compatible capacity sits idle.

        `apply=False` (the default) is a dry run: it reports what WOULD be
        assigned and changes nothing. `apply=True` assigns through
        `PMService.route_task(mode="AUTO")`, which records the decision and
        calls the existing `QueueService.assign_task`.

        THIS METHOD NEVER SENDS ANYTHING. Assignment puts a task in a lane;
        the actual keystroke still goes through `queue_engine._dispatch` and
        its guarded send / submission-confirmation path, entirely unchanged.
        Duplicate claims are likewise prevented where they already were --
        `claim_next_task`'s `BEGIN IMMEDIATE` + claim token -- rather than by
        anything invented here. A second dispatch pass over the same task is
        harmless because the task is no longer READY once assigned.
        """
        reconcile = self.reconcile(now=now)
        candidates = pm_service._candidates()
        report = coverage(tasks, candidates, reserve_workers=reserve_workers)
        watch_states = classify_watch_states(self._watch_rows())

        assigned: list[dict[str, Any]] = []
        if apply:
            for entry in report["assignable"]:
                result = pm_service.route_task(entry["task_id"], mode="AUTO")
                assigned.append({"task_id": entry["task_id"],
                                 "status": (result.get("decision") or {}).get("status"),
                                 "assign_result": result.get("assign_result")})
        return {
            "reconcile": reconcile,
            "coverage": report,
            "watch_states": watch_states,
            "applied": apply,
            "assigned": assigned,
            # Requirement 6: a stalled worker holds capacity and will never
            # finish on its own. Surfacing it beside the coverage numbers is
            # what stops "the fleet is idle" from being the end of the story.
            "attention_required": watch_states["stalled"],
        }

    def _watch_rows(self) -> list[dict[str, Any]]:
        if self.supervisor is None:
            return []
        try:
            listing = self.supervisor.list_watches()
        except Exception:  # noqa: BLE001
            return []
        rows = listing.get("watches", listing) if isinstance(listing, dict) else listing
        return [r for r in (rows or ()) if isinstance(r, dict)]

    def _active_records(self) -> Iterable[dict[str, Any]]:
        """Bounded: only rows the registry already considers ACTIVE. On this
        host that is 5 of 304 rows, so a reconcile pass never walks the whole
        historical registry."""
        try:
            records = self.session_registry.list(statuses=(STATUS_ACTIVE,))
        except TypeError:
            records = self.session_registry.list()
        for record in records:
            yield record if isinstance(record, dict) else _record_to_dict(record)


def _record_to_dict(record: Any) -> dict[str, Any]:
    """`SessionRecord` -> plain dict, without importing dataclasses at every
    call site and without assuming the record is a dataclass at all."""
    if hasattr(record, "__dataclass_fields__"):
        return {name: getattr(record, name) for name in record.__dataclass_fields__}
    return {name: getattr(record, name) for name in dir(record)
            if not name.startswith("_") and not callable(getattr(record, name, None))}


__all__ = [
    "ALL_SKIP_REASONS", "DEFAULT_ELIGIBLE_AGENT_TYPES", "DEFAULT_POLICY",
    "DiscoveryDecision", "ReconcileReport", "SOURCE_AUTO", "WorkerDiscoveryPolicy",
    "WorkerDiscoveryService", "derive_skills", "evaluate_session",
    "SKIP_AGENT_TYPE_NOT_ELIGIBLE", "SKIP_DISCOVERY_DISABLED", "SKIP_MISSING_IDENTITY",
    "SKIP_NODE_OFFLINE", "SKIP_NOT_ACTIVE", "SKIP_NO_INPUT_GRANT", "SKIP_NO_READ_GRANT",
    "SKIP_RECOVERY_IN_PROGRESS", "SKIP_STALE_LAST_SEEN",
    "ALL_COVERAGE_REASONS", "BUSY_STATUSES", "READY_STATUSES", "coverage",
    "SKIP_ALL_WORKERS_AT_WIP", "SKIP_NO_ELIGIBLE_WORKER", "SKIP_RESERVED_CAPACITY",
    "SKIP_TASK_BLOCKED", "SKIP_TASK_HAS_DEPENDENCY",
    "FREE_WATCH_STATES", "STALLED_WATCH_STATES", "WATCH_CREATED", "WATCH_EXISTS",
    "WATCH_REFUSED", "classify_watch_states",
]


# ---------------------------------------------------------------------------
# Work-conserving dispatch coverage (goals 2 and 7)
# ---------------------------------------------------------------------------
# "Work-conserving" here means exactly one thing: if a READY task exists and a
# compatible worker is idle, capacity must not stay idle unless something
# explicitly says it should. The something is enumerated below -- a dependency,
# a blocker, a WIP/reserve limit, or a real capability mismatch -- and idle
# capacity that matches NONE of those is a defect to be surfaced, not absorbed.
#
# This deliberately reuses `pm_router.hard_gate_failure` rather than
# reimplementing eligibility. A second copy of the matching rules would drift
# from the router's, and the report would then confidently explain a dispatch
# decision that never happened that way.

#: Why a READY task was intentionally left unassigned.
SKIP_NO_ELIGIBLE_WORKER = "NO_ELIGIBLE_WORKER"
SKIP_ALL_WORKERS_AT_WIP = "ALL_WORKERS_AT_WIP"
SKIP_TASK_HAS_DEPENDENCY = "TASK_HAS_DEPENDENCY"
SKIP_TASK_BLOCKED = "TASK_BLOCKED"
SKIP_RESERVED_CAPACITY = "RESERVED_CAPACITY"

ALL_COVERAGE_REASONS: tuple[str, ...] = (
    SKIP_NO_ELIGIBLE_WORKER, SKIP_ALL_WORKERS_AT_WIP, SKIP_TASK_HAS_DEPENDENCY,
    SKIP_TASK_BLOCKED, SKIP_RESERVED_CAPACITY,
)

#: Task statuses that mean "this is waiting for a worker right now".
READY_STATUSES: tuple[str, ...] = ("UNASSIGNED", "QUEUED", "READY")

#: Statuses that mean a worker is busy with something.
BUSY_STATUSES: tuple[str, ...] = ("PRECHECK", "READY", "DISPATCHING", "RUNNING", "VERIFYING")


def _is_capacity_only_failure(task: dict[str, Any], candidate: Any, hard_gate: Any) -> bool:
    """True when the ONLY thing standing between this candidate and this task is
    that the candidate is currently full.

    Asked by re-running the real gate against the same candidate with its queue
    drained, so the answer always matches whatever the router actually enforces.
    A candidate that is not a dataclass (a test double, say) simply reports
    False rather than raising -- an unknown is never reported as capacity.
    """
    try:
        drained = replace(candidate, queue_depth=0)
    except TypeError:
        return False
    return hard_gate(task, candidate) is not None and hard_gate(task, drained) is None


def coverage(tasks: Sequence[dict[str, Any]], candidates: Sequence[Any], *,
             reserve_workers: int = 0, hard_gate: Any = None) -> dict[str, Any]:
    """Pure. Answers "is capacity being wasted, and if not, why not".

    `tasks` are READY-ish task dicts; `candidates` are `pm_router.WorkerCandidate`
    objects already carrying live `online`/`queue_depth`/`max_queued`.
    `reserve_workers` is an explicit policy reserve -- capacity deliberately held
    back, which is a legitimate reason to stay idle and is reported as such
    rather than counted as a mismatch.

    `ready_idle_mismatch` is True only when there is a READY task, an available
    worker, and **no** stated reason keeping them apart. That is the alarm
    condition: the dispatcher left capacity idle for a reason nobody wrote down.
    """
    if hard_gate is None:  # imported lazily so this module stays importable alone
        from .pm_router import hard_gate_failure as hard_gate

    idle = [c for c in candidates
            if getattr(c, "online", True)
            and getattr(c, "permissions_ok", True)
            and (getattr(c, "max_queued", None) is None
                 or getattr(c, "queue_depth", 0) < getattr(c, "max_queued"))]
    available = max(0, len(idle) - max(0, reserve_workers))

    assignable: list[dict[str, Any]] = []
    skipped: dict[str, list[str]] = {}

    def _skip(task_id: str, reason: str) -> None:
        skipped.setdefault(reason, []).append(task_id)

    for task in tasks:
        task_id = str(task.get("id", "?"))
        if task.get("depends_on"):
            _skip(task_id, SKIP_TASK_HAS_DEPENDENCY)
            continue
        if task.get("status") == "BLOCKED" or task.get("blocked_reason"):
            _skip(task_id, SKIP_TASK_BLOCKED)
            continue
        eligible = [c for c in candidates if hard_gate(task, c) is None]
        if not eligible:
            # `hard_gate_failure` treats a reached WIP limit as a hard failure,
            # so "busy" and "cannot do this work" arrive here as the same
            # verdict. They are not the same thing to an operator: the first
            # resolves itself in minutes, the second never will. Distinguish
            # them by re-asking the REAL gate what it would say if the worker
            # were free -- never by parsing its reason string, which would
            # silently start lying the day that wording changes.
            would_fit = [c for c in candidates
                         if _is_capacity_only_failure(task, c, hard_gate)]
            _skip(task_id, SKIP_ALL_WORKERS_AT_WIP if would_fit else SKIP_NO_ELIGIBLE_WORKER)
            continue
        eligible_idle = [c for c in eligible if c in idle]
        if not eligible_idle:
            _skip(task_id, SKIP_ALL_WORKERS_AT_WIP)
            continue
        if available <= 0:
            _skip(task_id, SKIP_RESERVED_CAPACITY)
            continue
        assignable.append({"task_id": task_id, "candidates": [c.key() for c in eligible_idle]})

    ready = len(tasks)
    mismatch = bool(ready and available > 0 and not assignable and not skipped)
    return {
        "ready_tasks": ready,
        "idle_workers": len(idle),
        "available_workers": available,
        "reserved_workers": max(0, reserve_workers),
        "assignable": assignable,
        "skipped": {reason: sorted(ids) for reason, ids in sorted(skipped.items())},
        "skipped_counts": {reason: len(ids) for reason, ids in sorted(skipped.items())},
        # The headline an operator actually reads.
        "ready_idle_mismatch": mismatch,
        "summary": (f"{ready} ready / {len(idle)} idle ({available} available after reserve) "
                    f"-> {len(assignable)} assignable"),
    }
