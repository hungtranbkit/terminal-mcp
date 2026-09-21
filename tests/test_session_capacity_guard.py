"""A node must refuse session N+1, not degrade into refusing everything.

The live failure (dell-linux, 2026-09-21): nothing capped how many tmux
sessions a node accepted. The count grew unattended until the node
agent's own cgroup was exhausted, and from then on EVERY request --
including plain status reads of sessions that were perfectly healthy --
answered HTTP 500 `RuntimeError: can't start new thread`. The controller
surfaced that as flaky connectivity, so the search went to the network
and the real cause (pids.events showing 145780 ceiling hits) stayed
hidden.

One named refusal is strictly better than breaking everything that
already works, so terminal_create_session now does admission control
before it creates anything.

These tests stub the tmux inventory rather than creating real sessions:
capacity is arithmetic, and the box running the suite already has its own
sessions, which would make a count-based assertion environment-dependent.
"""
from __future__ import annotations

import dataclasses

import pytest

from terminal_mcp.config import (
    AppConfig,
    InputPolicyConfig,
    PermissionsConfig,
    SessionLifecycleConfig,
    _load_session_lifecycle_config,
)
from terminal_mcp.core import TerminalService


def _config(tmp_path, **lifecycle) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True),
        allowed_session_patterns=("cap-*",),
        max_capture_lines=200,
        default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("cap-*",)),
        session_lifecycle=SessionLifecycleConfig(
            enabled=True, allowed_cwd_roots=(str(tmp_path),), **lifecycle),
    )


class _FakeSession:
    def __init__(self, name: str) -> None:
        self.name = name


def _with_sessions(service: TerminalService, count: int) -> None:
    sessions = [_FakeSession(f"cap-existing-{i}") for i in range(count)]
    service.tmux.list_sessions = lambda: list(sessions)  # type: ignore[method-assign]


# -- the guard itself ----------------------------------------------------

def test_refuses_once_the_limit_is_reached(tmp_path):
    service = TerminalService(_config(tmp_path, max_sessions=3))
    _with_sessions(service, 3)
    result = service.terminal_create_session("cap-new", "shell")
    assert result["error"] == "NODE_AT_SESSION_CAPACITY"
    assert result["sessions"] == 3
    assert result["limit"] == 3
    # The caller must learn what to do, not just that it failed.
    assert "raise session_lifecycle.max_sessions" in result["detail"]


def test_refusal_creates_nothing(tmp_path):
    """The point of admission control: nothing is half-created."""
    service = TerminalService(_config(tmp_path, max_sessions=1))
    _with_sessions(service, 1)
    before = len(service.tmux.list_sessions())
    service.terminal_create_session("cap-never-born", "shell")
    assert len(service.tmux.list_sessions()) == before
    assert service.tmux.get_session("cap-never-born") is None


def test_below_the_limit_is_not_blocked_by_the_guard(tmp_path):
    """Under the limit the guard is transparent -- whatever comes back is
    decided by the normal create path, never by capacity."""
    service = TerminalService(_config(tmp_path, max_sessions=50))
    _with_sessions(service, 2)
    result = service.terminal_create_session("cap-allowed", "shell")
    assert result.get("error") != "NODE_AT_SESSION_CAPACITY"


# -- how the limit is chosen ---------------------------------------------

def test_explicit_limit_wins_over_derivation(tmp_path):
    service = TerminalService(_config(tmp_path, max_sessions=7, max_session_ram_mb=96))
    _with_sessions(service, 0)
    assert service._session_capacity() == (7, 0)


def test_zero_derives_a_positive_limit_from_machine_ram(tmp_path):
    """0 means "fit this machine", never "no ceiling" -- an unbounded node
    is the exact failure this guard exists to prevent."""
    service = TerminalService(_config(tmp_path, max_sessions=0, max_session_ram_mb=96))
    _with_sessions(service, 0)
    limit, current = service._session_capacity()
    assert current == 0
    assert limit >= 1
    with open("/proc/meminfo", encoding="utf-8") as handle:
        total_mb = next(int(l.split()[1]) for l in handle if l.startswith("MemTotal:")) // 1024
    assert limit == total_mb // 96


def test_a_smaller_ram_slice_allows_more_sessions(tmp_path):
    """The derivation actually tracks the knob, rather than being a
    constant dressed up as a calculation."""
    roomy = TerminalService(_config(tmp_path, max_session_ram_mb=32))
    tight = TerminalService(_config(tmp_path, max_session_ram_mb=256))
    _with_sessions(roomy, 0)
    _with_sessions(tight, 0)
    assert roomy._session_capacity()[0] > tight._session_capacity()[0]


def test_unreadable_tmux_does_not_refuse_real_work(tmp_path):
    """Cannot count -> cannot honestly enforce. Allowing is correct here;
    refusing on a number we do not have would invent an outage."""
    from terminal_mcp.tmux import TmuxError

    service = TerminalService(_config(tmp_path, max_sessions=1))

    def _boom():
        raise TmuxError("tmux server not running")

    service.tmux.list_sessions = _boom  # type: ignore[method-assign]
    assert service._session_capacity() == (0, 0)


# -- config validation ---------------------------------------------------

