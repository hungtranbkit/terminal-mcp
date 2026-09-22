"""P1.1 -- the per-project, event-driven coordinator (blg_324738a9eaf7).

WHAT WAS MISSING, precisely. `coordinator.CoordinatorGate` is real,
deterministic, fail-closed and production-used, but it answers ONE
question about ONE task: "is it safe to dispatch this?". `queue_loop.
QueueLoop` is what runs it, and it POLLS every lane on a timer. So there
was no component whose unit of attention is a PROJECT, and nothing that
reacted to the event bus at all -- the bus's own vocabulary named the
signals, `event_wiring` fed it, and nobody consumed it.

This module is that consumer, and it is deliberately small, because
almost everything it needs already exists.

WHAT IT IS NOT
--------------
It is NOT a second scheduler. It never dispatches, never sends to a
session, never mutates a queue task, and never starts a thread of its
own. `drain()` is a plain synchronous call that an existing loop (or a
test) invokes; wiring it to something that calls it is a separate,
deliberate act that this module does not perform. Dispatch safety stays
exactly where it is -- `CoordinatorGate`, deterministic, unchanged and
untouched by this file.

It also does not decide anything the deterministic gate decides. Its
output is a COORDINATION ACTION: an intent about scope, priority,
dependencies or clarification, published back onto the bus for whatever
applies it. Deciding and applying stay separate, exactly as
`CoordinatorGate.review()` decides and `queue_engine` applies.

ZERO NEW STATE, AND WHY THAT MATTERS
------------------------------------
This module adds no table, no database and no migration -- the same
constraint `project_service.py` holds itself to, for the same reason: a
coordinator keeping its own copy of what it had processed would
immediately be a second source of truth to drift from the bus.

Every durability guarantee is one the bus already provides:

  * PER-PROJECT SCOPE -- `read_since(consumer, project_id=...)` filters
    by project, and the consumer name embeds the project id, so two
    projects have two independent cursors and cannot observe or advance
    each other's position. Cross-project bleed is not prevented by a
    check that could be forgotten; it is unrepresentable.
  * RESTART RECOVERY -- the cursor IS the coordinator's state, and it
    lives in the bus database, not in a session's context. A restarted
    coordinator resumes at the last committed seq.
  * REPLAY / DOUBLE-ACTION -- every action is published with a
    deterministic `idempotency_key` derived from (project, source event,
    action). The bus returns the ORIGINAL event for a duplicate key and
    creates nothing, so replaying an event -- or two coordinators
    draining the same project concurrently -- produces one action, not
    two.
  * OUT-OF-ORDER -- `commit_cursor` is monotonic by construction: a
    lower seq is ignored rather than applied, so a late or replayed
    commit cannot rewind the stream and cause reprocessing.

FAIL CLOSED IS THE DEFAULT, NOT A BRANCH
----------------------------------------
`decide()` starts from HOLD and only leaves it when something positively
establishes otherwise. An unknown project, an unreadable project state, a
payload that is not a mapping, a missing entity the action would need, an
event whose project does not match the one being drained -- each is a
HOLD carrying the reason, never a guess and never a pass. That is the
same posture `CoordinatorGate` takes with unreadable evidence, and it is
the reason this module can be woken by an untrusted stream at all.

LEGACY BEHAVIOUR IS THE DEFAULT
-------------------------------
`enabled` defaults to False. A disabled coordinator reads nothing,
publishes nothing and -- importantly -- does NOT advance its cursor, so
turning the feature on later does not silently skip everything that
happened while it was off. A deployment that never enables this module
behaves exactly as it did before the file existed.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

_LOGGER = logging.getLogger(__name__)

# -- Coordination actions -------------------------------------------------
#
# Published back onto the bus as event types. Declared here rather than in
# event_bus.KNOWN_EVENT_TYPES so this feature adds nothing to a module
# several other lanes are editing; the bus does not validate types.

HOLD = "COORDINATION_HOLD"
"""The fail-closed default: this coordinator declined to act, and the
reason says why. A HOLD is a real, published decision rather than
silence, so "nothing happened" and "something decided nothing should
happen" are distinguishable afterwards."""
NOOP = "COORDINATION_NOOP"
"""The event is legitimately not this coordinator's business."""
CLARIFY = "COORDINATION_CLARIFY"
"""`scope_reasoner` judged the scope too unclear to coordinate. This is
the LLM seam the roadmap names -- the default reasoner is
coordinator._default_scope_reasoner's crude length heuristic."""
PRIORITISE = "COORDINATION_PRIORITISE"
DECOMPOSE = "COORDINATION_DECOMPOSE"
DEPENDENCY_BLOCKED = "COORDINATION_DEPENDENCY_BLOCKED"

