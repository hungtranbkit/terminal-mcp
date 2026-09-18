"""Module context packs and similar-bug retrieval.

Two ways to make the SECOND bug in a module cheaper than the first.

A *context pack* is the small, bounded briefing a worker needs about one
module: what it is, which files it owns, how to test it, what broke here
before. It is assembled from the knowledge map and the bug history rather
than re-derived by reading source, and it is deliberately capped -- an
unbounded pack is just the repository again.

*Similar-bug retrieval* asks whether this bug has been seen before. When it
has, the previous spec is offered as a starting point with its provenance
attached, and with every path it names already checked against the current
working tree and git delta -- because a match is decided on wording, and
wording matching says nothing about whether those files still exist. It is
never applied silently: the code is still the source of truth, and a reused
spec that no longer matches it must be adjusted, which is exactly what the
worker's plan-verification step is for.

Nothing here stores secrets -- it only ever reads material that
project_knowledge and bug_spec have already scrubbed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from .bug_spec import BugSpec, BugSpecStore
from .work_telemetry_runtime import note as _note_signal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .project_knowledge import ProjectKnowledge

# A pack is a briefing, not a dossier. These caps are what keep it one.
MAX_PACK_FILES = 12
MAX_PACK_BUGS = 5
MAX_PACK_CHARS = 4000

# How many modules one task's briefing may load, and how long the whole thing
# may get. A task that names six modules has not been narrowed enough for a
# briefing to help; loading all six would hand a worker the repository again
# under a different name.
MAX_TASK_MODULES = 3
MAX_BRIEF_CHARS = 6000

# Weakest-link order. A briefing is never more trustworthy than the least
# trustworthy thing in it.
_CONFIDENCE_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

# Similarity thresholds. STRONG means "start from this spec"; RELATED means
# "read this first, it is probably informative"; below WEAK we say nothing,
# because a bad suggestion costs more attention than no suggestion.
STRONG_MATCH = 0.62
RELATED_MATCH = 0.38

REUSED = "REUSED_BUG_SPEC"
RELATED_ONLY = "RELATED_BUGS_FOUND"
NO_MATCH = "NO_SIMILAR_BUG"

# Per-path verdicts for a spec offered for reuse. A match is decided on the
# wording of a symptom, and wording matching says nothing about whether the
# files that fixed it last time are still the files to edit.
PATH_UNCHANGED = "UNCHANGED"
PATH_CHANGED = "CHANGED"
PATH_MISSING = "MISSING"
PATH_UNVERIFIED = "UNVERIFIED"

_WORD = re.compile(r"[a-z0-9_]{3,}")
# Words that appear in nearly every bug report and therefore separate nothing.
_STOPWORDS = frozenset("""
the and for with that this from not but are was were has have had you your
when then than there their they them its it's about into over under after
before while does doing done can could should would will shall may might
bug issue problem error fail fails failed failing wrong broken bad still
""".split())


def _tokens(*parts: str) -> set[str]:
    out: set[str] = set()
    for part in parts:
        if not part:
            continue
        out.update(w for w in _WORD.findall(part.lower()) if w not in _STOPWORDS)
    return out


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def fingerprint(spec: BugSpec) -> str:
    """A coarse bucket: "this kind of bug, in this place".

    Deliberately NOT wording-sensitive. An exact hash over symptom words
    cannot do fuzzy matching -- two reports of one defect phrased differently
    hash apart, so such a fingerprint would never fire and would only look
    useful. The fuzzy work belongs to :func:`_score`; this is the cheap key
    that narrows which candidates are worth scoring at all, and it is derived
    rather than stored so older specs bucket correctly too.
    """
    basis = f"{(spec.likely_module or '?').lower()}|{spec.bug_type.lower()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class SimilarBug:
    """One past bug that may inform this one, with its score explained."""

    spec: BugSpec
    score: float
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "bug_id": self.spec.bug_id,
            "title": self.spec.title,
            "module": self.spec.likely_module,
            "score": round(self.score, 3),
            "reasons": list(self.reasons),
            "root_cause": self.spec.suspected_root_cause,
            "fix_strategy": list(self.spec.fix_strategy),
            "files": list(self.spec.likely_files),
            "source_commit": self.spec.source_commit,
            "plan_status": self.spec.plan_status,
        }


def _score(candidate: BugSpec, target: BugSpec) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.0

    if candidate.likely_module and candidate.likely_module == target.likely_module:
        score += 0.34
        reasons.append(f"same module ({candidate.likely_module})")
    if candidate.bug_type == target.bug_type:
        score += 0.16
        reasons.append(f"same bug type ({candidate.bug_type})")

    shared_files = set(candidate.likely_files) & set(target.likely_files)
    if shared_files:
        score += min(0.25, 0.12 * len(shared_files))
        reasons.append(f"shared files: {', '.join(sorted(shared_files)[:3])}")

    symptom_overlap = _jaccard(_tokens(candidate.title, candidate.user_symptom),
                               _tokens(target.title, target.user_symptom))
    if symptom_overlap:
        score += 0.35 * symptom_overlap
        if symptom_overlap >= 0.25:
            reasons.append(f"symptom wording overlaps {symptom_overlap:.0%}")

    shared_terms = set(candidate.search_terms) & set(target.search_terms)
    if shared_terms:
        score += min(0.10, 0.05 * len(shared_terms))
        reasons.append(f"shared search terms: {', '.join(sorted(shared_terms)[:3])}")

    return min(score, 1.0), reasons


def similar_bugs(store: BugSpecStore, target: BugSpec, *,
                 limit: int = MAX_PACK_BUGS, scan: int = 300) -> list[SimilarBug]:
    """Past specs ranked by how much they are likely to help, best first."""
    found: list[SimilarBug] = []
    for candidate in store.iter_recent(limit=scan):
        if candidate.bug_id == target.bug_id:
            continue
        score, reasons = _score(candidate, target)
        if score >= RELATED_MATCH:
            found.append(SimilarBug(candidate, score, tuple(reasons)))
    found.sort(key=lambda item: item.score, reverse=True)
    served = found[:limit]
    # Efficiency telemetry: how often history answered instead of a search.
    # A no-op unless a recorder is active for the task being worked.
    _note_signal("similar_bug_hits", len(served), source="context_pack.similar_bugs")
    return served


_PATHLIKE = re.compile(r"\.[A-Za-z0-9]{1,6}$")


def named_paths(spec: BugSpec) -> list[str]:
    """Every repository path this spec points a worker at, in order, once each.

    An entry point may be written ``path/to/file.py:symbol``; the half before
    the colon is the part a tree can be asked about. A bare symbol name is not
    a path, and checking one would only ever report it missing -- a fabricated
    failure, which is worse than the gap it pretends to close.
    """
    out: list[str] = []
    for raw in spec.likely_files:
        value = str(raw).strip()
        if value and value not in out:
            out.append(value)
    for raw in spec.entry_points:
        head = str(raw).split(":", 1)[0].strip()
        if head and head not in out and ("/" in head or _PATHLIKE.search(head)):
            out.append(head)
    return out


def verify_reused_paths(spec: BugSpec, *,
                        knowledge: "ProjectKnowledge | None" = None) -> dict[str, Any]:
    """Check every path a reused spec names against the code as it is NOW.

    This is the half of reuse that makes reuse safe. The match was made on
    wording; the fix was written against a commit that has since moved. A path
    that is gone, or that was rewritten after the spec was written, is exactly
    what a worker would otherwise discover only after trusting it -- and the
    whole saving reuse exists to produce would be spent there instead.

    Every verdict is derived from the working tree and the git delta. Where it
    cannot be derived the path is reported UNVERIFIED, never UNCHANGED: "I
    checked and nothing moved" and "I could not check" demand opposite next
    steps, and collapsing them is how a stale plan gets acted on.
    """
    paths = named_paths(spec)
    report: dict[str, Any] = {
        "verified": False, "checked": len(paths), "paths": [],
        "missing": [], "changed": [], "unverified": [], "gaps": [],
    }
    if not paths:
        # Nothing named is nothing to get wrong -- but say so, because a
        # silent empty check reads exactly like a passing one.
        report["verified"] = True
        report["gaps"].append("the matched spec names no path, so there is none to verify")
        return report

    root: Path | None = None
    delta: list[str] | None = None
    dirty: list[str] | None = None
    if knowledge is None:
        report["gaps"].append("no repository to check against -- verify each path "
                              "by hand before editing")
    else:
        root = Path(knowledge.root)
        # The working tree outranks the commit graph here for the same reason
        # it does everywhere else: an edited file is what will actually run.
        dirty = knowledge.uncommitted_paths()
        if spec.source_commit:
            delta = knowledge.changed_paths(spec.source_commit)
            if delta is None:
                report["gaps"].append(
                    f"cannot diff against {spec.source_commit[:12]} -- it is not in "
                    "this repository's history, so drift cannot be measured")
        else:
            report["gaps"].append("the matched spec records no commit, so what moved "
                                  "under it since cannot be computed")
    moved = set(delta or ()) | set(dirty or ())

    for path in paths:
        status, why = PATH_UNVERIFIED, "no repository to check against"
        if root is not None:
            relative = Path(path)
            if relative.is_absolute() or ".." in relative.parts:
                why = "not a repository-relative path"
            elif not (root / relative).exists():
                status, why = PATH_MISSING, "no longer in the working tree"
            elif path in moved:
                status, why = PATH_CHANGED, "rewritten since the spec was written"
            elif delta is None:
                why = "present, but there is no commit to measure drift from"
            else:
                status, why = PATH_UNCHANGED, "present and untouched since the spec"
        report["paths"].append({"path": path, "status": status, "why": why})
        if status == PATH_MISSING:
            report["missing"].append(path)
        elif status == PATH_CHANGED:
            report["changed"].append(path)
        elif status == PATH_UNVERIFIED:
            report["unverified"].append(path)

    report["verified"] = not report["unverified"]
    return report


def retrieval_result(store: BugSpecStore, target: BugSpec, *,
                     current_commit: str | None = None,
                     knowledge: "ProjectKnowledge | None" = None) -> dict[str, Any]:
    """Should the worker start from a previous spec, merely read it, or neither?

    Asked BEFORE investigation, never after. Retrieval that runs afterwards
    has already let the cost it exists to avoid be paid in full.

    A strong match is offered as a starting point together with the reason it
    matched, the commit it was written against, and -- when a repository is
    given -- a verdict per path from the current git delta. A fix that was
    correct three months ago may name a file that has since moved, and the
    worker has to be able to see that before editing rather than after.

    A match whose every named path has since vanished is handed back as
    reading instead of as a plan: the symptom really did match, so the root
    cause is worth knowing, but a fix strategy for code that no longer exists
    points at nothing.
    """
    if knowledge is not None and not current_commit:
        current_commit = knowledge.head()
    matches = similar_bugs(store, target)
    if not matches:
        return {"status": NO_MATCH, "matches": [],
                "guidance": "no comparable bug on record; investigate from the spec"}

    best = matches[0]
    payload: dict[str, Any] = {"matches": [m.as_dict() for m in matches]}
    if best.score < STRONG_MATCH:
        payload["status"] = RELATED_ONLY
        payload["guidance"] = ("related bugs exist -- read their root causes before "
                               "searching, but do not assume the same cause")
        return payload

    # Verified BEFORE the spec is offered for reuse, not alongside it: a
    # caller that receives REUSED_BUG_SPEC has already been told what moved.
    check = verify_reused_paths(best.spec, knowledge=knowledge)
    payload["path_check"] = check
    if check["checked"] and len(check["missing"]) == check["checked"]:
        payload["status"] = RELATED_ONLY
        payload["downgraded_from"] = REUSED
        payload["guidance"] = (
            "a strong match exists, but every path it names is gone from this "
            "repository -- read its root cause, then investigate from the current "
            "code instead of from its fix strategy")
        return payload

    stale = bool(current_commit and best.spec.source_commit
                 and best.spec.source_commit != current_commit)
    drifted = check["missing"] + check["changed"]
    payload["status"] = REUSED
    payload["reused_bug_id"] = best.spec.bug_id
    payload["reused_from_commit"] = best.spec.source_commit
    payload["reused_is_stale"] = stale
    payload["guidance"] = (
        "start from this spec's root cause and fix strategy, then VERIFY each "
        "referenced path against the current code before editing"
        + (" -- it was written against a different commit, so expect drift" if stale else "")
        + (" -- these have already moved: " + ", ".join(drifted[:4]) if drifted else "")
        + ("" if check["verified"]
           else " -- not every path could be checked here: " + "; ".join(check["gaps"])))
    return payload


@dataclass
class ContextPack:
    """The bounded briefing handed to a worker for one module."""

    module: str
    summary: str = ""
    confidence: str = "LOW"
    files: tuple[str, ...] = ()
    entry_points: tuple[str, ...] = ()
    test_runbook: str | None = None
    smoke_runbook: str | None = None
    known_issues: tuple[str, ...] = ()
    past_bugs: tuple[dict[str, Any], ...] = ()
    last_verified_commit: str | None = None
    stale: bool = False
    gaps: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "module": self.module, "summary": self.summary,
            "confidence": self.confidence, "files": list(self.files),
            "entry_points": list(self.entry_points),
            "test_runbook": self.test_runbook, "smoke_runbook": self.smoke_runbook,
            "known_issues": list(self.known_issues),
            "past_bugs": list(self.past_bugs),
            "last_verified_commit": self.last_verified_commit,
            "stale": self.stale, "gaps": list(self.gaps),
        }

    def render(self) -> str:
        """The pack as the worker sees it: short, and honest about its gaps."""
        lines = [f"MODULE {self.module} (knowledge confidence {self.confidence}"
                 + (", STALE" if self.stale else "") + ")"]
        if self.summary:
            lines.append(self.summary)
        if self.files:
            lines.append("FILES: " + ", ".join(self.files))
        if self.entry_points:
            lines.append("ENTRY POINTS: " + ", ".join(self.entry_points))
        runbooks = [f"{label}={name}" for label, name in
                    (("test", self.test_runbook), ("smoke", self.smoke_runbook)) if name]
        if runbooks:
            lines.append("RUNBOOKS: " + " ".join(runbooks))
        if self.known_issues:
            lines.append("KNOWN ISSUES: " + "; ".join(self.known_issues))
        for bug in self.past_bugs:
            cause = bug.get("root_cause") or "cause not recorded"
            lines.append(f"PAST BUG {bug['bug_id']}: {bug['title']} -> {cause}")
        if self.gaps:
            # Saying what the pack does NOT cover is what stops a worker
            # treating a thin pack as a complete picture.
            lines.append("NOT COVERED: " + "; ".join(self.gaps))
        if self.stale:
            lines.append("This module changed since it was last indexed -- "
                         "verify against the current code before relying on it.")
        text = "\n".join(lines)
        if len(text) > MAX_PACK_CHARS:
            text = text[:MAX_PACK_CHARS].rsplit("\n", 1)[0] + "\n[pack truncated]"
        return text


def build_context_pack(module: str, *,
                       knowledge: "ProjectKnowledge | None" = None,
                       store: BugSpecStore | None = None,
                       target: BugSpec | None = None) -> ContextPack:
    """Assemble one module's pack from knowledge and history.

    Degrades rather than fails: a project with no knowledge map yet still
    gets a pack listing what is missing, which is more useful to a worker
    than an exception and more honest than an empty pack that looks complete.
    """
    pack = ContextPack(module=module)
    gaps: list[str] = []

    if knowledge is not None and knowledge.exists():
        head = knowledge.head()
        # Ask about the one module, when the map can answer that. Listing every
        # module to answer about one costs a `git diff` per module nobody asked
        # about -- the exact cost a bounded briefing exists to avoid.
        state = (knowledge.module_state(module) if hasattr(knowledge, "module_state")
                 else next((m for m in knowledge.module_states() if m.name == module), None))
        if state is not None:
            pack.summary = state.summary
            pack.confidence = (getattr(state, "confidence", "")
                               or knowledge._confidence(state, head))
            pack.files = tuple(state.paths[:MAX_PACK_FILES])
            pack.entry_points = tuple(getattr(state, "entry_points", ())[:MAX_PACK_FILES])
            pack.test_runbook = getattr(state, "test_runbook", None)
            pack.smoke_runbook = getattr(state, "smoke_runbook", None)
            pack.last_verified_commit = state.last_verified_commit
            pack.stale = pack.confidence == "LOW"
            if len(state.paths) > MAX_PACK_FILES:
                gaps.append(f"{len(state.paths) - MAX_PACK_FILES} further files not listed")
        else:
            gaps.append(f"module '{module}' is not in the knowledge map yet")
        issues = knowledge.document("KNOWN_ISSUES.md") or ""
        pack.known_issues = tuple(
            line.strip("-* ").strip() for line in issues.splitlines()
            if module.lower() in line.lower() and line.strip())[:3]
    else:
        gaps.append("no knowledge map for this project yet -- pack is history only")

    if store is not None:
        if target is not None:
            bugs = [m.spec for m in similar_bugs(store, target, limit=MAX_PACK_BUGS)]
            if not bugs:
                bugs = store.recent_for_module(module, limit=MAX_PACK_BUGS)
        else:
            bugs = store.recent_for_module(module, limit=MAX_PACK_BUGS)
        pack.past_bugs = tuple(
            {"bug_id": b.bug_id, "title": b.title,
             "root_cause": b.suspected_root_cause,
             "fix_strategy": list(b.fix_strategy)[:2]}
            for b in bugs)
        if not bugs:
            gaps.append("no previous bugs recorded in this module")

    pack.gaps = tuple(gaps)
    # Counted where the retrieval actually happened, not inferred later: a
    # pack with a summary or files in it is a briefing the worker did not
    # have to reconstruct by reading the module.
    if pack.summary or pack.files:
        _note_signal("context_pack_hits", source="context_pack.build_context_pack")
        _note_signal("knowledge_hits", source="context_pack.build_context_pack")
    return pack


# -- the briefing a task starts with -----------------------------------------
#
# A map that merely exists on disk saves nothing. Every worker still opens the
# repository and re-derives the shape of the module it was sent to, because
# reading the map was one more thing to remember and searching the code was
# the path of least resistance.
#
# So the map is LOADED at the moment the task starts, for the modules the spec
# already names -- and only those. Not the whole map: a briefing that grows
# with the project is the repository again, and would cost more than the
# search it replaced.

MODULES_NOT_NAMED = "the spec names no module, so there was nothing specific to load"
NO_MAP = "this project has no knowledge map yet -- nothing could be loaded"


@dataclass
class TaskKnowledge:
    """What a worker is handed INSTEAD of re-reading the repository."""

    asked_for: tuple[str, ...] = ()
    packs: tuple[ContextPack, ...] = ()
    # Named by the spec and absent from the map. Reported, never silently
    # dropped: an unindexed module is precisely where a worker DOES have to
    # read the code, and it must be able to tell that case from a thin map.
    unknown: tuple[str, ...] = ()
    verified_commit: str | None = None
    gaps: tuple[str, ...] = ()

    @property
    def loaded(self) -> int:
        return len(self.packs)

    @property
    def usable(self) -> bool:
        """Did anything arrive that a worker can actually start from?

        A pack with neither a summary nor a file list is a name and nothing
        else, and calling that a hit would let a map full of empty entries
        report the saving it never produced.
        """
        return any(pack.summary or pack.files for pack in self.packs)

    @property
    def confidence(self) -> str:
        """The weakest loaded pack's confidence, never the best one's."""
        if not self.packs:
            return "LOW"
        return min((p.confidence or "LOW" for p in self.packs),
                   key=lambda level: _CONFIDENCE_ORDER.get(level, 0))

    def as_dict(self) -> dict[str, Any]:
        return {"asked_for": list(self.asked_for), "loaded": self.loaded,
                "usable": self.usable, "confidence": self.confidence,
                "unknown": list(self.unknown),
                "verified_commit": self.verified_commit,
                "modules": [p.as_dict() for p in self.packs],
                "gaps": list(self.gaps)}

    def render(self) -> str:
        """The briefing as the worker reads it, bounded and honest about gaps."""
        if not self.packs:
            return "KNOWLEDGE: nothing loaded -- " + (
                self.gaps[0] if self.gaps else "no module was named")
        parts = [pack.render() for pack in self.packs]
        if self.unknown:
            parts.append("NOT IN THE MAP: " + ", ".join(self.unknown)
                         + " -- read the code for these; nothing is recorded about them")
        if self.gaps:
            parts.append("BRIEFING GAPS: " + "; ".join(self.gaps))
        # The rule the map is subordinate to, restated where it is acted on.
        parts.append("The map says WHERE to look. The current code is the truth: "
                     "confirm the named paths and symbols before editing them.")
        text = "\n\n".join(parts)
        if len(text) > MAX_BRIEF_CHARS:
            text = text[:MAX_BRIEF_CHARS].rsplit("\n", 1)[0] + "\n[briefing truncated]"
        return text

    def as_handoff(self) -> dict[str, Any]:
        """The compact form that travels in a worker's task-start payload."""
        return {"MODULES": [p.module for p in self.packs],
                "CONFIDENCE": self.confidence,
                "VERIFIED_COMMIT": self.verified_commit,
                "NOT_INDEXED": list(self.unknown),
                "BRIEF": self.render()}


