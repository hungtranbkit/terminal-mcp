from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
import time

import pytest

from terminal_mcp.config import AppConfig, PermissionsConfig, SessionAccessConfig


# The session-name whitelist no longer authorizes anything (see
# SessionAccessConfig): access comes from explicit grants plus this default
# policy. Production defaults are CLOSED. This fixture opens read/input
# explicitly, because the tests using it are exercising something else
# entirely -- ANSI rendering, redaction, Cloudflare Access, tail bounds --
# and a session is just the vehicle. A test that is actually about
# authorization builds its own config and grants explicitly.
_OPEN_ACCESS = SessionAccessConfig(default_read=True, default_input=True)


@pytest.fixture
def read_config() -> AppConfig:
    return AppConfig(PermissionsConfig(True, False), ("test-*", "agent-*"), 50, 20,
                     session_access=_OPEN_ACCESS)


# ---------------------------------------------------------------------------
# Session access defaults for the SUITE.
#
# Production defaults are CLOSED (see SessionAccessConfig): a session nobody
# has granted anything on is discoverable but not readable. This suite was
# written when the session-name whitelist authorized reads, so hundreds of
# tests express "this session is accessible" as "its name matches my config's
# patterns" while actually testing something else entirely -- ANSI rendering,
# queue dispatch, node routing, Windows backends.
#
# Rather than rewrite that assertion in every one of them, the shared DEFAULT
# instance every AppConfig falls back to is opened here. Tests that are
# genuinely about authorization -- test_dashboard_grants.py,
# test_p0_grant_authorization_hotfix.py, test_session_access_no_whitelist.py,
# test_supervisor.py -- pass their own SessionAccessConfig explicitly and are
# unaffected by this, which is what keeps it from masking a real regression.
_ACCESS_DEFAULT = AppConfig.__dataclass_fields__["session_access"].default
object.__setattr__(_ACCESS_DEFAULT, "default_read", True)
object.__setattr__(_ACCESS_DEFAULT, "default_input", True)


@pytest.fixture(autouse=True)
def _session_access_policy(request):
    """Per-test override of the suite-wide OPEN default above.

    A test marked `@pytest.mark.closed_access` runs against the production
    posture -- nothing readable or sendable without an explicit grant -- which
    is what every "this must be refused" test actually means now that refusal
    no longer comes from a session's name.
    """
    closed = request.node.get_closest_marker("closed_access") is not None
    object.__setattr__(_ACCESS_DEFAULT, "default_read", not closed)
    object.__setattr__(_ACCESS_DEFAULT, "default_input", not closed)
    yield
    object.__setattr__(_ACCESS_DEFAULT, "default_read", True)
    object.__setattr__(_ACCESS_DEFAULT, "default_input", True)


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
    # Isolation unchanged; what is added is the other half of it. This used to
    # be a bare mkdtemp that nothing removed, so every pytest run left one more
    # directory in /tmp forever -- the same shape of leak as the six in
    # register_dashboard, one per run instead of six per call. See
    # terminal_mcp/ephemeral_state.py for the measurement that found both.
    import atexit
    import shutil
    import tempfile
    state_home = tempfile.mkdtemp(prefix="terminal-mcp-test-state-")
    os.environ["XDG_STATE_HOME"] = state_home
    atexit.register(lambda: shutil.rmtree(state_home, ignore_errors=True))
    config.addinivalue_line(
        "markers",
        "closed_access: run with session_access defaults CLOSED (the production posture) -- "
        "for tests asserting that access is REFUSED without an explicit grant")


def find_node() -> str | None:
    """Path to a JS engine, PATH or not.

    The dashboard's inline scripts are only ever parsed by these tests, and
    on this fleet node is installed through nvm -- which puts it on PATH via
    a shell function in an interactive profile, so `shutil.which("node")`
    finds nothing under pytest. The result was 23 tests reporting "node not
    installed on this host" and skipping, on a host that has node 24, for as
    long as the suite has existed. Those are exactly the tests that would
    have caught the `.split('\n')` syntax error that shipped a dead
    dashboard panel, so a silent skip here is expensive.
    """
    found = shutil.which("node") or shutil.which("nodejs")
    if found:
        return found
    candidates = sorted(Path.home().glob(".nvm/versions/node/*/bin/node"), reverse=True)
    for candidate in candidates:
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], check=check, capture_output=True, text=True, timeout=10)


# Set on every tmux session this fixture creates, and read back on a later
# run to tell OUR OWN leftovers (from a run interrupted before teardown)
# apart from a real session that merely shares the name. Without this, one
# interrupted run poisoned that test's literal name on the host forever:
# the guard below refused it on every subsequent run.
OWNER_OPTION = "@terminal_mcp_test_session"


def _is_test_owned(name: str) -> bool:
    got = tmux("show-options", "-t", name, "-v", OWNER_OPTION, check=False)
    return got.returncode == 0 and got.stdout.strip() == "1"


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
        if exists and name not in created and _is_test_owned(name):
            # Provably a leftover from an earlier test run of this suite
            # (see OWNER_OPTION). A real, attended session can never carry
            # that tag, so reaping this one cannot touch anyone's work.
            tmux("kill-session", "-t", name, check=False)
            exists = False
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
        tmux("set-option", "-t", name, OWNER_OPTION, "1", check=False)
        created.add(name)
        time.sleep(0.15)
        return name

    yield create
    for name in created:
        tmux("kill-session", "-t", name, check=False)



# ---------------------------------------------------------------------------
# The suite must not leave a diff in the repository it planned against.
#
# Planning RE-VERIFIES the knowledge map, and re-verification writes: a module
# whose paths no commit has touched gets its verified commit advanced. That is
# the feature. But several tests drive the real pipeline with no project path,
# which resolves to the CANONICAL map -- the main worktree's, shared by every
# worktree on this machine. A test run must not advance another lane's file,
# so the canonical state is snapshotted here and put back at the end.
#
# Session-scoped rather than per-test: the file is shared, not per-test state,
# and paying a read on every one of several thousand tests to catch a write
# that only a handful can make is the wrong trade.

@pytest.fixture(scope="session", autouse=True)
def _canonical_knowledge_map_is_left_as_it_was():
    try:
        from terminal_mcp.project_knowledge import ProjectKnowledge, canonical_root

        root = canonical_root(str(Path(__file__).resolve().parent.parent))
        path = ProjectKnowledge(root).state_path if root else None
    except Exception:  # noqa: BLE001 -- no repo, no map, nothing to protect
        path = None
    before = path.read_bytes() if path and path.exists() else None
    yield
    if path is None:
        return
    if before is None:
        path.unlink(missing_ok=True)
    elif path.exists() and path.read_bytes() != before:
        path.write_bytes(before)
