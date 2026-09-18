"""What a finished task leaves behind, so the next one starts closer.

THE ASYMMETRY THIS FIXES

Planning reads the knowledge map on every task. Almost nothing writes to it.
So the map decays: it describes the repository as it was whenever someone last
indexed it by hand, confidence drifts to LOW across the board, and the next
planner stops trusting it and reads the code instead -- which is the cost the
map existed to remove. A read-only cache that nobody refreshes is a cache that
stops being used.

Write-back closes the loop. A task that touched `work_spec.py` and proved it
with tests knows something the map does not: which module changed, at which
commit, and what was learned. Recording that is cheap at the end of a task and
expensive to re-derive at the start of the next one.

WHAT MAKES THIS SAFE TO TRUST LATER

**Only what actually changed.** Modules are refreshed from the real git diff
between the spec's `source_commit` and HEAD, never from the spec's intentions.
A spec that planned to touch three modules and touched one must not mark three
as verified -- that would be a map claiming confidence it did not earn, which
is worse than a stale map, because a stale map is at least honestly old.

**Nothing is claimed for work that did not land.** A spec that never reached
READY, or a run with no changed paths, writes nothing and says so. The
temptation is to record the analysis anyway "since we did the thinking"; that
produces a map full of modules marked verified at a commit where nothing was
verified.

**Secrets are REFUSED, not stripped.** `scrub_knowledge` raises. A knowledge
base is long-lived and rarely audited -- the worst possible place for a quiet
strip to fail open. Name the environment variable, never its value.

**Append, never rewrite.** The narrative documents (decisions, known issues,
test and deploy maps, the feature map) are shared with every other lane and
with the humans reading them. Rewriting one to add a line is how another lane's
paragraph disappears silently -- the same failure mode as clobbering a shared
index.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .project_knowledge import SecretInKnowledge, scrub_knowledge
from .work_spec import BUG, DEPLOY, WorkSpec

# The documents a task can contribute to. Named here so a caller cannot invent
# a document nobody reads.
DECISIONS = "DECISIONS.md"
KNOWN_ISSUES = "KNOWN_ISSUES.md"
TEST_MAP = "TEST_MAP.md"
DEPLOY_MAP = "DEPLOY_MAP.md"
FEATURE_MAP = "FEATURE_MAP.md"
DATA_FLOW = "DATA_FLOW.md"

WRITEABLE_DOCUMENTS = (DECISIONS, KNOWN_ISSUES, TEST_MAP, DEPLOY_MAP,
                       FEATURE_MAP, DATA_FLOW)

# Reasons a write-back declines to record anything. Stable strings: they reach
# the planner and the UI, and "nothing was written" needs to say why.
NOT_READY = "SPEC_NOT_READY"
NO_CHANGES = "NO_CHANGED_PATHS"
NO_KNOWLEDGE = "NO_KNOWLEDGE_MAP"
NOT_A_REPO = "NOT_A_GIT_REPOSITORY"


@dataclass
class WriteBackReport:
    """What was recorded, and -- just as important -- what was not."""

    written: bool = False
    reason: str = ""
    modules_refreshed: list[str] = field(default_factory=list)
    modules_skipped: list[str] = field(default_factory=list)
    documents_appended: list[str] = field(default_factory=list)
    changed_paths: list[str] = field(default_factory=list)
    verified_commit: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"written": self.written, "reason": self.reason,
                "modules_refreshed": list(self.modules_refreshed),
                "modules_skipped": list(self.modules_skipped),
                "documents_appended": list(self.documents_appended),
                "changed_paths": list(self.changed_paths),
                "verified_commit": self.verified_commit}


def _git(args: Sequence[str], *, cwd: str) -> str | None:
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    # rstrip, never strip: `git status --porcelain` puts the status in the
    # first two COLUMNS, so a modified-but-unstaged file's line begins with a
    # space. Stripping the whole output eats that space on the first line
    # only, and the fixed-column parse below then truncates its path by one
    # character -- silently, and only for the first entry.
    return done.stdout.rstrip() if done.returncode == 0 else None


def actual_changed_paths(spec: WorkSpec, *, cwd: str) -> list[str] | None:
    """What really moved between the spec's commit and now.

    The spec's own `likely_files` is a PLAN. Using it here would let a task
    mark modules verified that it never touched, which is the one way a
    knowledge map becomes actively misleading rather than merely old.
    """
    head = _git(["rev-parse", "HEAD"], cwd=cwd)
    if head is None:
        return None
    paths: set[str] = set()
    if spec.source_commit and spec.source_commit != head:
        diff = _git(["diff", "--name-only", f"{spec.source_commit}..{head}"], cwd=cwd)
        if diff:
            paths.update(line.strip() for line in diff.splitlines() if line.strip())
    # The working tree outranks the commit graph: an edited file is what will
    # actually run, whether or not it has been committed yet.
    dirty = _git(["status", "--porcelain"], cwd=cwd)
    if dirty:
        # Slice past the two status columns and strip, rather than assuming a
        # fixed offset of three: that survives both ` M path` and `?? path`
        # even if the leading column is ever lost again.
        paths.update(line[2:].strip() for line in dirty.splitlines() if len(line) > 2)
    # The knowledge map lives in the repo it describes, so writing to it makes
    # the tree dirty. Counting that as a code change would let any write-back
    # justify itself: the act of recording would become the evidence that
    # something happened.
    return sorted(p for p in paths if p and not _is_knowledge_path(p))


def _is_knowledge_path(path: str) -> bool:
    head = str(path).strip("/").split("/", 1)[0]
    return head in (".projectflow", ".terminal-mcp")


def _touched_modules(knowledge: Any, changed: Sequence[str]) -> tuple[list[Any], list[Any]]:
    """Split the map's modules into those this change touched and those it did not."""
    touched, untouched = [], []
    for module in knowledge.module_states():
        paths = tuple(getattr(module, "paths", ()) or ())
        if any(_covers(path, changed) for path in paths):
            touched.append(module)
        else:
            untouched.append(module)
    return touched, untouched


