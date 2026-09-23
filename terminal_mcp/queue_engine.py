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

import logging
import re
import logging

import hashlib
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, replace
from typing import Any, Callable, Protocol

from .coordinator import CoordinatorGate, OtherLaneSnapshot, SessionSnapshot, PLAIN_SHELL_COMMANDS
from . import delivery_gate, retry_recovery
from .queue_store import (
    BLOCKED, COMPLETED, DISPATCH_UNCERTAIN, DISPATCHING, FAILED, PRECHECK, QUEUED, READY, RUNNING, VERIFYING,
    WAITING_SESSION, QueueStore, QueueTask, RequirementsNotCoveredError, iso_now,
)
from .status import COMPLETION_MARKER_RE, parse_completion_marker, verify_completion_marker
from .request_governor import RequestGovernor
from .runbook_registry import RunbookRef, RunbookRegistry, lookup_key_for_task

DEFAULT_CLAIMED_BY = "queue-engine"
DEFAULT_LEASE_SECONDS = 300.0
DEFAULT_UNCERTAIN_GRACE_SECONDS = 60.0
_LOGGER = logging.getLogger(__name__)

SESSION_UNREACHABLE_ERRORS = frozenset({"SESSION_NOT_FOUND", "NODE_UNREACHABLE", "AMBIGUOUS_SESSION",
                                        # resolve_session's "the nodes that
                                        # answered do not have it and some node
                                        # could not be asked" -- strictly more
                                        # recoverable than the SESSION_NOT_FOUND
                                        # it was previously reported as, so it
                                        # belongs in exactly the same bucket.
                                        "SESSION_LOCATION_UNKNOWN"})
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


def build_runbook_notice(runbook: RunbookRef) -> str:
    """The advisory runbook block appended to a dispatch, and the ONLY
    place a retrieved runbook reaches a worker.

    Three properties are deliberate and are asserted by tests:

    1. It is a POINTER, never a procedure. `source` is where the steps
       actually live; the body is never inlined (see runbook_registry.py's
       own secret-handling boundary).
    2. It says out loud that it is advisory and subordinate to the task's
       own prompt. A retrieved runbook that contradicts the actual request
       must not quietly redirect the work -- the task's brief is the
       instruction, this is context.
    3. A runbook marked `destructive` carries an explicit do-not-execute
       line. Retrieval is advisory unless the operator's current policy
       says otherwise, so the wrapper never tells a worker to run
       anything, and is loudest exactly where running something would be
       hardest to undo."""
    lines = [
        "Runbook reference (ADVISORY -- retrieved from the runbook registry, not an instruction):",
        f"  id={runbook.id} version={runbook.version} source={runbook.source or 'unspecified'}",
        f"  {runbook.title}",
    ]
    if runbook.summary:
        lines.append(f"  {runbook.summary}")
    if runbook.destructive:
        lines.append("  DESTRUCTIVE: this runbook contains destructive steps. Do NOT execute them from this "
                     "reference. Confirm with the operator first.")
    lines.append("  Read it instead of rediscovering the procedure. If it conflicts with the task above, "
                 "the task above wins.")
    return "\n".join(lines)


# The one sentence that is unmistakably OUR prompt rather than a worker's
# output. `completion_after_instruction` anchors on it, so it must stay
# byte-identical between the text we send and the text we look for.
_WHITESPACE = re.compile(r"\s+")

COMPLETION_INSTRUCTION_SENTENCE = (
    "When (and only when) the above task is FULLY complete, print exactly one "
    "line in this exact format (once), then stop:")


#: Hard ceilings on injected skill text. A skill is standing guidance, not a
#: payload: past these limits the agent's own prompt starts competing with the
#: briefing for attention, and the briefing wins by being first.
#: Named here rather than reached for ad hoc. `_skills_preamble` already
#: referenced `_LOGGER` in its own except handler while the module defined
#: none, so a skill store that raised turned into a NameError raised OUT of
#: the handler meant to swallow it -- and blocked the dispatch it promises
#: never to block. See tests/test_skill_injection_bounds.py.
_LOGGER = logging.getLogger(__name__)

MAX_SKILL_PREAMBLE_CHARS = 8_000
MAX_SKILLS_INJECTED = 4

#: How many OTHER active lanes one coordinator review will spend a live status
#: read on. See _review: the enrichment is a heuristic input, the dispatch it
#: was starving is not.
MAX_OTHER_LANE_PROBES = 6
#: Per-probe ceiling for that enrichment, when the ops object offers a bounded
#: status read at all.
OTHER_LANE_PROBE_SECONDS = 1.5


def build_dispatch_text(task: QueueTask, *, nonce: str, continuation_text: str | None = None,
                        skills_preamble: str | None = None, runbook: RunbookRef | None = None) -> str:
    """The 'wrapper rất ngắn' item 7 explicitly allows and limits: the
    task's own prompt is included VERBATIM, first, unmodified -- nothing
    here rewrites or reinterprets the business request. Only a short,
    clearly-delimited completion-marker instruction (and, per the
    living-requirements convention, a one-line docs reminder) is
    appended, reusing status.py's own COMPLETION_MARKER_RE protocol
    (task_id+attempt+nonce-bound) so a real, verifiable completion
    signal is possible instead of relying on a bare 'final report'
    heuristic (item 11).

    `continuation_text` (TMCP-RETRY-CONTEXT-002) replaces the prompt -- and
    ONLY the prompt -- when this dispatch is a retry that retry_recovery
    decided can continue existing work. The completion-marker instruction
    still travels with it, still bound to this attempt's own nonce, because a
    continued attempt has to be able to report completion exactly like a
    first one.

    Why this parameter exists at all: replaying the prompt on every attempt is
    the production failure TMCP-RETRY-CONTEXT-002 was raised for. An agent
    handed its original prompt again cannot tell a retry from a new task, so
    it re-plans and an hour of reasoning is discarded. A retry that can
    continue must say "continue", not say everything again.

    `skills_preamble` (TMCP-PROJECT-BOOTSTRAP-001) is the bound-skill briefing
    for the agent that owns this task, placed BEFORE the prompt because
    standing instructions have to be read before the request they qualify.
    It is bounded in both count and size by the caller, and absent entirely
    for a task with no agent -- which is every task that predates the agent
    runtime, so their dispatch text stays byte-identical to before.
    """
    preamble = f"{skills_preamble.strip()}\n\n---\n\n" if skills_preamble else ""
    runbook_block = f"{build_runbook_notice(runbook)}\n\n" if runbook is not None else ""
    return (
        f"{preamble}"
        f"{continuation_text if continuation_text else task.prompt}\n\n"
        f"---\n"
        f"{REQUIREMENTS_REMINDER}\n\n"
        f"{runbook_block}"
        f"{COMPLETION_INSTRUCTION_SENTENCE}\n"
        f"###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 task_id={task.id} "
        f"attempt={task.attempt_count + 1} nonce={nonce} status=completion_candidate "
        f"summary_sha256={hashlib.sha256(task.id.encode()).hexdigest()[:16]}###\n"
    )


