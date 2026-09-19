"""Which runtimes could run this task, which could not, and why -- for each one.

THE RULE THIS MODULE ENFORCES

Never blindly choose an idle session. "Idle" is the weakest possible evidence
of suitability: this fleet is full of idle sessions sitting in worktrees that
were deleted weeks ago, in the wrong repository, on a node whose heartbeat has
gone stale, or holding a grant pinned to a tmux process that no longer exists.
Dispatching into one of those is worse than leaving the task queued, because
the task then LOOKS started while the prompt goes nowhere.

So eligibility is decided in two stages that are deliberately different in
kind:

  HARD REJECTS are safety facts. A session that fails one cannot run this task
  at all, and no amount of affinity elsewhere can buy its way past. They are
  checked first and they short-circuit, so a rejection reason names the FIRST
  disqualifying fact rather than a pile of them.

  SCORING is preference among sessions that are all genuinely usable. It is
  pure arithmetic over a fixed table -- no tie-breaking cleverness, no
  randomness, no model call -- because a routing decision has to be
  reproducible three days later when someone asks why a task went where it did.

WHAT "UNKNOWN" MEANS, AND WHY IT IS NOT "NO"

The controller runs on ONE host. A session's cwd on a remote node names a
directory on ANOTHER machine, and stat()ing it here would report this host's
filesystem under that path -- the exact confidently-wrong answer
controller._with_resource_health already refuses to give for git state. So a
worktree that cannot be checked is `None`, not `False`, and None never
hard-rejects. The same principle applies to repo identity: an unknown repo is
scored down (we are guessing) but a KNOWN-DIFFERENT repo is rejected (we are
not guessing at all).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .node_models import CAPACITY_HEALTHY, CAPACITY_OVERLOADED, NODE_ONLINE
from .project_identity import normalise_git_remote
from .task_profile import TaskProfile

# -- hard rejection reasons ------------------------------------------------
NODE_OFFLINE = "NODE_OFFLINE"
NODE_DRAINING = "NODE_DRAINING"
NODE_OVERLOADED = "NODE_OVERLOADED"
SESSION_NOT_ACTIVE = "SESSION_NOT_ACTIVE"
WORKTREE_MISSING = "WORKTREE_MISSING"
WAITING_INPUT = "WAITING_INPUT"
RUNTIME_MISMATCH = "RUNTIME_MISMATCH"
INPUT_NOT_PERMITTED = "INPUT_NOT_PERMITTED"
STALE_IDENTITY = "STALE_IDENTITY"
SESSION_BUSY = "SESSION_BUSY"
SESSION_CLAIMED = "SESSION_CLAIMED"
REPO_MISMATCH = "REPO_MISMATCH"
MISSING_REQUIRED_SKILL = "MISSING_REQUIRED_SKILL"
AGENT_BOUND_ELSEWHERE = "AGENT_BOUND_ELSEWHERE"

#: Human-readable text for each rejection, so the dashboard never has to
#: maintain its own copy of this vocabulary.
REJECTION_TEXT: dict[str, str] = {
    NODE_OFFLINE: "node is offline or unreachable",
    NODE_DRAINING: "node is draining; no new work placed here",
    NODE_OVERLOADED: "node is overloaded",
    SESSION_NOT_ACTIVE: "session is missing/killed/deleted in the registry",
    WORKTREE_MISSING: "the session's working directory no longer exists",
    WAITING_INPUT: "session is blocked waiting for human input",
    RUNTIME_MISMATCH: "session runtime does not match the runtime this task requires",
    INPUT_NOT_PERMITTED: "input into this session is not permitted",
    STALE_IDENTITY: "the session's grant is pinned to a process that no longer exists",
    SESSION_BUSY: "session is actively running something else",
    SESSION_CLAIMED: "another task already holds this session",
    REPO_MISMATCH: "session is checked out in a different repository",
    MISSING_REQUIRED_SKILL: "session does not provide a skill this task requires",
    AGENT_BOUND_ELSEWHERE: "this task's agent is bound to a different session",
}

# -- scoring table ---------------------------------------------------------
#
# One table, one place. Every number below is a preference between usable
# sessions, never a safety decision -- those are the hard rejects above.
SCORE_AGENT_BINDING = 50
SCORE_SAME_PROJECT = 30
SCORE_SAME_REPO = 30
SCORE_REQUIRED_SKILLS = 20
SCORE_IDLE = 20
SCORE_BRANCH_AFFINITY = 10
SCORE_OPTIONAL_SKILL = 5
SCORE_HEALTHY_NODE = 10
SCORE_CONTEXT_ROOMY = 10
PENALTY_CONTEXT_TIGHT = -20
PENALTY_DIRTY_BRANCH = -40
PENALTY_REPO_UNKNOWN = -50
PENALTY_QUEUE_BACKLOG = -5

CONTEXT_ROOMY_PERCENT = 70.0
CONTEXT_TIGHT_PERCENT = 85.0

#: A candidate must clear this to be dispatched to. Scoring can legitimately go
#: negative (an unknown repo alone is -50), and dispatching into a session the
#: arithmetic has just called a bad idea would make the score decorative.
MIN_ELIGIBLE_SCORE = 0

#: Session states that mean "nothing is running here right now".
IDLE_STATES = frozenset({"IDLE"})
#: Session states that mean a human, not the queue, owns the next move.
BLOCKED_STATES = frozenset({"WAITING_INPUT"})
#: Session states that mean work is in flight.
BUSY_STATES = frozenset({"RUNNING"})


@dataclass(frozen=True)
class SessionCandidate:
    """One runtime, described in the terms routing actually decides on.

    Assembled from sources that already exist -- the controller's fleet
    listing, the session registry, node registry rows and the durable queue --
    never from a new probe of its own. A field nothing could establish is None,
    and None is always read as "unknown", never as a value."""

    session: str
    node_id: str | None = None
    node_name: str | None = None
    node_online: bool = True
    node_draining: bool = False
    node_capacity: str | None = None
    node_agent_types: tuple[str, ...] = ()
    is_local_node: bool = False
    runtime: str | None = None
    state: str | None = None
    state_probed: bool = False
    """False means `state` came from the registry's last-known value rather
    than a live read. The matcher scores on it but never hard-rejects a
    top-ranked candidate on it -- see `probe` in `rank`."""
    input_allowed: bool = True
    stale_identity_pin: bool = False
    registry_status: str | None = None
    cwd: str | None = None
    worktree_path: str | None = None
    worktree_exists: bool | None = None
    repo: str | None = None
    branch: str | None = None
    dirty: bool | None = None
    context_percent: float | None = None
    project: str | None = None
    project_id: str | None = None
    agent_id: str | None = None
    skills: tuple[str, ...] = ()
    bindings: tuple[str, ...] = ()
    active_tasks: int = 0
    queued_tasks: int = 0
    claimed_by_task: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "session": self.session, "node_id": self.node_id, "node_name": self.node_name,
            "runtime": self.runtime, "state": self.state, "state_probed": self.state_probed,
            "repo": self.repo, "branch": self.branch, "cwd": self.cwd,
            "worktree_path": self.worktree_path, "worktree_exists": self.worktree_exists,
            "context_percent": self.context_percent, "project": self.project,
            "project_id": self.project_id, "agent_id": self.agent_id,
            "skills": list(self.skills), "active_tasks": self.active_tasks,
            "queued_tasks": self.queued_tasks, "claimed_by_task": self.claimed_by_task,
        }


@dataclass(frozen=True)
class ScoredCandidate:
    candidate: SessionCandidate
    score: int
    reasons: tuple[str, ...] = ()
    rejected: str | None = None
    """The FIRST hard-reject reason, or None. Set means ineligible whatever the
    score says."""

    @property
    def eligible(self) -> bool:
        return self.rejected is None and self.score >= MIN_ELIGIBLE_SCORE

    def as_dict(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "session": self.candidate.session, "node_id": self.candidate.node_id,
            "score": self.score, "reasons": list(self.reasons), "eligible": self.eligible,
        }
        if self.rejected:
            row["rejected"] = self.rejected
            row["rejected_detail"] = REJECTION_TEXT.get(self.rejected, self.rejected)
        elif not self.eligible:
            row["rejected"] = "SCORE_BELOW_THRESHOLD"
            row["rejected_detail"] = (f"score {self.score} is below the minimum "
                                      f"{MIN_ELIGIBLE_SCORE} required to dispatch")
        return row


@dataclass
class MatchResult:
    """The whole decision, including everything that did NOT win.

    The rejected list is not diagnostics-for-later: it is persisted with the
    task and shown on the dashboard, because "queued, and here is what was
    wrong with each of the eleven sessions we looked at" is the answer an
    operator needs and the answer this system could not previously give."""

    chosen: ScoredCandidate | None = None
    ranked: list[ScoredCandidate] = field(default_factory=list)
    considered: int = 0

    @property
    def eligible(self) -> list[ScoredCandidate]:
        return [row for row in self.ranked if row.eligible]

    def rejections(self, limit: int = 5) -> list[dict[str, Any]]:
        """The most informative near-misses first.

        Ordered by score descending, so the operator sees the sessions that
        ALMOST worked -- a list led by the worst candidates would be true and
        useless."""
        return [row.as_dict() for row in self.ranked if not row.eligible][:limit]

    def as_dict(self, *, limit: int = 5) -> dict[str, Any]:
        return {
            "chosen": self.chosen.as_dict() if self.chosen else None,
            "considered": self.considered,
            "eligible_count": len(self.eligible),
            "top_candidates": [row.as_dict() for row in self.ranked[:limit]],
            "rejected": self.rejections(limit),
        }


def _norm_repo(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    return normalise_git_remote(text) or text.rstrip("/")


def _repo_matches(profile_repo: str | None, candidate: SessionCandidate) -> bool:
    """True when this session is demonstrably in the repository the task wants.

    Compares against BOTH the candidate's normalised remote and its checkout
    path, because the profile may carry either form -- a caller who passed a
    directory and a caller who passed a git URL are naming the same thing, and
    failing to match them would reject every session for a path-shaped task.
    """
    if not profile_repo:
        return False
    wanted = _norm_repo(profile_repo)
    if wanted is None:
        return False
    for known in (candidate.repo, candidate.worktree_path, candidate.cwd):
        normalised = _norm_repo(known)
        if normalised and normalised == wanted:
            return True
    return False


def _repo_known_different(profile: TaskProfile, candidate: SessionCandidate) -> bool:
    """True only when both repos are KNOWN and they differ.

    The asymmetry is the whole point: unknown is scored, known-different is
    rejected. A session whose repo we cannot read might be the right one; a
    session we can see is in another repository is not."""
    if not profile.repo or not candidate.repo:
        return False
    return not _repo_matches(profile.repo, candidate)


def default_worktree_probe(candidate: SessionCandidate) -> bool | None:
    """Does this session's working directory still exist?

    LOCAL NODE ONLY. A remote session's cwd names a path on another machine;
    answering from this host's filesystem would be a confidently wrong answer,
    and a wrong True here dispatches a task into a deleted worktree while a
    wrong False rejects a perfectly good session. Unknown is the honest answer
    and, per this module's contract, unknown never rejects.
    """
    if not candidate.is_local_node:
        return None
    path = candidate.worktree_path or candidate.cwd
    if not path:
        return None
    return os.path.isdir(path)


def hard_reject(profile: TaskProfile, candidate: SessionCandidate) -> str | None:
    """The first fact that makes this session unusable for this task, or None.

    Ordered cheapest-and-most-fundamental first: there is no point reporting a
    repo mismatch on a session whose node is offline."""
    if not candidate.node_online:
        return NODE_OFFLINE
    if candidate.node_draining:
        return NODE_DRAINING
    if candidate.node_capacity == CAPACITY_OVERLOADED:
        return NODE_OVERLOADED
    if candidate.registry_status is not None and candidate.registry_status != "ACTIVE":
        return SESSION_NOT_ACTIVE
    if not candidate.input_allowed:
        return INPUT_NOT_PERMITTED
    if candidate.stale_identity_pin:
        return STALE_IDENTITY
    if candidate.worktree_exists is False:
        return WORKTREE_MISSING
    state = (candidate.state or "").upper()
    if state in BLOCKED_STATES:
        return WAITING_INPUT
    if state in BUSY_STATES:
        return SESSION_BUSY
    if candidate.claimed_by_task and candidate.claimed_by_task != profile.task_id:
        return SESSION_CLAIMED
    if candidate.active_tasks > 0:
        return SESSION_BUSY
    if profile.runtime_required and profile.preferred_runtime and candidate.runtime:
        if str(candidate.runtime).lower() != str(profile.preferred_runtime).lower():
            return RUNTIME_MISMATCH
    if _repo_known_different(profile, candidate):
        return REPO_MISMATCH
    if profile.required_skills and candidate.skills:
        # Only enforced when the candidate ADVERTISES skills. A session that
        # declares none has not said it lacks them, and rejecting on silence
        # would make the whole fleet ineligible the moment a task names a
        # skill -- which is a worse failure than a slightly loose match.
        missing = [skill for skill in profile.required_skills if skill not in candidate.skills]
        if missing:
            return MISSING_REQUIRED_SKILL
    if profile.agent_id and candidate.agent_id and candidate.agent_id != profile.agent_id:
        return AGENT_BOUND_ELSEWHERE
    return None


def score(profile: TaskProfile, candidate: SessionCandidate) -> tuple[int, list[str]]:
    """Preference among usable sessions. Pure arithmetic over the table above."""
    total = 0
    reasons: list[str] = []

    if profile.agent_id and candidate.agent_id == profile.agent_id:
        total += SCORE_AGENT_BINDING
        reasons.append(f"+{SCORE_AGENT_BINDING} agent {profile.agent_id} is bound to this session")
    if profile.project and candidate.project and profile.project == candidate.project:
        total += SCORE_SAME_PROJECT
        reasons.append(f"+{SCORE_SAME_PROJECT} same project ({profile.project})")
    elif profile.project_id and candidate.project_id and profile.project_id == candidate.project_id:
        total += SCORE_SAME_PROJECT
        reasons.append(f"+{SCORE_SAME_PROJECT} same project_id ({profile.project_id})")

    if _repo_matches(profile.repo, candidate):
        total += SCORE_SAME_REPO
        reasons.append(f"+{SCORE_SAME_REPO} same repo ({profile.repo})")
    elif profile.repo and not candidate.repo:
        total += PENALTY_REPO_UNKNOWN
        reasons.append(f"{PENALTY_REPO_UNKNOWN} task needs repo {profile.repo} and this "
                       f"session's repo is unknown")

    if profile.required_skills:
        if candidate.skills and all(skill in candidate.skills for skill in profile.required_skills):
            total += SCORE_REQUIRED_SKILLS
            reasons.append(f"+{SCORE_REQUIRED_SKILLS} provides every required skill")
    matched_optional = [skill for skill in profile.optional_skills if skill in candidate.skills]
    if matched_optional:
        total += SCORE_OPTIONAL_SKILL
        reasons.append(f"+{SCORE_OPTIONAL_SKILL} optional skill match ({', '.join(matched_optional)})")

    if (candidate.state or "").upper() in IDLE_STATES:
        total += SCORE_IDLE
        reasons.append(f"+{SCORE_IDLE} session is IDLE")

    if profile.branch and candidate.branch and profile.branch == candidate.branch:
        total += SCORE_BRANCH_AFFINITY
        reasons.append(f"+{SCORE_BRANCH_AFFINITY} already on branch {profile.branch}")

    if candidate.node_capacity == CAPACITY_HEALTHY:
        total += SCORE_HEALTHY_NODE
        reasons.append(f"+{SCORE_HEALTHY_NODE} node capacity healthy")

    if candidate.context_percent is not None:
        if candidate.context_percent >= CONTEXT_TIGHT_PERCENT:
            total += PENALTY_CONTEXT_TIGHT
            reasons.append(f"{PENALTY_CONTEXT_TIGHT} context at {candidate.context_percent:.0f}% "
                           f"(>= {CONTEXT_TIGHT_PERCENT:.0f}%)")
        elif candidate.context_percent < CONTEXT_ROOMY_PERCENT:
            total += SCORE_CONTEXT_ROOMY
            reasons.append(f"+{SCORE_CONTEXT_ROOMY} context at {candidate.context_percent:.0f}% "
                           f"(< {CONTEXT_ROOMY_PERCENT:.0f}%)")

    if candidate.dirty and profile.branch and candidate.branch and candidate.branch != profile.branch:
        # Dirty ALONE is normal -- an agent session mid-task is dirty by
        # definition. Dirty on the WRONG branch is the conflict: taking this
        # task here means either stashing someone's work or branching over it.
        total += PENALTY_DIRTY_BRANCH
        reasons.append(f"{PENALTY_DIRTY_BRANCH} uncommitted work on a different branch "
                       f"({candidate.branch}, task wants {profile.branch})")

    if candidate.queued_tasks:
        total += PENALTY_QUEUE_BACKLOG * min(candidate.queued_tasks, 4)
        reasons.append(f"{PENALTY_QUEUE_BACKLOG * min(candidate.queued_tasks, 4)} "
                       f"{candidate.queued_tasks} task(s) already queued here")

    return total, reasons


def evaluate(profile: TaskProfile, candidate: SessionCandidate) -> ScoredCandidate:
    """One candidate, fully judged: hard rejects first, then arithmetic.

    A rejected candidate is still SCORED. That costs nothing and it is what
    makes the dashboard's rejection list ordered by how close each session came
    rather than by the arbitrary order the fleet listing happened to return."""
    rejected = hard_reject(profile, candidate)
    total, reasons = score(profile, candidate)
    return ScoredCandidate(candidate=candidate, score=total, reasons=tuple(reasons), rejected=rejected)


def rank(profile: TaskProfile, candidates: Iterable[SessionCandidate], *,
         probe: Callable[[SessionCandidate], SessionCandidate] | None = None,
         probe_limit: int = 5) -> MatchResult:
    """Rank the fleet for this task and pick the best genuinely usable session.

    WHY THE LIVE PROBE IS RANKED, NOT EXHAUSTIVE. Deciding on registry state
    alone risks dispatching into a session that has started waiting for input
    since it was last seen; probing every session in the fleet costs one
    network round trip per session on every routing decision, which is the
    cost that makes a router something an operator turns off. So: score
    everything from cheap durable state, then re-verify in rank order and stop
    at the first candidate that still passes with live evidence. The session we
    actually dispatch into is always freshly verified; the ones we did not
    choose never needed to be.
    """
    scored = [evaluate(profile, candidate) for candidate in candidates]
    scored.sort(key=lambda row: (-row.score, row.candidate.session))
    result = MatchResult(ranked=scored, considered=len(scored))
    if probe is None:
        result.chosen = next((row for row in scored if row.eligible), None)
        return result

    probes_used = 0
    for index, row in enumerate(scored):
        if not row.eligible:
            continue
        if probes_used >= probe_limit:
            break
        probes_used += 1
        refreshed = evaluate(profile, probe(row.candidate))
        scored[index] = refreshed
        if refreshed.eligible:
            # Re-sort so the persisted evidence reflects the post-probe truth;
            # the chosen row is already known, so this only affects display.
            result.ranked = sorted(scored, key=lambda item: (-item.score, item.candidate.session))
            result.chosen = refreshed
            return result
    result.ranked = sorted(scored, key=lambda item: (-item.score, item.candidate.session))
    result.chosen = None
    return result
