"""Bug Execution Spec -- the analysis, done once, handed to whoever executes.

THE WASTE THIS REMOVES

A planner reads the report, the knowledge map and the git delta, and works out
where the bug probably lives. Then the executing worker receives a chat
transcript and does the whole analysis again from an empty repository. The
second pass is not verification -- it is repetition, and it is the single
largest avoidable cost in a two-agent workflow.

A spec is that analysis, persisted, compact, and attached to the queued task.
The worker receives the spec instead of the conversation.

THE CONTRACT THAT KEEPS IT SAFE

A spec is a HYPOTHESIS, never an instruction to be followed blindly. The
worker's first act is a SHORT plan verification -- current HEAD, do the named
files and symbols still exist, read the relevant functions, compare the code
against the suspected cause -- and it answers with exactly one of:

  PLAN_CONFIRMED   implement now
  PLAN_ADJUSTED    correct the small thing that moved, then implement
  PLAN_MISMATCH    the hypothesis is wrong; investigate around the named
                   module only, and widen further only on evidence

That third outcome is why this is safe to trust. A spec that is wrong costs a
short verification, not a wrong fix.

LEVELS SAY HOW MUCH IS ACTUALLY KNOWN

  L1 EXACT_FIX               files, cause and strategy are high confidence
  L2 DIRECTED_INVESTIGATION  module and flow known, one or two hypotheses
  L3 UNKNOWN                 symptom and repro only

The level is derived from the evidence present, never asserted: a spec that
names no files cannot be L1 however confident its prose sounds.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .project_knowledge import SecretInKnowledge, scrub_knowledge
from .schema import Migration, apply_migrations
from .task_classifier import (FAST_FIX, LARGE, MEDIUM, NORMAL, SAFE, SMALL,
                              classify)

L1 = "L1_EXACT_FIX"
L2 = "L2_DIRECTED_INVESTIGATION"
L3 = "L3_UNKNOWN"

PLAN_CONFIRMED = "PLAN_CONFIRMED"
PLAN_ADJUSTED = "PLAN_ADJUSTED"
PLAN_MISMATCH = "PLAN_MISMATCH"
PLAN_PENDING = "PLAN_PENDING"

BUG_TYPES = ("UI_ONLY", "FRONTEND_LOGIC", "API", "BACKEND", "DB", "AUTH", "INFRA")
RISKS = ("LOW", "MEDIUM", "HIGH")
CONFIDENCE = ("HIGH", "MEDIUM", "LOW")

SPEC_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: bug_specs", lambda connection: None),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_spec_db_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_BUGSPEC_DB")
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return base / "terminal-mcp" / "bug_specs.db"


@dataclass
class BugSpec:
    """One bug's analysis. Every field is cheap to carry and safe to persist."""

    bug_id: str
    title: str
    user_symptom: str
    expected_behavior: str = ""
    bug_type: str = "BACKEND"
    execution_mode: str = NORMAL
    risk: str = "MEDIUM"
    knowledge_confidence: str = "LOW"
    likely_module: str | None = None
    likely_files: tuple[str, ...] = ()
    entry_points: tuple[str, ...] = ()
    search_terms: tuple[str, ...] = ()
    relevant_flow: str = ""
    suspected_root_cause: str = ""
    root_cause_confidence: str = "LOW"
    fix_strategy: tuple[str, ...] = ()
    do_not_touch: tuple[str, ...] = ()
    regression_areas: tuple[str, ...] = ()
    # Runbook IDs, not command strings: the registry owns the command, and
    # copying it here would let the two diverge silently.
    test_runbook: str | None = None
    smoke_runbook: str | None = None
    deploy_runbook: str | None = None
    deploy_level: str = "PREVIEW"
    acceptance_criteria: tuple[str, ...] = ()
    source_commit: str | None = None
    knowledge_last_verified_commit: str | None = None
    # Uncertainty is recorded, never smoothed over. A planner that is unsure
    # says so here instead of inventing a path.
    uncertain: tuple[str, ...] = ()
    project_id: str | None = None
    work_id: str | None = None
    queue_task_id: str | None = None
    plan_status: str = PLAN_PENDING
    plan_note: str = ""
    difficulty: str = "MEDIUM"
    difficulty_confidence: str = "MEDIUM"
    difficulty_reasons: tuple[str, ...] = ()
    # A developer's answer is GUIDANCE, recorded with provenance. The code
    # stays the source of truth: a hint that turns out to be wrong is
    # corrected in the spec rather than quietly believed.
    human_hints: tuple[str, ...] = ()
    human_assist_status: str = "NOT_NEEDED"
    created_by: str | None = None
    created_at: str = ""
    updated_at: str = ""

    # -- derived ------------------------------------------------------------

    def level(self) -> str:
        """How much is actually known -- derived from evidence, not asserted.

        A spec naming no files cannot be L1 however confident its prose is.
        This is the check that stops a planner talking itself into certainty.
        """
        has_files = bool(self.likely_files)
        has_cause = bool(self.suspected_root_cause.strip())
        has_strategy = bool(self.fix_strategy)
        if (has_files and has_cause and has_strategy
                and self.root_cause_confidence == "HIGH"):
            return L1
        if self.likely_module or has_files or self.entry_points:
            return L2
        return L3

    def as_dict(self) -> dict[str, Any]:
        return {
            "bug_id": self.bug_id, "title": self.title, "user_symptom": self.user_symptom,
            "expected_behavior": self.expected_behavior, "type": self.bug_type,
            "execution_mode": self.execution_mode, "risk": self.risk,
            "knowledge_confidence": self.knowledge_confidence,
            "likely_module": self.likely_module, "likely_files": list(self.likely_files),
            "entry_points": list(self.entry_points), "search_terms": list(self.search_terms),
            "relevant_flow": self.relevant_flow,
            "suspected_root_cause": self.suspected_root_cause,
            "root_cause_confidence": self.root_cause_confidence,
            "fix_strategy": list(self.fix_strategy), "do_not_touch": list(self.do_not_touch),
            "regression_areas": list(self.regression_areas),
            "test_runbook": self.test_runbook, "smoke_runbook": self.smoke_runbook,
            "deploy_runbook": self.deploy_runbook, "deploy_level": self.deploy_level,
            "acceptance_criteria": list(self.acceptance_criteria),
            "source_commit": self.source_commit,
            "knowledge_last_verified_commit": self.knowledge_last_verified_commit,
            "uncertain": list(self.uncertain), "project_id": self.project_id,
            "work_id": self.work_id, "queue_task_id": self.queue_task_id,
            "plan_status": self.plan_status, "plan_note": self.plan_note,
            "difficulty": self.difficulty,
            "difficulty_confidence": self.difficulty_confidence,
            "difficulty_reasons": list(self.difficulty_reasons),
            "human_hints": list(self.human_hints),
            "human_assist_status": self.human_assist_status,
            "level": self.level(), "created_by": self.created_by,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    def handoff(self) -> str:
        """The compact brief a worker receives INSTEAD of a chat transcript.

        Everything here is addressable: a path to open, a term to grep, a
        hypothesis to check, a runbook to call. There is no restated
        architecture and no planner reasoning to re-read, because the worker
        does not need either to act.
        """
        lines = [
            f"BUG {self.bug_id} [{self.level()}] difficulty={self.difficulty} "
            f"mode={self.execution_mode} risk={self.risk}",
            f"TITLE: {self.title}",
            f"SYMPTOM: {self.user_symptom}",
        ]
        if self.expected_behavior:
            lines.append(f"EXPECTED: {self.expected_behavior}")
        if self.likely_module:
            lines.append(f"MODULE: {self.likely_module}")
        if self.likely_files:
            lines.append(f"FILES: {', '.join(self.likely_files)}")
        if self.entry_points:
            lines.append(f"ENTRY: {', '.join(self.entry_points)}")
        if self.search_terms:
            lines.append(f"SEARCH: {', '.join(self.search_terms)}")
        if self.relevant_flow:
            lines.append(f"FLOW: {self.relevant_flow}")
        if self.suspected_root_cause:
            lines.append(f"SUSPECTED ({self.root_cause_confidence}): {self.suspected_root_cause}")
        if self.fix_strategy:
            lines.append("STRATEGY: " + " | ".join(self.fix_strategy))
        if self.do_not_touch:
            lines.append(f"DO NOT TOUCH: {', '.join(self.do_not_touch)}")
        if self.uncertain:
            lines.append(f"UNCERTAIN: {', '.join(self.uncertain)}")
        if self.human_hints:
            # Marked as developer-provided so the worker weighs it as
            # guidance and still checks it against the code.
            lines.append("DEVELOPER HINTS (guidance, verify against code): "
                         + " | ".join(self.human_hints))
        runbooks = [f"{label}={value}" for label, value in
                    (("test", self.test_runbook), ("smoke", self.smoke_runbook),
                     ("deploy", self.deploy_runbook)) if value]
        if runbooks:
            lines.append("RUNBOOKS: " + " ".join(runbooks) + " (call them; do not re-derive)")
        if self.acceptance_criteria:
            lines.append("ACCEPT: " + " | ".join(self.acceptance_criteria))
        if self.source_commit:
            lines.append(f"ANALYSED AT: {self.source_commit[:12]}")
        lines.append(
            "CONTRACT: verify the plan briefly (HEAD, files/symbols exist, read the "
            "named functions, compare against the suspected cause) and answer "
            "PLAN_CONFIRMED / PLAN_ADJUSTED / PLAN_MISMATCH. Confirmed: implement. "
            "Adjusted: correct the small thing, then implement. Mismatch: investigate "
            "around the named module only, and widen further only on evidence. "
            "Do not re-audit the repository.")
        return "\n".join(lines)


def _scrub_spec(spec: BugSpec) -> None:
    """A spec is persisted and handed between agents -- exactly the kind of
    long-lived object a pasted credential would sit in."""
    for label, value in (("symptom", spec.user_symptom), ("expected", spec.expected_behavior),
                         ("flow", spec.relevant_flow), ("cause", spec.suspected_root_cause),
                         ("plan_note", spec.plan_note)):
        scrub_knowledge(value or "", where=f"bug_spec:{label}")
    for label, values in (("strategy", spec.fix_strategy),
                          ("acceptance", spec.acceptance_criteria),
                          ("search_terms", spec.search_terms)):
        for item in values:
            scrub_knowledge(str(item), where=f"bug_spec:{label}")


def plan_from_report(*, title: str, symptom: str, expected: str = "",
                     module: str | None = None, files: Sequence[str] = (),
                     entry_points: Sequence[str] = (), search_terms: Sequence[str] = (),
                     flow: str = "", suspected_cause: str = "",
                     cause_confidence: str = "LOW", fix_strategy: Sequence[str] = (),
                     do_not_touch: Sequence[str] = (), regression_areas: Sequence[str] = (),
                     acceptance: Sequence[str] = (), uncertain: Sequence[str] = (),
                     knowledge_confidence: str = "LOW", source_commit: str | None = None,
                     knowledge_commit: str | None = None, project_id: str | None = None,
                     created_by: str | None = None,
                     requested_mode: str | None = None) -> BugSpec:
    """Build a spec, deriving mode/risk/runbooks from the same classifier the
    rest of the system uses -- so a planner cannot hand a worker a FAST_FIX
    label for something the classifier would have escalated.
    """
    classification = classify(f"{title} {symptom} {flow} {suspected_cause}",
                              changed_paths=list(files), requested_mode=requested_mode)
    bug_type = _infer_type(files, f"{title} {symptom}", classification)
    spec = BugSpec(
        bug_id=f"bug_{uuid.uuid4().hex[:12]}", title=title.strip(),
        user_symptom=symptom.strip(), expected_behavior=expected.strip(),
        bug_type=bug_type, execution_mode=classification.mode,
        risk=("LOW" if classification.mode == FAST_FIX
              else "HIGH" if classification.mode == SAFE else "MEDIUM"),
        knowledge_confidence=knowledge_confidence if knowledge_confidence in CONFIDENCE else "LOW",
        likely_module=module, likely_files=tuple(files), entry_points=tuple(entry_points),
        search_terms=tuple(search_terms), relevant_flow=flow.strip(),
        suspected_root_cause=suspected_cause.strip(),
        root_cause_confidence=cause_confidence if cause_confidence in CONFIDENCE else "LOW",
        fix_strategy=tuple(fix_strategy), do_not_touch=tuple(do_not_touch),
        regression_areas=tuple(regression_areas),
        test_runbook=classification.gate_procedure,
        smoke_runbook="smoke", deploy_runbook="deploy_restart",
        deploy_level=classification.deploy_level,
        acceptance_criteria=tuple(acceptance), source_commit=source_commit,
        knowledge_last_verified_commit=knowledge_commit, uncertain=tuple(uncertain),
        project_id=project_id, created_by=created_by,
        created_at=_now(), updated_at=_now())
    verdict = triage(title, symptom, module=module, files=files, flow=flow,
                     knowledge_confidence=knowledge_confidence,
                     has_runbook=bool(spec.test_runbook))
    spec.difficulty = verdict["difficulty"]
    spec.difficulty_confidence = verdict["confidence"]
    spec.difficulty_reasons = tuple(verdict["reasons"])
    spec.human_assist_status = (ASSIST_REQUESTED if verdict["assist_recommended"]
                                else ASSIST_NOT_NEEDED)
    _scrub_spec(spec)
    return spec


_UI_HINTS = ("dashboard.py", ".html", ".css", ".svg", "webterm_assets")


def _infer_type(files: Sequence[str], text: str, classification: Any) -> str:
    hits = {reason for reason in classification.exclusions_hit}
    if "auth/authorization" in hits or "credentials/secrets" in hits:
        return "AUTH"
    if "database/schema/migration" in hits:
        return "DB"
    if "broad infrastructure" in hits:
        return "INFRA"
    if files and all(any(hint in str(f) for hint in _UI_HINTS) for f in files):
        return "UI_ONLY" if classification.mode == FAST_FIX else "FRONTEND_LOGIC"
    if re.search(r"(?i)\b(route|endpoint|api|http)\b", text):
        return "API"
    return "BACKEND"


class BugSpecStore:
    """Durable specs, keyed to the queue task that will execute them."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_spec_db_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        with self._connection:
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS bug_specs (
                    bug_id TEXT PRIMARY KEY,
                    project_id TEXT,
                    work_id TEXT,
                    queue_task_id TEXT,
                    plan_status TEXT NOT NULL DEFAULT 'PLAN_PENDING',
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""")
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_bug_specs_task ON bug_specs(queue_task_id)")
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_bug_specs_work ON bug_specs(work_id)")
        apply_migrations(self._connection, SPEC_MIGRATIONS)

    def save(self, spec: BugSpec) -> BugSpec:
        _scrub_spec(spec)
        spec.updated_at = _now()
        with self._connection:
            self._connection.execute(
                "INSERT INTO bug_specs (bug_id, project_id, work_id, queue_task_id, "
                "plan_status, payload, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(bug_id) DO UPDATE SET project_id=excluded.project_id, "
                "work_id=excluded.work_id, queue_task_id=excluded.queue_task_id, "
                "plan_status=excluded.plan_status, payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (spec.bug_id, spec.project_id, spec.work_id, spec.queue_task_id,
                 spec.plan_status, json.dumps(spec.as_dict()), spec.created_at,
                 spec.updated_at))
        return spec

    def get(self, bug_id: str) -> BugSpec | None:
        row = self._connection.execute(
            "SELECT payload FROM bug_specs WHERE bug_id = ?", (bug_id,)).fetchone()
        return _from_payload(row["payload"]) if row else None

    def for_task(self, queue_task_id: str) -> BugSpec | None:
        row = self._connection.execute(
            "SELECT payload FROM bug_specs WHERE queue_task_id = ?",
            (queue_task_id,)).fetchone()
        return _from_payload(row["payload"]) if row else None

    def for_work(self, work_id: str) -> list[BugSpec]:
        return [_from_payload(r["payload"]) for r in self._connection.execute(
            "SELECT payload FROM bug_specs WHERE work_id = ? ORDER BY created_at",
            (work_id,))]

    def record_plan_outcome(self, bug_id: str, *, status: str, note: str = "",
                            adjusted_files: Sequence[str] = ()) -> BugSpec:
        """The worker's verification verdict, persisted.

        A MISMATCH is kept rather than overwritten: the next planner for this
        module should know its last hypothesis was wrong, which is more
        useful than a spec that quietly forgets.
        """
        if status not in (PLAN_CONFIRMED, PLAN_ADJUSTED, PLAN_MISMATCH, PLAN_PENDING):
            raise ValueError(f"unknown plan status {status!r}")
        spec = self.get(bug_id)
        if spec is None:
            raise ValueError(f"unknown bug spec {bug_id!r}")
        scrub_knowledge(note or "", where="plan_note")
        spec.plan_status = status
        spec.plan_note = note
        if adjusted_files:
            spec.likely_files = tuple(adjusted_files)
        return self.save(spec)

    def iter_recent(self, *, limit: int = 300):
        """Most recent specs first -- the scan window for similarity search."""
        for row in self._connection.execute(
                "SELECT payload FROM bug_specs ORDER BY created_at DESC LIMIT ?", (limit,)):
            yield _from_payload(row["payload"])

    def recent_for_module(self, module: str, *, limit: int = 5) -> list[BugSpec]:
        """Past bugs in one module -- what makes the SECOND bug cheaper.

        This is the history a planner folds into DEBUG_MAP so the next spec
        starts from what was already learned rather than from nothing.
        """
        out = []
        for row in self._connection.execute(
                "SELECT payload FROM bug_specs ORDER BY created_at DESC LIMIT 200"):
            spec = _from_payload(row["payload"])
            if spec.likely_module == module:
                out.append(spec)
            if len(out) >= limit:
                break
        return out

    def close(self) -> None:
        self._connection.close()