ACTION_TYPES = (HOLD, NOOP, CLARIFY, PRIORITISE, DECOMPOSE, DEPENDENCY_BLOCKED)

# -- Hold reasons ---------------------------------------------------------

REASON_UNKNOWN_PROJECT = "UNKNOWN_PROJECT"
REASON_PROJECT_UNREADABLE = "PROJECT_UNREADABLE"
REASON_PROJECT_PAUSED = "PROJECT_PAUSED"
REASON_FOREIGN_EVENT = "FOREIGN_EVENT"
REASON_MALFORMED_PAYLOAD = "MALFORMED_PAYLOAD"
REASON_MISSING_ENTITY = "MISSING_ENTITY"
REASON_STALE_EVENT = "STALE_EVENT"
REASON_UNCLEAR_SCOPE = "UNCLEAR_SCOPE"
REASON_BLOCKED_DEPENDENCY = "BLOCKED_DEPENDENCY"
REASON_OWN_OUTPUT = "OWN_OUTPUT"

CONSUMER_PREFIX = "project-coordinator"


def consumer_name(project_id: str) -> str:
    """The bus consumer identity for one project.

    The project id is IN the name, which is what makes per-project
    cursors independent: `commit_cursor` is keyed on (consumer,
    project_id), so one project advancing cannot move another's
    high-water mark even if a caller passed the wrong scope."""
    return f"{CONSUMER_PREFIX}:{project_id}"


def action_idempotency_key(project_id: str, event_id: str, action: str) -> str:
    """Deterministic identity of one coordination action.

    Derived from the SOURCE EVENT rather than from a timestamp or a
    uuid, which is what makes replay safe: the same event re-delivered
    after a crash, a lease expiry or a manual retry produces the same
    key, and the bus's UNIQUE constraint turns the second publish into a
    no-op that returns the first action."""
    return f"coord:{project_id}:{event_id}:{action}"


@dataclass(frozen=True)
class CoordinationAction:
    """One decision about one event. Pure data -- `decide()` returns it
    without having touched the bus, which is what makes the whole
    decision table testable without a database."""
    action: str
    reason: str | None = None
    project_id: str | None = None
    source_event_id: str | None = None
    source_seq: int | None = None
    entity_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_hold(self) -> bool:
        return self.action == HOLD


# Which bus event types this coordinator has an opinion about. Anything
# else is a NOOP -- listed explicitly so a new event type added elsewhere
# does not silently acquire coordination behaviour nobody designed.
_DECOMPOSE_TRIGGERS = ("GOAL_SUBMITTED", "REQUIREMENT_CHANGED")
_PRIORITISE_TRIGGERS = ("TASK_CREATED", "TASK_READY", "WORKER_IDLE")
_DEPENDENCY_TRIGGERS = ("TASK_BLOCKED", "MERGE_CONFLICT", "TEST_FAILED")
_CLARIFY_CANDIDATES = _DECOMPOSE_TRIGGERS + _PRIORITISE_TRIGGERS

HANDLED_EVENT_TYPES = tuple(dict.fromkeys(
    _DECOMPOSE_TRIGGERS + _PRIORITISE_TRIGGERS + _DEPENDENCY_TRIGGERS))


