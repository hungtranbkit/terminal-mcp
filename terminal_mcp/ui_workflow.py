"""The UI workflow, as one deterministic policy -- TMCP-UI-WORKFLOW-001.

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
This module DECIDES; it never executes. It answers, for one UI task:

  * which project profile applies, and therefore which viewports and checks
  * whether Image-to-Code is in play at all
  * which design authority wins when two of them disagree
  * whether an audit result is allowed to proceed to browser verification
  * which stage comes next, and which checkpoint has been reached

It opens no browser, installs no skill and fetches nothing. Execution stays
where it already lives: `browser_gateway.py` for verification, the agent's own
skills for implementation. That separation is the reason this can be pure,
deterministic and fully unit-tested, and the reason nothing here can regress
the Browser Gateway's compact public surface (terminal_turn
browser_verify/browser_status/browser_screenshot/browser_stop).

WHY A MODULE AND NOT A PROMPT
-----------------------------
Same argument `orchestration_policy.py` makes one layer up: rules that live
only in chat history are re-taught every conversation and silently lost on a
chat reset. A precedence order that is re-derived per task is not a precedence
order. Here it is a table, and `resolve_precedence` is the only thing that
reads it.

DETERMINISM
-----------
The runtime must not fetch arbitrary skills mid-task, so `SKILL_CATALOG` is a
CURATED, PINNED index -- names, purposes and source references only. It is a
bill of materials, never an installer: nothing in this module downloads, and a
skill that is not installed is reported as `available=False` rather than being
fetched. `Awesome Design Agent Skills` is deliberately catalog-only for exactly
this reason -- bulk-loading a collection at runtime is the opposite of a
deterministic flow.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

UI_WORKFLOW_POLICY_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Precedence. The whole point of the feature.
# ---------------------------------------------------------------------------
# Lower number wins. A lower-precedence layer may ADD detail but must never
# override a higher one -- `resolve_precedence` enforces that by construction
# rather than by asking the caller to remember it.
PRECEDENCE: tuple[tuple[int, str, str], ...] = (
    (1, "user_instruction", "what the operator asked for in this task"),
    (2, "project_ui_rules", "the project's own UI rules (profile below)"),
    (3, "design_system", "the product's existing tokens/components"),
    (4, "reference_mockup", "a supplied screenshot/mockup"),
    (5, "taste", "Taste skill art direction"),
    (6, "web_design_guidelines", "Vercel Web Design Guidelines"),
    (7, "generic_ui_skill", "UI-UX-Pro-Max / generic support"),
)

LAYER_RANK: dict[str, int] = {name: rank for rank, name, _ in PRECEDENCE}


# ---------------------------------------------------------------------------
# Audit gate
# ---------------------------------------------------------------------------
GATE_PASS = "PASS"
GATE_FIX_REQUIRED = "FIX_REQUIRED"
GATE_FAIL = "FAIL"

SEVERITIES = ("critical", "major", "minor")


# ---------------------------------------------------------------------------
# Deterministic flow
# ---------------------------------------------------------------------------
STAGES: tuple[str, ...] = (
    "profile",           # resolve the project profile
    "taste",             # art direction
    "image_to_code",     # CONDITIONAL -- see route_image_to_code
    "implementation",
    "guidelines_audit",
    "fix_blocking",      # CONDITIONAL -- only when the gate demands it
    "browser_verify",
    "fix_reverify",      # CONDITIONAL -- only when verification failed
    "commit",
    "deploy",
    "live_verify",
)

#: The five checkpoints the task specified, mapped to the stage that completes
#: them. Kept separate from STAGES because a checkpoint is a durable, resumable
#: boundary and a stage is just a step -- a retry resumes at a checkpoint.
CHECKPOINTS: tuple[tuple[int, str, str], ...] = (
    (1, "visual_contract", "image_to_code"),
    (2, "implementation_complete", "implementation"),
    (3, "audit_complete", "guidelines_audit"),
    (4, "verification_complete", "browser_verify"),
    (5, "deployed_and_live", "live_verify"),
)


# ---------------------------------------------------------------------------
# Project profiles
# ---------------------------------------------------------------------------
#: Default viewports for every profile: desktop, large desktop, mobile.
DEFAULT_VIEWPORTS: tuple[tuple[int, int], ...] = ((1366, 768), (1920, 1080), (390, 844))

COMMON_CHECKS: tuple[str, ...] = ("responsive", "overflow", "console", "network")


@dataclass(frozen=True)
class ProjectProfile:
    """One project's UI rules. Data, not behaviour."""

    project_id: str
    display_name: str
    viewports: tuple[tuple[int, int], ...] = DEFAULT_VIEWPORTS
    checks: tuple[str, ...] = COMMON_CHECKS
    #: Layer-2 rules. These outrank Taste and the Guidelines, which is the
    #: entire reason they are recorded per project instead of being advice.
    ui_rules: tuple[str, ...] = ()
    #: Matched against a task's repo/cwd/text to pick a profile.
    match_patterns: tuple[str, ...] = ()
    #: False means "never change app behaviour for a UI task" -- MESFlow's
    #: explicit constraint, enforced by `allows_behaviour_change`.
    allow_behaviour_change: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "display_name": self.display_name,
            "viewports": [{"width": w, "height": h} for w, h in self.viewports],
            "checks": list(self.checks),
            "ui_rules": list(self.ui_rules),
            "allow_behaviour_change": self.allow_behaviour_change,
        }


