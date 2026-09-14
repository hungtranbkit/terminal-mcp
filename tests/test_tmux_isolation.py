"""Regression tests for disposable-tmux isolation (task blg_8a1389e7caeb).

These guard the FIX, not the product. The product behaviour that used to
surface as a failure -- `SESSION_ALREADY_EXISTS` when a session by that
name really exists -- is correct and is not relaxed anywhere here; what
changed is that tests no longer collide with leftovers.

The janitor's decisions are tested through injected session lists and an
injected liveness probe, so "would this kill a user's session?" is
answered without ever creating or killing a real one. The end-to-end
sweep test creates only sessions it owns.
"""
from __future__ import annotations

import os
import subprocess

import pytest

import tmux_isolation
from tmux_isolation import (
    OWNED_RE, RUN_ID, is_mine, is_owned, owned_name, owning_pid, sweep_orphans,
)

DEAD_PID = 2 ** 22 - 1          # far above /proc/sys/kernel/pid_max in practice
LIVE_PID = os.getpid()


def _other_run_name(slug, *, pid):
    return f"lifecycle-own{pid}x0a1b2c-{slug}"


# -- unique resource names ----------------------------------------------

def test_each_name_is_unique_to_this_run():
    assert owned_name("kill-1") == owned_name("kill-1"), "stable within a run"
    assert RUN_ID in owned_name("kill-1")
    assert owned_name("kill-1") != _other_run_name("kill-1", pid=LIVE_PID)


def test_names_still_match_the_suite_allowed_pattern():
    """The suites configure allowed_session_patterns=("lifecycle-*",) --
    a unique name that no longer matches would break every test."""
    assert owned_name("anything").startswith("lifecycle-")


def test_two_concurrent_runs_cannot_collide(monkeypatch):
    """Parallel runs of this suite must never pick the same name."""
    first = owned_name("smoke-kill-1")
    monkeypatch.setattr(tmux_isolation, "RUN_ID", "99999xffffff")
    second = tmux_isolation.owned_name("smoke-kill-1")
    assert first != second
    assert is_owned(first) and is_owned(second)


# -- ownership marker ----------------------------------------------------

@pytest.mark.parametrize("name", [
    "hp-work", "hp1", "test-http-secure", "terminal-mcp", "queue-smoke-a",
    "lifecycle-shell-only",          # a LEGACY fixed name -- no marker, not ours
    "lifecycle-smoke-kill-1",
    "lifecycle-own-nopid-x",
    "lifecycle-ownabcx123456-y",     # pid must be digits
    "", "lifecycle-",
])
def test_unowned_names_are_never_recognised_as_ours(name):
    assert is_owned(name) is False
    assert owning_pid(name) is None


def test_owned_names_are_recognised_and_carry_their_pid():
    name = _other_run_name("x", pid=4242)
    assert is_owned(name) is True
    assert owning_pid(name) == 4242
    assert is_mine(name) is False
    assert is_mine(owned_name("x")) is True


def test_kill_session_refuses_anything_it_does_not_own():
    """The one function that can destroy real work refuses by default."""
    for name in ("hp-work", "test-http-secure", "lifecycle-shell-only", "terminal-mcp"):
        with pytest.raises(AssertionError, match="not an owned test session"):
            tmux_isolation.kill_session(name)


# -- the janitor's decisions --------------------------------------------

def test_sweep_never_touches_pre_existing_unrelated_sessions():
    """The core safety property: a user's own sessions, a lane's
    sessions, and another suite's leftovers all survive."""
    bystanders = ["hp-work", "hp1", "hp2", "test-http-secure", "terminal-mcp",
                  "lifecycle-shell-only", "queue-smoke-a"]
    killed = []
    swept = sweep_orphans(sessions=list(bystanders), pid_is_alive=lambda pid: False,
                          killer=killed.append)
    assert swept == [] and killed == []


def test_sweep_removes_an_orphan_from_a_dead_run():
    orphan = _other_run_name("smoke-kill-1", pid=DEAD_PID)
    killed = []
    swept = sweep_orphans(sessions=["hp-work", orphan], pid_is_alive=lambda pid: False,
                          killer=killed.append)
    assert swept == [orphan] and killed == [orphan]


