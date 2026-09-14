"""Module context packs and similar-bug retrieval.

Two ways to make the SECOND bug in a module cheaper than the first.

A *context pack* is the small, bounded briefing a worker needs about one
module: what it is, which files it owns, how to test it, what broke here
before. It is assembled from the knowledge map and the bug history rather
than re-derived by reading source, and it is deliberately capped -- an
unbounded pack is just the repository again.

*Similar-bug retrieval* asks whether this bug has been seen before. When it
has, the previous spec is offered as a starting point with its provenance
attached. It is never applied silently: the code is still the source of
truth, and a reused spec that no longer matches the code must be adjusted,
which is exactly what the worker's plan-verification step is for.

Nothing here stores secrets -- it only ever reads material that
project_knowledge and bug_spec have already scrubbed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

from .bug_spec import BugSpec, BugSpecStore
from .work_telemetry_runtime import note as _note_signal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .project_knowledge import ProjectKnowledge

# A pack is a briefing, not a dossier. These caps are what keep it one.
MAX_PACK_FILES = 12
MAX_PACK_BUGS = 5
MAX_PACK_CHARS = 4000

# Similarity thresholds. STRONG means "start from this spec"; RELATED means
# "read this first, it is probably informative"; below WEAK we say nothing,
# because a bad suggestion costs more attention than no suggestion.
STRONG_MATCH = 0.62
RELATED_MATCH = 0.38

REUSED = "REUSED_BUG_SPEC"
RELATED_ONLY = "RELATED_BUGS_FOUND"
NO_MATCH = "NO_SIMILAR_BUG"

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


def retrieval_result(store: BugSpecStore, target: BugSpec, *,
                     current_commit: str | None = None) -> dict[str, Any]:
    """Should the worker start from a previous spec, or merely read it?

    A strong match is offered as a starting point together with the reason it
    matched and the commit it was written against -- a fix that was correct
    three months ago may name a file that has since moved, and the worker has
    to be able to see that for itself.
    """
    matches = similar_bugs(store, target)
    if not matches:
        return {"status": NO_MATCH, "matches": [],
                "guidance": "no comparable bug on record; investigate from the spec"}

    best = matches[0]
    payload = {"matches": [m.as_dict() for m in matches]}
    if best.score < STRONG_MATCH:
        payload["status"] = RELATED_ONLY
        payload["guidance"] = ("related bugs exist -- read their root causes before "
                               "searching, but do not assume the same cause")
        return payload

    stale = bool(current_commit and best.spec.source_commit
                 and best.spec.source_commit != current_commit)
    payload["status"] = REUSED
    payload["reused_bug_id"] = best.spec.bug_id
    payload["reused_from_commit"] = best.spec.source_commit
    payload["reused_is_stale"] = stale
    payload["guidance"] = (
        "start from this spec's root cause and fix strategy, then VERIFY each "
        "referenced path against the current code before editing"
        + (" -- it was written against a different commit, so expect drift" if stale else ""))
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
        state = next((m for m in knowledge.module_states() if m.name == module), None)
        if state is not None:
            pack.summary = state.summary
            pack.confidence = knowledge._confidence(state, head)
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
