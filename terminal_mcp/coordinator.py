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
import inspect
import os
import subprocess
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Callable

from .core import RECOVERY_STATE_RESUMED_OK
from . import worktree_cleanup
from .contract import CAP_REPO_EVIDENCE
from .queue_store import COMPLETED, QueueStore, QueueTask

READY = "READY"
BLOCKED = "BLOCKED"
NEEDS_REWORK = "NEEDS_REWORK"
NEEDS_HUMAN = "NEEDS_HUMAN"
ALL_DECISIONS = (READY, BLOCKED, NEEDS_REWORK, NEEDS_HUMAN)

# The prompt screen, in two tiers.
#
# WHY IT IS NOT ONE FLAT KEYWORD LIST ANY MORE. The original list was
# supervisor2.py's ATTENTION_STOP_PATTERNS -- a screen designed for a PANE'S
# OUTPUT, where a bare "token" or "credential" really is a signal. Applied to a
# task's PROMPT -- paragraphs of developer prose -- it matched almost nothing
# it meant to. Every one of the ten prompts it gated on this fleet was a false
# positive, and the four worst were self-inflicted:
#
#   "stop only for credential/destructive blockers"  -> matched 'credential'
#   "Do not merge main and do not push (no remote)"  -> matched 'merge main'
#   "ONE shared neutral visual token"                -> matched 'token'
#   "Reply with exactly this token: PHASEB_OK_..."   -> matched 'token'
#
# The first two are the prompt telling the agent to BE careful, and the gate
# reading its own safety instruction as the danger. A screen with a 100% false
# positive rate is not a security control: it trains everyone to wave tasks
# through, which is how the real ones get waved through too.
#
# So: a noun is no longer evidence. An ACTION on that noun is. And a match
# inside a negation ("do not ...", "never ...") is not a match at all.

_NEGATORS = (
    r"do not", r"don'?t", r"never", r"avoid", r"refuse to", r"without",
    # "stop only for credential/destructive blockers", "ask only for
    # credentials" -- the prompt delegating the escalation, not requesting it.
    r"stop (?:only )?(?:for|at)", r"ask (?:only )?(?:for|about)", r"escalate (?:only )?for",
    r"only if", r"unless",
    # "no API keys in repo", "no plaintext credentials in source/docs/logs".
    # A rule the prompt is imposing on itself, and the single most common way
    # a security-conscious prompt named a secret at all.
    r"\bno\b",
)
_NEGATION_WINDOW = 40
"""How far back a negator may sit and still govern the match. Deliberately
about a clause, not a sentence: far enough to catch "do not merge main", short
enough that an unrelated earlier "never" cannot launder a real instruction."""

_NEGATION_RE = re.compile(
    r"(?:" + "|".join(_NEGATORS) + r")[^.\n]{0,%d}$" % _NEGATION_WINDOW, re.IGNORECASE)


def _negated(text: str, start: int) -> bool:
    """Is the match at `start` governed by a negation just before it?

    Looks only at the text back to the start of the current clause, so a
    "do not" in a previous sentence never reaches forward into this one."""
    window = text[max(0, start - (_NEGATION_WINDOW + 24)):start]
    clause = re.split(r"[.\n;,]", window)[-1]
    return bool(_NEGATION_RE.search(clause))


# Tier 1: DESTRUCTIVE. An instruction to destroy, force-publish, or escalate
# privilege. These are imperative commands, not topics, so they stay exactly
# as they were -- this tier is the reason the gate exists and is not relaxed.
DESTRUCTIVE_PROMPT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
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

# Tier 2: SECRET HANDLING. A credential noun on its own is a topic; what makes
# it a gate is a verb that would EXPOSE it (print/paste/commit it somewhere) or
# SET it (write a real one into a config). Both are listed explicitly rather
# than inferred, and both are subject to the same negation rule as tier 1.
_SECRET_NOUN = (
    r"(?:passwords?|passphrases?|api[_ -]?keys?|client[_ -]?secrets?|private keys?|"
    r"credentials?|secret keys?|"
    # A bare "token" is a design token as often as a bearer token on this
    # fleet, so `token` counts only when something qualifies it as a secret.
    r"(?:auth|access|bearer|refresh|session|api|github|gitlab|npm|personal[_ -]access|secret)"
    r"[_ -]tokens?)"
)
_EXPOSE_VERB = (
    r"(?:print|echo|cat|show|reveal|display|dump|log|paste|send|post|upload|share|"
    r"e-?mail|leak|exfiltrat\w*|hard-?code|embed)"
)
_SET_VERB = r"(?:set|add|put|write|store|save|configure|rotate|replace|update|generate|create)"
_SINK = r"(?:git|repo(?:sitory)?|commit|source|chat|slack|e-?mail|log|logs|issue|ticket|comment|pr\b)"