NOVARETAIL = ProjectProfile(
    project_id="novaretail",
    display_name="NovaRetail",
    ui_rules=(
        "UI V3 ONLY -- never revive or reintroduce V2.",
        "Reuse the shared design tokens and components; do not fork them.",
        "Do not redesign business flows unless the operator asked explicitly.",
    ),
    checks=COMMON_CHECKS + ("forms", "modals", "tables_lists", "print"),
    match_patterns=("novaretail", "nova-retail", "nwr"),
)

MESFLOW = ProjectProfile(
    project_id="mesflow",
    display_name="MESFlow",
    ui_rules=(
        "Preserve the current MESFlow UX rules; this is not a redesign.",
        "Do not change application behaviour -- a safe test fixture only, "
        "and only if strictly necessary.",
    ),
    checks=COMMON_CHECKS + ("dashboard", "po_part_operation_views", "kiosk_mobile",
                            "quantity_multisession"),
    match_patterns=("mesflow", "mes-flow"),
)

#: Used when nothing matches. Deliberately the STRICTEST posture, not the
#: loosest: an unrecognised project is the case where we know least, so it
#: inherits the common checks and forbids behaviour change until someone
#: writes a profile for it.
GENERIC = ProjectProfile(
    project_id="generic",
    display_name="Generic project",
    ui_rules=("No project profile matched -- treat existing UI as authoritative "
              "and make the smallest change that satisfies the request.",),
)

PROFILES: tuple[ProjectProfile, ...] = (NOVARETAIL, MESFLOW)


# ---------------------------------------------------------------------------
# Curated skill catalog -- a bill of materials, never an installer
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SkillRef:
    name: str
    purpose: str
    precedence_layer: str
    #: Where it comes from, for the operator. This module never fetches it.
    source: str = ""
    #: Pinned revision when the install mechanism records one.
    revision: str = ""
    #: True only for the curated index that must NEVER be bulk-loaded.
    catalog_only: bool = False

    def as_dict(self, *, available: bool | None = None) -> dict[str, Any]:
        data = {
            "name": self.name, "purpose": self.purpose,
            "precedence_layer": self.precedence_layer,
            "precedence_rank": LAYER_RANK.get(self.precedence_layer),
            "source": self.source, "revision": self.revision,
            "catalog_only": self.catalog_only,
        }
        if available is not None:
            data["available"] = available
        return data


SKILL_CATALOG: tuple[SkillRef, ...] = (
    SkillRef(name="taste", purpose="art direction and design quality",
             precedence_layer="taste"),
    SkillRef(name="taste:image-to-code",
             purpose="turn a reference screenshot/mockup into code; "
                     "CONDITIONAL -- only with a reference",
             precedence_layer="reference_mockup"),
    SkillRef(name="web-design-guidelines",
             purpose="post-implementation audit gate (Vercel Web Design Guidelines)",
             precedence_layer="web_design_guidelines"),
    SkillRef(name="frontend-design",
             purpose="Anthropic frontend-design skill; generic UI support",
             precedence_layer="generic_ui_skill",
             source="claude-plugins-official/plugins/frontend-design"),
    SkillRef(name="ui-ux-pro-max",
             purpose="generic UI support, DELIBERATELY lowest precedence",
             precedence_layer="generic_ui_skill"),
    SkillRef(name="awesome-design-agent-skills",
             purpose="curated index only -- never bulk-loaded at runtime",
             precedence_layer="generic_ui_skill",
             catalog_only=True),
)


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------
def select_profile(*, project_id: str | None = None, repo_root: str | None = None,
                   text: str | None = None,
                   profiles: Iterable[ProjectProfile] = PROFILES) -> ProjectProfile:
    """Which project's rules apply.

    An explicit `project_id` always wins -- a caller that names the project is
    layer 1 (user instruction), and guessing over it would invert precedence.
    Only then does it fall back to matching the repo path and finally the task
    text, which is the least reliable signal and therefore last.
    """
    profiles = tuple(profiles)
    if project_id:
        wanted = project_id.strip().casefold()
        for profile in profiles:
            if profile.project_id == wanted:
                return profile
    for haystack in (repo_root, text):
        if not haystack:
            continue
        low = str(haystack).casefold()
        for profile in profiles:
            if any(pattern in low for pattern in profile.match_patterns):
                return profile
    return GENERIC


