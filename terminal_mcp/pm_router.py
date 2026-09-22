"""PM/Orchestrator Agent -- deterministic skill-based routing algorithm
(docs/REQUIREMENTS.md §20.2). Pure functions of plain data only -- no
tmux/sqlite/network I/O here, so this is trivially unit-testable (same
"deterministic, disclosed, no ML/LLM call" posture as coordinator.py's
own CoordinatorGate, reused rather than re-argued -- see that module's
docstring for the full reasoning). pm_service.py is the ONLY caller,
responsible for assembling real WorkerCandidate rows from PMStore/
QueueService/ControllerService and persisting the resulting
RoutingDecision.

Two phases, never one score blending everything (task's own explicit
requirement):
  1. Hard constraints (eligibility gate) -- deterministic, all-or-
     nothing per candidate. A pinned session/node (if the task declares
     one) must ITSELF pass every other hard constraint or routing goes
     BLOCKED with an explicit reason -- never silently rerouted away
     from an explicit human pin.
  2. Soft scoring -- only among candidates that passed phase 1: project
     affinity, skill-match count, idle/queue-depth, fairness (a
     candidate not picked in a while gets a small boost). The WINNING
     candidate's score + which factors contributed becomes the
     `reason`/`score_breakdown` this project's own explainability
     requirement asks for.

Task-side routing requirements are read from `task["metadata"]`
(`required_os`, `required_capabilities`, `project`, `pinned_session`,
`pinned_node`, `excluded_sessions`) -- a deliberate implementation
choice, same posture as the Kanban checkpoint's own UNASSIGNED_LANE
decision (docs/REQUIREMENTS.md §20.1a): this avoids ANY change to
queue_tasks' own schema/dispatch engine for a feature that only ever
READS a task and, when it decides to assign one, calls the EXISTING
QueueService.assign_task -- never a new column, never a new code path
inside queue_store.py/queue_engine.py.

Deliberately NOT enforced here as a hard gate (Phase A's own future
"WIP limits" per §20.6, not this checkpoint's scope): whether a
candidate already has queued work. `queue_depth` is a SOFT-scoring
factor only (prefer idle over already-queued) -- a session may
legitimately receive more than one queued task; only the actual
dispatch engine's own one-RUNNING-task-per-session rule (§7, already
real) is a hard limit today.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .capability_profile import (
    CandidateView, MATCH_MISSING, MATCH_OK, MATCH_UNKNOWN, declared_capability_set, diagnose,
    match_capabilities,
)

ROUTED = "ROUTED"
NO_ELIGIBLE_WORKER = "NO_ELIGIBLE_WORKER"
BLOCKED = "BLOCKED"
ROUTING_STATUSES = (ROUTED, NO_ELIGIBLE_WORKER, BLOCKED)


@dataclass(frozen=True)
class WorkerCandidate:
    """One (node_id, session)'s real, live-assembled routing profile --
    the declarative CapabilityProfile fields plus live state pm_
    service.py derives at decision time (never persisted -- see this
    module's own docstring)."""
    node_id: str
    session: str
    os: str | None = None
    runtime_tools: tuple[str, ...] = ()
    project_affinity: str | None = None
    role: str | None = None
    skills: tuple[dict[str, Any], ...] = ()  # [{"name": ..., "confidence": ...}, ...]
    online: bool = True
    permissions_ok: bool = True
    queue_depth: int = 0
    picks_since_last_fairness_reset: int = 0  # higher = picked more often recently; lower gets a fairness boost
    max_queued: int | None = None  # WIP limit (§20.6 Phase A) -- None means unbounded, same as today
    # blg_orch_no_workers_declared. `has_profile=False` is a session the
    # runtime can SEE (it is running queue work) but which nobody ever
    # declared. Default True because every candidate this module has ever
    # been given came from a capability_profiles row -- so nothing about
    # existing behaviour changes until a caller opts in by passing False.
    has_profile: bool = True
    # PROBED node capabilities, kept apart from the declared ones on
    # purpose (see capability_profile.py). Empty for every existing
    # caller, so it contributes nothing until someone wires a probe.
    detected_capabilities: tuple[str, ...] = ()

    def key(self) -> str:
        return f"{self.node_id}/{self.session}"

    def skill_names(self) -> set[str]:
        return {str(skill.get("name", "")).casefold() for skill in self.skills if skill.get("name")}

    def declared_capabilities(self) -> tuple[str, ...]:
        """Skills AND runtime_tools. `skill_names()` is kept as it was --
        it is what `_score_candidate` used to read and what the pm tests
        assert on -- but eligibility now asks this, because a profile that
        declared `runtime_tools=["playwright"]` was previously judged
        unable to do playwright work (see capability_profile.py)."""
        return declared_capability_set(runtime_tools=self.runtime_tools, skills=self.skills)

    def capability_match(self, required: Any, *, trust_declared: bool = True):
        return match_capabilities(required, declared=self.declared_capabilities(),
                                  detected=self.detected_capabilities,
                                  has_profile=self.has_profile, trust_declared=trust_declared)


@dataclass(frozen=True)
class RoutingDecision:
    status: str  # ROUTED | NO_ELIGIBLE_WORKER | BLOCKED
    chosen: WorkerCandidate | None
    reason: str
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    # Machine-readable "why is there nothing to route to" (see
    # capability_profile.diagnose). Mirrored into `evidence` as well so it
    # reaches pm_decisions without pm_store needing a new column.
    diagnosis: dict[str, Any] = field(default_factory=dict)


def _task_metadata(task: dict[str, Any]) -> dict[str, Any]:
    metadata = task.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _as_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item]


