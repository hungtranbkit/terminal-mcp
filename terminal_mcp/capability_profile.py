"""Orchestration V1 -- the shared capability MODEL and routing diagnosis.

WHAT THE AUDIT FOUND (blg_orch_no_workers_declared). `pm_store.
capability_profiles` has zero production rows, so `pm_service._candidates()`
builds an empty candidate list and `pm_router.route_task` answers
NO_ELIGIBLE_WORKER for every task with one undifferentiated reason string.
Three separate situations were collapsed into that one answer:

    nothing is registered at all     -- declare a worker
    a capable worker is busy         -- wait, or add capacity
    every worker lacks the capability-- a real routing gap
    nobody ever said what they can do-- declare capabilities

Only the third is a capability problem. Reporting all four the same way is
why "capability routing has zero candidates" stayed unexplained: the answer
never said which of them it was.

A SECOND, QUIETER BUG. `CapabilityProfile` has BOTH `runtime_tools` and
`skills`, and `pm_router.hard_gate_failure` matched required capabilities
against `skills` only. A profile declaring `runtime_tools=["playwright"]`
was therefore not eligible for a task requiring `playwright` -- the field
existed, was populated, and was silently not consulted. `declared_capability_
set` is now the ONE rule for "what did an operator explicitly declare",
shared by pm_router and worker_registry, so the two cannot drift again.

DECLARED vs DETECTED vs UNDECLARED -- the safety property this module
exists to protect. worker_registry.py already keeps declared (operator
assertion) apart from detected (capability_probe measurement), because a
node whose operator *wrote* `claude:` into a config but never installed the
CLI was once scheduled as claude-capable. This module adds the third state
that was missing and being silently treated as the first:

    DECLARED   an operator asserted it     -> may satisfy a requirement
    DETECTED   a probe measured it         -> may satisfy a requirement
    UNDECLARED nobody ever said            -> satisfies NOTHING by itself

A worker with no capability profile is UNKNOWN, never "capable of
everything": it can only ever be satisfied by what its NODE was probed
for, and whatever that probe does not cover stays UNKNOWN rather than
becoming a yes. Note the asymmetry, which is the subtle part -- a node
probe can CONFIRM a capability but never REFUTE one, because it measures
the node while a profile describes what the session is for. The legacy
fallback is ADVISORY: an undeclared worker is surfaced in diagnostics so
an operator can see it and declare it, and is never silently eligible.

EMPTY REQUIREMENT STILL MATCHES EVERYONE. `match_nodes_by_capability` in
verify_queue.py has always treated an empty requirement as "matches every
node", and changing that would make every unconstrained task unroutable.
Same rule here, deliberately.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

# -- capability sources ---------------------------------------------------

SOURCE_DECLARED = "declared"
SOURCE_DETECTED = "detected"
SOURCE_UNDECLARED = "undeclared"

# -- per-candidate match verdicts -----------------------------------------

MATCH_OK = "MATCH_OK"
MATCH_MISSING = "MATCH_MISSING"
MATCH_UNKNOWN = "MATCH_UNKNOWN"

# -- fleet-level diagnosis codes ------------------------------------------
#
# The four the audit asked for, plus two the same walk already knows for
# free. Machine-readable on purpose: a caller should branch on the code and
# show `reason` to a human, never parse the sentence.

CANDIDATES_AVAILABLE = "CANDIDATES_AVAILABLE"
NO_WORKERS_REGISTERED = "NO_WORKERS_REGISTERED"
NO_WORKERS_ONLINE = "NO_WORKERS_ONLINE"
WORKERS_BUSY = "WORKERS_BUSY"
WORKERS_LACK_CAPABILITY = "WORKERS_LACK_CAPABILITY"
CAPABILITY_UNKNOWN = "CAPABILITY_UNKNOWN"
WORKERS_INELIGIBLE = "WORKERS_INELIGIBLE"

DIAGNOSIS_CODES = (
    CANDIDATES_AVAILABLE, NO_WORKERS_REGISTERED, NO_WORKERS_ONLINE, WORKERS_BUSY,
    WORKERS_LACK_CAPABILITY, CAPABILITY_UNKNOWN, WORKERS_INELIGIBLE,
)


def normalise_capabilities(values: Sequence[str] | str | None) -> tuple[str, ...]:
    """Casefolded, de-duplicated, order-stable, empties dropped.

    Casefolding matches `pm_router.WorkerCandidate.skill_names()`, which
    already compared skills case-insensitively -- so "Playwright" in a
    profile and "playwright" in a task requirement were already meant to
    be the same capability. Applying the same rule to runtime_tools is
    what makes the two fields interchangeable at last."""
    if values is None:
        return ()
    if isinstance(values, str):
        values = [values]
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip().casefold()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


def declared_capability_set(*, runtime_tools: Sequence[str] | None = (),
                            skills: Sequence[dict[str, Any]] | None = ()) -> tuple[str, ...]:
    """What a capability profile EXPLICITLY declares: its runtime tools
    plus the names of its skills, as one set.

    This is the single definition of "declared". worker_registry.
    _assemble() already merged these two the same way for its `Worker.
    declared_capabilities`; pm_router did not, and consulted skills alone.
    Both now call this, so a third reading cannot appear."""
    names = [str(skill.get("name")) for skill in (skills or ())
             if isinstance(skill, dict) and skill.get("name")]
    return normalise_capabilities((*(runtime_tools or ()), *names))


@dataclass(frozen=True)
class CapabilityMatch:
    """Why one worker does or does not satisfy a capability requirement.

    `missing` is what the requirement asked for and the pool did not have.
    On MATCH_UNKNOWN those are not known to be absent -- nobody ever said
    either way, which is a different fact from "absent" and the reason
    this verdict exists at all."""
    verdict: str
    required: tuple[str, ...] = ()
    matched: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    source: str = SOURCE_UNDECLARED

    @property
    def ok(self) -> bool:
        return self.verdict == MATCH_OK

    def reason(self) -> str | None:
        """The hard-gate failure sentence, or None when it passes. The
        MATCH_MISSING wording is deliberately byte-identical to what
        pm_router.hard_gate_failure has always returned -- callers and
        tests already read that string."""
        if self.verdict == MATCH_OK:
            return None
        if self.verdict == MATCH_MISSING:
            return f"missing required capabilities: {list(self.missing)}"
        return (f"capability unknown: {list(self.missing)} -- this worker has no declared "
                "capability profile and its node was not probed for this, so it is not "
                "eligible (declare it with terminal_worker_declare)")

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "required": list(self.required),
                "matched": list(self.matched), "missing": list(self.missing),
                "source": self.source}


def match_capabilities(required: Sequence[str] | None, *,
                       declared: Sequence[str] = (), detected: Sequence[str] = (),
                       has_profile: bool = True,
                       trust_declared: bool = True) -> CapabilityMatch:
    """AND semantics over the union of what this worker declared and what
    was probed for it -- the same AND rule `Worker.can()` and
    `match_nodes_by_capability` already use.

    `trust_declared=False` restricts the pool to PROBED capability only,
    for a scheduler about to send work somewhere expensive: a declared
    tool is an assertion, a probed one is a measurement.

    The UNKNOWN verdict is returned when a worker with no profile is
    missing something: nobody declared what that session is for, so its
    absence from a NODE probe is not evidence that it cannot do the work.
    UNKNOWN is never treated as eligible, so this never becomes "capable of
    everything" -- it is the honest middle state between yes and no."""
    wanted = normalise_capabilities(required)
    declared_set = normalise_capabilities(declared)
    detected_set = normalise_capabilities(detected)
    pool = set(detected_set) | (set(declared_set) if trust_declared else set())
    if not wanted:
        # Unconstrained: matches everyone, including an undeclared worker.
        # Unchanged from every existing matcher in this codebase.
        return CapabilityMatch(MATCH_OK, source=_source_of(declared_set, detected_set, has_profile))
    matched = tuple(cap for cap in wanted if cap in pool)
    missing = tuple(cap for cap in wanted if cap not in pool)
    source = _source_of(declared_set, detected_set, has_profile)
    if not missing:
        return CapabilityMatch(MATCH_OK, wanted, matched, (), source)
    if not has_profile:
        # A probe measures the NODE, not what this session is for, so a
        # node-level capability can satisfy a requirement but its absence
        # cannot refute one: a session on a node without playwright may
        # still be the fleet's WPF verifier. Undeclared therefore stays
        # UNKNOWN for whatever is missing, however much the node reported.
        return CapabilityMatch(MATCH_UNKNOWN, wanted, matched, missing, SOURCE_UNDECLARED)
    return CapabilityMatch(MATCH_MISSING, wanted, matched, missing, source)