SECRET_PROMPT_PATTERNS = (
    # An interactive credential prompt, by its own signature.
    re.compile(r"enter (your |the )?(password|passphrase|api[_ -]?key)", re.IGNORECASE),
    # "print the API key", "paste the access token"
    re.compile(rf"\b{_EXPOSE_VERB}\b[^.\n]{{0,24}}\b{_SECRET_NOUN}\b", re.IGNORECASE),
    # "set the GitHub token", "rotate the client secret"
    re.compile(rf"\b{_SET_VERB}\b[^.\n]{{0,24}}\b{_SECRET_NOUN}\b", re.IGNORECASE),
    # "the credentials into the repo", "the api key in the commit"
    re.compile(rf"\b{_SECRET_NOUN}\b[^.\n]{{0,30}}\b(?:into|in|to)\b[^.\n]{{0,20}}\b{_SINK}",
               re.IGNORECASE),
)

SENSITIVE_PROMPT_PATTERNS = DESTRUCTIVE_PROMPT_PATTERNS + SECRET_PROMPT_PATTERNS

DEFAULT_MAX_REVIEW_ATTEMPTS = 5
"""Item: "Coordinator có... max review attempts, không loop vô hạn." A
task that keeps landing back in PRECHECK (NEEDS_REWORK, over and over)
without ever reaching READY/BLOCKED/NEEDS_HUMAN after this many
attempts is forced to NEEDS_HUMAN -- an operator has to look at it,
rather than the coordinator silently cycling it forever."""

MIN_PROMPT_LENGTH = 8
"""_default_scope_reasoner's own crude, disclosed heuristic threshold --
see its docstring."""

DEFAULT_REPEATED_FAILURE_THRESHOLD = 3
"""§20.6 Phase E "Agent failure policy": how many times in a row the
EXACT SAME coordinator decision reason has to repeat, with zero
progress, before this is treated as a stuck loop and forced to
NEEDS_HUMAN -- see CoordinatorGate.__init__'s own comment for why this
is deliberately lower than DEFAULT_MAX_REVIEW_ATTEMPTS."""


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


class RepoEvidenceUnavailable(RepoEvidenceError):
    """The evidence could not be COLLECTED here -- as distinct from a repo
    that was read and found wanting.

    The difference is the whole point. A controller cannot see the filesystem
    of a session running on another node, so a local `git status` against that
    session's cwd fails with "not a git repository" -- which reads exactly like
    a broken repo and is nothing of the sort. Reporting that as a repo failure
    is a FALSE NEGATIVE: it blames the worker's repo for the controller having
    looked in the wrong place.

    Still fail-closed by default -- the gate refuses to dispatch on unverified
    evidence -- but with an accurate reason and a deliberate, auditable opt-in,
    rather than a misleading one and no way forward."""


class RepoEvidenceNotARepository(RepoEvidenceError):
    """`cwd` exists on this host and is simply not a git repository.

    This is an ANSWER, not a failure to look. It used to be reported as
    "could not read git/repo status ... fail-closed", which sent operators to
    debug a repository that does not exist -- observed live on a task whose
    session sat in a scratchpad directory.

    Nothing is weakened by accepting it: the two checks the evidence feeds are
    "are there uncommitted changes" and "has this branch diverged", and a
    directory with no repository in it can be neither. It is recorded on the
    decision so the absence is visible rather than assumed."""


@dataclass(frozen=True)
class RepoEvidence:
    branch: str
    head: str
    clean: bool
    status_lines: tuple[str, ...]
    # Production-readiness pass (task: "git dirty/conflict/diverged/
    # unpushed state có ảnh hưởng không") -- has_upstream=False (a fresh
    # local-only branch, or one with no configured remote-tracking
    # branch at all) is a legitimate, common state, NOT an error/evidence
    # failure; ahead/behind are simply 0 in that case (nothing to compare
    # against), never guessed.
    has_upstream: bool = False
    ahead: int = 0
    behind: int = 0

    @property
    def diverged(self) -> bool:
        """True only when BOTH ahead and behind are nonzero -- the real
        "needs a merge/rebase decision, not safe to auto-continue" state.
        Being merely ahead (unpushed local commits) or merely behind (a
        fast-forward away) is routine mid-task state, not a divergence."""
        return self.has_upstream and self.ahead > 0 and self.behind > 0