def _from_payload(payload: str) -> BugSpec:
    raw = json.loads(payload)
    return BugSpec(
        bug_id=raw["bug_id"], title=raw.get("title") or "",
        user_symptom=raw.get("user_symptom") or "",
        expected_behavior=raw.get("expected_behavior") or "",
        bug_type=raw.get("type") or "BACKEND",
        execution_mode=raw.get("execution_mode") or NORMAL,
        risk=raw.get("risk") or "MEDIUM",
        knowledge_confidence=raw.get("knowledge_confidence") or "LOW",
        likely_module=raw.get("likely_module"),
        likely_files=tuple(raw.get("likely_files") or ()),
        entry_points=tuple(raw.get("entry_points") or ()),
        search_terms=tuple(raw.get("search_terms") or ()),
        relevant_flow=raw.get("relevant_flow") or "",
        suspected_root_cause=raw.get("suspected_root_cause") or "",
        root_cause_confidence=raw.get("root_cause_confidence") or "LOW",
        fix_strategy=tuple(raw.get("fix_strategy") or ()),
        do_not_touch=tuple(raw.get("do_not_touch") or ()),
        regression_areas=tuple(raw.get("regression_areas") or ()),
        test_runbook=raw.get("test_runbook"), smoke_runbook=raw.get("smoke_runbook"),
        deploy_runbook=raw.get("deploy_runbook"),
        deploy_level=raw.get("deploy_level") or "PREVIEW",
        acceptance_criteria=tuple(raw.get("acceptance_criteria") or ()),
        source_commit=raw.get("source_commit"),
        knowledge_last_verified_commit=raw.get("knowledge_last_verified_commit"),
        uncertain=tuple(raw.get("uncertain") or ()),
        project_id=raw.get("project_id"), work_id=raw.get("work_id"),
        queue_task_id=raw.get("queue_task_id"),
        plan_status=raw.get("plan_status") or PLAN_PENDING,
        plan_note=raw.get("plan_note") or "",
        difficulty=raw.get("difficulty") or "MEDIUM",
        difficulty_confidence=raw.get("difficulty_confidence") or "MEDIUM",
        difficulty_reasons=tuple(raw.get("difficulty_reasons") or ()),
        human_hints=tuple(raw.get("human_hints") or ()),
        human_assist_status=raw.get("human_assist_status") or "NOT_NEEDED",
        created_by=raw.get("created_by"), created_at=raw.get("created_at") or "",
        updated_at=raw.get("updated_at") or "")


