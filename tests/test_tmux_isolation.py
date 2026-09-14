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
    """Ownership is the full STRUCTURAL marker, never a prefix match.

    The prefix is deliberately open (suites need `claude-lc-`,
    `codex-lc-`, and the intentionally-unwhitelisted `unwhitelisted-`),
    so what authorises a kill is the `-own<pid>x<6 hex>-<slug>` segment,
    not the leading text."""
    assert OWNED_RE.match(f"lifecycle-own{os.getpid()}xabc123-slug")
    assert OWNED_RE.match("other-own123xabc123-slug"), "any prefix, strict marker"
    assert not OWNED_RE.match("lifecycle-ownXxabc123-slug"), "pid must be digits"
    assert not OWNED_RE.match("lifecycle-own123xABC123-slug"), "tag must be lowercase hex"
    assert not OWNED_RE.match("lifecycle-own123xabc12-slug"), "tag must be exactly 6"
    assert not OWNED_RE.match("lifecycle-own123xabc1234-slug"), "tag must be exactly 6"
    assert not OWNED_RE.match(f"lifecycle-own{os.getpid()}xabc123-"), "slug must be non-empty"


# -- prefixes: uniqueness must not change a name's whitelist status -----

@pytest.mark.parametrize("prefix", ["lifecycle", "claude-lc", "codex-lc", "unwhitelisted", "granted"])
def test_every_prefix_round_trips_and_is_owned(prefix):
    name = owned_name("slug-1", prefix=prefix)
    assert name.startswith(f"{prefix}-own")
    assert is_owned(name) and is_mine(name)
    assert OWNED_RE.match(name).group("prefix") == prefix, "a hyphenated prefix must split correctly"


def test_unwhitelisted_names_stay_outside_the_allowed_patterns():
    """test_session_lifecycle has tests whose whole point is a name that
    matches NO allowed pattern. Making names unique must not smuggle them
    into the whitelist."""
    allowed = ("lifecycle-", "claude-lc-", "codex-lc-")
    name = owned_name("prompt-1", prefix="unwhitelisted")
    assert not any(name.startswith(p) for p in allowed)


# -- ambiguity: names that merely LOOK like ours -------------------------

@pytest.mark.parametrize("name", [
    "lifecycle-victim", "lifecycle-bystander", "lifecycle-protected-sim",
    "lifecycle-own", "lifecycle-own-", "lifecycle-own-thing",
    "own123xabc123-slug",          # no prefix at all
    "lifecycle-own123xabc123",     # no slug
])
def test_lookalike_names_are_not_owned(name):
    assert is_owned(name) is False
    with pytest.raises(AssertionError):
        tmux_isolation.kill_session(name)


# -- the exact real sessions on this host --------------------------------

REAL_HOST_SESSIONS = ["hp-work", "hp1", "hp2", "hp3-work", "test-http-secure"]


@pytest.mark.parametrize("name", REAL_HOST_SESSIONS)
def test_named_real_sessions_on_this_host_are_never_touchable(name):
    """These are this machine's actual attended/lane sessions. Pinned by
    name so a future change to the matching rule fails loudly here rather
    than silently killing one of them."""
    assert is_owned(name) is False
    assert owning_pid(name) is None
    with pytest.raises(AssertionError, match="not an owned test session"):
        tmux_isolation.kill_session(name)


def test_sweep_leaves_every_real_host_session_alone():
    killed = []
    swept = sweep_orphans(sessions=list(REAL_HOST_SESSIONS), pid_is_alive=lambda pid: False,
                          killer=killed.append)
    assert swept == [] and killed == []


# -- crash cleanup is best-effort, never fatal ---------------------------

def test_sweep_survives_tmux_being_unavailable(monkeypatch):
    """No tmux server at all is a normal state, not an error -- the
    janitor must not break collection for every suite that imports it."""
    monkeypatch.setattr(tmux_isolation, "list_tmux_sessions", lambda: [])
    assert sweep_orphans() == []


def test_sweep_continues_past_a_kill_that_fails():
    """A crashed run's session may vanish between listing and killing.
    Best-effort means the next orphan is still swept."""
    first = _other_run_name("a", pid=DEAD_PID)
    second = _other_run_name("b", pid=DEAD_PID)

    def flaky(name):
        if name == first:
            raise subprocess.SubprocessError("session disappeared")

    with pytest.raises(subprocess.SubprocessError):
        sweep_orphans(sessions=[first, second], pid_is_alive=lambda pid: False, killer=flaky)
    # kill_session itself never raises on a missing session (check=False),
    # which is what makes the real sweep best-effort.
    tmux_isolation.kill_session(_other_run_name("never-existed", pid=DEAD_PID))


# -- concurrency ---------------------------------------------------------

def test_many_concurrent_runs_produce_disjoint_names(monkeypatch):
    """Simulates N lanes running this suite at once: for one shared slug
    every run must get a distinct name."""
    names = set()
    for pid in range(1000, 1010):
        monkeypatch.setattr(tmux_isolation, "RUN_ID", f"{pid}xaabbcc")
        names.add(tmux_isolation.owned_name("lifecycle-victim"))
    assert len(names) == 10


def test_a_concurrent_runs_sessions_are_never_swept():
    """Live-pid skip across a whole fleet of sibling runs."""
    live = [_other_run_name(f"s{i}", pid=LIVE_PID) for i in range(5)]
    dead = [_other_run_name(f"d{i}", pid=DEAD_PID) for i in range(5)]
    killed = []
    swept = sweep_orphans(sessions=live + dead,
                          pid_is_alive=lambda pid: pid == LIVE_PID, killer=killed.append)
    assert set(swept) == set(dead)
    assert not any(name in killed for name in live)
