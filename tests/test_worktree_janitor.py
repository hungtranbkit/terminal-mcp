"""Worktree Janitor P0 -- the classification engine.

Contract: docs/WORKTREE_JANITOR.md. Every test runs against a REAL git
repository with REAL linked worktrees, real symlinks and real ignored files in
tmp_path. Never a mocked `git`: the merge, containment and ignored-file rules
ARE the product, and a mock proves nothing about what
`git status --ignored=matching`, `git merge-base --is-ancestor` or
`Path.resolve()` actually do. Same posture as tests/test_repo_read.py.

Tests are named for the invariant (I1-I8) or failure mode (F1-F13) they pin.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from terminal_mcp import worktree_janitor as wj
from terminal_mcp.worktree_janitor import AUTO_SAFE, BLOCKED, REVIEW, UNKNOWN, JanitorPolicy

_ENV = {"HOME": "/tmp", "PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=True, env={**_ENV, "HOME": str(cwd)})


@pytest.fixture
def repo(tmp_path):
    """A real repo on `main` with two commits and a .gitignore."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text("*.db\n.env\n__pycache__/\nbuild/\nscratch.log\n")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return root


def _worktree(repo, name, *, branch=None, base="main"):
    path = repo.parent / name
    _git(repo, "worktree", "add", "-q", "-b", branch or f"task/{name}", str(path), base)
    return path


def _policy(tmp_path, **kw):
    kw.setdefault("allowed_roots", (str(tmp_path),))
    kw.setdefault("grace_seconds", 300)
    return JanitorPolicy(**kw)


def _clean_merged_task(**kw):
    """A task record that satisfies the terminal+grace predicate."""
    base = {"id": "T1", "status": "COMPLETED", "attempt_count": 1, "max_attempts": 3,
            "terminal_at": 0.0}
    base.update(kw)
    return base


_DEFAULT_TASK = object()


def _classify(path, policy, *, task=_DEFAULT_TASK, **kw):
    """`task=None` genuinely means "no task owns this" (the orphan case);
    omitting it supplies a satisfied terminal+grace task so other predicates
    can be tested in isolation."""
    kw.setdefault("process_cwds", [])
    kw.setdefault("tmux_paths", [])
    kw.setdefault("session_paths", [])
    kw.setdefault("service_roots", [])
    kw.setdefault("evidence_collected_at", 1e12)
    kw.setdefault("now", 1e12)
    return wj.classify({"worktree_path": str(path)}, policy,
                       task=_clean_merged_task() if task is _DEFAULT_TASK else task, **kw)


# == I1: never --force, no delete capability at all =======================

def test_i1_module_has_no_delete_or_force_capability():
    """The central P0 promise, asserted structurally against the AST rather
    than by grepping prose -- the module has to be free to *document* the very
    calls it must never make."""
    import ast

    source = Path(wj.__file__).read_text()
    tree = ast.parse(source)
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    for forbidden in ("rmtree", "remove", "unlink", "rmdir", "removedirs",
                      "remove_worktree", "rename", "replace", "write_text",
                      "write_bytes", "mkdir", "chmod"):
        assert forbidden not in called, f"janitor must not call {forbidden}()"
    imported = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}
    assert "shutil" not in imported, "janitor must not import shutil"
    # "--force" as a string literal is NOT checked here: the module needs the
    # literal to compare against in its own guard, and the docstring has to be
    # free to name what it forbids. The guard's behaviour is pinned by
    # test_i1_git_helper_refuses_force_and_mutating_subcommands instead.


def test_i1_git_helper_refuses_force_and_mutating_subcommands():
    with pytest.raises(ValueError, match="non-read-only"):
        wj._git("/tmp", "worktree", "remove", "x")
    with pytest.raises(ValueError, match="non-read-only"):
        wj._git("/tmp", "checkout", "main")
    with pytest.raises(ValueError, match="non-read-only"):
        wj._git("/tmp", "clean", "-fd")
    with pytest.raises(ValueError, match="never passes --force"):
        wj._git("/tmp", "status", "--force")


