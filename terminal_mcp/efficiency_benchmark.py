"""Does the briefing actually cost a worker less than looking? Measured.

WHAT THIS ANSWERS, AND WHAT IT REFUSES TO. Three capabilities shipped
claiming the same benefit: a runbook registry (call the procedure instead of
re-deriving the command), retrieval before investigation (a bounded briefing
instead of an empty repository), and runtime telemetry (what a task actually
cost). The claim they share is that a worker now carries LESS context and
does LESS re-analysis. This module tries to find out, over this repository's
own real bug fixes, and is built so that a disappointing answer is as
reportable as a flattering one.

It does NOT claim a token saving. Per-task provider counters exist only from
the moment the telemetry runtime is attached to a live queue, and no
historical task has any -- so the usage figures here are UNAVAILABLE and say
so, rather than being estimated from text length and quietly read as
measurements later. `collect_usage` looks for real reported counters on
every run; the day a runtime reports them, the same code path reports a REAL
delta instead.

THE BASELINE METHODOLOGY, STATED BEFORE ANY NUMBER IS PRODUCED

1. No old model is re-run. Reproducing the past would cost a large budget to
   learn something the past already recorded, and the recording is better
   evidence than a re-enactment: git knows exactly which files each fix
   touched.
2. The baseline is the UNASSISTED LOCATING SURFACE: the set of source files
   a worker starting from the bug's own words would have to triage, measured
   by really running `git grep` for terms derived from the symptom against
   the repository AS IT WAS at the fix's parent commit.
3. Term derivation is mechanical (`search_terms`), never hand-tuned per
   case: hand-picking the terms is how a benchmark ends up measuring the
   person who wrote it.
4. The symptom is the fix's own commit subject. That is GENEROUS TO THE
   BASELINE -- a subject is written with hindsight and often names the
   module or the symbol, which a real bug report does not -- so any
   advantage the assisted path shows here is understated, not flattered.
5. The headline compares against the baseline's BEST case: the single most
   selective term's own match set, not the union of every term tried. The
   luckiest grep is the hardest baseline to beat, which is the point.
6. The assisted side is not a simulation. It calls the shipped
   `context_pack.build_context_pack` and `context_pack.retrieval_result`,
   against this repository's real knowledge map, and counts what they
   actually return.
7. The module the briefing is built for is chosen FROM THE SYMPTOM ALONE, by
   the shipped scorer (`work_reuse._score_module`). The corpus's own record
   of which files the fix touched is used only to SCORE the outcome, never
   to steer it. A wrong module choice is a real failure and is reported as
   one.
8. Prior bugs are seeded leave-one-out: for each case the spec store holds
   every OTHER case, never itself.
9. Every figure carries REAL, ESTIMATE or UNAVAILABLE. A measured count is
   REAL; a proxy for something unmeasurable (what a human WOULD have read)
   is ESTIMATE; something nobody recorded is UNAVAILABLE and is never
   replaced by a plausible number.

WHAT A SMALLER SURFACE IS WORTH. Nothing at all, if the briefing does not
contain the file the fix actually touched. So every case records whether the
assisted file set CONTAINS a real fix path, and a case that shrank the
surface while missing the target counts as a MISS, not a saving. That single
rule is what stops this benchmark from rewarding a system for answering
confidently and wrongly.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .bug_spec import BugSpec, BugSpecStore
from .context_pack import (MAX_PACK_FILES, NO_MATCH, RELATED_ONLY, REUSED,
                           build_context_pack, named_paths, retrieval_result)
from .work_reuse import MENTION_THRESHOLD, _score_module

# -- provenance labels, the same vocabulary the telemetry store uses ---------
REAL = "REAL"
ESTIMATE = "ESTIMATE"
UNAVAILABLE = "UNAVAILABLE"

# How much re-analysis the worker is left to do. Ordinal, cheapest first.
REUSE_PRIOR = "REUSE_PRIOR_ROOT_CAUSE"
READ_RELATED = "READ_RELATED"
FULL_ANALYSIS = "FULL_ANALYSIS"
ANALYSIS_DEPTH = (REUSE_PRIOR, READ_RELATED, FULL_ANALYSIS)

# How many derived terms a worker is assumed to try before it starts reading.
# It bounds the UNION surface only; the headline uses the best single term,
# so this number cannot flatter the assisted side.
DEFAULT_TERM_BUDGET = 4

# Paths excluded from the search surface, matching the corpus's own rule: a
# benchmark about locating a defect must not be scored on finding the test
# that noticed it.
EXCLUDED_PATHSPECS = (":(exclude)tests/", ":(exclude)docs/",
                      ":(exclude).projectflow/", ":(exclude)*.md")

# Words that carry no locating power in this repository's commit subjects.
# Deliberately generic: a stop list tuned per case would be a hand-tuned
# baseline wearing a mechanical rule's clothes.
STOPWORDS = frozenset("""
a an and are as at be been before but by can cannot did do does doing for from
had has have how if in into is it its just left make makes never no not of off
on once one only or other our out over run runs said same see should show shows
so some still stop stopped than that the their them then there these they this
those through to too under until up use used uses using very was way were what
when where which while who why will with without would you your
after again against all also always any because being both each even ever every
first get gets give given go goes going keep keeps last let lets like long many
more most much must new now old own real really right say says second take taken
takes tell than thing things time times two upon want wants well work works
fix fixes fixed bug bugs hotfix urgent p0 follow up followup docs doc test tests
merge revert wip chore refactor
""".split())

# Tokens that are identifiers rather than prose keep their shape: a term like
# `submit_watchdog` or `queue.db` is exactly what a worker would grep for.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{2,}")


def search_terms(symptom: str, *, budget: int = DEFAULT_TERM_BUDGET) -> list[str]:
    """The terms a worker would grep for, derived mechanically from the words
    it was given.

    Ordering is by length, longest first: a longer token is more specific and
    is what somebody actually types first. Ties keep their order of
    appearance so the result is stable for the same input, which a benchmark
    needs more than it needs cleverness.
    """
    seen: dict[str, int] = {}
    for index, match in enumerate(_TOKEN.finditer(symptom or "")):
        raw = match.group(0).strip(".-")
        lowered = raw.lower()
        if len(raw) < 4 or lowered in STOPWORDS or lowered.isdigit():
            continue
        seen.setdefault(raw, index)
    ordered = sorted(seen.items(), key=lambda item: (-len(item[0]), item[1]))
    return [term for term, _ in ordered[: max(1, budget)]]


@dataclass(frozen=True)
class Figure:
    """A number that cannot be read without reading where it came from."""

    value: float | int | None
    label: str
    method: str

    @classmethod
    def real(cls, value: float | int, *, method: str) -> "Figure":
        return cls(value=value, label=REAL, method=method)

    @classmethod
    def estimate(cls, value: float | int, *, method: str) -> "Figure":
        return cls(value=value, label=ESTIMATE, method=method)

    @classmethod
    def unavailable(cls, why: str) -> "Figure":
        return cls(value=None, label=UNAVAILABLE, method=why)

    def as_dict(self) -> dict[str, Any]:
        return {"value": self.value, "label": self.label, "method": self.method}

    def display(self) -> str:
        if self.value is None:
            return f"UNAVAILABLE ({self.method})"
        rendered = (f"{self.value:.2f}" if isinstance(self.value, float)
                    else f"{self.value}")
        return rendered if self.label == REAL else f"~{rendered} ({self.label})"


@dataclass(frozen=True)
class BenchmarkCase:
    """One bug. REAL ones come from git; SYNTHETIC ones say so, here and
    everywhere they are reported."""

    case_id: str
    origin: str
    category: str
    symptom: str
    fix_paths: tuple[str, ...]
    commit: str = ""
    parent: str = ""
    date: str = ""
    note: str = ""
    mirrors: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BenchmarkCase":
        return cls(case_id=str(raw["case_id"]), origin=str(raw.get("origin") or "REAL"),
                   category=str(raw.get("category") or "unknown"),
                   symptom=str(raw.get("symptom") or ""),
                   fix_paths=tuple(raw.get("fix_paths") or ()),
                   commit=str(raw.get("commit") or ""),
                   parent=str(raw.get("parent") or ""),
                   date=str(raw.get("date") or ""), note=str(raw.get("note") or ""),
                   mirrors=str(raw.get("mirrors") or ""))

    @property
    def is_real(self) -> bool:
        return self.origin.upper() == "REAL"

    def as_spec(self, *, module: str | None, with_fix_paths: bool) -> BugSpec:
        """This case as a bug spec.

        `with_fix_paths` is the whole leave-one-out discipline in one flag:
        a SEEDED case is a solved bug, so its spec names the files its fix
        touched; the case being MEASURED is an open report, so it names none
        and cannot match itself on a file it was never told about.
        """
        return BugSpec(
            bug_id=f"bench_{self.case_id}", title=self.symptom[:80],
            user_symptom=self.symptom, likely_module=module,
            likely_files=tuple(self.fix_paths) if with_fix_paths else (),
            search_terms=tuple(search_terms(self.symptom)),
            suspected_root_cause=(f"recorded in {self.commit}" if with_fix_paths
                                  and self.commit else ""),
            source_commit=self.commit or None)


def load_corpus(path: str | Path) -> list[BenchmarkCase]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [BenchmarkCase.from_dict(raw) for raw in payload.get("cases") or ()]


def validate_corpus(cases: Sequence[BenchmarkCase], *, repo_root: str | Path
                    ) -> dict[str, Any]:
    """Every REAL case must still be a commit in this repository.

    A corpus that quietly rots into fiction is worse than no corpus: its
    cases keep their REAL label while the evidence behind them is gone.
    """
    problems: list[str] = []
    for case in cases:
        if not case.symptom.strip():
            problems.append(f"{case.case_id}: no symptom")
        if not case.fix_paths:
            problems.append(f"{case.case_id}: no fix paths")
        if not case.is_real:
            continue
        if not case.commit:
            problems.append(f"{case.case_id}: REAL case with no commit")
            continue
        found = _git(repo_root, "cat-file", "-t", case.commit)
        if found != "commit":
            problems.append(f"{case.case_id}: {case.commit} is not a commit in this repo")
    return {"cases": len(cases), "real": sum(1 for c in cases if c.is_real),
            "synthetic": sum(1 for c in cases if not c.is_real),
            "categories": sorted({c.category for c in cases}),
            "problems": problems, "ok": not problems}


# -- the unassisted baseline -------------------------------------------------

def _git(repo_root: str | Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo_root), *args],
                            capture_output=True, text=True)
    return result.stdout.strip()


def grep_files(repo_root: str | Path, term: str, *, rev: str = "HEAD") -> list[str]:
    """Files whose content contains `term` at `rev`. A real search, really run.

    Fixed-string and case-insensitive, which is what somebody types. `git
    grep` exits 1 for "no matches" -- an answer, not a failure.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), "grep", "-l", "-i", "-F", "-e", term, rev,
         "--", *EXCLUDED_PATHSPECS],
        capture_output=True, text=True)
    if result.returncode not in (0, 1):
        return []
    out: list[str] = []
    for line in result.stdout.splitlines():
        # `git grep <rev>` prefixes every path with "<rev>:".
        _, _, path = line.partition(":")
        if path and path not in out:
            out.append(path)
    return out


