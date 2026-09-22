"""Regression tests for registry_cleanup: what it selects, and -- far
more important -- what it must never select.

The whole value of this module is that a human can trust its selection
without re-deriving it, so most of these tests are about REAL sessions
staying untouched. Each "never selected" case below corresponds to a row
shape that actually exists in this host's production registry.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from terminal_mcp.registry_cleanup import (
    DEFAULT_MIN_AGE_DAYS,
    apply_plan,
    build_plan,
    mine_test_session_names,
)
from terminal_mcp.session_registry import SessionRegistryStore

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


@pytest.fixture
def registry(tmp_path):
    """A real registry database (real schema, via the real store), that
    tests then insert precise row shapes into."""
    path = tmp_path / "session_registry.db"
    SessionRegistryStore(path)  # creates + migrates the schema
    return path


def _insert(path, name, *, status="MISSING", node_id="local", last_seen_days=30,
            conversation_id=None, auto_recovery=None, notes=None, cwd=None):
    connection = sqlite3.connect(path)
    with connection:
        connection.execute(
            """INSERT INTO session_records
               (node_id, session_name, status, created_at, last_seen_at,
                conversation_id, auto_recovery_enabled, notes, cwd, stable_session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (node_id, name, status, _iso(last_seen_days + 1), _iso(last_seen_days),
             conversation_id, auto_recovery, notes, cwd, f"stable-{name}"),
        )
    connection.close()


def _plan(path, inventory_names, **kwargs):
    inventory = {name: ("tests/test_example.py",) for name in inventory_names}
    kwargs.setdefault("min_age_days", DEFAULT_MIN_AGE_DAYS)
    kwargs.setdefault("now", NOW)
    return build_plan(path, inventory, **kwargs)


def _selected(plan):
    return {c.session_name for c in plan.selected}


def _held(plan, name):
    return next(why for c, why in plan.needs_review if c.session_name == name)


# ------------------------------------------------- the one positive case
def test_a_genuine_test_row_is_selected(registry):
    _insert(registry, "test-pin-watch")
    plan = _plan(registry, ["test-pin-watch"])
    assert _selected(plan) == {"test-pin-watch"}
    assert plan.selected[0].stable_session_id == "stable-test-pin-watch"
    assert plan.selected[0].sources == ("tests/test_example.py",)
    assert plan.selected[0].age_days == pytest.approx(30, abs=0.1)


# ---------------------------------------- real sessions are never selected
def test_real_session_names_are_never_selected(registry):
    """The rows this host actually has. None is a test literal, so none
    is even a candidate -- the first and cheapest line of defence."""
    for name in ("mesflow1", "facebook", "mesflow-work", "codex-main", "dell1",
                 "ai-design-council", "qr-restaurant"):
        _insert(registry, name)
    plan = _plan(registry, ["test-pin-watch"])  # inventory contains none of them
    assert plan.selected == []
    assert plan.needs_review == []
    assert plan.total_rows == 7


@pytest.mark.parametrize("name,kwargs,expect", [
    # Each of these IS in the test-name inventory -- a name collision
    # between a real session and a test literal, which really happens
    # (`terminal-mcp`, `mesflow`, `mcp` all do). Only the guards separate
    # them, so each guard gets its own case.
    ("mesflow", dict(conversation_id="conv-123"), "conversation_id"),
    ("agent-recover", dict(auto_recovery=1), "auto_recovery_enabled"),
    ("quan_ly_ban_hang", dict(notes="backfilled 2026-09-05: found via audit.db"), "curated notes"),
    ("test-live-now", dict(status="ACTIVE"), "status=ACTIVE"),
    ("test-offline", dict(status="OFFLINE"), "status=OFFLINE"),
    ("test-already-purged", dict(status="DELETED"), "status=DELETED"),
    ("test-recent", dict(last_seen_days=1), "min 7"),
])
def test_guarded_rows_are_held_back_not_selected(registry, name, kwargs, expect):
    _insert(registry, name, **kwargs)
    plan = _plan(registry, [name])
    assert _selected(plan) == set(), f"{name} must never be selected"
    assert expect in _held(plan, name)


def test_protected_session_is_never_selected_even_when_a_test_literal(registry):
    """`terminal-mcp` is simultaneously this project's own control
    session AND a literal all over its tests."""
    _insert(registry, "terminal-mcp")
    plan = _plan(registry, ["terminal-mcp"], protected={"terminal-mcp"})
    assert _selected(plan) == set()
    assert "protected_sessions" in _held(plan, "terminal-mcp")


def test_a_session_alive_right_now_is_never_selected(registry):
    _insert(registry, "test-pin-watch")
    plan = _plan(registry, ["test-pin-watch"], live=frozenset({"test-pin-watch"}))
    assert _selected(plan) == set()
    assert "alive right now" in _held(plan, "test-pin-watch")


