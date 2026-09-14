"""Find what already exists before anything new gets built.

THE FAILURE THIS PREVENTS

On a bug, the expensive mistake is re-deriving the analysis. On a FEATURE, it
is building a second implementation of something the repository already has:
a second queue, a second export path, a second way to register a route. Two
implementations of one idea do not merely cost the tokens that built the
second -- they cost every future change, because from then on both have to be
found and kept in agreement.

`context_pack.similar_bugs` already does this for bugs. It is bug-shaped all
the way down: it scores `BugSpec` on `bug_type` and `suspected_root_cause`,
which a feature does not have. So this module does the same job over the
generic `WorkSpec`, and REUSES `context_pack`'s tokenizer and Jaccard scoring
rather than growing a second notion of similarity.

WHAT IT SEARCHES

  1. prior WorkSpecs      -- has someone already planned this, or something
                             close enough that its decisions carry over
  2. the knowledge map    -- which module owns this area, and what its
                             confidence is right now
  3. the procedure registry -- an existing verified runbook beats a new script

THE ANSWER IS A DECISION, NOT A LIST

A list of maybe-related things puts the judgement back on the worker, which is
where it is most expensive. So each finding carries a verdict --

  REUSE   call it as it is
  EXTEND  it nearly fits; add to it rather than beside it
  NEW     nothing close enough, and here is what was searched

-- with the evidence behind it. `NEW` with an empty search is not a verdict,
it is an omission, so the searched-and-found-nothing case says so explicitly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from .context_pack import _jaccard, _tokens
from .work_spec import TASK_TYPES, WorkSpec, WorkSpecStore

REUSE = "REUSE"
EXTEND = "EXTEND"
NEW = "NEW"

# Above this, two specs are close enough that the earlier one's decisions
# almost certainly apply and the work should extend it rather than restart.
REUSE_THRESHOLD = 0.55
# Below this a candidate is noise: surfacing it costs the worker a read and
# teaches it nothing.
MENTION_THRESHOLD = 0.25

MAX_CANDIDATES = 5


@dataclass(frozen=True)
class Candidate:
    """One prior thing that may carry over, with its score explained."""

    kind: str                      # "spec" | "module" | "runbook"
    ref: str                       # spec_id / module name / procedure id
    title: str
    score: float
    reasons: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ref": self.ref, "title": self.title,
                "score": round(self.score, 3), "reasons": list(self.reasons),
                **({"detail": self.detail} if self.detail else {})}


def _spec_tokens(spec: WorkSpec) -> set[str]:
    """The words that describe what this work IS.

    Deliberately excludes file paths and module names: those are scored
    separately and exactly, and letting them into the bag of words makes two
    unrelated features in one big module look similar.
    """
    return _tokens(spec.title, spec.requirement, spec.problem,
                   spec.expected_outcome, spec.user_value,
                   spec.symptom, spec.research_question,
                   " ".join(spec.scope), " ".join(spec.search_terms))


def _score_spec(candidate: WorkSpec, target: WorkSpec) -> tuple[float, list[str]]:
    """How much of the candidate's planning carries over to the target."""
    reasons: list[str] = []
    score = 0.0

    if candidate.task_type == target.task_type:
        score += 0.15
        reasons.append(f"same task type ({candidate.task_type})")

    if candidate.likely_module and candidate.likely_module == target.likely_module:
        score += 0.30
        reasons.append(f"same module ({candidate.likely_module})")

    shared_files = set(candidate.likely_files) & set(target.likely_files)
    if shared_files:
        score += 0.25
        reasons.append("touches " + ", ".join(sorted(shared_files)[:3]))

    overlap = _jaccard(_spec_tokens(candidate), _spec_tokens(target))
    if overlap:
        score += 0.30 * overlap
        reasons.append(f"described similarly ({overlap:.0%} word overlap)")

    return min(score, 1.0), reasons


def similar_work(store: WorkSpecStore, target: WorkSpec, *,
                 limit: int = MAX_CANDIDATES,
                 task_types: Sequence[str] = TASK_TYPES) -> list[Candidate]:
    """Prior specs worth reading before planning this one.

    Scans across task types on purpose: the REFACTOR that reshaped a module is
    often the most useful thing to read before adding a feature to it, and
    restricting the search to the same type would hide exactly that.
    """
    candidates: list[Candidate] = []
    for task_type in task_types:
        for spec in store.list(task_type=task_type, limit=200):
            if spec.spec_id == target.spec_id:
                continue
            score, reasons = _score_spec(spec, target)
            if score < MENTION_THRESHOLD:
                continue
            candidates.append(Candidate(
                kind="spec", ref=spec.spec_id, title=spec.title,
                score=score, reasons=tuple(reasons),
                detail={"task_type": spec.task_type,
                        "module": spec.likely_module,
                        "files": list(spec.likely_files),
                        "reuse_decisions": list(spec.reuse_decisions),
                        "implementation_plan": list(spec.implementation_plan),
                        "source_commit": spec.source_commit,
                        "plan_status": spec.plan_status}))
    candidates.sort(key=lambda c: (-c.score, c.ref))
    return candidates[:limit]


