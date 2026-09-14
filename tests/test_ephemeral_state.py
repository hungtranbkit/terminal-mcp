"""The /tmp leak, and the property that stops it coming back.

The leak was not subtle in effect -- 153,073 stray directories on m910, 34% of
the tmpfs inode table, and a per-uid quota exhaustion that took down the shell
for every session of that uid. It was subtle in *measurement*: the directories
held 0.01 GB between them, so `df -h` showed nothing wrong right up to the
failure. These tests therefore count ENTRIES, never bytes.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from terminal_mcp import ephemeral_state as es


@pytest.fixture(autouse=True)
def _clean_root():
    es.cleanup_ephemeral_state()
    yield
    es.cleanup_ephemeral_state()


def _top_level_count() -> int:
    """Entries directly in the system temp dir matching our prefixes.

    Counts the TOP LEVEL only, because that is where the inode pressure was:
    anything nested inside one managed root is removed with it.
    """
    tmp = Path(tempfile.gettempdir())
    return len([p for p in tmp.iterdir() if p.name.startswith("terminal-mcp-")])


def test_many_stores_produce_exactly_one_top_level_directory():
    """The property the leak violated: repeated use must not grow /tmp."""
    before = _top_level_count()
    for i in range(50):
        es.ephemeral_db_path(f"store{i}", "x.db")
    after = _top_level_count()
    assert after - before == 1, (
        f"50 ephemeral stores added {after - before} top-level /tmp entries; "
        f"the whole point is that they share one root")


def test_every_store_gets_its_own_private_subdirectory():
    a = es.ephemeral_db_path("queue", "queue.db")
    b = es.ephemeral_db_path("queue", "queue.db")
    assert a != b, "two stores must not share a directory"
    assert a.parent.parent == b.parent.parent == es.current_root()


def test_the_subdirectory_is_named_for_its_purpose():
    """A wall of random names is not diagnosable; a named one is."""
    path = es.ephemeral_db_path("planner", "planner.db")
    assert path.parent.name.startswith("planner-")


def test_the_directory_is_writable_and_real():
    path = es.ephemeral_db_path("queue", "queue.db")
    path.write_text("x")
    assert path.read_text() == "x"


# -- cleanup, on success and on failure -------------------------------------

def test_cleanup_removes_everything():
    root = es.ephemeral_state_dir("queue").parent
    es.ephemeral_db_path("planner", "p.db").write_text("x")
    assert root.exists()

    es.cleanup_ephemeral_state()

    assert not root.exists()
    assert es.current_root() is None


def test_cleanup_runs_even_when_the_caller_failed():
    """Cleanup must not be conditional on the work having succeeded -- the
    leak's whole shape was directories outliving a call that went wrong."""
    root_holder = {}

    def _work_that_raises():
        root_holder["root"] = es.ephemeral_state_dir("queue").parent
        raise RuntimeError("store construction failed")

    with pytest.raises(RuntimeError):
        try:
            _work_that_raises()
        finally:
            es.cleanup_ephemeral_state()

    assert not root_holder["root"].exists()


def test_cleanup_is_idempotent():
    es.ephemeral_state_dir("queue")
    es.cleanup_ephemeral_state()
    es.cleanup_ephemeral_state()          # must not raise
    assert es.current_root() is None


def test_cleanup_never_raises_even_if_the_root_vanished():
    """It runs from atexit, where an exception would surface on the way out of
    an otherwise successful process."""
    import shutil
    root = es.ephemeral_state_dir("queue").parent
    shutil.rmtree(root)
    es.cleanup_ephemeral_state()          # must not raise


def test_a_new_root_is_created_after_cleanup():
    first = es.ephemeral_state_dir("queue").parent
    es.cleanup_ephemeral_state()
    second = es.ephemeral_state_dir("queue").parent
    assert second.exists() and second != first


def test_atexit_is_registered_once(monkeypatch):
    registered = []
    monkeypatch.setattr(es.atexit, "register", lambda fn: registered.append(fn))
    monkeypatch.setattr(es, "_atexit_registered", False)
    es.cleanup_ephemeral_state()
    for _ in range(5):
        es.ephemeral_state_dir("queue")
    assert len(registered) == 1


# -- the call sites that leaked ---------------------------------------------

def test_register_dashboard_defaults_do_not_leak_into_tmp():
    """The original leak, as a regression test.

    `register_dashboard` without stores used to leave six directories directly
    in /tmp per call. Building the same fallbacks repeatedly must now leave the
    top level flat.
    """
    before = _top_level_count()
    for _ in range(10):
        for purpose, filename in (("connections", "connections.db"), ("queue", "queue.db"),
                                  ("integration", "integration.db"), ("pm", "pm.db"),
                                  ("planner", "planner.db"), ("fleet", "fleet.db"),
                                  ("nodes", "nodes.db")):
            es.ephemeral_db_path(purpose, filename)
    after = _top_level_count()
    assert after - before <= 1, (
        f"70 fallback stores added {after - before} top-level entries; "
        f"before this fix the same calls added 70")


def test_no_module_still_calls_mkdtemp_directly():
    """The fix is only worth as much as its exhaustiveness: a seventh call site
    added later would reopen the leak silently."""
    import terminal_mcp
    package = Path(terminal_mcp.__file__).parent
    offenders = []
    for path in package.glob("*.py"):
        if path.name == "ephemeral_state.py":
            continue
        if "mkdtemp(" in path.read_text():
            offenders.append(path.name)
    assert not offenders, (
        f"{offenders} call tempfile.mkdtemp directly; use "
        f"ephemeral_state.ephemeral_state_dir so the directory is cleaned up")