def test_i1_readonly_allowlist_excludes_every_mutating_subcommand():
    forbidden = {"worktree", "checkout", "switch", "reset", "clean", "commit", "push",
                 "fetch", "pull", "merge", "rebase", "stash", "apply", "add", "rm", "mv",
                 "prune", "gc", "update-ref", "init", "clone", "filter-branch"}
    assert wj.READ_ONLY_GIT_SUBCOMMANDS & forbidden == set()


# == I2: main worktree / repo root never removable ========================

def test_i2_main_worktree_is_blocked(repo, tmp_path):
    result = _classify(repo, _policy(tmp_path))
    assert result.policy_class == BLOCKED
    assert wj.MAIN_WORKTREE in result.reasons
    assert result.actionable is False


def test_i2_configured_repo_root_is_blocked_even_if_it_looks_linked(repo, tmp_path):
    predicate = wj.predicate_not_main_worktree(str(repo), _policy(tmp_path),
                                               repo_roots=(str(repo),))
    assert predicate.outcome == BLOCKED
    assert predicate.reason == wj.MAIN_WORKTREE


def test_i2_a_linked_worktree_is_not_flagged_as_main(repo, tmp_path):
    path = _worktree(repo, "wt-linked")
    predicate = wj.predicate_not_main_worktree(str(path), _policy(tmp_path))
    assert predicate.value is True
    assert predicate.outcome == AUTO_SAFE


# == The four headline classification cases ==============================

def test_clean_and_merged_is_auto_safe(repo, tmp_path):
    """Case 1. A worktree whose branch tip is already an ancestor of main,
    with nothing uncommitted and nothing using it."""
    path = _worktree(repo, "wt-clean")  # branched from main, no new commits
    result = _classify(path, _policy(tmp_path))
    assert result.policy_class == AUTO_SAFE, result.reasons
    assert result.actionable is True
    assert result.size_bytes is not None and result.size_bytes > 0
    assert result.branch == "task/wt-clean"


def test_dirty_is_blocked(repo, tmp_path):
    """Case 2 (I3)."""
    path = _worktree(repo, "wt-dirty")
    (path / "uncommitted.txt").write_text("work in progress\n")
    result = _classify(path, _policy(tmp_path))
    assert result.policy_class == BLOCKED
    assert wj.DIRTY in result.reasons
    assert result.actionable is False