def route_image_to_code(*, reference_images: Iterable[str] | None = None,
                        reference_provided: bool | None = None) -> dict[str, Any]:
    """Image-to-Code runs ONLY when a real reference exists.

    The condition is the feature. Running it without a reference means
    inventing a visual contract and then implementing against the invention,
    which is worse than not running it at all -- so the absence of a reference
    is an answer ("skip"), never a prompt to improvise one.

    `reference_provided` lets a caller state the fact directly when it holds
    the image somewhere this module cannot see; an explicit False always wins
    over a non-empty list, because the caller knows and we are guessing.
    """
    images = [str(i) for i in (reference_images or []) if str(i).strip()]
    if reference_provided is False:
        return {"route": "skip", "reason": "caller stated no reference is available",
                "references": [], "authority_layer": None}
    if reference_provided is True and not images:
        return {"route": "image_to_code",
                "reason": "caller stated a reference exists (not enumerated here)",
                "references": [], "authority_layer": "reference_mockup"}
    if not images:
        return {"route": "skip",
                "reason": "no reference screenshot or mockup supplied -- "
                          "implement from project rules and design system instead",
                "references": [], "authority_layer": None}
    return {"route": "image_to_code",
            "reason": f"{len(images)} reference(s) supplied",
            "references": images, "authority_layer": "reference_mockup"}


def resolve_precedence(layers: dict[str, Any] | None) -> dict[str, Any]:
    """Order the contributing authorities and say who won.

    Returns the winning layer plus the ordered chain, so a caller can show WHY
    a decision stands rather than only what it is. A layer present but empty
    does not count as contributing -- silence is not an opinion.
    """
    layers = layers or {}
    contributing = []
    for rank, name, description in PRECEDENCE:
        value = layers.get(name)
        if value in (None, "", [], (), {}):
            continue
        contributing.append({"rank": rank, "layer": name,
                             "description": description, "value": value})
    contributing.sort(key=lambda item: item["rank"])
    winner = contributing[0] if contributing else None
    return {
        "winner": winner["layer"] if winner else None,
        "winning_rank": winner["rank"] if winner else None,
        "chain": contributing,
        # Named explicitly so a caller does not have to re-derive the rule
        # from the ordering it was just handed.
        "rule": "lower rank wins; a lower-precedence layer may add detail but "
                "must never override a higher one",
        "overridden": [item["layer"] for item in contributing[1:]],
    }


def _count(findings: Iterable[Any], severity: str) -> int:
    total = 0
    for finding in findings or ():
        if isinstance(finding, dict):
            value = str(finding.get("severity") or "").strip().casefold()
        else:
            value = str(getattr(finding, "severity", "")).strip().casefold()
        if value == severity:
            total += 1
    return total