# ---------------------------------------------------------------------------
# Completeness gate, soft budgets, and the redefine loop.
#
# The point: a worker that receives a vague spec must ask for a better one,
# not go and earn the missing detail itself. Earning it is exactly the
# expensive re-analysis this whole layer exists to remove, and a worker doing
# it silently makes the waste invisible.
# ---------------------------------------------------------------------------

NEEDS_REDEFINE = "NEEDS_REDEFINE"
SPEC_READY = "SPEC_READY"
HIT_SOFT_LIMIT = "HIT_SOFT_LIMIT"

# What a spec must carry, and the question to ask the planner when it does
# not. Weighted because a missing root-cause hypothesis costs a worker far
# more than a missing do-not-touch list.
REQUIRED_FIELDS: tuple[tuple[str, float, str], ...] = (
    ("user_symptom", 1.5, "What exactly does the user see, and where?"),
    ("expected_behavior", 1.0, "What should happen instead?"),
    ("likely_module", 1.0, "Which module or area does this live in?"),
    ("locator", 1.5, "Which files, or which exact search terms, locate it?"),
    ("relevant_flow", 1.0, "What is the call/render flow involved?"),
    ("hypothesis", 1.5, "What is the suspected root cause, or the 1-2 hypotheses to check?"),
    ("fix_strategy", 1.0, "What is the intended fix approach?"),
    ("do_not_touch", 0.5, "What must this change NOT touch?"),
    ("test_runbook", 1.0, "Which verified test runbook proves it?"),
    ("deploy_level", 0.5, "Preview, staging, or no deploy?"),
    ("acceptance_criteria", 1.5, "How do we know it is fixed?"),
)