def git_repo_evidence(cwd: str, node_id: str | None = None, *,
                      timeout: float = 10.0) -> RepoEvidence:
    """The default RepoEvidenceCollector: real `git status --porcelain`/
    `rev-parse`/`rev-list` subprocess calls against `cwd`. Raises
    RepoEvidenceError on ANY failure a real repo could not legitimately
    produce -- git not installed, a permission error, a timeout, `cwd`
    not a git repo at all -- rather than returning a "looks clean"
    default, which is exactly the fail-open behavior item 7 forbids. A
    branch with no upstream configured is NOT such a failure (see
    RepoEvidence.has_upstream).

    `node_id` is accepted so every collector shares one signature. This one
    only ever reads the LOCAL filesystem, so a caller that hands it a remote
    session's cwd gets RepoEvidenceUnavailable rather than a local `git` run
    against a path that means nothing on this host."""
    if not os.path.isdir(cwd):
        # The single most common way this is reached is a session on another
        # node: its cwd is perfectly valid THERE and absent here. Saying
        # "could not read git status" would pin that on the repo.
        raise RepoEvidenceUnavailable(
            f"{cwd!r} does not exist on this host"
            + (f" -- the session runs on node {node_id!r}, whose filesystem this "
               f"controller cannot see" if node_id else ""))

    def run(*args: str, allow_failure: bool = False) -> tuple[int, str]:
        try:
            result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                                    timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RepoEvidenceError(f"git {' '.join(args)} failed to run in {cwd!r}: {exc}") from exc
        if result.returncode != 0 and not allow_failure:
            raise RepoEvidenceError(f"git {' '.join(args)} exited {result.returncode} in {cwd!r}: "
                                    f"{result.stderr.strip()[:300]}")
        return result.returncode, result.stdout

    # Asked FIRST and allowed to fail: this is the one question that
    # distinguishes "not a repository" from "the repository is unreadable",
    # and every call below would otherwise fail with the same opaque message.
    inside_code, inside = run("rev-parse", "--is-inside-work-tree", allow_failure=True)
    if inside_code != 0 or inside.strip() != "true":
        raise RepoEvidenceNotARepository(f"{cwd!r} is not a git repository")

    _, branch = run("rev-parse", "--abbrev-ref", "HEAD")
    branch = branch.strip()
    _, head = run("rev-parse", "HEAD")
    head = head.strip()
    _, status_output = run("status", "--porcelain")
    status_lines = tuple(line for line in status_output.splitlines() if line.strip())
    # `@{upstream}` resolution fails (a real, expected, non-zero exit --
    # not a repo-read failure) whenever the current branch has no
    # remote-tracking branch configured -- allow_failure=True here is
    # what distinguishes that ordinary case from a genuine git/repo
    # problem, which the two calls above (never allow_failure) still
    # catch and fail-closed on.
    upstream_code, upstream_name = run("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}",
                                       allow_failure=True)
    has_upstream = upstream_code == 0 and bool(upstream_name.strip())
    ahead = behind = 0
    if has_upstream:
        _, counts = run("rev-list", "--left-right", "--count", "HEAD...@{upstream}")
        parts = counts.split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            ahead, behind = int(parts[0]), int(parts[1])
    return RepoEvidence(branch=branch, head=head, clean=not status_lines, status_lines=status_lines,
                        has_upstream=has_upstream, ahead=ahead, behind=behind)


# (cwd, node_id) -> evidence. node_id is None for a local session.
RepoEvidenceCollector = Callable[..., RepoEvidence]


# How old a node's evidence may be before the gate refuses to rely on it. A
# node answers from its own clock, so this is deliberately generous -- it is a
# guard against a cached or replayed reply, not a clock-sync mechanism.
DEFAULT_EVIDENCE_MAX_AGE_SECONDS = 300.0


def _evidence_staleness(collected_at: Any, max_age_seconds: float) -> str | None:
    """Describe why this evidence is unusable, or None when it is fine.

    A payload with no timestamp is NOT assumed fresh: an agent that cannot say
    when it looked cannot support a claim about the repo's state now.
    """
    if collected_at is None:
        return "undated (the agent did not say when it was collected)"
    try:
        when = datetime.fromisoformat(str(collected_at))
    except (TypeError, ValueError):
        return f"timestamped unusably ({collected_at!r})"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - when).total_seconds()
    if age > max_age_seconds:
        return f"{int(age)}s old (limit {int(max_age_seconds)}s)"
    if age < -max_age_seconds:
        # A future timestamp means the clocks disagree badly enough that the
        # age is meaningless; treating it as fresh would trust that skew.
        return f"dated {int(-age)}s in the future (clock skew)"
    return None