def audit_gate(findings: Iterable[Any] | None = None, *,
               counts: dict[str, int] | None = None,
               minor_fix_is_low_risk: bool = False) -> dict[str, Any]:
    """The Web Design Guidelines gate.

    The thresholds are the specified ones, and the distinction between the
    two failing verdicts is the useful part:

      critical > 0 -> FAIL           the work does not proceed
      major    > 0 -> FIX_REQUIRED   fix BEFORE browser verification
      minor    > 0 -> PASS, with fix_now only when it is low risk;
                      otherwise reported and carried forward

    A caller may hand raw `findings` or pre-computed `counts`; counts win,
    because a caller that already aggregated has information we would only be
    re-deriving.
    """
    findings = list(findings or [])
    if counts is not None:
        tally = {s: int(counts.get(s, 0) or 0) for s in SEVERITIES}
    else:
        tally = {s: _count(findings, s) for s in SEVERITIES}

    if tally["critical"] > 0:
        verdict, blocking = GATE_FAIL, True
        reason = f"{tally['critical']} critical finding(s) -- the gate fails closed"
    elif tally["major"] > 0:
        verdict, blocking = GATE_FIX_REQUIRED, True
        reason = (f"{tally['major']} major finding(s) must be fixed before "
                  "browser verification")
    else:
        verdict, blocking = GATE_PASS, False
        reason = ("no critical or major findings"
                  + (f"; {tally['minor']} minor" if tally["minor"] else ""))

    minor_action = None
    if tally["minor"]:
        minor_action = "fix_now" if minor_fix_is_low_risk else "report_and_carry_forward"

    return {
        "verdict": verdict,
        "blocking": blocking,
        "reason": reason,
        "counts": tally,
        "minor_action": minor_action,
        # What the caller must do next, so the gate is actionable rather than
        # only judgemental.
        "next_stage": "fix_blocking" if blocking else "browser_verify",
        "policy_version": UI_WORKFLOW_POLICY_VERSION,
    }


def next_stage(*, completed: Iterable[str] | None = None,
               image_to_code: bool = False,
               gate_blocking: bool = False,
               verification_failed: bool = False) -> str | None:
    """The next stage, given what is already done.

    Conditional stages are SKIPPED rather than reported as pending, so a flow
    with no reference mockup and a clean audit is genuinely shorter instead of
    carrying two stages nobody will run.
    """
    done = {str(s) for s in (completed or ())}
    for stage in STAGES:
        if stage in done:
            continue
        if stage == "image_to_code" and not image_to_code:
            continue
        if stage == "fix_blocking" and not gate_blocking:
            continue
        if stage == "fix_reverify" and not verification_failed:
            continue
        return stage
    return None


def checkpoint_for(stage: str) -> dict[str, Any] | None:
    for number, name, completing_stage in CHECKPOINTS:
        if completing_stage == stage:
            return {"checkpoint": number, "name": name, "completed_by_stage": stage}
    return None


def allows_behaviour_change(profile: ProjectProfile, *, requested: bool) -> dict[str, Any]:
    """Whether this profile permits an app-behaviour change in a UI task."""
    if not requested:
        return {"allowed": True, "reason": "no behaviour change requested"}
    if profile.allow_behaviour_change:
        return {"allowed": True, "reason": f"{profile.display_name} permits it"}
    return {"allowed": False,
            "reason": (f"{profile.display_name}'s profile forbids changing application "
                       "behaviour during a UI task; a safe test fixture is the only "
                       "exception and needs an explicit operator decision")}


def skill_catalog(*, installed: Iterable[str] | None = None) -> dict[str, Any]:
    """The pinned catalog, annotated with what is actually installed.

    `installed` is supplied by the caller (who can see the skill directory);
    this module never probes and never fetches. An absent skill is reported
    absent -- the deterministic-runtime requirement means a missing skill
    degrades the flow honestly instead of triggering a download mid-task.
    """
    present = {str(name).strip().casefold() for name in (installed or ())}
    entries = [ref.as_dict(available=ref.name.casefold() in present) for ref in SKILL_CATALOG]
    missing = [e["name"] for e in entries if not e["available"] and not e["catalog_only"]]
    return {
        "policy_version": UI_WORKFLOW_POLICY_VERSION,
        "skills": entries,
        "missing": missing,
        "runtime_fetch": "never -- this catalog is a bill of materials, not an installer",
    }