# Path fragments every module in a Python project shares. Left in, they match
# everything equally and so distinguish nothing while inflating every score.
_PATH_NOISE = frozenset({
    "terminal", "mcp", "py", "src", "lib", "app", "scripts", "agent",
    "projectflow", "policies", "tests", "test", "init",
})


def _identifier_tokens(module: Any) -> set[str]:
    """The STRONG signal: a module's name and the stems of its own paths.

    `work_runtime` owning `work_service.py` and `work_loop.py` contributes
    work/runtime/service/loop -- words that name the thing rather than
    describe it. A request sharing one of these is talking about this module;
    a request sharing only prose from its summary may just be using English.
    """
    words: set[str] = set()
    for part in re.split(r"[^0-9a-zA-Z]+", str(module.name or "")):
        if part:
            words.add(part.lower())
    for path in (getattr(module, "paths", ()) or ()):
        for part in re.split(r"[^0-9a-zA-Z]+", str(path)):
            token = part.lower()
            if token and not token.isdigit():
                words.add(token)
    for topic in (getattr(module, "topics", ()) or ()):
        for part in re.split(r"[^0-9a-zA-Z]+", str(topic)):
            if part:
                words.add(part.lower())
    return words - _PATH_NOISE


def _score_module(module: Any, target_tokens: set[str]) -> tuple[float, list[str]]:
    """Composite, on the SAME scale as `_score_spec`.

    The bug this fixes: module matching used a raw Jaccard similarity and
    compared it against MENTION_THRESHOLD, which was calibrated for the
    COMPOSITE spec score (0.30 same-module + 0.25 shared-files + 0.30x text).
    Those are different units. Jaccard divides by the union of both bags, so a
    fifteen-word request against a twenty-word module description cannot reach
    0.25 even when it is unmistakably about that module -- measured on this
    repo's real map, the correct module for a real bug report scored 0.065 and
    was discarded. The knowledge stage therefore contributed nothing on real
    input while appearing to work.

    So the score is built the same way the spec score is: an identifier match
    carries most of the weight, prose overlap refines it.
    """
    identifiers = _identifier_tokens(module)
    prose = _tokens(module.name, module.summary)
    shared_ids = sorted(identifiers & target_tokens)

    score = 0.0
    reasons: list[str] = []
    if shared_ids:
        # Naming the module (or one of its files) is the strongest evidence a
        # free-text request can carry.
        score += 0.45
        reasons.append("names " + ", ".join(shared_ids[:3]))
        if len(shared_ids) > 1:
            score += 0.25
            reasons.append(f"{len(shared_ids)} identifier matches")

    # Prose is scored by OVERLAP COEFFICIENT, not Jaccard. Jaccard divides by
    # the union, so a short focused summary that genuinely describes the
    # request is punished simply for being short -- "serialises report rows to
    # CSV" against "add CSV download of report rows" shares most of what there
    # is to share and still scores near zero. The overlap coefficient asks the
    # question that actually matters: how much of the SMALLER bag matched.
    combined = prose | identifiers
    shared_prose = combined & target_tokens
    coefficient = (len(shared_prose) / min(len(combined), len(target_tokens))
                   if combined and target_tokens else 0.0)
    if coefficient >= 0.5:
        # Most of the shorter description is present in the request. That is
        # evidence in its own right, even with no identifier in common.
        score += 0.35
        reasons.append(f"description closely matches ({coefficient:.0%})")
    elif coefficient:
        score += 0.30 * coefficient
        reasons.append(f"description overlaps ({coefficient:.0%})")
    return min(score, 1.0), reasons


def knowledge_candidates(target: WorkSpec, *, knowledge: Any = None,
                         limit: int = MAX_CANDIDATES) -> list[Candidate]:
    """Modules whose recorded purpose overlaps this work.

    Uses the knowledge map's CURRENT confidence rather than a stored one, so a
    module that changed since it was indexed is offered with that fact
    attached instead of as settled truth.
    """
    if knowledge is None:
        return []
    try:
        modules = knowledge.module_states()
    except Exception:  # noqa: BLE001 -- a missing map is not a planning failure
        return []

    target_tokens = _spec_tokens(target)
    out: list[Candidate] = []
    for module in modules:
        score, reasons = _score_module(module, target_tokens)
        if score < MENTION_THRESHOLD:
            continue
        out.append(Candidate(
            kind="module", ref=module.name, title=module.summary or module.name,
            score=score,
            reasons=(*reasons,
                     f"confidence {module.confidence}: {module.confidence_reason}"),
            detail={"paths": list(getattr(module, "paths", ()) or ()),
                    "confidence": module.confidence,
                    "last_verified_commit": getattr(module, "last_verified_commit", None)}))
    out.sort(key=lambda c: (-c.score, c.ref))
    return out[:limit]


