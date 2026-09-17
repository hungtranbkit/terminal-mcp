"""Worktree Janitor P3 -- the periodic sweep.

Real git repos, real linked worktrees, a real QueueStore. The sweep's whole job
is deciding WHICH worktrees to hand the executor and WHEN, so the interesting
cases are all about restraint: the orphan it must not touch yet, the worktree
that is too young, the budget that cuts a pass short, the unreadable repo that
must not kill the loop.

Named per acceptance criterion (AC1-AC7).
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from terminal_mcp import worktree_cleanup as wc
from terminal_mcp import worktree_executor as wx
from terminal_mcp import worktree_janitor as wj
from terminal_mcp import worktree_sweep as sweepmod
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.worktree_sweep import WorktreeSweep

_ENV = {"PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=True, env={**_ENV, "HOME": str(cwd)})


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text("*.db\n.env\n")
    (root / "a.txt").write_text("one\n")
    (root / "payload.bin").write_bytes(b"z" * 4096)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return root


def _worktree(repo, name):
    path = repo.parent / name
    _git(repo, "worktree", "add", "-q", "-b", f"task/{name}", str(path), "main")
    return path


def _age(path, seconds):
    """Backdate a directory so the orphan min-age check sees it as old."""
    old = time.time() - seconds
    import os
    os.utime(path, (old, old))


def _executor(tmp_path, *, mode="auto_execute", store=None, audit=None, locks=None):
    # grace_seconds=0: a task that just reached COMPLETED has served no grace
    # at all, and these tests are about the SWEEP's decisions. The grace
    # predicate itself is covered by the P0 and P2 suites.
    policy = wj.JanitorPolicy(mode=mode, allowed_roots=(str(tmp_path),), grace_seconds=0)
    return wx.WorktreeExecutor(policy, store=store, audit=audit, locks=locks, node_id="local")


def _sweep(tmp_path, repo, *, store=None, dry_run=False, **kw):
    kw.setdefault("orphan_confirm_runs", 1)
    kw.setdefault("orphan_min_age_seconds", 0.0)
    return WorktreeSweep(_executor(tmp_path, store=store), store=store,
                         repo_roots=(str(repo),), dry_run=dry_run, **kw)


def _probes():
    return {"process_cwds": [], "tmux_paths": [], "session_paths": [], "service_roots": []}


# == AC1: the loop shape ==================================================

def test_ac1_follows_the_established_loop_shape(tmp_path, repo):
    sweep = _sweep(tmp_path, repo)
    for attr in ("start", "stop", "is_alive", "run_once", "status"):
        assert callable(getattr(sweep, attr)), attr
    assert sweep.is_alive() is False


def test_ac1_start_stop_is_alive_on_a_real_thread(tmp_path, repo):
    sweep = _sweep(tmp_path, repo, interval_seconds=30)
    assert sweep.is_alive() is False
    sweep.start()
    try:
        assert sweep.is_alive() is True
        sweep.start()  # idempotent -- never a second thread
        assert threading.active_count() >= 1
    finally:
        sweep.stop(timeout=10)
    assert sweep.is_alive() is False


def test_ac1_the_thread_is_a_daemon(tmp_path, repo):
    sweep = _sweep(tmp_path, repo, interval_seconds=30)
    sweep.start()
    try:
        assert sweep._thread.daemon is True, "a non-daemon loop would block shutdown"
    finally:
        sweep.stop(timeout=10)


def test_ac1_config_defaults_keep_the_loop_off(tmp_path):
    from terminal_mcp.config import WorktreeJanitorConfig

    assert WorktreeJanitorConfig().sweep_enabled is False
    assert WorktreeJanitorConfig().mode == "observe_only"


def test_ac1_server_http_starts_it_only_behind_the_flag():
    """The gate is a real `if` in the startup path, not a comment."""
    source = Path("terminal_mcp/server_http.py").read_text()
    assert "if config.worktree_janitor.sweep_enabled:" in source
    index = source.index("if config.worktree_janitor.sweep_enabled:")
    assert "worktree_sweep.start()" in source[index:index + 2000]
    # ...and nowhere else.
    assert source.count("worktree_sweep.start()") == 1


# == AC2: run_once works with the loop off ================================

def test_ac2_run_once_works_with_the_loop_stopped(tmp_path, repo):
    sweep = _sweep(tmp_path, repo)
    assert sweep.is_alive() is False
    report = sweep.run_once()
    assert report["finished_at"] is not None
    assert report["repos"] == [str(repo)]


def test_ac2_the_mcp_tool_is_registered():
    import asyncio

    from terminal_mcp.mcp_app import build_mcp

    names = {t.name for t in asyncio.run(build_mcp().list_tools())}
    assert "terminal_worktree_sweep_run_once" in names
    assert "terminal_worktree_sweep_status" in names


def test_ac2_status_is_readable_before_any_run(tmp_path, repo):
    status = _sweep(tmp_path, repo).status()
    assert status["running"] is False
    assert status["last_cycle_at"] is None
    assert status["mode"] == "auto_execute"


# == AC3: orphans need confirmation =======================================

def test_ac3_an_orphan_is_not_actioned_on_first_sighting(tmp_path, repo):
    path = _worktree(repo, "wt-orphan")
    _age(path, 99_999)
    sweep = _sweep(tmp_path, repo, orphan_confirm_runs=2, orphan_min_age_seconds=10)

    first = sweep.run_once(**_probes())
    assert path.is_dir(), "first sighting must never act"
    assert any(s["reason"] == sweepmod.ORPHAN_UNCONFIRMED for s in first["skipped"])
    assert first["removed_count"] == 0


def test_ac3_an_orphan_is_considered_once_confirmed(tmp_path, repo):
    path = _worktree(repo, "wt-confirm")
    _age(path, 99_999)
    sweep = _sweep(tmp_path, repo, orphan_confirm_runs=2, orphan_min_age_seconds=10)
    sweep.run_once(**_probes())
    second = sweep.run_once(**_probes())
    # Reached the executor this time. It still refuses (no task owns it, so the
    # classifier says ORPHAN_UNCONFIRMED -> REVIEW) -- confirmation buys a
    # CLASSIFICATION, never an automatic deletion.
    assert not any(s["reason"] == sweepmod.ORPHAN_UNCONFIRMED for s in second["skipped"])
    assert second["removed_count"] == 0
    assert path.is_dir()


@pytest.mark.parametrize("runs", [3, 5])
def test_ac3_confirmation_requires_that_many_consecutive_passes(tmp_path, repo, runs):
    path = _worktree(repo, f"wt-n{runs}")
    _age(path, 99_999)
    sweep = _sweep(tmp_path, repo, orphan_confirm_runs=runs, orphan_min_age_seconds=10)
    for _ in range(runs - 1):
        report = sweep.run_once(**_probes())
        assert any(s["reason"] == sweepmod.ORPHAN_UNCONFIRMED for s in report["skipped"])
    final = sweep.run_once(**_probes())
    assert not any(s["reason"] == sweepmod.ORPHAN_UNCONFIRMED for s in final["skipped"])


def test_ac3_a_claimed_worktree_is_never_treated_as_an_orphan(tmp_path, repo):
    """The dangerous direction. A task that is still RUNNING has no cleanup
    record at all, so a claimed-set derived from cleanup records would miss it
    entirely and its worktree would read as unclaimed."""
    store = QueueStore(tmp_path / "queue.db")
    path = _worktree(repo, "wt-live")
    _age(path, 99_999)
    task_id, = store.append_tasks("demo", [{
        "title": "live work", "prompt": "p" * 60,
        "metadata": {"git_isolation": {"worktree_path": str(path), "repo_path": str(repo)}}}])
    store.transition_task(task_id, "PRECHECK", event_type="CLAIMED")  # no cleanup record

    sweep = _sweep(tmp_path, repo, store=store)
    report = sweep.run_once(**_probes())
    considered = [r["worktree_path"] for r in report["results"]]
    assert str(path) not in considered, "a live task's worktree is not an orphan"
    assert path.is_dir()


# == AC4: a young worktree is never swept =================================

def test_ac4_a_worktree_created_seconds_ago_is_never_swept(tmp_path, repo):
    path = _worktree(repo, "wt-fresh")  # mtime = now
    sweep = _sweep(tmp_path, repo, orphan_confirm_runs=1, orphan_min_age_seconds=3600)
    report = sweep.run_once(**_probes())
    assert any(s["reason"] == sweepmod.ORPHAN_TOO_YOUNG for s in report["skipped"])
    assert path.is_dir()


def test_ac4_age_beats_sighting_count(tmp_path, repo):
    """Even after many passes, a young worktree stays young. Confirmation and
    age are AND-ed, not either/or -- the mid-creation window is exactly the case
    where a fast sweep would otherwise confirm within seconds."""
    path = _worktree(repo, "wt-young-many")
    sweep = _sweep(tmp_path, repo, orphan_confirm_runs=1, orphan_min_age_seconds=3600)
    for _ in range(5):
        report = sweep.run_once(**_probes())
        assert any(s["reason"] == sweepmod.ORPHAN_TOO_YOUNG for s in report["skipped"])
    assert path.is_dir()


def test_ac4_an_unstattable_path_is_treated_as_too_young(tmp_path, repo):
    """Fail-closed: if age cannot be established, it is not old enough."""
    assert WorktreeSweep._age_of("/no/such/path/anywhere", time.time()) == 0.0


# == AC5: bounded by count and wall clock, no sweep-wide lock =============

def test_ac5_the_sweep_is_bounded_by_max_candidates_per_run(tmp_path, repo):
    for n in range(6):
        _age(_worktree(repo, f"wt-many{n}"), 99_999)
    sweep = _sweep(tmp_path, repo, max_candidates_per_run=3)
    report = sweep.run_once(**_probes())
    assert report["candidates_considered"] <= 3
    assert report["truncated_by"] == sweepmod.LIMIT_REACHED


def test_ac5_the_sweep_is_bounded_by_a_wall_clock_budget(tmp_path, repo):
    for n in range(4):
        _age(_worktree(repo, f"wt-slow{n}"), 99_999)
    sweep = _sweep(tmp_path, repo, budget_seconds=1.0, max_candidates_per_run=100)
    # A budget already spent: the very first candidate check must stop the pass.
    sweep.budget_seconds = 1.0
    original = sweepmod.time.monotonic

    calls = {"n": 0}

    def _fast_forward():
        calls["n"] += 1
        return original() + (0 if calls["n"] < 3 else 10_000)

    sweepmod.time.monotonic = _fast_forward
    try:
        report = sweep.run_once(**_probes())
    finally:
        sweepmod.time.monotonic = original
    assert report["truncated_by"] == sweepmod.BUDGET_EXHAUSTED


def test_ac5_no_lock_is_held_across_the_sweep(tmp_path, repo):
    """Each candidate is locked and released by the executor individually. A
    sweep-wide lock would block every other janitor for a whole pass."""
    from terminal_mcp.lease import ResourceLockStore

    locks = ResourceLockStore(tmp_path / "locks.db")
    for n in range(3):
        _age(_worktree(repo, f"wt-lk{n}"), 99_999)
    executor = _executor(tmp_path, locks=locks)
    sweep = WorktreeSweep(executor, repo_roots=(str(repo),), dry_run=False,
                          orphan_confirm_runs=1, orphan_min_age_seconds=0)
    sweep.run_once(**_probes())
    assert locks.list_locks() == [], "every lock must be released by the end of a pass"


def test_ac5_the_sweep_module_takes_no_lock_of_its_own():
    """Structural: locking is the executor's job, per candidate."""
    source = Path(sweepmod.__file__).read_text()
    assert "ResourceLockStore" not in source
    assert ".acquire(" not in source