@dataclass
class BaselineResult:
    terms: list[str]
    per_term: dict[str, int]
    best_term: str | None
    best_surface: Figure
    union_surface: Figure
    search_calls: Figure
    seconds: Figure
    contains_fix_path: bool
    analysis: str = FULL_ANALYSIS

    def as_dict(self) -> dict[str, Any]:
        return {"terms": self.terms, "per_term": self.per_term,
                "best_term": self.best_term,
                "best_surface": self.best_surface.as_dict(),
                "union_surface": self.union_surface.as_dict(),
                "search_calls": self.search_calls.as_dict(),
                "seconds": self.seconds.as_dict(),
                "contains_fix_path": self.contains_fix_path,
                "analysis": self.analysis}


def measure_baseline(case: BenchmarkCase, *, repo_root: str | Path,
                     budget: int = DEFAULT_TERM_BUDGET) -> BaselineResult:
    """What locating this bug costs with no briefing at all.

    Measured against the repository as it was at the fix's PARENT commit --
    the state a worker would actually have been looking at. A synthetic case
    has no commit, so it is measured against the working tree's HEAD and says
    so in its method string.
    """
    rev = case.parent or "HEAD"
    terms = search_terms(case.symptom, budget=budget)
    started = time.monotonic()
    per_term: dict[str, list[str]] = {term: grep_files(repo_root, term, rev=rev)
                                      for term in terms}
    seconds = time.monotonic() - started

    non_empty = {term: files for term, files in per_term.items() if files}
    union = sorted({path for files in per_term.values() for path in files})
    best_term = min(non_empty, key=lambda t: len(non_empty[t])) if non_empty else None
    best_files = non_empty.get(best_term or "", [])
    fix_paths = set(case.fix_paths)
    return BaselineResult(
        terms=terms,
        per_term={term: len(files) for term, files in per_term.items()},
        best_term=best_term,
        best_surface=Figure.estimate(
            len(best_files),
            method=(f"files matching the most selective term {best_term!r} at "
                    f"{rev[:12]} -- a proxy for what a worker would triage, not a "
                    f"count of files anyone read")),
        union_surface=Figure.estimate(
            len(union),
            method=f"files matching any of {len(terms)} derived term(s) at {rev[:12]}"),
        # These searches were genuinely executed by this process.
        search_calls=Figure.real(len(terms),
                                 method="searches this benchmark actually ran"),
        seconds=Figure.real(round(seconds, 3),
                            method="wall clock of the real git grep calls"),
        contains_fix_path=bool(fix_paths & set(union)))