def hard_gate_failure(task: dict[str, Any], candidate: WorkerCandidate) -> str | None:
    """Returns the first hard-constraint failure reason, or None if
    `candidate` is eligible. Order matters only for which reason is
    reported first when several fail -- eligibility itself (any failure
    at all -> ineligible) does not depend on the order."""
    metadata = _task_metadata(task)
    if not candidate.online:
        return "session/node is not online"
    if not candidate.permissions_ok:
        return "session does not have effective input permission"
    required_os = metadata.get("required_os")
    if required_os and candidate.os and str(required_os).casefold() != candidate.os.casefold():
        return f"OS mismatch: task requires {required_os!r}, candidate is {candidate.os!r}"
    required_capabilities = _as_str_list(metadata.get("required_capabilities"))
    if required_capabilities:
        # One matcher for declared+probed capability, shared with
        # worker_registry (capability_profile.match_capabilities). An
        # UNDECLARED candidate fails here with a DIFFERENT reason than one
        # that is declared and genuinely lacks the tool -- and can never
        # pass, because an empty pool satisfies no requirement.
        failure = candidate.capability_match(required_capabilities).reason()
        if failure is not None:
            return failure
    required_role = metadata.get("required_role")
    if required_role:
        if candidate.role and str(required_role).casefold() != candidate.role.casefold():
            return f"role mismatch: task requires {required_role!r}, candidate role is {candidate.role!r}"
        if not candidate.role and not candidate.has_profile:
            # A declared profile that left `role` empty still passes, as it
            # always has -- tightening that would make currently-routable
            # tasks unroutable. An UNDECLARED session has no such history,
            # so it is not silently granted every role.
            return (f"role unknown: task requires {required_role!r} and this worker has no "
                    "declared capability profile")
    project = metadata.get("project")
    if project and candidate.project_affinity and str(project).casefold() != candidate.project_affinity.casefold():
        return f"project affinity mismatch: task is {project!r}, candidate is affine to {candidate.project_affinity!r}"
    excluded = {s.casefold() for s in _as_str_list(metadata.get("excluded_sessions"))}
    if candidate.session.casefold() in excluded:
        return "session is explicitly excluded for this task"
    # WIP limit (§20.6 Phase A: "PM never floods a worker's own queue
    # past it") -- a HARD gate, not a soft-scoring nudge, once a cap is
    # actually configured on the candidate's own Capability Profile
    # (max_queued=None, the default, means unbounded -- unchanged
    # behavior for every profile that never set one).
    if candidate.max_queued is not None and candidate.queue_depth >= candidate.max_queued:
        return f"WIP limit reached: {candidate.queue_depth} queued >= max_queued {candidate.max_queued}"
    return None