def node_aware_repo_evidence(local_node_id: str | None = None,
                             node_client_factory: Callable[[str], Any] | None = None,
                             max_age_seconds: float = DEFAULT_EVIDENCE_MAX_AGE_SECONDS,
                             ) -> RepoEvidenceCollector:
    """Collect evidence where the session actually lives.

    Local session -> read this filesystem, exactly as before.
    Remote session -> ask THAT node through the node adapter. A node whose
    agent predates the repo-evidence endpoint answers 404, which is reported
    as UNAVAILABLE (we could not look) and never as a repo failure.
    """
    def collect(cwd: str, node_id: str | None = None, **kwargs: Any) -> RepoEvidence:
        if node_id is None or (local_node_id is not None and node_id == local_node_id):
            return git_repo_evidence(cwd, node_id, **kwargs)
        if node_client_factory is None:
            raise RepoEvidenceUnavailable(
                f"session is on node {node_id!r} and this controller has no node "
                f"adapter configured to ask it for repo evidence")
        try:
            client = node_client_factory(node_id)
        except Exception as exc:  # noqa: BLE001 -- any lookup failure is "cannot look"
            raise RepoEvidenceUnavailable(
                f"no reachable node adapter for {node_id!r}: {exc}") from exc
        if client is None or not hasattr(client, "repo_evidence"):
            raise RepoEvidenceUnavailable(
                f"node {node_id!r} does not expose repo evidence (its agent predates "
                f"the /v1/repo-evidence endpoint)")
        try:
            payload = client.repo_evidence(cwd)
        except Exception as exc:  # noqa: BLE001
            raise RepoEvidenceUnavailable(
                f"node {node_id!r} could not be asked for repo evidence: {exc}") from exc
        if not isinstance(payload, dict):
            raise RepoEvidenceUnavailable(
                f"node {node_id!r} returned no usable repo evidence for {cwd!r}: {payload!r}")
        # An agent that predates the endpoint is identified by its DECLARED
        # capability, not by a 404. A 404 is also what a misrouted request, a
        # stale proxy or a half-deployed agent returns, and none of those may
        # be mistaken for "this node genuinely speaks an older protocol".
        capabilities = payload.get("contract_capabilities")
        if capabilities is not None and CAP_REPO_EVIDENCE not in capabilities:
            raise RepoEvidenceUnavailable(
                f"node {node_id!r} answered but does not declare the "
                f"{CAP_REPO_EVIDENCE!r} capability (contract v"
                f"{payload.get('contract_version', 0)}) -- treating as OLD AGENT "
                f"rather than as verified")
        if payload.get("error"):
            # The node ANSWERED and says the repo is bad. That is real evidence
            # about its own filesystem, not a failure to look -- so it fails
            # closed as a repo problem and is not waivable by
            # allow_unverified_repo, which exists only for "we could not see".
            raise RepoEvidenceError(
                f"node {node_id!r} could not read the repo at {cwd!r}: "
                f"{payload.get('detail') or payload.get('error')}")
        # An answer that never says the repo was valid is not an answer. An
        # older or partial payload omits repo_valid entirely, and defaulting
        # that to True would turn a silence into a verification.
        if payload.get("repo_valid") is not True:
            raise RepoEvidenceUnavailable(
                f"node {node_id!r} did not confirm repo_valid for {cwd!r} "
                f"(got {payload.get('repo_valid')!r}) -- not treating as verified")
        stale = _evidence_staleness(payload.get("collected_at"), max_age_seconds)
        if stale is not None:
            raise RepoEvidenceUnavailable(
                f"node {node_id!r} returned evidence for {cwd!r} that is {stale} -- "
                f"refusing to gate on evidence that may no longer describe the repo")
        status_lines = tuple(payload.get("status_lines") or ())
        return RepoEvidence(
            branch=str(payload.get("branch") or ""), head=str(payload.get("head") or ""),
            clean=bool(payload.get("clean", not status_lines)), status_lines=status_lines,
            has_upstream=bool(payload.get("has_upstream", False)),
            ahead=int(payload.get("ahead") or 0), behind=int(payload.get("behind") or 0))

    return collect


@dataclass(frozen=True)
class SmokeTestResult:
    passed: bool
    output_tail: str
    returncode: int | None = None


SmokeTestRunner = Callable[[tuple[str, ...], str, float], SmokeTestResult]
"""(command, cwd, timeout_seconds) -> SmokeTestResult. Pluggable exactly
like RepoEvidenceCollector/ScopeReasoner -- the default (run_smoke_test_
command below) is a real subprocess call; tests inject a fake to avoid
spawning slow/real processes."""

DEFAULT_SMOKE_TEST_TIMEOUT_SECONDS = 120.0
SMOKE_TEST_OUTPUT_TAIL_CHARS = 2000


