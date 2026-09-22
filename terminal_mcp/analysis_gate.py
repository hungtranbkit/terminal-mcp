"""Analysis Gate -- "Understand First, Code Second" (docs/AI_ANALYSIS_GATE.md,
docs/REQUIREMENTS.md §20.6 Phase F).

The big sibling of `dor_gate.py`, and deliberately the SAME shape: a
pure, deterministic, disclosed-heuristic check (no ML, no LLM, no
network, never raises) that answers one question -- "has whoever filed
this task actually understood the problem, or are they about to code a
guess?" -- and returns READY / NEEDS_CLARIFICATION with a machine-
readable account of exactly what is missing.

Why a separate module from dor_gate.py rather than more fields on it:
DoR asks "is this task *filed* well enough to leave UNASSIGNED"
(title/acceptance_criteria/project/risk_level -- cheap, always
answerable at creation time). This gate asks the much stronger "is the
problem *understood* well enough to write code" -- evidence, invariants,
assumptions with their impact resolved, acceptance tests, a live
verification plan. Those are two genuinely different bars at two
different moments, and collapsing them would force every small task
through the heavy one.

BACKWARD COMPATIBILITY IS THE HARD CONSTRAINT HERE. This project's real
queue already holds tasks created long before this gate existed, and
§20.6 Phase A's own DoR note is explicit about why retroactively
requiring new fields on every task is a breaking change with no safety
value. So:

  * A task is gated ONLY when it resolves to a real profile -- either
    because it explicitly declares one (`analysis.profile` /
    `metadata.analysis_profile`) or because it declares a task class
    this gate maps to one (`metadata.task_class` / `metadata.type`).
  * Every other task -- i.e. every legacy row in the existing queue --
    resolves to PROFILE_NONE and reports READY, exactly as it did
    before this module existed. No migration backfill, no mass
    re-classification, no queue full of suddenly-BLOCKED work.
  * A project that wants the strict posture opts in with
    `AnalysisGatePolicy(require_classification=True)`, which flips
    UNCLASSIFIED from "not gated" to "must classify itself first".
    That is the intended rollout end-state, not the default, so that
    turning it on is a deliberate, reviewable act.
  * `enforcement="advisory"` runs every check and reports the result
    without ever blocking, so a lane can measure how much of its real
    backlog would fail before anyone flips it to "enforce".

The gate NEVER invents a value for a missing field and never downgrades
a HIGH-impact unresolved assumption into an assumed-fine one: UNKNOWN >
guess is the whole point of the feature, so "missing" is always
reported as missing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

READY = "READY"
NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"

#: Bumped whenever the REQUIRED FIELD SETS below change in a way that
#: could flip an existing task's verdict. Stamped into every result and
#: into any analysis object this module accepts, so a stored verdict is
#: always attributable to the rules that produced it (a task analysed
#: under v1 is never silently re-judged by v2's rules without the
#: version difference being visible).
GATE_VERSION = 1

PROFILE_NONE = "none"
PROFILE_FULL = "full"
PROFILE_FAST_FIX = "fast_fix"
ALL_PROFILES = (PROFILE_NONE, PROFILE_FULL, PROFILE_FAST_FIX)

ENFORCE = "enforce"
ADVISORY = "advisory"
OFF = "off"
ALL_ENFORCEMENTS = (ENFORCE, ADVISORY, OFF)

#: `metadata.task_class` (preferred) or the older `metadata.type` ->
#: profile. Only classes that really mean "someone is about to change
#: behaviour" map to a gated profile; `chore`/`docs`/`research`/
#: `incident` deliberately do not (an incident is triaged under time
#: pressure and already has its own §20.6 Phase B lane; forcing a full
#: Feature Contract onto it would be actively harmful).
TASK_CLASS_PROFILES: dict[str, str] = {
    "implementation": PROFILE_FULL,
    "feature": PROFILE_FULL,
    "fix": PROFILE_FULL,
    "bugfix": PROFILE_FULL,
    "refactor": PROFILE_FULL,
    "migration": PROFILE_FULL,
    "fast_fix": PROFILE_FAST_FIX,
    "hotfix": PROFILE_FAST_FIX,
}

#: The Feature Contract fields the FULL profile requires. Each is a real
#: question the doc spells out; none is derivable from the others, which
#: is why each is separately required rather than folded into one blob.
FULL_REQUIRED_FIELDS: tuple[str, ...] = (
    "problem_statement",       # REQUEST: what is actually wrong/wanted
    "user_observable_goal",    # expected behaviour a USER can observe
    "source_of_truth",         # which code/doc/runtime IS the authority
    "evidence",                # what was actually read/run to establish it
    "invariants",              # what must stay true (user-observable)
    "acceptance_tests",        # how we will know it works
    "live_verification",       # how it is proven on the real runtime
)

#: The Fast Fix profile: lighter, but never empty. A one-line fix still
#: has to prove it reproduced the bug, knows WHY, and cannot silently
#: break the thing it touched.
FAST_FIX_REQUIRED_FIELDS: tuple[str, ...] = (
    "reproduce",
    "root_cause",
    "expected_behavior",
    "invariant",
    "regression_test",
    "verify_fix",
)

#: Categories where a second, adversarial read is mandatory before any
#: code -- the places this project has repeatedly been burned: state
#: machines and workflows (transition tables), auth/security, deploy,
#: data models/migrations, multi-agent coordination, automation that
#: acts on its own, and anything destructive.
CRITIC_REQUIRED_CATEGORIES: tuple[str, ...] = (
    "state-machine", "workflow", "auth", "security", "deploy",
    "data-model", "multi-agent", "automation", "destructive",
)

VALID_CONFIDENCE = ("LOW", "MEDIUM", "HIGH")
VALID_IMPACT = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
#: Impacts high enough that an UNRESOLVED assumption blocks Implementation
#: Ready outright. This is the single most important rule in the feature:
#: a guess that cannot hurt much is allowed to stay a guess; one that can
#: is not.
BLOCKING_IMPACTS = ("HIGH", "CRITICAL")
RESOLVED = "RESOLVED"


@dataclass(frozen=True)
class AnalysisGatePolicy:
    """Per-project/per-lane policy. The defaults are the SAFE-FOR-AN-
    EXISTING-QUEUE ones (classification opt-in, enforcement on only for
    tasks that classified themselves), never the strictest ones."""

    enforcement: str = ENFORCE
    #: False (default): an unclassified task is simply not gated -- the
    #: legacy-compatibility escape hatch. True: an unclassified task is
    #: treated as PROFILE_FULL and must classify itself.
    require_classification: bool = False
    critic_required_categories: tuple[str, ...] = CRITIC_REQUIRED_CATEGORIES

    def __post_init__(self) -> None:
        if self.enforcement not in ALL_ENFORCEMENTS:
            raise ValueError(f"enforcement must be one of {ALL_ENFORCEMENTS}, got {self.enforcement!r}")


DEFAULT_POLICY = AnalysisGatePolicy()


def _is_present(value: Any) -> bool:
    """A field counts as declared only if it carries real content. An
    empty string/list/dict is exactly the "someone filled the shape in
    but said nothing" case this gate exists to catch, so it is missing,
    not present."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def extract_analysis(task: dict[str, Any]) -> dict[str, Any]:
    """The one read path for a task's analysis object, tolerant on
    purpose. Prefers the real `analysis` column (migration v9) and falls
    back to `metadata.analysis`, so a caller that stored its contract in
    metadata before the column existed -- and any node/tool still doing
    that -- keeps working unchanged."""
    analysis = task.get("analysis")
    if isinstance(analysis, dict) and analysis:
        return analysis
    metadata = task.get("metadata") or {}
    nested = metadata.get("analysis") if isinstance(metadata, dict) else None
    return nested if isinstance(nested, dict) else {}


def resolve_profile(task: dict[str, Any], *, policy: AnalysisGatePolicy = DEFAULT_POLICY) -> str:
    """Which profile this task is judged under. Explicit declaration
    always wins over inference -- a task that says what it is is never
    overridden by a guess from its `type`."""
    analysis = extract_analysis(task)
    metadata = task.get("metadata") or {}
    explicit = analysis.get("profile") or metadata.get("analysis_profile")
    if isinstance(explicit, str) and explicit.strip():
        candidate = explicit.strip().lower()
        # An unrecognised profile is NOT silently treated as "none" --
        # that would turn a typo into a bypass. It escalates to the
        # strict profile, and _check reports it as an invalid field.
        return candidate if candidate in ALL_PROFILES else PROFILE_FULL
    for key in ("task_class", "type"):
        declared = metadata.get(key)
        if isinstance(declared, str) and declared.strip().lower() in TASK_CLASS_PROFILES:
            return TASK_CLASS_PROFILES[declared.strip().lower()]
    # Unclassified: the legacy case. Not gated unless the project has
    # explicitly opted into requiring classification.
    return PROFILE_FULL if policy.require_classification else PROFILE_NONE


def _check_assumptions(analysis: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    """Returns (malformed-field complaints, blocking unresolved
    assumptions). An assumption with no impact declared is treated as
    BLOCKING, not as low-impact: "I didn't say how bad this could be" is
    itself an unresolved high-impact unknown (UNKNOWN > guess)."""
    problems: list[str] = []
    blocking: list[dict[str, Any]] = []
    raw = analysis.get("assumptions")
    if not _is_present(raw):
        return problems, blocking
    if not isinstance(raw, (list, tuple)):
        return [f"assumptions (must be a list of objects, got {type(raw).__name__})"], blocking
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            problems.append(f"assumptions[{index}] (must be an object with confidence/impact/resolution)")
            continue
        statement = item.get("statement") or item.get("assumption") or ""
        confidence = str(item.get("confidence") or "").upper()
        impact = str(item.get("impact") or "").upper()
        status = str(item.get("status") or "").upper()
        resolution = item.get("resolution")
        if not _is_present(statement):
            problems.append(f"assumptions[{index}].statement")
        if confidence not in VALID_CONFIDENCE:
            problems.append(f"assumptions[{index}].confidence (must be one of {VALID_CONFIDENCE})")
        if impact not in VALID_IMPACT:
            problems.append(f"assumptions[{index}].impact (must be one of {VALID_IMPACT})")
        # An assumption is resolved only by BOTH saying so and saying
        # how. A bare status=RESOLVED with no resolution text is the
        # "marked it done to get past the gate" failure mode.
        resolved = status == RESOLVED and _is_present(resolution)
        blocks = impact in BLOCKING_IMPACTS or impact not in VALID_IMPACT
        if blocks and not resolved:
            blocking.append({
                "index": index,
                "statement": statement if isinstance(statement, str) else str(statement),
                "confidence": confidence or None,
                "impact": impact or None,
                "status": status or "OPEN",
                "has_resolution": _is_present(resolution),
            })
    return problems, blocking


def _critic_categories(task: dict[str, Any], analysis: dict[str, Any],
                       policy: AnalysisGatePolicy) -> list[str]:
    """Which critic-required categories this task actually declares.
    Read from the analysis object first, then `metadata.categories` --
    never inferred from the prompt text, because a keyword match on free
    text would both miss real cases and fire on false ones, and this gate
    only ever acts on what a task explicitly declares."""
    declared: list[str] = []
    for source in (analysis.get("categories"), (task.get("metadata") or {}).get("categories")):
        if isinstance(source, (list, tuple)):
            declared.extend(str(item).strip().lower() for item in source)
        elif isinstance(source, str) and source.strip():
            declared.append(source.strip().lower())
    return [c for c in policy.critic_required_categories if c in declared]


def check_analysis_gate(task: dict[str, Any], *,
                        policy: AnalysisGatePolicy = DEFAULT_POLICY) -> dict[str, Any]:
    """`task` is a plain dict shaped like `QueueTask.to_dict()` (reads
    `analysis` and `metadata` only). Returns a result dict that is both
    machine-readable (`status`/`profile`/`missing_fields`/
    `unresolved_assumptions`/`critic_required_categories`/`gate_version`)
    and human-readable (`reason`). Never raises, never guesses."""
    profile = resolve_profile(task, policy=policy)
    analysis = extract_analysis(task)
    result: dict[str, Any] = {
        "gate_version": GATE_VERSION,
        "profile": profile,
        "enforcement": policy.enforcement,
        "missing_fields": [],
        "unresolved_assumptions": [],
        "critic_required_categories": [],
    }

    if policy.enforcement == OFF:
        return {**result, "status": READY, "enforced": False,
                "reason": "analysis gate disabled by policy (enforcement=off)"}

    if profile == PROFILE_NONE:
        # The legacy path. Explicitly named in the reason so that an
        # operator reading a Work detail never mistakes "not gated" for
        # "passed the gate".
        return {**result, "status": READY, "enforced": False,
                "reason": "task declares no implementation/fix class -- analysis gate not applicable "
                          "(legacy/unclassified task; set metadata.task_class to opt in)"}

    required = FULL_REQUIRED_FIELDS if profile == PROFILE_FULL else FAST_FIX_REQUIRED_FIELDS
    missing = [name for name in required if not _is_present(analysis.get(name))]

    declared_profile = analysis.get("profile") or (task.get("metadata") or {}).get("analysis_profile")
    if isinstance(declared_profile, str) and declared_profile.strip() \
            and declared_profile.strip().lower() not in ALL_PROFILES:
        missing.append(f"profile (must be one of {ALL_PROFILES}, got {declared_profile!r})")

    assumption_problems, blocking_assumptions = _check_assumptions(analysis)
    missing.extend(assumption_problems)

    # The FULL profile must declare its assumptions explicitly. "No
    # assumptions" is a legitimate answer, but it has to be SAID (an
    # empty list), because silence is indistinguishable from never
    # having looked.
    if profile == PROFILE_FULL and analysis.get("assumptions") is None:
        missing.append("assumptions (declare [] explicitly if there genuinely are none)")

    critic_categories = _critic_categories(task, analysis, policy)
    result["critic_required_categories"] = critic_categories
    if critic_categories and not _is_present(analysis.get("critic_result")):
        missing.append(f"critic_result (required for categories: {', '.join(critic_categories)})")

    result["missing_fields"] = missing
    result["unresolved_assumptions"] = blocking_assumptions

    if not missing and not blocking_assumptions:
        return {**result, "status": READY, "enforced": policy.enforcement == ENFORCE,
                "reason": f"analysis contract complete for profile {profile!r}"}

    parts: list[str] = []
    if missing:
        parts.append(f"missing/invalid: {', '.join(str(m) for m in missing)}")
    if blocking_assumptions:
        statements = "; ".join(
            f"{a['statement'] or '(no statement)'} [impact={a['impact'] or 'UNDECLARED'}]"
            for a in blocking_assumptions
        )
        parts.append(f"{len(blocking_assumptions)} unresolved high-impact assumption(s): {statements}")
    reason = f"analysis gate ({profile}): " + "; ".join(parts)

    if policy.enforcement == ADVISORY:
        # Reported in full, but never blocking -- the measurement mode a
        # lane uses before turning enforcement on.
        return {**result, "status": READY, "enforced": False, "advisory_status": NEEDS_CLARIFICATION,
                "reason": f"ADVISORY ONLY (not blocking) -- {reason}"}
    return {**result, "status": NEEDS_CLARIFICATION, "enforced": True, "reason": reason}