# == AC6: a PENDING task whose directory vanished converges ===============

def test_ac6_a_pending_task_whose_directory_vanished_converges_to_done(tmp_path, repo):
    store = QueueStore(tmp_path / "queue.db")
    gone = tmp_path / "vanished"
    task_id, = store.append_tasks("demo", [{
        "title": "t", "prompt": "p" * 60,
        "metadata": {"git_isolation": {"worktree_path": str(gone), "repo_path": str(repo)},
                     wc.METADATA_KEY: {"state": wc.CLEANUP_PENDING}}}])
    sweep = _sweep(tmp_path, repo, store=store)
    report = sweep.run_once(**_probes())
    assert str(gone) in report["converged"]
    record = store.get_task(task_id).metadata[wc.METADATA_KEY]
    assert record["state"] == wc.CLEANUP_DONE


def test_ac6_convergence_is_idempotent_across_passes(tmp_path, repo):
    store = QueueStore(tmp_path / "queue.db")
    gone = tmp_path / "vanished2"
    store.append_tasks("demo", [{
        "title": "t", "prompt": "p" * 60,
        "metadata": {"git_isolation": {"worktree_path": str(gone), "repo_path": str(repo)},
                     wc.METADATA_KEY: {"state": wc.CLEANUP_PENDING}}}])
    sweep = _sweep(tmp_path, repo, store=store)
    first = sweep.run_once(**_probes())
    second = sweep.run_once(**_probes())
    assert str(gone) in first["converged"]
    # Second pass finds nothing left in PENDING -- the record moved to DONE.
    assert second["converged"] == []


