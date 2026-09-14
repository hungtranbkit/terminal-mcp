"""Work Execution Spec -- the planning contract for EVERY kind of task.

WHY THIS EXISTS ALONGSIDE `bug_spec`

`bug_spec.py` proved the shape: a planner does the analysis once, persists it,
and the worker receives the spec instead of the conversation. That removed the
single largest avoidable cost in a two-agent workflow -- the second full
analysis from an empty repository.

But every field in it is bug-shaped: `user_symptom`, `expected_behavior`,
`suspected_root_cause`. A new feature has no symptom and no root cause, so a
planner writing one either leaves the spec half-empty (and the completeness
gate correctly refuses it) or lies in prose to get past the gate. Both
outcomes push the analysis back into the worker, which is the cost the spec
existed to remove.

So the contract is generalised here by TASK TYPE. What a spec must carry to be
executable depends on what kind of work it is:

  FEATURE_NEW   requirement, user value, scope AND out-of-scope, architecture
                impact, reuse candidates, contracts, test plan, acceptance
  BUG           symptom, expected, locator, hypothesis, fix strategy, acceptance
  REFACTOR      requirement, scope/out-of-scope, behaviour-preservation proof,
                regression areas -- acceptance is "nothing observable changed"
  INTEGRATION   the two sides, the contract between them, failure modes
  RESEARCH      the question, the boundary, what a finished answer looks like
  DEPLOY        artifact, target level, rollback plan, verification

WHAT IS DELIBERATELY NOT DUPLICATED

The vocabulary is imported from `bug_spec`, not restated: L1/L2/L3, the
PLAN_CONFIRMED/ADJUSTED/MISMATCH handshake, NEEDS_REDEFINE. A second set of
names for the same states is how two screens come to disagree about what a
worker is doing. `task_classifier.classify` still decides execution mode and
deploy level; this module never re-derives risk.

SCOPE AND OUT_OF_SCOPE ARE BOTH REQUIRED FOR A FEATURE

Scope alone does not bound a feature. "Add CSV export" with no out-of-scope
line is how a worker also builds XLSX export, a scheduler and a settings page.
The out-of-scope list is what makes a feature spec finishable, so it carries
real weight in the completeness score rather than being a nicety.

REUSE IS A FIRST-CLASS FIELD, NOT ADVICE

`reuse_candidates` is required for FEATURE_NEW and INTEGRATION because the
expensive failure is a second implementation of something the repository
already has. A planner that has not looked has not finished planning, and an
empty list is a gate failure rather than a default.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .bug_spec import (HIT_SOFT_LIMIT, L1, L2, L3, NEEDS_REDEFINE,
                       PLAN_ADJUSTED, PLAN_CONFIRMED, PLAN_MISMATCH,
                       PLAN_PENDING, SPEC_READY)
from .project_knowledge import SecretInKnowledge, scrub_knowledge
from .schema import Migration, apply_migrations
from .task_classifier import (FAST_FIX, LARGE, MEDIUM, NORMAL, SAFE, SMALL,
                              classify)

# -- task types ---------------------------------------------------------------
# Enumerated rather than free-form for the same reason the issue states are: a
# type nobody enumerated is a type whose spec nobody validates.
FEATURE_NEW = "FEATURE_NEW"
BUG = "BUG"
REFACTOR = "REFACTOR"
INTEGRATION = "INTEGRATION"
RESEARCH = "RESEARCH"
DEPLOY = "DEPLOY"

TASK_TYPES = (FEATURE_NEW, BUG, REFACTOR, INTEGRATION, RESEARCH, DEPLOY)

# Verification outcomes, re-exported so a caller importing this module does not
# need to know the handshake was born in `bug_spec`.
__all__ = [
    "FEATURE_NEW", "BUG", "REFACTOR", "INTEGRATION", "RESEARCH", "DEPLOY",
    "TASK_TYPES", "WorkSpec", "WorkSpecStore", "completeness", "gate",
    "budget_for", "budget_check", "plan_from_request", "infer_task_type",
    "PLAN_CONFIRMED", "PLAN_ADJUSTED", "PLAN_MISMATCH", "PLAN_PENDING",
    "NEEDS_REDEFINE", "SPEC_READY",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_spec_db_path() -> Path:
    """Beside the bug specs, in the same state directory, for the same reason:
    a spec outlives the session that created it."""
    base = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return Path(os.environ.get("TERMINAL_MCP_WORK_SPEC_DB")
                or base / "terminal-mcp" / "work_specs.db")


@dataclass
class WorkSpec:
    """One task's analysis, whatever kind of task it is.

    Fields are a superset across types. That is deliberate: one table and one
    payload shape keeps the store, the UI and the MCP surface from growing a
    branch per type. Which subset is REQUIRED is decided by
    `REQUIRED_FIELDS_BY_TYPE`, so an unused field costs an empty string rather
    than a schema.
    """

    spec_id: str
    title: str
    task_type: str = FEATURE_NEW

    # -- what the work is -----------------------------------------------------
    requirement: str = ""          # what must be true when this is done
    problem: str = ""              # what is wrong or missing today
    user_value: str = ""           # who is better off, and how
    expected_outcome: str = ""     # what an observer sees afterwards
    symptom: str = ""              # BUG: what the user sees
    expected_behavior: str = ""    # BUG: what should happen instead
    hypothesis: str = ""           # BUG/REFACTOR: suspected cause or risk
    research_question: str = ""    # RESEARCH: the question being answered

    # -- boundaries -----------------------------------------------------------
    scope: tuple[str, ...] = ()
    out_of_scope: tuple[str, ...] = ()
    arch_impact: str = ""

    # -- reuse, before anything new gets built --------------------------------
    # Free-form lines naming an existing component, API, schema, test, runbook
    # or template. Required for FEATURE_NEW and INTEGRATION; see the module
    # docstring for why an empty list fails the gate rather than defaulting.
    reuse_candidates: tuple[str, ...] = ()
    # The decision the planner reached about each candidate, as REUSE / EXTEND
    # / NEW lines with their evidence. Recorded rather than implied, so a
    # reviewer can see that "NEW" was a choice and not an oversight.
    reuse_decisions: tuple[str, ...] = ()
    # Conventions this repository already follows for work of this shape --
    # the thing a worker should imitate instead of inventing a second style.
    existing_patterns: tuple[str, ...] = ()

    # -- where it lives -------------------------------------------------------
    likely_module: str | None = None
    relevant_modules: tuple[str, ...] = ()
    likely_files: tuple[str, ...] = ()
    entry_points: tuple[str, ...] = ()
    search_terms: tuple[str, ...] = ()
    relevant_flow: str = ""

    # -- the contracts a worker must not invent -------------------------------
    data_contract: str = ""
    api_contract: str = ""
    ui_contract: str = ""
    migration: str = ""

    # -- how it is executed ---------------------------------------------------
    implementation_plan: tuple[str, ...] = ()
    # Subtasks as a DAG: each entry is {"id", "title", "depends_on": [...]}.
    # Carried on the spec so a decomposition survives a restart even before
    # the queue tasks exist; `work_planning` is what turns it into queue rows.
    subtask_dag: tuple[dict[str, Any], ...] = ()
    fix_strategy: tuple[str, ...] = ()
    do_not_touch: tuple[str, ...] = ()
    regression_areas: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()

    # -- how it is proven -----------------------------------------------------
    # Runbook IDs, never command strings: the registry owns the command.
    test_plan: tuple[str, ...] = ()
    test_runbook: str | None = None
    smoke_runbook: str | None = None
    deploy_runbook: str | None = None
    acceptance_criteria: tuple[str, ...] = ()

    # -- how it ships ---------------------------------------------------------
    deploy_level: str = "PREVIEW"
    rollout: str = ""
    rollback_plan: str = ""

    # -- classification, never re-derived here --------------------------------
    execution_mode: str = NORMAL
    risk: str = "MEDIUM"
    knowledge_confidence: str = "LOW"
    # What the knowledge map said about this task's modules when it was
    # planned, already loaded. Persisted on the spec rather than fetched by
    # the worker: a briefing that has to be asked for is a briefing that gets
    # skipped, and the whole point is that the map is read BEFORE the code.
    # It is a snapshot with its commit recorded beside it, never a claim about
    # the repository as it stands now.
    knowledge_brief: str = ""
    knowledge_modules: tuple[str, ...] = ()
    # The soft allowance this spec was planned under, persisted alongside it.
    # Derived by `budget_for`, but recorded so "what was this worker allowed to
    # read" stays answerable after the fact, when the level may have moved.
    file_budget: int = 0
    search_budget: int = 0

    # -- which rules this ran under -------------------------------------------
    # A task records the policy version and hash it actually loaded, so "which
    # rules was this run under" is answerable later rather than inferred from
    # whatever the file says today.
    policy_version: str = ""
    policy_hash: str = ""

    # -- provenance and lifecycle ---------------------------------------------
    source_commit: str | None = None
    knowledge_last_verified_commit: str | None = None
    uncertain: tuple[str, ...] = ()
    project_id: str | None = None
    work_id: str | None = None
    queue_task_id: str | None = None
    parent_spec_id: str | None = None
    plan_status: str = PLAN_PENDING
    plan_note: str = ""
    # Why the gate last refused, and what it asked for. Persisted on the spec
    # rather than returned and forgotten, so a redefine RESUMES this task --
    # the planner adds the missing detail to the same spec and the same queue
    # task continues, instead of the work being re-created and the history
    # starting over.
    redefine_reason: str = ""
    redefine_missing: tuple[str, ...] = ()
    redefine_count: int = 0
    difficulty: str = "MEDIUM"
    human_hints: tuple[str, ...] = ()
    created_by: str | None = None
    created_at: str = ""
    updated_at: str = ""

    # -- derived --------------------------------------------------------------

    def level(self) -> str:
        """How much is actually known -- derived from evidence, never asserted.

        Same rule as a bug spec and for the same reason: a spec naming no
        files and no contract cannot be L1 however confident its prose sounds.
        For a feature, "knowing where it goes" means naming the modules AND
        having settled at least one contract, because a feature whose API
        shape is still open is an investigation, not an implementation.
        """
        has_place = bool(self.likely_files or self.likely_module)
        has_shape = bool((self.data_contract or self.api_contract
                          or self.ui_contract or self.hypothesis
                          or self.fix_strategy))
        has_boundary = bool(self.scope and self.out_of_scope)

        if self.task_type in (FEATURE_NEW, INTEGRATION):
            if has_place and has_shape and has_boundary:
                return L1
            if has_place or has_shape:
                return L2
            return L3
        if self.task_type == RESEARCH:
            # Research is never L1: if the answer were known it would not be
            # research. The useful distinction is whether it is bounded.
            return L2 if (self.research_question and self.out_of_scope) else L3
        if has_place and has_shape:
            return L1
        if has_place or has_shape:
            return L2
        return L3

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for key, value in list(out.items()):
            if isinstance(value, tuple):
                out[key] = list(value)
        out["level"] = self.level()
        return out

    def handoff(self) -> dict[str, Any]:
        """The compact payload a worker receives INSTEAD of the conversation.

        Only what is needed to start: where, what shape, what not to touch,
        how it is proven. The full spec stays in the store for anything the
        worker needs to look up, so this stays small on purpose.
        """
        payload: dict[str, Any] = {
            "SPEC": self.spec_id, "TYPE": self.task_type, "TITLE": self.title,
            "LEVEL": self.level(), "MODE": self.execution_mode,
            "REQUIREMENT": self.requirement or self.symptom or self.research_question,
            "SCOPE": list(self.scope), "OUT_OF_SCOPE": list(self.out_of_scope),
            "WHERE": {"module": self.likely_module, "files": list(self.likely_files),
                      "entry_points": list(self.entry_points),
                      "search_terms": list(self.search_terms)},
            "REUSE_FIRST": list(self.reuse_candidates),
            "REUSE_DECISIONS": list(self.reuse_decisions),
            "FOLLOW_EXISTING_PATTERN": list(self.existing_patterns),
            "IMPLEMENTATION_PLAN": list(self.implementation_plan),
            "DO_NOT_TOUCH": list(self.do_not_touch),
            "BUDGET": {"files": self.file_budget, "search_rounds": self.search_budget,
                       "on_exceed": "return NEEDS_REDEFINE with a reason; do not keep reading"},
            "POLICY": {"version": self.policy_version, "hash": self.policy_hash},
            "ACCEPTANCE": list(self.acceptance_criteria),
            "TEST": {"plan": list(self.test_plan), "runbook": self.test_runbook,
                     "smoke": self.smoke_runbook},
            "DEPLOY": {"level": self.deploy_level, "runbook": self.deploy_runbook,
                       "rollback": self.rollback_plan},
            "SOURCE_COMMIT": self.source_commit,
            "UNCERTAIN": list(self.uncertain),
            "VERIFY_PLAN_FIRST": (
                "Confirm HEAD, confirm the named files and symbols still exist, read only "
                "those, then answer PLAN_CONFIRMED / PLAN_ADJUSTED / PLAN_MISMATCH before "
                "changing anything."),
        }
        contracts = {k: v for k, v in (("data", self.data_contract),
                                       ("api", self.api_contract),
                                       ("ui", self.ui_contract),
                                       ("migration", self.migration)) if v}
        if contracts:
            payload["CONTRACTS"] = contracts
        if self.dependencies:
            payload["DEPENDS_ON"] = list(self.dependencies)
        if self.knowledge_brief:
            # Inside the handoff, because the handoff IS the worker's task
            # start. Anywhere else and reading the map is opt-in again, which
            # is the state this costs a worker a repository read to leave.
            payload["KNOWLEDGE"] = {
                "MODULES": list(self.knowledge_modules),
                "CONFIDENCE": self.knowledge_confidence,
                "VERIFIED_AT_COMMIT": self.knowledge_last_verified_commit,
                "BRIEF": self.knowledge_brief,
                "NOTE": ("loaded from the knowledge map when this was planned -- "
                         "start here instead of searching, then confirm the named "
                         "paths against the current code before editing")}
        if self.human_hints:
            payload["HUMAN_HINTS"] = {
                "hints": list(self.human_hints),
                "note": "guidance with provenance, not truth -- the code still decides"}
        return payload


# -- what each type must carry -------------------------------------------------
# (field key, weight, the question to ask the planner when it is missing).
# Weighted because a missing out-of-scope line costs a worker far more on a
# feature than a missing do-not-touch list does.

_COMMON_TAIL: tuple[tuple[str, float, str], ...] = (
    ("test_evidence", 1.0, "Which verified runbook or test plan proves this?"),
    ("acceptance_criteria", 1.5, "How do we know it is done, observed from outside?"),
)

REQUIRED_FIELDS_BY_TYPE: dict[str, tuple[tuple[str, float, str], ...]] = {
    FEATURE_NEW: (
        ("requirement", 1.5, "What exactly must be true when this is done?"),
        ("problem", 1.0, "What is wrong or missing today that this addresses?"),
        ("user_value", 1.0, "Who is better off, and how would they notice?"),
        ("expected_outcome", 1.0, "What does an observer see once this ships?"),
        ("scope", 1.0, "What is in scope?"),
        ("out_of_scope", 1.5, "What is explicitly OUT of scope for this task?"),
        ("arch_impact", 1.0, "What does this change architecturally, if anything?"),
        ("reuse_candidates", 1.5,
         "What already exists that this should reuse rather than rebuild?"),
        ("existing_patterns", 0.5,
         "Which convention in this repo should the implementation follow?"),
        ("locator", 1.0, "Which modules/files will this touch?"),
        ("contract", 1.5, "What is the data/API/UI contract?"),
        ("implementation_plan", 1.0, "What are the ordered steps to build it?"),
        ("dependencies", 0.5, "What must land first?"),
        ("risks", 0.5, "What could this break?"),
        ("deploy_level", 0.5, "Preview, staging, or no deploy?"),
    ) + _COMMON_TAIL,
    BUG: (
        ("symptom", 1.5, "What exactly does the user see, and where?"),
        ("expected_behavior", 1.0, "What should happen instead?"),
        ("locator", 1.5, "Which files, or which exact search terms, locate it?"),
        ("relevant_flow", 1.0, "What is the call/render flow involved?"),
        ("hypothesis", 1.5, "What is the suspected root cause?"),
        ("fix_strategy", 1.0, "What is the intended fix approach?"),
        ("do_not_touch", 0.5, "What must this change NOT touch?"),
        ("deploy_level", 0.5, "Preview, staging, or no deploy?"),
    ) + _COMMON_TAIL,
    REFACTOR: (
        ("requirement", 1.5, "What shape should the code have afterwards?"),
        ("scope", 1.0, "Which modules are being restructured?"),
        ("out_of_scope", 1.5, "What must NOT be restructured in this task?"),
        ("locator", 1.0, "Which files will move or change?"),
        ("regression_areas", 1.5,
         "Which behaviour must be provably unchanged, and how is that shown?"),
        ("do_not_touch", 1.0, "What must this change NOT touch?"),
        ("risks", 0.5, "What could this break?"),
    ) + _COMMON_TAIL,
    INTEGRATION: (
        ("requirement", 1.5, "What must the two sides be able to do together?"),
        ("scope", 1.0, "Which two systems/modules are being joined?"),
        ("out_of_scope", 1.0, "What is explicitly not being integrated here?"),
        ("contract", 1.5, "What is the contract between them: data, API, auth, errors?"),
        ("reuse_candidates", 1.0, "Which existing adapter/client/schema should this use?"),
        ("locator", 1.0, "Which modules/files will this touch?"),
        ("risks", 1.0, "What are the failure modes when the other side is down or slow?"),
        ("dependencies", 0.5, "What must land or be reachable first?"),
    ) + _COMMON_TAIL,
    RESEARCH: (
        ("research_question", 1.5, "What exact question is being answered?"),
        ("out_of_scope", 1.5, "What is the boundary -- what will NOT be investigated?"),
        ("scope", 1.0, "Where should the investigation look first?"),
        ("acceptance_criteria", 1.5, "What does a finished answer look like?"),
        ("dependencies", 0.5, "What does the answer unblock?"),
    ),
    DEPLOY: (
        ("requirement", 1.5, "What artifact is being deployed, and to what?"),
        ("deploy_level", 1.5, "Preview, staging, or production?"),
        ("rollback_plan", 1.5, "How is this rolled back, concretely?"),
        ("test_evidence", 1.5, "Which verification proves the artifact is good?"),
        ("risks", 1.0, "What breaks if this goes wrong, and who notices?"),
        ("dependencies", 0.5, "What must be in place first?"),
    ),
}

# A spec claiming more certainty must carry more evidence to back it. Same
# thresholds as a bug spec: the bar is about how much is claimed, not about
# what kind of work it is.
COMPLETENESS_THRESHOLDS = {L1: 0.90, L2: 0.75, L3: 0.40}

# Fields no score may excuse.
#
# A weighted threshold alone has a perverse edge: `level()` is derived from the
# evidence present, so DELETING a required field can drop the spec to a lower
# level, which lowers the threshold, which lets the thinner spec pass. A test
# caught exactly that -- removing `out_of_scope` from a complete feature spec
# made it READY, and a DEPLOY spec with nothing but a title passed at L3
# without a rollback plan.
#
# So these are a floor, checked independently of the score. Each one is here
# because its absence makes the task unfinishable or unsafe, not merely
# thinner: a feature with no out-of-scope line has no definition of done, and
# a deploy with no rollback plan is a one-way door.
# A field that `level()` reads must be in the floor for that type, or removing
# it lowers the level, lowers the threshold, and lets the thinner spec through.
# `scope` and `out_of_scope` are both read by the FEATURE_NEW/INTEGRATION
# branch, so both are listed there.
#
# A second edge, found the same way: ADDING required fields raises the total
# weight, so a field that used to be decisive stops being decisive. Weight is
# a statement about how much a gap costs, not about whether the task is
# executable without it -- which is why the floor is enumerated rather than
# inferred from weight.
MANDATORY_BY_TYPE: dict[str, tuple[str, ...]] = {
    FEATURE_NEW: ("requirement", "problem", "user_value", "expected_outcome",
                  "scope", "out_of_scope", "reuse_candidates", "acceptance_criteria"),
    BUG: ("symptom", "acceptance_criteria"),
    REFACTOR: ("out_of_scope", "regression_areas"),
    INTEGRATION: ("requirement", "scope", "out_of_scope", "contract"),
    RESEARCH: ("research_question", "out_of_scope", "acceptance_criteria"),
    DEPLOY: ("requirement", "rollback_plan", "test_evidence"),
}


def _present(spec: WorkSpec, field_name: str) -> bool:
    """Whether a required field is satisfied.

    Several keys are satisfied by any ONE of a set of fields -- a locator is a
    file list OR a search term OR an entry point, and a contract is any of
    data/API/UI. Requiring all three of a contract would fail a backend-only
    feature for not having a UI shape.
    """
    if field_name == "locator":
        return bool(spec.likely_files or spec.search_terms
                    or spec.entry_points or spec.likely_module)
    if field_name == "contract":
        return bool(spec.data_contract or spec.api_contract or spec.ui_contract)
    if field_name == "test_evidence":
        return bool(spec.test_plan or spec.test_runbook)
    value = getattr(spec, field_name, None)
    if isinstance(value, (tuple, list)):
        return bool(value)
    return bool(str(value or "").strip())


def completeness(spec: WorkSpec) -> dict[str, Any]:
    """Score a spec against what its TYPE and its claimed LEVEL require.

    Scored against the level `level()` derives, so a planner cannot lower the
    bar by claiming less confidence: the level comes from the evidence.
    """
    required = REQUIRED_FIELDS_BY_TYPE.get(spec.task_type)
    if required is None:
        raise ValueError(f"unknown task_type {spec.task_type!r}; "
                         f"expected one of {', '.join(TASK_TYPES)}")

    total = sum(weight for _, weight, _ in required)
    earned = sum(weight for name, weight, _ in required if _present(spec, name))
    missing = [name for name, _, _ in required if not _present(spec, name)]
    questions = [question for name, _, question in required if not _present(spec, name)]

    level = spec.level()
    threshold = COMPLETENESS_THRESHOLDS[level]
    score = (earned / total) if total else 0.0

    # The floor, checked independently of the score -- see MANDATORY_BY_TYPE
    # for why a threshold alone is not enough.
    mandatory_missing = [name for name in MANDATORY_BY_TYPE.get(spec.task_type, ())
                         if not _present(spec, name)]
    return {
        "task_type": spec.task_type,
        "level": level,
        "score": score,
        "threshold": threshold,
        "ready": score >= threshold and not mandatory_missing,
        "missing": missing,
        "mandatory_missing": mandatory_missing,
        "questions_for_planner": questions[:5],
    }


# -- budgets, by task type ------------------------------------------------------
# A feature legitimately needs to read more than a bug fix: it has to find the
# patterns it should follow and the code it should reuse. It still is not a
# licence to read the repository. Numbers are per task, counted by the worker,
# and are SOFT -- crossing one means "say why", never "stop mid-thought".

FILE_SEARCH_BUDGET: dict[str, dict[str, dict[str, int]]] = {
    BUG:         {L1: {"max_files": 5,  "max_search_rounds": 2},
                  L2: {"max_files": 15, "max_search_rounds": 4},
                  L3: {"max_files": 40, "max_search_rounds": 8}},
    FEATURE_NEW: {L1: {"max_files": 12, "max_search_rounds": 4},
                  L2: {"max_files": 25, "max_search_rounds": 7},
                  L3: {"max_files": 50, "max_search_rounds": 12}},
    REFACTOR:    {L1: {"max_files": 15, "max_search_rounds": 4},
                  L2: {"max_files": 30, "max_search_rounds": 8},
                  L3: {"max_files": 60, "max_search_rounds": 12}},
    INTEGRATION: {L1: {"max_files": 12, "max_search_rounds": 5},
                  L2: {"max_files": 25, "max_search_rounds": 8},
                  L3: {"max_files": 50, "max_search_rounds": 12}},
    RESEARCH:    {L1: {"max_files": 20, "max_search_rounds": 6},
                  L2: {"max_files": 35, "max_search_rounds": 10},
                  L3: {"max_files": 60, "max_search_rounds": 15}},
    DEPLOY:      {L1: {"max_files": 5,  "max_search_rounds": 2},
                  L2: {"max_files": 10, "max_search_rounds": 3},
                  L3: {"max_files": 20, "max_search_rounds": 5}},
}

_BUDGET_PROFILE_RULES: dict[str, list[str]] = {
    SMALL: [
        "load the spec and the module context pack first",
        "open only the listed files plus at most a couple of directly related ones",
        "never read the repository tree or docs globally",
        "call the registered runbook instead of composing a command",
    ],
    MEDIUM: [
        "load the spec and the module context pack first",
        "read the reuse candidates BEFORE writing anything new",
        "widen only where the evidence demands it, and say what widened it",
        "call the registered runbook instead of composing a command",
    ],
    LARGE: [
        "load the spec, the context pack and the similar prior work first",
        "read the reuse candidates BEFORE writing anything new",
        "state the boundary you are working within before widening past it",
        "if the shape is still unclear when the budget is spent, return "
        "NEEDS_REDEFINE rather than continuing to read",
    ],
}


def budget_for(spec: WorkSpec) -> dict[str, Any]:
    """The soft allowance for this spec, by type and by how much is known."""
    level = spec.level()
    limits = FILE_SEARCH_BUDGET[spec.task_type][level]
    if spec.execution_mode == FAST_FIX and level == L1:
        profile = SMALL
    elif level == L3 or spec.task_type in (RESEARCH, REFACTOR):
        profile = LARGE
    else:
        profile = MEDIUM
    # Recorded on the spec as well as returned, so "what was this worker
    # allowed to read" survives a later change of level.
    spec.file_budget = limits["max_files"]
    spec.search_budget = limits["max_search_rounds"]
    return {"profile": profile, "limits": limits,
            "rules": list(_BUDGET_PROFILE_RULES[profile])}


def bind_policy(spec: WorkSpec, *, cwd: str | None = None) -> WorkSpec:
    """Record which WORK_POLICY version and hash this spec was planned under.

    Bound at plan time rather than read at execution time: a policy that
    changes mid-flight must not silently redefine what a running task agreed
    to. A project without a policy file binds nothing and says so by leaving
    the fields empty, rather than inventing a version.
    """
    try:
        from .work_policy import load_policy

        policy = load_policy(cwd or os.getcwd())
    except Exception:  # noqa: BLE001 -- a missing policy is not a spec failure
        return spec
    spec.policy_version = policy.version or ""
    spec.policy_hash = policy.policy_hash or ""
    return spec


def budget_check(spec: WorkSpec, *, files_read: int,
                 search_rounds: int) -> dict[str, Any]:
    """Has this worker spent its allowance without arriving?

    Answers with an ACTION, not a number. Crossing the line on an L1 spec means
    the spec was not actually L1 and should go back to the planner; crossing it
    on anything else means say what widened the scope, then continue
    deliberately. Neither outcome is "keep reading and hope".
    """
    limits = FILE_SEARCH_BUDGET[spec.task_type][spec.level()]
    over_files = files_read > limits["max_files"]
    over_rounds = search_rounds > limits["max_search_rounds"]
    if not (over_files or over_rounds):
        return {"within_budget": True, "action": "continue", "limits": limits}

    exceeded = ([f"{files_read} files read (budget {limits['max_files']})"]
                if over_files else []) + \
               ([f"{search_rounds} search rounds (budget {limits['max_search_rounds']})"]
                if over_rounds else [])
    is_l1 = spec.level() == L1
    return {
        "within_budget": False,
        "action": NEEDS_REDEFINE if is_l1 else "escalate_with_reason",
        "limits": limits,
        "exceeded": exceeded,
        "note": ("an L1 spec that runs out of budget was not actually an L1 -- hand it "
                 "back for a better one rather than turning it into an investigation"
                 if is_l1 else
                 "state what widened the scope and why before reading further"),
    }


def gate(spec: WorkSpec) -> dict[str, Any]:
    """The whole pre-flight check a worker runs before touching anything.

    Returns the verdict, what is missing, the short questions for the planner,
    the budget and the compact handoff -- so a worker that gets NEEDS_REDEFINE
    has a complete reply to send without composing one.
    """
    report = completeness(spec)
    report["budget"] = budget_for(spec)
    report["spec_id"] = spec.spec_id
    report["execution_mode"] = spec.execution_mode
    report["handoff"] = spec.handoff() if report["ready"] else None
    report["status"] = SPEC_READY if report["ready"] else NEEDS_REDEFINE
    if not report["ready"]:
        report["reply_to_planner"] = {
            "STATUS": NEEDS_REDEFINE,
            "SPEC": spec.spec_id,
            "TYPE": spec.task_type,
            "LEVEL": report["level"],
            "COMPLETENESS": f"{report['score']:.0%} vs {report['threshold']:.0%} required",
            "MISSING": report["missing"],
            # Listed separately because these are not a scoring shortfall: no
            # amount of detail elsewhere substitutes for them.
            "BLOCKING": report["mandatory_missing"],
            "QUESTIONS_FOR_PLANNER": report["questions_for_planner"],
            "NOTE": ("not investigating further -- earning this detail here is the "
                     "re-analysis the spec exists to avoid"),
        }
    return report


def escalation(spec: WorkSpec, *, why: str, known: str, missing: str,
               redefine_request: str) -> dict[str, Any]:
    """The compact report a worker sends INSTEAD of widening its search."""
    for label, value in (("why", why), ("known", known), ("missing", missing),
                         ("redefine", redefine_request)):
        scrub_knowledge(value or "", where=f"escalation:{label}")
    return {
        "TOKEN_BUDGET_STATUS": HIT_SOFT_LIMIT,
        "SPEC": spec.spec_id,
        "TYPE": spec.task_type,
        "WHY": why,
        "WHAT_I_KNOW": known,
        "WHAT_I_AM_MISSING": missing,
        "REDEFINE_REQUEST": redefine_request,
    }


# -- inference ------------------------------------------------------------------

# Ordered: the first type whose signal matches wins, so the more specific
# patterns are listed before the general ones. Vietnamese terms are included
# because the operators of this system write requests in both languages, and a
# request that classifies as the wrong type gets the wrong required fields --
# which surfaces as a completeness gate failure nobody can act on.
_TYPE_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (DEPLOY, ("deploy", "release", "rollout", "ship it", "publish",
              "triển khai", "phát hành")),
    # Two kinds of signal. The first names the defect outright. The second is
    # the CONTRAST an operator writes when they report what they observed
    # against what they expected -- "shows X while it is actually Y",
    # "says X instead of Y". Caught by the dogfood: the Work-UI occupancy
    # defect, written exactly the way a real report is written, matched none of
    # the first group and fell through to FEATURE_NEW, so the gate then asked
    # a bug for user value and an out-of-scope list. Most real reports describe
    # the behaviour, not its category.
    (BUG, ("bug", "broken", "fails", "failing", "error", "crash", "regression",
           "wrong", "incorrect", "not working", "lỗi", "hỏng", "sai", "không chạy",
           # contrast markers -- deliberately multi-word, because bare "should"
           # or "while" also appear in perfectly ordinary feature requests
           "instead of", "should be", "should show", "should say",
           "even though", "but it still", "is actually", "still shows",
           "still says", "đáng lẽ", "nhưng lại", "vẫn hiện", "thực tế là")),
    (REFACTOR, ("refactor", "restructure", "clean up", "cleanup", "tidy",
                "extract", "rename", "dedupe", "tái cấu trúc", "dọn dẹp")),
    (INTEGRATION, ("integrate", "integration", "connect to", "sync with",
                   "webhook", "third-party", "tích hợp", "kết nối")),
    (RESEARCH, ("research", "investigate", "compare", "evaluate", "spike",
                "feasibility", "should we", "nghiên cứu", "khảo sát", "đánh giá")),
    (FEATURE_NEW, ("add", "new ", "build", "create", "implement", "support for",
                   "feature", "thêm", "xây dựng", "tạo", "tính năng")),
)


def infer_task_type(text: str) -> str:
    """Best guess at the task type from a request, for a planner to confirm.

    A guess, explicitly: the type decides which fields are required, so it is
    offered as a default the planner overrides rather than a verdict. When
    nothing matches, FEATURE_NEW is the safer default than BUG -- a feature
    spec asks for scope and out-of-scope, and being asked for boundaries on
    something that turns out to be a bug is cheaper than being asked for a
    root cause that does not exist.
    """
    lowered = f" {(text or '').lower()} "
    for task_type, signals in _TYPE_SIGNALS:
        if any(signal in lowered for signal in signals):
            return task_type
    return FEATURE_NEW


def plan_from_request(*, title: str, requirement: str = "",
                      task_type: str | None = None,
                      symptom: str = "", research_question: str = "",
                      changed_paths: Sequence[str] = (),
                      requested_mode: str | None = None,
                      project_id: str | None = None,
                      created_by: str | None = None,
                      **fields: Any) -> WorkSpec:
    """Start a spec from a request, with classification already applied.

    The execution mode, deploy level and risk come from `task_classifier`
    rather than being decided here, so there is exactly one place that knows
    which changes are excluded from the fast path.
    """
    resolved = task_type or infer_task_type(" ".join([title, requirement, symptom,
                                                      research_question]))
    if resolved not in TASK_TYPES:
        raise ValueError(f"unknown task_type {resolved!r}; "
                         f"expected one of {', '.join(TASK_TYPES)}")

    classification = classify(" ".join([title, requirement, symptom,
                                        research_question]),
                              changed_paths=changed_paths,
                              requested_mode=requested_mode)

    spec = WorkSpec(
        spec_id=f"spec_{uuid.uuid4().hex[:12]}",
        title=title,
        task_type=resolved,
        requirement=requirement,
        symptom=symptom,
        research_question=research_question,
        execution_mode=classification.mode,
        deploy_level=classification.deploy_level,
        risk="HIGH" if classification.mode == SAFE
             else "LOW" if classification.mode == FAST_FIX else "MEDIUM",
        project_id=project_id,
        created_by=created_by,
        created_at=_now(),
        updated_at=_now(),
    )
    for key, value in fields.items():
        if not hasattr(spec, key):
            raise ValueError(f"unknown spec field {key!r}")
        current = getattr(spec, key)
        setattr(spec, key, tuple(value) if isinstance(current, tuple) else value)
    # Bound at plan time, not read at execution time: a policy that changes
    # mid-flight must not silently redefine what a running task agreed to.
    bind_policy(spec)
    budget_for(spec)  # records file_budget/search_budget on the spec
    _scrub(spec)
    return spec


def _scrub(spec: WorkSpec) -> None:
    """A spec is long-lived and rarely re-read. A secret written into one is
    the worst place for a quiet strip to fail open, so it is REFUSED."""
    for name, value in spec.as_dict().items():
        if name == "level":
            continue
        if isinstance(value, str):
            scrub_knowledge(value, where=f"work_spec:{name}")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    scrub_knowledge(item, where=f"work_spec:{name}")
                elif isinstance(item, dict):
                    # subtask_dag entries. Scanned too: a subtask title is
                    # written by the same planner as everything else here.
                    for sub in item.values():
                        if isinstance(sub, str):
                            scrub_knowledge(sub, where=f"work_spec:{name}")


# -- persistence ----------------------------------------------------------------

def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS work_specs (
            spec_id TEXT PRIMARY KEY,
            task_type TEXT NOT NULL,
            title TEXT NOT NULL,
            project_id TEXT,
            work_id TEXT,
            queue_task_id TEXT,
            parent_spec_id TEXT,
            plan_status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """)
    for statement in (
            "CREATE INDEX IF NOT EXISTS idx_work_specs_type ON work_specs(task_type)",
            "CREATE INDEX IF NOT EXISTS idx_work_specs_project ON work_specs(project_id)",
            "CREATE INDEX IF NOT EXISTS idx_work_specs_work ON work_specs(work_id)",
            "CREATE INDEX IF NOT EXISTS idx_work_specs_parent ON work_specs(parent_spec_id)"):
        connection.execute(statement)


