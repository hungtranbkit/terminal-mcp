"""A whole-system operational snapshot: who is working, on what, and what is idle.

WHAT THIS IS FOR

Capacity management across every node, not one project's progress. The question
it has to answer in ten seconds is "is anything sitting idle while work is
waiting, and can I prove it" -- which means every number here must be traceable
to something that was measured.

THE RULE THAT SHAPES EVERY LINE

A number we do not have is reported as not having it. `work_telemetry.py`
states the same rule for token counts, and for the same reason: a
plausible-looking estimate presented as a measurement corrupts every decision
made from it afterwards, including the decision about whether the system is
working at all. So:

  * `progress_percent` is `null` unless something countable backs it, and
    `progress_basis` always names what that was.
  * An ambiguous session is `UNKNOWN` or `STALE`, never `IDLE`. Reporting
    "idle" from absent evidence is the exact failure this feature exists to
    prevent -- a detached session running an agent is busy, and no-recent-output
    is not evidence of anything on its own.
  * A node that could not be reached is `OFFLINE` with its staleness age, and
    leaves the utilization denominator entirely. A machine that is down is not
    a machine being wasted.

PURE ON PURPOSE

No I/O. Every input is handed in by the caller, which has already listed
sessions, nodes and lanes for its own reasons. That makes the whole rule set
exhaustively unit-testable, and it means one unreachable node degrades a field
rather than failing a report.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = 1
WINDOW_MINUTES = 15

# -- session states ----------------------------------------------------------
# BUSY and RUNNING_MANUAL both mean occupied; they differ only in who started
# the work, which a capacity reader needs to know and a utilization number
# must not care about.
BUSY = "BUSY"                      # the queue owns a task here
RUNNING_MANUAL = "RUNNING_MANUAL"  # something real is running that the queue did not start
IDLE = "IDLE"                      # eligible AND demonstrably free
BLOCKED = "BLOCKED"                # a task is here but cannot proceed
OFFLINE = "OFFLINE"                # session/node gone
UNAVAILABLE = "UNAVAILABLE"        # exists but the runtime may not drive it
STALE = "STALE"                    # evidence too old to classify honestly
UNKNOWN = "UNKNOWN"                # evidence absent or contradictory

OCCUPIED_STATES = frozenset({BUSY, RUNNING_MANUAL, BLOCKED})
"""BLOCKED is occupied: the lane is held, so the capacity is not available even
though nothing is progressing. Counting it as free would suggest reassigning
work onto a session that cannot take it."""

ELIGIBLE_DENOMINATOR_STATES = frozenset({BUSY, RUNNING_MANUAL, BLOCKED, IDLE})
"""Utilization = occupied / (occupied + idle). OFFLINE, UNAVAILABLE, STALE and
UNKNOWN are excluded and reported separately -- a denominator that quietly
includes machines nobody can dispatch to understates utilization and invents
idle capacity that does not exist."""

DEFAULT_STALE_AFTER_SECONDS = 900.0
"""One report window. Evidence older than the window the report claims to
describe cannot honestly be presented as describing it."""

# -- progress provenance -----------------------------------------------------
PROGRESS_NONE = "none"                    # nothing countable; percent is null
PROGRESS_TASK_WEIGHTS = "task_weights"    # work_store weighted task completion
PROGRESS_REQUIREMENTS = "requirements"    # requirement contract covered/required
PROGRESS_QUEUE_TASKS = "queue_tasks"      # completed/total tasks on a lane

# -- optimization findings ---------------------------------------------------
UNDERUTILIZED_WITH_BACKLOG = "UNDERUTILIZED_WITH_BACKLOG"
LOAD_IMBALANCE = "LOAD_IMBALANCE"
IDLE_NODE_WITH_NO_BACKLOG = "IDLE_NODE_WITH_NO_BACKLOG"
ALL_CAPACITY_BUSY = "ALL_CAPACITY_BUSY"

IMBALANCE_BUSY_THRESHOLD = 0.80
IMBALANCE_IDLE_THRESHOLD = 0.30

# -- task/OS compatibility ---------------------------------------------------
# Substrings that mean a task genuinely needs Windows. Deliberately narrow: a
# false positive here only forgoes a suggestion, while a false negative would
# propose moving work that cannot run where it is being sent.
WINDOWS_ONLY_MARKERS = (
    "wpf", "registry", "installer", "msi", "windows service", "win32",
    "powershell", "winforms", "desktop viewer", "windows gui", ".exe",
)
# Work that is portable in practice on this fleet.
PORTABLE_MARKERS = (
    "backend", "api", "git", "test", "tests", "frontend", "protocol",
    "review", "docs", "lint", "schema", "migration",
)


def _now(now: datetime | None = None) -> datetime:
    return now or datetime.now(timezone.utc)


def _age_seconds(timestamp: Any, now: datetime) -> float | None:
    """Seconds since `timestamp`, or None if it cannot be read.

    Unparseable is None, never 0 -- a zero age reads as "just seen", which is
    the most dangerous possible default for a staleness check.
    """
    if timestamp in (None, ""):
        return None
    if isinstance(timestamp, (int, float)):
        try:
            return max(0.0, now.timestamp() - float(timestamp))
        except (TypeError, ValueError, OSError):
            return None
    text = str(timestamp).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now - parsed).total_seconds())


@dataclass(frozen=True)
class SessionView:
    session: str
    node_id: str | None
    state: str
    project_id: str | None = None
    project_confidence: str = "none"
    task_id: str | None = None
    task_title: str | None = None
    task_status: str | None = None
    agent_type: str | None = None
    last_activity_age_seconds: float | None = None
    stale: bool = False
    progress_percent: float | None = None
    progress_basis: str = PROGRESS_NONE
    branch: str | None = None
    occupancy: str = "unknown"
    evidence: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session, "node_id": self.node_id, "state": self.state,
            "project_id": self.project_id, "project_confidence": self.project_confidence,
            "task_id": self.task_id, "task_title": self.task_title,
            "task_status": self.task_status, "agent_type": self.agent_type,
            "last_activity_age_seconds": self.last_activity_age_seconds,
            "stale": self.stale, "progress_percent": self.progress_percent,
            "progress_basis": self.progress_basis, "branch": self.branch,
            "occupancy": self.occupancy, "evidence": dict(self.evidence),
            "reason": self.reason,
        }


def classify_session(row: Mapping[str, Any], *, status: Mapping[str, Any] | None = None,
                     lane_task: Mapping[str, Any] | None = None,
                     node: Mapping[str, Any] | None = None,
                     now: datetime | None = None,
                     stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS) -> SessionView:
    """One session's state, with the evidence that produced it.

    Order matters and is deliberate:

    1. A node that is offline makes every session on it OFFLINE. No local
       signal can outrank "the machine is not answering".
    2. A queue task in flight is BUSY, or BLOCKED if it is held.
    3. Something running that the queue did not start is RUNNING_MANUAL. This
       is checked BEFORE idleness, because the whole "detached != idle" rule
       lives in that ordering.
    4. Only then, with no occupancy of any kind, does freshness decide between
       IDLE, STALE and UNKNOWN.
    """
    now = _now(now)
    status = status or {}
    session = str(row.get("name") or row.get("session") or "")
    node_id = row.get("node_id") or (node or {}).get("id") or (node or {}).get("node_id")
    agent_type = row.get("agent_type") or row.get("current_command") or status.get("current_command")

    age = _age_seconds(row.get("last_activity_at") or row.get("activity")
                       or status.get("last_activity_at"), now)
    occupied, occ_evidence = occupancy(row, status)
    evidence: dict[str, Any] = dict(occ_evidence)

    # 1. the machine, before anything local
    node_status = str((node or {}).get("status") or "").casefold()
    if node is not None and node_status and node_status != "online":
        return SessionView(session, node_id, OFFLINE, agent_type=agent_type,
                           last_activity_age_seconds=age, evidence=evidence,
                           reason=f"node {node_id} is {node_status}")
    if status.get("exists") is False or status.get("pane_dead"):
        return SessionView(session, node_id, OFFLINE, agent_type=agent_type,
                           last_activity_age_seconds=age, evidence=evidence,
                           reason="session no longer exists on its node")

    # 2. the queue's own claim
    if lane_task:
        task_status = str(lane_task.get("status") or "")
        state = BLOCKED if task_status in ("BLOCKED", "FAILED", "PAUSED") else BUSY
        return SessionView(
            session, node_id, state, task_id=lane_task.get("id"),
            task_title=lane_task.get("title"), task_status=task_status or None,
            agent_type=agent_type, last_activity_age_seconds=age,
            occupancy="queue", evidence=evidence,
            reason=None if state == BUSY else f"task is {task_status}")

    # 3. occupied without the queue -- checked before any freshness test
    if occupied:
        return SessionView(session, node_id, RUNNING_MANUAL, agent_type=agent_type,
                           last_activity_age_seconds=age, occupancy="manual",
                           evidence=evidence,
                           reason="something is running that the queue did not start")

    # 4. demonstrably free -- but only if the evidence is good enough to say so
    if not evidence.get("current_command") and not evidence.get("session_state"):
        return SessionView(session, node_id, UNKNOWN, agent_type=agent_type,
                           last_activity_age_seconds=age, occupancy="unknown",
                           evidence=evidence,
                           reason="no command or session state was observed; "
                                  "absence of evidence is not evidence of idleness")
    if age is not None and age > stale_after_seconds:
        return SessionView(session, node_id, STALE, agent_type=agent_type,
                           last_activity_age_seconds=age, stale=True,
                           occupancy="unknown", evidence=evidence,
                           reason=f"last evidence is {int(age)}s old, older than the "
                                  f"{int(stale_after_seconds)}s window this report describes")
    return SessionView(session, node_id, IDLE, agent_type=agent_type,
                       last_activity_age_seconds=age, occupancy="free",
                       evidence=evidence, reason=None)


_SHELL_COMMANDS = frozenset({
    "bash", "sh", "zsh", "fish", "dash", "ksh", "csh", "tcsh",
    "cmd", "cmd.exe", "powershell", "pwsh", "powershell.exe", "pwsh.exe",
    "login", "-bash", "-zsh", "",
})
_ACTIVE_SESSION_STATES = frozenset({"RUNNING", "BUSY", "WORKING", "THINKING"})


def occupancy(row: Mapping[str, Any],
              status: Mapping[str, Any] | None) -> tuple[bool, dict[str, Any]]:
    """Is something actually running here, queue or no queue?

    Mirrors `work_service._occupancy` deliberately rather than importing it:
    that function is private to a surface that lists only `-work` sessions,
    and widening its contract would change what that surface means. The two
    are pinned to the same behaviour by test_system_report.py.
    """
    status = status or {}
    command = str(row.get("current_command") or status.get("current_command") or "").strip()
    base = command.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    agent_running = bool(base) and base not in _SHELL_COMMANDS
    state = str(status.get("state") or status.get("status") or "")
    active_state = state in _ACTIVE_SESSION_STATES
    return (agent_running or active_state,
            {"current_command": command or None, "session_state": state or None,
             "agent_running": agent_running, "active_state": active_state})


# -- progress ----------------------------------------------------------------

def progress_from_weights(total_weight: Any, done_weight: Any) -> tuple[float | None, str]:
    """Weighted task completion, when the weights are real numbers."""
    try:
        total, done = float(total_weight), float(done_weight)
    except (TypeError, ValueError):
        return None, PROGRESS_NONE
    if total <= 0:
        return None, PROGRESS_NONE
    return round(max(0.0, min(1.0, done / total)) * 100.0, 1), PROGRESS_TASK_WEIGHTS


def progress_from_requirements(covered: Any, required: Any) -> tuple[float | None, str]:
    """Requirement-contract coverage. Countable, so it is allowed to be a
    percentage; anything softer is not."""
    try:
        total, done = int(required), int(covered)
    except (TypeError, ValueError):
        return None, PROGRESS_NONE
    if total <= 0:
        return None, PROGRESS_NONE
    return round(max(0, min(total, done)) / total * 100.0, 1), PROGRESS_REQUIREMENTS


def progress_from_queue_counts(counts: Mapping[str, int]) -> tuple[float | None, str]:
    done = int(counts.get("COMPLETED", 0) or 0)
    total = sum(int(v or 0) for v in counts.values())
    if total <= 0:
        return None, PROGRESS_NONE
    return round(done / total * 100.0, 1), PROGRESS_QUEUE_TASKS


# -- node rollup -------------------------------------------------------------

@dataclass(frozen=True)
class NodeView:
    node_id: str
    name: str | None = None
    platform: str | None = None
    status: str = UNKNOWN
    total_visible_sessions: int = 0
    busy: int = 0
    running_manual: int = 0
    idle: int = 0
    blocked: int = 0
    offline: int = 0
    unavailable: int = 0
    stale: int = 0
    unknown: int = 0
    utilization_percent: float | None = None
    utilization_denominator: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id, "name": self.name, "platform": self.platform,
            "status": self.status, "total_visible_sessions": self.total_visible_sessions,
            "busy": self.busy, "running_manual": self.running_manual, "idle": self.idle,
            "blocked": self.blocked, "offline": self.offline,
            "unavailable": self.unavailable, "stale": self.stale, "unknown": self.unknown,
            "utilization_percent": self.utilization_percent,
            "utilization_denominator": self.utilization_denominator,
            "denominator_definition": "occupied / (occupied + idle); OFFLINE, "
                                      "UNAVAILABLE, STALE and UNKNOWN excluded",
        }


def roll_up_node(node_id: str, views: Sequence[SessionView], *,
                 node: Mapping[str, Any] | None = None) -> NodeView:
    counts = {state: 0 for state in
              (BUSY, RUNNING_MANUAL, IDLE, BLOCKED, OFFLINE, UNAVAILABLE, STALE, UNKNOWN)}
    for view in views:
        counts[view.state] = counts.get(view.state, 0) + 1
    occupied = counts[BUSY] + counts[RUNNING_MANUAL] + counts[BLOCKED]
    denominator = occupied + counts[IDLE]
    utilization = round(occupied / denominator * 100.0, 1) if denominator else None
    return NodeView(
        node_id=node_id, name=(node or {}).get("display_name") or (node or {}).get("name"),
        platform=(node or {}).get("platform"),
        status=str((node or {}).get("status") or UNKNOWN),
        total_visible_sessions=len(views),
        busy=counts[BUSY], running_manual=counts[RUNNING_MANUAL], idle=counts[IDLE],
        blocked=counts[BLOCKED], offline=counts[OFFLINE],
        unavailable=counts[UNAVAILABLE], stale=counts[STALE], unknown=counts[UNKNOWN],
        utilization_percent=utilization, utilization_denominator=denominator)


# -- optimization ------------------------------------------------------------

def task_requires_windows(title: str | None) -> bool:
    text = (title or "").casefold()
    return any(marker in text for marker in WINDOWS_ONLY_MARKERS)


def task_looks_portable(title: str | None) -> bool:
    text = (title or "").casefold()
    return any(marker in text for marker in PORTABLE_MARKERS)


@dataclass(frozen=True)
class Finding:
    kind: str
    detail: str
    nodes: tuple[str, ...] = ()
    confidence: str = "rule"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail,
                "nodes": list(self.nodes), "confidence": self.confidence}


def suggest(nodes: Sequence[NodeView], *, ready_backlog: int,
            backlog_titles: Sequence[str] = ()) -> list[Finding]:
    """Rule-based, deterministic, and never a migration.

    V1 suggests; it never moves anything. Every finding names the rule that
    produced it so a reader can disagree with the rule rather than with an
    opaque recommendation.
    """
    findings: list[Finding] = []
    idle_nodes = [n for n in nodes if n.idle > 0]
    idle_capacity = sum(n.idle for n in nodes)

    if ready_backlog > 0 and idle_capacity > 0:
        findings.append(Finding(
            UNDERUTILIZED_WITH_BACKLOG,
            f"{ready_backlog} task(s) READY while {idle_capacity} eligible session(s) "
            f"sit idle on {', '.join(n.node_id for n in idle_nodes)}",
            tuple(n.node_id for n in idle_nodes)))

    busy_nodes = [n for n in nodes if n.utilization_percent is not None
                  and n.utilization_percent >= IMBALANCE_BUSY_THRESHOLD * 100]
    quiet_nodes = [n for n in nodes if n.utilization_percent is not None
                   and n.utilization_percent <= IMBALANCE_IDLE_THRESHOLD * 100
                   and n.idle > 0]
    for hot in busy_nodes:
        for quiet in quiet_nodes:
            if hot.node_id == quiet.node_id:
                continue
            movable = [t for t in backlog_titles
                       if not task_requires_windows(t) or quiet.platform == "windows"]
            candidates = [t for t in movable if task_looks_portable(t)]
            if not movable:
                continue
            detail = (f"{hot.node_id} is {hot.utilization_percent}% busy while "
                      f"{quiet.node_id} is {quiet.utilization_percent}% with "
                      f"{quiet.idle} idle session(s)")
            if candidates:
                detail += f"; candidate task(s): {', '.join(candidates[:3])}"
            else:
                detail += "; no task was identified as portable -- candidate only"
            findings.append(Finding(LOAD_IMBALANCE, detail, (hot.node_id, quiet.node_id),
                                    confidence="rule" if candidates else "low"))

    if ready_backlog == 0 and idle_capacity > 0:
        findings.append(Finding(
            IDLE_NODE_WITH_NO_BACKLOG,
            f"{idle_capacity} idle session(s) but nothing is READY -- spare capacity, "
            f"not a scheduling fault", tuple(n.node_id for n in idle_nodes)))
    if ready_backlog > 0 and idle_capacity == 0:
        findings.append(Finding(
            ALL_CAPACITY_BUSY,
            f"{ready_backlog} task(s) READY and no idle capacity anywhere"))
    return findings