# -- the assisted path, as shipped -------------------------------------------

@dataclass
class AssistedResult:
    chosen_module: str | None
    module_score: float
    module_is_right: bool | None
    # Which indexed module(s), if any, own the files the fix actually
    # touched. Empty means the knowledge map covers none of them, so no
    # briefing built from that map could have named the fix site -- a
    # coverage fact, not a retrieval failure, and the two must not be
    # reported as the same thing.
    owning_modules: tuple[str, ...]
    files: list[str]
    surface: Figure
    search_calls: Figure
    seconds: Figure
    retrieval_status: str
    analysis: str
    contains_fix_path: bool
    reused_bug_id: str = ""
    pack_gaps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"chosen_module": self.chosen_module,
                "module_score": round(self.module_score, 3),
                "module_is_right": self.module_is_right,
                "owning_modules": list(self.owning_modules),
                "fix_site_mapped": bool(self.owning_modules),
                "files": self.files, "surface": self.surface.as_dict(),
                "search_calls": self.search_calls.as_dict(),
                "seconds": self.seconds.as_dict(),
                "retrieval_status": self.retrieval_status, "analysis": self.analysis,
                "contains_fix_path": self.contains_fix_path,
                "reused_bug_id": self.reused_bug_id,
                "pack_gaps": self.pack_gaps}


def choose_module(symptom: str, *, knowledge: Any,
                  floor: float = MENTION_THRESHOLD) -> tuple[str | None, float]:
    """Which indexed module this report is about, from the report alone.

    Uses the shipped scorer AND the shipped threshold, not a second pair
    written for the benchmark. The floor matters more than it looks: the
    first version of this function took the argmax with no floor, and duly
    "chose" modules on scores of 0.03 -- a benchmark measuring a decision
    procedure the system does not use. Below the floor the honest answer is
    that nothing is known about where this bug lives, which is exactly what
    `work_reuse.knowledge_candidates` already says.

    The corpus's fix paths are NOT consulted here -- they only judge the
    answer afterwards.
    """
    from .context_pack import _tokens

    try:
        modules = knowledge.module_states()
    except Exception:  # noqa: BLE001 -- no map is an answer, not a crash
        return None, 0.0
    target = _tokens(symptom)
    best_name, best_score = None, 0.0
    for module in modules:
        score, _ = _score_module(module, target)
        if score > best_score:
            best_name, best_score = module.name, score
    if best_score < floor:
        return None, best_score
    return best_name, best_score


def module_owning(path: str, *, knowledge: Any) -> str | None:
    """Which indexed module (if any) claims this path. Used only for scoring."""
    try:
        modules = knowledge.module_states()
    except Exception:  # noqa: BLE001
        return None
    for module in modules:
        for owned in getattr(module, "paths", ()) or ():
            if path == owned or path.startswith(str(owned).rstrip("/") + "/"):
                return module.name
    return None


def measure_assisted(case: BenchmarkCase, *, knowledge: Any, spec_store: BugSpecStore,
                     repo_root: str | Path) -> AssistedResult:
    """What the shipped retrieval actually hands a worker for this bug."""
    started = time.monotonic()
    module, score = choose_module(case.symptom, knowledge=knowledge)
    target = case.as_spec(module=module, with_fix_paths=False)

    # The shipped call, with the repository it verifies paths against. The
    # commit passed is the fix's PARENT: the state a worker would have been
    # looking at, so a path that moved afterwards is reported as moved.
    retrieval = retrieval_result(spec_store, target, knowledge=knowledge,
                                 current_commit=case.parent or None)
    status = str(retrieval.get("status") or NO_MATCH)

    files: list[str] = []
    gaps: list[str] = []
    if module:
        pack = build_context_pack(module, knowledge=knowledge, store=spec_store,
                                  target=target)
        files.extend(pack.files)
        files.extend(entry.split(":", 1)[0] for entry in pack.entry_points)
        gaps = list(pack.gaps)
    if status == REUSED:
        reused = spec_store.get(str(retrieval.get("reused_bug_id") or ""))
        if reused is not None:
            files.extend(named_paths(reused))
    seconds = time.monotonic() - started

    ordered: list[str] = []
    for path in files:
        if path and path not in ordered:
            ordered.append(path)

    analysis = {REUSED: REUSE_PRIOR, RELATED_ONLY: READ_RELATED}.get(status, FULL_ANALYSIS)
    owning = {module_owning(path, knowledge=knowledge) for path in case.fix_paths}
    owning.discard(None)
    return AssistedResult(
        chosen_module=module, module_score=score,
        # None, not False, when NO module owns the fix path: the choice
        # cannot be wrong about a module that does not exist in the map.
        module_is_right=(module in owning if owning else None),
        owning_modules=tuple(sorted(str(name) for name in owning)),
        files=ordered,
        surface=Figure.real(len(ordered),
                            method="files the shipped briefing actually named "
                                   f"(bounded at {MAX_PACK_FILES} by the pack)"),
        search_calls=Figure.real(0, method="the briefing runs no repository search"),
        seconds=Figure.real(round(seconds, 3),
                            method="wall clock of the real retrieval + pack build"),
        retrieval_status=status, analysis=analysis,
        contains_fix_path=bool(set(case.fix_paths) & set(ordered)),
        reused_bug_id=str(retrieval.get("reused_bug_id") or ""),
        pack_gaps=gaps)


# -- provider usage: looked for every run, invented never --------------------

def collect_usage(telemetry_store: Any, *, case_ids: Iterable[str]) -> dict[str, Any]:
    """Provider counters for these cases, if anything ever reported any.

    This is the honest half of the token question. The benchmark cases are
    historical fixes made long before any telemetry existed, so the expected
    answer is UNAVAILABLE -- and it is produced by really asking the store,
    not by assuming. When a runtime does report counters for a measured task,
    the same call returns a REAL figure with no change here.
    """
    ids = list(case_ids)
    if telemetry_store is None:
        return {"available": False,
                "figure": Figure.unavailable(
                    "no telemetry store was supplied to this run").as_dict(),
                "tasks_with_usage": 0, "tasks_looked_for": len(ids)}
    found: list[dict[str, Any]] = []
    for case_id in ids:
        try:
            row = telemetry_store.for_task(case_id)
        except Exception:  # noqa: BLE001 -- an unreadable store is not a zero
            return {"available": False,
                    "figure": Figure.unavailable(
                        "the telemetry store could not be read").as_dict(),
                    "tasks_with_usage": 0, "tasks_looked_for": len(ids)}
        usage = (row or {}).get("usage") or {}
        if usage.get("available"):
            found.append({"case_id": case_id, "usage": usage})
    if not found:
        return {"available": False,
                "figure": Figure.unavailable(
                    f"no provider reported usage counters for any of the {len(ids)} "
                    f"benchmark cases -- they predate the telemetry runtime").as_dict(),
                "tasks_with_usage": 0, "tasks_looked_for": len(ids)}
    total = 0
    for entry in found:
        value = ((entry["usage"].get("total") or {}).get("value"))
        total += int(value or 0)
    return {"available": True,
            "figure": Figure.real(total,
                                  method=f"sum of provider-reported totals over "
                                         f"{len(found)} task(s)").as_dict(),
            "tasks_with_usage": len(found), "tasks_looked_for": len(ids),
            "rows": found}