def candidate_view(task: dict[str, Any], candidate: WorkerCandidate) -> CandidateView:
    """One candidate as the diagnosis walk sees it. Re-uses
    `hard_gate_failure` rather than re-deriving eligibility, so a
    candidate can never be reported eligible by one and ineligible by the
    other."""
    metadata = _task_metadata(task)
    match = candidate.capability_match(_as_str_list(metadata.get("required_capabilities")))
    required_role = metadata.get("required_role")
    role_match = MATCH_OK
    if required_role:
        if candidate.role:
            role_match = (MATCH_OK if str(required_role).casefold() == candidate.role.casefold()
                          else MATCH_MISSING)
        elif not candidate.has_profile:
            role_match = MATCH_UNKNOWN
    failure = hard_gate_failure(task, candidate)
    return CandidateView(
        key=candidate.key(), online=candidate.online, has_profile=candidate.has_profile,
        # WIP is the only hard "busy" this router has: queue_depth alone is
        # a soft-scoring nudge (see this module's docstring), so a worker is
        # reported BUSY only once a configured max_queued is actually hit.
        busy=(candidate.max_queued is not None and candidate.queue_depth >= candidate.max_queued),
        capability=match, role_match=role_match, ineligible_reason=failure)


def diagnose_candidates(task: dict[str, Any], candidates: list[WorkerCandidate]) -> dict[str, Any]:
    """Why this task has nowhere to go -- NO_WORKERS_REGISTERED /
    WORKERS_BUSY / WORKERS_LACK_CAPABILITY / CAPABILITY_UNKNOWN rather
    than one sentence covering all four. Read-only and pure; safe to call
    for an explainability read without routing anything."""
    metadata = _task_metadata(task)
    return diagnose([candidate_view(task, c) for c in candidates],
                    required_capabilities=_as_str_list(metadata.get("required_capabilities")))


def _score_candidate(task: dict[str, Any], candidate: WorkerCandidate) -> tuple[float, dict[str, Any]]:
    metadata = _task_metadata(task)
    breakdown: dict[str, Any] = {}
    score = 0.0

    project = metadata.get("project")
    if project and candidate.project_affinity and str(project).casefold() == candidate.project_affinity.casefold():
        breakdown["project_affinity_match"] = 10.0
        score += 10.0

    required_role = metadata.get("required_role")
    if required_role and candidate.role and str(required_role).casefold() == candidate.role.casefold():
        breakdown["role_match"] = 5.0
        score += 5.0

    required_capabilities = _as_str_list(metadata.get("required_capabilities"))
    preferred_capabilities = _as_str_list(metadata.get("preferred_capabilities"))
    # Everything this candidate can do from either source -- scoring reads
    # the same pool eligibility does, so a runtime_tool that now makes a
    # candidate eligible also earns it the match points it deserves.
    capability_pool = set(candidate.declared_capabilities()) | set(candidate.detected_capabilities)
    matched_required = len(set(c.casefold() for c in required_capabilities) & capability_pool)
    matched_preferred = len(set(c.casefold() for c in preferred_capabilities) & capability_pool)
    if matched_required:
        breakdown["required_skill_match_count"] = matched_required
        score += matched_required * 3.0
    if matched_preferred:
        breakdown["preferred_skill_match_count"] = matched_preferred
        score += matched_preferred * 1.5

    # Idle/load: fewer already-queued tasks scores higher -- a simple,
    # bounded, disclosed heuristic (same posture as coordinator.py's own
    # scope_reasoner), not a claim of real load-prediction.
    idle_bonus = max(0.0, 5.0 - candidate.queue_depth)
    breakdown["idle_bonus"] = round(idle_bonus, 2)
    score += idle_bonus

    # Fairness: a candidate picked less recently gets a small boost, so
    # routing doesn't always pile onto the same one eligible worker when
    # several are equally qualified (task's own explicit "fairness --
    # starvation prevention" requirement).
    fairness_bonus = max(0.0, 3.0 - candidate.picks_since_last_fairness_reset * 0.5)
    breakdown["fairness_bonus"] = round(fairness_bonus, 2)
    score += fairness_bonus

    breakdown["total"] = round(score, 2)
    return score, breakdown


