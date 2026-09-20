"""The ExecutionContract, and the structured verdict that judges it.

WHY THE CONTRACT IS IMMUTABLE

The failure this exists to make impossible: a Builder, halfway through, finds
the work harder than expected and quietly widens what "done" means -- or
narrows it. Nothing is lying; the definition simply moved, and afterwards
nobody can say whether the thing that shipped is the thing that was asked
for. Evidence collected against a definition that changed underneath it is
not evidence.

So the contract is written once, by the Planner, before any Builder starts,
and it is content-addressed. `content_hash` covers every field that says what
the work IS -- scope, exclusions, acceptance, checks, commands. Two contracts
with the same hash are the same contract no matter who made them or when. A
scope change is therefore not an edit: it is a NEW contract version, produced
by the Planner going through NEEDS_REDEFINE, with the old version still on
disk and still linked to the iterations that ran against it.

`freeze()` is what makes this real rather than a convention. Once a Builder
has started, the dataclass is frozen and the store refuses an UPDATE on that
row. A convention would be followed until the one time it mattered.

WHY "LOOKS GOOD" IS A PARSE ERROR

An evaluator that answers in prose cannot be checked, compared or replayed,
and prose is exactly what a model produces when it has not actually looked.
`parse_verdict` demands a result from a closed set and one CriterionResult per
declared acceptance criterion, each with its own evidence. A verdict missing a
criterion is rejected with the list of what is missing -- which is a far more
useful thing to hand back to an evaluator than a silent pass.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .harness_state import (BLOCKED_VERDICT, FAIL, NEEDS_REDEFINE_VERDICT, PASS,
                            VERDICTS)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class ContractImmutable(ValueError):
    """An attempt to change a contract a Builder has already started against."""

    def __init__(self, contract_id: str, field_name: str) -> None:
        self.contract_id = contract_id
        self.field_name = field_name
        super().__init__(
            f"contract {contract_id} is frozen; {field_name} can only change "
            f"through a Planner redefine that creates a new version")


class InsufficientSpecification(ValueError):
    """The task definition cannot support a contract. Becomes NEEDS_REDEFINE.

    Deliberately raised at PLANNING rather than discovered at EVALUATING: a
    run with no acceptance criterion cannot fail, which means it also cannot
    pass, and finding that out after a Builder has spent an hour is the
    expensive version of the same conversation.
    """

    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = list(missing)
        super().__init__("insufficient specification: " + ", ".join(missing))


def _normalise(values: Iterable[Any] | None) -> tuple[str, ...]:
    if not values:
        return ()
    if isinstance(values, (str, bytes)):
        return (str(values).strip(),) if str(values).strip() else ()
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if text:
            out.append(text)
    return tuple(out)


#: The fields that say what the work IS. Only these are hashed.
CONTENT_FIELDS: tuple[str, ...] = (
    "task_id", "scope", "out_of_scope", "affected_areas", "dependencies",
    "functional_acceptance", "visual_acceptance", "performance_acceptance",
    "security_acceptance", "required_checks", "manual_checks",
    "dev_command", "test_command", "build_command",
)


@dataclass(frozen=True)
class ExecutionContract:
    """One immutable, versioned statement of what this run must achieve."""

    id: str
    run_id: str
    task_id: str
    version: int = 1
    scope: str = ""
    out_of_scope: tuple[str, ...] = ()
    affected_areas: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    functional_acceptance: tuple[str, ...] = ()
    visual_acceptance: tuple[str, ...] = ()
    performance_acceptance: tuple[str, ...] = ()
    security_acceptance: tuple[str, ...] = ()
    required_checks: tuple[str, ...] = ()
    manual_checks: tuple[str, ...] = ()
    dev_command: str | None = None
    test_command: str | None = None
    build_command: str | None = None
    created_at: str = field(default_factory=iso_now)
    planner_agent: str | None = None
    frozen: bool = False

    # -- identity ------------------------------------------------------------
    def content(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in CONTENT_FIELDS:
            value = getattr(self, name)
            out[name] = list(value) if isinstance(value, tuple) else value
        return out

    @property
    def content_hash(self) -> str:
        blob = json.dumps(self.content(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # -- acceptance ----------------------------------------------------------
    def criteria(self) -> tuple[tuple[str, str], ...]:
        """((kind, text), ...) -- every declared acceptance criterion, in a
        stable order. This is the exact list an Evaluator must answer, and
        the exact list `parse_verdict` checks a verdict against."""
        out: list[tuple[str, str]] = []
        for kind, values in (("functional", self.functional_acceptance),
                             ("visual", self.visual_acceptance),
                             ("performance", self.performance_acceptance),
                             ("security", self.security_acceptance)):
            for text in values:
                out.append((kind, text))
        return tuple(out)

    def criterion_ids(self) -> tuple[str, ...]:
        return tuple(criterion_id(kind, text) for kind, text in self.criteria())

    # -- mutation ------------------------------------------------------------
    def freeze(self) -> "ExecutionContract":
        return replace(self, frozen=True)

    def redefine(self, **changes: Any) -> "ExecutionContract":
        """A NEW version. Never an edit -- the previous version stays on disk
        and stays linked to the iterations that already ran against it."""
        normalised = dict(changes)
        for name in ("out_of_scope", "affected_areas", "dependencies",
                     "functional_acceptance", "visual_acceptance",
                     "performance_acceptance", "security_acceptance",
                     "required_checks", "manual_checks"):
            if name in normalised:
                normalised[name] = _normalise(normalised[name])
        return replace(self, id=new_id("ctr"), version=self.version + 1,
                       created_at=iso_now(), frozen=False, **normalised)

    def require_mutable(self, field_name: str) -> None:
        if self.frozen:
            raise ContractImmutable(self.id, field_name)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id, "run_id": self.run_id, "task_id": self.task_id,
            "version": self.version, "created_at": self.created_at,
            "planner_agent": self.planner_agent, "frozen": self.frozen,
            "content_hash": self.content_hash,
        }
        data.update(self.content())
        data["criteria"] = [{"id": criterion_id(k, t), "kind": k, "text": t}
                            for k, t in self.criteria()]
        return data

    @classmethod
    def build(cls, *, run_id: str, task_id: str, scope: str,
              version: int = 1, planner_agent: str | None = None,
              **fields: Any) -> "ExecutionContract":
        """Construct with every sequence normalised and the minimum bar checked.

        The bar: a scope, and at least one acceptance criterion, and at least
        one required check. A contract without all three describes work whose
        completion is not a decidable question.
        """
        kwargs: dict[str, Any] = {}
        for name in ("out_of_scope", "affected_areas", "dependencies",
                     "functional_acceptance", "visual_acceptance",
                     "performance_acceptance", "security_acceptance",
                     "required_checks", "manual_checks"):
            kwargs[name] = _normalise(fields.get(name))
        for name in ("dev_command", "test_command", "build_command"):
            value = fields.get(name)
            kwargs[name] = str(value).strip() if value else None

        missing: list[str] = []
        if not str(scope or "").strip():
            missing.append("scope")
        if not (kwargs["functional_acceptance"] or kwargs["visual_acceptance"]
                or kwargs["performance_acceptance"] or kwargs["security_acceptance"]):
            missing.append("acceptance criteria")
        if not kwargs["required_checks"]:
            missing.append("required checks")
        if missing:
            raise InsufficientSpecification(missing)

        return cls(id=fields.get("id") or new_id("ctr"), run_id=run_id,
                   task_id=task_id, version=version, scope=str(scope).strip(),
                   planner_agent=planner_agent,
                   created_at=fields.get("created_at") or iso_now(),
                   frozen=bool(fields.get("frozen")), **kwargs)


def criterion_id(kind: str, text: str) -> str:
    """Stable id for one acceptance criterion, so an Evaluator's answer can be
    matched to the criterion it answers without relying on list order."""
    digest = hashlib.sha256(f"{kind}\x1f{text}".encode("utf-8")).hexdigest()
    return f"{kind}:{digest[:12]}"


# -- verdicts -----------------------------------------------------------------

class MalformedVerdict(ValueError):
    """An evaluator answered in a way that cannot be checked."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class CriterionResult:
    criterion_id: str
    kind: str
    text: str
    result: str
    evidence: str
    artifact: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"criterion_id": self.criterion_id, "kind": self.kind,
                "text": self.text, "result": self.result,
                "evidence": self.evidence, "artifact": self.artifact}