def test_sweep_skips_a_session_whose_creating_run_is_still_alive():
    """Another lane running this suite right now must not be disturbed."""
    live = _other_run_name("smoke-kill-1", pid=LIVE_PID)
    killed = []
    swept = sweep_orphans(sessions=[live], pid_is_alive=lambda pid: True, killer=killed.append)
    assert swept == [] and killed == []


def test_sweep_never_kills_this_runs_own_sessions():
    mine = owned_name("in-flight")
    killed = []
    sweep_orphans(sessions=[mine], pid_is_alive=lambda pid: False, killer=killed.append)
    assert killed == [], "a live test's own session must survive the janitor"


def test_sweep_is_idempotent():
    orphan = _other_run_name("x", pid=DEAD_PID)
    remaining = ["hp-work", orphan]

    def killer(name):
        remaining.remove(name)

    first = sweep_orphans(sessions=list(remaining), pid_is_alive=lambda pid: False, killer=killer)
    second = sweep_orphans(sessions=list(remaining), pid_is_alive=lambda pid: False, killer=killer)
    assert first == [orphan]
    assert second == [], "a second sweep finds nothing left to do"
    assert remaining == ["hp-work"]


def test_pid_alive_reports_this_process_and_not_an_absurd_one():
    assert tmux_isolation.pid_alive(os.getpid()) is True
    assert tmux_isolation.pid_alive(DEAD_PID) is False


# -- real tmux, owned sessions only --------------------------------------

@pytest.fixture
def real_owned_session():
    created: list[str] = []
    yield created.append
    for name in created:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False, capture_output=True)


def test_sweep_against_real_tmux_removes_only_the_dead_runs_orphan(real_owned_session):
    """End to end with the real tmux server: one orphan from a dead run
    and one session from a live run. Only the orphan goes."""
    orphan = _other_run_name("real-orphan", pid=DEAD_PID)
    live = _other_run_name("real-live", pid=LIVE_PID)
    for name in (orphan, live):
        real_owned_session(name)
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 60"], check=True)

    before = tmux_isolation.list_tmux_sessions()
    assert orphan in before and live in before
    unrelated_before = [n for n in before if not is_owned(n)]

    swept = sweep_orphans()

    after = tmux_isolation.list_tmux_sessions()
    assert orphan in swept and orphan not in after
    assert live in after, "a live run's session must survive"
    assert [n for n in after if not is_owned(n)] == unrelated_before, \
        "no unrelated session was touched"


def test_a_stale_legacy_artifact_does_not_affect_a_run(real_owned_session):
    """The original failure mode, inverted: a leftover with an OLD fixed
    name may sit on the host forever and must not make anything fail --
    no test claims that name any more, and the janitor will not touch it
    either (it carries no ownership marker)."""
    legacy = "lifecycle-shell-only"
    if legacy in tmux_isolation.list_tmux_sessions():
        pytest.skip("a real leftover by that name already exists on this host")
    real_owned_session(legacy)
    subprocess.run(["tmux", "new-session", "-d", "-s", legacy, "sleep 60"], check=True)

    assert is_owned(legacy) is False
    assert sweep_orphans() == [] or legacy not in sweep_orphans()
    assert legacy in tmux_isolation.list_tmux_sessions(), "left alone, not killed"

    # And the suite that used to own that name no longer generates it.
    source = (os.path.dirname(__file__), "test_kill_reopen.py")
    text = open(os.path.join(*source), encoding="utf-8").read()
    assert '"lifecycle-shell-only"' not in text
    assert 'name = "lifecycle-smoke-kill-1"' not in text


def test_owned_regex_shape_is_what_the_janitor_documents():
    assert OWNED_RE.match(f"lifecycle-own{os.getpid()}xabc123-slug")
    assert not OWNED_RE.match("lifecycle-ownXxabc123-slug")
    assert not OWNED_RE.match("other-own123xabc123-slug")