def _covers(module_path: str, changed: Sequence[str]) -> bool:
    needle = str(module_path).strip("/")
    return any(needle == c.strip("/") or c.strip("/").startswith(needle + "/")
               or needle.startswith(c.strip("/") + "/")
               for c in changed)


def _append(knowledge: Any, document: str, lines: Iterable[str], *,
            owner: str) -> bool:
    """Add to a shared document without disturbing what is already in it."""
    body = [line for line in lines if str(line).strip()]
    if not body:
        return False
    for line in body:
        scrub_knowledge(str(line), where=f"writeback:{document}")

    existing = knowledge.document(document) or f"# {document[:-3].replace('_', ' ').title()}\n"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    block = "\n".join(f"- {line}" for line in body)
    knowledge.write_document(document, f"{existing.rstrip()}\n\n## {stamp}\n{block}\n",
                             owner=owner)
    return True


def write_back(spec: WorkSpec, *, knowledge: Any, cwd: str,
               changed_paths: Sequence[str] | None = None,
               root_cause: str = "", decisions: Sequence[str] = (),
               tests: Sequence[str] = (), deploy_proof: str = "",
               patterns: Sequence[str] = (), data_flow: str = "",
               owner: str = "worker") -> WriteBackReport:
    """Record what this task learned, against the commit it actually landed on.

    Returns a report rather than raising on the ordinary "nothing to record"
    cases, because a task that changed nothing is a normal outcome and should
    not look like a failure. A SECRET, by contrast, does raise -- see the
    module docstring.
    """
    report = WriteBackReport()

    if knowledge is None:
        report.reason = NO_KNOWLEDGE
        return report

    head = _git(["rev-parse", "HEAD"], cwd=cwd)
    if head is None:
        report.reason = NOT_A_REPO
        return report
    report.verified_commit = head

    changed = list(changed_paths) if changed_paths is not None \
        else (actual_changed_paths(spec, cwd=cwd) or [])
    report.changed_paths = changed
    if not changed:
        # Normal, and worth saying plainly: a task that changed nothing has
        # nothing to verify. Recording the analysis anyway would mark modules
        # verified at a commit where nothing was verified.
        report.reason = NO_CHANGES
        return report

    # -- module freshness, from the real diff ---------------------------------
    touched, untouched = _touched_modules(knowledge, changed)
    for module in touched:
        knowledge.record_module(module.name, paths=tuple(getattr(module, "paths", ()) or ()),
                                summary=module.summary, verified_commit=head, owner=owner)
        report.modules_refreshed.append(module.name)
    report.modules_skipped = [m.name for m in untouched]

    # -- the narrative documents ----------------------------------------------
    label = f"{spec.task_type} {spec.spec_id}: {spec.title}"

    if decisions and _append(knowledge, DECISIONS,
                             [f"{label} -- {d}" for d in decisions], owner=owner):
        report.documents_appended.append(DECISIONS)

    if spec.task_type == BUG and root_cause and _append(
            knowledge, KNOWN_ISSUES,
            [f"{label} -- root cause: {root_cause} (fixed at {head[:12]})"], owner=owner):
        report.documents_appended.append(KNOWN_ISSUES)

    if tests and _append(knowledge, TEST_MAP,
                         [f"{label} -- {t}" for t in tests], owner=owner):
        report.documents_appended.append(TEST_MAP)

    if deploy_proof and spec.task_type == DEPLOY and _append(
            knowledge, DEPLOY_MAP,
            [f"{label} -- {spec.deploy_level}: {deploy_proof}"], owner=owner):
        report.documents_appended.append(DEPLOY_MAP)

    # The feature map is what makes the NEXT feature's reuse search useful:
    # without it, "has someone built something like this" has only prior specs
    # to go on, and a spec is a plan while this is what shipped.
    feature_lines = []
    if spec.task_type != BUG:
        feature_lines.append(
            f"{label} -- {spec.requirement or spec.user_value or 'no requirement recorded'} "
            f"[{', '.join(changed[:4])}{' …' if len(changed) > 4 else ''}]")
    feature_lines.extend(f"{label} -- pattern: {p}" for p in patterns)
    if feature_lines and _append(knowledge, FEATURE_MAP, feature_lines, owner=owner):
        report.documents_appended.append(FEATURE_MAP)

    if data_flow and _append(knowledge, DATA_FLOW, [f"{label} -- {data_flow}"],
                             owner=owner):
        report.documents_appended.append(DATA_FLOW)

    knowledge.mark_indexed(commit=head, owner=owner)
    report.written = bool(report.modules_refreshed or report.documents_appended)
    if not report.written:
        report.reason = "nothing in the map matched the changed paths"
    return report


def write_back_from_result(spec: WorkSpec, *, knowledge: Any, cwd: str,
                           **kwargs: Any) -> WriteBackReport:
    """Convenience for the common case: a spec that reached READY and landed.

    Refuses on a spec that never became executable. Recording knowledge from a
    plan that was never accepted would teach the next planner that an unproven
    hypothesis is settled fact.
    """
    from .work_spec import gate

    if not gate(spec)["ready"]:
        report = WriteBackReport()
        report.reason = NOT_READY
        return report
    return write_back(spec, knowledge=knowledge, cwd=cwd, **kwargs)