@dataclass(frozen=True)
class EvaluationResult:
    """One Evaluator's structured answer about one result commit."""

    id: str
    run_id: str
    iteration: int
    contract_id: str
    contract_hash: str
    result: str
    criteria: tuple[CriterionResult, ...]
    evaluator_agent: str | None = None
    evaluator_session_id: str | None = None
    result_commit: str | None = None
    summary: str = ""
    failure_class: str | None = None
    created_at: str = field(default_factory=iso_now)

    @property
    def failed_criteria(self) -> tuple[CriterionResult, ...]:
        return tuple(c for c in self.criteria if c.result != PASS)

    def feedback(self) -> dict[str, Any]:
        """Exactly what the next Builder revision is told to fix: the failed
        criteria and nothing else. Not the whole contract again, and never the
        Evaluator's prose -- a revision that re-reads everything re-does
        everything, which is how a targeted fix becomes a rewrite."""
        return {
            "run_id": self.run_id,
            "iteration": self.iteration,
            "contract_hash": self.contract_hash,
            "fix_only": [c.to_dict() for c in self.failed_criteria],
            "already_passing": [c.criterion_id for c in self.criteria if c.result == PASS],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "run_id": self.run_id, "iteration": self.iteration,
            "contract_id": self.contract_id, "contract_hash": self.contract_hash,
            "result": self.result, "summary": self.summary,
            "evaluator_agent": self.evaluator_agent,
            "evaluator_session_id": self.evaluator_session_id,
            "result_commit": self.result_commit,
            "failure_class": self.failure_class,
            "created_at": self.created_at,
            "criteria": [c.to_dict() for c in self.criteria],
        }