def test_unmerged_and_unpushed_is_blocked(repo, tmp_path):
    """Case 3 + F2: a commit that exists on no remote and is not merged. This
    work exists nowhere else, so it must never be collectable."""
    path = _worktree(repo, "wt-unmerged")
    (path / "new.txt").write_text("real work\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "unmerged work")
    result = _classify(path, _policy(tmp_path))
    assert result.policy_class == BLOCKED
    assert wj.UNMERGED_UNPUSHED in result.reasons


def test_active_process_reference_is_blocked_and_never_removable(repo, tmp_path):
    """Case 4. A clean+merged worktree that would otherwise be AUTO_SAFE is
    BLOCKED purely because something is working in it."""
    path = _worktree(repo, "wt-inuse")
    assert _classify(path, _policy(tmp_path)).policy_class == AUTO_SAFE  # baseline
    for kwargs, reason in (
        ({"process_cwds": [str(path / "subdir")]}, wj.PROCESS_IN_USE),
        ({"tmux_paths": [str(path)]}, wj.TMUX_IN_USE),
        ({"session_paths": [str(path)]}, wj.SESSION_IN_USE),
        ({"service_roots": [str(path)]}, wj.SERVICE_ROOT),
    ):
        result = _classify(path, _policy(tmp_path), **kwargs)
        assert result.policy_class == BLOCKED, (kwargs, result.reasons)
        assert reason in result.reasons
        assert result.actionable is False


# == Fail-closed / UNKNOWN ===============================================

def test_unknown_never_becomes_auto_safe_when_a_probe_cannot_look():
    """The fail-closed rule. A probe returning None means "we did not
    establish this", and unestablished is never safe."""
    predicate = wj.predicate_no_live_references("/tmp", JanitorPolicy(), process_cwds=None)
    assert predicate.value is None
    assert predicate.outcome == UNKNOWN


def test_combine_lets_the_worst_outcome_win():
    worst, reasons = wj.combine([
        wj.Predicate("a", True, AUTO_SAFE),
        wj.Predicate("b", None, UNKNOWN, wj.NODE_UNREACHABLE),
        wj.Predicate("c", True, AUTO_SAFE),
    ])
    assert worst == UNKNOWN
    assert wj.NODE_UNREACHABLE in reasons
    worst2, _ = wj.combine([wj.Predicate("a", None, UNKNOWN, "X"),
                            wj.Predicate("b", False, BLOCKED, "Y")])
    assert worst2 == BLOCKED, "a positively dangerous fact outranks a failed probe"


def test_no_allowed_roots_means_nothing_is_collectable(repo, tmp_path):
    path = _worktree(repo, "wt-noroots")
    result = _classify(path, JanitorPolicy(allowed_roots=(), grace_seconds=300))
    assert result.policy_class != AUTO_SAFE
    assert wj.PATH_NOT_ALLOWED in result.reasons


def test_path_outside_the_allowlist_is_blocked(repo, tmp_path):
    path = _worktree(repo, "wt-outside")
    narrow = JanitorPolicy(allowed_roots=(str(tmp_path / "elsewhere"),), grace_seconds=300)
    result = _classify(path, narrow)
    assert result.policy_class == BLOCKED
    assert wj.PATH_NOT_ALLOWED in result.reasons


def test_a_non_repo_directory_is_unknown_not_safe(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = _classify(plain, _policy(tmp_path))
    assert result.policy_class in (UNKNOWN, BLOCKED)
    assert result.actionable is False


# == F10: symlink / mount uncertainty ====================================

def test_f10_a_symlinked_worktree_path_is_blocked(repo, tmp_path):
    real = _worktree(repo, "wt-real")
    link = tmp_path / "wt-link"
    link.symlink_to(real)
    predicate = wj.predicate_path_allowlisted(str(link), _policy(tmp_path))
    assert predicate.outcome == BLOCKED
    assert predicate.reason == wj.SYMLINK_OR_MOUNT


# == F3: sensitive / valuable ignored data ===============================

@pytest.mark.parametrize("filename", ["secrets.db", ".env", "creds.pem"])
def test_f3_valuable_ignored_data_blocks_collection(repo, tmp_path, filename):
    path = _worktree(repo, "wt-valuable")
    (repo / ".gitignore").write_text("*.db\n.env\n*.pem\n")
    (path / ".gitignore").write_text("*.db\n.env\n*.pem\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "ignore rules")
    (path / filename).write_text("SECRET_VALUE_MUST_NOT_APPEAR\n")
    result = _classify(path, _policy(tmp_path))
    assert result.policy_class == BLOCKED
    assert wj.VALUABLE_IGNORED_DATA in result.reasons
    # Paths only -- the contract forbids ever recording contents.
    assert "SECRET_VALUE_MUST_NOT_APPEAR" not in str(result.to_dict())


def test_f3_cache_only_ignored_data_does_not_block(repo, tmp_path):
    path = _worktree(repo, "wt-cache")
    (path / "__pycache__").mkdir()
    (path / "__pycache__" / "m.cpython-314.pyc").write_bytes(b"\x00cached")
    (path / "build").mkdir()
    (path / "build" / "out.o").write_bytes(b"obj")
    result = _classify(path, _policy(tmp_path))
    assert result.policy_class == AUTO_SAFE, result.reasons


def test_f3_unrecognised_ignored_data_is_treated_as_valuable():
    """"We did not recognise it" is not evidence it was worthless."""
    valuable, safe = wj.classify_ignored_entries(["mystery-artifact.bin"], JanitorPolicy())
    assert valuable == ["mystery-artifact.bin"]
    assert safe == []


def test_f3_extra_valuable_globs_can_only_add_denials(repo, tmp_path):
    path = _worktree(repo, "wt-extra")
    (path / "scratch.log").write_text("x\n")  # ignored via the repo .gitignore
    wide = _policy(tmp_path, extra_valuable_globs=("*.log",))
    assert _classify(path, wide).policy_class == BLOCKED
    # Built-ins still apply alongside an operator's additions.
    valuable, _ = wj.classify_ignored_entries([".env"], wide)
    assert valuable == [".env"]


def test_credential_file_names_are_inherited_from_redaction():
    """Folded in rather than copied, so a name added to redaction.py is
    protected here automatically."""
    from terminal_mcp.redaction import CREDENTIAL_FILE_NAMES

    globs = JanitorPolicy().all_valuable_globs()
    for name in CREDENTIAL_FILE_NAMES:
        assert name in globs


# == Detached HEAD / preserved-unmerged ==================================

def test_detached_head_is_review_never_auto_safe(repo, tmp_path):
    path = _worktree(repo, "wt-detached")
    head = _git(path, "rev-parse", "HEAD").stdout.strip()
    _git(path, "checkout", "-q", "--detach", head)
    result = _classify(path, _policy(tmp_path))
    assert result.policy_class == REVIEW
    assert wj.DETACHED_HEAD in result.reasons
    assert result.actionable is False


def test_pushed_but_unmerged_is_review_by_default_and_auto_safe_only_on_optin(repo, tmp_path):
    path = _worktree(repo, "wt-pushed")
    (path / "n.txt").write_text("pushed work\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "pushed not merged")
    # Simulate "exists on a remote": a remote-tracking ref at this exact commit.
    head = _git(path, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/remotes/origin/task/wt-pushed", head)

    default = _classify(path, _policy(tmp_path))
    assert default.policy_class == REVIEW
    assert wj.PRESERVED_UNMERGED in default.reasons

    optin = _classify(path, _policy(tmp_path, allow_preserved_unmerged=True))
    assert optin.policy_class == AUTO_SAFE, optin.reasons


# == F1: bare FAILED is retryable ========================================

def test_f1_a_bare_failed_task_blocks_collection(repo, tmp_path):
    """There is no FAILED_FINAL. A retryable FAILED task's worktree is the
    retry's own working directory."""
    path = _worktree(repo, "wt-failed")
    task = _clean_merged_task(status="FAILED", attempt_count=1, max_attempts=3)
    result = _classify(path, _policy(tmp_path), task=task)
    assert result.policy_class == BLOCKED
    assert wj.TASK_NOT_FINAL in result.reasons
    assert "retryable" in "".join(p.detail or "" for p in result.predicates)


def test_f1_failed_with_retries_exhausted_is_final(repo, tmp_path):
    path = _worktree(repo, "wt-exhausted")
    task = _clean_merged_task(status="FAILED", attempt_count=3, max_attempts=3)
    assert _classify(path, _policy(tmp_path), task=task).policy_class == AUTO_SAFE


@pytest.mark.parametrize("status", ["COMPLETED", "SKIPPED", "CANCELLED"])
def test_the_three_terminal_statuses_are_accepted(repo, tmp_path, status):
    path = _worktree(repo, f"wt-{status.lower()}")
    task = _clean_merged_task(status=status)
    assert _classify(path, _policy(tmp_path), task=task).policy_class == AUTO_SAFE


@pytest.mark.parametrize("status", ["RUNNING", "QUEUED", "BLOCKED", "VERIFYING", "PAUSED"])
def test_a_non_terminal_task_blocks_collection(repo, tmp_path, status):
    path = _worktree(repo, f"wt-live-{status.lower()}")
    task = _clean_merged_task(status=status)
    result = _classify(path, _policy(tmp_path), task=task)
    assert result.policy_class == BLOCKED
    assert wj.TASK_NOT_FINAL in result.reasons


# == Grace, orphans, stale admin entries, freshness ======================

def test_grace_not_elapsed_is_review(repo, tmp_path):
    path = _worktree(repo, "wt-grace")
    task = _clean_merged_task(terminal_at=1e12 - 10)  # 10s ago, grace is 300s
    result = _classify(path, _policy(tmp_path), task=task)
    assert result.policy_class == REVIEW
    assert wj.GRACE_NOT_ELAPSED in result.reasons


def test_an_orphan_with_no_task_is_review_not_auto_safe(repo, tmp_path):
    path = _worktree(repo, "wt-orphan")
    result = _classify(path, _policy(tmp_path), task=None)
    assert result.policy_class == REVIEW
    assert wj.ORPHAN_UNCONFIRMED in result.reasons


def test_f7_a_stale_admin_entry_is_reported_not_acted_on(tmp_path):
    """git still lists a worktree whose directory is gone. P0 reports it; it
    must not prune (that is P2's job, under a lock)."""
    result = wj.classify({"worktree_path": str(tmp_path / "vanished")}, _policy(tmp_path))
    assert result.policy_class == REVIEW
    assert wj.ADMIN_ENTRY_STALE in result.reasons


def test_f11_stale_evidence_is_review(repo, tmp_path):
    path = _worktree(repo, "wt-stale")
    result = _classify(path, _policy(tmp_path), evidence_collected_at=1e12 - 9999)
    assert result.policy_class == REVIEW
    assert wj.EVIDENCE_STALE in result.reasons


def test_missing_evidence_timestamp_is_unknown():
    predicate = wj.predicate_evidence_fresh(None, JanitorPolicy())
    assert predicate.value is None
    assert predicate.outcome == UNKNOWN


# == I5: classification is side-effect free ==============================

def test_i5_classification_changes_nothing_on_disk(repo, tmp_path):
    path = _worktree(repo, "wt-pure")
    before = {p: p.stat().st_mtime_ns for p in sorted(path.rglob("*")) if p.is_file()}
    listing_before = sorted(str(p) for p in tmp_path.rglob("*"))
    _classify(path, _policy(tmp_path))
    after = {p: p.stat().st_mtime_ns for p in sorted(path.rglob("*")) if p.is_file()}
    assert before == after, "classification must not touch any file"
    assert sorted(str(p) for p in tmp_path.rglob("*")) == listing_before
    assert path.is_dir(), "the worktree must still exist after classification"


def test_i5_classification_does_not_remove_the_worktree_even_when_auto_safe(repo, tmp_path):
    path = _worktree(repo, "wt-stillhere")
    assert _classify(path, _policy(tmp_path)).policy_class == AUTO_SAFE
    assert path.is_dir(), "AUTO_SAFE must not mean 'already deleted'"
    assert (path / "a.txt").exists()


# == I8: observe_only default, and scan() reports only ====================

def test_i8_default_mode_is_observe_only():
    from terminal_mcp.config import WorktreeJanitorConfig

    assert WorktreeJanitorConfig().mode == "observe_only"
    assert JanitorPolicy().mode == "observe_only"


def test_scan_reports_every_worktree_and_removes_nothing(repo, tmp_path):
    clean = _worktree(repo, "wt-s-clean")
    dirty = _worktree(repo, "wt-s-dirty")
    (dirty / "wip.txt").write_text("wip\n")
    report = wj.scan(str(repo), _policy(tmp_path), probe_local=False,
                     process_cwds=[], tmux_paths=[], session_paths=[], service_roots=[],
                     tasks_by_worktree={str(clean): _clean_merged_task(),
                                        str(dirty): _clean_merged_task(id="T2")},
                     now=1e12, evidence_collected_at=1e12)
    assert report["observe_only"] is True
    assert report["executor_present"] is False
    assert report["total"] == 3  # main + two linked
    by_path = {c["worktree_path"]: c for c in report["candidates"]}
    assert by_path[str(clean)]["policy_class"] == AUTO_SAFE
    assert by_path[str(dirty)]["policy_class"] == BLOCKED
    assert by_path[str(repo)]["policy_class"] == BLOCKED  # main worktree
    assert report["counts"][BLOCKED] == 2
    assert report["actionable_count"] == 1
    assert "does not free any other mount" in report["filesystem_note"]
    # Nothing was removed.
    assert clean.is_dir() and dirty.is_dir() and repo.is_dir()


def test_scan_on_a_non_repo_reports_an_error_not_a_crash(tmp_path):
    plain = tmp_path / "nope"
    plain.mkdir()
    report = wj.scan(str(plain), _policy(tmp_path))
    assert report["error"] == wj.GIT_UNAVAILABLE
    assert report["candidates"] == []


# == reclaimable bytes ===================================================

def test_reclaimable_bytes_counts_real_files_and_never_follows_symlinks(repo, tmp_path):
    path = _worktree(repo, "wt-size")
    (path / "big.bin").write_bytes(b"x" * 5000)
    outside = tmp_path / "outside-huge.bin"
    outside.write_bytes(b"y" * 100_000)
    (path / "link-to-outside").symlink_to(outside)
    size, partial = wj.reclaimable_bytes(str(path))
    assert size >= 5000
    assert size < 100_000, "a symlink's target must not be counted"


def test_reclaimable_bytes_on_a_missing_path_is_partial():
    size, partial = wj.reclaimable_bytes("/no/such/path/anywhere")
    assert (size, partial) == (0, True)


# == contract surface ====================================================

def test_policy_classes_and_reason_codes_are_pinned():
    assert set(wj.POLICY_CLASSES) == {AUTO_SAFE, REVIEW, BLOCKED, UNKNOWN}
    for code in (wj.MAIN_WORKTREE, wj.DIRTY, wj.UNMERGED_UNPUSHED, wj.VALUABLE_IGNORED_DATA,
                 wj.PROCESS_IN_USE, wj.TMUX_IN_USE, wj.SESSION_IN_USE, wj.SERVICE_ROOT,
                 wj.DETACHED_HEAD, wj.SYMLINK_OR_MOUNT, wj.PATH_NOT_ALLOWED,
                 wj.EVIDENCE_STALE, wj.TASK_NOT_FINAL, wj.ADMIN_ENTRY_STALE):
        assert code in wj.REASON_CODES


def test_only_auto_safe_is_actionable():
    for policy_class in wj.POLICY_CLASSES:
        result = wj.Classification(policy_class, (), ())
        assert result.actionable == (policy_class == AUTO_SAFE)


def test_no_executor_exists_in_this_build():
    """P0's scope boundary, asserted. If someone adds an executor, they must
    delete this test deliberately."""
    for name in ("execute", "remove", "prune", "cleanup", "run_once", "reclaim"):
        assert not hasattr(wj, name), f"P0 must not expose {name}()"


# == local probes ========================================================

def test_probes_return_none_when_they_cannot_look_not_an_empty_list(monkeypatch):
    """The distinction that keeps fail-closed working: None means UNKNOWN, []
    means a confident "nothing". A probe that swallowed its own failure into []
    would silently turn every candidate AUTO_SAFE."""
    def _boom(*args, **kwargs):
        raise FileNotFoundError("not installed")

    monkeypatch.setattr(wj.subprocess, "run", _boom)
    assert wj.collect_tmux_paths() is None
    assert wj.collect_service_roots() is None


def test_tmux_probe_reports_empty_when_no_server_is_running(monkeypatch):
    class _Result:
        returncode = 1
        stdout = ""
        stderr = "no server running on /tmp/tmux-1000/default"

    monkeypatch.setattr(wj.subprocess, "run", lambda *a, **k: _Result())
    assert wj.collect_tmux_paths() == [], "no server is a real zero, not UNKNOWN"


def test_scan_probes_locally_and_never_claims_to_know_about_another_node(repo, tmp_path):
    """scan()'s probes are local-only by construction. session_paths is left
    caller-supplied (the scanner does not own session_registry), so it stays
    UNKNOWN rather than being guessed -- which is what keeps a controller from
    answering F4-style about a remote node."""
    _worktree(repo, "wt-probe")
    report = wj.scan(str(repo), _policy(tmp_path), now=1e12, evidence_collected_at=1e12)
    candidate = next(c for c in report["candidates"] if c["worktree_path"].endswith("wt-probe"))
    live = next(p for p in candidate["predicates"] if p["name"] == "no_live_references")
    assert live["value"] is None
    assert live["reason"] == wj.NODE_UNREACHABLE
    assert "session" in (live["detail"] or "")
    assert candidate["policy_class"] != AUTO_SAFE


def test_a_real_tmux_pane_inside_a_worktree_blocks_it(repo, tmp_path):
    """End-to-end on the real probe path, with the pane list supplied as the
    probe would return it."""
    path = _worktree(repo, "wt-realtmux")
    result = _classify(path, _policy(tmp_path), tmux_paths=[str(path / "src")])
    assert result.policy_class == BLOCKED
    assert wj.TMUX_IN_USE in result.reasons