def plan(*, task: str = "", project_id: str | None = None, repo_root: str | None = None,
         reference_images: Iterable[str] | None = None,
         reference_provided: bool | None = None,
         behaviour_change_requested: bool = False,
         installed_skills: Iterable[str] | None = None,
         layers: dict[str, Any] | None = None) -> dict[str, Any]:
    """The whole decision for one UI task, in one deterministic call.

    Pure: same inputs, same output, no clock, no network, no filesystem. That
    is what makes the flow reproducible across a chat reset -- the caller can
    re-ask and get the identical plan rather than a re-improvised one.
    """
    profile = select_profile(project_id=project_id, repo_root=repo_root, text=task)
    routing = route_image_to_code(reference_images=reference_images,
                                  reference_provided=reference_provided)
    # The profile's own rules enter the precedence chain as layer 2 unless the
    # caller already supplied that layer explicitly.
    chain_input = dict(layers or {})
    chain_input.setdefault("project_ui_rules", list(profile.ui_rules) or None)
    if routing["route"] == "image_to_code":
        chain_input.setdefault("reference_mockup", routing["references"] or True)
    precedence = resolve_precedence(chain_input)
    behaviour = allows_behaviour_change(profile, requested=behaviour_change_requested)
    uses_image_to_code = routing["route"] == "image_to_code"
    return {
        "policy_version": UI_WORKFLOW_POLICY_VERSION,
        "profile": profile.as_dict(),
        "image_to_code": routing,
        "precedence": precedence,
        "behaviour_change": behaviour,
        "stages": [s for s in STAGES
                   if not (s == "image_to_code" and not uses_image_to_code)
                   and s not in ("fix_blocking", "fix_reverify")],
        "conditional_stages": {
            "image_to_code": uses_image_to_code,
            "fix_blocking": "decided by the guidelines audit gate",
            "fix_reverify": "decided by browser verification",
        },
        "checkpoints": [{"checkpoint": n, "name": name, "completed_by_stage": stage}
                        for n, name, stage in CHECKPOINTS],
        "next_stage": next_stage(completed=(), image_to_code=uses_image_to_code),
        "verification": {
            # The Browser Gateway's own compact surface -- named, not
            # reimplemented, so this module cannot drift from it.
            "engine": "browser_gateway (playwright)",
            "actions": ["browser_verify", "browser_status", "browser_screenshot",
                        "browser_stop"],
            "viewports": [{"width": w, "height": h} for w, h in profile.viewports],
            "checks": list(profile.checks),
        },
        "skills": skill_catalog(installed=installed_skills),
    }


def policy_document() -> str:
    """The human-readable form, generated from the same constants the code
    uses, so the doc cannot disagree with the behaviour."""
    lines = [
        "# UI Workflow Policy (TMCP-UI-WORKFLOW-001)",
        "",
        f"Policy version {UI_WORKFLOW_POLICY_VERSION}. Generated from "
        "`terminal_mcp/ui_workflow.py`; do not edit by hand.",
        "",
        "## Deterministic flow",
        "",
        "```",
        "TASK -> project profile -> Taste -> [Image-to-Code only with a reference]",
        "     -> implementation -> Web Design Guidelines audit",
        "     -> fix critical/major -> Playwright verify -> fix/reverify",
        "     -> commit -> safe deploy -> live verify",
        "```",
        "",
        "## Precedence (lower number wins)",
        "",
        "| # | Layer | Authority |",
        "| --- | --- | --- |",
    ]
    for rank, name, description in PRECEDENCE:
        lines.append(f"| {rank} | `{name}` | {description} |")
    lines += [
        "",
        "A lower-precedence layer may add detail. It may never override a higher one.",
        "",
        "## Audit gate",
        "",
        "| Severity | Verdict |",
        "| --- | --- |",
        f"| critical > 0 | `{GATE_FAIL}` -- does not proceed |",
        f"| major > 0 | `{GATE_FIX_REQUIRED}` -- fix before browser verification |",
        f"| minor > 0 | `{GATE_PASS}` -- fix when low risk, else report |",
        "",
        "## Project profiles",
        "",
    ]
    for profile in PROFILES + (GENERIC,):
        viewports = ", ".join(f"{w}x{h}" for w, h in profile.viewports)
        lines += [
            f"### {profile.display_name} (`{profile.project_id}`)",
            "",
            f"- Viewports: {viewports}",
            f"- Checks: {', '.join(profile.checks)}",
            f"- Behaviour change allowed: {profile.allow_behaviour_change}",
        ]
        for rule in profile.ui_rules:
            lines.append(f"- Rule: {rule}")
        lines.append("")
    lines += [
        "## Skills",
        "",
        "Curated and pinned. This policy never fetches a skill at runtime; a",
        "missing skill is reported missing.",
        "",
        "| Skill | Layer | Purpose |",
        "| --- | --- | --- |",
    ]
    for ref in SKILL_CATALOG:
        note = " (catalog only)" if ref.catalog_only else ""
        lines.append(f"| `{ref.name}`{note} | `{ref.precedence_layer}` | {ref.purpose} |")
    lines += [
        "",
        "## Verification",
        "",
        "Verification is the existing Browser Gateway compact surface and nothing",
        "else: `browser_verify`, `browser_status`, `browser_screenshot`,",
        "`browser_stop` via `terminal_turn`. This policy decides WHAT to verify;",
        "it never opens a browser itself.",
        "",
    ]
    return "\n".join(lines)