# A spec claiming more certainty must carry more evidence to back it.
COMPLETENESS_THRESHOLDS = {L1: 0.90, L2: 0.75, L3: 0.40}

# L3 is allowed to be vague about the cause, but never about its own limits:
# an open-ended investigation with no boundary is how a "quick look" becomes
# a repository audit.
L3_REQUIRED = ("reproduction", "search boundary", "max investigation scope")


def _present(spec: BugSpec, field_name: str) -> bool:
    if field_name == "locator":
        return bool(spec.likely_files or spec.search_terms or spec.entry_points)
    if field_name == "hypothesis":
        return bool(spec.suspected_root_cause.strip())
    if field_name == "test_runbook":
        return bool(spec.test_runbook)
    if field_name == "deploy_level":
        return bool(spec.deploy_level)
    value = getattr(spec, field_name, None)
    if isinstance(value, (tuple, list)):
        return bool(value)
    return bool(str(value or "").strip())


def completeness(spec: BugSpec) -> dict[str, Any]:
    """Score a spec against what its own claimed level requires.

    Scored against the LEVEL the spec derives, so a planner cannot lower the
    bar by claiming less confidence while still labelling the work L1 --
    `level()` is computed from the evidence, not from a field.
    """
    total = sum(weight for _, weight, _ in REQUIRED_FIELDS)
    earned = sum(weight for name, weight, _ in REQUIRED_FIELDS if _present(spec, name))
    missing = [name for name, _, _ in REQUIRED_FIELDS if not _present(spec, name)]
    questions = [question for name, _, question in REQUIRED_FIELDS
                 if not _present(spec, name)]
    score = round(earned / total, 3) if total else 0.0
    level = spec.level()
    threshold = COMPLETENESS_THRESHOLDS[level]

    l3_gaps: list[str] = []
    if level == L3:
        # The vaguest level needs the tightest boundary.
        text = " ".join([spec.user_symptom, spec.relevant_flow, spec.plan_note]).casefold()
        if "repro" not in text and not spec.acceptance_criteria:
            l3_gaps.append("reproduction")
        if not (spec.likely_module or spec.search_terms or spec.entry_points):
            l3_gaps.append("search boundary")
        if not spec.do_not_touch and not spec.regression_areas:
            l3_gaps.append("max investigation scope")

    ready = score >= threshold and not l3_gaps
    return {
        "level": level, "score": score, "threshold": threshold, "ready": ready,
        "status": SPEC_READY if ready else NEEDS_REDEFINE,
        "missing": missing + l3_gaps,
        "questions_for_planner": questions + [
            f"L3 needs an explicit {gap}." for gap in l3_gaps],
    }