def _payload_of(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """The event's payload as a mapping, or None if it is anything else.

    None is a HOLD upstream, never an empty dict: a payload that failed
    to parse and a payload that was genuinely empty are different facts,
    and treating the first as the second is how a coordinator starts
    acting on data it never read."""
    raw = event.get("payload")
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def decide(event: Mapping[str, Any], *, project_id: str,
           project_state: Mapping[str, Any] | None,
           scope_reasoner: Callable[[str], str | None] | None = None,
           last_acted_seq: int = 0) -> CoordinationAction:
    """The whole decision table, as one pure function.

    Deliberately takes `project_state` as data rather than a service, so
    every branch below -- including the ones that only happen when a
    project is missing, paused or unreadable -- is reachable in a test
    without constructing the world.

    Ordering of the checks is itself the safety property: identity and
    readability are settled BEFORE anything looks at the payload, and the
    payload is settled before anything reasons about scope. A later check
    can therefore never rescue an earlier failure."""
    source_event_id = str(event.get("id") or "")
    seq = event.get("seq")
    seq = int(seq) if isinstance(seq, int) else None
    base = {"project_id": project_id, "source_event_id": source_event_id, "source_seq": seq}

    # 0. Never react to this coordinator's OWN output. Actions are
    #    published onto the same project stream the coordinator reads, so
    #    without this every drain would decide something about the
    #    previous drain's decisions and the stream would grow on every
    #    call -- found by the first end-to-end run of this module, not by
    #    reasoning about it. Checked two ways because either alone is one
    #    refactor away from being wrong: by actor (who wrote it) and by
    #    type (what it is).
    if event.get("actor") == CONSUMER_PREFIX or str(event.get("type") or "") in ACTION_TYPES:
        return CoordinationAction(NOOP, REASON_OWN_OUTPUT, **base)

    # 1. Identity. An event from another project must never be acted on
    #    under this project's name, even if a caller hands it over.
    event_project = event.get("project_id")
    if event_project != project_id:
        return CoordinationAction(HOLD, REASON_FOREIGN_EVENT, **base,
                                  detail={"event_project_id": event_project})

    # 2. Source of truth. Unknown or unreadable is a HOLD, never a pass:
    #    the coordinator cannot reason about a project it cannot read.
    if project_state is None:
        return CoordinationAction(HOLD, REASON_UNKNOWN_PROJECT, **base)
    if project_state.get("error"):
        return CoordinationAction(HOLD, REASON_PROJECT_UNREADABLE, **base,
                                  detail={"error": project_state.get("error")})
    if project_state.get("paused"):
        return CoordinationAction(HOLD, REASON_PROJECT_PAUSED, **base)

    # 3. Freshness. A seq at or below what this project has already acted
    #    on is a replay or an out-of-order redelivery. It is held rather
    #    than re-decided, so a late event can never overwrite a decision
    #    made from newer information.
    if seq is not None and last_acted_seq and seq <= last_acted_seq:
        return CoordinationAction(HOLD, REASON_STALE_EVENT, **base,
                                  detail={"last_acted_seq": last_acted_seq})

    # 4. Payload. Unparseable is a HOLD -- see _payload_of.
    payload = _payload_of(event)
    if payload is None:
        return CoordinationAction(HOLD, REASON_MALFORMED_PAYLOAD, **base)

    event_type = str(event.get("type") or "")
    if event_type not in HANDLED_EVENT_TYPES:
        return CoordinationAction(NOOP, None, **base)

    entity_id = event.get("entity_id") or payload.get("task_id") or payload.get("entity_id")

    # 5. Dependencies. A blocked dependency is reported as blocked and
    #    never coordinated around -- resolving it is a human/LLM call,
    #    and guessing an order is exactly what fail-closed forbids.
    blocked_by = payload.get("blocked_by") or payload.get("depends_on") or ()
    if event_type in _DEPENDENCY_TRIGGERS or blocked_by:
        if not entity_id:
            return CoordinationAction(HOLD, REASON_MISSING_ENTITY, **base)
        return CoordinationAction(
            DEPENDENCY_BLOCKED, REASON_BLOCKED_DEPENDENCY, **base, entity_id=str(entity_id),
            detail={"blocked_by": list(blocked_by) if isinstance(blocked_by, Sequence)
                    and not isinstance(blocked_by, str) else [blocked_by]})

    # 6. Scope. THE LLM SEAM: the same pluggable `scope_reasoner`
    #    contract coordinator.py already exposes -- text in, a reason to
    #    refuse or None. A reasoner that raises is treated as a refusal,
    #    not as approval, because an exception is an absence of judgment
    #    and this module never reads an absence as a yes.
    if event_type in _CLARIFY_CANDIDATES and scope_reasoner is not None:
        text = str(payload.get("goal") or payload.get("prompt") or payload.get("title") or "")
        try:
            unclear = scope_reasoner(text)
        except Exception as exc:  # noqa: BLE001 -- see comment above
            return CoordinationAction(HOLD, REASON_UNCLEAR_SCOPE, **base,
                                      detail={"reasoner_error": f"{type(exc).__name__}: {exc}"})
        if unclear:
            return CoordinationAction(CLARIFY, REASON_UNCLEAR_SCOPE, **base,
                                      entity_id=str(entity_id) if entity_id else None,
                                      detail={"scope_reason": unclear})

    if event_type in _DECOMPOSE_TRIGGERS:
        return CoordinationAction(DECOMPOSE, None, **base,
                                  entity_id=str(entity_id) if entity_id else None)

    if not entity_id:
        return CoordinationAction(HOLD, REASON_MISSING_ENTITY, **base)
    return CoordinationAction(PRIORITISE, None, **base, entity_id=str(entity_id))


class ProjectCoordinator:
    """Per-project event consumer. One instance can serve every project;
    scope is a `drain()` argument, not construction state, so nothing
    about one project is cached across a call into another."""

    def __init__(self, *, bus: Any, project_resolver: Callable[[str], Mapping[str, Any] | None] | None = None,
                 scope_reasoner: Callable[[str], str | None] | None = None,
                 action_sink: Callable[[CoordinationAction], None] | None = None,
                 enabled: bool = False, batch_size: int = 50) -> None:
        self.bus = bus
        # Injected rather than importing ProjectService: this module has no
        # business constructing the world to read one project, and every
        # fail-closed branch above needs to be reachable from a test that
        # simply returns None or an error dict.
        self.project_resolver = project_resolver
        self.scope_reasoner = scope_reasoner
        # Applying an action is deliberately somebody else's job and OFF
        # by default. A coordinator that both decided and applied would
        # be the second scheduler this feature is explicitly not allowed
        # to become.
        self.action_sink = action_sink
        self.enabled = enabled
        self.batch_size = max(1, int(batch_size))

    # -- reads ----------------------------------------------------------

    def cursor(self, project_id: str) -> int:
        return int(self.bus.cursor(consumer_name(project_id), project_id=project_id)["last_seq"])

    def _resolve_project(self, project_id: str) -> Mapping[str, Any] | None:
        """Fail closed on ANY resolver failure. A resolver that raises is
        indistinguishable from a project that cannot be read, and both
        must hold rather than proceed."""
        if self.project_resolver is None:
            return None
        try:
            return self.project_resolver(project_id)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("project resolver failed for %s: %s", project_id, exc)
            return {"error": "PROJECT_RESOLVER_FAILED", "detail": str(exc)}

    # -- the one entry point --------------------------------------------

    def drain(self, project_id: str, *, max_events: int | None = None) -> dict[str, Any]:
        """Process this project's unread events once and return what was
        decided. Synchronous and finite -- it never loops waiting for
        more, because owning a loop is what would make this a scheduler.

        Disabled is a true no-op: nothing read, nothing published, and
        the cursor deliberately NOT advanced, so enabling the feature
        later resumes from where the stream actually was rather than
        silently skipping everything that happened meanwhile."""
        if not self.enabled:
            return {"project_id": project_id, "enabled": False, "processed": 0,
                    "actions": [], "cursor": self.cursor(project_id)}
        if not project_id or not str(project_id).strip():
            return {"project_id": project_id, "enabled": True, "processed": 0, "actions": [],
                    "error": "INVALID_REQUEST", "detail": "project_id is required"}

        limit = self.batch_size if max_events is None else max(1, int(max_events))
        consumer = consumer_name(project_id)
        events = self.bus.read_since(consumer, project_id=project_id, limit=limit)
        project_state = self._resolve_project(project_id) if events else None

        actions: list[dict[str, Any]] = []
        highest_seq = 0
        acted_seq = self.cursor(project_id)
        for event in events:
            action = decide(event, project_id=project_id, project_state=project_state,
                            scope_reasoner=self.scope_reasoner, last_acted_seq=acted_seq)
            # A NOOP is a decision that this event is not this
            # coordinator's business, and publishing one would put a new
            # event on the very stream being read -- the feedback loop
            # this module's `decide` step 0 also guards. It is reported
            # to the caller and advances the cursor, but never published.
            if action.action == NOOP:
                actions.append({"action": NOOP, "reason": action.reason,
                                "source_event_id": action.source_event_id,
                                "published": False, "duplicate": False, "detail": action.detail})
            else:
                actions.append(self._publish(action))
            seq = event.get("seq")
            if isinstance(seq, int):
                highest_seq = max(highest_seq, seq)

        # Cursor advances only after every event in the batch has a
        # published decision. A crash mid-batch therefore re-reads the
        # whole batch, which is safe precisely because each action's
        # idempotency key makes the republish a no-op.
        if highest_seq:
            self.bus.commit_cursor(consumer, highest_seq, project_id=project_id)

        return {"project_id": project_id, "enabled": True, "processed": len(events),
                "actions": actions, "cursor": self.cursor(project_id)}

    def _publish(self, action: CoordinationAction) -> dict[str, Any]:
        """Publish one action, then hand it to the sink only if THIS call
        actually created it. A duplicate must not re-trigger the sink --
        that is the difference between an idempotent record and an
        idempotent effect."""
        key = action_idempotency_key(action.project_id or "", action.source_event_id or "",
                                     action.action)
        event = self.bus.publish(
            action.action, project_id=action.project_id, entity_type="coordination",
            entity_id=action.entity_id, causation_id=action.source_event_id or None,
            idempotency_key=key, actor=CONSUMER_PREFIX,
            payload={"reason": action.reason, "source_event_id": action.source_event_id,
                     "source_seq": action.source_seq, **action.detail})
        duplicate = bool(event.get("duplicate"))
        if not duplicate and self.action_sink is not None:
            try:
                self.action_sink(action)
            except Exception:  # noqa: BLE001 -- a sink must never break the stream
                _LOGGER.exception("coordination action sink failed for %s", action.action)
        return {"action": action.action, "reason": action.reason, "entity_id": action.entity_id,
                "source_event_id": action.source_event_id, "event_id": event.get("id"),
                "published": True, "duplicate": duplicate, "detail": action.detail}
