"""What a task was actually asked for, in a form another component can check.

THE GAP THIS FILLS

`docs/MISS_TASK_ROOT_CAUSE.md` traces a task that reached COMPLETED with an
acceptance criterion silently dropped. The load-bearing finding was RC3: a
queue task has `prompt` (free text) and `metadata` (opaque JSON), so the
sentence "requirement R3 was not delivered" has nowhere to live. A gate cannot
enforce a fact the schema cannot express.

This module gives that sentence somewhere to live, and RC2 something to check:
completion stops asking "is there evidence?" and starts asking "does the
evidence cover what was asked?"

THREE NOUNS

  Requirement        one checkable thing, with a STABLE id. The id is what
                     survives amendments, evidence and reporting.
  RequirementContract  an append-only chain of versions. v1 is the original
                     prompt's requirements; each amendment adds a version.
                     Nothing is ever overwritten -- the reported failure was
                     an amendment being lost, so superseding by mutation is
                     the one thing this must not permit.
  EvidenceMatrix     requirement_id -> covered | partial | missing | waived,
                     with the evidence that says so, and the contract version
                     it was reconciled against.

PURE ON PURPOSE

No database, no config, no clock beyond an injected timestamp. The same
property `analysis_gate.py` relies on: exhaustively unit-testable, and safe to
call inside an open transaction.

DETERMINISTIC FIRST

`reconcile` compares required ids against covered ids. That comparison is the
hard gate. `Detector` exists so an LLM reviewer can ADD findings later, but a
detector returning nothing can never turn a failing gate into a passing one --
see `reconcile`'s own contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

SCHEMA_VERSION = 1
"""Shape of the serialised contract itself, distinct from a contract's own
`contract_version` (which counts amendments)."""

# Requirement kinds. `constraint` and `deliverable` are held separately from
# `acceptance` because they are reported differently, not because the gate
# treats them differently -- a required constraint is as blocking as a
# required acceptance criterion.
KIND_ACCEPTANCE = "acceptance"
KIND_CONSTRAINT = "constraint"
KIND_DELIVERABLE = "deliverable"
KINDS = (KIND_ACCEPTANCE, KIND_CONSTRAINT, KIND_DELIVERABLE)

SOURCE_ORIGINAL = "original"
SOURCE_AMENDMENT = "amendment"

# Evidence statuses.
COVERED = "covered"
PARTIAL = "partial"
MISSING = "missing"
WAIVED = "waived"
STATUSES = (COVERED, PARTIAL, MISSING, WAIVED)
SATISFYING_STATUSES = frozenset({COVERED, WAIVED})

# Gate reasons.
VERIFIED = "VERIFIED"
NO_CONTRACT = "NO_CONTRACT"
STALE_CONTRACT_VERSION = "STALE_CONTRACT_VERSION"
REQUIREMENTS_NOT_COVERED = "REQUIREMENTS_NOT_COVERED"
INVALID_WAIVER = "INVALID_WAIVER"
UNKNOWN_STATUS = "UNKNOWN_STATUS"
UNKNOWN_REQUIREMENT = "UNKNOWN_REQUIREMENT"
DETECTOR_FINDING = "DETECTOR_FINDING"


class ContractError(ValueError):
    """A malformed contract or amendment, refused rather than stored."""


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str
    kind: str = KIND_ACCEPTANCE
    required: bool = True
    source: str = SOURCE_ORIGINAL
    added_in_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "kind": self.kind,
                "required": self.required, "source": self.source,
                "added_in_version": self.added_in_version}

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "Requirement":
        rid = str(raw.get("id") or "").strip()
        if not rid:
            raise ContractError("requirement needs a non-empty stable id")
        text = str(raw.get("text") or "").strip()
        if not text:
            raise ContractError(f"requirement {rid!r} needs non-empty text")
        kind = str(raw.get("kind") or KIND_ACCEPTANCE).strip().casefold()
        if kind not in KINDS:
            raise ContractError(f"requirement {rid!r} has unknown kind {kind!r}")
        return Requirement(
            id=rid, text=text, kind=kind,
            required=bool(raw.get("required", True)),
            source=str(raw.get("source") or SOURCE_ORIGINAL),
            added_in_version=int(raw.get("added_in_version", 1)))


@dataclass(frozen=True)
class ContractVersion:
    """One link in the append-only chain. `prompt` is preserved verbatim so the
    original wording survives whatever structuring was done on top of it --
    v1 does not need perfect parsing, it needs to not lose the source."""
    version: int
    created_at: str
    source: str
    prompt: str
    requirements: tuple[Requirement, ...] = ()
    actor: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "created_at": self.created_at,
                "source": self.source, "prompt": self.prompt, "actor": self.actor,
                "requirements": [r.to_dict() for r in self.requirements]}

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "ContractVersion":
        return ContractVersion(
            version=int(raw["version"]), created_at=str(raw.get("created_at") or ""),
            source=str(raw.get("source") or SOURCE_ORIGINAL),
            prompt=str(raw.get("prompt") or ""), actor=raw.get("actor"),
            requirements=tuple(Requirement.from_dict(r)
                               for r in (raw.get("requirements") or ())))


@dataclass(frozen=True)
class RequirementContract:
    versions: tuple[ContractVersion, ...]
    schema_version: int = SCHEMA_VERSION

    @property
    def contract_version(self) -> int:
        """The latest version number. Completion reconciles against THIS."""
        return max((v.version for v in self.versions), default=0)

    def requirements(self) -> tuple[Requirement, ...]:
        """Merged across every version, in the order they were introduced.

        A later version may RESTATE an id (to correct wording); the latest
        statement wins for TEXT, but the id -- and therefore its evidence and
        its place in the gate -- is the same requirement throughout. A later
        version can never DELETE one; removal is expressed by waiving it with
        a reason and an actor, which stays on the record.
        """
        merged: dict[str, Requirement] = {}
        for version in sorted(self.versions, key=lambda v: v.version):
            for requirement in version.requirements:
                if requirement.id in merged:
                    merged[requirement.id] = replace(
                        merged[requirement.id], text=requirement.text,
                        kind=requirement.kind, required=requirement.required)
                else:
                    merged[requirement.id] = requirement
        return tuple(merged.values())

    def required_ids(self) -> tuple[str, ...]:
        return tuple(r.id for r in self.requirements() if r.required)

    def original_prompt(self) -> str:
        for version in sorted(self.versions, key=lambda v: v.version):
            return version.prompt
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version,
                "contract_version": self.contract_version,
                "versions": [v.to_dict() for v in self.versions]}

    @staticmethod
    def from_dict(raw: Mapping[str, Any] | None) -> "RequirementContract | None":
        if not raw:
            return None
        versions = raw.get("versions")
        if not versions:
            return None
        return RequirementContract(
            versions=tuple(ContractVersion.from_dict(v) for v in versions),
            schema_version=int(raw.get("schema_version", SCHEMA_VERSION)))


def create_contract(*, prompt: str, requirements: Sequence[Mapping[str, Any]] = (),
                    created_at: str, actor: str | None = None) -> RequirementContract:
    """v1 of a contract. `requirements` may be empty: a task whose prompt was
    never structured still gets a contract that preserves the prompt, and an
    empty required set means the gate has nothing to block on -- exactly the
    behaviour such a task has today."""
    parsed = tuple(Requirement.from_dict({**dict(r), "source": SOURCE_ORIGINAL,
                                          "added_in_version": 1})
                   for r in requirements)
    _refuse_duplicate_ids(parsed)
    return RequirementContract(versions=(ContractVersion(
        version=1, created_at=created_at, source=SOURCE_ORIGINAL,
        prompt=prompt, requirements=parsed, actor=actor),))


def amend_contract(contract: RequirementContract, *, prompt: str,
                   requirements: Sequence[Mapping[str, Any]] = (),
                   created_at: str, actor: str | None = None) -> RequirementContract:
    """Append a version. Never overwrites, never removes.

    The reported failure was an amendment's requirement being dropped, so the
    one shape this must refuse is anything that loses a prior version.
    """
    if contract is None:
        raise ContractError("cannot amend a task with no contract")
    next_version = contract.contract_version + 1
    parsed = tuple(Requirement.from_dict({**dict(r), "source": SOURCE_AMENDMENT,
                                          "added_in_version": next_version})
                   for r in requirements)
    existing = {r.id for r in contract.requirements()}
    fresh = [r for r in parsed if r.id not in existing]
    _refuse_duplicate_ids(tuple(fresh))
    return RequirementContract(
        versions=contract.versions + (ContractVersion(
            version=next_version, created_at=created_at, source=SOURCE_AMENDMENT,
            prompt=prompt, requirements=parsed, actor=actor),),
        schema_version=contract.schema_version)


def _refuse_duplicate_ids(requirements: tuple[Requirement, ...]) -> None:
    seen: set[str] = set()
    for requirement in requirements:
        if requirement.id in seen:
            raise ContractError(f"duplicate requirement id {requirement.id!r} "
                                f"in one version -- ids must be stable AND unique")
        seen.add(requirement.id)


@dataclass(frozen=True)
class EvidenceEntry:
    requirement_id: str
    status: str
    evidence: tuple[str, ...] = ()
    note: str | None = None
    waived_by: str | None = None
    waived_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"requirement_id": self.requirement_id, "status": self.status,
                "evidence": list(self.evidence), "note": self.note,
                "waived_by": self.waived_by, "waived_reason": self.waived_reason}

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "EvidenceEntry":
        evidence = raw.get("evidence") or ()
        if isinstance(evidence, str):
            evidence = (evidence,)
        return EvidenceEntry(
            requirement_id=str(raw.get("requirement_id") or "").strip(),
            status=str(raw.get("status") or "").strip().casefold(),
            evidence=tuple(str(e) for e in evidence),
            note=raw.get("note"), waived_by=raw.get("waived_by"),
            waived_reason=raw.get("waived_reason"))


@dataclass(frozen=True)
class EvidenceMatrix:
    entries: tuple[EvidenceEntry, ...] = ()
    reconciled_contract_version: int = 0
    produced_by: str | None = None
    produced_at: str | None = None

    def by_id(self) -> dict[str, EvidenceEntry]:
        return {e.requirement_id: e for e in self.entries}

    def to_dict(self) -> dict[str, Any]:
        return {"entries": [e.to_dict() for e in self.entries],
                "reconciled_contract_version": self.reconciled_contract_version,
                "produced_by": self.produced_by, "produced_at": self.produced_at}

    @staticmethod
    def from_dict(raw: Mapping[str, Any] | None) -> "EvidenceMatrix":
        if not raw:
            return EvidenceMatrix()
        return EvidenceMatrix(
            entries=tuple(EvidenceEntry.from_dict(e) for e in (raw.get("entries") or ())),
            reconciled_contract_version=int(raw.get("reconciled_contract_version", 0)),
            produced_by=raw.get("produced_by"), produced_at=raw.get("produced_at"))


# A deterministic (or, later, LLM-backed) extra check. Returns findings as
# (requirement_id, reason) pairs. See `reconcile` for why a detector can only
# ever ADD findings.
Detector = Callable[[RequirementContract, EvidenceMatrix], Iterable[tuple[str, str]]]


@dataclass(frozen=True)
class GateDecision:
    verified_done: bool
    reason: str
    detail: str
    contract_version: int = 0
    reconciled_version: int = 0
    missing_requirements: tuple[str, ...] = ()
    partial_requirements: tuple[str, ...] = ()
    invalid_waivers: tuple[str, ...] = ()
    unknown_requirements: tuple[str, ...] = ()
    detector_findings: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    checklist: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def blocking_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for group in (self.missing_requirements, self.partial_requirements,
                      self.invalid_waivers, tuple(f[0] for f in self.detector_findings)):
            for rid in group:
                if rid not in seen:
                    seen.append(rid)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        """The shape a tool/API returns. `checklist` and `missing_requirements`
        are the two fields a reader should be able to act on without reading
        anything else."""
        return {
            "verified_done": self.verified_done, "reason": self.reason,
            "detail": self.detail,
            "contract_version": self.contract_version,
            "reconciled_version": self.reconciled_version,
            "missing_requirements": list(self.missing_requirements),
            "partial_requirements": list(self.partial_requirements),
            "invalid_waivers": list(self.invalid_waivers),
            "unknown_requirements": list(self.unknown_requirements),
            "detector_findings": [{"requirement_id": r, "reason": why}
                                  for r, why in self.detector_findings],
            "blocking_requirements": list(self.blocking_ids()),
            "checklist": list(self.checklist),
        }


def reconcile(contract: RequirementContract | None, matrix: EvidenceMatrix | None, *,
              detectors: Sequence[Detector] = ()) -> GateDecision:
    """The hard gate. Deterministic, and never softened by a detector.

    A detector may only ADD findings: the required-vs-covered comparison below
    stands on its own, so a detector that returns nothing (or is not
    configured, or is an LLM that failed to answer) cannot turn a failing gate
    into a passing one. That is why the comparison is computed first and the
    detectors are folded in afterwards.
    """
    if contract is None or not contract.versions:
        # No contract means this task predates the feature. Not a failure --
        # it is the backward-compatible path, and the caller decides whether
        # an un-contracted task may complete (it may, today).
        return GateDecision(True, NO_CONTRACT,
                            "task carries no requirement contract; nothing to reconcile")

    requirements = contract.requirements()
    matrix = matrix or EvidenceMatrix()
    entries = matrix.by_id()

    missing: list[str] = []
    partial: list[str] = []
    invalid_waivers: list[str] = []
    unknown_status: list[str] = []
    checklist: list[dict[str, Any]] = []
    # Requirements the matrix makes ANY claim about (anything but `missing`).
    # Collected here, against the same normalisation the loop already applies,
    # so the staleness rule below does not have to re-derive it from raw rows.
    claimed: list[str] = []

    for requirement in requirements:
        entry = entries.get(requirement.id)
        status = entry.status if entry else MISSING
        if entry is not None and status not in STATUSES:
            # An unrecognised status is not "probably fine".
            unknown_status.append(requirement.id)
            status = MISSING
        if status != MISSING:
            claimed.append(requirement.id)
        if status == WAIVED:
            # A waiver without an actor AND a reason is not a waiver.
            if not (entry and str(entry.waived_by or "").strip()
                    and str(entry.waived_reason or "").strip()):
                invalid_waivers.append(requirement.id)
        elif status == PARTIAL and requirement.required:
            partial.append(requirement.id)
        elif status == MISSING and requirement.required:
            missing.append(requirement.id)
        checklist.append({
            "requirement_id": requirement.id, "text": requirement.text,
            "kind": requirement.kind, "required": requirement.required,
            "added_in_version": requirement.added_in_version,
            "status": status,
            "evidence": list(entry.evidence) if entry else [],
            "waived_by": entry.waived_by if entry else None,
            "waived_reason": entry.waived_reason if entry else None,
        })

    requirements_by_id = {r.id: r for r in requirements}
    unknown_requirements = tuple(sorted(set(entries) - set(requirements_by_id)))

    findings: list[tuple[str, str]] = []
    for detector in detectors:
        try:
            findings.extend((str(rid), str(why)) for rid, why in detector(contract, matrix))
        except Exception as exc:  # noqa: BLE001 -- an advisory check never decides the gate
            findings.append(("", f"detector {getattr(detector, '__name__', detector)!r} "
                                 f"failed: {exc}"))

    base = GateDecision(
        False, VERIFIED, "", contract_version=contract.contract_version,
        reconciled_version=matrix.reconciled_contract_version,
        missing_requirements=tuple(missing), partial_requirements=tuple(partial),
        invalid_waivers=tuple(invalid_waivers),
        unknown_requirements=unknown_requirements,
        detector_findings=tuple(findings), checklist=tuple(checklist))

    # Staleness is about UNTRUSTWORTHY EVIDENCE, not about a version counter.
    #
    # A bare `reconciled_contract_version != contract_version` comparison used
    # to decide this, and it pinned a finished task in VERIFYING forever
    # (queue task 4b5ecb09..., session facebook-property-claude: the agent
    # emitted its completion marker, the gate answered STALE_CONTRACT_VERSION
    # 549 times in 28 minutes naming ZERO requirements, and only a human
    # writing the row by hand ever got it out). Two shapes hit it, and neither
    # is a real failure:
    #
    #   * an amendment that adds no requirements ("v2: +0 requirement(s)") --
    #     the counter moved, nothing was newly asked;
    #   * any contract whose matrix was never written -- and nothing in this
    #     package writes one outside tests, so `reconciled_contract_version`
    #     is 0 on every real task.
    #
    # In both, the refusal named nothing (`blocking_ids() == ()`), so no
    # worker, engine or operator action could ever clear it: an unsatisfiable
    # gate on a lane-holding status is a deadlock, not a safety property.
    #
    # What the check is genuinely for is narrower: evidence that CLAIMS to
    # cover a requirement which did not yet exist when that evidence was
    # produced. That claim cannot be true, so it is refused by name. A
    # requirement added later and NOT claimed needs no special case -- the
    # ordinary comparison above already counted it as missing and will name it
    # through REQUIREMENTS_NOT_COVERED, which is the accurate reason for it.
    stale_claims = tuple(sorted(
        rid for rid in claimed
        if requirements_by_id[rid].added_in_version > matrix.reconciled_contract_version))
    if stale_claims:
        return replace(
            base, reason=STALE_CONTRACT_VERSION,
            detail=(f"evidence was reconciled against contract v"
                    f"{matrix.reconciled_contract_version} but "
                    f"{', '.join(stale_claims)} "
                    f"{'was' if len(stale_claims) == 1 else 'were'} added later, "
                    f"in contract v{contract.contract_version}"),
            missing_requirements=tuple(sorted(set(missing) | set(stale_claims))))
    if unknown_status:
        # Carried into missing_requirements so blocking_ids() names them. A
        # status nobody can read is not coverage, and on an OPTIONAL
        # requirement nothing else would have recorded the id -- leaving the
        # same "refused, but nothing named" shape that stranded the task this
        # module's staleness rule was rewritten for.
        return replace(base, reason=UNKNOWN_STATUS,
                       missing_requirements=tuple(sorted(set(missing) | set(unknown_status))),
                       detail=f"unrecognised evidence status for: {', '.join(unknown_status)}")
    if invalid_waivers:
        return replace(base, reason=INVALID_WAIVER,
                       detail=("a waiver needs both waived_by and waived_reason: "
                               f"{', '.join(invalid_waivers)}"))
    if missing or partial:
        return replace(base, reason=REQUIREMENTS_NOT_COVERED,
                       detail=("required criteria not covered: "
                               f"{', '.join(missing + partial)}"))
    if findings:
        return replace(base, reason=DETECTOR_FINDING,
                       detail="; ".join(f"{rid or '-'}: {why}" for rid, why in findings))
    return replace(base, verified_done=True, reason=VERIFIED,
                   detail=f"all {len(requirements)} requirement(s) reconciled "
                          f"against contract v{contract.contract_version}")


# -- deterministic detectors -------------------------------------------------

def detector_evidence_must_not_be_self_reported(
        contract: RequirementContract, matrix: EvidenceMatrix) -> list[tuple[str, str]]:
    """RC2, as a reusable check.

    `{"completion_marker": ...}` was the evidence that let the reported failure
    through: a string the agent emitted about itself. An entry whose only
    evidence is a bare completion marker is flagged, because it asserts
    completion rather than showing it.
    """
    out: list[tuple[str, str]] = []
    for entry in matrix.entries:
        if entry.status != COVERED:
            continue
        refs = [e.strip() for e in entry.evidence if e and e.strip()]
        if not refs:
            out.append((entry.requirement_id, "marked covered with no evidence reference"))
            continue
        if all(r.startswith("completion_marker") for r in refs):
            out.append((entry.requirement_id,
                        "only evidence is a self-reported completion marker"))
    return out