# Soft budgets. Behaviour rules, not a token count -- the count is not
# reliably measurable here, and a hard cut mid-task would produce a guess,
# which costs far more than it saves.
BUDGET_RULES: dict[str, dict[str, Any]] = {
    "SMALL": {
        "read_files_before_reassess": 3,
        "rules": [
            "look up knowledge and the spec first",
            "open only the listed files plus at most a couple of directly related ones",
            "never read the repository tree or docs globally",
            "never read a passing log -- the runbook's one-line result is the result",
            "call the registered runbook instead of composing a command",
            "if the cause is still unclear when the budget is spent, return "
            "NEEDS_REDEFINE or escalate WITH A REASON -- do not keep reading",
        ]},
    "MEDIUM": {
        "read_files_before_reassess": 10,
        "rules": [
            "targeted caller/callee trace from the entry points",
            "git history scoped to the module's paths only",
            "module-scoped tests before anything wider",
            "still no repository-wide audit by default",
        ]},
    "LARGE": {
        "read_files_before_reassess": 30,
        "rules": [
            "permitted only for a genuine L3",
            "must state an investigation boundary before starting",
            "widen past the boundary only with evidence, and say what the evidence was",
        ]},
}


def budget_for(spec: BugSpec) -> dict[str, Any]:
    level = spec.level()
    profile = (SMALL if level == L1 and spec.execution_mode == FAST_FIX
               else MEDIUM if level in (L1, L2) else LARGE)
    return {"profile": profile, **BUDGET_RULES[profile]}


