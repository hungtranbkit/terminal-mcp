"""The Supervisor Queue v2 dispatch loop (task: "Supervisor Queue v2
Phase 2 -- Coordinator Agent").

Role split (see coordinator.py's own docstring for the full reasoning):
this module is the deterministic Scheduler/Queue Manager half -- it
claims tasks, calls the Coordinator Agent for a gate review, dispatches
READY tasks, and detects completion. It never decides on its own
whether a task is safe to run; every dispatch is gated by a prior
CoordinatorGate.review() call that returned READY.

REUSE (explicit instruction: "Reuse Supervisor v2 Phase 1 claim/
decision/idempotent-send/completion verifier thay vì viết lại"):
  - dispatch's own send goes through the EXACT SAME `terminal_send_text`
    (core.py) every other send path uses, via `idempotency_key` derived
    from (task_id, attempt) -- the SAME durable-across-restart dedup
    primitive P0-4 built, not a second one (item 8/9).
  - completion detection reuses status.py's own classify_status (already
    computed inside terminal_status -- this module just reads the
    `state` field back) and its structured completion-marker protocol
    (parse_completion_marker/verify_completion_marker, task_id+attempt+
    nonce-bound) -- the SAME protocol supervisor.py's own COMPLETION_
    CANDIDATE -> VERIFIED_DONE promotion uses (item 11).
  - the sensitive/destructive-prompt screen inside CoordinatorGate reuses
    supervisor2.py's own ATTENTION_STOP_PATTERNS content.

`tick(session)` is the single public entry point -- one full
reconciliation step for one session's lane, safe to call from a test
directly (deterministic, no thread), from a manual MCP tool
(terminal_queue_run_once, mirroring supervisor.py's own
supervisor_run_once), or from a real background poll loop (not wired to
run automatically in this phase -- see QueueEngine's own docstring for
the explicit opt-in-per-session feature flag this requires).

SAFETY: nothing in this module contains a session-name allow list. The
constraint that `window`/`window2` (or any other real production
session) must never have auto-dispatch enabled until this feature's own
acceptance demo passes and the user/ChatGPT explicitly confirms is
enforced by `queue_lanes.auto_dispatch_enabled` -- an explicit,
per-session, OFF-by-default opt-in column (see queue_store.py's
migration v3) that `tick()` itself refuses to bypass. A caller invoking
tick() directly (e.g. a test, or a manual terminal_queue_run_once tool
call) can still exercise one session on demand regardless of that flag
-- the flag only gates an AUTOMATIC background loop from ever touching
a lane on its own; see server_http.py's own wiring (not yet added in
this phase -- see the final report's own limitations section) for where
that automatic loop would start.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .coordinator import CoordinatorGate, OtherLaneSnapshot, SessionSnapshot
from .queue_store import (
    BLOCKED, COMPLETED, DISPATCH_UNCERTAIN, DISPATCHING, FAILED, PRECHECK, QUEUED, READY, RUNNING, VERIFYING,
    WAITING_SESSION, QueueStore, QueueTask,
)
from .status import parse_completion_marker, verify_completion_marker

DEFAULT_CLAIMED_BY = "queue-engine"
DEFAULT_LEASE_SECONDS = 300.0
DEFAULT_UNCERTAIN_GRACE_SECONDS = 60.0

SESSION_UNREACHABLE_ERRORS = frozenset({"SESSION_NOT_FOUND", "NODE_UNREACHABLE", "AMBIGUOUS_SESSION"})
"""P0 (task: "persist-before-dispatch", item 9): these specific
terminal_status/controller error codes mean the SESSION/NODE itself
isn't reachable right now -- routine and auto-recoverable, never a
coordinator refusal or an execution failure. Any OTHER error string is
left to whichever existing path already handles it (NEEDS_HUMAN in
_review, FAILED in _check_completion) -- this set is deliberately
narrow, not a catch-all."""


class SessionOps(Protocol):
    """The narrow slice of ControllerService (or any equivalent, e.g. a
    fake in tests) this engine needs -- deliberately the same shape as
    NodeClient's own methods (node_client.py), so a real
    ControllerService instance satisfies this with zero adapter code.
    Every response is expected to carry `node_id` the way
    ControllerService._route already merges it into every routed
    result."""

    def terminal_status(self, session: str) -> dict[str, Any]: ...
    def terminal_tail(self, session: str, lines: int | None = None) -> dict[str, Any]: ...
    def terminal_send_text(self, session: str, text: str, press_enter: bool = False, dry_run: bool = False,
                           **kwargs: Any) -> dict[str, Any]: ...


def idempotency_key_for(task_id: str, attempt: int) -> str:
    """Deterministic, durable-across-restart dedup key (item 8/9) -- the
    SAME (task_id, attempt) pair always produces the SAME key, so a
    re-dispatch of the same attempt (e.g. after a DELIVERY_UNKNOWN
    reconciliation, or an engine restart mid-dispatch) hits core.py's
    existing idempotent_sends store and gets the ORIGINAL result back
    instead of sending twice -- see core.py's terminal_send_text
    docstring."""
    return f"queue:{task_id}:{attempt}"


REQUIREMENTS_REMINDER = (
    "Before coding: read docs/REQUIREMENTS.md (and its own Backlog section) so you know what "
    "the system already has. If this task changes behavior/contract (not a pure refactor/chore), "
    "update the relevant entry in docs/REQUIREMENTS.md in place before this task is done -- the "
    "Integration review gate checks for this and returns REWORK_REQUIRED if it's missing."
)
"""Living-requirements convention (task: 'sau khi hoàn tất + verify một
feature... phải cập nhật living requirements/spec'): a short, clearly-
delimited reminder appended to every dispatched task, same posture as
the completion-marker instruction right below it -- never rewrites or
reinterprets the task's own prompt, which stays verbatim and first.
Enforcement itself lives in integration_reviewer.py (the one place a
real diff already exists to check against), not here -- this is only
the worker-facing half of the mechanism."""


def build_dispatch_text(task: QueueTask, *, nonce: str) -> str:
    """The 'wrapper rất ngắn' item 7 explicitly allows and limits: the
    task's own prompt is included VERBATIM, first, unmodified -- nothing
    here rewrites or reinterprets the business request. Only a short,
    clearly-delimited completion-marker instruction (and, per the
    living-requirements convention, a one-line docs reminder) is
    appended, reusing status.py's own COMPLETION_MARKER_RE protocol
    (task_id+attempt+nonce-bound) so a real, verifiable completion
    signal is possible instead of relying on a bare 'final report'
    heuristic (item 11)."""
    return (
        f"{task.prompt}\n\n"
        f"---\n"
        f"{REQUIREMENTS_REMINDER}\n\n"
        f"When (and only when) the above task is FULLY complete, print exactly one line in this "
        f"exact format (once), then stop:\n"
        f"###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 task_id={task.id} "
        f"attempt={task.attempt_count + 1} nonce={nonce} status=completion_candidate "
        f"summary_sha256={hashlib.sha256(task.id.encode()).hexdigest()[:16]}###\n"
    )


@dataclass(frozen=True)
class TickResult:
    session: str
    action: str  # IDLE | PAUSED | CLAIMED | COORDINATOR_* | DISPATCHED | RUNNING | VERIFYING | COMPLETED | NO_OP | ...
    task_id: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"session": self.session, "action": self.action, "task_id": self.task_id, "detail": self.detail}


class QueueEngine:
    def __init__(self, store: QueueStore, ops: SessionOps, *, coordinator: CoordinatorGate | None = None,
                claimed_by: str = DEFAULT_CLAIMED_BY, lease_seconds: float = DEFAULT_LEASE_SECONDS,
                on_completed: Callable[[QueueTask], None] | None = None) -> None:
        self.store = store
        self.ops = ops
        self.coordinator = coordinator or CoordinatorGate()
        self.claimed_by = claimed_by
        self.lease_seconds = lease_seconds
        # 3-role model follow-up ("publish immutable handoff... rồi lập
        # tức nhận feature task kế tiếp"): an OPTIONAL callback invoked
        # with the freshly-COMPLETED task, right after mark_completed_
        # with_evidence succeeds -- deliberately NOT a direct import of
        # integration_store.py here (queue_engine.py stays fully
        # decoupled from the Integration Agent's own store/schema; see
        # integration_store.py's own module docstring on why they're
        # separate). mcp_app.py wires this to a small publish-handoff
        # glue function for any task whose own metadata declares
        # integration_required. Never blocks/fails the task's own
        # COMPLETED transition if the callback itself raises.
        self.on_completed = on_completed

    def tick(self, session: str) -> TickResult:
        """One full reconciliation step for `session`'s lane. Always
        reconciles stale claims FIRST (restart safety, item 8), then
        does at most one meaningful step forward (claim, coordinator
        review, dispatch, or completion check) -- never more than one
        state transition per call, so a test (or an operator watching
        terminal_queue_events) can observe each step individually."""
        self.store.reconcile_stale_claims(session)
        self.store.reconcile_uncertain_and_waiting(session)
        lane = self.store.lane_status(session)
        if lane["paused"]:
            return TickResult(session, "PAUSED", detail=lane.get("paused_reason") or "")

        active = lane["current_task"]
        if active is None:
            claimed = self.store.claim_next_task(session, claimed_by=self.claimed_by,
                                                  lease_seconds=self.lease_seconds)
            if claimed is None:
                return TickResult(session, "IDLE")
            return TickResult(session, "CLAIMED", task_id=claimed.id)

        task_id = active["id"]
        status = active["status"]
        if status == PRECHECK:
            return self._review(session, task_id)
        if status == READY:
            return self._dispatch(session, task_id)
        if status in (RUNNING, VERIFYING):
            return self._check_completion(session, task_id, status)
        if status == DISPATCH_UNCERTAIN:
            return self._recheck_uncertain(session, task_id)
        if status == WAITING_SESSION:
            return self._recheck_waiting_session(session, task_id)
        return TickResult(session, "NO_OP", task_id=task_id, detail=f"status={status}")

    # -- coordinator review ------------------------------------------------

    def _review(self, session: str, task_id: str) -> TickResult:
        task = self.store.get_task(task_id)
        status_response = self.ops.terminal_status(session)
        error = status_response.get("error")
        if error in SESSION_UNREACHABLE_ERRORS:
            # P0 item 9: the session/node itself isn't there -- WAITING_
            # SESSION, never NEEDS_HUMAN/PAUSED and never dropped. Skip
            # the coordinator gate entirely (nothing to review yet).
            self.store.mark_waiting_session(task_id, reason=error)
            return TickResult(session, "WAITING_SESSION", task_id=task_id, detail=error)
        session_snapshot = SessionSnapshot(
            node_id=status_response.get("node_id"), cwd=status_response.get("cwd"),
            current_command=status_response.get("current_command"),
            error=error,
            # Production-readiness pass: real fields straight off the
            # routed terminal_status response (core.py's _status_payload
            # -- state/input_required always present; reader_alive only
            # ever present for a Windows-backed session, None on tmux).
            state=status_response.get("state"), input_required=status_response.get("input_required"),
            reader_alive=status_response.get("reader_alive"),
        )
        other_active = tuple(
            OtherLaneSnapshot(session=lane["session"],
                             node_id=(lane["current_task"] or {}).get("node_id"),
                             cwd=None)  # populated below only for lanes actually queried
            for lane in self.store.list_all_lanes()
            if lane["session"] != session and lane["current_task"] is not None
        )
        # Cheaply enrich other_active with a real observed cwd ONLY for
        # lanes that actually have something in flight right now (bounded
        # by however many OTHER lanes exist -- never unbounded, never
        # touches a lane with nothing active).
        enriched = []
        for other in other_active:
            try:
                other_status = self.ops.terminal_status(other.session)
                enriched.append(OtherLaneSnapshot(session=other.session, node_id=other_status.get("node_id"),
                                                  cwd=other_status.get("cwd")))
            except Exception:  # noqa: BLE001 -- best-effort enrichment only; never blocks this task's own review
                enriched.append(other)

        decision = self.coordinator.review(task, store=self.store, session=session_snapshot,
                                          other_active=tuple(enriched))
        self.store.record_coordinator_decision(
            task_id, status=decision.status, reason=decision.reason, blockers=list(decision.blockers),
            required_actions=list(decision.required_actions), evidence=decision.evidence,
        )
        return TickResult(session, f"COORDINATOR_{decision.status}", task_id=task_id, detail=decision.reason)

    # -- dispatch -----------------------------------------------------------

    def _dispatch(self, session: str, task_id: str) -> TickResult:
        task = self.store.get_task(task_id)
        nonce = self.store.ensure_verification_nonce(task_id)
        # STICKY idempotency key (item 8/9, and the real Phase 2 fix
        # this closes -- see queue_store.py's own dispatch_idempotency_
        # key column docstring): if this task already has one (a prior
        # claim cycle already attempted a send whose outcome is still
        # genuinely unconfirmed -- e.g. reconciled back to QUEUED by a
        # stale-lease sweep after an engine crash), REUSE it rather than
        # minting a new one. attempt_count still bumps on every
        # DISPATCHING transition (for audit visibility -- "how many
        # times has this been attempted"), but the ACTUAL key used for
        # terminal_send_text's own dedup only changes on a genuinely NEW
        # attempt (an explicit operator retry_task after a real BLOCKED/
        # FAILED, which clears it).
        idempotency_key = task.dispatch_idempotency_key or idempotency_key_for(task_id, task.attempt_count + 1)
        dispatch_text = build_dispatch_text(task, nonce=nonce)
        self.store.transition_task(task_id, DISPATCHING, event_type="DISPATCHED",
                                   extra_fields={"dispatch_idempotency_key": idempotency_key})

        response = self.ops.terminal_send_text(session, dispatch_text, press_enter=True,
                                               idempotency_key=idempotency_key)
        delivery_state = response.get("delivery_state")
        if response.get("error"):
            self.store.transition_task(task_id, BLOCKED, event_type="BLOCKED",
                                       reason=f"send failed: {response['error']}")
            return TickResult(session, "BLOCKED", task_id=task_id, detail=str(response["error"]))
        if delivery_state == "DELIVERY_UNKNOWN":
            # P0 item 2: never resend blindly, and never silently look
            # like an ordinary QUEUED task either -- DISPATCH_UNCERTAIN,
            # KEEPING the same dispatch_idempotency_key (the outcome is
            # genuinely unknown -- if the send actually went through,
            # core.py's own idempotent_sends store will return that
            # original result the next time this exact key is reused,
            # rather than sending twice). _recheck_uncertain resolves
            # this on a later tick: real evidence of activity -> RUNNING,
            # else a grace period -> QUEUED for a fresh attempt.
            self.store.mark_dispatch_uncertain(task_id, reason="delivery_state=DELIVERY_UNKNOWN")
            return TickResult(session, "DISPATCH_UNCERTAIN", task_id=task_id)
        self.store.transition_task(task_id, RUNNING, event_type="STARTED")
        return TickResult(session, "DISPATCHED", task_id=task_id, detail=idempotency_key)

    # -- P0 persist-before-dispatch: uncertain/waiting reconciliation -------

    def _recheck_uncertain(self, session: str, task_id: str) -> TickResult:
        """A DISPATCH_UNCERTAIN task, revisited on a later tick (item 2):
        if the session now shows real activity (RUNNING, or simply no
        longer showing an error), the send almost certainly landed --
        promote straight to RUNNING rather than waiting out the full
        grace period pointlessly. Otherwise leave it for reconcile_
        uncertain_and_waiting's own grace-period timeout to eventually
        return it to QUEUED (already run once at the top of this same
        tick, before this method is ever reached -- so by construction
        this branch only sees a task still genuinely within its grace
        window)."""
        status_response = self.ops.terminal_status(session)
        if not status_response.get("error") and status_response.get("state") == "RUNNING":
            self.store.transition_task(task_id, RUNNING, event_type="STARTED",
                                       reason="confirmed real activity after DISPATCH_UNCERTAIN")
            return TickResult(session, "RUNNING", task_id=task_id, detail="confirmed after uncertain dispatch")
        return TickResult(session, "NO_OP", task_id=task_id, detail="still DISPATCH_UNCERTAIN, within grace period")

    def _recheck_waiting_session(self, session: str, task_id: str) -> TickResult:
        """A WAITING_SESSION task, revisited on a later tick (item 9): if
        the session is resolvable again RIGHT NOW, don't wait out the
        rest of the grace period -- return it to QUEUED immediately for
        a fresh claim+review (WAITING_SESSION's only outgoing edge;
        never resumes "in place")."""
        status_response = self.ops.terminal_status(session)
        if not status_response.get("error"):
            self.store.transition_task(task_id, QUEUED, event_type="SESSION_RECOVERED",
                                       reason="session resolvable again",
                                       extra_fields={"uncertain_or_waiting_since": None})
            return TickResult(session, "QUEUED", task_id=task_id, detail="session recovered")
        return TickResult(session, "NO_OP", task_id=task_id, detail="still WAITING_SESSION, within grace period")

    # -- completion detection -----------------------------------------------

    def _check_completion(self, session: str, task_id: str, current_status: str) -> TickResult:
        task = self.store.get_task(task_id)
        status_response = self.ops.terminal_status(session)
        error = status_response.get("error")
        if error in SESSION_UNREACHABLE_ERRORS:
            # P0 item 9: the session/node vanished mid-flight -- WAITING_
            # SESSION (auto-recoverable), never FAILED (a real execution
            # failure) and never silently dropped.
            self.store.mark_waiting_session(task_id, reason=error)
            return TickResult(session, "WAITING_SESSION", task_id=task_id, detail=error)
        if error:
            self.store.transition_task(task_id, FAILED, event_type="FAILED",
                                       reason=f"could not read session status: {error}")
            return TickResult(session, "FAILED", task_id=task_id, detail=str(error))

        state = status_response.get("state")
        if current_status == RUNNING:
            if state == "RUNNING":
                return TickResult(session, "RUNNING", task_id=task_id)
            # Anything else (IDLE/WAITING_INPUT/UNKNOWN) -- the agent has
            # gone quiet; move to VERIFYING to look for real evidence.
            self.store.transition_task(task_id, VERIFYING, event_type="VERIFYING")
            return TickResult(session, "VERIFYING", task_id=task_id, detail=f"state={state}")

        # current_status == VERIFYING: look for a verified completion
        # marker -- item 11's "không phụ thuộc heuristic final report
        # đơn thuần... fallback cần explicit coordinator verification".
        # No marker found is NOT treated as failure and is NOT treated
        # as silent success either -- the task simply stays in
        # VERIFYING, awaiting either a later marker or an explicit
        # terminal_queue_verify call (queue_service.py) -- there is no
        # code path here that reaches COMPLETED without real evidence.
        capture = self.ops.terminal_tail(session, 200)
        output = capture.get("output", "")
        marker = parse_completion_marker(output)
        verified = verify_completion_marker(
            marker, task_id=task_id, attempt=task.attempt_count, nonce=task.verification_nonce,
            nonce_consumed=False,
        )
        if verified:
            completed = self.store.mark_completed_with_evidence(task_id, evidence={"completion_marker": marker})
            self._notify_completed(completed)
            return TickResult(session, "COMPLETED", task_id=task_id)
        if state == "RUNNING":
            # A false alarm -- the agent picked back up (e.g. a slow
            # first response mistaken for quiet). Re-arm.
            self.store.transition_task(task_id, RUNNING, event_type="RUNNING")
            return TickResult(session, "RUNNING", task_id=task_id, detail="false alarm, re-armed")
        return TickResult(session, "AWAITING_VERIFICATION", task_id=task_id,
                          detail="no verified completion marker yet")

    def _notify_completed(self, task: QueueTask) -> None:
        if self.on_completed is None:
            return
        try:
            self.on_completed(task)
        except Exception:  # noqa: BLE001 -- a handoff-publishing glitch must never un-complete a real task
            pass
