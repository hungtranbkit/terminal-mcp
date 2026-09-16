"""Does this work run carry enough analysis to be allowed to run.

WHY THIS IS A SEPARATE MODULE

`work_store.transition_run` already centralises *which* edges exist. This
module answers a different question -- whether the run has done the thinking
the edge implies -- and keeping the two apart means the transition table stays
a statement about the lifecycle, not a place where content rules accumulate.

It is a pure function over a run's `metadata`. It opens no database, reads no
config file and performs no I/O, so it can be unit-tested exhaustively and
called from inside an open SQLite transaction without widening it.

THE ENFORCEMENT BOUNDARY, AND WHY LEGACY RUNS ARE NOT BROKEN

A run opts in by carrying `metadata["analysis_gate"]["version"] >= 1`. A run
created before this module existed has no such key, so it evaluates to
NOT_ENFORCED and behaves exactly as it did yesterday. That is deliberate: a
gate that retroactively invalidates every existing run is a gate nobody can
deploy. Deployments that want the stricter posture pass `require_gate=True`
(wired to a config flag) and get MISSING_GATE instead.

FAIL-CLOSED, AND WHERE IT STOPS

Evidence that is *absent* on an un-opted-in run is not a failure -- that run
never claimed to have any. Evidence that is absent, malformed or unrecognised
on a run that DID opt in IS a failure, because that run asserted the gate
applies to it and then did not supply what the gate reads. The same principle
`work_eligibility.evaluate` already states for dispatch: "Unknown evidence is
NOT treated as permission."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

GATE_VERSION = 1
"""Current schema version of the `analysis_gate` metadata block. A run
declaring a HIGHER version than this build understands is refused rather than
approximated -- an older controller must not silently accept a payload whose
rules it cannot evaluate."""

METADATA_KEY = "analysis_gate"

PROFILE_FULL = "full"
PROFILE_FAST_FIX = "fast_fix"
PROFILES = (PROFILE_FULL, PROFILE_FAST_FIX)

# The full analysis profile. `state_model` is the one field allowed to be
# explicitly not-applicable, because plenty of real work has no state machine
# and forcing a fabricated one is worse than admitting there is none.
FULL_REQUIRED_FIELDS = (
    "problem_statement",
    "user_observable_goal",
    "source_of_truth",
    "state_model",
    "invariants",
    "assumptions",
    "edge_cases",
    "dangerous_failure_modes",
    "acceptance_tests",
    "live_verification",
    "critic_result",
)

# The fast-fix profile. Deliberately short: a one-line regression fix that had
# to write eleven analysis fields would be routed around, and a gate that gets
# routed around protects nothing.
FAST_FIX_REQUIRED_FIELDS = (
    "reproduce",
    "root_cause",
    "expected_behavior",
    "invariant",
    "regression_test",
    "live_verify",
)

NOT_APPLICABLE_ALLOWED = frozenset({"state_model"})
"""Fields that may be answered `{"n/a": true, "reason": "..."}`. The reason is
mandatory -- "n/a" with no justification is indistinguishable from "not done"
and must not read as done."""

# Assumption bookkeeping.
IMPACT_HIGH = "high"
IMPACT_VALUES = frozenset({"high", "medium", "low"})
RESOLUTION_RESOLVED = frozenset({"resolved", "verified", "confirmed"})

# Verdict reasons.
PASS = "PASS"
NOT_ENFORCED = "NOT_ENFORCED"
MISSING_GATE = "MISSING_GATE"
MISSING_FIELDS = "MISSING_FIELDS"
UNRESOLVED_HIGH_IMPACT_ASSUMPTION = "UNRESOLVED_HIGH_IMPACT_ASSUMPTION"
MALFORMED_ASSUMPTION = "MALFORMED_ASSUMPTION"
MALFORMED_GATE = "MALFORMED_GATE"
UNKNOWN_PROFILE = "UNKNOWN_PROFILE"
UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"


class AnalysisGateError(ValueError):
    """Refused for want of analysis, with a reason a human can act on."""

    def __init__(self, verdict: "GateVerdict") -> None:
        super().__init__(verdict.message())
        self.verdict = verdict


@dataclass(frozen=True)
class GateVerdict:
    allowed: bool
    reason: str
    detail: str
    profile: str | None = None
    version: int | None = None
    missing_fields: tuple[str, ...] = ()
    blocking_assumptions: tuple[str, ...] = field(default_factory=tuple)

    def message(self) -> str:
        parts = [self.detail]
        if self.missing_fields:
            parts.append(f"missing: {', '.join(self.missing_fields)}")
        if self.blocking_assumptions:
            parts.append(f"unresolved high-impact: {', '.join(self.blocking_assumptions)}")
        return " -- ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed, "reason": self.reason, "detail": self.detail,
            "profile": self.profile, "version": self.version,
            "missing_fields": list(self.missing_fields),
            "blocking_assumptions": list(self.blocking_assumptions),
        }


def _is_filled(value: Any) -> bool:
    """Present AND non-empty. `""`, `[]`, `{}` and None all mean "not done";
    treating an empty list of edge cases as "edge cases considered" is exactly
    the silent pass this module exists to prevent."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _is_declared_na(value: Any) -> bool:
    """`{"n/a": true, "reason": "<non-empty>"}` and nothing looser."""
    if not isinstance(value, Mapping):
        return False
    flag = value.get("n/a", value.get("na"))
    if flag is not True:
        return False
    return bool(str(value.get("reason") or "").strip())