def _flatten(text: str) -> str:
    """Whitespace-collapsed text, for matching against a wrapped pane."""
    return _WHITESPACE.sub(" ", text).strip()


_FLAT_INSTRUCTION = None


def _is_our_own_template(output: str, marker_start: int) -> bool:
    """Is the marker at this position the one WE dispatched?

    Our template is always written immediately after the instruction
    sentence. A worker's marker is printed later, after its own work. So the
    text directly preceding a marker decides whose it is.

    Compared on whitespace-collapsed text because a pane wraps at its width:
    the sentence arrives split across lines, and a literal match finds none of
    it. That exact miss let three tasks be marked COMPLETED against untouched
    worktrees while their workers were still running.
    """
    global _FLAT_INSTRUCTION
    if _FLAT_INSTRUCTION is None:
        _FLAT_INSTRUCTION = _flatten(COMPLETION_INSTRUCTION_SENTENCE)
    # A generous look-back: enough to hold the sentence however it wrapped,
    # short enough that unrelated earlier text cannot reach into it.
    window = _flatten(output[max(0, marker_start - 400):marker_start])
    return window.endswith(_FLAT_INSTRUCTION)


def worker_output_after_prompt(output: str) -> str:
    """The part of the pane that is a WORKER's output, not our own prompt.

    Found by dogfooding on 2026-09-13, and it invalidated every completion
    this engine had ever verified: `build_dispatch_text` writes a COMPLETE,
    VALID, nonce-bound marker into the pane as the INSTRUCTION, and
    `verify_completion_marker` checks only task_id/attempt/nonce -- every one
    of which that instruction contains. So the engine read its own prompt
    back and called it evidence. A session running `sleep` forever, printing
    nothing, had its task marked COMPLETED.

    Implementation notes, both learned the hard way:

    Anchor on the instruction SENTENCE, not on the marker string. `rfind` on
    the marker lands on the WORKER's copy when it printed one, discarding
    exactly the evidence we came for.

    Then skip the first marker-SHAPED run after that sentence rather than a
    reconstructed exact string. The first attempt rebuilt the expected marker
    from `attempt_count + 1` -- right at dispatch, wrong at verification,
    where the counter has already been incremented -- so it matched nothing
    and the echo sailed through. The shape is what matters here, not the
    fields; whatever follows our own template is the worker speaking.

    If the prompt is not in the captured tail (it scrolled away), everything
    present is the worker's and is returned unchanged, so a real completion
    is never hidden.
    """
    if not output:
        return ""
    # Walk every marker in the window and keep only what follows one that is
    # NOT our own template. Anchoring on the sentence alone was too brittle in
    # both directions: a wrapped sentence let our template through, and
    # demanding the sentence be visible stranded genuine completions once a
    # long run scrolled it away.
    last_ours_end = None
    for match in COMPLETION_MARKER_RE.finditer(output):
        if _is_our_own_template(output, match.start()):
            last_ours_end = match.end()
        elif last_ours_end is None:
            # A marker that nothing of ours precedes: the worker's own.
            return output
    if last_ours_end is None:
        return output
    return output[last_ours_end:]


@dataclass(frozen=True)
class TickResult:
    session: str
    action: str  # IDLE | PAUSED | CLAIMED | COORDINATOR_* | DISPATCHED | RUNNING | VERIFYING | COMPLETED | NO_OP | ...
    task_id: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"session": self.session, "action": self.action, "task_id": self.task_id, "detail": self.detail}


def _target_state_from_status(status: dict) -> str | None:
    """Map terminal_status's own `state` onto the adapter target-state
    vocabulary delivery_gate speaks. Only RUNNING is a positive
    acceptance signal; WAITING_INPUT means the target is blocked on a
    human, which is explicitly NOT acceptance of our prompt."""
    from .adapters import TARGET_RUNNING, TARGET_WAITING
    state = status.get("state")
    if state == "RUNNING":
        return TARGET_RUNNING
    if state == "WAITING_INPUT":
        return TARGET_WAITING
    return None


