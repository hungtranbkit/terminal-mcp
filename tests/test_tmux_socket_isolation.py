"""The test suite must not be visible to this host's real, running
terminal-mcp service.

That service reconciles the DEFAULT tmux server on its own schedule and
writes what it finds into the PRODUCTION session_registry.db. For years
that meant every disposable session this suite created could be recorded
as a real session in production (backlog blg_80635ab65719: ~268 such
rows). Redirecting the test process's own state paths could never fix
it -- the writer is a different process.

`tmux -L <socket>` selects a SERVER, and separate servers share no
sessions at all, so pointing this suite at its own socket removes the
channel entirely. These tests hold that property in place.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from terminal_mcp.tmux import TMUX_SOCKET_ENV, TmuxClient, default_socket_name

from tests.conftest import tmux as conftest_tmux


def _default_socket_sessions() -> set[str]:
    """Session names on the REAL, default tmux server -- deliberately a
    bare `tmux` argv with no -L, i.e. exactly what this host's own
    service sees. Read-only; this never creates or kills anything."""
    result = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                            capture_output=True, text=True, timeout=10, check=False)
    if result.returncode != 0:
        return set()  # no server running at all -- trivially isolated
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def test_the_suite_runs_on_its_own_tmux_socket():
    socket_name = os.environ.get(TMUX_SOCKET_ENV, "")
    assert socket_name, "pytest_configure must set an isolated tmux socket for the run"
    assert socket_name != "", "an isolated socket name is required"
    assert default_socket_name() == socket_name


def test_a_session_this_suite_creates_is_invisible_on_the_default_server(tmux_session_factory):
    """The property the whole fix exists for, asserted end to end: the
    production reconciler looks at the default server, and what it finds
    there must not include anything this suite made."""
    name = "test-socket-isolation-probe"
    before = _default_socket_sessions()
    tmux_session_factory(name, "bash -lc 'sleep 30'")

    # Alive on ours...
    assert name in {s.name for s in TmuxClient().list_sessions()}
    # ...and absent from the one the real service reconciles.
    after = _default_socket_sessions()
    assert name not in after
    assert after == before, "the suite must not add or remove sessions on the default server"


def test_the_service_layer_uses_the_isolated_socket_too(tmux_session_factory):
    """Not just the raw client: a bare TerminalService() -- how most of
    this suite builds one -- must land on the same isolated server, or
    its reconcile passes would read the real one."""
    from terminal_mcp.config import AppConfig, PermissionsConfig
    from terminal_mcp.core import TerminalService

    name = "test-socket-service-probe"
    tmux_session_factory(name, "bash -lc 'sleep 30'")
    service = TerminalService(AppConfig(PermissionsConfig(True, False), ("test-*",), 50, 20))

    listed = {row["name"] for row in service.terminal_list_sessions()["sessions"]}
    assert name in listed
    # Real, attended sessions on the default server are not in this view.
    assert listed.isdisjoint(_default_socket_sessions() - {name})


@pytest.mark.parametrize("socket_name,expected_prefix", [
    ("some-socket", ["tmux", "-L", "some-socket"]),
    (None, ["tmux"]),
])
def test_argv_carries_the_socket_only_when_one_is_set(socket_name, expected_prefix):
    """Production sets no socket, and must therefore keep a byte-identical
    argv to before this feature existed."""
    client = TmuxClient(socket_name=socket_name)
    if socket_name is None:
        # An explicit None still falls back to the env var, which IS set
        # inside this suite -- so assert the no-socket shape directly.
        client.socket_name = None
    assert client._argv(["list-sessions"]) == [*expected_prefix, "list-sessions"]


def test_conftest_tmux_helper_targets_the_isolated_server():
    """conftest's raw helper is used for kill-session against real names;
    a bare argv there would reach this host's attended sessions."""
    result = conftest_tmux("list-sessions", "-F", "#{session_name}", check=False)
    ours = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    assert ours.isdisjoint(_default_socket_sessions())


def test_webterm_attaches_on_the_isolated_socket():
    """webterm builds its own PTY argv instead of going through
    TmuxClient, so it needs the same server selection applied separately
    -- otherwise it would attach to a same-named session on the real
    server, or fail to find the test's own."""
    import inspect

    from terminal_mcp import webterm

    source = inspect.getsource(webterm.WebTerminalProcess.__init__)
    assert "default_socket_name()" in source
    assert '"attach-session"' in source
