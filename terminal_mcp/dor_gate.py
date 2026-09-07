"""Definition of Ready (DoR) -- docs/REQUIREMENTS.md §20.6 Phase A.
Pure, deterministic check (same "no ML/LLM, disclosed heuristic"
posture as coordinator.py's own gate) of whether a task has enough
declared substance to leave UNASSIGNED/Backlog at all.

Deliberately OPT-IN per task (`metadata.dor_required: true`), never a
blanket requirement on every task in this project: this project's own
Kanban/PM/Planner checkpoints already created (and this file's own test
suite continues to create) many small, simple tasks with none of these
fields declared -- retroactively requiring them on EVERY task would be
a breaking behavior change with no real safety value for a small,
opportunistic task. A caller/project that wants this rigor opts in
explicitly, same posture as `docs_exempt`/`artificial_blocker` elsewhere
in this project.

Scope cut (disclosed, not guessed at): `required_os`/`required_
capabilities` are read if present but NOT mandated by this gate --
their absence is itself a meaningful, valid declaration ("no specific
requirement"), not missing information. `dependencies` is not checked
either -- `depends_on` is already a real, always-present column (empty
is a valid "no dependencies" declaration); there is no way to
distinguish "declared none, deliberately" from "never considered" from
outside data the task itself carries, so this gate does not pretend to
enforce it. `priority` always has a real default (0) -- never
"missing". The fields THIS gate actually requires when opted in:
`title`, `acceptance_criteria`, `project`, `risk_level`.
"""
from __future__ import annotations

from typing import Any

READY = "READY"
NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"

VALID_RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")


def check_definition_of_ready(task: dict[str, Any]) -> dict[str, Any]:
    """`task` is a plain dict shaped like QueueTask.to_dict()/QueueService.
    board() row (`title`/`metadata` are read). Returns `{"status":
    READY}` or `{"status": NEEDS_CLARIFICATION, "missing_fields": [...]}`
    -- never raises, never guesses a value for a missing field."""
    metadata = task.get("metadata") or {}
    if not metadata.get("dor_required"):
        return {"status": READY, "reason": "DoR not required for this task (metadata.dor_required not set)"}

    missing: list[str] = []
    if not (task.get("title") or "").strip():
        missing.append("title")
    acceptance_criteria = metadata.get("acceptance_criteria")
    if not (acceptance_criteria.strip() if isinstance(acceptance_criteria, str) else acceptance_criteria):
        missing.append("acceptance_criteria")
    if not (metadata.get("project") or "").strip():
        missing.append("project")
    risk_level = metadata.get("risk_level")
    if not risk_level:
        missing.append("risk_level")
    elif risk_level not in VALID_RISK_LEVELS:
        missing.append(f"risk_level (must be one of {VALID_RISK_LEVELS}, got {risk_level!r})")

    if missing:
        return {"status": NEEDS_CLARIFICATION, "missing_fields": missing,
                "reason": f"DoR required but missing/invalid: {', '.join(missing)}"}
    return {"status": READY, "reason": "all required DoR fields present"}
