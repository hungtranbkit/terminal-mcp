"""Worktree Janitor P2 -- the executor. THE FIRST TESTS WHERE REAL DIRECTORIES
ARE REALLY DELETED.

Every worktree here is created by real `git worktree add` in tmp_path and, in
the happy-path tests, really removed by real `git worktree remove`. Nothing is
mocked away, because the thing under test is precisely whether a deletion
happens and under exactly which conditions -- a mocked remove would prove that
we *called* something, which is the part that was never in doubt.

Named per acceptance criterion (AC1-AC8) and per contract invariant/failure
mode (I1, I3, I6, I7, I8, F6, F7, F11).
"""
from __future__ import annotations

import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from terminal_mcp import worktree_executor as ex
from terminal_mcp import worktree_cleanup as wc
from terminal_mcp import worktree_janitor as wj
from terminal_mcp.audit import AuditStore
from terminal_mcp.lease import ResourceLockStore
from terminal_mcp.queue_store import QueueStore

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
    (root / ".gitignore").write_text("*.db\n.env\n__pycache__/\n")
    (root / "a.txt").write_text("one\n")
    # Committed, not written per-worktree: a file created inside a fresh
    # worktree would be UNTRACKED, which classifies DIRTY and would make every
    # candidate here un-removable for the wrong reason. Committing it on main
    # means each worktree is genuinely clean AND has real bytes to reclaim.
    (root / "payload.bin").write_bytes(b"z" * 4096)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return root


def _worktree(repo, name):
    path = repo.parent / name
    _git(repo, "worktree", "add", "-q", "-b", f"task/{name}", str(path), "main")
    return path  # clean, merged (branched from main with no new commits)


def _policy(tmp_path, mode="auto_execute", **kw):
    kw.setdefault("allowed_roots", (str(tmp_path),))
    kw.setdefault("grace_seconds", 300)
    return wj.JanitorPolicy(mode=mode, **kw)


def _task(**kw):
    base = {"id": "T1", "status": "COMPLETED", "attempt_count": 1, "max_attempts": 3,
            "terminal_at": 0.0, "metadata": {}}
    base.update(kw)
    return base


def _probes():
    """All four liveness probes answered as "nothing", so classification can
    reach AUTO_SAFE. A probe left unset is UNKNOWN, which is never actionable --
    proved by its own test below."""
    return {"process_cwds": [], "tmux_paths": [], "session_paths": [], "service_roots": []}


def _executor(tmp_path, *, mode="auto_execute", audit=None, locks=None, store=None,
              max_attempts=3):
    return ex.WorktreeExecutor(_policy(tmp_path, mode=mode), audit=audit, locks=locks,
                               store=store, node_id="local", max_attempts=max_attempts)


def _run(executor, repo, path, *, dry_run=False, task=None, **kw):
    return executor.execute({"worktree_path": str(path), "node_id": "local"},
                            task=task if task is not None else _task(),
                            repo_path=str(repo), dry_run=dry_run, **_probes(), **kw)


# == AC1: AUTO_SAFE only, re-classified under the lock =====================

def test_ac1_an_auto_safe_worktree_is_really_removed(repo, tmp_path):
    path = _worktree(repo, "wt-go")
    assert path.is_dir()
    result = _run(_executor(tmp_path), repo, path)
    assert result.outcome == ex.REMOVED, (result.reason, result.detail)
    assert not path.exists(), "the directory must actually be gone"
    assert result.deleted_something is True


def test_ac1_a_non_auto_safe_candidate_is_never_removed(repo, tmp_path):
    path = _worktree(repo, "wt-dirty")
    (path / "uncommitted.txt").write_text("wip\n")
    result = _run(_executor(tmp_path), repo, path)
    assert result.outcome != ex.REMOVED
    assert path.is_dir(), "a dirty worktree must survive"


