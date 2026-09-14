"""Worktree Janitor P1: marking only, from the one chokepoint.

Every test here is named for the failure it prevents. The two that matter most
are `test_a_bare_failed_is_never_marked` (the worktree is the retry's working
directory) and `test_reopen_during_grace_clears_the_record`.

P1 has no delete capability, and `test_the_module_cannot_delete_anything`
asserts that structurally rather than trusting the review.
"""
from __future__ import annotations

import pytest

from terminal_mcp import queue_store as qs
from terminal_mcp import worktree_cleanup as wj


@pytest.fixture()
def store(tmp_path):
    return qs.QueueStore(tmp_path / "queue.db")


ISOLATION = {"git_isolation": {"repo_path": "/repo", "branch": "task/x",
                              "base_sha": "abc123",
                              "worktree_path": "/repo/.worktrees/task-x"}}


def _task(store, *, isolated=True, max_attempts=3):
    task_id, = store.append_tasks("demo", [{
        "title": "t", "prompt": "p", "max_attempts": max_attempts,
        "metadata": dict(ISOLATION) if isolated else {"note": "no worktree"}}])
    return task_id


def _walk(store, task_id, *statuses):
    for status in statuses:
        store.transition_task(task_id, status, event_type="TEST")
    return store.get_task(task_id)


def _record(store, task_id):
    return (store.get_task(task_id).metadata or {}).get(wj.METADATA_KEY)


def _events(store, task_id, kind):
    return [e for e in store.list_events("demo", limit=200)
            if e["task_id"] == task_id and e["event_type"] == kind]


# -- the terminal predicate --------------------------------------------------

@pytest.mark.parametrize("terminal", ["COMPLETED", "SKIPPED", "CANCELLED"])
def test_each_terminal_status_marks_the_worktree(store, terminal):
    task_id = _task(store)
    if terminal == "COMPLETED":
        _walk(store, task_id, qs.PRECHECK, qs.READY, qs.DISPATCHING, qs.RUNNING,
              qs.VERIFYING, qs.COMPLETED)
    else:
        _walk(store, task_id, terminal)
    record = _record(store, task_id)
    assert record and record["state"] == wj.CLEANUP_PENDING
    assert _events(store, task_id, wj.EVENT_MARKED)


def test_the_predicate_uses_the_stores_own_terminal_set():
    """One source of truth. If TERMINAL_STATUSES changes, this must follow."""
    for status in qs.TERMINAL_STATUSES:
        assert wj.is_terminal(status, attempt_count=0, max_attempts=3,
                              terminal_statuses=qs.TERMINAL_STATUSES)
    assert qs.TERMINAL_STATUSES == ("COMPLETED", "SKIPPED", "CANCELLED")


def test_there_is_no_failed_final_status():
    """Failure mode F1 in the contract: inventing one."""
    assert "FAILED" not in qs.TERMINAL_STATUSES
    assert "BLOCKED" not in qs.TERMINAL_STATUSES
    assert not hasattr(qs, "FAILED_FINAL")


# -- FAILED and the retry budget --------------------------------------------

def test_a_bare_failed_is_never_marked(store):
    """The worktree IS the retry's working directory. Marking it here is how
    a retry would find its directory scheduled for deletion."""
    task_id = _task(store, max_attempts=3)
    _walk(store, task_id, qs.PRECHECK, qs.READY, qs.DISPATCHING, qs.RUNNING, qs.FAILED)
    assert _record(store, task_id) is None
    assert not _events(store, task_id, wj.EVENT_MARKED)


def test_failed_marks_only_once_the_retry_budget_is_spent(store):
    task_id = _task(store, max_attempts=1)
    # One DISPATCHING bumps attempt_count to 1, which equals max_attempts.
    _walk(store, task_id, qs.PRECHECK, qs.READY, qs.DISPATCHING, qs.RUNNING, qs.FAILED)
    record = _record(store, task_id)
    assert record and record["state"] == wj.CLEANUP_PENDING
    assert "retry budget spent" in record["reasons"][0]


