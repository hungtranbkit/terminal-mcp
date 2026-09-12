"""Terminal Wall: many sessions at a glance, and which of them are stuck.

WHY A MODULE AND NOT A LOOP IN THE ROUTE
----------------------------------------
There is no batch API on a node agent -- only per-session `status`.
A wall of 20 sessions is therefore 20 round-trips, and a dashboard
left open in three tabs would triple that every few seconds. So the fan-out
happens ONCE here, in parallel, behind a short TTL cache that every open tab
shares. A refresh costs the fleet one fan-out per TTL window regardless of
how many people are watching.

WHAT "RUNNING" IS ALLOWED TO MEAN
---------------------------------
The hard requirement, and the reason this reuses status.classify_status
rather than inventing anything: a live agent process is NOT evidence of
work in progress. classify_status only says RUNNING when the pane's command
is an agent AND tmux recorded activity within the last 60 seconds; an agent
sitting at a finished task falls through to IDLE or UNKNOWN on its own.
classify_supervisor_state then layers ERROR and completion evidence from the
pane text. This module maps that vocabulary to the wall's badges and adds
exactly one state of its own -- OFFLINE, for a node that did not answer --
because that is the one thing the per-session classifier cannot see.

Nothing here sends input. The wall is read-only by construction: the only
fleet call it makes is `terminal_status`, and there is no code path from it
to send_keys. `tests/test_terminal_wall.py` asserts that directly, by
handing `build_snapshot` a controller whose write methods raise.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from .status import classify_supervisor_state

# The wall's own vocabulary, in the order a person scans for trouble.
STATE_RUNNING = "RUNNING"
STATE_WAITING = "WAITING"
STATE_ERROR = "ERROR"
STATE_DONE = "DONE"
STATE_IDLE = "IDLE"
STATE_OFFLINE = "OFFLINE"
STATE_UNKNOWN = "UNKNOWN"

# Sort weight: work in progress first, then what is waiting on a person,
# then what has stopped -- and what we cannot see at all, last.
STATE_ORDER = {
    STATE_RUNNING: 0,
    STATE_WAITING: 1,      # a person is being waited on
    STATE_ERROR: 2,
    STATE_IDLE: 3,
    STATE_UNKNOWN: 4,
    STATE_DONE: 5,
    STATE_OFFLINE: 6,
}
# Which badges count as "active" for the wall's own filter.
ACTIVE_STATES = (STATE_RUNNING, STATE_WAITING, STATE_ERROR)

# classify_supervisor_state's vocabulary -> the wall's.
_FROM_SUPERVISOR = {
    "WAITING_INPUT": STATE_WAITING,
    "ERROR": STATE_ERROR,
    "COMPLETION_CANDIDATE": STATE_DONE,
    "VERIFIED_DONE": STATE_DONE,
    "RUNNING": STATE_RUNNING,
    "IDLE": STATE_IDLE,
    "UNKNOWN": STATE_UNKNOWN,
}

DEFAULT_TAIL_LINES = 16
MAX_TAIL_LINES = 40
DEFAULT_TTL_SECONDS = 4.0
# One worker per session up to this bound: the cost is one slow node's
# round-trip (~1s over the tailnet), not CPU, so a wider pool turns a serial
# wait into a single wait. Measured: 20 sessions 5.4s at 8 workers, 1.3s at 24.
FANOUT_WORKERS = 24


@dataclass(frozen=True)
class WallBox:
    """One tile. `change_token` is what lets the page repaint only what moved."""

    node_id: str
    node_name: str | None
    session: str
    agent: str | None
    command: str | None
    state: str
    reason: str
    last_activity: float | None
    age_seconds: float | None
    lines: list[str]
    age_is_witnessed: bool
    attached: bool
    is_offline: bool
    change_token: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id, "node_name": self.node_name, "session": self.session,
            "agent": self.agent, "command": self.command, "state": self.state,
            "reason": self.reason, "last_activity": self.last_activity,
            "age_seconds": self.age_seconds,
            "age_is_witnessed": self.age_is_witnessed, "lines": self.lines,
            "attached": self.attached, "offline": self.is_offline,
            "change_token": self.change_token, "order": STATE_ORDER.get(self.state, 9),
        }


def _token(state: str, lines: list[str], activity: float | None) -> str:
    """Changes exactly when the tile's visible content does.

    Deliberately excludes the AGE, which ticks every second: including it
    would mark every tile changed on every poll and defeat the point.
    """
    digest = hashlib.sha256()
    digest.update(state.encode())
    digest.update(str(activity or "").encode())
    digest.update("\n".join(lines).encode("utf-8", "replace"))
    return digest.hexdigest()[:16]


# An agent command that has not moved for this long is idle, not a mystery.
# classify_status reserves IDLE for shells and returns UNKNOWN for an agent
# with stale activity, which is accurate but tells a wall-watcher nothing.
# Measured age is evidence, so naming it IDLE here is a reading of the data,
# not a guess about intent -- and the age is always shown beside the badge.
# Output seen changing within this window is work in progress. Beyond it the
# agent is alive but idle -- which is the distinction the wall exists for.
RUNNING_WITHIN_SECONDS = 90.0

_COMMAND_RE = re.compile(r"current command is '([^']*)'")


def command_from(status: dict[str, Any]) -> str | None:
    """The pane's command, which the classifier writes into its own reason.

    The status payload has no dedicated field for it; the reason string is
    where it actually lives, and it is written there deterministically.
    """
    reason = status.get("reason") or ""
    match = _COMMAND_RE.search(reason)
    command = match.group(1) if match else None
    return command or None


def derive_state(status: dict[str, Any], output: str,
                 *, age_seconds: float | None = None, witnessed: bool = True,
                 watched_for: float | None = None) -> tuple[str, str]:
    """The wall badge for one session, from evidence the fleet already has.

    A status payload that came back with an error is not a guess about the
    session -- it is a statement that we could not see it, which is OFFLINE.
    """
    if not status or status.get("error"):
        return STATE_OFFLINE, (status or {}).get("error") or "node did not answer"
    if status.get("exists") is False:
        return STATE_OFFLINE, "session no longer exists on its node"
    raw = status.get("state") or "UNKNOWN"
    reason = status.get("reason") or ""
    mapped, why = classify_supervisor_state(raw, reason, output)
    state = _FROM_SUPERVISOR.get(mapped, STATE_UNKNOWN)
    command = (command_from(status) or "").casefold()
    # WAITING and ERROR are read from the pane text and always win: they are
    # direct evidence about what the session is doing.
    if state in (STATE_WAITING, STATE_ERROR, STATE_DONE):
        return state, (why or reason)
    # From here the wall decides RUNNING on its own evidence, for EVERY
    # command -- not just the ones that look like an agent.
    #
    # `classify_status` upstream will say RUNNING on the strength of tmux's
    # activity timestamp alone. On this fleet that field is wrong in both
    # directions: it never moves for a long-lived agent (see
    # OutputChangeTracker), and it moves for a bash session that was merely
    # typed at, which is how two freshly-created shells turned up on the wall
    # wearing RUNNING badges with nothing running in them. Neither is work in
    # progress, so neither may borrow the badge.
    label = command or "session"
    if age_seconds is None:
        # No change history at all (no tracker wired in). Nothing of our own
        # to go on, and upstream's only extra evidence is the activity claim
        # just rejected -- so decline the badge rather than repeat it.
        if state == STATE_RUNNING:
            return STATE_UNKNOWN, (reason or "no observed output change to justify RUNNING")
        return state, (why or reason)
    # RUNNING requires a change this controller actually SAW. An age measured
    # from a baseline we merely adopted on first sight is a lower bound on
    # silence -- a session idle for a week reads as "4s" on the second poll --
    # and must never buy a RUNNING badge.
    if witnessed and age_seconds <= RUNNING_WITHIN_SECONDS:
        return STATE_RUNNING, f"{label} produced new output {int(age_seconds)}s ago"
    if witnessed:
        return STATE_IDLE, (f"{label} is alive but its output has not changed for "
                            f"{int(age_seconds)}s")
    # Never witnessed a change. Once we have watched longer than the RUNNING
    # window without seeing one, silence is itself the evidence.
    if watched_for is not None and watched_for >= RUNNING_WITHIN_SECONDS:
        return STATE_IDLE, (f"{label} has produced no new output in the "
                            f"{int(watched_for)}s this wall has been watching it")
    return STATE_UNKNOWN, (f"{label} is alive; watching for "
                           f"{int(watched_for or 0)}s, no output change seen yet")



@dataclass(frozen=True)
class _Watch:
    """What we have stored about one session between polls."""

    fingerprint: str
    marked_at: float        # when this fingerprint was first recorded
    witnessed: bool         # did we actually SEE it replace a different one?
    first_seen_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.first_seen_at:
            object.__setattr__(self, "first_seen_at", self.marked_at)


@dataclass(frozen=True)
class Change:
    """One reading of a session's output activity.

    `witnessed` is the whole point: an age without it is a lower bound on
    silence, not a measurement of when output last appeared.
    """

    age_seconds: float
    witnessed: bool
    watched_for: float


class OutputChangeTracker:
    """When each session's pane text last actually changed.

    MEASURED, NOT ASSUMED: tmux's own `session_activity` is unreliable here.
    On this fleet all three local sessions report an activity timestamp
    exactly equal to their creation time -- it has not moved in days -- while
    one of them is producing output continuously. Anything built on that
    field (an "idle for 2 days" age, and classify_status's RUNNING rule,
    which requires activity within 60s) is therefore wrong in the one
    direction that matters: it can never say RUNNING.

    So the wall keeps its own evidence. Each snapshot fingerprints the visible
    tail; when a fingerprint differs from the last one, that is the moment the
    session demonstrably produced output.

    The distinction that matters, and that this got wrong once: "unchanged
    since I started watching" is NOT "changed when I started watching". A
    session idle for a week looks, on the second poll four seconds later,
    exactly like one that produced its last line four seconds ago. Reporting
    that as a four-second age painted RUNNING on every idle agent for the
    first 90 seconds of the controller's life. So `witnessed` travels with
    the age, and only a change this tracker actually SAW may justify RUNNING.
    """

    def __init__(self) -> None:
        self._last: dict[str, _Watch] = {}

    def observe(self, session: str, fingerprint: str, now: float) -> "Change":
        previous = self._last.get(session)
        if previous is None:
            # First sight. We know nothing about this session's history: the
            # text on screen may be a second old or a week old.
            self._last[session] = _Watch(fingerprint, now, witnessed=False)
            return Change(age_seconds=0.0, witnessed=False, watched_for=0.0)
        if fingerprint != previous.fingerprint:
            self._last[session] = _Watch(fingerprint, now, witnessed=True,
                                         first_seen_at=previous.first_seen_at)
            return Change(age_seconds=0.0, witnessed=True,
                          watched_for=now - previous.first_seen_at)
        return Change(age_seconds=now - previous.marked_at,
                      witnessed=previous.witnessed,
                      watched_for=now - previous.first_seen_at)

    def forget(self, keep: set[str]) -> None:
        for session in [s for s in self._last if s not in keep]:
            del self._last[session]


class WallSnapshotCache:
    """One fan-out per TTL, shared by every open tab."""

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self.ttl = ttl_seconds
        self._payload: dict[str, Any] | None = None
        self._taken_at = 0.0
        # The whole check-and-build is serialised. Not merely to save a
        # duplicate fan-out: two builds racing would both call
        # `tracker.observe` on the same session, and the second would compare
        # the fingerprint against the one the first had just stored, see no
        # difference against a `changed_at` of ~now, and report "changed 0s
        # ago" -- a RUNNING badge with no output behind it.
        self._lock = threading.Lock()
        # Lives with the cache, not with a request: change history is only
        # meaningful across snapshots.
        self.tracker = OutputChangeTracker()

    def get(self, build, *, now: float | None = None, force: bool = False) -> dict[str, Any]:
        with self._lock:
            at = now or time.time()
            if not force and self._payload is not None and (at - self._taken_at) < self.ttl:
                fresh = dict(self._payload)
                fresh["cached"] = True
                fresh["cache_age_seconds"] = round(at - self._taken_at, 2)
                return fresh
            self._payload = build()
            # Timed after the build, not before: a 1.3s fan-out that stamped
            # itself with its start time would be served as already stale.
            self._taken_at = now or time.time()
            fresh = dict(self._payload)
            fresh["cached"] = False
            fresh["cache_age_seconds"] = 0.0
            return fresh


def build_snapshot(controller, *, tail_lines: int = DEFAULT_TAIL_LINES,
                   now: float | None = None,
                   tracker: "OutputChangeTracker | None" = None) -> dict[str, Any]:
    """Status + a short tail for every session the fleet can see, in one pass.

    Each session costs exactly ONE call to its node -- `terminal_status`
    already returns the recent pane text, so asking for a tail on top of it
    would double every round-trip for text we were just handed. The calls go
    out in parallel behind a bounded pool. A node that fails answers OFFLINE
    for its sessions rather than removing them: a tile that vanishes is
    indistinguishable from a session that ended.
    """
    now = now or time.time()
    tail_lines = max(1, min(int(tail_lines), MAX_TAIL_LINES))
    listing = controller.terminal_list_sessions()
    rows = listing.get("sessions", [])
    unreachable = listing.get("unreachable_nodes", [])

    def fetch(row: dict[str, Any]) -> WallBox:
        node_id = row.get("node_id") or "local"
        session = row.get("name")
        status: dict[str, Any] = {}
        try:
            status = controller.terminal_status(session) or {}
        except Exception as exc:  # noqa: BLE001 -- one bad node never blanks the wall
            status = {"error": f"{type(exc).__name__}: {exc}"}
        # `status` already carries the recent pane text (core.py captures ~20
        # lines for its own classification), so asking for a tail as well
        # would double every round-trip for text the fleet just sent us.
        output = status.get("last_output") or ""
        lines = [line for line in output.splitlines()][-tail_lines:]
        # Trailing spaces come and go as a TUI repaints its own status line;
        # they are not output. A ticking duration inside that line IS -- it
        # only ticks while an operation is in flight -- so it is left alone.
        fingerprint = hashlib.sha256(
            "\n".join(line.rstrip() for line in lines).encode("utf-8", "replace")).hexdigest()
        # Age since the output last CHANGED, observed across snapshots --
        # not tmux's session_activity, which does not move on this fleet.
        change = tracker.observe(session, fingerprint, now) if tracker else None
        age = change.age_seconds if change else None
        witnessed = change.witnessed if change else False
        # `last_activity` is a claim about when output appeared, so it exists
        # only when a change was actually seen. Deriving it from an adopted
        # baseline would put a confident timestamp on a guess.
        activity = (now - age) if (change is not None and witnessed) else None
        state, reason = derive_state(status, output, age_seconds=age,
                                     witnessed=witnessed,
                                     watched_for=change.watched_for if change else None)
        return WallBox(
            node_id=node_id, node_name=row.get("node_name"), session=session,
            agent=row.get("agent_type") or status.get("agent_type"),
            command=command_from(status) or row.get("current_command"),
            state=state, reason=reason,
            last_activity=activity if isinstance(activity, (int, float)) else None,
            age_seconds=round(age, 1) if age is not None else None,
            age_is_witnessed=witnessed,
            lines=lines, attached=bool(row.get("attached")),
            is_offline=state == STATE_OFFLINE,
            change_token=_token(state, lines, activity if isinstance(activity, (int, float)) else None),
        )

    boxes: list[WallBox] = []
    if rows:
        with ThreadPoolExecutor(max_workers=FANOUT_WORKERS) as pool:
            boxes = list(pool.map(fetch, rows))

    # A node that never answered has no rows at all, so its sessions cannot
    # be listed -- but saying nothing about it would read as "no sessions
    # there", which is a different and wrong statement.
    for node in unreachable:
        boxes.append(WallBox(
            node_id=node.get("node_id"), node_name=node.get("node_name"),
            session="(node unreachable)", agent=None, command=None,
            state=STATE_OFFLINE,
            reason=f"node status: {node.get('status')}", last_activity=None,
            age_seconds=None, age_is_witnessed=False, lines=[],
            attached=False, is_offline=True,
            change_token=_token(STATE_OFFLINE, [], None)))

    # Drop change history for sessions that no longer exist, so a long-lived
    # controller does not accumulate a fingerprint per session ever seen.
    if tracker is not None:
        tracker.forget({b.session for b in boxes if b.session})

    boxes.sort(key=lambda b: (STATE_ORDER.get(b.state, 9),
                              -(b.last_activity or 0), b.session or ""))
    counts: dict[str, int] = {}
    for box in boxes:
        counts[box.state] = counts.get(box.state, 0) + 1
    return {
        "generated_at": now,
        "tail_lines": tail_lines,
        "boxes": [box.as_dict() for box in boxes],
        "counts": counts,
        "unreachable_nodes": unreachable,
        # Published so the page can state the real threshold instead of
        # carrying its own copy of the number, which would silently drift
        # the moment this constant is tuned.
        "running_within_seconds": RUNNING_WITHIN_SECONDS,
        "read_only": True,
    }


def _epoch(value: Any) -> float | None:
    from datetime import datetime, timezone

    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()
