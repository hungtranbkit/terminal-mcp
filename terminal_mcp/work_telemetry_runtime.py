"""Efficiency telemetry, filled by the runtime instead of by recollection.

WHAT WAS WRONG WITH THE OLD SHAPE. `work_telemetry.py` has always had the
right model -- counters that are counted, token figures that carry their
provenance -- but the only thing that ever wrote a row was a worker calling
`work_telemetry_report` about its own finished task. That makes the whole
record optional and retrospective: a task that crashed reports nothing, a
task that forgot reports nothing, and a task that remembers reports whatever
it believes. Efficiency decisions were being made from a self-selected
sample.

WHAT THIS MODULE DOES INSTEAD. It listens to the queue's own transition
chokepoint. `QueueStore._record_event_locked` is the single point every one
of its state changes flows through, and it already publishes each recorded
event to an optional sink after the transaction commits (see
`event_wiring.py`, which uses the same hook for the event bus). So:

    DISPATCHING   -> open the row, or reopen the one this task already has
    RUNNING       -> the agent actually started
    VERIFYING     -> first preview: a reviewable result now exists
    COMPLETED/... -> finish, and decide first-pass success from the record

None of those are guesses about what a worker was doing. They are the
queue's own record of what it did, which is the same record the UI, the
contract evaluation and the recovery loop already trust.

FIRST PREVIEW IS A DEFINITION, AND IT IS STATED. This runtime has no
separate "preview" state, so the honest options were to invent one or to
name an existing transition and say so. Every row records `preview_basis`:
the transition into VERIFYING, which is the first moment a result exists for
someone to look at. A caller who has a truer signal -- a deploy preview URL,
a screenshot artifact -- calls `mark_preview` with its own basis and that is
what the row will say instead.

PROVIDER USAGE IS NEVER SYNTHESISED HERE. The queue cannot see a provider's
token counters, and this module does not pretend otherwise: a row's usage
stays UNAVAILABLE until something that genuinely has those numbers reports
them through `record_provider_usage`. In particular it is NOT back-filled
from the local AI Usage Monitor, whose figures are per-machine quota for a
whole day and cannot be attributed to one task without inventing the
attribution.

NOTHING HERE IS A CREDENTIAL PATH. This module reads task ids, statuses,
module names and usage counters. It never reads, stores or forwards a token,
key, cookie or password, and there is no code path in it that could begin to.

COUNTERS FROM THE REAL CALL SITES. `files_read`, `search_calls`, knowledge,
context-pack and similar-bug hits are things only the retrieval code can
see. Those call sites report through `note()`, which does nothing at all
unless a recorder has been made active for a task -- so an uninstrumented
process behaves exactly as before, and a zero counter on a row with no
`signal_sources` means "nobody reported", not "it never happened".
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator, Mapping

from .work_telemetry import SIGNAL_ALIASES, TaskTelemetry, TelemetryStore

_LOGGER = logging.getLogger(__name__)

# Read from queue_store rather than restated, for the same reason work_service
# reads them: a status renamed there must not leave this module silently
# matching nothing.
try:  # pragma: no cover - import shape only
    from .queue_store import (BLOCKED, CANCELLED, COMPLETED, DISPATCHING, FAILED,
                              RUNNING, SKIPPED, VERIFYING)
except Exception:  # pragma: no cover - defensive
    (DISPATCHING, RUNNING, VERIFYING, COMPLETED,
     FAILED, BLOCKED, SKIPPED, CANCELLED) = (
        "DISPATCHING", "RUNNING", "VERIFYING", "COMPLETED",
        "FAILED", "BLOCKED", "SKIPPED", "CANCELLED")

# Terminal for telemetry purposes: the task stopped. A later DISPATCHING
# reopens the same row rather than starting a second one.
FINISHING_STATUSES = (COMPLETED, SKIPPED, CANCELLED, FAILED, BLOCKED)
EXCURSION_STATUSES = (FAILED, BLOCKED)

PREVIEW_BASIS = ("queue transition into VERIFYING -- the worker produced a "
                 "result there is something to look at")

# The attribute names a resolver may supply. Anything else it returns is
# ignored: a telemetry row records the facts this contract names, and a
# resolver inventing a field would put it somewhere no aggregate reads.
ATTRIBUTE_FIELDS = ("work_id", "bug_id", "project_id", "module", "execution_mode",
                    "spec_level", "difficulty", "lane", "redefine_count")

AttributeResolver = Callable[[str], Mapping[str, Any]]

# How many not-yet-dispatched tasks may hold buffered signals at once. A
# bound rather than none: a long-running process that keeps seeing signals
# for tasks that never dispatch would otherwise grow a dictionary forever,
# and losing the oldest buffered counts is a much smaller problem than a
# process that cannot be left running.
MAX_PENDING_TASKS = 256


class RuntimeTelemetry:
    """Opens, advances and closes telemetry rows from real queue transitions.

    Every method is defensive in the same way the queue's own sink contract
    is: a telemetry failure must never disturb the transition that triggered
    it. A row that could not be written is logged and lost, which is a far
    better outcome than a dispatch that did not happen because bookkeeping
    raised.
    """

    def __init__(self, store: TelemetryStore, *,
                 attributes: AttributeResolver | None = None,
                 clock: Callable[[], str] | None = None) -> None:
        self.store = store
        self._attributes = attributes
        self._clock = clock
        self._lock = threading.Lock()
        # Signals reported for a task that has no row yet -- a retrieval done
        # during PRECHECK, or by a process that started mid-task. Held rather
        # than dropped, and applied when the row opens.
        self._pending: dict[str, dict[str, int]] = {}
        self._pending_sources: dict[str, set[str]] = {}

    # -- the sink ------------------------------------------------------------

    def sink(self) -> Callable[[dict[str, Any]], None]:
        """The callable `QueueStore(event_sink=...)` takes.

        Never raises, for the reason that hook's own docstring gives: a
        publishing glitch must not un-commit a real state transition.
        """
        def _sink(entry: dict[str, Any]) -> None:
            try:
                self.on_queue_event(entry)
            except Exception:  # noqa: BLE001 -- see the class docstring
                _LOGGER.exception("work telemetry: queue event not recorded")
        return _sink

    def on_queue_event(self, entry: Mapping[str, Any]) -> dict[str, Any] | None:
        """Translate one queue transition into telemetry, or ignore it.

        Returning None is a real answer: most queue events (lane pauses,
        coordinator decisions, project scoping) say nothing about what a task
        cost, and recording them would put noise in a table whose whole value
        is that every row means something.
        """
        task_id = str((entry or {}).get("task_id") or "")
        to_status = str((entry or {}).get("to_status") or "")
        if not task_id or not to_status:
            return None
        session = (entry or {}).get("session")
        with self._lock:
            if to_status == DISPATCHING:
                record = self._open(task_id, lane=session)
                record.mark_dispatched(lane=session or record.lane)
                self._save(record)
                return {"action": "opened" if record.dispatch_count == 1 else "redispatched",
                        "telemetry_id": record.telemetry_id, "task_id": task_id}
            record = self._load(task_id)
            if record is None:
                # A transition for a task this recorder never saw dispatched.
                # Not opened here: a row whose start time is "whenever this
                # process happened to attach" would make every duration wrong.
                return None
            if to_status == RUNNING:
                record.mark_running(self._now())
                action = "running"
            elif to_status == VERIFYING:
                record.mark_preview(self._now(), basis=PREVIEW_BASIS)
                action = "previewed"
            elif to_status in FINISHING_STATUSES:
                if to_status in EXCURSION_STATUSES:
                    record.record_excursion(to_status)
                self._apply_attributes(record, refresh=True)
                record.finish(to_status, when=self._now())
                action = "finished"
            else:
                return None
            self._save(record)
            return {"action": action, "telemetry_id": record.telemetry_id,
                    "task_id": task_id}

    # -- signals from instrumented call sites --------------------------------

    def note(self, task_id: str, kind: str, count: int = 1, *, source: str = "") -> bool:
        """Add a counted observation to a task's row.

        Buffered when the row does not exist yet, so a retrieval that happens
        between claim and dispatch still lands on the task it belongs to.
        """
        field = SIGNAL_ALIASES.get(kind)
        if not task_id or field is None or count <= 0:
            return False
        with self._lock:
            record = self._load(task_id)
            if record is None:
                self._pending.setdefault(task_id, {})
                self._pending[task_id][field] = (
                    self._pending[task_id].get(field, 0) + int(count))
                if source:
                    self._pending_sources.setdefault(task_id, set()).add(source)
                self._trim_pending()
                return True
            record.record_signal(field, int(count), source=source)
            self._save(record)
            return True

    def record_provider_usage(self, task_id: str, payload: Any, *,
                              provider: str | None = None) -> dict[str, Any]:
        """Record counters a runtime actually reported for this task.

        A payload with nothing readable in it leaves the row's usage exactly
        as it was -- silence is not a correction, and it is certainly not a
        zero.
        """
        with self._lock:
            record = self._load(task_id)
            if record is None:
                return {"recorded": False,
                        "reason": "no telemetry row for this task yet"}
            usage = record.record_provider_usage(payload, provider=provider,
                                                 reported_at=self._now())
            self._save(record)
            return {"recorded": usage.available(), "usage": usage.as_dict(),
                    "telemetry_id": record.telemetry_id}

    def mark_preview(self, task_id: str, *, basis: str,
                     when: str | None = None) -> bool:
        """A truer preview signal than the queue's own, from a caller that has
        one (a deploy preview, a screenshot artifact). The basis is required:
        a preview time with no stated basis is a number nobody can check."""
        if not basis.strip():
            return False
        with self._lock:
            record = self._load(task_id)
            if record is None:
                return False
            already = record.first_preview_at
            record.mark_preview(when or self._now(), basis=basis.strip())
            self._save(record)
            return already is None

    def row(self, task_id: str) -> dict[str, Any] | None:
        return self.store.for_task(task_id)

    # -- internals -----------------------------------------------------------

    def _now(self) -> str | None:
        return self._clock() if self._clock else None

    def _load(self, task_id: str) -> TaskTelemetry | None:
        raw = self.store.for_task(task_id)
        return TaskTelemetry.from_dict(raw) if raw else None

    def _open(self, task_id: str, *, lane: str | None) -> TaskTelemetry:
        record = self._load(task_id)
        if record is None:
            started = self._now()
            record = TaskTelemetry(task_id=task_id, lane=lane,
                                   **({"started_at": started} if started else {}))
            self._apply_attributes(record, refresh=False)
            self._drain_pending(record)
        return record

    def _apply_attributes(self, record: TaskTelemetry, *, refresh: bool) -> None:
        """Fill in what IS known about this task, and nothing that is not.

        A resolver that fails is recorded as a note on the row rather than
        being allowed to stop a dispatch: the cost of an unattributed row is
        a gap in a report, and the cost of raising here is a task that did
        not run.
        """
        if self._attributes is None:
            return
        try:
            attributes = self._attributes(record.task_id or "") or {}
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("work telemetry: attributes unavailable for %s: %s",
                            record.task_id, exc)
            record.note(f"attributes unavailable: {type(exc).__name__}")
            return
        for name in ATTRIBUTE_FIELDS:
            value = attributes.get(name)
            if value is None or value == "":
                continue
            if name == "redefine_count":
                # The planner's own count, which is the real record of how
                # many times this task was sent back. Taken as the truth
                # rather than added to, so a refresh cannot double it.
                try:
                    record.redefine_count = max(record.redefine_count, int(value))
                except (TypeError, ValueError):
                    continue
                continue
            if refresh or getattr(record, name, None) in (None, ""):
                setattr(record, name, value)

    def _trim_pending(self) -> None:
        """Drop the oldest buffered task once the bound is exceeded."""
        while len(self._pending) > MAX_PENDING_TASKS:
            oldest = next(iter(self._pending))
            self._pending.pop(oldest, None)
            self._pending_sources.pop(oldest, None)
            _LOGGER.warning("work telemetry: dropped buffered signals for %s -- "
                            "it was never dispatched", oldest)

    def _drain_pending(self, record: TaskTelemetry) -> None:
        buffered = self._pending.pop(record.task_id or "", None)
        sources = self._pending_sources.pop(record.task_id or "", set())
        if not buffered:
            return
        for field, count in buffered.items():
            record.record_signal(field, count)
        for source in sorted(sources):
            if source not in record.signal_sources:
                record.signal_sources = (*record.signal_sources, source)

    def _save(self, record: TaskTelemetry) -> None:
        self.store.save(record)


# -- resolving what is known about a dispatched task -------------------------

def queue_attributes(queue_store: Any) -> AttributeResolver:
    """Work id, project and lane, straight off the queue row.

    These are the attributes the queue itself holds: `work_id` is written
    into a task's metadata by `WorkService.plan`, and project/session are
    columns. Nothing here is inferred from a prompt.
    """
    def _resolve(task_id: str) -> dict[str, Any]:
        task = queue_store.get_task(task_id)
        if task is None:
            return {}
        metadata = getattr(task, "metadata", None) or {}
        return {"work_id": metadata.get("work_id"),
                "project_id": getattr(task, "project_id", None),
                "lane": getattr(task, "session", None)}
    return _resolve


def spec_attributes(spec_store: Any) -> AttributeResolver:
    """Module, execution mode, spec level and difficulty -- from the SPEC.

    The spec is where those facts were recorded when the work was planned, so
    reading them back is a lookup, not a classification. A task with no spec
    simply has none of them, which is why every one of these fields is
    nullable in the row.
    """
    def _resolve(task_id: str) -> dict[str, Any]:
        spec = _spec_for_queue_task(spec_store, task_id)
        if spec is None:
            return {}
        return {"module": getattr(spec, "likely_module", None),
                "execution_mode": getattr(spec, "execution_mode", None),
                "spec_level": spec.level() if hasattr(spec, "level") else None,
                "difficulty": getattr(spec, "difficulty", None),
                "work_id": getattr(spec, "work_id", None),
                "project_id": getattr(spec, "project_id", None),
                "redefine_count": getattr(spec, "redefine_count", None)}
    return _resolve


def _spec_for_queue_task(spec_store: Any, task_id: str) -> Any:
    finder = getattr(spec_store, "by_queue_task", None)
    if callable(finder):
        return finder(task_id)
    return None


def combined_attributes(*resolvers: AttributeResolver | None) -> AttributeResolver:
    """Ask each source in turn; the first to KNOW a field wins.

    Order matters and is the caller's choice: the spec knows the planning
    facts, the queue knows where the task actually ran. Neither is asked to
    guess the other's.
    """
    active = [r for r in resolvers if r is not None]

    def _resolve(task_id: str) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for resolver in active:
            try:
                found = resolver(task_id) or {}
            except Exception as exc:  # noqa: BLE001 -- one bad source, not a failure
                _LOGGER.warning("work telemetry: attribute source failed: %s", exc)
                continue
            for key, value in found.items():
                if value not in (None, "") and merged.get(key) in (None, ""):
                    merged[key] = value
        return merged
    return _resolve


# -- attaching to a live queue store -----------------------------------------

def fan_out(*sinks: Callable[[dict[str, Any]], None] | None
            ) -> Callable[[dict[str, Any]], None]:
    """One sink that feeds several. Used so attaching telemetry never has to
    displace the event bus, which is already on this hook."""
    active = [s for s in sinks if s is not None]

    def _sink(entry: dict[str, Any]) -> None:
        for sink in active:
            try:
                sink(entry)
            except Exception:  # noqa: BLE001 -- one consumer must not starve another
                _LOGGER.exception("work telemetry: a queue event sink raised")
    return _sink


def attach(queue_store: Any, recorder: RuntimeTelemetry) -> RuntimeTelemetry:
    """Put the recorder on a live QueueStore's event hook, additively.

    Whatever sink is already installed keeps receiving every event: this
    composes with it rather than replacing it, because the store holds one
    hook and the event bus is usually already on it.

    Attaching twice is a no-op. It has to be: a recorder wired onto the same
    store twice would see every DISPATCHING event twice and count two
    attempts where there was one, which is precisely the kind of quietly
    doubled number this whole module exists to prevent.
    """
    attached = list(getattr(queue_store, "_telemetry_recorders", ()))
    if any(existing is recorder for existing in attached):
        return recorder
    if attached:
        _LOGGER.warning("work telemetry: a second recorder is being attached to this "
                        "queue store; both will write rows for the same tasks")
    queue_store._event_sink = fan_out(getattr(queue_store, "_event_sink", None),
                                      recorder.sink())
    queue_store._telemetry_recorders = [*attached, recorder]
    return recorder


def install(*, queue_store: Any, telemetry_store: TelemetryStore | None = None,
            spec_store: Any = None, clock: Callable[[], str] | None = None
            ) -> RuntimeTelemetry:
    """The one call that wires efficiency telemetry into a running queue.

    Everything it needs is something the caller already has: the queue store
    whose transitions are the signal, and optionally the spec store that
    holds each task's module, mode, level and difficulty. Given no spec
    store, rows are still opened and closed on real transitions -- they just
    carry fewer attributes, which is the honest outcome rather than a
    classified guess.
    """
    recorder = RuntimeTelemetry(
        telemetry_store or TelemetryStore(),
        attributes=combined_attributes(
            spec_attributes(spec_store) if spec_store is not None else None,
            queue_attributes(queue_store)),
        clock=clock)
    return attach(queue_store, recorder)


# -- ambient recorder, for call sites that cannot be handed one --------------

# The retrieval code (context packs, similar-bug lookup, reuse analysis) is
# called from many places and knows nothing about telemetry. Threading a
# recorder through every signature would be a large, invasive change to code
# that has no other reason to know this module exists -- so instead a
# recorder is made active around the work, and those call sites report into
# whatever is active. With nothing active, `note` does nothing whatsoever.
_ACTIVE: ContextVar[tuple[RuntimeTelemetry, str] | None] = ContextVar(
    "terminal_mcp_work_telemetry", default=None)


@contextmanager
def observing(recorder: RuntimeTelemetry | None, task_id: str | None) -> Iterator[None]:
    """Make a recorder active for one task, for the duration of a block."""
    if recorder is None or not task_id:
        yield
        return
    token = _ACTIVE.set((recorder, task_id))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def active() -> tuple[RuntimeTelemetry, str] | None:
    return _ACTIVE.get()


def note(kind: str, count: int = 1, *, source: str = "") -> bool:
    """Report one counted observation to whatever recorder is active.

    Silent and false when nothing is active, which is the normal case for a
    process that has not opted in. Never raises: instrumentation that can
    break its own call site is worse than no instrumentation.
    """
    current = _ACTIVE.get()
    if current is None or count <= 0:
        return False
    recorder, task_id = current
    try:
        return recorder.note(task_id, kind, count, source=source)
    except Exception:  # noqa: BLE001 -- see the docstring
        _LOGGER.exception("work telemetry: signal %s not recorded", kind)
        return False