def run_smoke_test_command(command: tuple[str, ...], cwd: str, timeout_seconds: float) -> SmokeTestResult:
    """The default SmokeTestRunner (task item 2: "test/build/smoke còn
    fail không") -- OPT-IN per task (task.metadata['require_smoke_test_
    command']), never run for an ordinary task that didn't declare one
    (this module has no way to guess what "the test suite" is for an
    arbitrary repo). A real subprocess call, bounded by timeout_seconds
    -- a timeout is itself treated as a FAILED smoke test (fail-closed:
    "did it definitely pass" is the only way to get READY), never
    silently ignored or treated as a pass."""
    try:
        result = subprocess.run(list(command), cwd=cwd, capture_output=True, text=True,
                                timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") + (exc.stderr or "")
        return SmokeTestResult(passed=False, output_tail=(partial or "(no output)")[-SMOKE_TEST_OUTPUT_TAIL_CHARS:],
                               returncode=None)
    except OSError as exc:
        return SmokeTestResult(passed=False, output_tail=f"failed to run {command!r}: {exc}", returncode=None)
    combined = (result.stdout or "") + (result.stderr or "")
    return SmokeTestResult(passed=result.returncode == 0, output_tail=combined[-SMOKE_TEST_OUTPUT_TAIL_CHARS:],
                           returncode=result.returncode)


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
    # Production-readiness pass (task: "session có đang WAITING_INPUT/
    # BLOCKED/stream stale/node offline không") -- straight from
    # terminal_status's own classify_status()-derived fields (core.py),
    # never re-derived or guessed here.
    state: str | None = None            # e.g. "RUNNING" | "WAITING_INPUT" | "IDLE" | "UNKNOWN"
    input_required: bool | None = None
    reader_alive: bool | None = None    # Windows backend only; None (no such concept) on tmux -- never treated as False
    # Conversation-continuity follow-up (2026-09-07): RESTORING/
    # RECOVERY_FAILED from session_registry.py's own transient recovery_
    # state column (via terminal_status -- core.py's _status_payload),
    # None for the overwhelmingly common case (no recovery history, or
    # the session's last recovery -- if any -- already resolved to
    # RESUMED_OK and was cleared back to None on the next ordinary ACTIVE
    # sighting).
    recovery_state: str | None = None


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


def _recent_coordinator_reasons(store: QueueStore, task: QueueTask, *, limit: int) -> list[str]:
    """§20.6 Phase E: this task's own most recent coordinator-decision
    reasons, newest first, for the repeated-identical-failure check.
    Reuses QueueStore.list_events (§7, already real) rather than adding
    a second history mechanism -- that method is bounded/ordered by
    SESSION, not task_id, so this filters client-side to this one
    task's own "COORDINATOR_DECISION" events (the ONE event type
    guaranteed emitted exactly once per real coordinator decision --
    the sibling "COORDINATOR_{status}" event that same call also
    writes would double-count each decision if included here).
    A generous-but-still-bounded scan window (never "the full
    history") -- same disclosed-heuristic posture as
    _default_scope_reasoner above."""
    scan_window = max(limit * 20, 100)
    events = store.list_events(task.session, limit=scan_window)
    reasons = [
        event["reason"] for event in events
        if event.get("task_id") == task.id and event.get("event_type") == "COORDINATOR_DECISION"
        and event.get("reason")
    ]
    return reasons[:limit]


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
                smoke_test_runner: SmokeTestRunner = run_smoke_test_command,
                max_review_attempts: int = DEFAULT_MAX_REVIEW_ATTEMPTS,
                require_approval_for_risk_levels: tuple[str, ...] = (),
                repeated_failure_threshold: int | None = DEFAULT_REPEATED_FAILURE_THRESHOLD) -> None:
        self.sensitive_patterns = sensitive_patterns
        self.evidence_collector = evidence_collector
        self.scope_reasoner = scope_reasoner
        self.smoke_test_runner = smoke_test_runner
        self.max_review_attempts = max_review_attempts
        # Agent failure policy (§20.6 Phase E): ON by default (unlike
        # the risk-level gate above) -- this is a real EXTENSION of the
        # already-always-on max_review_attempts cap just below, not a
        # new opt-in behavior change. Deliberately set LOWER than
        # max_review_attempts (3 < 5, the two real defaults) so a
        # genuine stuck-in-a-loop pattern (the exact same coordinator
        # decision reason repeating, not just "many attempts") is
        # caught sooner than the raw attempt budget alone would.
        # None disables this check entirely (falls back to the raw
        # attempt-count cap only).
        self.repeated_failure_threshold = repeated_failure_threshold
        # Risk classification (§20.6 Phase A): OFF by default (empty
        # tuple -- "exact gate strength is a per-project policy, not
        # hardcoded here", task's own explicit words) -- a project opts
        # in by passing e.g. ("HIGH", "CRITICAL") to require an explicit
        # human/PM sign-off (task.metadata.risk_approved: true) before a
        # task at one of these declared risk_level values may reach
        # READY. A task with no risk_level declared at all is never
        # blocked by this -- only a task that explicitly declares itself
        # HIGH/CRITICAL (§20.1's own risk_level field) and whose
        # project has opted this specific level into the requirement.
        self.require_approval_for_risk_levels = require_approval_for_risk_levels

    def _collect_repo_evidence(self, cwd: str, node_id: str | None) -> RepoEvidence:
        """Call the configured collector, whichever signature it has.

        Collectors predating node-awareness take only `cwd`; several tests and
        embeddings supply one. Rather than force every caller to change at
        once, the node is passed when the collector can accept it and dropped
        when it cannot -- a one-argument collector is inherently local, which
        is exactly what it was before.
        """
        collector = self.evidence_collector
        try:
            signature = inspect.signature(collector)
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            return collector(cwd)
        takes_node = len(signature.parameters) > 1 or any(
            parameter.kind is inspect.Parameter.VAR_POSITIONAL
            for parameter in signature.parameters.values())
        return collector(cwd, node_id) if takes_node else collector(cwd)

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

        # -0.5. Risk-level approval gate (§20.6 Phase A) -- OFF by
        #       default (self.require_approval_for_risk_levels == ()).
        #       When a project HAS opted a risk_level into this
        #       requirement and this task declares that exact level,
        #       an explicit metadata.risk_approved: true (a human/PM
        #       sign-off, never inferred) is required before READY --
        #       never a guess at whether a HIGH/CRITICAL task is "safe
        #       enough" from the prompt text alone.
        risk_level = task.metadata.get("risk_level")
        if risk_level in self.require_approval_for_risk_levels and not task.metadata.get("risk_approved"):
            return CoordinatorDecision(
                NEEDS_HUMAN, evidence={"risk_level": risk_level},
                reason=f"risk_level={risk_level!r} requires explicit human/PM approval "
                      f"(metadata.risk_approved: true) before this project will dispatch it",
                required_actions=["a human/PM reviews this task and sets metadata.risk_approved=true, "
                                 "or reduces/reclassifies its risk_level"],
            )

        # -0.4. Repeated-identical-failure ("stuck in a loop") detection
        #       (§20.6 Phase E) -- a real EXTENSION of the raw attempt-
        #       count cap just below: not "how many times has this been
        #       tried" but "did the exact same coordinator decision
        #       reason repeat, with zero actual progress". Reads this
        #       task's own real event history (queue_events, §7,
        #       already real) -- a best-effort scan of the lane's most
        #       recent events (bounded, same "disclosed heuristic, not
        #       perfect" posture as this module's own scope_reasoner):
        #       for a small threshold this is more than enough in
        #       practice, never a claim of scanning the FULL history.
        if self.repeated_failure_threshold and task.coordinator_attempts >= self.repeated_failure_threshold:
            recent_reasons = _recent_coordinator_reasons(store, task, limit=self.repeated_failure_threshold)
            if len(recent_reasons) >= self.repeated_failure_threshold and len(set(recent_reasons)) == 1:
                return CoordinatorDecision(
                    NEEDS_HUMAN, evidence={"repeated_reason": recent_reasons[0], "repeat_count": len(recent_reasons)},
                    reason=f"the exact same coordinator decision reason repeated {len(recent_reasons)} times in "
                          f"a row ({recent_reasons[0]!r}) -- stuck in a loop, needs a human rather than another "
                          f"identical automatic retry",
                    required_actions=["a human reviews this task -- repeating the same automatic retry will not help"],
                )

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
        prompt_text = task.prompt or ""
        for pattern in self.sensitive_patterns:
            for match in pattern.finditer(prompt_text):
                # "Do not merge main", "stop only for credential blockers":
                # the prompt is ruling the action OUT. Gating on it told an
                # operator the task wanted to do the very thing it forbade.
                # Keep scanning -- a later, ungoverned occurrence of the same
                # pattern in the same prompt still gates.
                if _negated(prompt_text, match.start()):
                    continue
                excerpt = prompt_text[max(0, match.start() - 40):match.end() + 40].replace("\n", " ")
                return CoordinatorDecision(
                    NEEDS_HUMAN,
                    evidence={"matched_pattern": pattern.pattern, "matched_text": match.group(0),
                              "excerpt": excerpt},
                    reason=f"task prompt matches a sensitive/destructive pattern ({pattern.pattern!r}) "
                           f"at {match.group(0)!r}",
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

        # 4b. Session state check (task item: "session có đang WAITING_
        #     INPUT/BLOCKED/stream stale/node offline không") -- a
        #     session already waiting on a human/other prompt, or whose
        #     output stream is known-dead, must never receive a NEW
        #     dispatch on top -- that would interleave the new task's
        #     text with whatever is already pending and confuse both.
        #     "node offline" is covered by session.error above (a
        #     SESSION_UNREACHABLE-class error never even reaches here --
        #     queue_engine.py routes it to WAITING_SESSION before the
        #     coordinator gate ever runs), so this is specifically the
        #     "session IS reachable but not actually available" case.
        if session.input_required or session.state == "WAITING_INPUT":
            return CoordinatorDecision(
                NEEDS_HUMAN, evidence={"session_state": session.state, "input_required": session.input_required},
                reason="session is already waiting on input (a prompt/confirmation pending) -- "
                      "refusing to dispatch a new task on top of it",
            )
        if session.reader_alive is False:
            return CoordinatorDecision(
                NEEDS_HUMAN, evidence={"reader_alive": False},
                reason="session's own output stream reader is not alive (stale stream) -- "
                      "refusing to dispatch until it's confirmed healthy",
            )
        # 4c. Recovery-in-flight/failed check (conversation-continuity
        #     follow-up, 2026-09-07, task item 5: "task RUNNING khi
        #     restart... sau resume Coordinator xác minh agent thực sự
        #     tiếp tục đúng task trước khi state trở lại RUNNING") -- a
        #     session terminal_registry_reopen is still mid-resume
        #     (RESTORING) or whose last resume attempt is known to have
        #     failed (RECOVERY_FAILED) must never receive a fresh
        #     dispatch: RESTORING could still land moments before the new
        #     text (a real double-dispatch/interleaving risk, same class
        #     as the WAITING_INPUT check above); RECOVERY_FAILED means
        #     nobody has yet confirmed this session is genuinely the same
        #     conversation continuing -- dispatching onto it would risk
        #     silently treating an unverified/failed recovery as if it
        #     were an ordinary healthy session. Requires an explicit
        #     human/operator resolution (a fresh registry_reopen retry,
        #     or accepting a plain new session) before this task's own
        #     session can be dispatched into again.
        # Auto Recovery follow-up (2026-09-07): the same gate now also
        # covers the 3 new automatic-reconciliation states (recovery_
        # engine.py) -- RECOVERY_PENDING (about to attempt, not started),
        # RECOVERY_DEGRADED (a real new process exists but WITHOUT
        # verified conversation continuity), RECOVERY_BLOCKED (policy/
        # metadata explicitly refused an attempt) -- every one of these
        # is "do not trust this session's own continuity yet" exactly
        # like RESTORING/RECOVERY_FAILED already were, so this is a
        # denylist-of-the-one-good-value check now rather than an
        # allowlist of two, automatically covering any of these five
        # without needing its own separate branch.
        if session.recovery_state is not None and session.recovery_state != RECOVERY_STATE_RESUMED_OK:
            return CoordinatorDecision(
                NEEDS_HUMAN, evidence={"recovery_state": session.recovery_state},
                reason=f"session's own conversation recovery is {session.recovery_state} -- "
                      "refusing to dispatch until a human confirms it actually continued the right task",
            )

        # 5. Session identity/cwd/branch check -- the exact P0 lesson
        #    from the real window/window2 transcript-collision incident
        #    this same feature's earlier phase fixed live.
        expected_cwd = task.metadata.get("expected_cwd")
        # Checked BEFORE the cwd comparison below, deliberately. Once a task's
        # worktree has been reclaimed, the session's cwd will of course not
        # match `expected_cwd` -- so the generic mismatch branch would fire and
        # tell an operator the session is in the wrong directory, when the truth
        # is that the directory is gone and is never coming back. Same refusal
        # either way; this one names the actual cause (contract F12).
        if expected_cwd and worktree_cleanup.is_removed(task.metadata):
            return CoordinatorDecision(
                NEEDS_HUMAN,
                evidence={"expected_cwd": expected_cwd,
                          "worktree_cleanup": task.metadata.get(worktree_cleanup.METADATA_KEY)},
                reason=f"{worktree_cleanup.WORKTREE_REMOVED}: this task's worktree "
                       f"({expected_cwd}) was reclaimed by the worktree janitor -- it must be "
                       f"recreated before this task can be dispatched again",
            )
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
                if other.session == task.session or not other.cwd:
                    continue
                if other.cwd != session.cwd:
                    continue
                # SAME PATH IS NOT THE SAME DIRECTORY unless it is the same
                # machine. Every node in this fleet lays its workspaces out
                # the same way, so /home/kimex/workspace/terminal-mcp names a
                # different working tree on each of them -- comparing the
                # string alone reported a conflict between two sessions that
                # could not touch each other's files. Only compared when both
                # nodes are actually known; an unknown node stays a conflict,
                # because refusing to dispatch is the safe direction.
                if (other.node_id and session.node_id and other.node_id != session.node_id):
                    continue
                return CoordinatorDecision(
                    NEEDS_HUMAN, evidence={"conflicting_session": other.session, "cwd": session.cwd,
                                           "node_id": session.node_id},
                    reason=f"session {other.session!r} is already actively working in the same "
                          f"repo/worktree ({session.cwd})",
                )

        # 7. Repo evidence (git status/branch/HEAD) -- fail-closed on
        #    ANY collection failure (item 7).
        repo: RepoEvidence | None = None
        not_a_repository: str | None = None
        if session.cwd:
            try:
                repo = self._collect_repo_evidence(session.cwd, session.node_id)
            except RepoEvidenceUnavailable as exc:
                # We could not LOOK. That is not evidence the repo is bad, and
                # reporting it as such sends an operator to debug a healthy
                # repository. Still fail-closed -- dispatching on unverified
                # evidence is exactly what this gate exists to prevent -- but
                # with the real reason and a deliberate way through.
                if task.metadata.get("allow_unverified_repo"):
                    repo = None
                else:
                    return CoordinatorDecision(
                        NEEDS_HUMAN,
                        evidence={"repo_evidence_unavailable": str(exc),
                                  "session_node_id": session.node_id,
                                  "cwd": session.cwd,
                                  "override": "set task metadata allow_unverified_repo=true to "
                                              "dispatch without repo verification"},
                        reason=f"repo evidence for {session.cwd!r} could not be collected "
                               f"({exc}) -- fail-closed, refusing to dispatch",
                        required_actions=[
                            "give this node a repo-evidence capable agent, or set "
                            "allow_unverified_repo on the task to accept dispatch without it"],
                    )
            except RepoEvidenceNotARepository as exc:
                # STILL FAIL-CLOSED. A task whose session is sitting somewhere
                # that is not a repository is usually a session in the wrong
                # place, and that is worth stopping for.
                #
                # What changes is the REASON. This used to be reported as
                # "could not read git/repo status ... fail-closed", which sent
                # an operator to debug a repository that does not exist --
                # observed live on a task whose session sat in a scratchpad
                # directory. It now says what is actually true, and offers the
                # same deliberate, auditable opt-out RepoEvidenceUnavailable
                # already had rather than leaving no way forward.
                if task.metadata.get("allow_unverified_repo"):
                    repo = None
                    not_a_repository = str(exc)
                else:
                    return CoordinatorDecision(
                        NEEDS_HUMAN,
                        evidence={"not_a_git_repository": str(exc), "cwd": session.cwd,
                                  "session_node_id": session.node_id,
                                  "override": "set task metadata allow_unverified_repo=true to "
                                              "dispatch into a directory with no repository"},
                        reason=f"{session.cwd!r} is not a git repository -- fail-closed, refusing "
                               f"to dispatch (the session may be in the wrong directory)",
                        required_actions=[
                            "point the session at the task's repository/worktree, or set "
                            "allow_unverified_repo on the task if this work genuinely has no repo"],
                    )
            except RepoEvidenceError as exc:
                return CoordinatorDecision(
                    NEEDS_HUMAN, evidence={"repo_evidence_error": str(exc)},
                    reason=f"could not read git/repo status for {session.cwd!r} -- fail-closed, refusing to dispatch",
                )
        if repo is not None:
            if not repo.clean and not task.metadata.get("allow_dirty_repo"):
                return CoordinatorDecision(
                    NEEDS_REWORK, blockers=repo.status_lines,
                    evidence={"branch": repo.branch, "head": repo.head, "status_lines": list(repo.status_lines)},
                    reason=f"uncommitted changes present in {session.cwd!r} ({len(repo.status_lines)} line(s))",
                    required_actions=["commit or stash the uncommitted changes before this task proceeds"],
                )
            # 7b. Diverged from upstream (task item: "...conflict/diverged/
            #     unpushed state có ảnh hưởng không") -- both ahead AND
            #     behind means a merge/rebase decision is needed; a human
            #     call, not something to auto-resolve. Merely ahead
            #     (unpushed commits) or merely behind (a clean fast-
            #     forward) is routine mid-task state and never blocks on
            #     its own -- ahead/behind are still always recorded as
            #     evidence either way, visible on the dashboard.
            if repo.diverged and not task.metadata.get("allow_diverged_branch"):
                return CoordinatorDecision(
                    NEEDS_HUMAN,
                    evidence={"branch": repo.branch, "head": repo.head, "ahead": repo.ahead, "behind": repo.behind},
                    reason=f"branch {repo.branch!r} has diverged from its upstream "
                          f"({repo.ahead} ahead, {repo.behind} behind) -- needs a human merge/rebase decision",
                    required_actions=["a human resolves the divergence (merge or rebase) before this task proceeds"],
                )

        # 8. Smoke test (task item 2: "test/build/smoke còn fail không")
        #    -- OPT-IN per task via metadata['require_smoke_test_command']
        #    (a list of argv strings); a task that doesn't declare one is
        #    completely unaffected (this module has no way to guess what
        #    "the test suite" is for an arbitrary repo/task). A failure
        #    here is NEEDS_REWORK, not BLOCKED -- the expected remediation
        #    is a corrective task routed back to the SAME worker (task's
        #    own explicit "ưu tiên đưa corrective task quay lại chính
        #    worker"), not a hard stop requiring a human every time.
        smoke_command = task.metadata.get("require_smoke_test_command")
        if smoke_command and session.cwd:
            timeout_seconds = float(task.metadata.get("smoke_test_timeout_seconds", DEFAULT_SMOKE_TEST_TIMEOUT_SECONDS))
            result = self.smoke_test_runner(tuple(smoke_command), session.cwd, timeout_seconds)
            if not result.passed:
                return CoordinatorDecision(
                    NEEDS_REWORK,
                    evidence={"smoke_test_command": list(smoke_command), "returncode": result.returncode,
                             "output_tail": result.output_tail},
                    reason=f"smoke test command {list(smoke_command)!r} failed (returncode={result.returncode})",
                    required_actions=["fix the failing test/build/smoke check before this task proceeds"],
                )

        # All checks passed.
        ready_evidence: dict[str, Any] = {"node_id": session.node_id, "cwd": session.cwd}
        if not_a_repository is not None:
            ready_evidence["repo_evidence"] = not_a_repository
        if repo is not None:
            ready_evidence.update({"branch": repo.branch, "head": repo.head, "has_upstream": repo.has_upstream,
                                   "ahead": repo.ahead, "behind": repo.behind})
        return CoordinatorDecision(READY, reason="all coordinator checks passed", evidence=ready_evidence)

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