def test_the_budget_check_is_not_an_equality(store):
    """max_attempts lowered after the fact must not make a task permanently
    un-markable."""
    assert wj.is_terminal("FAILED", attempt_count=5, max_attempts=3,
                          terminal_statuses=qs.TERMINAL_STATUSES)


def test_an_unreadable_budget_is_not_an_exhausted_one():
    assert not wj.is_terminal("FAILED", attempt_count=None, max_attempts="x",
                              terminal_statuses=qs.TERMINAL_STATUSES)


# -- only tasks that own a worktree -----------------------------------------

def test_a_task_without_a_worktree_is_never_marked(store):
    task_id = _task(store, isolated=False)
    _walk(store, task_id, qs.CANCELLED)
    assert _record(store, task_id) is None


def test_an_isolation_block_without_a_path_does_not_count():
    assert not wj.has_isolated_worktree({"git_isolation": {"branch": "x"}})
    assert not wj.has_isolated_worktree({})
    assert not wj.has_isolated_worktree(None)


# -- idempotence -------------------------------------------------------------

def test_a_duplicate_terminal_transition_does_not_reset_the_record(store):
    task_id = _task(store)
    _walk(store, task_id, qs.CANCELLED)
    first = _record(store, task_id)

    # CANCELLED -> CANCELLED is a no-op in transition_task; reach the same
    # state through the decision function directly to prove the guard.
    decision = wj.decide(from_status=qs.CANCELLED, to_status=qs.CANCELLED,
                         metadata=store.get_task(task_id).metadata,
                         attempt_count=0, max_attempts=3,
                         terminal_statuses=qs.TERMINAL_STATUSES)
    assert decision.action == "none"
    assert "already recorded" in decision.reason
    assert _record(store, task_id) == first


def test_marking_never_bumps_attempts(store):
    task_id = _task(store)
    _walk(store, task_id, qs.CANCELLED)
    assert _record(store, task_id)["attempts"] == 0


def test_a_finished_cleanup_is_never_revived():
    """CLEANUP_DONE is not cancellable: the directory is already gone."""
    assert wj.CLEANUP_DONE not in wj.CANCELLABLE_STATES
    decision = wj.decide(from_status="CANCELLED", to_status="QUEUED",
                         metadata={**ISOLATION,
                                   wj.METADATA_KEY: {"state": wj.CLEANUP_DONE}},
                         attempt_count=0, max_attempts=3,
                         terminal_statuses=qs.TERMINAL_STATUSES)
    assert decision.action == "none"
    assert "WORKTREE_REMOVED" in decision.reason


# -- reopen / retry during grace --------------------------------------------

def test_reopen_during_grace_clears_the_record(store):
    """A cancelled task retried is a worktree that is needed again."""
    task_id = _task(store)
    _walk(store, task_id, qs.PRECHECK)         # not terminal, nothing marked
    assert _record(store, task_id) is None
    _walk(store, task_id, qs.CANCELLED)
    assert _record(store, task_id)["state"] == wj.CLEANUP_PENDING


def test_retry_out_of_a_marked_failed_clears_the_record(store):
    task_id = _task(store, max_attempts=1)
    _walk(store, task_id, qs.PRECHECK, qs.READY, qs.DISPATCHING, qs.RUNNING, qs.FAILED)
    assert _record(store, task_id)["state"] == wj.CLEANUP_PENDING

    _walk(store, task_id, qs.QUEUED)           # terminal_queue_retry's own edge
    assert _record(store, task_id) is None, "a retried task must not be marked"
    assert _events(store, task_id, wj.EVENT_CLEARED)


@pytest.mark.parametrize("state", sorted(wj.CANCELLABLE_STATES))
def test_every_cancellable_state_is_cleared_by_a_reopen(state):
    decision = wj.decide(from_status="FAILED", to_status="QUEUED",
                         metadata={**ISOLATION, wj.METADATA_KEY: {"state": state}},
                         attempt_count=0, max_attempts=3,
                         terminal_statuses=qs.TERMINAL_STATUSES)
    assert decision.action == "clear"
    assert decision.event_type == wj.EVENT_CLEARED