def gate(spec: BugSpec) -> dict[str, Any]:
    """The whole pre-flight check a worker runs before touching anything.

    Returns everything needed to either start or hand back: the verdict, what
    is missing, the short questions for the planner, the budget, and the
    compact handoff. A worker that gets NEEDS_REDEFINE has a complete reply
    to send without composing one.
    """
    report = completeness(spec)
    report["budget"] = budget_for(spec)
    report["bug_id"] = spec.bug_id
    report["execution_mode"] = spec.execution_mode
    report["handoff"] = spec.handoff() if report["ready"] else None
    if not report["ready"]:
        report["reply_to_planner"] = {
            "STATUS": NEEDS_REDEFINE,
            "BUG": spec.bug_id,
            "LEVEL": report["level"],
            "COMPLETENESS": f"{report['score']:.0%} < {report['threshold']:.0%}",
            "MISSING": report["missing"],
            "QUESTIONS_FOR_PLANNER": report["questions_for_planner"],
            "NOTE": ("not investigating further -- earning this detail here is the "
                     "re-analysis the spec exists to avoid"),
        }
    return report


def escalation(spec: BugSpec, *, why: str, known: str, missing: str,
               redefine_request: str) -> dict[str, Any]:
    """The compact report a worker sends INSTEAD of widening its search.

    Deliberately four short fields. The work already done is preserved in the
    spec and the queue task, so the planner adds detail and the SAME task
    resumes -- nothing is reset and nothing is lost.
    """
    for label, value in (("why", why), ("known", known), ("missing", missing),
                         ("redefine", redefine_request)):
        scrub_knowledge(value or "", where=f"escalation:{label}")
    return {
        "TOKEN_BUDGET_STATUS": HIT_SOFT_LIMIT,
        "BUG": spec.bug_id,
        "WHY": why,
        "WHAT_IS_KNOWN": known,
        "WHAT_IS_MISSING": missing,
        "REDEFINE_REQUEST": redefine_request,
        "TASK_CONTINUES": True,
    }