class QueueEngine:
    def __init__(self, store: QueueStore, ops: SessionOps, *, coordinator: CoordinatorGate | None = None,
                claimed_by: str = DEFAULT_CLAIMED_BY, lease_seconds: float = DEFAULT_LEASE_SECONDS,
                on_completed: Callable[[QueueTask], None] | None = None,
                verify_queue: Any = None, delivery_policy: Any = None,
                governor: RequestGovernor | None = None,
                runbooks: RunbookRegistry | None = None,
                skill_loader: Callable[[QueueTask], str | None] | None = None) -> None:
        self.store = store
        self.ops = ops
        # TMCP-PROJECT-BOOTSTRAP-001: returns the bound-skill briefing for one
        # task, or None. Injected rather than imported so the engine keeps
        # knowing nothing about agents or skills -- it asks a callable and
        # prepends whatever it gets back.
        self.skill_loader = skill_loader
        # PROMPT DELIVERY / ACCEPTANCE GATE (delivery_gate.py). None ->
        # PromptDeliveryConfig()'s own default, which is advisory: the
        # verdict is computed and recorded, and every transition below
        # behaves exactly as it did before. Nothing here changes an
        # outcome until an operator sets prompt_delivery.mode=enforce.
        if delivery_policy is None:
            from .config import PromptDeliveryConfig
            delivery_policy = PromptDeliveryConfig()
        self.delivery_policy = delivery_policy
        self.runbooks = runbooks
        # P0.5 Verify Queue -- OPTIONAL, and inert unless a task's OWN
        # completion_policy carries a `verify` block. Wiring a
        # VerifyQueue here does NOT change how any existing lane behaves:
        # a task with no verify policy takes exactly the code path it
        # took before, including the in-session marker check below. See
        # verify_queue.py's own module docstring for why the opt-in is
        # per-task rather than a global switch.
        self.verify_queue = verify_queue
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
        self.governor = governor
        self._inactive_active_observations: dict[str, tuple[tuple, float]] = {}

    def reconcile_resolved_preflight_pauses(self) -> list[str]:
        """Probe recoverable preflight pauses; never dispatch or override a user pause."""
        candidates = []
        for lane in self.store.list_all_lanes():
            if not lane["paused"] or lane.get("paused_origin") != "coordinator":
                continue
            for row in lane["tasks"]:
                if row["status"] != "PAUSED" or row["started_at"] or row["attempt_count"]:
                    continue
                reason = row.get("coordinator_reason") or ""
                # Review budgets wrap the original dirty-repo reason. Read
                # bounded audit history to identify that recoverable cause.
                if "exceeded max coordinator review attempts" in reason:
                    events = self.store.list_events(lane["session"], limit=100)
                    reason = next((e.get("reason") or "" for e in events
                                   if e.get("task_id") == row["id"]
                                   and e.get("event_type") == "COORDINATOR_NEEDS_REWORK"), reason)
                if "uncommitted changes present in " in reason or reason.startswith("AGENT_NOT_RUNNING:"):
                    candidates.append(row["id"])
        recovered = []
        if not candidates:
            return recovered
        offset = getattr(self, "_preflight_probe_cursor", 0) % len(candidates)
        selected = (candidates[offset:] + candidates[:offset])[:8]
        self._preflight_probe_cursor = offset + len(selected)
        for task_id in selected:
            try:
                task = self.store.get_task(task_id)
                observed = self._status_bounded(task.session)
                if observed.get("error") or observed.get("state") != "IDLE" or not observed.get("cwd"):
                    continue
                snapshot = SessionSnapshot(**{key: observed.get(key) for key in SessionSnapshot.__dataclass_fields__})
                decision = self.coordinator.review(replace(task, coordinator_attempts=0),
                                                   store=self.store, session=snapshot)
                # Only re-open after the resolved condition passes all local
                # gates. The normal claim/review still checks competing lanes.
                if decision.status == READY and self.store.resume_resolved_preflight(task):
                    recovered.append(task.id)
            except Exception:
                _LOGGER.exception("preflight recovery failed for task=%s", task_id)
        return recovered

    def reconcile_stale_active_tasks(self) -> list[str]:
        """Release stale reservations without dispatching work on any lane.

        Missing sessions use task age. Reachable sessions require a full
        interval of unchanged inactivity, not merely a long task runtime.
        This conservative observation clock resets after a controller restart.
        """
        timeout = self.governor.config.stale_active_timeout_seconds if self.governor else 900.0
        now = datetime.now(timezone.utc)
        observed_at = time.monotonic()
        recovered = []
        tasks = self.store.tasks_with_statuses((RUNNING, VERIFYING))
        active_ids = {task.id for task in tasks}
        self._inactive_active_observations = {
            key: value for key, value in self._inactive_active_observations.items() if key in active_ids
        }
        for task in tasks:
            try:
                if self.store.has_live_verification(task.id):
                    self._inactive_active_observations.pop(task.id, None)
                    continue
                observation = self.ops.terminal_status(task.session)
                error = observation.get("error")
                state = str(observation.get("state") or "").upper()
                if not error and state == "IDLE" and task.status == VERIFYING:
                    # Finish already-dispatched work even after a follower
                    # restart or on a lane not opted into NEW dispatch.
                    result = self._check_completion(task.session, task.id, VERIFYING)
                    if result.action in {COMPLETED, BLOCKED}:
                        recovered.append(task.id)
                        self._inactive_active_observations.pop(task.id, None)
                        continue
                if error in SESSION_UNREACHABLE_ERRORS:
                    self._inactive_active_observations.pop(task.id, None)
                    updated = datetime.fromisoformat(task.updated_at.replace("Z", "+00:00"))
                    if updated.tzinfo is None or (now - updated).total_seconds() < timeout:
                        continue
                    target = WAITING_SESSION
                    reason = f"STALE_ACTIVE_TIMEOUT: {error}; released admission after {timeout:g}s"
                elif not error and state in {"IDLE", "WAITING_INPUT", "WAITING_APPROVAL", "PAGER"}:
                    signature = (task.status, task.updated_at, task.attempt_count, task.claim_token,
                                 state, hashlib.sha256(str(observation.get("last_output") or "").encode()).hexdigest())
                    previous = self._inactive_active_observations.get(task.id)
                    if previous is None or previous[0] != signature:
                        self._inactive_active_observations[task.id] = (signature, observed_at)
                        continue
                    if observed_at - previous[1] < timeout:
                        continue
                    # A quiet terminal may contain a real completion. Leave it
                    # to the existing evidence/requirements verification path.
                    capture = self.ops.terminal_tail(task.session, 200)
                    if capture.get("error"):
                        self._inactive_active_observations.pop(task.id, None)
                        continue
                    marker = parse_completion_marker(worker_output_after_prompt(str(capture.get("output") or "")))
                    if verify_completion_marker(marker, task_id=task.id, attempt=task.attempt_count,
                                                nonce=task.verification_nonce, nonce_consumed=False):
                        self._inactive_active_observations.pop(task.id, None)
                        # A real completion is here. Deferring to the evidence
                        # path is right -- but this branch used to defer to it
                        # without ever INVOKING it, so a VERIFYING task whose
                        # marker the gate refuses was skipped by the very sweep
                        # that exists to bound it, on this pass and every pass
                        # after. The marker never expires, so the skip never
                        # ended. Run that path here instead of naming it.
                        #
                        # The IDLE case is already handled above; this covers
                        # the other quiet states (WAITING_INPUT/WAITING_
                        # APPROVAL/PAGER), where nothing else was reached.
                        if task.status == VERIFYING and state != "IDLE":
                            settled = self._check_completion(task.session, task.id, VERIFYING)
                            if settled.action in {COMPLETED, BLOCKED}:
                                recovered.append(task.id)
                        continue
                    target = BLOCKED
                    reason = f"STALE_ACTIVE_TIMEOUT: session is {state}; no progress or completion for {timeout:g}s"
                else:
                    # Live work and uncertain observations break the inactivity
                    # interval; neither proves a task stopped making progress.
                    self._inactive_active_observations.pop(task.id, None)
                    continue
                changed = self.store.recover_stale_active_task(task, to_status=target, reason=reason)
                self._inactive_active_observations.pop(task.id, None)
                if changed is not None:
                    recovered.append(task.id)
                    if self.store.long_task_watch(task.id):
                        self.store.update_long_task_watch(task.id, state="BLOCKED", blocker=reason, reason=reason)
                    _LOGGER.warning("stale-active recovery task=%s session=%s %s -> %s: %s",
                                    task.id, task.session, task.status, target, reason)
            except Exception:
                self._inactive_active_observations.pop(task.id, None)
                _LOGGER.exception("stale-active recovery probe failed for task=%s", task.id)
        return recovered

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
            if self.governor is not None:
                claimed, queued_reason = self.governor.try_admit_and_claim(
                    session, claimed_by=self.claimed_by, lease_seconds=self.lease_seconds)
                if claimed is None and queued_reason:
                    return TickResult(session, "QUEUED", detail=queued_reason)
            else:
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

    def _status_bounded(self, session: str) -> dict[str, Any]:
        """A status read for best-effort enrichment, with a ceiling.

        Falls back to the plain call for an ops object that has no bounded
        form (every test double, and the local-only TerminalService where the
        read is a tmux call rather than a network round trip)."""
        bounded = getattr(self.ops, "terminal_status_bounded", None)
        if bounded is None:
            return self.ops.terminal_status(session)
        return bounded(session, OTHER_LANE_PROBE_SECONDS)

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
            recovery_state=status_response.get("recovery_state"),
        )
        other_tasks = {
            lane["session"]: lane["current_task"]
            for lane in self.store.list_all_lanes()
            if lane["session"] != session and lane["current_task"] is not None
        }
        other_active = tuple(
            OtherLaneSnapshot(session=name,
                             node_id=other_task.get("node_id"),
                             cwd=None)  # populated below only for lanes actually queried
            for name, other_task in other_tasks.items()
        )
        # Cheaply enrich other_active with a real observed cwd ONLY for
        # lanes that actually have something in flight right now.
        #
        # HARD CAP, because "however many OTHER lanes exist" is not a bound.
        # Found live on hp-linux (2026-09-20): a 78-session fleet had enough
        # concurrently-active lanes that this loop alone -- one routed,
        # cross-node terminal_status per lane, in series -- could outlast the
        # router's whole synchronous dispatch budget, so a PRECHECK review
        # performed during route_start never finished and the task came back
        # not-started. The enrichment only feeds a cwd-overlap heuristic, so
        # missing it degrades a scope check; missing the dispatch does not
        # degrade anything, it loses the dispatch.
        enriched = []
        for index, other in enumerate(other_active):
            if index >= MAX_OTHER_LANE_PROBES:
                enriched.append(other)
                continue
            try:
                other_status = self._status_bounded(other.session)
                other_task = other_tasks[other.session]
                # A preflight refusal occupies its own lane, but has never
                # acquired the repo. Require live IDLE evidence as well:
                # paused work that actually started must retain ownership.
                if (other_task["status"] == "PAUSED"
                        and other_task.get("paused_from_status") == QUEUED
                        and not other_task.get("started_at")
                        and not other_task.get("attempt_count")
                        and not other_task.get("dispatch_idempotency_key")
                        and other_status.get("state") == "IDLE"
                        and not other_status.get("error")):
                    continue
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

    def _snapshot_recovery_state(self, task: QueueTask, session: str, *, why: str) -> None:
        """Persist recovery identity + a progress capsule for `task`.

        Called BEFORE anything that destroys context -- a dispatch (so identity
        is on disk before the agent can die), a BLOCKED/uncertain transition, or
        a requeue. That ordering is the entire point: after a lease expires or an
        agent process dies, the pane and its scrollback are gone, and anything
        not already written down cannot be recovered.

        Best-effort by construction. A failure here must never block or fail the
        dispatch it is protecting -- a missing capsule degrades a later retry to
        a weaker recovery mode, which is strictly better than refusing to run
        the task at all.
        """
        try:
            status = self.ops.terminal_status(session) or {}
        except Exception:  # noqa: BLE001
            status = {}
        # The conversation id and agent_type come from the SESSION REGISTRY, not
        # from terminal_status. Learned from the live smoke: terminal_status's
        # `resume_conversation_id` is populated on the Windows backend only --
        # tmux leaves it None by design (see models.py) -- so sourcing it from
        # there persisted None for a real Claude session whose id was known all
        # along, and the whole native-resume path silently degraded to
        # checkpoint/restart. The registry is where terminal_create_session
        # durably writes it, which is exactly what it is for.
        registry_row: dict[str, Any] = {}
        registry_get = getattr(self.ops, "terminal_registry_get", None)
        if registry_get is not None:
            try:
                answer = registry_get(session) or {}
                record = answer.get("record") if isinstance(answer.get("record"), dict) else answer
                registry_row = record if isinstance(record, dict) else {}
            except Exception:  # noqa: BLE001
                registry_row = {}
        identity: dict[str, Any] = {
            "session": session,
            "node_id": status.get("node_id") or registry_row.get("node_id"),
            "cwd": status.get("cwd") or registry_row.get("cwd"),
            "worktree": status.get("cwd") or registry_row.get("worktree_path") or registry_row.get("cwd"),
            "agent_type": (status.get("agent_type") or registry_row.get("agent_type")
                           or registry_row.get("launcher_type") or (task.metadata or {}).get("agent_type")),
            "conversation_id": (registry_row.get("conversation_id")
                                or status.get("resume_conversation_id") or status.get("conversation_id")),
            "branch": registry_row.get("git_branch"),
            "last_state": status.get("state"),
            "request_key": task.request_key,
            "attempt": task.attempt_count,
            "reason": why,
        }
        # The capsule records only what can be observed here without asking the
        # agent anything: the tail it last produced, and the git/branch facts
        # the coordinator already reads. Richer fields (completed_steps,
        # next_step) are written by whoever actually knows them -- an agent
        # reporting progress, or an operator -- through record_recovery_state.
        capsule: dict[str, Any] = {}
        try:
            tail = (self.ops.terminal_tail(session, 40) or {}).get("output") or ""
        except Exception:  # noqa: BLE001
            tail = ""
        if tail.strip():
            # Bounded on purpose: this is a recovery hint, not a transcript
            # store, and an unbounded blob in a task row would grow forever.
            capsule["last_decision"] = tail.strip()[-1200:]
        if status.get("cwd"):
            capsule["worktree"] = status["cwd"]
        try:
            self.store.record_recovery_state(task.id, identity=identity, capsule=capsule or None)
        except Exception:  # noqa: BLE001 -- never let bookkeeping break dispatch
            pass

    def _relaunch_for_native_resume(self, session: str, plan: retry_recovery.RetryPlan) -> bool:
        """Carry out RESUME_NATIVE_CONVERSATION's relaunch half.

        Uses the agent's OWN resume mechanism through the existing
        registry_reopen path (which already knows how to pass `--resume
        <conversation_id>` for a resume-capable agent_type -- see core.py's
        terminal_create_session), rather than a second, parallel relaunch
        implementation. Returns True only if a reopen was actually performed,
        so the caller can tell "resumed" from "could not resume".

        A controller that does not expose registry_reopen (an older node, a
        reduced ops object in a test) simply reports False: the continuation is
        still sent into whatever is there, which is the pre-existing behaviour,
        never a crash and never a silent fresh conversation presented as a
        resume.
        """
        reopen = getattr(self.ops, "terminal_registry_reopen", None) or getattr(
            self.ops, "registry_reopen", None)
        if reopen is None or not plan.resume_conversation_id:
            return False
        try:
            result = reopen(session, resume_session_id=plan.resume_conversation_id)
        except TypeError:
            # Older signature without the keyword -- try positionally rather
            # than giving up on the resume entirely.
            try:
                result = reopen(session)
            except Exception:  # noqa: BLE001
                return False
        except Exception:  # noqa: BLE001
            return False
        return isinstance(result, dict) and not result.get("error")

    def _retry_plan_for(self, task: QueueTask, session: str) -> retry_recovery.RetryPlan | None:
        """The retry plan for THIS dispatch, or None if this is a first attempt.

        Returns None for `attempt_count == 0` so a brand-new task takes exactly
        the path it always has -- the prompt, verbatim, unchanged. Only a real
        retry consults retry_recovery, and only a mode that does not replay the
        prompt produces a continuation.

        Facts come from the session's own live status (the same terminal_status
        every other gate here reads) plus whatever recovery capsule the task's
        metadata durably carries. Never raises: a retry whose facts cannot be
        gathered falls back to None, i.e. today's replay behaviour, because a
        broken lookup must not be able to strand a task.
        """
        if task.attempt_count < 1:
            return None
        try:
            status = self.ops.terminal_status(session) or {}
        except Exception:  # noqa: BLE001 -- a fact-gathering failure must never strand a retry
            status = {}
        # `exists` is the session; a non-error status whose state is not UNKNOWN
        # means the pane is answering, which is the agent process still being
        # there. A session that exists while its agent died reads UNKNOWN, and
        # that is precisely the native-resume case.
        session_alive = bool(status.get("exists")) and not status.get("error")
        state = str(status.get("state") or "UNKNOWN").upper()
        agent_alive = session_alive and state in ("RUNNING", "IDLE", "WAITING_INPUT")
        # Read the DURABLE record, not anything held in memory: a retry after a
        # service restart has no in-process state, and this is the path that has
        # to work then. get_recovery_state re-reads from disk every call.
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        try:
            recovery = self.store.get_recovery_state(task.id)
        except Exception:  # noqa: BLE001 -- fall back to the row we already hold
            recovery = metadata.get("recovery") if isinstance(metadata.get("recovery"), dict) else {}
        ctx = retry_recovery.RetryContext(
            task_id=task.id, session=session, request_key=task.request_key,
            attempt=task.attempt_count + 1,
            session_alive=session_alive, agent_process_alive=agent_alive,
            agent_type=recovery.get("agent_type") or metadata.get("agent_type"),
            conversation_id=recovery.get("conversation_id"),
            capsule=retry_recovery.RecoveryCapsule.from_mapping(recovery.get("capsule")),
            branch=recovery.get("branch"), worktree=recovery.get("worktree"),
        )
        plan = retry_recovery.plan_retry(ctx)
        # RECOVERY_RESTART is the one mode that deliberately replays the
        # prompt, so it reports no continuation and the caller falls back to
        # exactly the pre-existing behaviour.
        return None if plan.replays_prompt else plan

    def _retrieve_runbook(self, task: QueueTask) -> RunbookRef | None:
        """Advisory retrieval, on the dispatch path, that cannot fail the
        dispatch.

        The evidence trail is a `queue_events` row (RUNBOOK_HIT/MISS/STALE/
        UNAVAILABLE) carrying the full lookup result -- status, matched
        axis, chosen runbook id+version, the other candidates considered,
        and the registry's own source+revision. That reuses the audit trail
        every other queue transition already writes to, so hit/miss/source/
        version tracking needs no new table, no migration, and no second
        place to look when reconstructing what a worker was told.

        A task carrying no lookup keys at all produces NO event: nothing
        was asked, so recording a miss for every legacy task would bury the
        real ones in noise (see lookup_key_for_task on why keys are read
        explicitly and never guessed from error text)."""
        if self.runbooks is None:
            return None
        try:
            key = lookup_key_for_task(task)
            if key.empty:
                return None
            result = self.runbooks.lookup(key)
            self.store.record_event(session=task.session, task_id=task.id,
                                    event_type=f"RUNBOOK_{result.status}",
                                    reason=result.reason or None, metadata=result.to_dict())
            return result.runbook if result.hit else None
        except Exception:  # noqa: BLE001 -- advisory context is never worth failing a real dispatch over
            return None

    def _dispatch(self, session: str, task_id: str) -> TickResult:
        task = self.store.get_task(task_id)
        if task.metadata.get("execution_mode") != "shell":
            observed = self._status_bounded(session)
            if str(observed.get("current_command") or "").casefold() in PLAIN_SHELL_COMMANDS:
                return self._review(session, task_id)
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
        retry_plan = self._retry_plan_for(task, session)
        # Identity on disk BEFORE the send, so an agent that dies mid-turn is
        # still recoverable -- after it dies there is nothing left to read.
        self._snapshot_recovery_state(task, session, why="pre-dispatch")
        resumed_natively = False
        if retry_plan is not None and retry_plan.relaunch_agent:
            resumed_natively = self._relaunch_for_native_resume(session, retry_plan)
        dispatch_text = build_dispatch_text(
            task, nonce=nonce,
            continuation_text=(retry_plan.continuation_text if retry_plan is not None else None),
            skills_preamble=self._skills_preamble(task), runbook=self._retrieve_runbook(task))
        self.store.transition_task(task_id, DISPATCHING, event_type="DISPATCHED",
                                   extra_fields={"dispatch_idempotency_key": idempotency_key})

        response = self.ops.terminal_send_text(session, dispatch_text, press_enter=True,
                                               idempotency_key=idempotency_key)
        delivery_state = response.get("delivery_state")
        # PROMPT DELIVERY / ACCEPTANCE GATE. The verdict is ALWAYS computed
        # and recorded; whether it can change a transition is gated on
        # prompt_delivery.mode (advisory by default -- see below).
        verdict = self._delivery_verdict(session, response, dispatch_text)
        self.store.record_delivery_verdict(task_id, verdict.to_dict())
        if response.get("error"):
            # About to lose this attempt: write what we can still see first.
            self._snapshot_recovery_state(task, session, why="blocked: send failed")
            self.store.transition_task(task_id, BLOCKED, event_type="BLOCKED",
                                       reason=f"send failed: {response['error']}")
            return TickResult(session, "BLOCKED", task_id=task_id, detail=str(response["error"]))
        # Gate 1, POSITIVE allowlist. The original code asked "is this the
        # one literal string DELIVERY_UNKNOWN?" and fell through to RUNNING
        # otherwise -- so any other non-confirmed state (TEXT_SENT from an
        # idempotent replay, a state added to adapters.py later) meant
        # RUNNING. Now anything that is not provably confirmed is held.
        # This half is pure hardening and is enforced in BOTH modes: it can
        # only ever refuse a dispatch the old code would have wrongly
        # advanced, and for every state that actually occurs today it
        # produces the identical outcome.
        if verdict.kind == delivery_gate.REFUSED:
            self.store.transition_task(
                task_id, BLOCKED, event_type="BLOCKED",
                reason=f"delivery refused: {verdict.activation} ({verdict.detail or delivery_state})")
            return TickResult(session, "BLOCKED", task_id=task_id, detail=verdict.activation)
        # Gate 2, acceptance. ONLY enforced in enforce mode: holding a task
        # that the old code would have marked RUNNING is a real behaviour
        # change, so it needs the operator's explicit opt-in.
        if verdict.kind == delivery_gate.NOT_ACCEPTED and self.delivery_policy.enforcing:
            self.store.mark_dispatch_uncertain(
                task_id, reason=f"submit confirmed but acceptance not observed: {verdict.acceptance}")
            return TickResult(session, "DISPATCH_UNCERTAIN", task_id=task_id,
                              detail=verdict.acceptance)
        if verdict.kind == delivery_gate.UNCERTAIN or delivery_state == "DELIVERY_UNKNOWN":
            # P0 item 2: never resend blindly, and never silently look
            # like an ordinary QUEUED task either -- DISPATCH_UNCERTAIN,
            # KEEPING the same dispatch_idempotency_key (the outcome is
            # genuinely unknown -- if the send actually went through,
            # core.py's own idempotent_sends store will return that
            # original result the next time this exact key is reused,
            # rather than sending twice). _recheck_uncertain resolves
            # this on a later tick: real evidence of activity -> RUNNING,
            # else a grace period -> QUEUED for a fresh attempt.
            self._snapshot_recovery_state(task, session, why="dispatch uncertain")
            self.store.mark_dispatch_uncertain(task_id, reason="delivery_state=DELIVERY_UNKNOWN")
            return TickResult(session, "DISPATCH_UNCERTAIN", task_id=task_id)
        self.store.transition_task(task_id, RUNNING, event_type="STARTED")
        self._ensure_long_task_watch(task, session, response, idempotency_key)
        detail = idempotency_key
        if retry_plan is not None:
            detail = (f"{idempotency_key} retry_mode={retry_plan.mode}"
                      + (" native_resume=ok" if resumed_natively else ""))
        return TickResult(session, "DISPATCHED", task_id=task_id, detail=detail)

    def _skills_preamble(self, task: QueueTask) -> str | None:
        """The agent's standing briefing for this task, or None.

        Never raises and never blocks a dispatch: a skill store that is slow,
        broken or simply absent must not stop work being sent. A task with no
        agent has no briefing, which is the overwhelmingly common case and the
        reason existing dispatch text is unchanged."""
        if self.skill_loader is None or not getattr(task, "skill_ids", ()):
            return None
        try:
            preamble = self.skill_loader(task)
        except Exception:  # noqa: BLE001 -- a briefing is an enhancement, never a gate
            _LOGGER.exception("queue-engine: skill preamble failed for task %s", task.id)
            return None
        if not preamble:
            return None
        text = str(preamble)
        if len(text) > MAX_SKILL_PREAMBLE_CHARS:
            # Truncated at a line boundary with an explicit marker: a briefing
            # that stops mid-sentence reads as corruption, and silently
            # dropping the tail would hide that a skill is too large.
            text = text[:MAX_SKILL_PREAMBLE_CHARS].rsplit("\n", 1)[0]
            text += "\n\n[skill briefing truncated at the configured size limit]"
        return text

    @staticmethod
    def _long_task_metadata(task: QueueTask) -> tuple[bool, int | None]:
        metadata = task.metadata or {}
        expected = metadata.get("expected_minutes", metadata.get("packet_duration_minutes"))
        try:
            expected = int(expected) if expected is not None else None
        except (TypeError, ValueError):
            expected = None
        return bool(metadata.get("long_task") is True or (expected is not None and expected >= 10)), expected

    def _ensure_long_task_watch(self, task: QueueTask, session: str, response: dict[str, Any], request_key: str) -> None:
        long_task, expected = self._long_task_metadata(task)
        if not long_task:
            return
        submission_id = response.get("submission_id") or response.get("correlation_id")
        if not submission_id:
            return
        watch = self.store.ensure_long_task_watch(
            task.id, str(submission_id), request_key, session,
            node_id=response.get("node_id"), expected_minutes=expected,
        )
        if response.get("execution_started") or response.get("ack_state") in {"RUNNING", "EXECUTION_STARTED"}:
            now = iso_now()
            self.store.update_long_task_watch(
                task.id, state="WATCHING", execution_started_at=now, last_progress_at=now,
                watch_lease_expires_at=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
            )

    def _delivery_verdict(self, session: str, response: dict, sent_text: str):
        """Compute the delivery verdict for one send.

        The post-submit observation is ONE extra read (status + tail), not a
        poll loop: long-running observation already belongs to the
        completion watcher, and the rule is explicitly that polling for
        completion stops once delivery is established. A read that fails is
        UNOBSERVABLE, which is never a pass."""
        after_lines = None
        target_state = None
        if self.delivery_policy.require_acceptance:
            try:
                status = self.ops.terminal_status(session)
                if not status.get("error"):
                    target_state = status.get("target_state") or _target_state_from_status(status)
                    tail = status.get("last_output")
                    if isinstance(tail, str):
                        after_lines = tail.splitlines()
            except Exception:  # noqa: BLE001 -- an unobservable target must not crash dispatch
                after_lines = None
        return delivery_gate.evaluate(
            response, before_lines=response.get("pre_submit_lines"), after_lines=after_lines,
            target_state=target_state, sent_text=sent_text,
            require_acceptance=self.delivery_policy.require_acceptance)

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
        task = self.store.get_task(task_id)
        status_response = self.ops.terminal_status(session)
        # DELIVERY_UNKNOWN means only that acceptance was not observable in
        # the submit window. The worker may already have completed before a
        # later tick. Reconcile the same nonce-bound evidence used by the
        # ordinary VERIFYING path before considering any grace-period retry;
        # this completes without resending and cannot accept the marker that
        # was embedded in our own dispatched instruction.
        capture = self.ops.terminal_tail(session, 200)
        output = worker_output_after_prompt(str(capture.get("output") or ""))
        marker = parse_completion_marker(output)
        if verify_completion_marker(
            marker, task_id=task_id, attempt=task.attempt_count,
            nonce=task.verification_nonce, nonce_consumed=False,
        ):
            # Preserve the existing state machine/audit path: uncertain
            # delivery is first confirmed as started, then verified complete.
            self.store.transition_task(
                task_id, RUNNING, event_type="STARTED",
                reason="verified completion proves uncertain dispatch was accepted")
            self.store.transition_task(
                task_id, VERIFYING, event_type="VERIFYING",
                reason="nonce-bound completion evidence observed during reconciliation")
            try:
                completed = self.store.mark_completed_with_evidence(
                    task_id, evidence={"completion_marker": marker, "reconciled_from": DISPATCH_UNCERTAIN})
            except Exception as exc:
                # Same settlement as the ordinary VERIFYING path: this route
                # reaches the identical contract gate, so an escape here would
                # leave the task in VERIFYING holding its lane while every
                # later tick re-raised the same refusal.
                #
                # Re-read first: `task` was loaded before the two transitions
                # THIS method just made, so it still says DISPATCH_UNCERTAIN.
                # Settling needs the row as it now is -- the optimistic
                # comparison inside recover_stale_active_task is there to
                # catch a CONCURRENT writer, not our own edits.
                return self._settle_refused_completion(self.store.get_task(task_id), exc)
            self._notify_completed(completed)
            if self.governor is not None:
                self.governor.note_success(completed)
            return TickResult(session, "COMPLETED", task_id=task_id,
                              detail="verified completion after uncertain dispatch")
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
            # A node-agent reload does not stop the CLI. Requeueing here
            # incremented the attempt while idempotency retained the OLD
            # prompt, so its eventual marker could never verify. Preserve
            # execution identity; the fleet stale-active sweep handles a
            # genuinely prolonged outage using its bounded timeout.
            return TickResult(session, "OBSERVATION_UNAVAILABLE", task_id=task_id, detail=error)
        if error:
            self.store.transition_task(task_id, FAILED, event_type="FAILED",
                                       reason=f"could not read session status: {error}")
            return TickResult(session, "FAILED", task_id=task_id, detail=str(error))

        if self.governor is not None:
            # This is local pane state, not a provider poll.  It lets the
            # admission layer stop a fresh burst while the CLI owns its one
            # internal retry policy.
            self.governor.note_output(task, str(status_response.get("last_output") or ""))

        state = status_response.get("state")
        watch = self.store.long_task_watch(task_id)
        if watch:
            output = str(status_response.get("last_output") or "")
            if state == "RUNNING" and watch.get("state") == "EXECUTION_START_PENDING":
                now = iso_now()
                self.store.update_long_task_watch(task_id, state="WATCHING", execution_started_at=now,
                                                  last_progress_at=now, watch_lease_expires_at=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
                                                  status_hash=hashlib.sha256(str(status_response).encode()).hexdigest())
            elif state == "RUNNING":
                self.store.update_long_task_watch(task_id, state="WATCHING", last_progress_at=iso_now(),
                                                  watch_lease_expires_at=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
                                                  status_hash=hashlib.sha256(str(status_response).encode()).hexdigest())
            if state == "RUNNING" and output and not watch.get("first_checkpoint_at"):
                self.store.update_long_task_watch(task_id, first_checkpoint_at=iso_now(), last_progress_at=iso_now(),
                                                  output_hash=hashlib.sha256(output.encode()).hexdigest(),
                                                  reason="execution_output_checkpoint")
        if current_status == RUNNING:
            if state == "RUNNING":
                return TickResult(session, "RUNNING", task_id=task_id)
            if watch and watch.get("state") == "WATCHING" and not watch.get("first_checkpoint_at"):
                # A long task that fell back to an idle prompt before its
                # first checkpoint gets one bounded continuation.  This is
                # a new send with a stable key, never a duplicate prompt.
                if (watch.get("resume_count", 0) < 1
                        and not status_response.get("input_required")
                        and str(status_response.get("state") or "").upper() not in {"WAITING_APPROVAL", "WAITING_INPUT", "PAGER"}):
                    resume_key = f"{watch['request_key']}:resume:1"
                    self.ops.terminal_send_text(
                        session,
                        "Continue the same task from the persisted task context; do not restart or duplicate work.",
                        press_enter=True, idempotency_key=resume_key,
                    )
                    self.store.update_long_task_watch(task_id, resume_count=1,
                                                      state="EXECUTION_START_PENDING", reason="auto_resume_before_checkpoint")
                    return TickResult(session, "LONG_TASK_RESUMED", task_id=task_id)
            # Anything else (IDLE/WAITING_INPUT/UNKNOWN) -- the agent has
            # gone quiet; move to VERIFYING to look for real evidence.
            #
            # P0.5: if THIS task asked for external verification, the job
            # is created here and it performs the RUNNING -> VERIFYING
            # transition itself, in the same transaction, so a job can
            # never exist for a task that never entered VERIFYING.
            requested = self._request_verification(task)
            if requested is None:
                self.store.transition_task(task_id, VERIFYING, event_type="VERIFYING")
                return TickResult(session, "VERIFYING", task_id=task_id, detail=f"state={state}")
            return TickResult(session, "VERIFY_REQUESTED", task_id=task_id,
                              detail=f"state={state}, verify_job={requested.id}")

        # current_status == VERIFYING: look for a verified completion
        # marker -- item 11's "không phụ thuộc heuristic final report
        # đơn thuần... fallback cần explicit coordinator verification".
        # No marker found is NOT treated as failure and is NOT treated
        # as silent success either -- the task simply stays in
        # VERIFYING, awaiting either a later marker or an explicit
        # terminal_queue_verify call (queue_service.py) -- there is no
        # code path here that reaches COMPLETED without real evidence.
        # P0.5: a task holding an OPEN verify job with the `hold` fallback
        # must not be completed from its own session's output. Demanding
        # an independent verifier and then accepting the implementer's own
        # marker when none is available would defeat the entire point --
        # so the task waits here, visibly, rather than being passed.
        held = self._external_verification_hold(task_id)
        if held is not None:
            return TickResult(session, "AWAITING_EXTERNAL_VERIFICATION", task_id=task_id, detail=held)

        capture = self.ops.terminal_tail(session, 200)
        output = capture.get("output", "")
        # Only look at what the WORKER wrote. Our own dispatched prompt
        # contains a fully valid marker, and reading that back as evidence is
        # how a session that did nothing got marked COMPLETED.
        output = worker_output_after_prompt(output)
        marker = parse_completion_marker(output)
        verified = verify_completion_marker(
            marker, task_id=task_id, attempt=task.attempt_count, nonce=task.verification_nonce,
            nonce_consumed=False,
        )
        if verified:
            try:
                completed = self.store.mark_completed_with_evidence(task_id, evidence={"completion_marker": marker})
            except Exception as exc:
                return self._settle_refused_completion(task, exc, watch=watch)
            if watch:
                self.store.update_long_task_watch(task_id, state="DONE", first_checkpoint_at=watch.get("first_checkpoint_at") or iso_now(),
                                                  last_progress_at=iso_now(), reason="verified_completion")
            self._notify_completed(completed)
            if self.governor is not None:
                self.governor.note_success(completed)
            return TickResult(session, "COMPLETED", task_id=task_id)
        if state == "RUNNING":
            # A false alarm -- the agent picked back up (e.g. a slow
            # first response mistaken for quiet). Re-arm.
            self.store.transition_task(task_id, RUNNING, event_type="RUNNING")
            return TickResult(session, "RUNNING", task_id=task_id, detail="false alarm, re-armed")
        return TickResult(session, "AWAITING_VERIFICATION", task_id=task_id,
                          detail="no verified completion marker yet")

    def _settle_refused_completion(self, task: QueueTask, exc: Exception, *,
                                   watch: Any = None) -> TickResult:
        """A completion the store refused must SETTLE, never be retried.

        Both routes to COMPLETED go through the same contract gate, and that
        gate is deterministic: the identical evidence re-offered on the next
        tick gets the identical answer. Letting the exception escape therefore
        does not retry anything useful -- it leaves the task in VERIFYING,
        which holds the session's lane, and every QUEUED sibling behind it
        waits on a verdict that will never change. That is precisely how task
        4b5ecb09... accumulated 549 identical refusals in 28 minutes while its
        session's backlog sat undispatched.

        So the refusal is recorded where a human will see it (BLOCKED, with
        the gate's own reason) and the lane is released. The gate is NOT
        softened here: work is never replayed, and an unexpected verifier
        error is never allowed to read as success.
        """
        if isinstance(exc, RequirementsNotCoveredError):
            reason = f"VERIFICATION_REQUIREMENTS: {exc}"
        else:
            _LOGGER.exception("completion verification failed for %s", task.id)
            reason = f"VERIFICATION_ERROR: {type(exc).__name__}"
        changed = self.store.recover_stale_active_task(task, to_status=BLOCKED, reason=reason)
        if changed is None:
            current = self.store.get_task(task.id)
            return TickResult(task.session, current.status, task_id=task.id,
                              detail="concurrent verification update")
        if watch:
            self.store.update_long_task_watch(task.id, state="BLOCKED", blocker=reason,
                                              reason=reason)
        return TickResult(task.session, BLOCKED, task_id=task.id, detail=reason)

    def _request_verification(self, task: QueueTask) -> Any:
        """Create this task's verify job if -- and only if -- its own
        completion_policy asked for one and a VerifyQueue is wired.
        Returns the job, or None to mean "carry on exactly as before".

        Idempotent by construction: ensure_verify_job keys on (task_id,
        attempt), so a re-entered tick or a restart mid-transition
        produces the same job rather than a second one. A failure to
        create the job is deliberately NOT fatal -- the task falls back to
        the ordinary in-session path rather than being stranded in
        RUNNING, the same posture _notify_completed takes."""
        if self.verify_queue is None:
            return None
        from .verify_queue import VerifyQueue
        policy = VerifyQueue.verify_policy_for(task)
        if policy is None:
            return None
        try:
            return self.verify_queue.ensure_verify_job(
                task,
                required_capabilities=policy.get("required_capabilities", ()),
                require_independent=bool(policy.get("require_independent", True)),
                fallback=policy.get("fallback", "in_session"),
                backlog_id=policy.get("backlog_id"),
                branch=policy.get("branch") or task.metadata.get("branch"),
                commit_sha=policy.get("commit_sha") or task.metadata.get("commit_sha"),
            )
        except Exception:  # noqa: BLE001 -- never strand a task on a verify-queue glitch
            return None

    def _external_verification_hold(self, task_id: str) -> str | None:
        """A short reason string when in-session completion must be
        suppressed for this task, else None."""
        if self.verify_queue is None:
            return None
        try:
            job = self.verify_queue.open_job_for_task(task_id)
        except Exception:  # noqa: BLE001
            return None
        if job is None or job.fallback != "hold":
            return None
        return (f"verify job {job.id} is {job.status} and requires an independent verifier "
                f"({', '.join(job.required_capabilities) or 'no capability constraint'})")

    def _notify_completed(self, task: QueueTask) -> None:
        if self.on_completed is None:
            return
        try:
            self.on_completed(task)
        except Exception:  # noqa: BLE001 -- a handoff-publishing glitch must never un-complete a real task
            pass
