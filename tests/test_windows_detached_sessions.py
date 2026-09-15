"""Detached Windows sessions, end to end: real hosts, a real agent death.

These are the tests that decide whether the redesign actually works, so none of
them mock the thing under test. Each one spawns a REAL detached session host
running a REAL child process over a REAL pty, then kills the process that
created it -- the way a node-agent update kills the agent -- and checks the
session is still running and still usable afterwards.

The requirement numbers referenced below are the ones in the task contract:

    1  an agent restart cannot terminate active sessions
    2  after restart the agent rediscovers them under the same logical ids
    3  stdout/history and input continue
    4  orphan cleanup is explicit and safe
    5  no duplicate session creation on restart
    6  crash/reboot behaviour (documented; the crash half is tested here)
    8  atomic metadata and stale-PID protection
    10 restart simulation, reconnect, stale metadata, concurrency, crash,
       claude.exe adapter

Platform honesty: the child processes here are Python over a POSIX pty, because
this project's dev and CI hosts are Linux and the live Windows node must not be
restarted. What is exercised for real is every line of the host, the proxy, the
adoption logic and the backend integration. What is NOT exercised is ConPTY and
the Win32 creation flags; those are asserted structurally and reviewed against
the Win32 contract -- see docs/WINDOWS_SESSION_HOST.md, which states the same
limitation in the rollout procedure rather than implying they were measured.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from terminal_mcp import windows_detached, windows_session_host as wsh
from terminal_mcp.windows_backend import WindowsSessionBackend
from terminal_mcp.windows_session_host import HOST_ALIVE, HOST_GONE, SessionPaths

pytestmark = pytest.mark.skipif(os.name == "nt",
                                reason="the POSIX pty factory stands in for ConPTY here")

REPO_ROOT = Path(__file__).resolve().parent.parent
PTY_FACTORY = "posix_pty_factory:factory"

# A stand-in for an interactive agent CLI: echoes what it is told, so a test can
# prove input reached the child and output came back through the spool.
ECHO_CHILD = r"""
import sys
sys.stdout.write("READY\n")
sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if line == "quit":
        break
    sys.stdout.write("got:" + line + "\n")
    sys.stdout.flush()
"""


def _host_env():
    env = dict(os.environ)
    env[wsh.PTY_FACTORY_ENV] = PTY_FACTORY
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), str(REPO_ROOT / "tests"), env.get("PYTHONPATH", "")])
    return env


def _wait(predicate, *, timeout=15.0, interval=0.05, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    pytest.fail(f"timed out after {timeout:g}s waiting for {what}")


def _spool_contains(paths, needle):
    return lambda: needle in wsh.read_spool(paths, max_bytes=1 << 20)[0]


@pytest.fixture
def state_root(tmp_path):
    """Every test's state root, cleaned up unconditionally when it ends.

    Not a courtesy: these tests deliberately create processes that outlive
    pytest, so a test failing BEFORE its own kill_session leaves a real detached
    host running on the machine with no owner. Teardown, not the happy path, is
    what has to guarantee cleanup -- proven the hard way by a failing run that
    leaked exactly that."""
    root = tmp_path / "win-sessions"
    try:
        yield root
    finally:
        _cleanup(root)


@pytest.fixture
def child_script(tmp_path):
    script = tmp_path / "agent_stub.py"
    script.write_text(ECHO_CHILD)
    return script


def _make_session(state_root, name, child_script, cwd=None):
    """Create a real detached session, exactly as the backend's factory does."""
    factory = windows_detached.session_process_factory(state_root)
    env = _host_env()
    proxy = factory(name, [sys.executable, "-u", str(child_script)], str(cwd or REPO_ROOT))
    return proxy


@pytest.fixture
def spawn_env(monkeypatch):
    """Point spawn_host's inherited environment at the POSIX pty factory."""
    env = _host_env()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


# == requirement 1 + 2 + 3 + 5: the flagship test ========================

_AGENT_SCRIPT = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, os.path.join(sys.argv[1], "tests"))
from terminal_mcp import windows_detached
factory = windows_detached.session_process_factory(sys.argv[2])
for name in sys.argv[4:]:
    proxy = factory(name, [sys.executable, "-u", sys.argv[3]], sys.argv[1])
    sys.stdout.write("created %s host=%d child=%d\n" % (name, proxy.host_pid, proxy.pid))