@pytest.mark.parametrize("raw", [
    {"max_sessions": -1},
    {"max_sessions": True},      # bool is an int subclass; must not slip through
    {"max_sessions": "many"},
    {"max_session_ram_mb": 8},   # below the floor
    {"max_session_ram_mb": True},
])
def test_invalid_capacity_config_is_rejected(raw):
    with pytest.raises(ValueError):
        _load_session_lifecycle_config({"enabled": True, **raw})


def test_defaults_are_bounded_not_unlimited():
    default = SessionLifecycleConfig()
    assert default.max_sessions == 0          # 0 == derive, checked above
    assert default.max_session_ram_mb >= 16
    loaded = _load_session_lifecycle_config({"enabled": True})
    assert dataclasses.replace(loaded, enabled=False).max_sessions == default.max_sessions


# -- reclaim instead of refuse -------------------------------------------

class _Info:
    """Minimal SessionInfo stand-in for reap selection."""
    def __init__(self, name, *, cmd="bash", idle_h=99.0, attached=False, dead=False):
        import time
        self.name = name
        self.attached = attached
        self.pane_dead = dead
        self.pane_current_command = cmd
        self.activity_epoch = time.time() - idle_h * 3600


def _inventory(service, items):
    service.tmux.list_sessions = lambda: list(items)  # type: ignore[method-assign]


def test_reaper_is_off_by_default(tmp_path):
    """Killing sessions is destructive and not trivially undoable, so a
    node opts in per host rather than inheriting it."""
    assert SessionLifecycleConfig().reap_idle_sessions is False
    service = TerminalService(_config(tmp_path, max_sessions=1))
    _inventory(service, [_Info("cap-idle")])
    assert service._reclaim_idle_sessions(1) == {"reclaimed": [], "enabled": False}


def test_running_agent_is_never_reaped_however_quiet(tmp_path):
    """A thinking agent looks idle to tmux -- 99 of 110 sessions on
    dell-linux looked abandoned by timestamp while all were ACTIVE."""
    service = TerminalService(_config(tmp_path, reap_idle_sessions=True, reap_idle_hours=1))
    _inventory(service, [_Info("cap-claude", cmd="claude", idle_h=500),
                         _Info("cap-codex", cmd="codex", idle_h=500)])
    assert service._idle_reap_candidates() == []


def test_attached_and_protected_are_never_reaped(tmp_path):
    service = TerminalService(_config(tmp_path, reap_idle_sessions=True, reap_idle_hours=1,
                                      protected_sessions=("terminal-mcp", "cap-keep")))
    _inventory(service, [_Info("cap-keep"), _Info("terminal-mcp"),
                         _Info("cap-attached", attached=True)])
    assert service._idle_reap_candidates() == []


def test_recently_active_shell_is_not_reaped(tmp_path):
    service = TerminalService(_config(tmp_path, reap_idle_sessions=True, reap_idle_hours=6))
    _inventory(service, [_Info("cap-fresh", idle_h=0.5)])
    assert service._idle_reap_candidates() == []


def test_dead_pane_is_reapable_at_any_age(tmp_path):
    """Nothing runs in it and nothing can, so age is irrelevant."""
    service = TerminalService(_config(tmp_path, reap_idle_sessions=True, reap_idle_hours=99))
    _inventory(service, [_Info("cap-dead", cmd="claude", idle_h=0.0, dead=True)])
    assert [c.name for c in service._idle_reap_candidates()] == ["cap-dead"]


def test_candidates_are_ordered_oldest_idle_first(tmp_path):
    service = TerminalService(_config(tmp_path, reap_idle_sessions=True, reap_idle_hours=1))
    _inventory(service, [_Info("cap-b", idle_h=10), _Info("cap-c", idle_h=50),
                         _Info("cap-a", idle_h=30)])
    assert [c.name for c in service._idle_reap_candidates()] == ["cap-c", "cap-a", "cap-b"]


def test_reclaim_frees_only_the_shortfall(tmp_path):
    """Never a blanket sweep -- one slot needed, one session killed."""
    service = TerminalService(_config(tmp_path, max_sessions=3, reap_idle_sessions=True,
                                      reap_idle_hours=1))
    _inventory(service, [_Info("cap-1", idle_h=9), _Info("cap-2", idle_h=8),
                         _Info("cap-3", idle_h=7)])
    killed: list[str] = []
    service.terminal_delete_session = lambda n: (killed.append(n), {"action": "deleted"})[1]  # type: ignore[method-assign]
    assert service._reclaim_idle_sessions(1)["reclaimed"] == ["cap-1"]
    assert killed == ["cap-1"]


def test_capacity_refusal_reports_what_it_reclaimed(tmp_path):
    """Full of sessions nothing may reap -> still refuses, and says so."""
    service = TerminalService(_config(tmp_path, max_sessions=2, reap_idle_sessions=True,
                                      reap_idle_hours=1))
    _inventory(service, [_Info("cap-x", cmd="claude"), _Info("cap-y", cmd="codex")])
    result = service.terminal_create_session("cap-new", "shell")
    assert result["error"] == "NODE_AT_SESSION_CAPACITY"
    assert result["reclaimed"] == []
    assert "0 idle session(s) reclaimed" in result["detail"]