def test_every_guard_is_exercised_by_this_file():
    """If someone adds a guard, this file must grow a case for it --
    otherwise a new guard could silently never be tested."""
    from terminal_mcp.registry_cleanup import GUARDS
    assert {name for name, _ in GUARDS} == {
        "status", "live", "protected", "conversation", "auto_recovery", "backfilled", "age",
    }


# ------------------------------------------------------------- the miner
def test_miner_only_takes_literals_used_as_session_names(tmp_path):
    """The `quan_ly_ban_hang` regression: a real agent session whose name
    also appears in the test suite -- but as a search string and a
    directory name, never as a session name. Mining every string literal
    selected a real row for purge."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_sample.py").write_text(
        '''
"""A docstring that mentions mesflow-work, a real session."""
def test_things(tmux_session_factory, store, tmp_path):
    # a real session name in a comment: codex-main
    tmux_session_factory("test-real-name", "bash")
    store.search("quan_ly_ban_hang")
    proj = tmp_path / "quan_ly_ban_hang"
    client.new_session("test-via-method", cwd=str(proj))
    tmux("kill-session", "-t", "test-via-flag")
'''
    )
    mined = mine_test_session_names(tests_dir)
    assert set(mined) == {"test-real-name", "test-via-method", "test-via-flag"}
    assert "quan_ly_ban_hang" not in mined   # search string + dir name only
    assert "mesflow-work" not in mined       # docstring only
    assert "codex-main" not in mined         # comment only


def test_miner_does_not_reduce_fstring_names_to_a_prefix(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_dyn.py").write_text(
        'def test_x(tmux_session_factory):\n'
        '    tmux_session_factory(f"lane-{uuid4().hex}", "bash")\n'
    )
    assert mine_test_session_names(tests_dir) == {}


# ------------------------------------------------------- dry run vs apply
def test_build_plan_cannot_write_to_the_database(registry):
    """A dry run against production must be structurally incapable of
    mutating it, not merely careful: the connection is opened mode=ro."""
    _insert(registry, "test-pin-watch")
    before = registry.read_bytes()
    _plan(registry, ["test-pin-watch"])
    assert registry.read_bytes() == before


def test_apply_purges_to_a_tombstone_and_keeps_the_row(registry):
    _insert(registry, "test-pin-watch")
    _insert(registry, "mesflow1")
    plan = _plan(registry, ["test-pin-watch"])
    assert apply_plan(registry, plan, purged_by="pytest") == 1

    store = SessionRegistryStore(registry)
    purged = store.get("local", "test-pin-watch")
    assert purged is not None, "purge must tombstone, never hard-DELETE the row"
    assert purged.status == "DELETED"
    assert "pytest" in (purged.notes or "")
    assert store.get("local", "mesflow1").status == "MISSING"  # untouched


def _cli(*args, expect_rc=None):
    result = subprocess.run(
        [sys.executable, "-m", "terminal_mcp.registry_cleanup", *args],
        capture_output=True, text=True, timeout=60,
    )
    if expect_rc is not None:
        assert result.returncode == expect_rc, result.stderr
    return result


def test_cli_dry_run_is_the_default_and_writes_nothing(registry, tmp_path):
    _insert(registry, "test-pin-watch")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_s.py").write_text(
        'def t(tmux_session_factory):\n    tmux_session_factory("test-pin-watch", "bash")\n')
    before = registry.read_bytes()

    result = _cli("--db", str(registry), "--tests-dir", str(tests_dir), "--json", expect_rc=0)
    plan = json.loads(result.stdout)  # stdout must be pure JSON

    assert plan["selected_count"] == 1
    assert "DRY RUN" in result.stderr
    assert "--apply --confirm-count 1" in result.stderr
    assert registry.read_bytes() == before


def test_cli_apply_requires_the_confirm_count_and_refuses_a_stale_one(registry, tmp_path):
    _insert(registry, "test-pin-watch")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_s.py").write_text(
        'def t(tmux_session_factory):\n    tmux_session_factory("test-pin-watch", "bash")\n')
    base = ["--db", str(registry), "--tests-dir", str(tests_dir)]
    before = registry.read_bytes()

    bare = _cli(*base, "--apply", expect_rc=3)
    assert "requires --confirm-count" in bare.stderr
    assert registry.read_bytes() == before

    stale = _cli(*base, "--apply", "--confirm-count", "9", expect_rc=3)
    assert "now selects 1" in stale.stderr
    assert registry.read_bytes() == before

    ok = _cli(*base, "--apply", "--confirm-count", "1", expect_rc=0)
    assert "purged 1 row" in ok.stdout
    assert SessionRegistryStore(registry).get("local", "test-pin-watch").status == "DELETED"