def test_ac1_f11_evidence_changed_between_scan_and_execute_aborts(repo, tmp_path):
    """The scan said AUTO_SAFE; by the time we hold the lock a human has started
    working in the tree. The stale verdict must not authorise anything."""
    path = _worktree(repo, "wt-changed")
    stale = wj.classify({"worktree_path": str(path)}, _policy(tmp_path),
                        task=_task(), evidence_collected_at=1e12, now=1e12, **_probes())
    assert stale.actionable, "precondition: the scan really did say AUTO_SAFE"

    (path / "human-was-here.txt").write_text("mine\n")  # now dirty
    result = _run(_executor(tmp_path), repo, path, classification=stale)
    assert result.outcome == ex.ABORTED
    assert result.reason == ex.EVIDENCE_CHANGED
    assert path.is_dir(), "nothing may be removed once evidence changed"


def test_ac1_a_stale_verdict_is_never_what_authorises(repo, tmp_path):
    """Passing in a hand-made AUTO_SAFE verdict for a dirty worktree must not
    get it deleted -- the fresh classification under the lock decides."""
    path = _worktree(repo, "wt-lying")
    (path / "wip.txt").write_text("x\n")
    forged = wj.Classification(wj.AUTO_SAFE, (), (), worktree_path=str(path))
    result = _run(_executor(tmp_path), repo, path, classification=forged)
    assert result.outcome == ex.ABORTED
    assert path.is_dir()


def test_ac1_an_unknown_probe_is_not_actionable(repo, tmp_path):
    """Fail-closed carries into the executor: a liveness probe that could not
    look means UNKNOWN, and UNKNOWN is never removed."""
    path = _worktree(repo, "wt-unknown")
    executor = _executor(tmp_path)
    result = executor.execute({"worktree_path": str(path), "node_id": "local"},
                              task=_task(), repo_path=str(repo), dry_run=False,
                              process_cwds=None, tmux_paths=[], session_paths=[],
                              service_roots=[])
    assert result.outcome == ex.ABORTED
    assert path.is_dir()


# == AC2 / I1: force=False, always =========================================

def test_ac2_remove_is_called_with_force_false(repo, tmp_path, monkeypatch):
    """Asserted on the ACTUAL call arguments."""
    from terminal_mcp import git_worktree

    seen = {}
    real = git_worktree.remove_worktree

    def _spy(repo_path, worktree_path, *, force=False):
        seen["force"] = force
        seen["worktree_path"] = worktree_path
        return real(repo_path, worktree_path, force=force)

    monkeypatch.setattr(ex.git_worktree, "remove_worktree", _spy)
    path = _worktree(repo, "wt-force")
    result = _run(_executor(tmp_path), repo, path)
    assert result.outcome == ex.REMOVED
    assert seen["force"] is False, "the janitor must never pass force=True"