def route_task(task: dict[str, Any], candidates: list[WorkerCandidate]) -> RoutingDecision:
    """The whole two-phase algorithm, in one pure call. `task` is a plain
    dict shaped like QueueTask.to_dict()/QueueService.board() row (only
    `id`/`metadata` are actually read)."""
    metadata = _task_metadata(task)
    pinned_session = metadata.get("pinned_session")
    pinned_node = metadata.get("pinned_node")

    if pinned_session:
        pinned = next(
            (c for c in candidates if c.session == pinned_session
             and (not pinned_node or c.node_id == pinned_node)),
            None,
        )
        if pinned is None:
            return RoutingDecision(
                BLOCKED, None,
                reason=f"pinned session/node is not eligible: no capability profile found for "
                      f"session={pinned_session!r} node={pinned_node!r}",
                evidence={"pinned_session": pinned_session, "pinned_node": pinned_node},
            )
        failure = hard_gate_failure(task, pinned)
        if failure is not None:
            return RoutingDecision(
                BLOCKED, None, reason=f"pinned session/node is not eligible: {failure}",
                evidence={"pinned_session": pinned_session, "pinned_node": pinned_node, "hard_gate_failure": failure},
            )
        score, breakdown = _score_candidate(task, pinned)
        return RoutingDecision(
            ROUTED, pinned, reason=f"human pin: routed to {pinned.key()} (explicit pinned_session)",
            score_breakdown=breakdown, evidence={"pinned": True},
        )

    eligible: list[WorkerCandidate] = []
    failures: dict[str, str] = {}
    for candidate in candidates:
        failure = hard_gate_failure(task, candidate)
        if failure is None:
            eligible.append(candidate)
        else:
            failures[candidate.key()] = failure

    if not eligible:
        diagnosis = diagnose_candidates(task, candidates)
        return RoutingDecision(
            NO_ELIGIBLE_WORKER, None,
            reason=f"{diagnosis['code']}: {diagnosis['reason']} -- stays UNASSIGNED, retried "
                  "automatically once a capability profile changes or a node reconnects",
            evidence={"candidates_considered": len(candidates), "hard_gate_failures": failures,
                     "diagnosis": diagnosis},
            diagnosis=diagnosis,
        )

    scored = [(candidate, *_score_candidate(task, candidate)) for candidate in eligible]
    # Deterministic tie-break: highest score wins; ties broken by session
    # name so the same input always produces the same output (task's own
    # explicit "deterministic, no ML/LLM" requirement extends to tie-
    # breaking too -- never an arbitrary dict/set iteration order).
    scored.sort(key=lambda row: (-row[1], row[0].key()))
    winner, winner_score, winner_breakdown = scored[0]
    all_scores = {candidate.key(): breakdown for candidate, _score, breakdown in scored}
    reason_bits = [f"{key}={value}" for key, value in winner_breakdown.items() if key != "total"]
    reason = f"routed to {winner.key()} (score {winner_score:.2f}: {', '.join(reason_bits) or 'no scoring factors matched'})"
    return RoutingDecision(
        ROUTED, winner, reason=reason, score_breakdown=winner_breakdown,
        evidence={"candidates_considered": len(candidates), "eligible_count": len(eligible),
                 "hard_gate_failures": failures, "all_scores": all_scores},
    )