# -- running the whole thing -------------------------------------------------

@dataclass
class CaseResult:
    case: BenchmarkCase
    baseline: BaselineResult
    assisted: AssistedResult

    @property
    def fix_site_mapped(self) -> bool:
        return bool(self.assisted.owning_modules)

    @property
    def smaller_than_union(self) -> bool | None:
        """Against the baseline a worker actually pays, not its luckiest case.

        SECONDARY, and deliberately not part of the verdict: the headline was
        pre-registered against the single most selective term and stays
        there. But the luckiest grep is not what a worker triages -- it tries
        its several terms and triages what they all return -- so the union is
        the comparison that describes the real cost, and leaving it out
        because the headline was registered differently would be hiding half
        the measurement.
        """
        if not self.assisted.contains_fix_path:
            return None
        return (self.assisted.surface.value or 0) < (self.baseline.union_surface.value or 0)

    @property
    def verdict(self) -> str:
        """What this case actually shows. A miss is a miss, whatever it cost.

        SHRUNK        the briefing named the fix site AND fewer files than
                      the luckiest grep would have left to triage
        NO_SHRINK     it named the fix site, but not more cheaply
        MISSED        it did not name the fix site -- no credit, however
                      small the briefing was
        UNLOCATABLE   neither path reached the fix site; the case says
                      nothing about either and is counted apart
        """
        if not self.assisted.contains_fix_path:
            return "UNLOCATABLE" if not self.baseline.contains_fix_path else "MISSED"
        baseline_value = self.baseline.best_surface.value or 0
        assisted_value = self.assisted.surface.value or 0
        return "SHRUNK" if assisted_value < baseline_value else "NO_SHRINK"

    def as_dict(self) -> dict[str, Any]:
        return {"case_id": self.case.case_id, "origin": self.case.origin,
                "smaller_than_union_baseline": self.smaller_than_union,
                "category": self.case.category, "commit": self.case.commit,
                "symptom": self.case.symptom, "fix_paths": list(self.case.fix_paths),
                "baseline": self.baseline.as_dict(), "assisted": self.assisted.as_dict(),
                "verdict": self.verdict}


def seed_store(cases: Sequence[BenchmarkCase], *, exclude: str, store: BugSpecStore,
               knowledge: Any) -> None:
    """Fill the spec store with every case EXCEPT the one being measured."""
    for case in cases:
        if case.case_id == exclude:
            continue
        module = None
        for path in case.fix_paths:
            module = module_owning(path, knowledge=knowledge)
            if module:
                break
        store.save(case.as_spec(module=module, with_fix_paths=True))


def case_store_path(store_path: str | Path, case_id: str) -> Path:
    """A FRESH database per case. Not an optimisation -- a correctness rule.

    Sharing one file across cases silently breaks leave-one-out: the store is
    keyed by bug id, so the spec written for case B while measuring case A is
    still there when B's own turn comes, and B matches itself. The first
    version of this harness did exactly that, and the cases it measured after
    the first were scored against their own answers.
    """
    base = Path(store_path)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", case_id)
    return base.with_name(f"{base.stem}-{safe}{base.suffix or '.db'}")


def run_case(case: BenchmarkCase, *, cases: Sequence[BenchmarkCase], repo_root: str | Path,
             knowledge: Any, store_path: str | Path,
             budget: int = DEFAULT_TERM_BUDGET) -> CaseResult:
    store = BugSpecStore(case_store_path(store_path, case.case_id))
    try:
        seed_store(cases, exclude=case.case_id, store=store, knowledge=knowledge)
        assisted = measure_assisted(case, knowledge=knowledge, spec_store=store,
                                    repo_root=repo_root)
    finally:
        store._connection.close()  # noqa: SLF001 -- the store has no close()
    baseline = measure_baseline(case, repo_root=repo_root, budget=budget)
    return CaseResult(case=case, baseline=baseline, assisted=assisted)


def summarise(results: Sequence[CaseResult]) -> dict[str, Any]:
    """The aggregate, with every count it is built from left visible."""
    if not results:
        return {"cases": 0, "note": "nothing measured"}
    verdicts = [r.verdict for r in results]
    located = [r for r in results if r.verdict in ("SHRUNK", "NO_SHRINK")]
    comparable = [r for r in results if r.verdict != "UNLOCATABLE"]
    shrunk = [r for r in results if r.verdict == "SHRUNK"]

    def mean(values: Sequence[float]) -> float | None:
        return round(sum(values) / len(values), 2) if values else None

    baseline_best = [r.baseline.best_surface.value or 0 for r in results]
    assisted_surface = [r.assisted.surface.value or 0 for r in results]
    return {
        "cases": len(results),
        "verdicts": {name: verdicts.count(name)
                     for name in ("SHRUNK", "NO_SHRINK", "MISSED", "UNLOCATABLE")},
        # Rates are over the cases they can be computed from, and the
        # denominator is printed beside every one of them.
        "located_rate": (round(len(located) / len(comparable), 3) if comparable else None),
        "located_denominator": len(comparable),
        "shrunk_rate": (round(len(shrunk) / len(located), 3) if located else None),
        "shrunk_denominator": len(located),
        "mean_baseline_best_surface": Figure.estimate(
            mean(baseline_best) or 0,
            method="mean over cases of the most selective term's match count").as_dict(),
        "mean_assisted_surface": Figure.real(
            mean(assisted_surface) or 0,
            method="mean over cases of the files the briefing named").as_dict(),
        "mean_baseline_searches": Figure.real(
            mean([r.baseline.search_calls.value or 0 for r in results]) or 0,
            method="searches this benchmark really ran per case").as_dict(),
        "mean_assisted_searches": Figure.real(
            0, method="the briefing runs no repository search").as_dict(),
        "analysis_depth": {
            "assisted": {name: sum(1 for r in results if r.assisted.analysis == name)
                         for name in ANALYSIS_DEPTH},
            "baseline": {FULL_ANALYSIS: len(results),
                         "note": "by definition: an unassisted worker is offered "
                                 "no prior root cause"},
        },
        "module_choice": {
            "right": sum(1 for r in results if r.assisted.module_is_right is True),
            "wrong": sum(1 for r in results if r.assisted.module_is_right is False),
            "unmapped": sum(1 for r in results if r.assisted.module_is_right is None),
        },
        "fix_site_mapped": sum(1 for r in results if r.fix_site_mapped),
        "fix_site_unmapped": sum(1 for r in results if not r.fix_site_mapped),
        # SECONDARY (see CaseResult.smaller_than_union): never feeds the verdict.
        "secondary_vs_union_baseline": {
            "smaller": sum(1 for r in results if r.smaller_than_union is True),
            "denominator": sum(1 for r in results if r.smaller_than_union is not None),
            "mean_union_surface": Figure.estimate(
                mean([r.baseline.union_surface.value or 0 for r in results]) or 0,
                method="mean files matching ANY derived term -- what a worker "
                       "triages when no single term happens to be selective").as_dict(),
            "note": "reported because the headline's baseline is the luckiest "
                    "single grep, which is the hardest case and not the usual one",
        },
        "mean_seconds": {
            "baseline": Figure.real(
                mean([r.baseline.seconds.value or 0.0 for r in results]) or 0.0,
                method="wall clock of the real greps; NOT a worker's own time, "
                       "which nothing recorded").as_dict(),
            "assisted": Figure.real(
                mean([r.assisted.seconds.value or 0.0 for r in results]) or 0.0,
                method="wall clock of the real retrieval + pack build").as_dict(),
        },
    }