#: Answers that are not answers. An evaluator returning one of these as its
#: whole verdict is treated as not having evaluated at all.
_NON_ANSWERS = frozenset({"looks good", "lgtm", "ok", "fine", "seems fine",
                          "works", "all good", "good", "done"})


def parse_verdict(payload: Any, contract: ExecutionContract, *, iteration: int,
                  evaluator_agent: str | None = None,
                  evaluator_session_id: str | None = None,
                  result_commit: str | None = None) -> EvaluationResult:
    """Structured payload -> EvaluationResult, or MalformedVerdict.

    Every declared criterion must be answered, each with its own result and
    its own evidence. The overall `result` is then CHECKED against the
    per-criterion results rather than trusted: an evaluator that reports
    `pass` while marking a criterion failed is contradicting itself, and the
    per-criterion detail is the part that carries evidence.
    """
    problems: list[str] = []
    if isinstance(payload, str):
        if payload.strip().lower() in _NON_ANSWERS:
            raise MalformedVerdict([
                f"{payload.strip()!r} is not a verdict: every acceptance "
                f"criterion needs its own result and evidence"])
        raise MalformedVerdict(["verdict must be a structured object, not prose"])
    if not isinstance(payload, dict):
        raise MalformedVerdict([f"verdict must be an object, got {type(payload).__name__}"])

    declared = {criterion_id(kind, text): (kind, text) for kind, text in contract.criteria()}
    raw_criteria = payload.get("criteria")
    if not isinstance(raw_criteria, (list, tuple)) or not raw_criteria:
        raise MalformedVerdict(
            ["verdict.criteria must list one result per acceptance criterion; "
             f"expected {len(declared)}"])

    seen: dict[str, CriterionResult] = {}
    for index, entry in enumerate(raw_criteria):
        if not isinstance(entry, dict):
            problems.append(f"criteria[{index}] is not an object")
            continue
        cid = str(entry.get("criterion_id") or "").strip()
        if not cid:
            kind = str(entry.get("kind") or "functional")
            text = str(entry.get("text") or "").strip()
            if text:
                cid = criterion_id(kind, text)
        if cid not in declared:
            problems.append(
                f"criteria[{index}] answers {cid or '(unidentified)'!r}, which the "
                f"contract does not declare")
            continue
        result = str(entry.get("result") or "").strip().lower()
        if result not in (PASS, FAIL, BLOCKED_VERDICT):
            problems.append(f"criteria[{index}].result must be pass|fail|blocked, got {result!r}")
            continue
        evidence = str(entry.get("evidence") or "").strip()
        if not evidence:
            problems.append(f"criteria[{index}] ({cid}) has no evidence")
            continue
        if evidence.lower() in _NON_ANSWERS:
            problems.append(f"criteria[{index}] ({cid}) evidence {evidence!r} is not evidence")
            continue
        kind, text = declared[cid]
        seen[cid] = CriterionResult(criterion_id=cid, kind=kind, text=text,
                                    result=result, evidence=evidence,
                                    artifact=(str(entry["artifact"]) if entry.get("artifact") else None))

    unanswered = [cid for cid in declared if cid not in seen]
    if unanswered:
        problems.append("unanswered criteria: " + ", ".join(sorted(unanswered)))
    if problems:
        raise MalformedVerdict(problems)

    stated = str(payload.get("result") or "").strip().lower()
    computed = _result_from_criteria(tuple(seen.values()))
    if stated and stated not in VERDICTS:
        raise MalformedVerdict([f"result must be one of {', '.join(VERDICTS)}, got {stated!r}"])
    if stated == NEEDS_REDEFINE_VERDICT:
        # The one verdict the evaluator may assert against a clean sweep: the
        # contract itself is wrong, which no per-criterion result can express.
        computed = NEEDS_REDEFINE_VERDICT
    elif stated and stated != computed:
        raise MalformedVerdict([
            f"verdict says {stated!r} but the per-criterion results say {computed!r}"])

    return EvaluationResult(
        id=new_id("evl"), run_id=contract.run_id, iteration=iteration,
        contract_id=contract.id, contract_hash=contract.content_hash,
        result=computed, criteria=tuple(seen[cid] for cid in declared if cid in seen),
        evaluator_agent=evaluator_agent, evaluator_session_id=evaluator_session_id,
        result_commit=result_commit, summary=str(payload.get("summary") or "").strip(),
    )