def _source_of(declared: Sequence[str], detected: Sequence[str], has_profile: bool) -> str:
    if detected and not declared:
        return SOURCE_DETECTED
    if declared or has_profile:
        return SOURCE_DECLARED
    return SOURCE_UNDECLARED


@dataclass(frozen=True)
class CandidateView:
    """One worker as the diagnosis walk sees it. Deliberately plain data:
    the three callers (pm_router over WorkerCandidates, worker_registry
    over Workers, scheduler over Nodes) each build these from their own
    entity, so one precedence rule serves all three without any of them
    depending on another's types."""
    key: str
    online: bool = True
    busy: bool = False
    has_profile: bool = True
    capability: CapabilityMatch = field(default_factory=lambda: CapabilityMatch(MATCH_OK))
    role_match: str = MATCH_OK
    ineligible_reason: str | None = None

    @property
    def verdict(self) -> str:
        """Ordered so the reported blocker is the one an operator must act
        on first. Offline outranks everything (nothing else is knowable
        about an offline worker); UNKNOWN outranks MISSING because
        "declare it" is actionable while "it cannot do this" is a real
        gap; BUSY is only ever reached by a worker that actually matched,
        so "busy" never hides a capability problem."""
        if not self.online:
            return "OFFLINE"
        if MATCH_UNKNOWN in (self.capability.verdict, self.role_match):
            return "UNKNOWN"
        if MATCH_MISSING in (self.capability.verdict, self.role_match):
            return "MISSING"
        if self.busy:
            return "BUSY"
        if self.ineligible_reason:
            return "INELIGIBLE"
        return "ELIGIBLE"

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "verdict": self.verdict, "online": self.online,
                "busy": self.busy, "has_profile": self.has_profile,
                "capability": self.capability.to_dict(), "role_match": self.role_match,
                "ineligible_reason": self.ineligible_reason}