def test_ac6_a_still_present_pending_worktree_is_handled_not_converged(tmp_path, repo):
    store = QueueStore(tmp_path / "queue.db")
    path = _worktree(repo, "wt-pending")
    store.append_tasks("demo", [{
        "title": "t", "prompt": "p" * 60,
        "metadata": {"git_isolation": {"worktree_path": str(path), "repo_path": str(repo)},
                     wc.METADATA_KEY: {"state": wc.CLEANUP_PENDING}}}])
    report = _sweep(tmp_path, repo, store=store).run_once(**_probes())
    assert report["converged"] == []
    assert any(r["worktree_path"] == str(path) for r in report["results"])


# == AC7: run_once never raises ==========================================

def test_ac7_an_unreadable_repo_does_not_kill_the_pass(tmp_path, repo):
    good = _worktree(repo, "wt-good")
    _age(good, 99_999)
    sweep = WorktreeSweep(_executor(tmp_path), repo_roots=("/no/such/repo", str(repo)),
                          dry_run=False, orphan_confirm_runs=1, orphan_min_age_seconds=0)
    report = sweep.run_once(**_probes())
    assert any("no/such/repo" in e["scope"] for e in report["errors"])
    assert str(repo) in report["repos"], "the good repo is still swept"


def test_ac7_an_exploding_executor_does_not_kill_the_pass(tmp_path, repo):
    class _Boom(wx.WorktreeExecutor):
        def execute(self, *a, **k):
            raise RuntimeError("executor exploded")

    _age(_worktree(repo, "wt-boom"), 99_999)
    policy = wj.JanitorPolicy(mode="auto_execute", allowed_roots=(str(tmp_path),))
    sweep = WorktreeSweep(_Boom(policy, node_id="local"), repo_roots=(str(repo),),
                          dry_run=False, orphan_confirm_runs=1, orphan_min_age_seconds=0)
    report = sweep.run_once(**_probes())  # must not raise
    assert any("executor exploded" in e["error"] for e in report["errors"])


