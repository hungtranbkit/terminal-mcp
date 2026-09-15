"""The tmux_session_factory guard, tested on real tmux sessions.

Every case here cost a real debugging session on this host: an interrupted
run poisoning a literal name until a human ran kill-session by hand, and two
worktree lanes running the suite at the same time silently killing each
other's sessions.
"""
from __future__ import annotations

import os

import pytest

from tests.conftest import (  # noqa: E402 -- see _name_ledger
    OWNER_OPTION,
    _known_created_names,
    _owner_alive,
    _remember_created,
    _session_owner,
    tmux,
)


@pytest.fixture
def raw_session():
    """A session made WITHOUT the factory, so the test controls its tag."""
    made: list[str] = []

    def make(name: str, owner: str | None = None) -> str:
        tmux("kill-session", "-t", name, check=False)
        tmux("new-session", "-d", "-s", name, "bash")
        if owner is not None:
            tmux("set-option", "-t", name, OWNER_OPTION, owner, check=False)
        made.append(name)
        return name

    yield make
    for name in made:
        tmux("kill-session", "-t", name, check=False)


def _alive(name: str) -> bool:
    return tmux("has-session", "-t", name, check=False).returncode == 0


def test_untagged_session_is_still_refused(tmux_session_factory, raw_session):
    """The original protection, unchanged: a session this suite never made
    and never named is a real session until proven otherwise."""
    name = "tmux-own-untagged-unknown"
    raw_session(name)
    with pytest.raises(RuntimeError, match="refuses to touch"):
        tmux_session_factory(name)
    assert _alive(name), "a session we refused must be left running"


def test_leftover_from_a_dead_run_is_reaped(tmux_session_factory, raw_session):
    """A tag naming a pid that is gone is a leftover, not someone's work."""
    name = "tmux-own-dead-owner"
    raw_session(name, owner="999999:0")
    assert tmux_session_factory(name) == name
    assert _session_owner(name) not in (None, "999999:0"), (
        "the recreated session must carry THIS run's tag")


def test_session_of_a_live_parallel_run_is_never_killed(tmux_session_factory, raw_session):
    """The bug this catches: a second suite read 'test-owned' and killed a
    session the first suite was using, which surfaced as a storm of
    unrelated failures in the OTHER run."""
    name = "tmux-own-live-owner"
    from tests.conftest import _process_start_time
    live = f"{os.getpid()}:{_process_start_time(os.getpid())}"
    raw_session(name, owner=live)
    # Not this fixture's own _RUN_OWNER string, but a provably live pid --
    # stands in for the other suite's process.
    with pytest.raises(RuntimeError, match="still alive"):
        tmux_session_factory(name)
    assert _alive(name), "another run's live session must survive untouched"


def test_untagged_squatter_under_a_name_we_own_is_reaped(tmux_session_factory, raw_session):
    """The recovery-engine case: the code under test rebuilds a session by
    name, the replacement has no tag, and an interrupted run leaves it
    behind. Before the ledger this poisoned the name permanently."""
    name = "tmux-own-recreated-untagged"
    _remember_created(name)
    assert name in _known_created_names()
    raw_session(name)  # no tag at all, as production recreation leaves it
    assert tmux_session_factory(name) == name
    assert _session_owner(name) is not None


def test_owner_alive_rejects_a_recycled_pid():
    """pid alone is not identity: the start time is what stops a recycled
    pid from making a dead run look alive."""
    from tests.conftest import _process_start_time
    real = _process_start_time(os.getpid())
    assert _owner_alive(f"{os.getpid()}:{real}") is True
    assert _owner_alive(f"{os.getpid()}:{int(real) + 12345}") is False
    assert _owner_alive("not-a-pid:0") is False
    assert _owner_alive("999999:0") is False