_REASONS = {
    CANDIDATES_AVAILABLE: "at least one worker is eligible right now",
    NO_WORKERS_REGISTERED: ("no worker is registered at all -- nothing has a capability "
                            "profile and no session is running queue work"),
    NO_WORKERS_ONLINE: "every known worker is offline",
    WORKERS_BUSY: ("a capable worker exists but is busy or at its WIP limit -- this is "
                   "capacity, not a capability gap"),
    WORKERS_LACK_CAPABILITY: ("workers are online and declared, and none of them declares "
                              "what this work requires"),
    CAPABILITY_UNKNOWN: ("workers are online but nobody declared what they can do, so they "
                         "are not eligible -- declare them with terminal_worker_declare"),
    WORKERS_INELIGIBLE: ("workers are online and capable, but every one of them failed some "
                         "other hard constraint (affinity, permissions, exclusion, pin)"),
}


def diagnose(views: Sequence[CandidateView], *,
             required_capabilities: Sequence[str] = ()) -> dict[str, Any]:
    """Turn a candidate walk into ONE machine-readable answer to "why is
    there nothing to route to".

    Precedence, highest first: something is eligible -> nothing registered
    -> nothing online -> a capable worker is busy -> nobody declared
    anything -> nobody has the capability -> some other constraint. See
    `CandidateView.verdict` for why UNKNOWN sorts above MISSING."""
    counts = {"total": len(views), "eligible": 0, "offline": 0, "busy": 0,
              "missing_capability": 0, "unknown_capability": 0, "ineligible": 0,
              "undeclared": 0}
    for view in views:
        if not view.has_profile:
            counts["undeclared"] += 1
        verdict = view.verdict
        counts[{"ELIGIBLE": "eligible", "OFFLINE": "offline", "BUSY": "busy",
                "MISSING": "missing_capability", "UNKNOWN": "unknown_capability",
                "INELIGIBLE": "ineligible"}[verdict]] += 1

    if not views:
        code = NO_WORKERS_REGISTERED
    elif counts["eligible"]:
        code = CANDIDATES_AVAILABLE
    elif counts["offline"] == len(views):
        code = NO_WORKERS_ONLINE
    elif counts["busy"]:
        code = WORKERS_BUSY
    elif counts["unknown_capability"]:
        code = CAPABILITY_UNKNOWN
    elif counts["missing_capability"]:
        code = WORKERS_LACK_CAPABILITY
    else:
        code = WORKERS_INELIGIBLE

    return {
        "code": code,
        "reason": _REASONS[code],
        "required_capabilities": list(normalise_capabilities(required_capabilities)),
        "counts": counts,
        "eligible": [v.key for v in views if v.verdict == "ELIGIBLE"],
        "candidates": [v.to_dict() for v in views],
    }