def test_ac7_an_exploding_store_does_not_kill_the_pass(tmp_path, repo):
    class _BrokenStore:
        def list_worktree_cleanup_tasks(self, **k):
            raise RuntimeError("db gone")

        def list_isolated_worktree_paths(self, **k):
            raise RuntimeError("db gone")

    sweep = _sweep(tmp_path, repo, store=_BrokenStore())
    report = sweep.run_once(**_probes())  # must not raise
    assert report["errors"], "the failure is reported, not swallowed silently"


def test_ac7_a_store_failure_skips_the_orphan_half_rather_than_guessing(tmp_path, repo):
    """The fail-closed direction. Without a readable claimed-set, every worktree
    would look unclaimed -- so the orphan half must not run at all."""
    class _BrokenStore:
        def list_worktree_cleanup_tasks(self, **k):
            return []

        def list_isolated_worktree_paths(self, **k):
            raise RuntimeError("db gone")

    path = _worktree(repo, "wt-noclaims")
    _age(path, 99_999)
    report = _sweep(tmp_path, repo, store=_BrokenStore()).run_once(**_probes())
    assert report["results"] == [], "nothing may be considered without a claimed-set"
    assert path.is_dir()


def test_ac7_the_loop_thread_survives_a_failing_pass(tmp_path, repo):
    sweep = WorktreeSweep(_executor(tmp_path), repo_roots=("/no/such/repo",),
                          interval_seconds=5, dry_run=True)
    sweep.start()
    try:
        deadline = time.time() + 10
        while sweep.status()["last_cycle_at"] is None and time.time() < deadline:
            time.sleep(0.1)
        assert sweep.status()["last_cycle_at"] is not None, "a pass ran"
        assert sweep.is_alive() is True, "and the thread survived it"
    finally:
        sweep.stop(timeout=10)


# == end to end ==========================================================

