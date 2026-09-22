from __future__ import annotations

import os
import uuid
import shutil
import subprocess
from pathlib import Path
import time

import pytest

from terminal_mcp.tmux import TMUX_SOCKET_ENV

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
    db for it. A unique tmux socket per test run also isolates sessions
    from live services and other concurrent test runs."""
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
    config._tmcp_test_socket = f"tmcp-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    os.environ[TMUX_SOCKET_ENV] = config._tmcp_test_socket
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


def pytest_unconfigure(config: pytest.Config) -> None:
    socket_name = getattr(config, "_tmcp_test_socket", None)
    if socket_name and shutil.which("tmux"):
        subprocess.run(["tmux", "-L", socket_name, "kill-server"],
                       check=False, capture_output=True, timeout=10)


def tmux_cmd() -> list[str]:
    socket_name = os.environ.get(TMUX_SOCKET_ENV, "")
    return ["tmux", *(("-L", socket_name) if socket_name else ())]


def tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*tmux_cmd(), *args], check=check, capture_output=True, text=True, timeout=10)


# Set on every tmux session this fixture creates, and read back on a later
# run to tell OUR OWN leftovers (from a run interrupted before teardown)
# apart from a real session that merely shares the name. Without this, one
# interrupted run poisoned that test's literal name on the host forever:
# the guard below refused it on every subsequent run.
OWNER_OPTION = "@terminal_mcp_test_session"

# The tag now carries WHICH run owns the session, not just "a test does".
# Two full suites on one host is this project's normal state (one per
# worktree lane), and they collide on these literal names. With the old
# "1" marker the second run read "test-owned" and KILLED a session the
# first run was actively using, which surfaces as a storm of unrelated
# failures in the other run rather than as anything pointing here.

# Names this suite has actually CREATED on this host, remembered across
# runs. Needed because the tag lives on the session, and a session can be
# recreated during a test by the code under test (the recovery engine
# rebuilds a session by name; so does terminal_create_session). That
# replacement carries no tag, so an interrupted run leaves an UNTAGGED
# session under a name only this suite ever uses -- which is exactly what
# poisoned `webterm-smoke-readonly` and `test-tail-order` here. A name
# only lands in this ledger when the fixture created it from nothing, so
# a real session's name can never enter it by being refused.
def _name_ledger() -> Path:
    """Deliberately NOT under XDG_STATE_HOME, and deliberately resolved on
    each call rather than at import.

    pytest_configure above points XDG_STATE_HOME at a fresh temp directory
    per run and deletes it on exit -- correct for everything the suite
    writes, and fatal for this one file, whose entire job is to be read by
    the NEXT run. Resolving at import would also make the path depend on
    whether this module was imported before or after that hook ran, which
    is the kind of difference that shows up as one baffling failure and
    nothing else."""
    return Path.home() / ".local" / "state" / "terminal-mcp" / "test-session-names"


def _process_start_time(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat: the tick the process started at.

    Paired with the pid so a recycled pid cannot make a dead owner look
    alive. Returns None off Linux or when the process is gone, and every
    caller treats None as "cannot prove it is alive".
    """
    try:
        with open(f"/proc/{pid}/stat", "r") as handle:
            data = handle.read()
    except OSError:
        return None
    # The comm field can contain spaces and parentheses; everything after
    # the LAST ')' is positional.
    tail = data[data.rfind(")") + 1:].split()
    return tail[19] if len(tail) > 19 else None


_RUN_OWNER = f"{os.getpid()}:{_process_start_time(os.getpid()) or 0}"


def _owner_alive(owner: str) -> bool:
    """True only when the run that tagged this session is provably still
    running. Anything unparseable is treated as not alive: the sessions
    this is asked about carry test-only names, and refusing to reap a
    genuine leftover forever is the failure mode that sent a human to a
    terminal to run kill-session by hand."""
    if owner == _RUN_OWNER:
        return True
    pid_text, _, start = owner.partition(":")
    if not pid_text.isdigit():
        return False
    return _process_start_time(int(pid_text)) == start