def _missing(block: Mapping[str, Any], required: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    for name in required:
        value = block.get(name)
        if _is_filled(value):
            continue
        if name in NOT_APPLICABLE_ALLOWED and _is_declared_na(value):
            continue
        out.append(name)
    return tuple(out)


def _check_assumptions(raw: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Returns (malformed, blocking).

    Every assumption needs confidence, impact and resolution. A high-impact
    assumption that is not resolved blocks: the whole point of writing it down
    was that being wrong about it would be expensive.
    """
    malformed: list[str] = []
    blocking: list[str] = []
    if not isinstance(raw, (list, tuple)):
        return ("assumptions is not a list",), ()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            malformed.append(f"#{index} is not an object")
            continue
        label = str(item.get("id") or item.get("statement") or f"#{index}")[:80]
        impact = str(item.get("impact") or "").strip().casefold()
        confidence = item.get("confidence")
        resolution = str(item.get("resolution") or "").strip().casefold()
        if not _is_filled(confidence) or not impact or not resolution:
            malformed.append(f"{label} lacks confidence/impact/resolution")
            continue
        if impact not in IMPACT_VALUES:
            # Unrecognised impact is not "probably low". Fail closed.
            malformed.append(f"{label} has unrecognised impact {impact!r}")
            continue
        if impact == IMPACT_HIGH and resolution not in RESOLUTION_RESOLVED:
            blocking.append(f"{label} (resolution={resolution})")
    return tuple(malformed), tuple(blocking)


def evaluate(metadata: Mapping[str, Any] | None, *,
             require_gate: bool = False) -> GateVerdict:
    """Decide whether a run carrying `metadata` may enter an enforced state.

    `require_gate=False` (the default) is the backward-compatible posture:
    a run that never opted in is not judged. Set it True to require every run
    to carry the block.
    """
    block = (metadata or {}).get(METADATA_KEY)

    if block is None:
        if require_gate:
            return GateVerdict(
                False, MISSING_GATE,
                f"no {METADATA_KEY!r} block and this deployment requires one")
        return GateVerdict(
            True, NOT_ENFORCED,
            f"no {METADATA_KEY!r} block; run predates the gate or did not opt in")

    if not isinstance(block, Mapping):
        return GateVerdict(False, MALFORMED_GATE,
                           f"{METADATA_KEY!r} is {type(block).__name__}, expected an object")

    raw_version = block.get("version")
    try:
        version = int(raw_version)
    except (TypeError, ValueError):
        # Opted in but unreadable. Fail closed -- see module docstring.
        return GateVerdict(False, UNSUPPORTED_VERSION,
                           f"version {raw_version!r} is not an integer")
    if version < 1:
        return GateVerdict(False, UNSUPPORTED_VERSION,
                           f"version {version} is below 1", version=version)
    if version > GATE_VERSION:
        return GateVerdict(
            False, UNSUPPORTED_VERSION,
            f"run declares gate version {version}; this build understands "
            f"{GATE_VERSION} and will not approximate a newer contract",
            version=version)

    profile = str(block.get("profile") or PROFILE_FULL).strip().casefold()
    if profile not in PROFILES:
        return GateVerdict(False, UNKNOWN_PROFILE,
                           f"profile {profile!r} is not one of {list(PROFILES)}",
                           version=version)

    required = FULL_REQUIRED_FIELDS if profile == PROFILE_FULL else FAST_FIX_REQUIRED_FIELDS
    missing = _missing(block, required)
    if missing:
        return GateVerdict(False, MISSING_FIELDS,
                           f"{profile} profile is incomplete",
                           profile=profile, version=version, missing_fields=missing)

    if profile == PROFILE_FULL:
        malformed, blocking = _check_assumptions(block.get("assumptions"))
        if malformed:
            return GateVerdict(
                False, MALFORMED_ASSUMPTION,
                "every assumption needs confidence, impact and resolution",
                profile=profile, version=version, missing_fields=malformed)
        if blocking:
            return GateVerdict(
                False, UNRESOLVED_HIGH_IMPACT_ASSUMPTION,
                "a high-impact assumption is still unresolved; resolve it or "
                "lower its impact with a stated reason",
                profile=profile, version=version, blocking_assumptions=blocking)

    return GateVerdict(True, PASS, f"{profile} profile complete",
                       profile=profile, version=version)
