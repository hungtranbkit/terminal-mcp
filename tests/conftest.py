from __future__ import annotations

import os
import subprocess
import time

import pytest

from terminal_mcp.config import AppConfig, PermissionsConfig


@pytest.fixture
def read_config() -> AppConfig:
    return AppConfig(PermissionsConfig(True, False), ("test-*", "agent-*"), 50, 20)


@pytest.fixture(autouse=True, scope="session")
def _isolate_default_state_dir(tmp_path_factory):
    """Global safety net -- a real, live incident found TWICE in one
    session: grants.py's SessionGrantStore(), then session_registry.py's
    SessionRegistryStore() (reconciled on every terminal_list_sessions/
    dashboard_list_sessions call -- i.e. from a huge fraction of this
    whole suite, not just tests that mention it by name), each silently
    accumulated real test-session-name rows in this host's REAL
    ~/.local/state/terminal-mcp/*.db, because a bare TerminalService
    (config)/SessionGrantStore()/... with no explicit store override
    defaults to that real, production path. Every default_*_path()
    function in this project (grants.py/session_registry.py/audit.py/
    bindings.py/lease.py/killed_sessions.py/webauth.py/connection_
    store.py/bridge.py/tunnel_diagnostics.py) already checks a specific
    TERMINAL_MCP_*_DB env var FIRST, then XDG_STATE_HOME, then finally
    ~/.local/state -- redirecting XDG_STATE_HOME here retroactively
    isolates every existing test in this suite that never explicitly
    passed its own store (and every one written from now on that forgets
    to), without touching each one individually. A test that DOES pass an
    explicit store instance (e.g. SessionGrantStore(tmp_path /
    "grants.db")) or an explicit TERMINAL_MCP_*_DB env var is completely
    unaffected -- both outrank this in every default_*_path()'s own
    lookup order.

    session-scoped, not per-test: a module/class-scoped fixture
    constructing a TerminalService (several test files do exactly this,
    reusing one instance across many tests) is itself set up BEFORE any
    function-scoped fixture ever runs for that module's first test --
    pytest instantiates broader-scoped fixtures first regardless of
    request order -- so a PER-TEST (function-scoped) version of this
    fixture would already be too late for those, confirmed live (it
    still leaked ~10 rows in one full-suite run). One shared isolated
    directory for the whole test session closes that ordering gap
    entirely, at the cost of sharing state across unrelated test files
    within a single run -- an acceptable trade here: no test in this
    suite asserts the real ~/.local/state fallback path itself (verified:
    none reference XDG_STATE_HOME or any default_*_path function by
    name), and this project's own tests already assume a shared real tmux
    server/session-name namespace across files (protected_sessions /
    disposable-name conventions), the same discipline that keeps this
    additionally-shared directory collision-free in practice."""
    os.environ["XDG_STATE_HOME"] = str(tmp_path_factory.mktemp("terminal-mcp-test-state"))


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