def compact_result(*, plan_status: str, root_cause: str, changed: Sequence[str],
                   test_runbook: str | None, test_passed: bool,
                   deploy_url: str | None = None) -> dict[str, Any]:
    """What a SUCCESSFUL run puts back into context. Facts, no narrative.

    No reasoning, no restated architecture, no passing log. Everything here
    is something a later reader would need; nothing here is something they
    would skim.
    """
    return {
        "PLAN": plan_status.replace("PLAN_", ""),
        "ROOT_CAUSE": (root_cause or "").strip()[:300],
        "CHANGED": list(changed),
        "TEST": f"{'PASS' if test_passed else 'FAIL'} {test_runbook or 'none'}",
        "DEPLOY": deploy_url or "none",
    }


# ---------------------------------------------------------------------------
# Difficulty triage, developer assist, and the file/search budget.
#
# Difficulty is a SEPARATE axis from execution mode. Mode says how much
# process a change needs; difficulty says how much is understood. A one-line
# auth fix is SAFE mode and EASY difficulty; a rendering bug nobody can
# reproduce is FAST_FIX-shaped and HARD. Collapsing them loses exactly the
# distinction that decides whether to ask a human.
# ---------------------------------------------------------------------------

EASY = "EASY"
MEDIUM_DIFFICULTY = "MEDIUM"
HARD = "HARD"

ASSIST_NOT_NEEDED = "NOT_NEEDED"
ASSIST_REQUESTED = "REQUESTED"
ASSIST_RECEIVED = "RECEIVED"
ASSIST_UNAVAILABLE = "UNAVAILABLE"

# Signals that a bug is genuinely hard REGARDLESS of how small the diff looks.
_HARD_SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("auth/session", re.compile(r"(?i)\b(auth|session|login|token|permission|grant)\b")),
    ("concurrency", re.compile(r"(?i)\b(race|concurren|deadlock|lock|lease|atomic|flaky)\b")),
    ("data consistency", re.compile(r"(?i)\b(inconsistent|corrupt|duplicate|lost update|drift)\b")),
    ("multi-service", re.compile(r"(?i)\b(across (?:nodes|services)|cross[- ]module|federat|remote node)\b")),
    ("unknown regression", re.compile(r"(?i)\b(used to work|worked before|regressed|suddenly|intermittent)\b")),
    ("infra/security", re.compile(r"(?i)\b(infra|systemd|tunnel|firewall|tls|certificate|csrf|xss)\b")),
)
_EASY_SIGNALS = re.compile(
    r"(?i)\b(css|style|spacing|margin|padding|align|overlap|layout|responsive|mobile"
    r"|label|wording|typo|colou?r|icon|badge|tooltip|placeholder|hidden|visib)\b")


def triage(title: str, symptom: str, *, module: str | None = None,
           files: Sequence[str] = (), flow: str = "",
           knowledge_confidence: str = "LOW",
           has_runbook: bool = True) -> dict[str, Any]:
    """EASY / MEDIUM / HARD, with the reasons and an honest confidence.

    Hard signals are checked first and are not outvoted by an easy-looking
    surface: "the button is misaligned, but only after re-login" is an auth
    bug wearing a CSS costume, and triaging it EASY is how a worker spends a
    day in the wrong file.
    """
    text = " ".join([title or "", symptom or "", flow or "", " ".join(map(str, files))])
    hard = [name for name, pattern in _HARD_SIGNALS if pattern.search(text)]
    reasons: list[str] = []
    confidence = "MEDIUM"

    if hard:
        reasons.append(f"hard signal: {', '.join(hard)}")
        # Confident it is hard: these signals are specific, and a false
        # positive only costs extra care.
        return {"difficulty": HARD, "confidence": "HIGH", "reasons": reasons,
                "hard_signals": hard,
                "assist_recommended": True,
                "assist_rationale": ("a developer who knows this area can eliminate whole "
                                     "branches that would otherwise cost a long trace")}

    easy_surface = bool(_EASY_SIGNALS.search(text))
    localized = bool(module) and bool(files)
    if easy_surface and localized and has_runbook:
        reasons += ["localized visual/text change", "module and files known",
                    "a verified runbook exists"]
        confidence = "HIGH" if knowledge_confidence == "HIGH" else "MEDIUM"
        return {"difficulty": EASY, "confidence": confidence, "reasons": reasons,
                "hard_signals": [], "assist_recommended": False, "assist_rationale": ""}

    if module or files:
        reasons.append("module known, but the cause needs tracing")
        if not localized:
            reasons.append("files not pinned down yet")
        return {"difficulty": MEDIUM_DIFFICULTY, "confidence": confidence, "reasons": reasons,
                "hard_signals": [], "assist_recommended": False, "assist_rationale": ""}

    reasons.append("neither module nor files identified; scope is unknown")
    return {"difficulty": HARD, "confidence": "LOW", "reasons": reasons,
            "hard_signals": [], "assist_recommended": True,
            "assist_rationale": "the area itself is unknown, which is the cheapest "
                                "thing for a developer to answer"}