def runbook_candidates(target: WorkSpec, *, registry: Any = None,
                       limit: int = MAX_CANDIDATES) -> list[Candidate]:
    """Verified procedures that already do what this task would script.

    A VERIFIED runbook is called, not read. A STALE or BROKEN one is still
    offered -- with its state -- because knowing a procedure exists and needs
    re-verifying beats writing a second one beside it.
    """
    if registry is None:
        return []
    try:
        procedures = registry.list()
    except Exception:  # noqa: BLE001
        return []

    target_tokens = _spec_tokens(target)
    out: list[Candidate] = []
    for proc in procedures:
        # `ProcedureRegistry.list()` returns dicts carrying the procedure's own
        # fields plus a derived `status` (VERIFIED/STALE/BROKEN/UNVERIFIED).
        get = proc.get if isinstance(proc, dict) else (lambda k, d=None: getattr(proc, k, d))
        proc_id = str(get("id", "") or "")
        name = str(get("name", "") or "")
        status = str(get("status", "") or "UNVERIFIED")
        overlap = _jaccard(_tokens(proc_id, name, str(get("args_hint", "") or "")),
                           target_tokens)
        if overlap < MENTION_THRESHOLD:
            continue
        out.append(Candidate(
            kind="runbook", ref=proc_id, title=name or proc_id,
            score=overlap,
            reasons=(f"name overlaps ({overlap:.0%})",
                     # The state matters as much as the match: a VERIFIED
                     # procedure is called, a STALE one is re-verified, and
                     # either beats writing a second script beside it.
                     f"status {status}"),
            detail={"status": status, "command": list(get("command", []) or [])}))
    out.sort(key=lambda c: (-c.score, c.ref))
    return out[:limit]


def analyse(target: WorkSpec, *, store: WorkSpecStore | None = None,
            knowledge: Any = None, registry: Any = None) -> dict[str, Any]:
    """The reuse verdict for one spec, with everything it was based on.

    The verdict is deliberately conservative. REUSE is only claimed when a
    prior spec scores above the threshold, because telling a worker to reuse
    something that does not fit costs more than telling it to look.
    """
    specs = similar_work(store, target) if store is not None else []
    modules = knowledge_candidates(target, knowledge=knowledge)
    runbooks = runbook_candidates(target, registry=registry)

    searched = [f"{len(specs)} prior spec(s) above the mention threshold",
                f"{len(modules)} knowledge module(s)",
                f"{len(runbooks)} registered procedure(s)"]

    best = specs[0] if specs else None
    if best and best.score >= REUSE_THRESHOLD:
        verdict, why = EXTEND, (
            f"{best.ref} ({best.title!r}) scores {best.score:.0%}; extend it rather "
            f"than building beside it")
    elif best or modules or runbooks:
        verdict, why = REUSE, (
            "existing code/procedures cover part of this; read the candidates before "
            "writing anything new")
    else:
        verdict, why = NEW, (
            "nothing above the mention threshold in prior specs, the knowledge map or "
            "the procedure registry")

    return {
        "verdict": verdict,
        "why": why,
        # What was actually searched, so a NEW verdict is a finding rather than
        # an omission. A verdict with no search behind it is not a verdict.
        "searched": searched,
        "candidates": {
            "specs": [c.as_dict() for c in specs],
            "modules": [c.as_dict() for c in modules],
            "runbooks": [c.as_dict() for c in runbooks],
        },
        "suggested_reuse_candidates": [
            f"{c.kind}:{c.ref} -- {c.title}" for c in (*specs, *modules, *runbooks)
        ][:MAX_CANDIDATES],
    }


def apply_to_spec(target: WorkSpec, analysis: dict[str, Any]) -> WorkSpec:
    """Write the analysis onto the spec, without overwriting a human's list.

    A planner that already named its reuse candidates has said something the
    search cannot; the search fills an empty field, never replaces a filled
    one.
    """
    if not target.reuse_candidates and analysis.get("suggested_reuse_candidates"):
        target.reuse_candidates = tuple(analysis["suggested_reuse_candidates"])
    verdict_line = f"{analysis['verdict']}: {analysis['why']}"
    if verdict_line not in target.reuse_decisions:
        target.reuse_decisions = (*target.reuse_decisions, verdict_line)
    return target