def test_ac2_i1_no_force_path_exists_in_the_executor():
    """Structural, via the AST: no call anywhere passes force=True, and no
    deletion primitive is reachable other than git_worktree.remove_worktree."""
    import ast

    tree = ast.parse(Path(ex.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "force":
                    assert isinstance(kw.value, ast.Constant) and kw.value.value is False, \
                        "force= must be a literal False everywhere"
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    for forbidden in ("rmtree", "unlink", "rmdir", "removedirs"):
        assert forbidden not in called, f"executor must not call {forbidden}()"
    assert "shutil" not in {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}


def test_ac2_git_refusal_is_never_retried_with_force(repo, tmp_path, monkeypatch):
    """git refused (its own dirty check is a second guard behind ours). The
    executor must report and stop -- no fallback, no escalation."""
    from terminal_mcp import git_worktree

    calls = []

    def _refuse(repo_path, worktree_path, *, force=False):
        calls.append(force)
        return {"error": "WORKTREE_DIRTY", "detail": "contains modified or untracked files"}

    monkeypatch.setattr(ex.git_worktree, "remove_worktree", _refuse)
    path = _worktree(repo, "wt-refused")
    result = _run(_executor(tmp_path), repo, path)
    assert result.outcome == ex.FAILED
    assert result.reason == "WORKTREE_DIRTY"
    assert calls == [False], "exactly one attempt, with force=False"
    assert path.is_dir()


# == AC3 / I3 / I7: disk pressure never overrides safety ===================

def test_ac3_i3_i7_a_dirty_worktree_survives_99_percent_full(repo, tmp_path, monkeypatch):
    """Disk pressure simulated at the most extreme: 99% full, 0 bytes free.
    Pressure may shorten grace and reorder candidates; it may never relax a
    predicate. A dirty tree stays."""
    monkeypatch.setattr(ex.wj, "reclaimable_bytes", lambda *a, **k: (10 ** 12, False))

    class _Full:
        percent = 99.0
        free_bytes = 0

    monkeypatch.setattr("terminal_mcp.host_metrics.collect", lambda **k: _Full(), raising=False)
    path = _worktree(repo, "wt-pressure")
    (path / "precious.txt").write_text("unsaved work\n")
    result = _run(_executor(tmp_path, mode="auto_execute"), repo, path)
    assert result.outcome != ex.REMOVED
    assert path.is_dir()
    assert (path / "precious.txt").read_text() == "unsaved work\n"


def test_ac3_an_unmerged_worktree_survives_pressure_too(repo, tmp_path):
    path = _worktree(repo, "wt-unmerged")
    (path / "new.txt").write_text("real work\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "unmerged")
    result = _run(_executor(tmp_path), repo, path)
    assert result.outcome == ex.ABORTED
    assert path.is_dir()


# == AC4: the lock ========================================================

def test_ac4_lock_key_matches_the_contract_spelling():
    assert ex.lock_key("dell-5530", "/a/b") == "worktree_janitor:dell-5530:/a/b"
    # A missing node id normalises rather than collapsing two nodes onto one key.
    assert ex.lock_key(None, "/a/b") == "worktree_janitor:local:/a/b"
    assert ex.lock_key("", "/a/b") == "worktree_janitor:local:/a/b"


def test_ac4_a_lock_held_by_another_janitor_blocks_this_one(repo, tmp_path):
    locks = ResourceLockStore(tmp_path / "locks.db")
    path = _worktree(repo, "wt-locked")
    locks.acquire(ex.DEFAULT_LOCK_PROJECT, ex.lock_key("local", str(path)),
                  "some-other-janitor", reason="already working")
    result = _run(_executor(tmp_path, locks=locks), repo, path)
    assert result.outcome == ex.SKIPPED
    assert result.reason == ex.LOCK_UNAVAILABLE
    assert path.is_dir()


def test_ac4_two_concurrent_janitors_produce_exactly_one_removal(repo, tmp_path):
    """The real race, with real threads against one real lock store."""
    locks = ResourceLockStore(tmp_path / "locks.db")
    path = _worktree(repo, "wt-race")
    barrier = threading.Barrier(2)
    results = []

    def _worker(owner):
        executor = ex.WorktreeExecutor(_policy(tmp_path), locks=locks, node_id="local",
                                       owner_id=owner)
        barrier.wait()
        results.append(_run(executor, repo, path))

    threads = [threading.Thread(target=_worker, args=(f"janitor-{n}",)) for n in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    removed = [r for r in results if r.outcome == ex.REMOVED]
    assert len(removed) == 1, [(r.outcome, r.reason) for r in results]
    assert not path.exists()
    # The loser must not claim success, and must not have deleted anything.
    other = [r for r in results if r.outcome != ex.REMOVED]
    assert len(other) == 1
    assert other[0].outcome in (ex.SKIPPED, ex.ALREADY_GONE)


def test_ac4_the_lock_is_released_after_a_run(repo, tmp_path):
    locks = ResourceLockStore(tmp_path / "locks.db")
    path = _worktree(repo, "wt-release")
    _run(_executor(tmp_path, locks=locks), repo, path)
    assert locks.holder(ex.DEFAULT_LOCK_PROJECT, ex.lock_key("local", str(path))) is None


def test_ac4_a_lock_store_failure_never_authorises_a_removal(repo, tmp_path):
    class _Broken:
        def acquire(self, *a, **k):
            raise RuntimeError("lock db gone")

    path = _worktree(repo, "wt-brokenlock")
    result = _run(_executor(tmp_path, locks=_Broken()), repo, path)
    assert result.outcome == ex.SKIPPED
    assert result.reason == ex.LOCK_UNAVAILABLE
    assert path.is_dir()


# == AC5 / I6: audit before and after, with bytes, without content =========

def test_ac5_i6_an_audit_row_is_written_before_and_after(repo, tmp_path):
    audit = AuditStore(tmp_path / "audit.db")
    path = _worktree(repo, "wt-audit")
    result = _run(_executor(tmp_path, audit=audit), repo, path)
    assert result.outcome == ex.REMOVED

    rows = audit.list(limit=50)
    actions = [r["action"] for r in rows]
    assert ex.AUDIT_ATTEMPT in actions, "the attempt must be logged BEFORE the removal"
    assert ex.AUDIT_RESULT in actions, "the result must be logged after"
    attempt = next(r for r in rows if r["action"] == ex.AUDIT_ATTEMPT)
    outcome = next(r for r in rows if r["action"] == ex.AUDIT_RESULT)
    assert "reclaimed_bytes=" in attempt["reason"]
    assert "reclaimed_bytes=" in outcome["reason"]
    assert outcome["result"] == "OK"
    assert str(path) in attempt["reason"]


def test_ac5_the_attempt_row_survives_a_failure_after_it(repo, tmp_path, monkeypatch):
    """The point of logging the attempt first: a process that dies during the
    removal still leaves a record of what it was about to do."""
    audit = AuditStore(tmp_path / "audit.db")
    monkeypatch.setattr(ex.git_worktree, "remove_worktree",
                        lambda *a, **k: {"error": "WORKTREE_REMOVE_FAILED", "detail": "boom"})
    path = _worktree(repo, "wt-auditfail")
    _run(_executor(tmp_path, audit=audit), repo, path)
    actions = [r["action"] for r in audit.list(limit=50)]
    assert ex.AUDIT_ATTEMPT in actions and ex.AUDIT_RESULT in actions


def test_ac5_i6_the_audit_never_records_file_content(repo, tmp_path):
    """A cleanup audit that stored the secret would defeat the denial it is
    recording. Paths and sizes only."""
    audit = AuditStore(tmp_path / "audit.db")
    path = _worktree(repo, "wt-secret")
    (path / ".env").write_text("SECRET_VALUE_MUST_NEVER_BE_LOGGED=1\n")
    _run(_executor(tmp_path, audit=audit), repo, path)  # BLOCKED by valuable data
    serialized = repr(audit.list(limit=50))
    assert "SECRET_VALUE_MUST_NEVER_BE_LOGGED" not in serialized
    # text is deliberately unset so AuditStore does not fingerprint/preview it.
    assert all(r.get("text_preview") in (None, "") for r in audit.list(limit=50))
    assert path.is_dir(), "a worktree holding a .env is never removed"


def test_ac5_audit_failure_does_not_change_the_outcome(repo, tmp_path):
    class _BrokenAudit:
        def record(self, **kwargs):
            raise RuntimeError("audit db gone")

    path = _worktree(repo, "wt-brokenaudit")
    result = _run(_executor(tmp_path, audit=_BrokenAudit()), repo, path)
    assert result.outcome == ex.REMOVED, "logging must never decide the outcome"
    assert not path.exists()


# == AC6 / F6 / F7: idempotent convergence, and prune discipline ===========

def test_ac6_f6_an_already_removed_directory_converges_to_done(repo, tmp_path):
    store = QueueStore(tmp_path / "queue.db")
    task_id, = store.append_tasks("demo", [{
        "title": "t", "prompt": "p" * 60,
        "metadata": {"git_isolation": {"worktree_path": str(tmp_path / "gone")},
                     wc.METADATA_KEY: {"state": wc.CLEANUP_PENDING}}}])
    task = {"id": task_id, "status": "COMPLETED", "attempt_count": 1, "max_attempts": 3,
            "terminal_at": 0.0, "metadata": store.get_task(task_id).metadata}
    executor = _executor(tmp_path, store=store)
    result = executor.execute({"worktree_path": str(tmp_path / "gone"), "node_id": "local"},
                              task=task, repo_path=str(repo), dry_run=False, **_probes())
    assert result.outcome == ex.ALREADY_GONE
    record = store.get_task(task_id).metadata[wc.METADATA_KEY]
    assert record["state"] == wc.CLEANUP_DONE


def test_ac6_convergence_is_repeatable(repo, tmp_path):
    executor = _executor(tmp_path)
    missing = {"worktree_path": str(tmp_path / "never-existed"), "node_id": "local"}
    first = executor.execute(missing, task=_task(), repo_path=str(repo), dry_run=False, **_probes())
    second = executor.execute(missing, task=_task(), repo_path=str(repo), dry_run=False, **_probes())
    assert first.outcome == second.outcome == ex.ALREADY_GONE


def test_ac6_f7_prune_runs_only_after_a_real_removal(repo, tmp_path, monkeypatch):
    """Never speculative: pruning while another worktree is mid-creation is F7."""
    pruned = []
    real = ex.git_worktree._run_git

    def _spy(args, cwd, **kwargs):
        if args[:2] == ["worktree", "prune"]:
            pruned.append(cwd)
        return real(args, cwd, **kwargs)

    monkeypatch.setattr(ex.git_worktree, "_run_git", _spy)

    dirty = _worktree(repo, "wt-noprune")
    (dirty / "wip.txt").write_text("x\n")
    _run(_executor(tmp_path), repo, dirty)
    assert pruned == [], "a refused candidate must not trigger a prune"

    clean = _worktree(repo, "wt-doprune")
    result = _run(_executor(tmp_path), repo, clean)
    assert result.outcome == ex.REMOVED
    assert result.pruned is True
    assert len(pruned) == 1


# == AC7: bounded retries, no partial state ===============================

def test_ac7_attempts_are_bounded_then_become_review(repo, tmp_path, monkeypatch):
    store = QueueStore(tmp_path / "queue.db")
    task_id, = store.append_tasks("demo", [{
        "title": "t", "prompt": "p" * 60,
        "metadata": {"git_isolation": {"worktree_path": "/x"},
                     wc.METADATA_KEY: {"state": wc.CLEANUP_PENDING, "attempts": 2}}}])
    monkeypatch.setattr(ex.git_worktree, "remove_worktree",
                        lambda *a, **k: {"error": "WORKTREE_REMOVE_FAILED", "detail": "transient"})
    path = _worktree(repo, "wt-bounded")
    task = {"id": task_id, "status": "COMPLETED", "attempt_count": 1, "max_attempts": 3,
            "terminal_at": 0.0, "metadata": store.get_task(task_id).metadata}
    result = _run(_executor(tmp_path, store=store, max_attempts=3), repo, path, task=task)
    assert result.outcome == ex.FAILED
    record = store.get_task(task_id).metadata[wc.METADATA_KEY]
    assert record["attempts"] == 3
    assert record["state"] == wc.CLEANUP_REVIEW, "bounded -- never an infinite retry loop"


def test_ac7_an_exhausted_task_is_not_attempted_again(repo, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ex.git_worktree, "remove_worktree",
                        lambda *a, **k: calls.append(1) or {"removed": True})
    path = _worktree(repo, "wt-exhausted")
    task = _task(metadata={wc.METADATA_KEY: {"state": wc.CLEANUP_PENDING, "attempts": 3}})
    result = _run(_executor(tmp_path, max_attempts=3), repo, path, task=task)
    assert result.outcome == ex.SKIPPED
    assert result.reason == ex.ATTEMPTS_EXHAUSTED
    assert calls == [], "no removal may be attempted once the budget is spent"
    assert path.is_dir()


def test_ac7_a_permission_denied_subtree_leaves_no_partial_state(repo, tmp_path):
    """git either removes a worktree or refuses; it never half-removes one. The
    executor must not paper over a refusal, and must leave the tree intact."""
    path = _worktree(repo, "wt-perm")
    locked = path / "locked"
    locked.mkdir()
    (locked / "file.txt").write_text("x\n")
    locked.chmod(0o500)  # no write => the directory cannot be emptied
    try:
        result = _run(_executor(tmp_path), repo, path)
        assert result.outcome != ex.REMOVED or path.exists() is False
        if result.outcome != ex.REMOVED:
            assert path.is_dir(), "a failed removal must leave the worktree intact"
    finally:
        locked.chmod(0o700)


# == AC8 / I8: dry_run and mode gates ====================================

def test_ac8_dry_run_defaults_to_true_and_removes_nothing(repo, tmp_path):
    """A caller that forgets the argument gets a report, never a deletion."""
    path = _worktree(repo, "wt-default")
    executor = _executor(tmp_path)
    result = executor.execute({"worktree_path": str(path), "node_id": "local"},
                              task=_task(), repo_path=str(repo), **_probes())
    assert result.outcome == ex.WOULD_REMOVE
    assert result.reason == ex.DRY_RUN
    assert path.is_dir()
    assert result.reclaimed_bytes and result.reclaimed_bytes > 0, \
        "a dry run still reports what it would reclaim"


@pytest.mark.parametrize("mode", ["observe_only", "suggest_only"])
def test_ac8_i8_nothing_is_removed_below_auto_execute(repo, tmp_path, mode):
    path = _worktree(repo, f"wt-{mode}")
    result = _run(_executor(tmp_path, mode=mode), repo, path, dry_run=False)
    assert result.outcome == ex.WOULD_REMOVE
    assert result.reason == ex.MODE_NOT_AUTO_EXECUTE
    assert path.is_dir()


def test_ac8_both_gates_must_open(repo, tmp_path):
    """auto_execute alone is not enough, and dry_run=False alone is not enough."""
    path = _worktree(repo, "wt-bothgates")
    assert _run(_executor(tmp_path, mode="auto_execute"), repo, path,
                dry_run=True).outcome == ex.WOULD_REMOVE
    assert path.is_dir()
    assert _run(_executor(tmp_path, mode="suggest_only"), repo, path,
                dry_run=False).outcome == ex.WOULD_REMOVE
    assert path.is_dir()
    assert _run(_executor(tmp_path, mode="auto_execute"), repo, path,
                dry_run=False).outcome == ex.REMOVED
    assert not path.exists()


# == metadata + surface ==================================================

def test_the_task_record_reaches_cleanup_done_with_reclaimed_bytes(repo, tmp_path):
    store = QueueStore(tmp_path / "queue.db")
    path = _worktree(repo, "wt-record")
    task_id, = store.append_tasks("demo", [{
        "title": "t", "prompt": "p" * 60,
        "metadata": {"git_isolation": {"worktree_path": str(path)},
                     wc.METADATA_KEY: {"state": wc.CLEANUP_PENDING}}}])
    task = {"id": task_id, "status": "COMPLETED", "attempt_count": 1, "max_attempts": 3,
            "terminal_at": 0.0, "metadata": store.get_task(task_id).metadata}
    result = _run(_executor(tmp_path, store=store), repo, path, task=task)
    assert result.outcome == ex.REMOVED
    record = store.get_task(task_id).metadata[wc.METADATA_KEY]
    assert record["state"] == wc.CLEANUP_DONE
    assert record["reclaimed_bytes"] >= 4096
    assert record["removed_at"]


def test_a_store_write_failure_does_not_turn_a_removal_into_a_failure(repo, tmp_path):
    class _BrokenStore:
        def patch_worktree_cleanup(self, *a, **k):
            raise RuntimeError("db gone")

    path = _worktree(repo, "wt-brokenstore")
    result = _run(_executor(tmp_path, store=_BrokenStore()), repo, path)
    assert result.outcome == ex.REMOVED
    assert not path.exists(), "the directory state is the truth"


def test_outcomes_are_pinned():
    assert set(ex.OUTCOMES) == {ex.REMOVED, ex.WOULD_REMOVE, ex.SKIPPED, ex.ABORTED,
                                ex.FAILED, ex.ALREADY_GONE}
    assert ex.ExecutionResult(ex.REMOVED, "/x").deleted_something is True
    for outcome in (ex.WOULD_REMOVE, ex.SKIPPED, ex.ABORTED, ex.FAILED, ex.ALREADY_GONE):
        assert ex.ExecutionResult(outcome, "/x").deleted_something is False


def test_the_main_worktree_can_never_be_removed_through_the_executor(repo, tmp_path):
    """I2, carried into the executor: the classifier refuses, so the executor
    cannot act even when pointed straight at the main checkout."""
    result = _run(_executor(tmp_path), repo, repo)
    assert result.outcome == ex.ABORTED
    assert repo.is_dir()
    assert (repo / "a.txt").exists()