# The developer is a developer. Questions must be worth their time: specific,
# answerable in a sentence, and each one capable of eliminating a branch.
# A vague "can you give more detail?" is banned by construction -- every
# question here names a concrete discriminator.
ASSIST_QUESTION_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("unknown regression", "Did this work before a specific commit or release? Which one?"),
    ("auth/session", "Does it fail before or after the session/auth step completes?"),
    ("multi-service", "Is the behaviour different in production than locally or on staging?"),
    ("concurrency", "Does it only happen with concurrent use, or also single-user?"),
    ("data consistency", "Is the bad state visible in storage, or only in what is rendered?"),
    ("infra/security", "Did any infrastructure change around the time it started?"),
)
MAX_ASSIST_QUESTIONS = 3


def developer_assist_request(spec: BugSpec, triage_result: dict[str, Any], *,
                             findings: Sequence[str] = (),
                             hypotheses: Sequence[str] = ()) -> dict[str, Any] | None:
    """At most three precise questions, with what is already known attached.

    Returns None when assistance would not materially help -- asking anyway
    costs a developer's attention for nothing, which is the fastest way to
    make them stop answering.

    The planner's own analysis goes out WITH the questions. A developer who
    can see the current hypotheses answers in one line; one who cannot has to
    reconstruct the context first, which defeats the purpose.
    """
    if not triage_result.get("assist_recommended"):
        return None
    questions = [question for signal, question in ASSIST_QUESTION_TEMPLATES
                 if signal in (triage_result.get("hard_signals") or [])]
    if not questions:
        questions = ["Which module or flow do you believe owns this behaviour?",
                     "Is there a known-good version or commit to compare against?"]
    return {
        "bug_id": spec.bug_id,
        "status": ASSIST_REQUESTED,
        "why_asking": triage_result.get("assist_rationale")
                      or "a targeted answer here removes a long trace",
        "current_findings": list(findings),
        "current_hypotheses": list(hypotheses) or (
            [spec.suspected_root_cause] if spec.suspected_root_cause else []),
        "questions": questions[:MAX_ASSIST_QUESTIONS],
        "if_unavailable": ("proceed on the best available evidence with the "
                           "investigation boundary already in the spec -- never block"),
    }


# Soft file/search budget. A guard, never a kill: stopping mid-task produces
# a guess, and a guess is more expensive than the reading it avoided.
FILE_SEARCH_BUDGET = {
    L1: {"max_files": 5, "max_search_rounds": 2},
    L2: {"max_files": 15, "max_search_rounds": 4},
    L3: {"max_files": 40, "max_search_rounds": 8},
}


def budget_check(spec: BugSpec, *, files_read: int, search_rounds: int) -> dict[str, Any]:
    """Has this worker spent its allowance without finding the cause?

    Answers with an ACTION rather than a number, because the useful output is
    "hand this back for a better spec" rather than "you are at 6 of 5".
    """
    limits = FILE_SEARCH_BUDGET[spec.level()]
    over_files = files_read > limits["max_files"]
    over_rounds = search_rounds > limits["max_search_rounds"]
    if not (over_files or over_rounds):
        return {"within_budget": True, "action": "continue", "limits": limits}
    exceeded = ([f"{files_read} files read (budget {limits['max_files']})"] if over_files else []) \
        + ([f"{search_rounds} search rounds (budget {limits['max_search_rounds']})"]
           if over_rounds else [])
    return {
        "within_budget": False,
        "action": NEEDS_REDEFINE if spec.level() == L1 else "escalate_with_reason",
        "limits": limits, "exceeded": exceeded,
        "note": ("an L1 spec that runs out of budget was not actually an L1 -- hand it "
                 "back for a better one rather than turning it into an investigation"
                 if spec.level() == L1 else
                 "state what widened the scope and why before reading further"),
    }