def spec_modules(spec: Any, *, limit: int = MAX_TASK_MODULES) -> list[str]:
    """The modules a spec NAMES, best first, deduplicated and capped.

    Duck-typed across `WorkSpec` and `BugSpec` on purpose: both name where
    the work lives, and a briefing that only worked for one of them would be
    absent exactly half the time.
    """
    names: list[str] = []
    for value in (getattr(spec, "likely_module", None),
                  *(getattr(spec, "relevant_modules", ()) or ())):
        name = str(value).strip() if value else ""
        if name and name not in names:
            names.append(name)
    return names[:limit]


def load_task_knowledge(spec: Any, *, knowledge: "ProjectKnowledge | None" = None,
                        store: BugSpecStore | None = None,
                        limit: int = MAX_TASK_MODULES) -> TaskKnowledge:
    """Load the knowledge this task needs, and nothing else.

    Called at task start, which is the only moment it can save anything: a
    briefing assembled after a worker has read the module has already let the
    cost it exists to avoid be paid in full.

    Degrades rather than raises, like every other retrieval on this path. A
    project with no map, a spec that names no module, and a module that was
    never indexed are three different situations, and each is reported as
    itself rather than as an empty result.
    """
    asked = spec_modules(spec, limit=limit)
    gaps: list[str] = []
    named_total = len(spec_modules(spec, limit=10_000))
    if named_total > len(asked):
        gaps.append(f"{named_total - len(asked)} further module(s) the spec names "
                    f"were not loaded (briefing capped at {limit})")
    if not asked:
        return TaskKnowledge(gaps=(MODULES_NOT_NAMED,))
    if knowledge is None:
        return TaskKnowledge(asked_for=tuple(asked), gaps=(NO_MAP, *gaps))

    try:
        if not knowledge.exists():
            return TaskKnowledge(asked_for=tuple(asked), gaps=(NO_MAP, *gaps))
    except Exception as exc:  # noqa: BLE001 -- a map that cannot be read is a gap
        return TaskKnowledge(asked_for=tuple(asked),
                             gaps=(f"the knowledge map could not be read: {exc}", *gaps))

    known, unknown = _resolve_modules(knowledge, asked)
    packs: list[ContextPack] = []
    for module in known:
        try:
            packs.append(build_context_pack(module, knowledge=knowledge, store=store))
        except Exception as exc:  # noqa: BLE001
            gaps.append(f"{module}: briefing failed ({type(exc).__name__}: {exc})")
    if unknown:
        gaps.append(f"not in the knowledge map: {', '.join(unknown)}")

    verified = next((p.last_verified_commit for p in packs if p.last_verified_commit), None)
    return TaskKnowledge(asked_for=tuple(asked), packs=tuple(packs),
                         unknown=tuple(unknown), verified_commit=verified,
                         gaps=tuple(gaps))


def _resolve_modules(knowledge: Any, asked: Sequence[str]) -> tuple[list[str], list[str]]:
    """Which of the named modules the map actually has.

    Uses `load_modules` when the map offers it -- that asks about exactly
    these modules -- and falls back to listing for any other knowledge-shaped
    object, because a briefing that only worked against one implementation
    would be absent wherever it was most needed.
    """
    try:
        if hasattr(knowledge, "load_modules"):
            found, unknown = knowledge.load_modules(list(asked))
            return [m.name for m in found], list(unknown)
        recorded = {m.name for m in knowledge.module_states()}
    except Exception:  # noqa: BLE001 -- treated as "the map could not answer"
        return list(asked), []
    return ([name for name in asked if name in recorded],
            [name for name in asked if name not in recorded])