# The bar, written down before the run rather than after it.
ACCEPTANCE = {
    "located_rate": 0.50,
    "shrunk_rate": 0.50,
    "min_real_cases": 10,
}


def acceptance(summary: dict[str, Any], *, usage: dict[str, Any],
               criteria: dict[str, Any] | None = None) -> dict[str, Any]:
    """PASS, FAIL or INCONCLUSIVE -- decided by the numbers, not by the author.

    The token-efficiency claim is reported separately and is UNVERIFIED
    whenever no provider reported counters. It never contributes to the
    verdict: a benchmark that let an unmeasured quantity influence its own
    conclusion would be exactly the invention this whole exercise refuses.
    """
    criteria = {**ACCEPTANCE, **(criteria or {})}
    checks: dict[str, Any] = {}
    cases = int(summary.get("cases") or 0)
    checks["sample_size"] = {
        "status": "MET" if cases >= criteria["min_real_cases"] else "NOT_MET",
        "actual": cases, "required": criteria["min_real_cases"]}
    for name in ("located_rate", "shrunk_rate"):
        actual = summary.get(name)
        if actual is None:
            checks[name] = {"status": "UNKNOWN",
                            "reason": "no case could be compared on this axis"}
            continue
        checks[name] = {"status": "MET" if actual >= criteria[name] else "NOT_MET",
                        "actual": actual, "required": criteria[name],
                        "denominator": summary.get(f"{name.split('_')[0]}_denominator")}
    statuses = {name: entry["status"] for name, entry in checks.items()}
    if "UNKNOWN" in statuses.values() or statuses.get("sample_size") == "NOT_MET":
        verdict = "INCONCLUSIVE"
    elif all(status == "MET" for status in statuses.values()):
        verdict = "PASS"
    elif all(status == "NOT_MET" for name, status in statuses.items()
             if name != "sample_size"):
        verdict = "FAIL"
    else:
        verdict = "INCONCLUSIVE"
    return {"verdict": verdict, "checks": checks,
            "token_claim": {
                "status": "MEASURED" if usage.get("available") else "UNVERIFIED",
                "detail": (usage.get("figure") or {}).get("method", ""),
                "note": "context surface and re-analysis depth are what this "
                        "benchmark can evidence; a token saving is only claimed "
                        "when a provider actually reported counters"}}


def map_provenance(knowledge: Any, *, repo_root: str | Path) -> dict[str, Any]:
    """What the knowledge map actually contained when this ran.

    A retrieval result is only meaningful beside the map it was drawn from,
    and "the map" is a thing that changes. Recording its size, its coverage
    of the package and the commit it was indexed at makes two runs of this
    benchmark comparable instead of merely adjacent.
    """
    try:
        modules = knowledge.module_states()
        state = knowledge.load_state()
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "detail": f"{type(exc).__name__}: {exc}"}
    indexed_files: set[str] = set()
    for module in modules:
        for path in getattr(module, "paths", ()) or ():
            indexed_files.add(str(path))
    package = {f"terminal_mcp/{p.name}"
               for p in (Path(repo_root) / "terminal_mcp").glob("*.py")
               if p.name != "__init__.py"}
    covered = package & indexed_files
    return {
        "available": True,
        "modules": len(modules),
        "indexed_paths": len(indexed_files),
        "package_files": len(package),
        "package_files_indexed": len(covered),
        "package_coverage": (round(len(covered) / len(package), 3) if package else None),
        "indexed_at_commit": (state.get("last_indexed_commit") or "")[:12],
        "modules_without_summary": sorted(m.name for m in modules if not m.summary),
    }


def run_benchmark(cases: Sequence[BenchmarkCase], *, repo_root: str | Path,
                  knowledge: Any, store_path: str | Path,
                  telemetry_store: Any = None,
                  budget: int = DEFAULT_TERM_BUDGET) -> dict[str, Any]:
    """Measure every case, then report real and synthetic separately.

    Synthetic cases never enter the headline. They bracket it: one restates a
    real bug to test the repeat case the history could not supply, the other
    sits in a module the map does not cover at all.
    """
    results = [run_case(case, cases=cases, repo_root=repo_root, knowledge=knowledge,
                        store_path=store_path, budget=budget) for case in cases]
    real = [r for r in results if r.case.is_real]
    synthetic = [r for r in results if not r.case.is_real]
    usage = collect_usage(telemetry_store, case_ids=[r.case.case_id for r in results])
    wording = wording_sensitivity(results)
    summary = summarise(real)
    by_category: dict[str, Any] = {}
    for category in sorted({r.case.category for r in real}):
        by_category[category] = summarise([r for r in real if r.case.category == category])
    mapped = [r for r in real if r.fix_site_mapped]
    unmapped = [r for r in real if not r.fix_site_mapped]
    report: dict[str, Any] = {
        "corpus": {"cases": len(results), "real": len(real), "synthetic": len(synthetic)},
        "knowledge_map": map_provenance(knowledge, repo_root=repo_root),
        "methodology": METHODOLOGY,
        "results": [r.as_dict() for r in results],
        "summary": summary,
        "by_category": by_category,
        # POST-HOC. This split was not pre-registered: it was added after the
        # first run, when the per-case table showed that most cases were not
        # a retrieval failure at all -- the knowledge map simply does not
        # cover the file the fix touched, so no briefing built from it could
        # ever have named the site. Saying that plainly matters more than
        # protecting the headline, and the headline is NOT recomputed from
        # this split: the pre-registered verdict stands as measured.
        "stratified": {
            "disclosure": ("added after the first run, having seen the data; "
                           "the pre-registered acceptance verdict is unchanged "
                           "by it"),
            "fix_site_mapped": {
                "meaning": "the knowledge map covers a file the fix touched, so "
                           "a briefing could in principle have named it",
                "summary": summarise(mapped)},
            "fix_site_unmapped": {
                "meaning": "the map covers none of the files the fix touched; "
                           "this measures coverage, not retrieval",
                "summary": summarise(unmapped)},
        },
        "synthetic": {"cases": len(synthetic),
                      "results": [r.as_dict() for r in synthetic],
                      "summary": summarise(synthetic),
                      "note": "reported apart from the headline; these are written "
                              "cases, not recovered ones"},
        "wording_sensitivity": wording,
        "usage": usage,
        "acceptance_criteria_preregistered": dict(ACCEPTANCE),
        "acceptance": acceptance(summary, usage=usage),
    }
    report["conclusions"] = conclusions(report)
    return report


