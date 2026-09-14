"""The planning pipeline: one ordered pass from a request to an executable spec.

WHY THE ORDER IS THE SAVING

Every piece this calls already existed and none of them were wired together:
`project_knowledge` (the map), `context_pack` (the module briefing),
`work_reuse` (what already exists), `task_classifier` (how much process),
`work_policy` (which rules), `work_spec` (the contract). An audit found
`bug_spec` and `context_pack` had no production caller at all -- reachable only
from their own tests.

Built but unwired is the same as absent, and worse to reason about: the
capability appears on a roadmap as done while every worker still starts from an
empty repository. This module is the wiring, and the ORDER is the point:

  capture     the request as written, before anything interprets it
  classify    what kind of work, and how much process it deserves
  knowledge   the map -- where this lives, and how stale that claim is
  similar     prior specs for this shape of work
  delta       what actually changed since the map was last verified
  reuse       REUSE / EXTEND / NEW, with evidence
  spec        the contract, filled from all of the above
  gate        executable, or NEEDS_REDEFINE with answerable questions

Cheapest evidence first, and each stage narrows the next. Reversing any two
means paying for the wide search before the narrow one that would have made it
unnecessary.

THE MAP IS A MAP; GIT IS THE TRUTH

`delta` runs AFTER `knowledge` deliberately. The map says where to look, then
the git delta says what has moved under it since -- so a stale claim is caught
before it reaches the spec rather than after a worker has trusted it.

DEGRADES, NEVER FAILS

A project with no knowledge map, no prior specs and no procedure registry still
gets a spec. Each stage records what it could not do, and those gaps reach the
planner as part of the result. A pipeline that raises on a missing optional
input would make the whole system unusable in exactly the projects that need it
most -- the ones with nothing indexed yet.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import work_reuse
from .work_spec import (NEEDS_REDEFINE, SPEC_READY, WorkSpec, WorkSpecStore,
                        bind_policy, gate, infer_task_type, plan_from_request)

# Stage names, as stable strings: they reach the UI, the telemetry and the MCP
# surface, and a caller may branch on them.
CAPTURE = "capture"
CLASSIFY = "classify"
KNOWLEDGE = "knowledge"
SIMILAR = "similar"
DELTA = "delta"
REUSE = "reuse"
SPEC = "spec"
GATE = "gate"

STAGE_ORDER = (CAPTURE, CLASSIFY, KNOWLEDGE, SIMILAR, DELTA, REUSE, SPEC, GATE)


@dataclass
class Stage:
    """What one stage did, and what it could not do.

    `gaps` is not decoration. A planner reading a result needs to tell "this
    module has no known issues" from "there is no knowledge map", and those
    look identical if a stage reports only its findings.
    """

    name: str
    ok: bool = True
    findings: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.name, "ok": self.ok, "findings": list(self.findings),
                "gaps": list(self.gaps), **({"detail": self.detail} if self.detail else {})}


@dataclass
class PlanningResult:
    """The whole pass, auditable stage by stage."""

    spec: WorkSpec
    status: str
    stages: list[Stage] = field(default_factory=list)
    gate_report: dict[str, Any] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.status == SPEC_READY

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "ready": self.ready,
                "spec": self.spec.as_dict(), "gate": self.gate_report,
                "stages": [s.as_dict() for s in self.stages],
                # Counted, never estimated. Token counts belong to the provider
                # and are reported by work_telemetry with their provenance; a
                # number invented here would be indistinguishable from a real
                # one and would make every efficiency claim untrustworthy.
                "counters": dict(self.counters)}


def _git(args: Sequence[str], *, cwd: str) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def plan(request: str, *, store: WorkSpecStore, task_type: str | None = None,
         knowledge: Any = None, registry: Any = None, cwd: str | None = None,
         project_id: str | None = None, created_by: str | None = None,
         changed_paths: Sequence[str] = ()) -> PlanningResult:
    """Run the pipeline once, from a raw request to a spec plus a verdict.

    The spec is SAVED whatever the verdict, including NEEDS_REDEFINE. That is
    what makes a redefine resume rather than restart: the planner adds the
    missing detail to this spec, and the work already done is still here.
    """
    cwd = cwd or os.getcwd()
    stages: list[Stage] = []
    counters = {"knowledge_hits": 0, "similar_hits": 0, "runbook_hits": 0,
                "changed_paths_seen": 0, "redefine_count": 0}

    # -- capture ---------------------------------------------------------------
    capture = Stage(CAPTURE, findings=[f"request captured ({len(request)} chars)"])
    if not request.strip():
        capture.ok = False
        capture.gaps.append("empty request")
    stages.append(capture)

    # -- classify --------------------------------------------------------------
    resolved_type = (task_type or infer_task_type(request)).upper()
    classify = Stage(CLASSIFY, findings=[f"task type {resolved_type}"
                                         + ("" if task_type else " (inferred -- confirm it)")])
    stages.append(classify)

    spec = plan_from_request(title=request.strip().splitlines()[0][:120] if request.strip()
                             else "(empty request)",
                             requirement=request.strip(),
                             task_type=resolved_type,
                             changed_paths=changed_paths,
                             project_id=project_id, created_by=created_by)
    classify.detail = {"execution_mode": spec.execution_mode,
                       "deploy_level": spec.deploy_level, "risk": spec.risk}

    # -- knowledge -------------------------------------------------------------
    know = Stage(KNOWLEDGE)
    modules = work_reuse.knowledge_candidates(spec, knowledge=knowledge)
    if knowledge is None:
        know.ok = False
        know.gaps.append("no knowledge map available for this project")
    elif not modules:
        know.gaps.append("no indexed module overlaps this request")
    for candidate in modules:
        know.findings.append(f"{candidate.ref}: {candidate.title}")
    counters["knowledge_hits"] = len(modules)
    if modules:
        spec.likely_module = spec.likely_module or modules[0].ref
        spec.relevant_modules = tuple(c.ref for c in modules)
        paths = [p for c in modules for p in (c.detail.get("paths") or ())]
        spec.likely_files = spec.likely_files or tuple(paths[:8])
        spec.knowledge_confidence = modules[0].detail.get("confidence", "LOW")
    stages.append(know)

    # -- similar prior work ----------------------------------------------------
    similar = Stage(SIMILAR)
    prior = work_reuse.similar_work(store, spec)
    counters["similar_hits"] = len(prior)
    if not prior:
        similar.gaps.append("no prior spec above the mention threshold")
    for candidate in prior:
        similar.findings.append(f"{candidate.ref} ({candidate.score:.0%}): {candidate.title}")
    stages.append(similar)

    # -- git delta: the map says where, git says what moved --------------------
    delta = Stage(DELTA)
    head = _git(["rev-parse", "HEAD"], cwd=cwd)
    if head is None:
        delta.ok = False
        delta.gaps.append("not a git repository, or git unavailable -- "
                          "the map cannot be checked against the code")
    else:
        spec.source_commit = head
        delta.findings.append(f"HEAD {head[:12]}")
        dirty = _git(["status", "--porcelain"], cwd=cwd)
        if dirty:
            changed = [line[3:] for line in dirty.splitlines() if len(line) > 3]
            counters["changed_paths_seen"] = len(changed)
            delta.findings.append(f"{len(changed)} uncommitted path(s) in the working tree")
            # The working tree outranks the commit graph: an edited file is
            # what will actually run.
            overlap = [p for p in changed if p in spec.likely_files]
            if overlap:
                spec.uncertain = (*spec.uncertain,
                                  "uncommitted edits in " + ", ".join(overlap[:4]))
                delta.findings.append("uncommitted edits overlap the files this will touch")
    stages.append(delta)

    # -- reuse -----------------------------------------------------------------
    reuse_stage = Stage(REUSE)
    analysis = work_reuse.analyse(spec, store=store, knowledge=knowledge, registry=registry)
    counters["runbook_hits"] = len(analysis["candidates"]["runbooks"])
    work_reuse.apply_to_spec(spec, analysis)
    reuse_stage.findings.append(f"{analysis['verdict']}: {analysis['why']}")
    reuse_stage.gaps.extend(g for g in analysis["searched"] if g.startswith("0 "))
    reuse_stage.detail = {"verdict": analysis["verdict"], "searched": analysis["searched"]}
    stages.append(reuse_stage)

    # -- spec ------------------------------------------------------------------
    bind_policy(spec, cwd=cwd)
    spec_stage = Stage(SPEC, findings=[f"spec {spec.spec_id} at level {spec.level()}"])
    if spec.policy_version:
        spec_stage.findings.append(f"policy {spec.policy_version} ({spec.policy_hash})")
    else:
        spec_stage.gaps.append("no work policy bound")
    stages.append(spec_stage)

    # -- gate ------------------------------------------------------------------
    report = gate(spec)
    gate_stage = Stage(GATE, ok=report["ready"],
                       findings=[f"{report['status']} at {report['score']:.0%} "
                                 f"(needs {report['threshold']:.0%})"])
    if not report["ready"]:
        gate_stage.gaps = list(report["missing"])
        spec.redefine_reason = (
            f"completeness {report['score']:.0%} < {report['threshold']:.0%}; "
            f"blocking: {', '.join(report['mandatory_missing']) or 'none'}")
        spec.redefine_missing = tuple(report["missing"])
        spec.redefine_count += 1
        counters["redefine_count"] = spec.redefine_count
    stages.append(gate_stage)

    # Saved whatever the verdict: a redefine must RESUME this spec, not start a
    # new one, or the work already done is thrown away with it.
    store.save(spec)

    return PlanningResult(spec=spec,
                          status=SPEC_READY if report["ready"] else NEEDS_REDEFINE,
                          stages=stages, gate_report=report, counters=counters)


def redefine(store: WorkSpecStore, spec_id: str, fields: dict[str, Any]) -> PlanningResult:
    """Add the missing detail to an existing spec and re-gate it.

    The same spec, the same id, the same queue task: this is a resume. The
    redefine history stays on the spec so "how many rounds did this take" is
    answerable, which is the number that tells you whether the planner is
    actually learning the shape of this project.
    """
    spec = store.get(spec_id)
    if spec is None:
        raise KeyError(spec_id)

    for key, value in (fields or {}).items():
        if not hasattr(spec, key):
            raise ValueError(f"unknown spec field {key!r}")
        current = getattr(spec, key)
        setattr(spec, key, tuple(value)
                if isinstance(current, tuple) and isinstance(value, list) else value)

    report = gate(spec)
    if report["ready"]:
        # Cleared, but the count stays: it is the honest record of what this
        # spec cost to get right.
        spec.redefine_reason = ""
        spec.redefine_missing = ()
    else:
        spec.redefine_reason = (
            f"completeness {report['score']:.0%} < {report['threshold']:.0%}; "
            f"blocking: {', '.join(report['mandatory_missing']) or 'none'}")
        spec.redefine_missing = tuple(report["missing"])
        spec.redefine_count += 1
    store.save(spec)

    stage = Stage(GATE, ok=report["ready"],
                  findings=[f"{report['status']} after redefine round "
                            f"{spec.redefine_count}"],
                  gaps=list(report["missing"]))
    return PlanningResult(spec=spec,
                          status=SPEC_READY if report["ready"] else NEEDS_REDEFINE,
                          stages=[stage], gate_report=report,
                          counters={"redefine_count": spec.redefine_count})