def test_a_confirmed_old_orphan_owned_by_a_finished_task_is_really_removed(tmp_path, repo):
    """The whole point, end to end: the sweep finds a genuinely collectable
    worktree and the executor really removes it."""
    store = QueueStore(tmp_path / "queue.db")
    path = _worktree(repo, "wt-e2e")
    _age(path, 99_999)
    task_id, = store.append_tasks("demo", [{
        "title": "finished work", "prompt": "p" * 60,
        "metadata": {"git_isolation": {"worktree_path": str(path), "repo_path": str(repo)},
                     wc.METADATA_KEY: {"state": wc.CLEANUP_PENDING}}}])
    for status in ("PRECHECK", "READY", "DISPATCHING", "RUNNING", "VERIFYING", "COMPLETED"):
        store.transition_task(task_id, status, event_type="TEST")

    report = _sweep(tmp_path, repo, store=store).run_once(**_probes())
    assert report["removed_count"] == 1, report
    assert not path.exists()
    assert report["reclaimed_bytes"] > 0
    assert store.get_task(task_id).metadata[wc.METADATA_KEY]["state"] == wc.CLEANUP_DONE


def test_observe_only_mode_makes_the_whole_sweep_a_reporter(tmp_path, repo):
    store = QueueStore(tmp_path / "queue.db")
    path = _worktree(repo, "wt-observe")
    _age(path, 99_999)
    store.append_tasks("demo", [{
        "title": "t", "prompt": "p" * 60,
        "metadata": {"git_isolation": {"worktree_path": str(path), "repo_path": str(repo)},
                     wc.METADATA_KEY: {"state": wc.CLEANUP_PENDING}}}])
    executor = _executor(tmp_path, mode="observe_only", store=store)
    sweep = WorktreeSweep(executor, store=store, repo_roots=(str(repo),), dry_run=False,
                          orphan_confirm_runs=1, orphan_min_age_seconds=0)
    report = sweep.run_once(**_probes())
    assert report["removed_count"] == 0
    assert path.is_dir()


def test_forget_sightings_resets_the_confirmation_window(tmp_path, repo):
    path = _worktree(repo, "wt-forget")
    _age(path, 99_999)
    sweep = _sweep(tmp_path, repo, orphan_confirm_runs=2, orphan_min_age_seconds=0)
    sweep.run_once(**_probes())
    assert sweep.forget_sightings() == 1
    report = sweep.run_once(**_probes())
    assert any(s["reason"] == sweepmod.ORPHAN_UNCONFIRMED for s in report["skipped"])


def test_probes_are_collected_once_per_pass_not_once_per_candidate(tmp_path, repo, monkeypatch):
    """Performance and consistency, both real. Re-walking /proc and re-shelling
    tmux/systemctl per candidate would make a 50-candidate pass the most
    expensive thing on the box; and a probe that changed halfway through would
    classify two worktrees in one pass against different pictures of the world."""
    calls = {"proc": 0, "tmux": 0, "svc": 0}
    monkeypatch.setattr(sweepmod.wj, "collect_process_cwds",
                        lambda: calls.__setitem__("proc", calls["proc"] + 1) or [])
    monkeypatch.setattr(sweepmod.wj, "collect_tmux_paths",
                        lambda: calls.__setitem__("tmux", calls["tmux"] + 1) or [])
    monkeypatch.setattr(sweepmod.wj, "collect_service_roots",
                        lambda: calls.__setitem__("svc", calls["svc"] + 1) or [])
    for n in range(5):
        _age(_worktree(repo, f"wt-probe{n}"), 99_999)
    report = _sweep(tmp_path, repo).run_once()
    assert report["candidates_considered"] >= 5
    assert calls == {"proc": 1, "tmux": 1, "svc": 1}


def test_explicit_probes_suppress_the_local_ones(tmp_path, repo, monkeypatch):
    """A caller that supplies its own observations -- a node answering about its
    OWN filesystem -- must not have them overwritten by the controller's."""
    called = {"n": 0}
    monkeypatch.setattr(sweepmod.wj, "collect_process_cwds",
                        lambda: called.__setitem__("n", called["n"] + 1) or [])
    _age(_worktree(repo, "wt-explicit"), 99_999)
    _sweep(tmp_path, repo).run_once(**_probes())
    assert called["n"] == 0, "explicit probes must win"