def wording_sensitivity(results: Sequence[CaseResult]) -> dict[str, Any]:
    """The same defect, reported two ways. The most uncomfortable number here.

    A synthetic case may declare that it `mirrors` a real one: same bug, same
    fix, but worded as a user would report it instead of as the commit
    subject that fixed it. Comparing the pair isolates how much of the
    assisted path's success comes from the REPORT naming the module -- which
    a commit subject does with hindsight and a bug report generally does not.

    If the pair diverges, the mapped-stratum result is optimistic and this
    says by how much, rather than leaving a reader to discover it.
    """
    by_id = {r.case.case_id: r for r in results}
    pairs: list[dict[str, Any]] = []
    for result in results:
        mirrored = by_id.get(result.case.mirrors)
        if result.case.mirrors and mirrored is not None:
            pairs.append({
                "as_reported": {"case_id": result.case.case_id,
                                "symptom": result.case.symptom,
                                "module": result.assisted.chosen_module,
                                "module_score": round(result.assisted.module_score, 3),
                                "verdict": result.verdict},
                "as_committed": {"case_id": mirrored.case.case_id,
                                 "symptom": mirrored.case.symptom,
                                 "module": mirrored.assisted.chosen_module,
                                 "module_score": round(mirrored.assisted.module_score, 3),
                                 "verdict": mirrored.verdict},
                "diverges": result.verdict != mirrored.verdict,
            })
    diverging = [p for p in pairs if p["diverges"]]
    return {
        "pairs": pairs, "diverging": len(diverging),
        "note": ("each pair is one defect described twice. A divergence means the "
                 "assisted path succeeded on the wording that already named the "
                 "module and failed on the wording that did not -- so results "
                 "measured from commit subjects are optimistic about real bug "
                 "reports." if diverging else
                 "no pair diverged, so the assisted path did not depend on the "
                 "commit subject's hindsight in these cases."),
    }


METHODOLOGY = [
    "No old model is re-run; git's record of each fix is the evidence.",
    "Baseline = the unassisted locating surface: source files matching terms "
    "derived mechanically from the bug's own words, measured by really running "
    "git grep at the fix's parent commit.",
    "The symptom is the fix's commit subject. Its hindsight cuts BOTH ways: it "
    "gives the baseline specific terms to grep for, and it gives the assisted "
    "path a module name to match on. The synthetic mirror pair measures the "
    "second effect directly rather than leaving it assumed.",
    "The headline compares against the baseline's BEST case -- the single most "
    "selective term -- not the union of every term tried.",
    "The assisted side calls the shipped retrieval and context pack against this "
    "repository's real knowledge map and counts what they return.",
    "The module is chosen from the symptom alone by the shipped scorer; the "
    "corpus's fix paths only judge the outcome, never steer it.",
    "Prior bugs are seeded leave-one-out: no case ever sees itself.",
    "A briefing that does not contain a real fix path is a MISS, however small.",
    "Every figure is labelled REAL, ESTIMATE or UNAVAILABLE; nothing unmeasured "
    "is filled in.",
    "MAP PROVENANCE, disclosed: the first run measured a map of 9 modules / 19 "
    "paths, and its finding was that coverage -- not retrieval -- was the "
    "constraint. The package was then indexed COMPLETELY (every file assigned to "
    "exactly one module, enforced by the indexer), which is a change made after "
    "seeing the result. It was done by a uniform rule rather than by indexing the "
    "modules the corpus needed, because the second would have been tuning the "
    "system to its own test.",
    "TWO SUMMARY STYLES were measured and the numbers for both are recorded here: "
    "prose (the module's own first docstring sentence) and names (that plus every "
    "public name its files define). Prose was kept -- it locates the fix site more "
    "often AND is the more defensible artifact -- but choosing between them by "
    "benchmark result is fitting to this corpus, so the kept variant's number is "
    "optimistic by an unknown amount.",
    "The instrument was corrected twice while it was being built, and neither "
    "correction improved the result. (1) It chose a module by argmax with no "
    "confidence floor, 'choosing' modules on scores of 0.03; applying the shipped "
    "threshold turned several of those into no-module-chosen, which cost the "
    "assisted side cases it had been credited with. (2) It reused one spec "
    "database across cases, so a case could meet its own spec; each case now gets "
    "its own. That one changed no number, because the shipped matcher already "
    "refuses to match a spec against itself by id -- verified by re-running both "
    "ways and diffing, not assumed.",
]


# Below this many cases in a stratum, a rate is reported with a warning
# attached: five cases can produce any percentage at all.
UNDERPOWERED_BELOW = 10