SPEC_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: work_specs", _create_schema),
]


class WorkSpecStore:
    """Durable specs. Same store shape as `BugSpecStore` on purpose: one
    payload column so a new field costs no migration, plus the few columns
    that are actually queried."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_spec_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            apply_migrations(conn, SPEC_MIGRATIONS)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, spec: WorkSpec) -> WorkSpec:
        _scrub(spec)
        spec.updated_at = _now()
        if not spec.created_at:
            spec.created_at = spec.updated_at
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO work_specs (spec_id, task_type, title, project_id, work_id, "
                "queue_task_id, parent_spec_id, plan_status, created_at, updated_at, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(spec_id) DO UPDATE SET task_type=excluded.task_type, "
                "title=excluded.title, project_id=excluded.project_id, "
                "work_id=excluded.work_id, queue_task_id=excluded.queue_task_id, "
                "parent_spec_id=excluded.parent_spec_id, plan_status=excluded.plan_status, "
                "updated_at=excluded.updated_at, payload=excluded.payload",
                (spec.spec_id, spec.task_type, spec.title, spec.project_id, spec.work_id,
                 spec.queue_task_id, spec.parent_spec_id, spec.plan_status,
                 spec.created_at, spec.updated_at, json.dumps(spec.as_dict())))
        return spec

    def get(self, spec_id: str) -> WorkSpec | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM work_specs WHERE spec_id = ?",
                               (spec_id,)).fetchone()
        return _from_payload(row["payload"]) if row else None

    def list(self, *, task_type: str | None = None, project_id: str | None = None,
             work_id: str | None = None, limit: int = 50) -> list[WorkSpec]:
        clauses, params = [], []
        if task_type:
            clauses.append("task_type = ?")
            params.append(task_type)
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if work_id:
            clauses.append("work_id = ?")
            params.append(work_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload FROM work_specs {where} ORDER BY updated_at DESC LIMIT ?",
                (*params, limit)).fetchall()
        return [_from_payload(row["payload"]) for row in rows]

    def by_queue_task(self, queue_task_id: str) -> WorkSpec | None:
        """The spec that was planned for a given queue task, if there is one.

        Looked up on the column the spec already stores rather than by
        scanning payloads, and returning None is a real answer: plenty of
        queue tasks were never planned through a spec, and telemetry records
        them with no module or difficulty rather than with a guessed one.
        """
        if not queue_task_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM work_specs WHERE queue_task_id = ? "
                "ORDER BY updated_at DESC LIMIT 1", (queue_task_id,)).fetchone()
        return _from_payload(row["payload"]) if row else None

    def children(self, parent_spec_id: str) -> list[WorkSpec]:
        """Subtask specs of a decomposed feature, oldest first -- the order a
        DAG was planned in is the order it reads best."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM work_specs WHERE parent_spec_id = ? ORDER BY created_at",
                (parent_spec_id,)).fetchall()
        return [_from_payload(row["payload"]) for row in rows]


def _from_payload(payload: str) -> WorkSpec:
    data = json.loads(payload)
    data.pop("level", None)
    known = {f for f in WorkSpec.__dataclass_fields__}  # noqa: F821
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        if key not in known:
            continue
        default = WorkSpec.__dataclass_fields__[key].default  # noqa: F821
        cleaned[key] = tuple(value) if isinstance(default, tuple) and isinstance(value, list) \
            else value
    return WorkSpec(**cleaned)
