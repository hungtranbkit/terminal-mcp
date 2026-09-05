from __future__ import annotations

import os
import subprocess
import time

import pytest

from terminal_mcp.config import AppConfig, PermissionsConfig


@pytest.fixture
def read_config() -> AppConfig:
    return AppConfig(PermissionsConfig(True, False), ("test-*", "agent-*"), 50, 20)


def pytest_configure(config: pytest.Config) -> None:
    """Global safety net -- a real, live incident found repeatedly in one
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

    A `pytest_configure` hook, deliberately NOT a fixture -- tried a
    function-scoped autouse fixture first (still leaked ~10 rows: a
    module/class-scoped fixture building a TerminalService is set up
    BEFORE any function-scoped fixture runs for that module's first
    test), then a session-scoped autouse fixture (STILL leaked a
    handful of rows on a full-suite run -- pytest only guarantees
    higher-scoped fixtures are set up before LOWER-scoped ones that
    actually DEPEND on them through the request graph; a module that
    builds its TerminalService as a bare local inside a test function
    with no fixture at all is unaffected by fixture setup order
    entirely, since there is no fixture in that path to order against).
    `pytest_configure` is a pytest hook, not a fixture -- it runs once,
    before collection even begins, before ANY test module is imported
    or ANY test function executes, so there is no ordering question left
    to get wrong. Uses a plain tempfile dir (not tmp_path_factory, which
    is itself only available inside a fixture) since this runs outside
    the fixture system entirely.

    Shares one directory for the whole run (not per-test): no test in
    this suite asserts the real ~/.local/state fallback path itself
    (verified: none reference XDG_STATE_HOME or any default_*_path
    function by name), and this project's own tests already assume a
    shared real tmux server/session-name namespace across files
    (protected_sessions / disposable-name conventions), the same
    discipline that keeps this additionally-shared directory collision-
    free in practice.

    A DIFFERENT, residual source of the same symptom this cannot fix
    (confirmed live, worth remembering): this project's own real,
    separately-running `terminal-mcp-http.service` shares the SAME real
    tmux server this test suite creates disposable sessions on -- while
    that service is up (the normal state on this dev host) and something
    is polling its dashboard/MCP session listing (a real open dashboard
    tab, a real MCP client), ITS OWN reconcile pass sees whatever test
    session happens to be alive on the shared tmux server at that
    instant and writes a real row into the PRODUCTION session_registry.
    db for it -- a completely separate process, with its own unmodified
    XDG_STATE_HOME, that this hook has no way to reach or isolate. Not a
    bug in this isolation mechanism (which correctly covers everything
    the TEST process itself writes) -- an inherent consequence of this
    project's own testing philosophy (real tmux, real shared server)
    combined with a real, live sibling service. Harmless (rows are
    obviously test-named, and correctly age into MISSING once the test's
    tmux session ends) -- clean up periodically with the same read-only-
    diff-then-DELETE approach used to discover this, never treat it as
    a regression to chase further."""
    import tempfile
    os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(prefix="terminal-mcp-test-state-")


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

