"""Pre-merge review gate for the Integration Agent (task: "3-role model
... review phải là thật, không chỉ check status: diff review, suspicious
changes, API/schema/migration impact, security/basic regression risk,
merge conflict"). Runs BEFORE integration_engine.py ever attempts a real
`git merge` -- the Integration Agent's own analogue of coordinator.py's
CoordinatorGate, same fail-closed philosophy, same "real, mechanical
checks first; a pluggable deeper-judgment hook is a disclosed, separate,
not-yet-made decision" posture (see coordinator.py's own module
docstring for the full reasoning, reused here rather than re-argued).

Decision vocabulary is deliberately narrower than CoordinatorGate's
(READY | BLOCKED | NEEDS_REWORK | NEEDS_HUMAN): a Handoff's own state
machine only has two non-READY outcomes at this stage (REWORK_REQUIRED,
BLOCKED -- integration_store.py), so this module's IntegrationReviewDecision
maps directly onto those, with no fourth bucket to invent a meaning for.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any

from .coordinator import SENSITIVE_PROMPT_PATTERNS
from .integration_store import Handoff

READY = "READY"
REWORK_REQUIRED = "REWORK_REQUIRED"
BLOCKED = "BLOCKED"

MIGRATION_PATH_MARKERS = ("/migrations/", "/migration/", "alembic/", "schema.sql", ".sql")
"""Informational only (item 5's "API/schema/migration impact") -- never
blocks by itself. Surfaced as a risk_flag in the decision's evidence so
a human/dashboard can see it, exactly the same posture as coordinator.py's
own scope-reasoner: a real judgment call about whether a given schema
change is safe is not something this mechanical check claims to make."""

REQUIREMENTS_DOC_PATH = "docs/REQUIREMENTS.md"
"""Living-requirements convention (task: "sau khi hoàn tất + verify một
feature... phải cập nhật living requirements/spec"; "Integration/merge-
test khi review phải kiểm tra docs đã được update cùng code, thiếu docs
thì trả NEEDS_REWORK"): the ONE canonical requirements file every
project this Integration Agent manages is expected to keep current --
see that file's own header for the full convention this check enforces."""

AGENT_GUIDE_DOC_PATH = "docs/CHATGPT_USAGE.md"
AGENT_FACING_PATH_MARKERS = ("terminal_mcp/mcp_app.py", "terminal_mcp/dashboard.py")
"""Comprehensive-docs checkpoint (2026-09-07): a diff touching the MCP
tool surface or the dashboard's own routes is a strong signal that
ChatGPT/agent-facing BEHAVIOR changed (a new tool, a changed return
shape, a new route) -- exactly what docs/CHATGPT_USAGE.md exists to
keep truthful. Deliberately INFORMATIONAL only (a risk_flag, never a
REWORK_REQUIRED block, same posture as MIGRATION_PATH_MARKERS above):
unlike docs/REQUIREMENTS.md's own hard gate, not every mcp_app.py/
dashboard.py touch changes something a ChatGPT caller needs to know
(an internal bugfix with no surface-shape change, e.g.), so a human/
reviewer judgment call is still needed -- this only ensures it's never
silently missed."""

_NON_BEHAVIOR_PATH_MARKERS = ("tests/", "/tests/", "test_", "docs/")
"""A changed path matching one of these is never, on its own, evidence
that this diff needs a requirements-doc update -- test-only and doc-only
changes are exempt by construction (there is no "behavior" to document
beyond what the diff already says). A diff whose changed paths are
ENTIRELY covered by these markers skips the requirements-doc check
outright; the exact same disclosed-heuristic posture as coordinator.py's
own _default_scope_reasoner -- simple and conservative, not a claim of
real code understanding."""


def _looks_like_a_pure_test_or_doc_change(changed_paths: list[str]) -> bool:
    if not changed_paths:
        return True  # nothing changed at all -- trivially nothing to document
    return all(any(marker in path for marker in _NON_BEHAVIOR_PATH_MARKERS) for path in changed_paths)


class ReviewEvidenceError(RuntimeError):
    """Raised when the diff/ancestry evidence this review needs could
    not be collected at all -- treated as fail-closed BLOCKED, never as
    'assume it's fine'."""


@dataclass(frozen=True)
class IntegrationReviewDecision:
    status: str  # READY | REWORK_REQUIRED | BLOCKED
    reason: str
    risk_flags: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason, "risk_flags": list(self.risk_flags),
               "evidence": self.evidence}


def _git(args: list[str], cwd: str, *, check: bool = False, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=check)


class IntegrationReviewGate:
    """`review()` is called ONCE per handoff, right after it's claimed
    and before any merge is attempted. `review_depth` ("basic" | "deep",
    per project pipeline config -- item 5's "Cho phép cấu hình policy
    độ sâu review"): "basic" runs the cheap, always-safe checks
    (repo/base-sha readable, working tree clean, sensitive-content
    scan); "deep" additionally computes real diff stats and migration/
    schema-impact flags. Both depths are equally fail-closed on
    evidence-collection failure -- depth only changes how MUCH is
    checked, never whether an unreadable check is treated as passing."""

    def __init__(self, *, sensitive_patterns: tuple = SENSITIVE_PROMPT_PATTERNS) -> None:
        self.sensitive_patterns = sensitive_patterns

    def review(self, handoff: Handoff, pipeline: dict[str, Any]) -> IntegrationReviewDecision:
        repo_path = pipeline["repo_path"]
        depth = pipeline.get("review_depth") or "basic"

        # 1. The commit/base itself must actually be resolvable in this
        #    repo -- fail-closed if not (item 7's own "không đọc được
        #    thì không tự dispatch" applies here identically).
        try:
            resolved_commit = _git(["rev-parse", "--verify", f"{handoff.commit_sha}^{{commit}}"], repo_path,
                                   check=True).stdout.strip()
            _git(["rev-parse", "--verify", f"{handoff.base_sha}^{{commit}}"], repo_path, check=True)
        except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
            return IntegrationReviewDecision(
                BLOCKED, reason=f"could not resolve commit_sha/base_sha in the repo -- fail-closed: {exc}",
            )

        # 2. Working tree cleanliness of the repo overall (protects
        #    against manual/out-of-band interference; the engine's own
        #    flow should never leave it dirty, so this is a genuine
        #    safety net, not routine housekeeping).
        status = _git(["status", "--porcelain"], repo_path)
        if status.stdout.strip():
            return IntegrationReviewDecision(
                BLOCKED, reason="repo working tree is not clean before merge -- fail-closed",
                evidence={"git_status": status.stdout[:1000]},
            )

        # 3. Real diff review (item 5's own "diff review"): the actual
        #    changed content between base_sha and commit_sha, not just
        #    the handoff's own self-reported changed_paths.
        diff = _git(["diff", f"{handoff.base_sha}..{handoff.commit_sha}"], repo_path)
        if diff.returncode != 0:
            return IntegrationReviewDecision(
                BLOCKED, reason=f"could not compute diff base_sha..commit_sha -- fail-closed: {diff.stderr[:300]}",
            )
        diff_text = diff.stdout

        # 4. Sensitive/suspicious content in the ACTUAL diff (reused
        #    pattern list from coordinator.py -- credentials/secrets/
        #    destructive commands accidentally committed).
        for pattern in self.sensitive_patterns:
            if pattern.search(diff_text):
                return IntegrationReviewDecision(
                    BLOCKED, reason=f"diff contains a sensitive/destructive pattern ({pattern.pattern!r})",
                    evidence={"matched_pattern": pattern.pattern},
                )

        risk_flags: list[str] = []
        evidence: dict[str, Any] = {"resolved_commit": resolved_commit}

        # 4b. Living-requirements convention (task: "Integration/merge-
        #     test khi review phải kiểm tra docs đã được update cùng
        #     code, thiếu docs thì trả NEEDS_REWORK"). Computed at BOTH
        #     review depths -- docs discipline is not something a
        #     project should be able to skip just by choosing "basic"
        #     review_depth. Exempt: a handoff whose diff touches nothing
        #     but tests/docs (nothing behavior-changing to document), or
        #     one whose originating task declared
        #     artifacts['docs_exempt'] ("refactor"/"chore" -- see
        #     publish_handoff_for_completed_task).
        name_only = _git(["diff", "--name-only", f"{handoff.base_sha}..{handoff.commit_sha}"], repo_path)
        changed_paths = [line for line in name_only.stdout.splitlines() if line.strip()]
        docs_exempt = (handoff.artifacts or {}).get("docs_exempt")
        if not docs_exempt and not _looks_like_a_pure_test_or_doc_change(changed_paths) \
                and REQUIREMENTS_DOC_PATH not in changed_paths:
            return IntegrationReviewDecision(
                REWORK_REQUIRED,
                reason=f"this diff changes behavior but does not update {REQUIREMENTS_DOC_PATH} -- "
                      f"the living-requirements convention requires updating it before this can be marked done",
                evidence={"changed_paths": changed_paths[:200]},
                risk_flags=("missing_requirements_doc_update",),
            )

        # 4c. Agent-guide currency (informational -- see AGENT_FACING_
        #     PATH_MARKERS' own docstring for why this is a risk_flag,
        #     never a block, unlike the REQUIREMENTS.md check above).
        touches_agent_facing_surface = any(
            marker in path for path in changed_paths for marker in AGENT_FACING_PATH_MARKERS)
        if not docs_exempt and touches_agent_facing_surface and AGENT_GUIDE_DOC_PATH not in changed_paths:
            risk_flags.append("missing_agent_guide_update")

        if depth == "deep":
            name_status = _git(["diff", "--name-status", f"{handoff.base_sha}..{handoff.commit_sha}"], repo_path)
            changed = [line for line in name_status.stdout.splitlines() if line.strip()]
            evidence["diff_name_status"] = changed[:200]
            if any(any(marker in line.casefold() for marker in MIGRATION_PATH_MARKERS) for line in changed):
                risk_flags.append("schema_or_migration_change")

            # Divergence check: is base_sha still an ancestor of the
            # integration branch's current tip? Informational only --
            # a normal 3-way merge already handles real divergence
            # correctly; this just surfaces it for visibility (item 5's
            # own "regression risk" awareness), never blocks by itself.
            integration_branch = pipeline["integration_branch"]
            ancestor_check = _git(["merge-base", "--is-ancestor", handoff.base_sha, integration_branch], repo_path)
            if ancestor_check.returncode != 0:
                risk_flags.append("base_diverged_from_integration_branch")

        return IntegrationReviewDecision(READY, reason="pre-merge review passed", risk_flags=tuple(risk_flags),
                                         evidence=evidence)
