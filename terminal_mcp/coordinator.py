"""Coordinator Agent -- the gate review every task goes through before
queue_engine.py ever dispatches it (task: "Supervisor Queue v2 Phase 2
-- Coordinator Agent").

Role split (explicit, per the task spec): the Scheduler/Queue Manager
(queue_store.py's claim_next_task + queue_engine.py's tick loop) is
deterministic bookkeeping -- it never decides whether a task is SAFE to
run, only WHEN a task is next in line. The Coordinator Agent (this
module) is the one that reasons about safety/readiness; it never sends
anything, never codes anything, never runs the worker's own task --
"Coordinator không code thay worker" (item 3). The Worker session
(claude/codex/... running inside the actual tmux/ConPTY session) is
what does the real work, entirely unaware a coordinator exists.

WHY THIS PHASE'S GATE IS DETERMINISTIC, NOT AN LLM CALL (disclosed
design decision): almost everything the task spec asks the Coordinator
to check (previous-task evidence, git status/branch/HEAD, uncommitted
changes, test status, session cwd/identity, cross-lane conflicts,
destructive/sensitive prompt patterns, a review-attempt budget) is
objectively, mechanically checkable -- it does not need subjective
judgment to get right, and getting it WRONG here is exactly the kind of
safety-critical mistake this whole feature exists to prevent (see the
real window/window2 transcript-collision incident this same session
fixed). Wiring an actual LLM call into an autonomous dispatch gate is a
separate, bigger architecture/cost/reliability/prompt-injection-surface
decision that deserves its own explicit sign-off, not something to bake
in silently as part of this phase. CoordinatorGate is built so that ONE
piece -- the "task quá rộng/không rõ" scope/clarity judgment, the one
check in the spec's own list that most genuinely needs reasoning rather
than a rule -- is a pluggable `scope_reasoner` callable; the built-in
default is a conservative, disclosed heuristic (see _default_scope_
reasoner), not a claim of true judgment. Swapping in a real LLM-backed
reasoner later is a drop-in change, not a redesign.

FAIL-CLOSED (item 7, "nếu không đọc được status/git/test/evidence thì
không tự dispatch"): every evidence-collection step here is wrapped so
that an exception, a missing/error session status, or a collector that
can't run git AT ALL results in NEEDS_HUMAN -- never READY. There is no
code path in `review()` that reaches READY without every check having
affirmatively passed.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

from .queue_store import COMPLETED, QueueStore, QueueTask

READY = "READY"
BLOCKED = "BLOCKED"
NEEDS_REWORK = "NEEDS_REWORK"
NEEDS_HUMAN = "NEEDS_HUMAN"
ALL_DECISIONS = (READY, BLOCKED, NEEDS_REWORK, NEEDS_HUMAN)

# Reused as-is from supervisor2.py's own ATTENTION_STOP_PATTERNS (task
# instruction: "Reuse Supervisor v2 Phase 1 claim/decision/... thay vì
# viết lại") -- the same content-based safety screen (credentials,
# destructive shell commands, confirmation prompts) already proven in
# production there, applied here to a task's own PROMPT before it is
# ever dispatched, rather than to a pane's output after the fact.
SENSITIVE_PROMPT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"enter (your |the )?password",
        r"api[_ -]?key",
        r"credential",
        r"\bsecret\b",
        r"\btoken\b",
        r"force[ -]push",
        r"\brm -rf\b",
        r"drop (table|database)",
        r"\bsudo\b",
        r"reset --hard",
        r"\bgit clean\b",
        r"merge (to |into )?main\b",
        r"push (to |origin )?main\b",
    )
)

DEFAULT_MAX_REVIEW_ATTEMPTS = 5
"""Item: "Coordinator có... max review attempts, không loop vô hạn." A
task that keeps landing back in PRECHECK (NEEDS_REWORK, over and over)
without ever reaching READY/BLOCKED/NEEDS_HUMAN after this many
attempts is forced to NEEDS_HUMAN -- an operator has to look at it,
rather than the coordinator silently cycling it forever."""

MIN_PROMPT_LENGTH = 8
"""_default_scope_reasoner's own crude, disclosed heuristic threshold --
see its docstring."""


@dataclass(frozen=True)
class CoordinatorDecision:
    status: str  # one of ALL_DECISIONS
    reason: str
    blockers: tuple[str, ...] = ()
    required_actions: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason, "blockers": list(self.blockers),
                "required_actions": list(self.required_actions), "evidence": self.evidence}


class RepoEvidenceError(RuntimeError):
    """Raised by a RepoEvidenceCollector when evidence could not be read
    at all (not a git repo, git missing, permission denied, timeout).
    CoordinatorGate.review treats this as fail-closed NEEDS_HUMAN --
    NEVER as "assume clean and proceed"."""


@dataclass(frozen=True)
class RepoEvidence:
    branch: str
    head: str
    clean: bool
    status_lines: tuple[str, ...]


def git_repo_evidence(cwd: str, *, timeout: float = 10.0) -> RepoEvidence:
    """The default RepoEvidenceCollector: real `git status --porcelain`/
    `rev-parse` subprocess calls against `cwd`. Raises RepoEvidenceError
    on ANY failure -- a non-repo directory, git not installed, a
    permission error, a timeout -- rather than returning a "looks clean"
    default, which is exactly the fail-open behavior item 7 forbids."""
    def run(*args: str) -> str:
        try:
            result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                                    timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RepoEvidenceError(f"git {' '.join(args)} failed to run in {cwd!r}: {exc}") from exc
        if result.returncode != 0:
            raise RepoEvidenceError(f"git {' '.join(args)} exited {result.returncode} in {cwd!r}: "
                                    f"{result.stderr.strip()[:300]}")
        return result.stdout

    branch = run("rev-parse", "--abbrev-ref", "HEAD").strip()
    head = run("rev-parse", "HEAD").strip()
    status_output = run("status", "--porcelain")
    status_lines = tuple(line for line in status_output.splitlines() if line.strip())
    return RepoEvidence(branch=branch, head=head, clean=not status_lines, status_lines=status_lines)


RepoEvidenceCollector = Callable[[str], RepoEvidence]


@dataclass(frozen=True)
class SessionSnapshot:
    """What the Coordinator observed about the task's OWN target session
    right now -- collected by queue_engine.py (via ControllerService,
    which is already node-aware: every routed response carries node_id --
    see controller.py's own _route) and handed in here as plain data, so
    this module never has to know how to talk to a session itself."""
    node_id: str | None
    cwd: str | None
    current_command: str | None
    error: str | None = None  # set when the engine itself could not read status -- fail-closed trigger


@dataclass(frozen=True)
class OtherLaneSnapshot:
    """One OTHER session's currently-active task, for cross-lane conflict
    detection (item 3's "có conflict/race với session khác đang sửa cùng
    repo/file/branch không")."""
    session: str
    node_id: str | None
    cwd: str | None


def _default_scope_reasoner(prompt: str) -> str | None:
    """Crude, DISCLOSED heuristic for "task quá rộng/không rõ" (item 3's
    one bullet that most genuinely calls for judgment rather than a
    mechanical rule) -- returns a reason string if the prompt looks too
    vague to safely autonomously dispatch, else None. This is
    deliberately NOT a claim of real understanding: a prompt shorter
    than MIN_PROMPT_LENGTH characters (after stripping) is flagged as
    too vague; nothing else is. See this module's own docstring for why
    a real judgment call (an LLM-backed reasoner) is a separate,
    pluggable, not-yet-wired decision -- pass a different callable as
    `scope_reasoner` to CoordinatorGate to replace this."""
    stripped = (prompt or "").strip()
    if len(stripped) < MIN_PROMPT_LENGTH:
        return f"prompt is only {len(stripped)} characters -- too short/vague to safely dispatch autonomously"
    return None


ScopeReasoner = Callable[[str], "str | None"]


class CoordinatorGate:
    """The Coordinator Agent itself. `review()` is the one entry point
    queue_engine.py calls for every PRECHECK task, exactly once per
    tick -- it never sends anything, never mutates queue_store state
    itself (the engine applies the returned CoordinatorDecision via
    queue_store.record_coordinator_decision, keeping "who decides" and
    "who persists the decision" cleanly separate)."""

    def __init__(self, *, sensitive_patterns: tuple[re.Pattern, ...] = SENSITIVE_PROMPT_PATTERNS,
                evidence_collector: RepoEvidenceCollector = git_repo_evidence,
                scope_reasoner: ScopeReasoner = _default_scope_reasoner,
                max_review_attempts: int = DEFAULT_MAX_REVIEW_ATTEMPTS) -> None:
        self.sensitive_patterns = sensitive_patterns
        self.evidence_collector = evidence_collector
        self.scope_reasoner = scope_reasoner
        self.max_review_attempts = max_review_attempts

    def review(self, task: QueueTask, *, store: QueueStore, session: SessionSnapshot,
              other_active: tuple[OtherLaneSnapshot, ...] = ()) -> CoordinatorDecision:
        """Runs every check in order, returning the FIRST one that fails
        -- never partially applies, never continues past a fail-closed
        trigger to see if something else "would have passed too". Order
        matters only for which single `reason` is reported; every check
        is independently sufficient to deny READY."""
        # -1. Explicit, operator-declared blocker (task.metadata's own
        #     "artificial_blocker": a string reason, or True). A real,
        #     supported mechanism -- not a test-only backdoor -- for an
        #     operator/staging harness to deterministically force a
        #     BLOCKED decision on a specific task (e.g. "this depends on
        #     an external approval not modeled as a queue dependency
        #     yet"), and what this task's own required smoke test uses
        #     to prove "task có artificial blocker bị BLOCKED và không
        #     làm dừng queue khác" deterministically rather than relying
        #     on a real failure to happen to occur.
        artificial_blocker = task.metadata.get("artificial_blocker")
        if artificial_blocker:
            reason = artificial_blocker if isinstance(artificial_blocker, str) else "artificial_blocker=true in task metadata"
            return CoordinatorDecision(BLOCKED, reason=f"operator-declared blocker: {reason}",
                                       evidence={"artificial_blocker": artificial_blocker})

        # 0. Review-attempt budget -- never loop forever (item: Coordinator design).
        if task.coordinator_attempts >= self.max_review_attempts:
            return CoordinatorDecision(
                NEEDS_HUMAN, evidence={"coordinator_attempts": task.coordinator_attempts},
                reason=f"exceeded max coordinator review attempts ({self.max_review_attempts}) -- "
                      f"needs a human decision instead of another automatic re-review",
                required_actions=["a human reviews this task and either retries, edits, or skips/cancels it"],
            )

        # 1. Sensitive/destructive prompt screen (item 3's "destructive/
        #    sensitive"; reused pattern list from supervisor2.py).
        for pattern in self.sensitive_patterns:
            if pattern.search(task.prompt or ""):
                return CoordinatorDecision(
                    NEEDS_HUMAN, evidence={"matched_pattern": pattern.pattern},
                    reason=f"task prompt matches a sensitive/destructive pattern ({pattern.pattern!r})",
                    required_actions=["a human confirms this task is safe/intended before it is dispatched"],
                )

        # 2. Scope/clarity check (pluggable -- see _default_scope_reasoner).
        scope_reason = self.scope_reasoner(task.prompt)
        if scope_reason is not None:
            return CoordinatorDecision(
                NEEDS_HUMAN, reason=scope_reason,
                required_actions=["a human clarifies or expands the task prompt"],
            )

        # 3. Previous-task-really-done check (item 3's first bullet) --
        #    independently re-verified from evidence, not just trusted
        #    from the status label (which is exactly the gap item 11
        #    exists to close).
        previous = self._previous_task_in_lane(store, task)
        if previous is not None:
            if previous.status != COMPLETED:
                # Should not normally be reachable (claim_next_task's own
                # FIFO-per-lane invariant already guarantees this), but
                # checked independently anyway -- defense in depth, and
                # fail-closed if that invariant is ever violated by a bug
                # elsewhere.
                return CoordinatorDecision(
                    NEEDS_HUMAN, evidence={"previous_task_id": previous.id, "previous_status": previous.status},
                    reason=f"previous task in this lane ({previous.id}) is not COMPLETED "
                          f"(status={previous.status}) -- refusing to dispatch out of order",
                )
            if not previous.verification_evidence:
                return CoordinatorDecision(
                    NEEDS_REWORK, evidence={"previous_task_id": previous.id},
                    reason=f"previous task ({previous.id}) has no recorded completion evidence -- "
                          f"marked COMPLETED without a verifier/evidence attached",
                    required_actions=["re-verify the previous task's completion before proceeding"],
                )

        # 4. Session read failure -- fail-closed (item 7).
        if session.error is not None:
            return CoordinatorDecision(
                NEEDS_HUMAN, evidence={"session_error": session.error},
                reason=f"could not read the target session's status ({session.error}) -- fail-closed, refusing to dispatch",
            )

        # 5. Session identity/cwd/branch check -- the exact P0 lesson
        #    from the real window/window2 transcript-collision incident
        #    this same feature's earlier phase fixed live.
        expected_cwd = task.metadata.get("expected_cwd")
        if expected_cwd and session.cwd and session.cwd.rstrip("/\\") != str(expected_cwd).rstrip("/\\"):
            return CoordinatorDecision(
                NEEDS_HUMAN, evidence={"expected_cwd": expected_cwd, "observed_cwd": session.cwd},
                reason=f"session cwd ({session.cwd}) does not match this task's expected worktree ({expected_cwd})",
            )
        expected_node_id = task.metadata.get("expected_node_id")
        if expected_node_id and session.node_id and session.node_id != expected_node_id:
            return CoordinatorDecision(
                NEEDS_HUMAN, evidence={"expected_node_id": expected_node_id, "observed_node_id": session.node_id},
                reason=f"session is on node {session.node_id!r}, expected {expected_node_id!r}",
            )

        # 6. Cross-lane conflict (item 3's "conflict/race với session
        #    khác đang sửa cùng repo/file/branch").
        if session.cwd:
            for other in other_active:
                if other.session != task.session and other.cwd and other.cwd == session.cwd:
                    return CoordinatorDecision(
                        NEEDS_HUMAN, evidence={"conflicting_session": other.session, "cwd": session.cwd},
                        reason=f"session {other.session!r} is already actively working in the same "
                              f"repo/worktree ({session.cwd})",
                    )

        # 7. Repo evidence (git status/branch/HEAD) -- fail-closed on
        #    ANY collection failure (item 7).
        if session.cwd:
            try:
                repo = self.evidence_collector(session.cwd)
            except RepoEvidenceError as exc:
                return CoordinatorDecision(
                    NEEDS_HUMAN, evidence={"repo_evidence_error": str(exc)},
                    reason=f"could not read git/repo status for {session.cwd!r} -- fail-closed, refusing to dispatch",
                )
            if not repo.clean and not task.metadata.get("allow_dirty_repo"):
                return CoordinatorDecision(
                    NEEDS_REWORK, blockers=repo.status_lines,
                    evidence={"branch": repo.branch, "head": repo.head, "status_lines": list(repo.status_lines)},
                    reason=f"uncommitted changes present in {session.cwd!r} ({len(repo.status_lines)} line(s))",
                    required_actions=["commit or stash the uncommitted changes before this task proceeds"],
                )

        # All checks passed.
        return CoordinatorDecision(READY, reason="all coordinator checks passed",
                                   evidence={"node_id": session.node_id, "cwd": session.cwd})

    def _previous_task_in_lane(self, store: QueueStore, task: QueueTask) -> QueueTask | None:
        """The most recent (highest position < task.position) task in
        the SAME lane that isn't itself CANCELLED/SKIPPED (those are
        "never happened" for this purpose -- a cancelled task has
        nothing to verify). None if this is the first real task in its
        lane."""
        lane = store.lane_status(task.session)
        candidates = [t for t in lane["tasks"]
                     if t["position"] < task.position and t["status"] not in ("CANCELLED", "SKIPPED")]
        if not candidates:
            return None
        best = max(candidates, key=lambda t: t["position"])
        return store.get_task(best["id"])
