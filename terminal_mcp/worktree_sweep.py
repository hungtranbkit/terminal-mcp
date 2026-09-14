"""Worktree Janitor P3 -- the periodic sweep.

Contract: docs/WORKTREE_JANITOR.md §"P3 — periodic sweep". Catches what the
lifecycle hook structurally cannot:

  - ORPHANS: a worktree no task claims. The task was pruned, the registry was
    lost, or the worktree was made by hand. The P1 hook only fires on a task
    transition, so an orphan is invisible to it forever.
  - POST-CRASH STALE STATE: a task marked CLEANUP_PENDING whose directory is
    already gone, because a previous run died between the removal and the
    metadata write (F6).
  - STALE ADMIN ENTRIES: worktrees git still lists whose directory is absent.

Shape follows queue_loop.py / maintenance.py exactly -- daemon thread, a
threading.Event to stop, `run_once()` doing one full pass, `start`/`stop`/
`is_alive`/`status`. That shape is not decoration: `run_once()` must be
callable with the loop switched off, because this project's standing posture is
that the MANUAL path always works and only the AUTOMATIC trigger is gated.

Two things this module is careful about that a naive sweep gets wrong:

ORPHANS ARE NOT ACTIONED ON FIRST SIGHT. A worktree that appears unclaimed may
simply be mid-creation -- `git worktree add` has finished but the task row that
will reference it does not exist yet (git_isolation_service creates the worktree
BEFORE the task, by necessity: the task's own metadata has to carry the path).
Deleting in that window destroys a task that was about to start. So an orphan
must be seen unclaimed in `orphan_confirm_runs` CONSECUTIVE passes and be older
than `orphan_min_age_seconds` before it is even classified as a candidate. The
sighting counter lives in memory and resets on restart, which is the
conservative direction: a restart costs another observation window, it never
shortens one.

THE SWEEP NEVER HOLDS A LOCK ACROSS ITSELF. Each candidate is locked, handled
and released by the executor individually. A sweep-wide lock would block every
other janitor on the fleet for the duration of a full pass, and a sweep that
crashed mid-pass would leave it held until the TTL lapsed.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import git_worktree, worktree_cleanup as wc, worktree_executor as wx, worktree_janitor as wj

_LOGGER = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 900.0
DEFAULT_ORPHAN_CONFIRM_RUNS = 2
DEFAULT_ORPHAN_MIN_AGE_SECONDS = 3600.0
DEFAULT_BUDGET_SECONDS = 60.0

# Reasons a candidate was not acted on this pass.
ORPHAN_UNCONFIRMED = "ORPHAN_UNCONFIRMED"
ORPHAN_TOO_YOUNG = "ORPHAN_TOO_YOUNG"
BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
LIMIT_REACHED = "LIMIT_REACHED"


def _same_path(left: str, right: str) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except (OSError, RuntimeError, ValueError):
        return left == right


@dataclass
class _Sighting:
    runs: int = 0
    first_seen_at: float = 0.0


@dataclass
class SweepReport:
    started_at: float
    finished_at: float | None = None
    repos: list[str] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    converged: list[str] = field(default_factory=list)
    truncated_by: str | None = None

    @property
    def removed_count(self) -> int:
        return sum(1 for r in self.results if r.get("outcome") == wx.REMOVED)

    @property
    def reclaimed_bytes(self) -> int:
        return sum(int(r.get("reclaimed_bytes") or 0) for r in self.results
                   if r.get("outcome") == wx.REMOVED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at, "finished_at": self.finished_at,
            "duration_seconds": (round(self.finished_at - self.started_at, 3)
                                 if self.finished_at else None),
            "repos": self.repos, "results": self.results, "skipped": self.skipped,
            "errors": self.errors, "converged": self.converged,
            "truncated_by": self.truncated_by,
            "removed_count": self.removed_count, "reclaimed_bytes": self.reclaimed_bytes,
            "candidates_considered": len(self.results) + len(self.skipped),
        }


class WorktreeSweep:
    """One full pass = converge stale cleanup records, then sweep orphans.

    `executor` does the acting (and owns the lock, the audit rows and the
    force=False guarantee); this class only decides WHAT to hand it, and when.
    Passing an executor whose policy is observe_only makes the whole sweep a
    reporter, which is the default everywhere."""

    def __init__(self, executor: wx.WorktreeExecutor, *, store: Any = None,
                 repo_roots: tuple[str, ...] = (),
                 interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
                 orphan_confirm_runs: int = DEFAULT_ORPHAN_CONFIRM_RUNS,
                 orphan_min_age_seconds: float = DEFAULT_ORPHAN_MIN_AGE_SECONDS,
                 max_candidates_per_run: int = 50,
                 budget_seconds: float = DEFAULT_BUDGET_SECONDS,
                 dry_run: bool = True, session_registry: Any = None) -> None:
        self.executor = executor
        self.store = store
        # Per-host, passed in: a controller must never answer session liveness
        # about another node's filesystem.
        self.session_registry = session_registry
        self.repo_roots = tuple(repo_roots)
        self.interval_seconds = max(5.0, float(interval_seconds))
        self.orphan_confirm_runs = max(1, int(orphan_confirm_runs))
        self.orphan_min_age_seconds = max(0.0, float(orphan_min_age_seconds))
        self.max_candidates_per_run = max(1, int(max_candidates_per_run))
        self.budget_seconds = max(1.0, float(budget_seconds))
        self.dry_run = dry_run
        self._sightings: dict[str, _Sighting] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_cycle_at: float | None = None
        self._last_report: dict[str, Any] | None = None
        self._last_error: dict[str, str] | None = None

    # -- thread mechanics (AC1) -------------------------------------------

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            return  # never a second thread for one instance
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="terminal-mcp-worktree-sweep",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self.is_alive(), "interval_seconds": self.interval_seconds,
                    "mode": self.executor.policy.mode, "dry_run": self.dry_run,
                    "orphan_confirm_runs": self.orphan_confirm_runs,
                    "orphan_min_age_seconds": self.orphan_min_age_seconds,
                    "max_candidates_per_run": self.max_candidates_per_run,
                    "budget_seconds": self.budget_seconds,
                    "tracked_sightings": len(self._sightings),
                    "last_cycle_at": self._last_cycle_at,
                    "last_error": self._last_error,
                    "last_report": self._last_report}

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.run_once()
            self._stop_event.wait(self.interval_seconds)

    # -- one pass (AC2) ----------------------------------------------------

    def run_once(self, *, now: float | None = None, **probe_overrides: Any) -> dict[str, Any]:
        """One full pass. NEVER RAISES (AC7).

        Callable with the loop stopped -- that is the point of it being a
        method rather than the loop body. Every failure is collected into
        `errors` and reported; one unreadable repo must not cost the other
        repos their pass, let alone kill the thread."""
        now = time.time() if now is None else now
        report = SweepReport(started_at=now)
        deadline = time.monotonic() + self.budget_seconds
        # Collect the liveness probes ONCE for the whole pass, and hand the same
        # answers to every candidate. Two reasons, both real: re-walking /proc
        # and re-shelling tmux per candidate would make a 50-candidate pass the
        # most expensive thing on the box, and a probe that changed halfway
        # through would classify two worktrees against different pictures of the
        # world in one pass.
        #
        # This was a live bug: the converge half originally passed no probes at
        # all, so every candidate saw None, classified UNKNOWN (fail-closed) and
        # was never actionable -- the half was inert in production while its
        # tests passed by injecting probes the real caller never supplied.
        # `if key not in` rather than setdefault: setdefault evaluates its
        # default EAGERLY, so a caller that already supplied observations would
        # still pay for a /proc walk and two subprocess calls whose results were
        # then thrown away. It also matters for correctness on a node answering
        # about its own filesystem -- its probes must win, not merely be kept.
        probes = dict(probe_overrides)
        if not probes:
            # The shared collector, so the sweep cannot drift from the node-side
            # callers -- and so session_paths is actually populated. Leaving it
            # out (as this did originally) meant one permanently-UNKNOWN
            # predicate and therefore nothing ever actionable in production.
            probes = wj.collect_local_probes(self.session_registry)
        else:
            for key, collector in (("process_cwds", wj.collect_process_cwds),
                                   ("tmux_paths", wj.collect_tmux_paths),
                                   ("service_roots", wj.collect_service_roots)):
                if key not in probes:
                    probes[key] = collector()
        try:
            self._converge_stale_records(report, deadline, probes)
            self._sweep_repos(report, now, deadline, probes)
        except Exception as exc:  # noqa: BLE001 -- a sweep that can crash is a loop that stops
            _LOGGER.exception("worktree sweep failed")
            report.errors.append({"scope": "sweep", "error": f"{type(exc).__name__}: {exc}"[:300]})
        report.finished_at = time.time()
        payload = report.to_dict()
        with self._lock:
            self._last_cycle_at = report.finished_at
            self._last_report = payload
            self._last_error = report.errors[0] if report.errors else None
        return payload

    # -- half 1: stale cleanup records (AC6) -------------------------------

    def _converge_stale_records(self, report: SweepReport, deadline: float,
                                probes: dict[str, Any]) -> None:
        """A task marked CLEANUP_PENDING whose directory is already gone (F6),
        or which is now genuinely collectable.

        The executor already converges an absent directory to CLEANUP_DONE and
        does so idempotently, so this half simply finds the records and hands
        each one over -- it does not re-implement the convergence."""
        if self.store is None:
            return
        try:
            tasks = self.store.list_worktree_cleanup_tasks(
                states=(wc.CLEANUP_PENDING, wc.CLEANUP_ELIGIBLE),
                limit=self.max_candidates_per_run)
        except Exception as exc:  # noqa: BLE001
            report.errors.append({"scope": "store", "error": f"{type(exc).__name__}: {exc}"[:300]})
            return
        for task in tasks:
            if time.monotonic() > deadline:
                report.truncated_by = BUDGET_EXHAUSTED
                return
            path = task.get("worktree_path")
            if not path:
                continue
            try:
                result = self.executor.execute(
                    {"worktree_path": path, "node_id": self.executor.node_id},
                    task=task, repo_path=task.get("repo_path"), dry_run=self.dry_run,
                    **probes)
            except Exception as exc:  # noqa: BLE001 -- one bad task never ends the pass
                report.errors.append({"scope": f"task:{task.get('id')}",
                                      "error": f"{type(exc).__name__}: {exc}"[:300]})
                continue
            report.results.append(result.to_dict())
            if result.outcome == wx.ALREADY_GONE:
                report.converged.append(path)

    # -- half 2: orphans + stale admin entries (AC3, AC4) ------------------

    def _sweep_repos(self, report: SweepReport, now: float, deadline: float,
                     probe_overrides: dict[str, Any]) -> None:
        claimed = self._claimed_paths()
        considered = 0
        for repo_path in self._roots():
            if time.monotonic() > deadline:
                report.truncated_by = BUDGET_EXHAUSTED
                return
            report.repos.append(repo_path)
            try:
                worktrees = git_worktree.list_worktrees(repo_path)
            except Exception as exc:  # noqa: BLE001 -- one unreadable repo (AC7)
                report.errors.append({"scope": f"repo:{repo_path}",
                                      "error": f"{type(exc).__name__}: {exc}"[:300]})
                continue
            if not worktrees:
                report.errors.append({"scope": f"repo:{repo_path}",
                                      "error": "git listed no worktrees"})
                continue
            for entry in worktrees:
                if considered >= self.max_candidates_per_run:
                    report.truncated_by = LIMIT_REACHED
                    return
                if time.monotonic() > deadline:
                    report.truncated_by = BUDGET_EXHAUSTED
                    return
                path = entry.get("worktree_path") or ""
                if not path or path in claimed:
                    continue  # a claimed worktree is the other half's business
                if _same_path(path, repo_path):
                    # The main worktree can never be a candidate (I2), and the
                    # classifier would refuse it anyway -- but skipping it here
                    # keeps it out of the sighting table entirely, so it never
                    # consumes a slot in max_candidates_per_run or shows up as
                    # a perpetually-unconfirmed orphan in every report.
                    continue
                considered += 1
                verdict = self._consider_orphan(path, now)
                if verdict is not None:
                    report.skipped.append({"worktree_path": path, "reason": verdict})
                    continue
                try:
                    result = self.executor.execute(
                        {"worktree_path": path, "node_id": self.executor.node_id},
                        task=None, repo_path=repo_path, dry_run=self.dry_run,
                        **probe_overrides)
                except Exception as exc:  # noqa: BLE001
                    report.errors.append({"scope": f"worktree:{path}",
                                          "error": f"{type(exc).__name__}: {exc}"[:300]})
                    continue
                report.results.append(result.to_dict())

    def _consider_orphan(self, path: str, now: float) -> str | None:
        """-> a skip reason, or None when this orphan may be handed to the
        executor. Records the sighting either way.

        AC3 + AC4. Age is checked from the directory's own mtime, not from when
        we first noticed it: a worktree created seconds ago is young regardless
        of how long this process has been running, which is what protects the
        mid-creation window (`git worktree add` completes before the task row
        that will reference it exists)."""
        sighting = self._sightings.setdefault(path, _Sighting(first_seen_at=now))
        sighting.runs += 1

        age = self._age_of(path, now)
        if age is not None and age < self.orphan_min_age_seconds:
            return ORPHAN_TOO_YOUNG
        if sighting.runs < self.orphan_confirm_runs:
            return ORPHAN_UNCONFIRMED
        return None

    @staticmethod
    def _age_of(path: str, now: float) -> float | None:
        try:
            return max(0.0, now - Path(path).stat().st_mtime)
        except OSError:
            # Cannot stat it -> cannot establish it is old enough. Returning 0
            # (treated as "too young") is the fail-closed direction.
            return 0.0

    def _claimed_paths(self) -> set[str]:
        """Every worktree path some task's metadata references -- in ANY cleanup
        state, and also tasks with no cleanup record at all.

        Getting this wrong in the permissive direction is the worst bug this
        module could have: a live task's worktree misread as an orphan. So a
        store failure returns a sentinel that makes the orphan half skip
        entirely rather than proceed with an empty claimed-set."""
        if self.store is None:
            return set()
        # list_isolated_worktree_paths, NOT list_worktree_cleanup_tasks: the
        # latter only returns tasks that already carry a cleanup record, so a
        # still-RUNNING task -- the one it matters most to protect -- would be
        # absent and its worktree would read as an orphan.
        return self.store.list_isolated_worktree_paths()

    def _roots(self) -> list[str]:
        return [r for r in self.repo_roots if r]

    def forget_sightings(self) -> int:
        """Drop the in-memory sighting counters. Exposed for an operator who has
        just changed policy and does not want a candidate confirmed on evidence
        gathered under the old one."""
        with self._lock:
            count = len(self._sightings)
            self._sightings.clear()
        return count