def conclusions(report: dict[str, Any]) -> list[dict[str, Any]]:
    """What the measurement supports, derived from it rather than asserted.

    Each statement carries the numbers it came from, so a reader can disagree
    with the reading without having to re-derive the arithmetic. Statements
    that would flatter the system are held to the same rule as the rest: a
    rate over a handful of cases is reported as underpowered, not as a
    result.
    """
    summary = report.get("summary") or {}
    stratified = (report.get("stratified") or {})
    out: list[dict[str, Any]] = []

    mapped = ((stratified.get("fix_site_mapped") or {}).get("summary") or {})
    unmapped = ((stratified.get("fix_site_unmapped") or {}).get("summary") or {})
    total = int(summary.get("cases") or 0)
    unmapped_cases = int(summary.get("fix_site_unmapped") or 0)
    if total and unmapped_cases:
        out.append({
            "statement": (f"The binding constraint is knowledge-map COVERAGE, not the "
                          f"retrieval mechanism: in {unmapped_cases} of {total} real "
                          f"cases the map covers none of the files the fix touched, so "
                          f"no briefing built from it could have named the site."),
            "evidence": {"unmapped_cases": unmapped_cases, "real_cases": total,
                         "unmapped_located_rate": unmapped.get("located_rate")},
            "supports": "a coverage finding",
        })
    mapped_cases = int(mapped.get("cases") or 0)
    mapped_verdicts = mapped.get("verdicts") or {}
    located_hits = (mapped_verdicts.get("SHRUNK", 0) + mapped_verdicts.get("NO_SHRINK", 0))
    shrunk_hits = mapped_verdicts.get("SHRUNK", 0)
    if mapped_cases:
        entry = {
            "statement": (f"Where the map does cover the fix site, the briefing named "
                          f"it in {located_hits} of "
                          f"{mapped.get('located_denominator')} comparable cases "
                          f"(rate {mapped.get('located_rate')}), and was smaller than "
                          f"the luckiest single grep in {shrunk_hits} of "
                          f"{mapped.get('shrunk_denominator')} "
                          f"(rate {mapped.get('shrunk_rate')})."),
            "evidence": {"cases": mapped_cases,
                         "located_rate": mapped.get("located_rate"),
                         "shrunk_rate": mapped.get("shrunk_rate")},
            "supports": "a narrow, conditional claim",
        }
        if mapped_cases < UNDERPOWERED_BELOW:
            entry["caveat"] = (f"UNDERPOWERED: {mapped_cases} cases. This is not a "
                               f"result, it is a direction worth measuring again on a "
                               f"larger indexed map.")
        out.append(entry)

    wording = report.get("wording_sensitivity") or {}
    if wording.get("diverging"):
        out.append({
            "statement": ("The assisted path's success depends on the REPORT naming "
                          "the module. The same defect, reworded as a user would "
                          "report it, dropped below the module-choice floor and "
                          "returned nothing -- so every rate here, measured from "
                          "commit subjects, is optimistic about real bug reports."),
            "evidence": {"diverging_pairs": wording["diverging"],
                         "pairs": wording["pairs"]},
            "supports": "a qualification of every other number in this report",
        })

    usage = report.get("usage") or {}
    if not usage.get("available"):
        out.append({
            "statement": ("No token saving is claimed. No provider reported usage "
                          "counters for any case in this corpus, and an estimate "
                          "presented beside measurements would be read as one."),
            "evidence": {"tasks_looked_for": usage.get("tasks_looked_for"),
                         "tasks_with_usage": usage.get("tasks_with_usage"),
                         "method": (usage.get("figure") or {}).get("method")},
            "supports": "nothing -- it is the absence of a claim",
        })

    searches = (summary.get("mean_baseline_searches") or {}).get("value")
    if searches:
        out.append({
            "statement": (f"Search rounds are the one axis that moves unconditionally: "
                          f"the briefing runs no repository search at all, against "
                          f"{searches} real greps per case for the unassisted path. "
                          f"That holds whether or not the briefing was useful, which "
                          f"is exactly why it is worth little on its own."),
            "evidence": {"baseline_mean_searches": searches, "assisted_searches": 0},
            "supports": "a real but weak claim",
        })
    return out


# Every run of this benchmark that has been performed, with the MAP it was
# run against. Recorded because the question "did indexing help?" cannot be
# answered by a single run, and because the honest answer turned out to be
# more complicated than yes or no. Each row is a real run; the current run's
# own numbers are computed, never copied from here.
PRIOR_RUNS: list[dict[str, Any]] = [
    {
        "date": "2026-09-14",
        "map": "9 modules / 19 paths (the map as it had grown on its own)",
        "located": "5/17 (0.294)",
        "beat_luckiest_grep": "2/5 (0.400)",
        "module_choice": "5 right / 2 wrong / 11 fix sites unmapped",
        "analysis_depth": "3 reuse / 3 read-related / 12 full",
        "mean_assisted_surface": 0.61,
        "note": "coverage was the binding constraint: in 11 of 18 cases the map "
                "contained none of the files the fix touched",
    },
    {
        "date": "2026-09-15",
        "map": "33 modules / 147 paths, summaries = docstring sentence + every "
               "public name the module's files define",
        "located": "5/17 (0.294)",
        "beat_luckiest_grep": "1/5 (0.200)",
        "module_choice": "6 right / 12 wrong / 0 unmapped",
        "analysis_depth": "1 reuse / 6 read-related / 11 full",
        "mean_assisted_surface": 4.67,
        "note": "complete coverage, no improvement: the bottleneck moved from "
                "coverage to module CHOICE, and a summary full of symbol names "
                "made choosing worse",
    },
]


