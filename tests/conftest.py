from __future__ import annotations

import subprocess
import time

import pytest

from terminal_mcp.config import AppConfig, PermissionsConfig


@pytest.fixture
def read_config() -> AppConfig:
    return AppConfig(PermissionsConfig(True, False), ("test-*", "agent-*"), 50, 20)


def tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], check=check, capture_output=True, text=True, timeout=10)


@pytest.fixture
def tmux_session_factory():
    """Creates a real tmux session for the duration of one test, then kills
    it on teardown -- but ONLY a session this fixture itself created.

    This used to unconditionally `kill-session` the requested name before
    creating it (to clear stale state from a previous crashed run). On a
    machine where the test suite runs in the same tmux server as real,
    attended sessions -- this project's own actual deployment model -- a
    test that (as at least one in test_session_lifecycle.py deliberately
    does, to exercise protected_sessions) passes a real session's name
    would silently kill-session that live session and its running process
    out from under whoever/whatever was using it, then leave a bare-shell
    impostor behind under the same name. That happened for real against
    this project's own controlling "terminal-mcp" session. Refusing to
    touch a name that already exists turns that into a loud, immediate
    test error instead of a silent production incident."""
    created: set[str] = set()

    def create(name: str, command: str = "bash") -> str:
        exists = tmux("has-session", "-t", name, check=False).returncode == 0
        if exists and name not in created:
            # A session by this name already exists and this fixture
            # instance did not make it -- refuse rather than kill it. Once
            # a name IS in `created`, calling create() again for the same
            # name is a deliberate, supported "kill and recreate" (a test
            # simulating a session recreated under the same name, e.g. PID
            # reuse) -- safe, because it can only ever be a session this
            # same fixture call already owns.
            raise RuntimeError(
                f"tmux_session_factory refuses to touch pre-existing tmux session {name!r} "
                "-- it may be a real, live session on this host. Use a unique disposable "
                "name (e.g. via tmp_path or uuid) instead of a literal real session name."
            )
        if exists:
            tmux("kill-session", "-t", name, check=False)
        tmux("new-session", "-d", "-s", name, command)
        created.add(name)
        time.sleep(0.15)
        return name

    yield create
    for name in created:
        tmux("kill-session", "-t", name, check=False)