sys.stdout.flush()
time.sleep(300)
"""


def _run_agent(state_root, child_script, names):
    """Start a throwaway process that plays the node agent: it creates the
    sessions and then sits there, exactly like an agent serving requests."""
    agent = subprocess.Popen(
        [sys.executable, "-u", "-c", _AGENT_SCRIPT, str(REPO_ROOT), str(state_root),
         str(child_script), *names],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env=_host_env(), start_new_session=True)
    assert os.getpgid(agent.pid) == agent.pid, "the agent must own its process group"
    lines = []
    for _ in names:
        line = agent.stdout.readline()
        assert line.startswith("created "), f"agent failed to create a session: {line!r}\n"
        lines.append(line.strip())
    return agent, lines


def _kill_agent(agent):
    """Kill the agent the harshest way an update can: SIGKILL to its whole
    process group. No cleanup, no chance to tidy up, nothing graceful."""
    os.killpg(os.getpgid(agent.pid), signal.SIGKILL)
    agent.wait(timeout=15)
    assert agent.poll() is not None


def test_sessions_survive_the_agent_being_killed_and_are_re_adopted(state_root, child_script):
    """Requirements 1, 2, 3 and 5 in one run, which is the only way they are
    meaningful: an agent creates two sessions, the agent is SIGKILLed with its
    whole process group, and a fresh agent then finds both sessions alive under
    their original names, reads the history produced before the kill, and sends
    new input that the still-running child answers."""
    agent, created = _run_agent(state_root, child_script, ["win1", "win2"])
    try:
        paths = {name: SessionPaths(state_root, name) for name in ("win1", "win2")}
        for name, p in paths.items():
            _wait(_spool_contains(p, "READY"), what=f"{name} to start")
        host_pids = {}
        for line in created:
            parts = dict(token.split("=") for token in line.split() if "=" in token)
            host_pids[line.split()[1]] = int(parts["host"])

        # Produce history the NEXT agent never saw being produced.
        for name, p in paths.items():
            assert wsh.write_input(p, f"before-restart-{name}\n") is True
            _wait(_spool_contains(p, f"got:before-restart-{name}"),
                  what=f"{name} to echo pre-restart input")

        _kill_agent(agent)

        # Requirement 1: the hosts and their children are still running.
        for name, pid in host_pids.items():
            assert wsh.pid_alive(pid), f"{name}'s host died with the agent (requirement 1)"

        # Requirement 2: a brand-new agent finds them by name.
        report = windows_detached.adopt_sessions(state_root)
        assert set(report) == {"win1", "win2"}
        assert all(entry["verdict"] == windows_detached.ADOPTED
                   for entry in report.values()), report

        for name, entry in report.items():
            proxy = entry["proxy"]
            assert proxy.isalive(), f"{name} adopted but not alive"

            # Requirement 3a: history from before the restart is readable.
            history = proxy.read(1 << 20)
            assert "READY" in history
            assert f"got:before-restart-{name}" in history, \
                "history produced while no agent was running must survive"

            # Requirement 3b: input still reaches the live child.
            proxy.write(f"after-restart-{name}\n")
            _wait(_spool_contains(paths[name], f"got:after-restart-{name}"),
                  what=f"{name} to echo post-restart input")

        # Requirement 5: adopting again with those names present is a no-op.
        again = windows_detached.adopt_sessions(state_root, existing=set(report))
        assert all(entry["verdict"] == windows_detached.ALREADY_PRESENT
                   for entry in again.values()), again
        assert all(entry["proxy"] is None for entry in again.values())
    finally:
        _cleanup(state_root)
        if agent.poll() is None:
            _kill_agent(agent)


def test_the_child_process_itself_outlives_the_agent(state_root, child_script):
    """Requirement 1 at the level that matters to a user: not just the host
    wrapper, but the actual conversation process, is still the same PID."""
    agent, created = _run_agent(state_root, child_script, ["win-work"])
    try:
        p = SessionPaths(state_root, "win-work")
        _wait(_spool_contains(p, "READY"), what="the session to start")
        child_pid = int(dict(t.split("=") for t in created[0].split() if "=" in t)["child"])
        assert wsh.pid_alive(child_pid)
        _kill_agent(agent)
        assert wsh.pid_alive(child_pid), \
            "the session's own process died with the agent -- the bug is not fixed"
        # And it is the SAME process, not a respawn: same pid in the metadata.
        assert wsh.read_meta(p).child_pid == child_pid
    finally:
        _cleanup(state_root)
        if agent.poll() is None:
            _kill_agent(agent)


# == requirement 6: crash ================================================

def test_a_crashed_host_is_reported_as_an_orphan_and_never_adopted(state_root, child_script,
                                                                   spawn_env):
    """Requirement 6's crash half: when the HOST dies (not the agent), the
    session is unrecoverable -- the pty went with it. The agent must say so
    rather than adopt a session it cannot read from."""
    proxy = _make_session(state_root, "crashy", child_script)
    p = SessionPaths(state_root, "crashy")
    _wait(_spool_contains(p, "READY"), what="the session to start")

    os.kill(proxy.host_pid, signal.SIGKILL)
    _wait(lambda: not wsh.pid_alive(proxy.host_pid), what="the host to die")

    report = windows_detached.adopt_sessions(state_root)
    assert report["crashy"]["verdict"] == windows_detached.ORPHAN_HOST_GONE
    assert report["crashy"]["proxy"] is None
    assert windows_detached.orphan_names(report) == ["crashy"]
    # Requirement 4: reporting an orphan does not delete it.
    assert p.meta.exists(), "adoption must not delete state on its own"
    assert p.spool.exists()


def test_a_crashed_session_is_not_reported_alive_by_its_proxy(state_root, child_script,
                                                              spawn_env):
    proxy = _make_session(state_root, "dies", child_script)
    _wait(_spool_contains(SessionPaths(state_root, "dies"), "READY"), what="start")
    assert proxy.isalive() is True
    os.kill(proxy.host_pid, signal.SIGKILL)
    _wait(lambda: not proxy.isalive(), what="the proxy to notice the host is gone")
    with pytest.raises(OSError):
        proxy.write("this must be refused\n")


# == requirement 8: stale metadata and PID reuse =========================

def test_a_stale_record_whose_pid_is_live_again_is_not_adopted(state_root):
    """Requirement 8. The host is long gone but its PID now belongs to an
    unrelated process. Adopting that would mean telling an operator the session
    is healthy while writing their keystrokes into a file nobody reads."""
    p = SessionPaths(state_root, "reused")
    wsh.write_meta_atomic(p, wsh.SessionMeta(
        name="reused", host_pid=os.getpid(), host_generation="old",
        child_pid=os.getpid(), cwd=str(REPO_ROOT), argv=["claude.exe"],
        created_at=time.time() - 9999))
    old = time.time() - (wsh.STALE_HEARTBEAT_SECONDS + 120)
    os.utime(p.meta, (old, old))

    report = windows_detached.adopt_sessions(state_root)
    assert report["reused"]["verdict"] == windows_detached.ORPHAN_PID_REUSED
    assert report["reused"]["proxy"] is None


def test_a_live_host_is_not_mistaken_for_a_reused_pid(state_root, child_script, spawn_env):
    """The other direction, which matters just as much: a genuinely live session
    must not be classified as an orphan, or a restart would abandon it."""
    _make_session(state_root, "healthy", child_script)
    p = SessionPaths(state_root, "healthy")
    _wait(_spool_contains(p, "READY"), what="the session to start")
    # Let at least one heartbeat land, then confirm it stays ALIVE.
    time.sleep(wsh.HEARTBEAT_INTERVAL_SECONDS + 1.0)
    entry = wsh.discover(state_root)["healthy"]
    assert entry["host_state"] == HOST_ALIVE
    assert windows_detached.adopt_sessions(state_root)["healthy"]["verdict"] \
        == windows_detached.ADOPTED
    _cleanup(state_root)


def test_metadata_with_no_name_is_an_orphan_not_a_crash(state_root):
    p = SessionPaths(state_root, "broken")
    p.ensure()
    p.meta.write_text('{"host_pid": 1}')
    report = windows_detached.adopt_sessions(state_root)
    assert report["broken"]["verdict"] == windows_detached.ORPHAN_HOST_GONE


# == requirement 10: concurrency =========================================

def test_many_concurrent_sessions_are_adopted_independently(state_root, child_script):
    """Four sessions, the number live on dell-5530 today. Each must come back
    with its own history -- a crossed spool would show one session another's
    conversation."""
    names = ["win1", "win2", "wtest", "win-work"]
    agent, _ = _run_agent(state_root, child_script, names)
    try:
        for name in names:
            p = SessionPaths(state_root, name)
            _wait(_spool_contains(p, "READY"), what=f"{name} to start")
            assert wsh.write_input(p, f"id-{name}\n") is True
        for name in names:
            _wait(_spool_contains(SessionPaths(state_root, name), f"got:id-{name}"),
                  what=f"{name} to echo")
        _kill_agent(agent)

        report = windows_detached.adopt_sessions(state_root)
        assert sorted(report) == sorted(names)
        for name in names:
            assert report[name]["verdict"] == windows_detached.ADOPTED
            text = report[name]["proxy"].read(1 << 20)
            assert f"got:id-{name}" in text
            for other in names:
                if other != name:
                    assert f"got:id-{other}" not in text, \
                        f"{name}'s spool contains {other}'s output"
    finally:
        _cleanup(state_root)
        if agent.poll() is None:
            _kill_agent(agent)


def test_one_broken_session_directory_does_not_block_the_others(state_root, child_script,
                                                                spawn_env):
    _make_session(state_root, "good", child_script)
    _wait(_spool_contains(SessionPaths(state_root, "good"), "READY"), what="start")
    bad = SessionPaths(state_root, "bad")
    bad.ensure()
    bad.meta.write_text("{ truncated")

    report = windows_detached.adopt_sessions(state_root)
    assert report["good"]["verdict"] == windows_detached.ADOPTED
    assert report["bad"]["verdict"] == windows_detached.ORPHAN_HOST_GONE
    _cleanup(state_root)


# == requirement 10: the claude.exe adapter ==============================

def test_the_claude_adapter_argv_survives_a_restart_verbatim(state_root, child_script,
                                                             spawn_env):
    """The resume contract: core.py passes ("--session-id", uuid) / ("--resume",
    uuid) to claude.exe, and after an agent restart the adopted session must
    still report the exact argv it was started with -- that is what makes a
    resumable conversation identifiable. An argv mangled by quoting would
    silently start a NEW conversation instead of continuing one."""
    session_id = "7f3c1e4a-0000-4a11-9b22-abcdefabcdef"
    argv = [sys.executable, "-u", str(child_script), "--session-id", session_id,
            "--flag with spaces"]
    factory = windows_detached.session_process_factory(state_root)
    proxy = factory("claude-like", argv, str(REPO_ROOT))
    p = SessionPaths(state_root, "claude-like")
    _wait(_spool_contains(p, "READY"), what="the adapter session to start")

    meta = wsh.read_meta(p)
    assert meta.argv == argv, "argv must round-trip through the state file unchanged"

    report = windows_detached.adopt_sessions(state_root)
    adopted = report["claude-like"]
    assert adopted["verdict"] == windows_detached.ADOPTED
    assert adopted["meta"]["argv"] == argv
    assert session_id in adopted["meta"]["argv"]
    _cleanup(state_root)


# == requirement 4: explicit cleanup =====================================

def test_cleanup_removes_an_orphan_only_when_asked(state_root, child_script, spawn_env):
    proxy = _make_session(state_root, "tidy", child_script)
    p = SessionPaths(state_root, "tidy")
    _wait(_spool_contains(p, "READY"), what="start")
    os.kill(proxy.host_pid, signal.SIGKILL)
    _wait(lambda: not wsh.pid_alive(proxy.host_pid), what="the host to die")

    # Several adoption passes: none of them may delete anything.
    for _ in range(3):
        windows_detached.adopt_sessions(state_root)
    assert p.dir.exists()

    assert wsh.cleanup_session_dir(p) is True
    assert not p.dir.exists()
    assert windows_detached.adopt_sessions(state_root) == {}


def test_terminate_stops_the_host_and_removes_its_state(state_root, child_script, spawn_env):
    proxy = _make_session(state_root, "killme", child_script)
    p = SessionPaths(state_root, "killme")
    _wait(_spool_contains(p, "READY"), what="start")
    host_pid, child_pid = proxy.host_pid, proxy.pid

    proxy.terminate(force=True)
    _wait(lambda: not wsh.pid_alive(host_pid), what="the host to exit")
    _wait(lambda: not wsh.pid_alive(child_pid), what="the child to exit")
    assert not p.dir.exists(), "an explicit kill must clean up its state directory"


# == backend integration =================================================

def _backend(state_root):
    return WindowsSessionBackend(
        shell=sys.executable, history_lines=500,
        session_process_factory=windows_detached.session_process_factory(state_root),
        pid_alive_resolver=wsh.pid_alive)


def test_the_backend_creates_detached_sessions_and_a_new_backend_adopts_them(
        state_root, child_script, spawn_env):
    """The integration that matters in production: the backend, unchanged in
    every other respect, creates a session through a detached host; a SECOND
    backend instance -- which is what a restarted agent has -- adopts it and can
    read its history and send it input."""
    first = _backend(state_root)
    first.new_session("win1", str(REPO_ROOT), command=f"{sys.executable}")
    p = SessionPaths(state_root, "win1")
    _wait(lambda: wsh.read_meta(p) is not None, what="the host to publish metadata")
    info = first.get_session("win1")
    assert info is not None and info.name == "win1"

    # The old agent's registry is simply discarded -- no shutdown, no handoff.
    restarted = _backend(state_root)
    assert restarted.list_sessions() == []
    verdicts = restarted.adopt_detached_sessions(state_root)
    assert verdicts == {"win1": windows_detached.ADOPTED}
    assert [s.name for s in restarted.list_sessions()] == ["win1"]

    restarted.send_text("win1", "print('hello-after-restart')", press_enter=True)
    _wait(lambda: any("hello-after-restart" in line
                      for line in restarted.capture_lines("win1", 200)),
          what="the adopted session to answer input")
    restarted.kill_session("win1")
    assert not p.dir.exists()


def test_adopting_twice_does_not_duplicate_a_session(state_root, child_script, spawn_env):
    """Requirement 5 through the backend: a second adoption pass -- a retried
    startup, or two callers racing -- must not produce two entries, two reader
    threads, or two proxies onto one host."""
    backend = _backend(state_root)
    backend.new_session("win2", str(REPO_ROOT), command=f"{sys.executable}")
    p = SessionPaths(state_root, "win2")
    _wait(lambda: wsh.read_meta(p) is not None, what="metadata")

    assert backend.adopt_detached_sessions(state_root) == \
        {"win2": windows_detached.ALREADY_PRESENT}
    assert [s.name for s in backend.list_sessions()] == ["win2"]

    restarted = _backend(state_root)
    assert restarted.adopt_detached_sessions(state_root) == {"win2": windows_detached.ADOPTED}
    assert restarted.adopt_detached_sessions(state_root) == \
        {"win2": windows_detached.ALREADY_PRESENT}
    assert len(restarted.list_sessions()) == 1
    restarted.kill_session("win2")


def test_new_session_still_refuses_a_name_that_was_adopted(state_root, child_script,
                                                           spawn_env):
    """The duplicate guard must cover adopted names too, or a restart followed
    by a create would spawn a second host for a session that already exists --
    two conversations behind one id."""
    from terminal_mcp.tmux import TmuxError

    backend = _backend(state_root)
    backend.new_session("wtest", str(REPO_ROOT), command=f"{sys.executable}")
    _wait(lambda: wsh.read_meta(SessionPaths(state_root, "wtest")) is not None, what="metadata")
    restarted = _backend(state_root)
    restarted.adopt_detached_sessions(state_root)
    with pytest.raises(TmuxError, match="already exists"):
        restarted.new_session("wtest", str(REPO_ROOT), command=f"{sys.executable}")
    restarted.kill_session("wtest")


def test_an_orphaned_session_is_reported_by_the_backend_not_registered(
        state_root, child_script, spawn_env):
    backend = _backend(state_root)
    backend.new_session("gone", str(REPO_ROOT), command=f"{sys.executable}")
    p = SessionPaths(state_root, "gone")
    _wait(lambda: wsh.read_meta(p) is not None, what="metadata")
    host_pid = wsh.read_meta(p).host_pid
    os.kill(host_pid, signal.SIGKILL)
    _wait(lambda: not wsh.pid_alive(host_pid), what="the host to die")

    restarted = _backend(state_root)
    verdicts = restarted.adopt_detached_sessions(state_root)
    assert verdicts == {"gone": windows_detached.ORPHAN_HOST_GONE}
    assert restarted.list_sessions() == [], "an orphan must never enter the registry"
    wsh.cleanup_session_dir(p)


def test_the_linux_backend_path_is_untouched_by_all_of_this(state_root):
    """Requirement 9. A backend with no session_process_factory behaves exactly
    as before -- the detached path is opt-in, so tmux/Linux and any existing
    Windows deployment are unaffected until the factory is wired in."""
    calls = []

    class _Proc:
        pid = os.getpid()

        def isalive(self):
            return True

        def read(self, size=4096):
            time.sleep(0.01)
            return ""

        def write(self, data):
            return len(data)

        def setwinsize(self, rows, cols):
            pass

        def terminate(self, force=False):
            pass

    def _plain_factory(argv, cwd):
        calls.append((argv, cwd))
        return _Proc()

    backend = WindowsSessionBackend(shell=sys.executable, process_factory=_plain_factory,
                                    pid_alive_resolver=lambda pid: True)
    backend.new_session("plain", str(REPO_ROOT))
    assert calls == [([sys.executable], str(REPO_ROOT))], \
        "the 2-arg ProcessFactory contract must be unchanged"
    assert not state_root.exists(), "no state directory may be created for a plain session"
    backend.kill_session("plain")


def _cleanup(state_root):
    """Stop every host these tests created. Runs from fixture teardown, so a
    test that fails before its own kill_session cannot leave a detached process
    running with no owner.

    Only ever signals a host confirmed ALIVE for this session directory. An
    earlier version killed any live pid it found in a metadata file -- and
    `test_a_stale_record_whose_pid_is_live_again_is_not_adopted` deliberately
    writes THIS PROCESS's pid into one, so that version SIGKILLed the test
    runner mid-suite. The same mistake against a real node's state directory
    would kill an arbitrary unrelated process, which is why `terminate_host` now
    demands a fresh heartbeat rather than merely a live pid."""
    root = Path(state_root)
    if not root.is_dir():
        return
    for name, entry in wsh.discover(root).items():
        paths = SessionPaths(root, name)
        meta = wsh.read_meta(paths)
        if meta is None or entry.get("host_state") != HOST_ALIVE:
            continue  # not confirmed ours: never signal it
        if meta.host_pid == os.getpid():
            continue  # belt and braces: never signal the test runner
        wsh.terminate_host(paths, meta)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and wsh.pid_alive(meta.host_pid):
            time.sleep(0.05)
        if (meta.child_pid and meta.child_pid != os.getpid()
                and wsh.pid_alive(meta.child_pid)):
            try:
                os.kill(int(meta.child_pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
    for child in root.iterdir():
        if child.is_dir():
            wsh.cleanup_session_dir(SessionPaths(root, child.name))


# == the two defects found in review =====================================

SILENT_CHILD = r"""
import sys, time
sys.stdout.write("READY\n")
sys.stdout.flush()
time.sleep(600)
"""


def test_an_idle_session_keeps_heartbeating_and_stays_adoptable(tmp_path, state_root,
                                                                spawn_env):
    """The defect: the heartbeat used to live in the same loop as the output
    read. A real pty read blocks until the child says something, so a session
    sitting idle at a prompt -- the normal state of a terminal -- would skip
    heartbeats, go stale past the 30s threshold, and be classified PID_REUSED
    and abandoned while perfectly healthy.

    This test makes the child say READY and then nothing at all for longer than
    the staleness threshold, and requires the session to still be ALIVE."""
    script = tmp_path / "silent.py"
    script.write_text(SILENT_CHILD)
    proxy = _make_session(state_root, "idle", script)
    p = SessionPaths(state_root, "idle")
    _wait(_spool_contains(p, "READY"), what="the session to start")

    try:
        quiet_for = wsh.STALE_HEARTBEAT_SECONDS + 5.0
        first = p.meta.stat().st_mtime
        time.sleep(quiet_for)
        assert p.meta.stat().st_mtime > first, \
            "an idle session stopped heartbeating -- it would be abandoned as PID_REUSED"
        assert wsh.discover(state_root)["idle"]["host_state"] == HOST_ALIVE
        assert windows_detached.adopt_sessions(state_root)["idle"]["verdict"] \
            == windows_detached.ADOPTED
    finally:
        _cleanup(state_root)


def test_a_graceful_terminate_does_not_orphan_the_host(state_root, child_script,
                                                       spawn_env):
    """The defect: terminate(force=False) removed the state directory
    immediately, deleting the shutdown request in ctl.json before the host could
    read it. The host stayed alive with no state directory -- an orphan nothing
    could ever find again, and invisible to every cleanup path."""
    proxy = _make_session(state_root, "graceful", child_script)
    p = SessionPaths(state_root, "graceful")
    _wait(_spool_contains(p, "READY"), what="start")
    host_pid, child_pid = proxy.host_pid, proxy.pid

    proxy.terminate(force=False)

    assert not p.dir.exists(), "state must be removed once the host is gone"
    assert not wsh.pid_alive(host_pid), \
        "the host survived a graceful terminate with its state deleted -- orphaned"
    assert not wsh.pid_alive(child_pid), "the child outlived its host"


# == node-agent wiring ===================================================

def test_detached_sessions_are_off_by_default_in_the_agent_cli():
    """The rollout safety property: copying this code onto the live 0.12.0 node
    must not change how its agent behaves. Only an explicit --detached-sessions
    flag switches the spawn path, so a file deploy and a behaviour change are two
    separate decisions."""
    from terminal_mcp import windows_agent

    parser = _agent_parser()
    defaults = parser.parse_args(["--node-id", "n", "--controller-url", "http://x"])
    assert defaults.detached_sessions is False
    assert defaults.session_state_root is None
    assert parser.parse_args(["--node-id", "n", "--controller-url", "http://x",
                              "--detached-sessions"]).detached_sessions is True


def _agent_parser():
    """Build the agent's parser without starting it, by running main() up to the
    point it parses arguments."""
    import argparse

    from terminal_mcp import windows_agent

    captured = {}
    real_parse = argparse.ArgumentParser.parse_args

    def _capture(self, *a, **kw):
        captured["parser"] = self
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = _capture
    try:
        try:
            windows_agent.main(["--node-id", "n", "--controller-url", "http://x"])
        except SystemExit:
            pass
    finally:
        argparse.ArgumentParser.parse_args = real_parse
    assert "parser" in captured
    return captured["parser"]


def test_the_default_state_root_sits_under_the_node_workspace():
    from terminal_mcp import windows_agent

    class _Lifecycle:
        allowed_cwd_roots = ["/srv/work"]

    class _Config:
        session_lifecycle = _Lifecycle()

    root = windows_agent._default_session_state_root(_Config())
    assert root == Path("/srv/work/.terminal-mcp/win-sessions")


def test_adoption_preserves_the_identity_that_pinned_grants_depend_on(
        state_root, child_script, spawn_env):
    """`SessionInfo.session_id` is `win:<name>:<created_epoch>` and `pane_id` is
    `pid:<child pid>`. Session grants and send-bindings can pin those values, so
    if adoption stamped a fresh creation time -- the obvious thing to do, since
    the entry is being created now -- every pinned grant and binding would stop
    matching after a restart and the operator would silently lose read or input
    capability on a session that is plainly still running.

    Requirement 2 asks for the same logical ids AND read/input capability; this
    is the second half of that, and it is one `time.time()` away from breaking."""
    first = _backend(state_root)
    first.new_session("pinned", str(REPO_ROOT), command=f"{sys.executable}")
    p = SessionPaths(state_root, "pinned")
    _wait(lambda: wsh.read_meta(p) is not None, what="metadata")
    before = first.get_session("pinned")

    time.sleep(1.1)  # so a fresh timestamp would differ visibly
    restarted = _backend(state_root)
    assert restarted.adopt_detached_sessions(state_root) == {"pinned": windows_detached.ADOPTED}
    after = restarted.get_session("pinned")

    assert after.session_id == before.session_id, \
        "session_id changed across adoption -- pinned grants would stop matching"
    assert after.pane_id == before.pane_id, \
        "pane_id changed across adoption -- pinned bindings would stop matching"
    assert after.pane_current_path == before.pane_current_path
    restarted.kill_session("pinned")