def render_markdown(report: dict[str, Any]) -> str:
    """The report a person reads, with the unflattering parts kept in."""
    summary = report["summary"]
    acceptance_block = report["acceptance"]
    lines: list[str] = []
    add = lines.append
    add("# Token-efficiency benchmark — measured result")
    add("")
    add(f"**Verdict: {acceptance_block['verdict']}** — "
        f"token claim: {acceptance_block['token_claim']['status']}")
    add("")
    add(f"Corpus: {report['corpus']['real']} real cases recovered from this "
        f"repository's git history, {report['corpus']['synthetic']} synthetic "
        f"(reported separately).")
    add("")
    add("## What this measurement supports")
    add("")
    for item in conclusions(report):
        add(f"- {item['statement']}")
        if item.get("caveat"):
            add(f"  - **{item['caveat']}**")
        add(f"  - _supports: {item['supports']}_")
    add("")
    if PRIOR_RUNS:
        add("## How this has moved")
        add("")
        add("| Run | Map | Named the fix site | Beat the luckiest grep | Module choice "
            "| Re-analysis | Mean briefing |")
        add("|---|---|---|---|---|---|---|")
        for run in PRIOR_RUNS:
            add(f"| {run['date']} | {run['map']} | {run['located']} "
                f"| {run['beat_luckiest_grep']} | {run['module_choice']} "
                f"| {run['analysis_depth']} | {run['mean_assisted_surface']} files |")
        current = report["summary"]
        module_choice = current["module_choice"]
        analysis = current["analysis_depth"]["assisted"]
        located_hits = (current["verdicts"]["SHRUNK"] + current["verdicts"]["NO_SHRINK"])
        add(f"| **this run** | {(report.get('knowledge_map') or {}).get('modules', '?')} "
            f"modules / {(report.get('knowledge_map') or {}).get('indexed_paths', '?')} "
            f"paths, prose summaries "
            f"| {located_hits}/{current['located_denominator']} "
            f"({current['located_rate']}) "
            f"| {current['verdicts']['SHRUNK']}/{current['shrunk_denominator']} "
            f"({current['shrunk_rate']}) "
            f"| {module_choice['right']} right / {module_choice['wrong']} wrong / "
            f"{module_choice['unmapped']} unmapped "
            f"| {analysis.get(REUSE_PRIOR, 0)} reuse / "
            f"{analysis.get(READ_RELATED, 0)} read-related / "
            f"{analysis.get(FULL_ANALYSIS, 0)} full "
            f"| {current['mean_assisted_surface']['value']} files |")
        add("")
        for run in PRIOR_RUNS:
            add(f"- {run['date']}: {run['note']}")
        add("")
    provenance = report.get("knowledge_map") or {}
    if provenance.get("available"):
        add("## The map the briefing was built from")
        add("")
        add(f"- {provenance['modules']} modules, {provenance['indexed_paths']} indexed "
            f"paths, at commit `{provenance['indexed_at_commit']}`")
        add(f"- package coverage: {provenance['package_files_indexed']}"
            f"/{provenance['package_files']} files "
            f"({provenance['package_coverage']})")
        if provenance.get("modules_without_summary"):
            add(f"- modules with NO summary (their largest file has no module "
                f"docstring): {', '.join(provenance['modules_without_summary'])}")
        add("")
    add("## Methodology, stated before measuring")
    add("")
    for item in report["methodology"]:
        add(f"- {item}")
    add("")
    add("## Headline (real cases only)")
    add("")
    add("| Figure | Value | Label | How it was obtained |")
    add("|---|---|---|---|")
    for name in ("mean_baseline_best_surface", "mean_assisted_surface",
                 "mean_baseline_searches", "mean_assisted_searches"):
        figure = summary[name]
        add(f"| {name.replace('_', ' ')} | {figure['value']} | {figure['label']} "
            f"| {figure['method']} |")
    for name, figure in summary["mean_seconds"].items():
        add(f"| mean seconds ({name}) | {figure['value']} | {figure['label']} "
            f"| {figure['method']} |")
    usage_figure = report["usage"]["figure"]
    add(f"| provider usage delta | {usage_figure['value']} | {usage_figure['label']} "
        f"| {usage_figure['method']} |")
    add("")
    add(f"Verdicts: {summary['verdicts']}")
    add("")
    add(f"- briefing named the fix site in {summary['verdicts']['SHRUNK'] + summary['verdicts']['NO_SHRINK']}"
        f"/{summary['located_denominator']} comparable cases "
        f"(rate {summary['located_rate']})")
    add(f"- of those, it was smaller than the luckiest single grep in "
        f"{summary['verdicts']['SHRUNK']}/{summary['shrunk_denominator']} "
        f"(rate {summary['shrunk_rate']})")
    add(f"- module choice: {summary['module_choice']}")
    secondary = summary.get("secondary_vs_union_baseline") or {}
    if secondary:
        add(f"- **secondary, not part of the verdict**: against the baseline a "
            f"worker actually pays -- everything its {summary['mean_baseline_searches']['value']} "
            f"derived terms return, {secondary['mean_union_surface']['value']} files "
            f"on average -- the briefing was smaller in "
            f"{secondary['smaller']}/{secondary['denominator']} located cases. "
            f"{secondary['note']}.")
    add(f"- re-analysis depth (assisted): {summary['analysis_depth']['assisted']}")
    add("")
    stratified = report.get("stratified") or {}
    if stratified:
        add("## Where the map covers the fix site (post-hoc split)")
        add("")
        add(f"_{stratified['disclosure']}_")
        add("")
        add("| Stratum | Cases | Named the fix site | Smaller than the luckiest grep |")
        add("|---|---|---|---|")
        for key in ("fix_site_mapped", "fix_site_unmapped"):
            block = stratified[key]["summary"]
            add(f"| {key.replace('_', ' ')} | {block.get('cases', 0)} "
                f"| {block.get('located_rate')} (n={block.get('located_denominator')}) "
                f"| {block.get('shrunk_rate')} (n={block.get('shrunk_denominator')}) |")
        add("")
        for key in ("fix_site_mapped", "fix_site_unmapped"):
            add(f"- **{key.replace('_', ' ')}**: {stratified[key]['meaning']}")
        add("")
    wording = report.get("wording_sensitivity") or {}
    if wording.get("pairs"):
        add("## The same defect, worded two ways")
        add("")
        add(f"_{wording['note']}_")
        add("")
        add("| Wording | Case | Module chosen | Score | Verdict |")
        add("|---|---|---|---|---|")
        for pair in wording["pairs"]:
            for kind in ("as_committed", "as_reported"):
                side = pair[kind]
                add(f"| {kind.replace('as_', 'as ')} | {side['case_id']} "
                    f"| {side['module'] or 'none above the floor'} "
                    f"| {side['module_score']} | {side['verdict']} |")
        add("")
    add("## Per case")
    add("")
    add("| Case | Origin | Category | Baseline best | Assisted | Mapped | Verdict | Analysis |")
    add("|---|---|---|---|---|---|---|---|")
    for row in report["results"]:
        assisted = row["assisted"]
        add(f"| {row['case_id']} | {row['origin']} | {row['category']} "
            f"| {row['baseline']['best_surface']['value']} "
            f"(`{row['baseline']['best_term']}`) "
            f"| {assisted['surface']['value']} "
            f"({assisted['chosen_module'] or 'no module above the floor'}) "
            f"| {'yes' if assisted['fix_site_mapped'] else 'no'} "
            f"| {row['verdict']} | {assisted['analysis']} |")
    add("")
    add("## Acceptance")
    add("")
    add("The bar, registered before the run and published with every result: "
        + ", ".join(f"`{name} = {value}`" for name, value in sorted(ACCEPTANCE.items()))
        + ".")
    add("")
    for name, check in acceptance_block["checks"].items():
        actual = check.get("actual")
        required = check.get("required")
        denominator = check.get("denominator")
        detail = f"{actual} vs required {required}"
        if denominator is not None:
            detail += f", n={denominator}"
        if check.get("reason"):
            detail = str(check["reason"])
        add(f"- **{name}**: {check['status']} ({detail})")
    add("")
    add(f"- **token claim**: {acceptance_block['token_claim']['status']} — "
        f"{acceptance_block['token_claim']['detail']}")
    add("")
    add("---")
    add("")
    add("Generated by `scripts/benchmark/run_tokeff_benchmark.py` from "
        "`benchmarks/tokeff_corpus.json`; the machine-readable result, including "
        "every per-case measurement, is `benchmarks/tokeff_result.json`. This file "
        "is generated — edit the harness or the corpus, not the report.")
    return "\n".join(lines) + "\n"