def _session_owner(name: str) -> str | None:
    """The run that owns this session, "" for the legacy untargeted marker,
    or None when the session carries no marker at all."""
    got = tmux("show-options", "-t", name, "-v", OWNER_OPTION, check=False)
    if got.returncode != 0:
        return None
    value = got.stdout.strip()
    if not value:
        return None
    # "1" is what runs from before this change write. Treat it as owned by
    # a run we cannot identify -- reapable, since that is what it meant.
    return "" if value == "1" else value


def _remember_created(name: str) -> None:
    try:
        _name_ledger().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if name in _known_created_names():
            return
        with open(_name_ledger(), "a") as handle:
            handle.write(name + "\n")
    except OSError:
        # A ledger that cannot be written costs us the untagged-orphan
        # cleanup and nothing else; it must never fail a test run.
        pass


def _known_created_names() -> set[str]:
    try:
        with open(_name_ledger(), "r") as handle:
            return {line.strip() for line in handle if line.strip()}
    except OSError:
        return set()


def _is_test_owned(name: str) -> bool:
    return _session_owner(name) is not None


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
            owner = _session_owner(name)
            if owner is not None and _owner_alive(owner):
                # Tagged, and the run that tagged it is STILL RUNNING --
                # another suite on this host (one per worktree lane is
                # normal here) is using this session right now. Killing it
                # would break that run in ways that point nowhere near
                # this line, so refuse loudly instead.
                raise RuntimeError(
                    f"tmux session {name!r} belongs to another test run that is still "
                    f"alive (owner {owner}). Two suites on one host collide on these "
                    "literal names -- run them one at a time, or give this test a "
                    "unique disposable name."
                )
            if owner is not None:
                # Tagged by a run that is gone: a leftover from a suite
                # interrupted before teardown. Reaping it cannot touch
                # anyone's work.
                tmux("kill-session", "-t", name, check=False)
                exists = False
            elif name in _known_created_names():
                # UNTAGGED, but this suite has created this exact name on
                # this host before. That is the recreated-by-the-code-under
                # -test case: the recovery engine (and terminal_create_
                # session) rebuild a session by name, and the replacement
                # carries no tag, so an interrupted run leaves a nameless
                # squatter that refused this test on every later run until
                # a human killed it by hand. The name reached the ledger
                # only by being created from nothing here, so a real
                # session's name cannot arrive this way.
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
        tmux("set-option", "-t", name, OWNER_OPTION, _RUN_OWNER, check=False)
        _remember_created(name)
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


# ---------------------------------------------------------------------------
# A DECLARED TOOLCHAIN, so a harness test asserts about the harness.
#
# harness_engine.partition_checks asks THIS machine whether a declared check's
# first word is a real program, and that is deliberate: the evaluator-skip is
# sound only because an exit status is not an opinion, and "typecheck" has no
# exit status. The consequence is that any test naming `node -v` or
# `npm test` is secretly asserting something about the host, and the harness
# tests are full of both.
#
# That is exactly what happened on the failover to this machine: ten harness
# tests that were green where they were written went red here, not because
# the engine changed but because the host has no Node at all. A suite whose
# result depends on which laptop is running it cannot be used to prove a
# recovery, which is the one job it had.
#
# So the toolchain a test DECLARES is materialised for that test. The shims
# are never executed -- every one of these tests injects `check_runner`, and
# the engine's real subprocess path is covered separately by checks that use
# shell builtins -- so this supplies the one fact partition_checks needs
# (`shutil.which` resolves the name) and supplies nothing else.
#
# It is not autouse: a test that means "a name this machine does not have"
# must keep getting that answer.

_DECLARED_PROGRAMS = ("node", "npm")


@pytest.fixture
def declared_toolchain(monkeypatch, tmp_path):
    """Make the programs the harness tests name resolvable on PATH."""
    bin_dir = tmp_path / "declared-toolchain-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for program in _DECLARED_PROGRAMS:
        shim = bin_dir / program
        # Exits non-zero if it is ever actually run, so a test that starts
        # depending on the OUTPUT of one of these fails loudly here rather
        # than silently passing against a stub.
        shim.write_text(
            "#!/bin/sh\n"
            f"echo '{program}: test shim, not a real toolchain' >&2\n"
            "exit 70\n")
        shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir
