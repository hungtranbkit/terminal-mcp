"""Implementation Contract v1 -- the Analysis Gate's data format, its
validation rules, and the coding-agent prompt built from it.

WHY THIS EXISTS. The expensive reasoning in this fleet is done once, by
the analysis agent (ChatGPT), and then thrown away: today a task carries
a free-text `prompt`, `queue_engine.build_dispatch_text` appends a
completion marker, and every coding agent that picks the task up
re-derives the same decisions from the same prose -- sometimes
differently, usually silently. The failure that costs real time is not a
coding agent that cannot code; it is a coding agent that GUESSED at a
high-impact decision the analysis never actually made, and got it wrong
in a way nobody notices until the behavior is live.

This module is the fix, and it is deliberately only half of one: it
defines a STRUCTURED contract (`ImplementationContract`), a deterministic
gate over it (`evaluate_contract`), and a prompt builder that REFERENCES
the contract and its context pack instead of restating it as more prose
(`build_contract_prompt`). It dispatches nothing, sends nothing, writes
no database rows and reads no telemetry. Wiring this into the queue
delivery gate, persisting its verdicts, measuring it, and drawing it on
the dashboard are four separate pieces of work owned elsewhere; the
interfaces they consume are the four public functions at the bottom of
this file plus `ContractGateResult.to_dict()`.

THE GATE IS DETERMINISTIC, NOT AN LLM CALL -- same posture, and for the
same reason, as `coordinator.py`'s own gate and `dor_gate.py`: "is this
HIGH-impact decision marked resolved" is mechanically checkable, and a
gate that is itself a guess cannot credibly refuse a guess. What this
gate cannot check is whether a declared decision is a GOOD one -- that
is what `critic_result` is for on HIGH_RISK work, and the critic is a
separate agent whose measurement contract is owned elsewhere. This
module only requires that a critic verdict be PRESENT and not FAIL; it
never scores, re-runs or second-guesses one.

THE DECISION BUDGET is the core idea, and it is a budget in the literal
sense: judgment is finite, so spend it where being wrong is expensive.
  * HIGH   -- must be RESOLVED before READY. An open HIGH decision is
              exactly the thing that must not be left to a coding agent
              at 2am, so the contract goes back for analysis
              (NEED_ANALYSIS), it does not dispatch with a shrug.
  * MEDIUM -- may stay open, but only if the analysis says what to do
              anyway: an explicit `default` AND a `guardrail` (what
              detects/bounds it being the wrong default). "Decide it
              later" without those two is not a budget, it is a deferral.
  * LOW    -- the coding agent chooses, always, and a LOW decision NEVER
              blocks READY no matter how many of them there are or how
              little detail they carry. This is load-bearing: a gate
              that punishes detail teaches agents to write less of it.

LEGACY COMPATIBILITY, three explicit layers (nothing retrofits onto the
~thousands of existing free-text tasks):
  1. No contract at all -> `SKIPPED`, never blocking. Same opt-in
     posture as `dor_gate.py`'s `metadata.dor_required`.
  2. A contract with `enforcement: "advisory"` (THE DEFAULT for v1) ->
     findings are computed and reported in full, `blocks_ready` is
     False. This is the rollout mode: real verdicts, zero refusals,
     so the finding rate can be measured before anything is enforced.
  3. `enforcement: "enforcing"` -> a blocking finding means
     NEED_ANALYSIS. Opted into per task/project, never globally here.
Version skew degrades LOUDLY, not silently: a contract declaring an
unknown `protocol` is `UNSUPPORTED_VERSION` and is never partially
validated against v1's rules, because a rule applied to a format it
does not describe produces a confident wrong answer.

DISCLOSED SCOPE CUTS (not silent omissions):
  * `context_pack` entries are validated for SHAPE only -- that a ref
    exists, is non-empty and names its kind. This module never opens a
    file, resolves a repo path or fetches a URL: a gate that touches the
    filesystem cannot be run on the analysis agent's side, where the
    contract is actually authored and where catching a bad ref is worth
    the most.
  * No field's CONTENT is judged for quality. "acceptance: it works" is
    present, and this gate passes it. Detecting a vacuous field is a
    reasoning task; that is the critic's job (HIGH_RISK) or a human's.
  * `live_verify` is required as a declaration at STANDARD/HIGH_RISK but
    never executed here -- running it is the delivery gate's job.
  * `stateful` is DECLARED, never detected. An analysis that forgets to
    set it on genuinely stateful work escapes the source-of-truth rule
    entirely, and nothing here can catch that -- deciding whether a
    change touches state is exactly the reasoning this gate refuses to
    fake. The mitigation is a `profile`/`risk_level` policy upstream
    (e.g. a project that defaults `stateful: true` for anything touching
    a migration/store path), not a heuristic in here.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

# --------------------------------------------------------------------------
# Protocol generation. A contract that does not name one is v0 legacy; a
# contract naming a generation this build does not implement is refused
# rather than guessed at.
# --------------------------------------------------------------------------
CONTRACT_PROTOCOL = "terminal-mcp-impl-contract/v1"
SUPPORTED_PROTOCOLS = (CONTRACT_PROTOCOL,)

# Gate verdicts.
READY = "READY"
NEED_ANALYSIS = "NEED_ANALYSIS"
SKIPPED = "SKIPPED"
UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
ALL_VERDICTS = (READY, NEED_ANALYSIS, SKIPPED, UNSUPPORTED_VERSION)

# Rollout modes (layer 2/3 above).
ADVISORY = "advisory"
ENFORCING = "enforcing"
ENFORCEMENT_MODES = (ADVISORY, ENFORCING)
DEFAULT_ENFORCEMENT = ADVISORY

# Work profiles.
FAST_FIX = "FAST_FIX"
STANDARD = "STANDARD"
HIGH_RISK = "HIGH_RISK"
PROFILES = (FAST_FIX, STANDARD, HIGH_RISK)
DEFAULT_PROFILE = STANDARD

# The decision budget's three tiers, and the assumption confidence scale.
HIGH = "HIGH"
MEDIUM = "MEDIUM"
LOW = "LOW"
IMPACT_LEVELS = (HIGH, MEDIUM, LOW)
CONFIDENCE_LEVELS = (HIGH, MEDIUM, LOW)

RESOLVED = "RESOLVED"
OPEN = "OPEN"
DECISION_STATUSES = (RESOLVED, OPEN)

# Critic verdicts this module understands. Only FAIL is treated as a
# blocking answer -- PASS_WITH_FINDINGS deliberately does NOT block,
# because a critic that can veto by listing nitpicks stops being read.
CRITIC_PASS = "PASS"
CRITIC_PASS_WITH_FINDINGS = "PASS_WITH_FINDINGS"
CRITIC_FAIL = "FAIL"
CRITIC_VERDICTS = (CRITIC_PASS, CRITIC_PASS_WITH_FINDINGS, CRITIC_FAIL)

# Fields required per profile before a contract may be READY. FAST_FIX is
# the "minimal gate" -- a one-line fix must not need a state model and an
# out-of-scope list to move, or nobody will write a contract for one.
REQUIRED_FIELDS_BY_PROFILE: dict[str, tuple[str, ...]] = {
    FAST_FIX: ("goal", "expected_behavior", "acceptance"),
    STANDARD: ("goal", "current_behavior", "expected_behavior", "invariants",
               "edge_cases", "dangerous_failure_modes", "acceptance", "live_verify",
               "out_of_scope"),
    HIGH_RISK: ("goal", "current_behavior", "expected_behavior", "invariants",
                "edge_cases", "dangerous_failure_modes", "acceptance", "live_verify",
                "out_of_scope", "context_pack"),
}

# Every field a v1 contract may carry. Anything else is reported as an
# unknown field (advisory only) rather than dropped in silence -- a typo'd
# `acceptance_criteria` that vanishes is how a gate passes an empty task.
CONTRACT_FIELDS = (
    "protocol", "profile", "enforcement", "stateful",
    "goal", "current_behavior", "expected_behavior",
    "source_of_truth", "state_model", "invariants", "assumptions",
    "edge_cases", "dangerous_failure_modes", "acceptance", "live_verify",
    "out_of_scope", "context_pack", "decision_budget", "critic_result",
)

# Finding codes. Stable strings: telemetry groups by these, so they are
# part of this module's public interface and do not get reworded.
UNRESOLVED_HIGH_DECISION = "UNRESOLVED_HIGH_DECISION"
MEDIUM_DECISION_WITHOUT_DEFAULT = "MEDIUM_DECISION_WITHOUT_DEFAULT"
MEDIUM_DECISION_WITHOUT_GUARDRAIL = "MEDIUM_DECISION_WITHOUT_GUARDRAIL"
HIGH_IMPACT_LOW_CONFIDENCE_ASSUMPTION = "HIGH_IMPACT_LOW_CONFIDENCE_ASSUMPTION"
HIGH_IMPACT_UNGUARDED_ASSUMPTION = "HIGH_IMPACT_UNGUARDED_ASSUMPTION"
MISSING_SOURCE_OF_TRUTH = "MISSING_SOURCE_OF_TRUTH"
MISSING_STATE_MODEL = "MISSING_STATE_MODEL"
MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
MISSING_CRITIC_RESULT = "MISSING_CRITIC_RESULT"
CRITIC_VERDICT_FAIL = "CRITIC_VERDICT_FAIL"
MALFORMED_ENTRY = "MALFORMED_ENTRY"
UNKNOWN_FIELD = "UNKNOWN_FIELD"
UNKNOWN_PROFILE = "UNKNOWN_PROFILE"
LOW_DECISION_DELEGATED = "LOW_DECISION_DELEGATED"


class ContractFinding(dict):
    """A single gate finding. A plain dict subclass on purpose: every
    consumer here (MCP tool payloads, the event bus, telemetry rows) has
    to JSON-serialise it, and a dataclass would only add a `.to_dict()`
    everyone immediately calls."""

    def __init__(self, code: str, *, blocking: bool, detail: str, field: str = "",
                 ref: str = "") -> None:
        super().__init__(code=code, blocking=blocking, detail=detail, field=field, ref=ref)

    @property
    def code(self) -> str:
        return self["code"]

    @property
    def blocking(self) -> bool:
        return self["blocking"]


class ContractGateResult(dict):
    """The Analysis Gate's verdict. `verdict` is what a caller acts on;
    `blocks_ready` is the ONLY field a delivery gate should branch on,
    because it already folds in the advisory/enforcing rollout mode --
    a caller that re-derives "did anything block" from `findings` will
    silently start refusing work the moment advisory mode is on."""

    def __init__(self, *, verdict: str, blocks_ready: bool, enforcement: str,
                 profile: str, findings: list[ContractFinding], reason: str,
                 contract_digest: str = "") -> None:
        super().__init__(verdict=verdict, blocks_ready=blocks_ready,
                         enforcement=enforcement, profile=profile,
                         findings=list(findings), reason=reason,
                         contract_digest=contract_digest,
                         protocol=CONTRACT_PROTOCOL)

    @property
    def verdict(self) -> str:
        return self["verdict"]

    @property
    def blocks_ready(self) -> bool:
        return self["blocks_ready"]

    @property
    def findings(self) -> list[ContractFinding]:
        return self["findings"]

    def blocking_findings(self) -> list[ContractFinding]:
        return [f for f in self["findings"] if f["blocking"]]

    def to_dict(self) -> dict[str, Any]:
        """Explicit, so this stays a stable interface even if the base
        class ever stops being a dict."""
        return dict(self)


def _non_empty(value: Any) -> bool:
    """Present-and-meaningful. A whitespace-only string, an empty list and
    an empty dict are all "not declared" -- but `False` and `0` are real
    declared values and are never treated as missing."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True


def contract_digest(contract: dict[str, Any]) -> str:
    """Stable short digest of a contract's declared content, for tying a
    dispatched prompt / a telemetry row / a NEED_ANALYSIS report back to
    the EXACT contract text that produced it. Deliberately order- and
    whitespace-insensitive at the top level only: re-serialising the same
    contract must not look like a revision, but genuinely editing a field
    must."""
    parts = []
    for key in sorted(contract):
        parts.append(f"{key}={_stable_repr(contract[key])}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _stable_repr(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ",".join(f"{k}:{_stable_repr(value[k])}" for k in sorted(value)) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_stable_repr(v) for v in value) + "]"
    if isinstance(value, str):
        return value.strip()
    return repr(value)


# --------------------------------------------------------------------------
# Decision budget
# --------------------------------------------------------------------------
def check_decision_budget(decisions: Any) -> list[ContractFinding]:
    """The budget rules, in one place so telemetry can call them without
    evaluating a whole contract.

    HIGH   open            -> blocking. The whole point of the gate.
    MEDIUM open            -> blocking unless BOTH `default` and
                              `guardrail` are declared. Reported as two
                              distinct codes so "analysis keeps skipping
                              guardrails" is separable from "analysis
                              keeps skipping defaults" in the data.
    LOW    open or missing -> NEVER blocking, at any profile. Emitted as
                              a non-blocking LOW_DECISION_DELEGATED note
                              so the coding agent's prompt can list what
                              it is explicitly allowed to choose.
    A RESOLVED decision of any impact needs a `decision` -- "RESOLVED"
    with no answer recorded is the same guess this gate exists to stop,
    wearing a status field."""
    findings: list[ContractFinding] = []
    if not _non_empty(decisions):
        return findings
    if not isinstance(decisions, (list, tuple)):
        return [ContractFinding(MALFORMED_ENTRY, blocking=True,
                                detail="decision_budget must be a list of decision objects",
                                field="decision_budget")]
    for index, entry in enumerate(decisions):
        ref = f"decision_budget[{index}]"
        if not isinstance(entry, dict):
            findings.append(ContractFinding(
                MALFORMED_ENTRY, blocking=True, field="decision_budget", ref=ref,
                detail="each decision must be an object with id/question/impact/status"))
            continue
        ref = str(entry.get("id") or ref)
        impact = str(entry.get("impact") or "").upper()
        status = str(entry.get("status") or OPEN).upper()
        question = str(entry.get("question") or "").strip()

        if impact not in IMPACT_LEVELS:
            # An undeclared impact is NOT quietly demoted to LOW: the
            # cheapest way to defeat this gate would be to leave the
            # field off, so an unreadable impact is treated as HIGH.
            findings.append(ContractFinding(
                MALFORMED_ENTRY, blocking=True, field="decision_budget", ref=ref,
                detail=(f"impact must be one of {IMPACT_LEVELS}, got {entry.get('impact')!r} -- "
                        "an undeclared impact is treated as HIGH, never as LOW")))
            continue

        if impact == LOW:
            findings.append(ContractFinding(
                LOW_DECISION_DELEGATED, blocking=False, field="decision_budget", ref=ref,
                detail=f"LOW-impact decision left to the coding agent: {question or ref}"))
            continue

        if status == RESOLVED:
            if not _non_empty(entry.get("decision")):
                findings.append(ContractFinding(
                    MALFORMED_ENTRY, blocking=True, field="decision_budget", ref=ref,
                    detail="status=RESOLVED but no `decision` recorded"))
            continue

        if impact == HIGH:
            findings.append(ContractFinding(
                UNRESOLVED_HIGH_DECISION, blocking=True, field="decision_budget", ref=ref,
                detail=(f"HIGH-impact decision is still {status}: {question or ref}. "
                        "A HIGH-impact decision is resolved by analysis, never by the coding agent.")))
            continue

        # MEDIUM, open: allowed, but only as a real deferral.
        if not _non_empty(entry.get("default")):
            findings.append(ContractFinding(
                MEDIUM_DECISION_WITHOUT_DEFAULT, blocking=True, field="decision_budget", ref=ref,
                detail=f"MEDIUM-impact decision is open with no explicit `default`: {question or ref}"))
        if not _non_empty(entry.get("guardrail")):
            findings.append(ContractFinding(
                MEDIUM_DECISION_WITHOUT_GUARDRAIL, blocking=True, field="decision_budget", ref=ref,
                detail=(f"MEDIUM-impact decision is open with no `guardrail` (what detects or bounds "
                        f"the default being wrong): {question or ref}")))
    return findings


def check_assumptions(assumptions: Any) -> list[ContractFinding]:
    """An assumption is a decision the analysis made without checking, so
    it is budgeted the same way -- by IMPACT, not by how confident the
    prose sounds.

    impact HIGH + confidence LOW    -> blocking, always. This is a guess
                                       about something expensive, which
                                       is the exact failure mode.
    impact HIGH + confidence MEDIUM -> blocking UNLESS a `guardrail`
                                       declares how a wrong assumption is
                                       detected or bounded. Either you
                                       are sure, or the blast radius is.
    impact HIGH + confidence HIGH   -> fine.
    impact MEDIUM/LOW               -> never blocking, whatever the
                                       confidence.
    As with decisions, an unreadable impact is treated as HIGH."""
    findings: list[ContractFinding] = []
    if not _non_empty(assumptions):
        return findings
    if not isinstance(assumptions, (list, tuple)):
        return [ContractFinding(MALFORMED_ENTRY, blocking=True,
                                detail="assumptions must be a list of assumption objects",
                                field="assumptions")]
    for index, entry in enumerate(assumptions):
        ref = f"assumptions[{index}]"
        if not isinstance(entry, dict):
            findings.append(ContractFinding(
                MALFORMED_ENTRY, blocking=True, field="assumptions", ref=ref,
                detail="each assumption must be an object with statement/confidence/impact"))
            continue
        statement = str(entry.get("statement") or "").strip()
        ref = str(entry.get("id") or ref)
        impact = str(entry.get("impact") or "").upper()
        confidence = str(entry.get("confidence") or "").upper()

        if impact not in IMPACT_LEVELS or confidence not in CONFIDENCE_LEVELS:
            findings.append(ContractFinding(
                MALFORMED_ENTRY, blocking=True, field="assumptions", ref=ref,
                detail=(f"assumption needs impact in {IMPACT_LEVELS} and confidence in "
                        f"{CONFIDENCE_LEVELS}, got impact={entry.get('impact')!r} "
                        f"confidence={entry.get('confidence')!r} -- an undeclared impact is "
                        "treated as HIGH, never as LOW")))
            continue
        if impact != HIGH or confidence == HIGH:
            continue
        if confidence == LOW:
            findings.append(ContractFinding(
                HIGH_IMPACT_LOW_CONFIDENCE_ASSUMPTION, blocking=True, field="assumptions", ref=ref,
                detail=(f"HIGH-impact assumption held with LOW confidence: {statement or ref}. "
                        "Verify it during analysis or convert it to a HIGH decision and resolve it.")))
        elif not _non_empty(entry.get("guardrail")):
            findings.append(ContractFinding(
                HIGH_IMPACT_UNGUARDED_ASSUMPTION, blocking=True, field="assumptions", ref=ref,
                detail=(f"HIGH-impact assumption held with MEDIUM confidence and no `guardrail`: "
                        f"{statement or ref}. Declare how a wrong assumption is detected or bounded.")))
    return findings


def check_state_declarations(contract: dict[str, Any], *, profile: str) -> list[ContractFinding]:
    """A task that changes state and does not say WHERE the truth lives is
    the highest-yield thing this gate catches: two stores that disagree is
    a class of bug no amount of careful coding prevents, because the
    coding agent picks whichever store it read first.

    `source_of_truth` is required for a stateful task at EVERY profile,
    FAST_FIX included -- a one-line fix to the wrong store is still a
    write to the wrong store. `state_model` is required at STANDARD and
    HIGH_RISK only; a FAST_FIX that names its store does not also owe a
    state diagram."""
    findings: list[ContractFinding] = []
    if not contract.get("stateful"):
        return findings
    if not _non_empty(contract.get("source_of_truth")):
        findings.append(ContractFinding(
            MISSING_SOURCE_OF_TRUTH, blocking=True, field="source_of_truth",
            detail=("stateful: true but no `source_of_truth` declared -- name the one store/table/"
                    "file that is authoritative, and what reads derive from it")))
    if profile in (STANDARD, HIGH_RISK) and not _non_empty(contract.get("state_model")):
        findings.append(ContractFinding(
            MISSING_STATE_MODEL, blocking=True, field="state_model",
            detail="stateful: true but no `state_model` declared (states, transitions, who may write)"))
    return findings


def check_critic(contract: dict[str, Any], *, profile: str) -> list[ContractFinding]:
    """HIGH_RISK work requires an independent critic to have looked at the
    contract. This module checks only that a verdict is PRESENT, well-
    formed, and not FAIL -- what a critic measures, and how well, is a
    separate contract owned elsewhere. PASS_WITH_FINDINGS does not block
    on purpose (see CRITIC_VERDICTS above)."""
    if profile != HIGH_RISK:
        return []
    critic = contract.get("critic_result")
    if not _non_empty(critic):
        return [ContractFinding(
            MISSING_CRITIC_RESULT, blocking=True, field="critic_result",
            detail=f"profile={HIGH_RISK} requires a `critic_result` from an independent critic pass")]
    if not isinstance(critic, dict):
        return [ContractFinding(
            MALFORMED_ENTRY, blocking=True, field="critic_result",
            detail="critic_result must be an object carrying at least `verdict`")]
    verdict = str(critic.get("verdict") or "").upper()
    if verdict not in CRITIC_VERDICTS:
        return [ContractFinding(
            MALFORMED_ENTRY, blocking=True, field="critic_result",
            detail=f"critic_result.verdict must be one of {CRITIC_VERDICTS}, got {critic.get('verdict')!r}")]
    if verdict == CRITIC_FAIL:
        return [ContractFinding(
            CRITIC_VERDICT_FAIL, blocking=True, field="critic_result",
            detail=f"critic returned FAIL: {str(critic.get('summary') or '').strip() or '(no summary)'}")]
    return []


def check_required_fields(contract: dict[str, Any], *, profile: str) -> list[ContractFinding]:
    """Per-profile presence check. Presence only -- never quality; see the
    module docstring's disclosed scope cuts."""
    findings: list[ContractFinding] = []
    for name in REQUIRED_FIELDS_BY_PROFILE.get(profile, REQUIRED_FIELDS_BY_PROFILE[STANDARD]):
        if not _non_empty(contract.get(name)):
            findings.append(ContractFinding(
                MISSING_REQUIRED_FIELD, blocking=True, field=name,
                detail=f"profile={profile} requires `{name}` to be declared"))
    for name in contract:
        if name not in CONTRACT_FIELDS:
            findings.append(ContractFinding(
                UNKNOWN_FIELD, blocking=False, field=name,
                detail=(f"`{name}` is not a v1 contract field and is carried through unvalidated "
                        "(check for a typo -- a misspelled field is not a declared one)")))
    return findings


# --------------------------------------------------------------------------
# The Analysis Gate itself
# --------------------------------------------------------------------------
def extract_contract(task: Any) -> dict[str, Any] | None:
    """Pull a contract off a task dict / QueueTask-shaped object. Returns
    None when the task carries none -- the legacy path, which is every
    task created before this module existed."""
    metadata: Any = None
    if isinstance(task, dict):
        metadata = task.get("metadata")
    else:
        metadata = getattr(task, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    contract = metadata.get("impl_contract")
    return contract if isinstance(contract, dict) else None


def evaluate_contract(contract: dict[str, Any] | None, *,
                      enforcement: str | None = None) -> ContractGateResult:
    """THE public gate. Deterministic, side-effect free, never raises on
    malformed input -- a contract this module cannot parse comes back as
    findings, because a gate that throws is a gate someone wraps in a
    bare `except` and ignores.

    `enforcement` overrides the contract's own declared mode; a caller
    (a project policy, a rollout flag) passes it to enforce a contract
    that still declares itself advisory. Precedence is caller > contract
    > DEFAULT_ENFORCEMENT (advisory), and an unreadable value falls back
    to advisory rather than to enforcing: a typo must not start refusing
    work."""
    if contract is None or not _non_empty(contract):
        return ContractGateResult(
            verdict=SKIPPED, blocks_ready=False, enforcement=ADVISORY, profile="",
            findings=[], reason="no implementation contract on this task (legacy free-text task)")
    if not isinstance(contract, dict):
        return ContractGateResult(
            verdict=UNSUPPORTED_VERSION, blocks_ready=False, enforcement=ADVISORY, profile="",
            findings=[ContractFinding(MALFORMED_ENTRY, blocking=True,
                                      detail="contract must be an object")],
            reason="contract is not an object")

    mode = str(enforcement or contract.get("enforcement") or DEFAULT_ENFORCEMENT).lower()
    if mode not in ENFORCEMENT_MODES:
        mode = ADVISORY
    digest = contract_digest(contract)

    protocol = str(contract.get("protocol") or "").strip()
    if protocol not in SUPPORTED_PROTOCOLS:
        # Loud, not partial. v1's rules are NOT applied to a format that
        # does not claim to be v1 -- see the module docstring.
        return ContractGateResult(
            verdict=UNSUPPORTED_VERSION, blocks_ready=(mode == ENFORCING), enforcement=mode,
            profile="", contract_digest=digest,
            findings=[ContractFinding(
                MALFORMED_ENTRY, blocking=True, field="protocol",
                detail=(f"contract declares protocol={protocol or '(none)'}; this build implements "
                        f"{SUPPORTED_PROTOCOLS}. Not validated against v1's rules."))],
            reason=f"unsupported contract protocol {protocol or '(none)'}")

    profile = str(contract.get("profile") or DEFAULT_PROFILE).upper()
    findings: list[ContractFinding] = []
    if profile not in PROFILES:
        findings.append(ContractFinding(
            UNKNOWN_PROFILE, blocking=True, field="profile",
            detail=(f"profile must be one of {PROFILES}, got {contract.get('profile')!r} -- "
                    f"validated against {STANDARD}'s rules, never against the loosest profile")))
        profile = STANDARD

    findings.extend(check_required_fields(contract, profile=profile))
    findings.extend(check_state_declarations(contract, profile=profile))
    findings.extend(check_decision_budget(contract.get("decision_budget")))
    findings.extend(check_assumptions(contract.get("assumptions")))
    findings.extend(check_critic(contract, profile=profile))
    findings.extend(_check_context_pack(contract.get("context_pack")))

    blocking = [f for f in findings if f["blocking"]]
    if not blocking:
        return ContractGateResult(
            verdict=READY, blocks_ready=False, enforcement=mode, profile=profile,
            findings=findings, contract_digest=digest,
            reason=f"contract complete for profile={profile}")

    reason = (f"{len(blocking)} blocking finding(s) for profile={profile}: "
              + ", ".join(sorted({f['code'] for f in blocking})))
    if mode == ADVISORY:
        # Rollout layer 2: real verdict, no refusal. `verdict` still says
        # NEED_ANALYSIS so the data is honest about what WOULD have
        # happened; `blocks_ready` is the field callers branch on.
        return ContractGateResult(
            verdict=NEED_ANALYSIS, blocks_ready=False, enforcement=ADVISORY, profile=profile,
            findings=findings, contract_digest=digest,
            reason=f"ADVISORY (not blocking): {reason}")
    return ContractGateResult(
        verdict=NEED_ANALYSIS, blocks_ready=True, enforcement=ENFORCING, profile=profile,
        findings=findings, contract_digest=digest, reason=reason)


def _check_context_pack(pack: Any) -> list[ContractFinding]:
    """Shape only -- this module never resolves a ref. See the module
    docstring's disclosed scope cuts for why."""
    findings: list[ContractFinding] = []
    if not _non_empty(pack):
        return findings
    if not isinstance(pack, (list, tuple)):
        return [ContractFinding(MALFORMED_ENTRY, blocking=True, field="context_pack",
                                detail="context_pack must be a list of {ref, kind} objects")]
    for index, entry in enumerate(pack):
        ref = f"context_pack[{index}]"
        if isinstance(entry, str):
            if not entry.strip():
                findings.append(ContractFinding(MALFORMED_ENTRY, blocking=True,
                                                field="context_pack", ref=ref,
                                                detail="empty context_pack ref"))
            continue
        if not isinstance(entry, dict) or not _non_empty(entry.get("ref")):
            findings.append(ContractFinding(
                MALFORMED_ENTRY, blocking=True, field="context_pack", ref=ref,
                detail="each context_pack entry must be a non-empty string or an object with `ref`"))
            continue
        if not _non_empty(entry.get("kind")):
            findings.append(ContractFinding(
                MALFORMED_ENTRY, blocking=False, field="context_pack",
                ref=str(entry.get("ref")),
                detail="context_pack entry has no `kind` (file/doc/url/commit/test/decision-log)"))
    return findings


# --------------------------------------------------------------------------
# NEED_ANALYSIS: the coding agent's own escape hatch
# --------------------------------------------------------------------------
NEED_ANALYSIS_PROTOCOL = "terminal-mcp-need-analysis/v1"
NEED_ANALYSIS_MARKER_RE = re.compile(
    r"###TERMINAL_MCP_NEED_ANALYSIS\s+protocol=terminal-mcp-need-analysis/v1\s+([^#]*?)###"
)
_MARKER_FIELD_RE = re.compile(r"(\w+)=(\S+)")
NEED_ANALYSIS_REQUIRED_FIELDS = ("task_id", "status", "impact")
"""Same marker shape, and the same parsing posture, as `status.py`'s
completion marker -- deliberately, so the two protocols are read by the
same kind of code and an agent that already knows one knows the other.
The `question` is NOT carried in the marker (it cannot survive
`\\S+` field parsing); the marker announces that analysis is needed and
points at a decision id, the prose goes in the agent's normal output
right above it."""


def parse_need_analysis_marker(output: str) -> dict[str, str] | None:
    """Parse the LAST well-formed NEED_ANALYSIS marker in `output`, if
    any. An ambiguous or incomplete marker is the same as no marker --
    never partially trusted."""
    matches = NEED_ANALYSIS_MARKER_RE.findall(output)
    if not matches:
        return None
    fields = dict(_MARKER_FIELD_RE.findall(matches[-1]))
    if not all(name in fields for name in NEED_ANALYSIS_REQUIRED_FIELDS):
        return None
    if fields.get("status") != "need_analysis":
        return None
    if fields.get("impact", "").upper() not in IMPACT_LEVELS:
        return None
    return fields


def build_need_analysis_marker(*, task_id: str, attempt: int, nonce: str,
                               impact: str = HIGH, decision_id: str = "") -> str:
    """The exact line an agent is told to print. Built here rather than
    written out in the prompt template so the emitter and the parser can
    never drift apart."""
    decision = re.sub(r"\s+", "_", (decision_id or "unspecified").strip()) or "unspecified"
    return (f"###TERMINAL_MCP_NEED_ANALYSIS protocol={NEED_ANALYSIS_PROTOCOL} "
            f"task_id={task_id} attempt={attempt} nonce={nonce} "
            f"status=need_analysis impact={impact} decision_id={decision}###")


# --------------------------------------------------------------------------
# Prompt packaging
# --------------------------------------------------------------------------
_FIELD_CHAR_BUDGET = 600
"""Per-field cap in the dispatched prompt. The whole premise of this
module is that the analysis is READ FROM the contract, not re-narrated
into a prompt: a field long enough to hit this cap is prose the agent
should open the contract for, and the prompt says exactly that instead
of pasting 4KB of it."""


def _bullets(value: Any, *, budget: int = _FIELD_CHAR_BUDGET) -> str:
    if isinstance(value, dict):
        items = [f"{k}: {v}" for k, v in value.items()]
    elif isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    else:
        items = [str(value)]
    lines, used, dropped = [], 0, 0
    for item in items:
        text = _one_line(item, budget=budget)
        if used + len(text) > budget and lines:
            dropped += 1
            continue
        lines.append(f"  - {text}")
        used += len(text)
    if dropped:
        lines.append(f"  - (+{dropped} more -- read the full field in the contract)")
    return "\n".join(lines)


def _one_line(value: Any, *, budget: int = _FIELD_CHAR_BUDGET) -> str:
    text = " ".join(str(value).split())
    if len(text) <= budget:
        return text
    return text[:budget].rstrip() + " ... (truncated -- read the full field in the contract)"


def build_contract_prompt(contract: dict[str, Any], *, task_id: str, attempt: int,
                          nonce: str, contract_ref: str = "") -> str:
    """Build the coding agent's prompt FROM a contract.

    This is the packaging half of the module and it has one rule: the
    prompt REFERENCES the contract and its context pack, it does not
    restate them. Concretely it carries (a) the goal and the behavior
    delta, (b) the parts of the decision budget the agent must ACT on --
    MEDIUM defaults with their guardrails, and the LOW decisions it is
    explicitly allowed to choose, (c) the invariants and dangerous
    failure modes, (d) acceptance + live_verify + out_of_scope, and (e)
    the NEED_ANALYSIS escape hatch. Everything else -- current behavior
    at length, the state model, resolved HIGH decisions and their
    rationale, the assumptions ledger -- is NAMED and pointed at, never
    pasted, and every field is capped at `_FIELD_CHAR_BUDGET`.

    `contract_ref` is how the agent fetches the full contract (a tool
    call, a file path, a task id -- this module does not care and does
    not resolve it). It is included verbatim; when empty the prompt says
    the contract is on the task's own metadata, which is always true.

    Deliberately NOT included: the completion marker. `queue_engine.
    build_dispatch_text` already appends that to every dispatched task
    and duplicating it here would put two markers in one prompt."""
    profile = str(contract.get("profile") or DEFAULT_PROFILE).upper()
    digest = contract_digest(contract)
    where = contract_ref.strip() or "this task's own `metadata.impl_contract`"

    out: list[str] = []
    out.append(f"IMPLEMENTATION CONTRACT {digest} (protocol={CONTRACT_PROTOCOL}, profile={profile})")
    out.append(f"Full contract: {where}. Read it before you start; this prompt is a pointer to it, "
               f"not a replacement for it.")
    out.append("")
    out.append(f"GOAL: {_one_line(contract.get('goal') or '(not declared)')}")
    if _non_empty(contract.get("current_behavior")):
        out.append(f"CURRENT: {_one_line(contract['current_behavior'])}")
    if _non_empty(contract.get("expected_behavior")):
        out.append(f"EXPECTED: {_one_line(contract['expected_behavior'])}")

    if contract.get("stateful"):
        out.append("")
        out.append(f"SOURCE OF TRUTH (authoritative -- do not write a second store):\n"
                   f"{_bullets(contract.get('source_of_truth') or '(not declared)')}")
        if _non_empty(contract.get("state_model")):
            out.append("STATE MODEL: declared in the contract -- read it there before changing "
                       "any transition.")

    for label, key in (("INVARIANTS (must hold after your change)", "invariants"),
                       ("DANGEROUS FAILURE MODES (what must not happen, even once)",
                        "dangerous_failure_modes"),
                       ("EDGE CASES", "edge_cases")):
        if _non_empty(contract.get(key)):
            out.append("")
            out.append(f"{label}:\n{_bullets(contract[key])}")

    out.append("")
    out.append(_render_decision_budget(contract.get("decision_budget")))

    guarded = [a for a in (contract.get("assumptions") or [])
               if isinstance(a, dict) and str(a.get("impact", "")).upper() == HIGH]
    if guarded:
        out.append("")
        out.append("HIGH-IMPACT ASSUMPTIONS the analysis is relying on -- if you observe any of "
                   "these to be FALSE, stop and return NEED_ANALYSIS:\n"
                   + _bullets([f"{a.get('statement')} [confidence={a.get('confidence')}]"
                               + (f" guardrail: {a.get('guardrail')}" if a.get("guardrail") else "")
                               for a in guarded]))

    for label, key in (("ACCEPTANCE (this is what 'done' means)", "acceptance"),
                       ("LIVE VERIFY (run this for real, not just the unit tests)", "live_verify"),
                       ("OUT OF SCOPE (do not do these, even if they look easy)", "out_of_scope")):
        if _non_empty(contract.get(key)):
            out.append("")
            out.append(f"{label}:\n{_bullets(contract[key])}")

    if _non_empty(contract.get("context_pack")):
        out.append("")
        out.append("CONTEXT PACK -- read these instead of searching the repo from scratch:\n"
                   + _bullets(_context_pack_lines(contract["context_pack"]), budget=1200))

    out.append("")
    out.append(_need_analysis_instruction(task_id=task_id, attempt=attempt, nonce=nonce))
    return "\n".join(out)


def _context_pack_lines(pack: Any) -> list[str]:
    lines = []
    for entry in pack if isinstance(pack, (list, tuple)) else [pack]:
        if isinstance(entry, dict):
            ref, kind, why = entry.get("ref"), entry.get("kind"), entry.get("why")
            lines.append(f"[{kind or 'ref'}] {ref}" + (f" -- {why}" if why else ""))
        else:
            lines.append(str(entry))
    return lines


def _render_decision_budget(decisions: Any) -> str:
    """The three tiers, rendered as three different INSTRUCTIONS rather
    than one list -- the agent needs to do something different with each,
    and a flat list of "decisions" is exactly the prose it would have to
    reinterpret."""
    resolved, deferred, delegated, unresolved = [], [], [], []
    for entry in decisions if isinstance(decisions, (list, tuple)) else []:
        if not isinstance(entry, dict):
            continue
        impact = str(entry.get("impact") or "").upper()
        status = str(entry.get("status") or OPEN).upper()
        ident = str(entry.get("id") or "").strip()
        question = " ".join(str(entry.get("question") or "").split())
        prefix = f"[{ident}] " if ident else ""
        if status == RESOLVED:
            resolved.append(f"{prefix}{question} -> {entry.get('decision')}")
        elif impact == LOW:
            delegated.append(f"{prefix}{question}"
                             + (f" (suggestion, not binding: {entry.get('default')})"
                                if entry.get("default") else ""))
        elif _non_empty(entry.get("default")):
            deferred.append(f"{prefix}{question} -> default: {entry.get('default')} "
                            f"| guardrail: {entry.get('guardrail') or '(none declared)'}")
        else:
            # Only reachable in advisory mode (enforcing would have blocked
            # this contract). Saying "default: None" would read as an
            # instruction to use None; the true thing is that nobody answered.
            unresolved.append(f"{prefix}{question}")

    blocks = ["DECISION BUDGET"]
    if resolved:
        blocks.append("Already decided by analysis -- implement as written, do not re-litigate:\n"
                      + _bullets(resolved))
    if deferred:
        blocks.append("Deferred with an explicit default -- use the default, and implement the "
                      "guardrail with it:\n" + _bullets(deferred))
    if delegated:
        blocks.append("Yours to choose -- pick sensibly and move on, do not ask:\n"
                      + _bullets(delegated))
    if unresolved:
        blocks.append("UNRESOLVED and NOT low-impact -- the analysis did not answer these. Do not "
                      "pick one: if the work actually depends on it, return NEED_ANALYSIS:\n"
                      + _bullets(unresolved))
    if not (resolved or deferred or delegated or unresolved):
        blocks.append("  (no decisions recorded on this contract)")
    return "\n".join(blocks)


def _need_analysis_instruction(*, task_id: str, attempt: int, nonce: str) -> str:
    """The escape hatch, stated as the CHEAPER option. An agent will only
    stop instead of guessing if stopping is obviously allowed and
    obviously not a failure, so this says so in as many words."""
    return (
        "---\n"
        "IF YOU HIT SOMETHING HIGH-IMPACT THIS CONTRACT DOES NOT SPECIFY -- an unspecified "
        "behavior that changes stored data, a contract/API shape nothing declares, a state "
        "transition the state model does not cover, a security or permission question, or any "
        "answer you would be guessing at where being wrong is expensive -- do NOT guess and do "
        "NOT pick the option that is easiest to implement.\n"
        "Stop, leave the tree in a state you can describe, write a short note saying exactly "
        "what is unspecified and what the candidate answers are, and print this line (once):\n"
        f"{build_need_analysis_marker(task_id=task_id, attempt=attempt, nonce=nonce, impact=HIGH)}\n"
        "Replace decision_id with the contract's decision id if one covers it, or a short "
        "underscored slug if not. Returning NEED_ANALYSIS is a CORRECT outcome and costs the "
        "fleet far less than a wrong high-impact guess -- it is not a failed task.\n"
        "LOW-impact details are the opposite: choose one, note it in your summary, and keep going."
    )


def contract_summary(contract: dict[str, Any] | None) -> dict[str, Any]:
    """Flat, countable shape for whatever measures this -- telemetry and
    benchmark tooling are owned elsewhere, so this returns plain counters
    and no opinion about where they get stored."""
    if not isinstance(contract, dict):
        return {"has_contract": False}
    decisions = contract.get("decision_budget") or []
    assumptions = contract.get("assumptions") or []

    def _count(entries: Any, impact: str, **match: str) -> int:
        total = 0
        for entry in entries if isinstance(entries, (list, tuple)) else []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("impact") or "").upper() != impact:
                continue
            if all(str(entry.get(k) or ("" if k != "status" else OPEN)).upper() == v
                   for k, v in match.items()):
                total += 1
        return total

    return {
        "has_contract": True,
        "protocol": str(contract.get("protocol") or ""),
        "profile": str(contract.get("profile") or DEFAULT_PROFILE).upper(),
        "enforcement": str(contract.get("enforcement") or DEFAULT_ENFORCEMENT).lower(),
        "contract_digest": contract_digest(contract),
        "stateful": bool(contract.get("stateful")),
        "decisions_total": len(decisions) if isinstance(decisions, (list, tuple)) else 0,
        "decisions_high_open": _count(decisions, HIGH, status=OPEN),
        "decisions_high_resolved": _count(decisions, HIGH, status=RESOLVED),
        "decisions_medium_open": _count(decisions, MEDIUM, status=OPEN),
        "decisions_low": _count(decisions, LOW),
        "assumptions_total": len(assumptions) if isinstance(assumptions, (list, tuple)) else 0,
        "assumptions_high_impact": _count(assumptions, HIGH),
        "has_critic_result": _non_empty(contract.get("critic_result")),
        "context_pack_refs": len(contract.get("context_pack") or []),
    }