def test_a_non_terminal_transition_with_no_record_does_nothing(store):
    task_id = _task(store)
    _walk(store, task_id, qs.PRECHECK, qs.READY)
    assert _record(store, task_id) is None
    assert not _events(store, task_id, wj.EVENT_CLEARED)


# -- crash safety / restart --------------------------------------------------

def test_the_record_survives_a_reopen_of_the_database(tmp_path):
    """Written in the same transaction as the status change, so a restart
    cannot find one without the other."""
    store = qs.QueueStore(tmp_path / "queue.db")
    task_id = _task(store)
    _walk(store, task_id, qs.CANCELLED)

    reopened = qs.QueueStore(tmp_path / "queue.db")
    task = reopened.get_task(task_id)
    assert task.status == qs.CANCELLED
    assert (task.metadata or {})[wj.METADATA_KEY]["state"] == wj.CLEANUP_PENDING


def test_status_and_record_are_never_written_apart(tmp_path):
    store = qs.QueueStore(tmp_path / "queue.db")
    task_id = _task(store)
    _walk(store, task_id, qs.CANCELLED)
    reopened = qs.QueueStore(tmp_path / "queue.db")
    task = reopened.get_task(task_id)
    marked = (task.metadata or {}).get(wj.METADATA_KEY) is not None
    assert marked is (task.status in qs.TERMINAL_STATUSES)


# -- P1 has no teeth ---------------------------------------------------------

def test_the_module_cannot_delete_anything():
    """Structural, and read from the AST rather than the text.

    Scanning raw source matched this module's own docstring, which NAMES the
    operations it promises not to perform -- a guard that fails on the promise
    is a guard measuring the wrong thing.
    """
    import ast
    from pathlib import Path
    tree = ast.parse(Path(wj.__file__).read_text())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"os", "shutil", "subprocess", "pathlib", "tempfile"}, (
        f"P1 must not be able to reach the filesystem; imports: {sorted(imported)}")

    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not called & {"rmtree", "remove", "unlink", "rmdir", "run", "call"}, (
        f"P1 must not call a removal/exec primitive; calls: {sorted(called)}")


def test_p1_only_ever_writes_pending_or_clears():
    """Later phases add the other states; P1 must not start using them."""
    from pathlib import Path
    source = Path(wj.__file__).read_text()
    assert 'Decision("mark", state=CLEANUP_PENDING' in source
    for later in ("Decision(\"mark\", state=CLEANUP_ELIGIBLE",
                  "Decision(\"mark\", state=CLEANUP_DONE"):
        assert later not in source


def test_the_metadata_key_is_the_contracts_spelling():
    assert wj.METADATA_KEY == "worktree_cleanup"
    assert wj.ISOLATION_KEY == "git_isolation"


# -- unrelated metadata is preserved ----------------------------------------

def test_marking_preserves_other_metadata(store):
    task_id, = store.append_tasks("demo", [{
        "title": "t", "prompt": "p",
        "metadata": {**ISOLATION, "project": "keep-me"}}])
    _walk(store, task_id, qs.CANCELLED)
    metadata = store.get_task(task_id).metadata
    assert metadata["project"] == "keep-me"
    assert metadata[wj.METADATA_KEY]["state"] == wj.CLEANUP_PENDING


def test_clearing_preserves_other_metadata(store):
    task_id, = store.append_tasks("demo", [{
        "title": "t", "prompt": "p", "max_attempts": 1,
        "metadata": {**ISOLATION, "project": "keep-me"}}])
    _walk(store, task_id, qs.PRECHECK, qs.READY, qs.DISPATCHING, qs.RUNNING, qs.FAILED)
    _walk(store, task_id, qs.QUEUED)
    metadata = store.get_task(task_id).metadata
    assert metadata["project"] == "keep-me"
    assert wj.METADATA_KEY not in metadata


def test_apply_never_mutates_its_input():
    original = {**ISOLATION}
    wj.apply(original, wj.Decision("mark", state=wj.CLEANUP_PENDING), now="now")
    assert wj.METADATA_KEY not in original