def _result_from_criteria(criteria: Sequence[CriterionResult]) -> str:
    if any(c.result == BLOCKED_VERDICT for c in criteria):
        return BLOCKED_VERDICT
    if any(c.result == FAIL for c in criteria):
        return FAIL
    return PASS


# -- request keys -------------------------------------------------------------
#: Every key the engine uses for exactly-once behaviour, in one place so the
#: shapes cannot drift between the producer and the consumer.

def run_request_key(project_id: str, task_id: str, definition_hash: str) -> str:
    return f"run:{project_id}:{task_id}:{definition_hash}"


def contract_request_key(project_id: str, task_id: str, content_hash: str) -> str:
    return f"run:{project_id}:{task_id}:{content_hash}"


def iteration_request_key(run_id: str, iteration: int) -> str:
    return f"run:{run_id}:iteration:{iteration}"


def builder_request_key(run_id: str, iteration: int) -> str:
    return f"builder:{run_id}:{iteration}"


def evaluator_request_key(run_id: str, iteration: int, commit: str | None) -> str:
    return f"evaluator:{run_id}:{iteration}:{commit or 'nocommit'}"


def definition_hash(*, project_id: str, task_id: str, mode: str, prompt: str,
                    acceptance: Sequence[Any] = (), checks: Sequence[Any] = ()) -> str:
    """The pre-contract identity of a run.

    A run is started before its contract exists (the Planner writes that), so
    exactly-once at START has to key on the DEFINITION the start was made
    from. Once the contract exists the run also carries
    `contract_request_key`, and the two together are what make a repeated
    start idempotent whether it repeats before or after planning.
    """
    blob = json.dumps({
        "project_id": project_id, "task_id": task_id, "mode": mode,
        "prompt": prompt, "acceptance": list(_normalise(acceptance)),
        "checks": list(_normalise(checks)),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]
