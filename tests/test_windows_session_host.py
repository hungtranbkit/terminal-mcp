"""Detached session hosts: survival across agent restart, and reconnect.

The claim under test is that a session outlives the process that started it. A
mock cannot show that, so these tests spawn REAL processes and really kill the
"agent" that spawned them, then check the session is still running and that a
fresh reader picks up its output and can still type into it. The PTY is a real
POSIX pty (this host is Linux, as are CI and dev), exactly the discipline
tests/test_windows_backend.py already uses.

What these tests DO prove, on this host: the metadata is atomic, a stale or
reused PID cannot be mistaken for a live host, the spool preserves history
across a reader restart, input still reaches the child after the spawner is
gone, discovery rebuilds the session set from disk with no handoff, orphan
cleanup refuses to touch anything that might be alive, and concurrent sessions
stay independent.

What they CANNOT prove: that Windows' DETACHED_PROCESS /
CREATE_BREAKAWAY_FROM_JOB / ConPTY behave as designed on a real Windows host.
That is asserted structurally (the exact flag combination) and reviewed against
the Win32 contract; it is not measured here, and docs/WINDOWS_SESSION_HOST.md
says so in the rollout procedure.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from terminal_mcp import windows_session_host as host
from terminal_mcp.windows_session_host import (
    HOST_ALIVE, HOST_GONE, HOST_PID_REUSED, SessionHost, SessionMeta, SessionPaths,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the POSIX doubles need a POSIX host")


def _meta(name="s1", **kw):
    base = {"name": name, "host_pid": os.getpid(),
            "host_generation": "gen-abc", "child_pid": 4242, "cwd": "/tmp",
            "argv": ["powershell.exe"], "created_at": time.time()}
    base.update(kw)
    return SessionMeta(**base)


@pytest.fixture
def paths(tmp_path):
    p = SessionPaths(tmp_path / "win-sessions", "s1")
    p.ensure()
    return p


# == metadata: atomic, and never half-read ================================

def test_metadata_round_trips(paths):
    host.write_meta_atomic(paths, _meta())
    loaded = host.read_meta(paths)
    assert loaded is not None
    assert loaded.name == "s1" and loaded.host_generation == "gen-abc"
    assert loaded.argv == ["powershell.exe"]


def test_metadata_is_written_via_rename_not_truncation(paths, monkeypatch):
    """A plain open("w") truncates first, so a reader during the write window
    sees an empty file and concludes the session does not exist -- then acts on
    it. The temp-then-replace is what makes that window impossible."""
    seen = {}
    real_replace = os.replace

    def _spy(src, dst):
        seen["src"], seen["dst"] = str(src), str(dst)
        return real_replace(src, dst)

    monkeypatch.setattr(host.os, "replace", _spy)
    host.write_meta_atomic(paths, _meta())
    assert seen["dst"].endswith("meta.json")
    assert seen["src"] != seen["dst"], "must land via a rename, not an in-place write"


def test_no_temp_files_are_left_behind(paths):
    for _ in range(3):
        host.write_meta_atomic(paths, _meta())
    assert not [p for p in paths.dir.iterdir() if p.name.startswith(".meta.json")]


@pytest.mark.parametrize("content", ["", "   ", "{", "[]", '{"no_name": 1}', "null"])
def test_corrupt_metadata_reads_as_absent_never_as_a_guess(paths, content):
    paths.meta.write_text(content)
    assert host.read_meta(paths) is None


def test_missing_metadata_reads_as_absent(tmp_path):
    assert host.read_meta(SessionPaths(tmp_path, "nope")) is None


# == stale PID and PID reuse =============================================

def test_a_dead_host_pid_is_gone(paths):
    meta = _meta(host_pid=999_999_999)
    host.write_meta_atomic(paths, meta)
    assert host.host_state(paths, meta, alive=lambda pid: False) == HOST_GONE


def test_a_live_pid_with_a_fresh_heartbeat_is_alive(paths):
    meta = _meta()
    host.write_meta_atomic(paths, meta)  # mtime = now, i.e. a fresh beat
    assert host.host_state(paths, meta, alive=lambda pid: True) == HOST_ALIVE


def test_a_live_pid_with_a_stale_heartbeat_is_treated_as_pid_reuse(paths):
    """The dangerous case: the host died and the OS handed its number to
    something unrelated. Windows recycles PIDs aggressively, so a live PID is
    not evidence. Adopting it would mean writing input into a pipe nobody reads
    while reporting the session healthy."""
    meta = _meta()
    host.write_meta_atomic(paths, meta)
    old = time.time() - (host.STALE_HEARTBEAT_SECONDS + 60)
    os.utime(paths.meta, (old, old))
    assert host.host_state(paths, meta, alive=lambda pid: True) == HOST_PID_REUSED


def test_a_live_pid_with_no_metadata_at_all_is_pid_reuse(tmp_path):
    p = SessionPaths(tmp_path, "ghost")
    p.ensure()
    assert host.host_state(p, _meta(), alive=lambda pid: True) == HOST_PID_REUSED


def test_pid_alive_on_this_process_and_on_a_reaped_one():
    assert host.pid_alive(os.getpid()) is True
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()  # reaped, so the PID is genuinely gone
    assert host.pid_alive(child.pid) is False
    assert host.pid_alive(0) is False
    assert host.pid_alive(None) is False
    assert host.pid_alive(-1) is False


def test_an_unreaped_dead_process_is_not_reported_alive():
    """A zombie answers kill(0) successfully, so the naive check calls a killed
    host ALIVE for as long as its parent has not waited on it -- and the agent
    adopts a session whose pty is already gone. Windows has no zombie state, so
    this is what makes liveness mean the same thing on both platforms."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            state = Path(f"/proc/{child.pid}/stat").read_bytes().rpartition(b")")[2].split()[0]
            if state == b"Z":
                break
            time.sleep(0.02)
        else:
            pytest.skip("could not observe a zombie on this host")
        assert os.kill(child.pid, 0) is None, "precondition: kill(0) still succeeds"
        assert host.pid_alive(child.pid) is False, "a zombie must never read as alive"
    finally:
        child.wait()


# == the spool: history survives the reader ===============================

def test_the_spool_accumulates_and_reads_from_an_offset(paths):
    session = SessionHost(paths, process=None)
    session.append_output("first\n")
    session.append_output("second\n")
    text, offset = host.read_spool(paths)
    assert text == "first\nsecond\n"
    session.append_output("third\n")
    more, offset2 = host.read_spool(paths, offset=offset)
    assert more == "third\n", "an offset read must not repeat what was already seen"
    assert offset2 > offset


def test_reading_past_a_rotation_resets_rather_than_returning_garbage(paths):
    session = SessionHost(paths, process=None, spool_max_bytes=200)
    for n in range(80):
        session.append_output(f"line {n:04d}\n")
    _, offset = host.read_spool(paths)
    session._rotate_spool()
    text, new_offset = host.read_spool(paths, offset=offset + 10_000)
    assert new_offset >= 0
    assert "\x00" not in text


def test_rotation_keeps_the_recent_tail_not_the_ancient_head(paths):
    session = SessionHost(paths, process=None, spool_max_bytes=400)
    for n in range(200):
        session.append_output(f"line {n:04d}\n")
    text, _ = host.read_spool(paths)
    assert "line 0199" in text, "the newest output must survive rotation"
    assert "line 0000" not in text, "the oldest is what gets dropped"
    assert paths.spool.stat().st_size <= 400 * 3


def test_rotation_resumes_on_a_line_boundary(paths):
    session = SessionHost(paths, process=None, spool_max_bytes=300)
    for n in range(100):
        session.append_output(f"line {n:04d}\n")
    text, _ = host.read_spool(paths)
    assert text.startswith("line "), "a reader must not start mid-line"


def test_appending_nothing_is_a_no_op(paths):
    SessionHost(paths, process=None).append_output("")
    assert not paths.spool.exists()


# == control requests ====================================================

def test_control_requests_are_consumed_exactly_once(paths):
    host.request_control(paths, {"op": "resize", "rows": 40, "cols": 100})
    host.request_control(paths, {"op": "shutdown"})
    session = SessionHost(paths, process=None)
    first = session.drain_control()
    assert [r["op"] for r in first] == ["resize", "shutdown"]
    assert session.drain_control() == [], "a consumed request must not replay"


def test_a_malformed_control_line_is_skipped_not_fatal(paths):
    paths.control.write_text('not json\n{"op": "shutdown"}\n\n')
    assert [r["op"] for r in SessionHost(paths, process=None).drain_control()] == ["shutdown"]


def test_resize_reaches_the_process_and_updates_metadata(paths):
    class _Proc:
        def __init__(self):
            self.size = None

        def setwinsize(self, rows, cols):
            self.size = (rows, cols)

    proc = _Proc()
    session = SessionHost(paths, proc)
    session.publish(_meta())
    session.apply_control({"op": "resize", "rows": 50, "cols": 120})
    assert proc.size == (50, 120)
    assert host.read_meta(paths).rows == 50


def test_a_resize_that_throws_does_not_kill_the_host(paths):
    class _Boom:
        def setwinsize(self, rows, cols):
            raise RuntimeError("conpty said no")

    session = SessionHost(paths, _Boom())
    session.publish(_meta())
    session.apply_control({"op": "resize", "rows": 10, "cols": 10})  # must not raise


def test_shutdown_control_stops_the_host(paths):
    session = SessionHost(paths, process=None)
    session.apply_control({"op": "shutdown"})
    assert session._stop is True


# == THE CENTRAL TEST: survival across the spawner's death ================

_CHILD = (
    "import sys, time, pathlib\n"
    "marker = pathlib.Path(sys.argv[1])\n"
    "for n in range(600):\n"
    "    marker.write_text(str(n))\n"
    "    time.sleep(0.05)\n"
)


def _spawn_detached(script_args, *, cwd):
    """Spawn with the same detachment intent spawn_host uses on POSIX."""
    return subprocess.Popen([sys.executable, "-c", _CHILD, *script_args],
                            cwd=cwd, start_new_session=True, close_fds=True,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_a_detached_child_survives_its_spawner_being_killed(tmp_path):
    """The whole point, reduced to its core on this platform: a process started
    with a new session is not in the spawner's process group, so killing that
    group does not reach it.

    This is the POSIX analogue of CREATE_BREAKAWAY_FROM_JOB. It does not prove
    the Windows behaviour -- nothing on this host can -- but it does prove the
    spawn shape is right and that nothing in our own code re-couples them."""
    marker = tmp_path / "beat"
    spawner = subprocess.Popen(
        # Its own session, so the killpg below reaches the spawner and NOT the
        # pytest process that started it.
        start_new_session=True,
        args=[sys.executable, "-c",
         "import subprocess, sys, time\n"
         "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]],\n"
         "                 start_new_session=True, close_fds=True,\n"
         "                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
         "                 stderr=subprocess.DEVNULL)\n"
         "time.sleep(30)\n",
         _CHILD, str(marker)])
    assert os.getpgid(spawner.pid) == spawner.pid, "spawner must own its group"
    try:
        deadline = time.time() + 10
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "the grandchild never started"

        # Kill the spawner's whole process group -- the harshest thing the
        # agent's death can look like.
        os.killpg(os.getpgid(spawner.pid), signal.SIGKILL)
        spawner.wait(timeout=10)

        before = marker.read_text()
        time.sleep(0.6)
        after = marker.read_text()
        assert after != before, "the session must keep running after its spawner died"
    finally:
        if spawner.poll() is None:
            spawner.kill()


def test_history_and_input_continue_across_a_reader_restart(paths, tmp_path):
    """Requirement 3, end to end: a fresh 'agent' reads the full history it
    never saw being produced, and its input still reaches the child."""
    import pty

    master, slave = pty.openpty()
    child = subprocess.Popen([sys.executable, "-u", "-c",
                              "import sys\n"
                              "for line in sys.stdin:\n"
                              "    sys.stdout.write('echo:' + line)\n"
                              "    sys.stdout.flush()\n"],
                             stdin=slave, stdout=slave, stderr=slave,
                             start_new_session=True, close_fds=True)
    os.close(slave)
    try:
        os.set_blocking(master, False)
        session = SessionHost(paths, process=None)
        session.publish(_meta(child_pid=child.pid))

        os.write(master, b"one\n")
        time.sleep(0.4)
        try:
            session.append_output(os.read(master, 4096).decode())
        except BlockingIOError:
            pass

        # "Agent restart": everything in memory is discarded. A brand-new reader
        # starts from offset 0 and must see the history anyway.
        text, offset = host.read_spool(paths)
        assert "echo:one" in text, "history produced before the restart must survive"

        os.write(master, b"two\n")
        time.sleep(0.4)
        try:
            session.append_output(os.read(master, 4096).decode())
        except BlockingIOError:
            pass
        more, _ = host.read_spool(paths, offset=offset)
        assert "echo:two" in more, "input must still reach the child after the restart"
    finally:
        child.kill()
        os.close(master)


# == discovery / reconnect / no duplicate creation ========================

def test_discovery_rebuilds_the_session_set_from_disk(tmp_path):
    root = tmp_path / "win-sessions"
    for name in ("win1", "win2"):
        p = SessionPaths(root, name)
        host.write_meta_atomic(p, _meta(name=name))
    found = host.discover(root, alive=lambda pid: True)
    assert set(found) == {"win1", "win2"}
    assert all(entry["host_state"] == HOST_ALIVE for entry in found.values())


def test_discovery_classifies_dead_and_reused_hosts_separately(tmp_path):
    root = tmp_path / "win-sessions"
    alive_p = SessionPaths(root, "live")
    host.write_meta_atomic(alive_p, _meta(name="live"))
    dead_p = SessionPaths(root, "dead")
    host.write_meta_atomic(dead_p, _meta(name="dead", host_pid=999_999_999))
    stale_p = SessionPaths(root, "stale")
    host.write_meta_atomic(stale_p, _meta(name="stale"))
    old = time.time() - 10_000
    os.utime(stale_p.meta, (old, old))

    found = host.discover(root, alive=lambda pid: pid != 999_999_999)
    assert found["live"]["host_state"] == HOST_ALIVE
    assert found["dead"]["host_state"] == HOST_GONE
    assert found["stale"]["host_state"] == HOST_PID_REUSED


def test_discovery_reports_a_directory_with_no_metadata_as_gone(tmp_path):
    root = tmp_path / "win-sessions"
    (root / "half-made").mkdir(parents=True)
    found = host.discover(root)
    assert found["half-made"]["host_state"] == HOST_GONE
    assert found["half-made"]["reason"] == "NO_METADATA"


def test_discovery_on_a_missing_root_is_empty_not_an_error(tmp_path):
    assert host.discover(tmp_path / "never-created") == {}


def test_discovery_ignores_stray_files_next_to_session_dirs(tmp_path):
    root = tmp_path / "win-sessions"
    root.mkdir(parents=True)
    (root / "README.txt").write_text("not a session\n")
    assert host.discover(root) == {}


def test_rediscovery_is_idempotent_so_a_restart_creates_no_duplicates(tmp_path):
    """Requirement 5. Discovery is a pure read keyed by directory name, so
    running it twice cannot produce two sessions for one name -- there is no
    create path in it at all."""
    root = tmp_path / "win-sessions"
    host.write_meta_atomic(SessionPaths(root, "win1"), _meta(name="win1"))
    first = host.discover(root, alive=lambda pid: True)
    second = host.discover(root, alive=lambda pid: True)
    assert first == second
    assert len(list(root.iterdir())) == 1


def test_concurrent_sessions_stay_independent(tmp_path):
    root = tmp_path / "win-sessions"
    sessions = {}
    for name in ("win1", "win2", "wtest", "win-work"):
        p = SessionPaths(root, name)
        session = SessionHost(p, process=None)
        session.publish(_meta(name=name, child_pid=1000 + len(sessions)))
        session.append_output(f"output for {name}\n")
        sessions[name] = p

    for name, p in sessions.items():
        text, _ = host.read_spool(p)
        assert text == f"output for {name}\n", f"{name} saw another session's output"
        assert host.read_meta(p).name == name

    # Removing one leaves the others untouched.
    assert host.cleanup_session_dir(sessions["wtest"]) is True
    remaining = host.discover(root, alive=lambda pid: True)
    assert set(remaining) == {"win1", "win2", "win-work"}


# == orphan cleanup: explicit and safe ===================================

def test_cleanup_removes_only_the_directory_it_is_given(tmp_path):
    root = tmp_path / "win-sessions"
    keep = SessionPaths(root, "keep")
    drop = SessionPaths(root, "drop")
    for p in (keep, drop):
        host.write_meta_atomic(p, _meta(name=p.name))
    assert host.cleanup_session_dir(drop) is True
    assert not drop.dir.exists()
    assert keep.meta.exists()


def test_cleanup_of_an_absent_directory_is_false_not_an_exception(tmp_path):
    assert host.cleanup_session_dir(SessionPaths(tmp_path, "never")) is False


def test_terminate_refuses_a_dead_pid(paths):
    killed = []
    meta = _meta(host_pid=999_999_999)
    host.write_meta_atomic(paths, meta)
    assert host.terminate_host(paths, meta, alive=lambda pid: False,
                              killer=killed.append) is False
    assert killed == [], "nothing may be signalled for a host already gone"


def test_terminate_refuses_a_zero_or_negative_pid(paths):
    killed = []
    for bad in (0, -1, None):
        assert host.terminate_host(paths, _meta(host_pid=bad), alive=lambda pid: True,
                                  killer=killed.append) is False
    assert killed == []


def test_terminate_refuses_a_record_whose_pid_looks_reused(paths):
    """The dangerous one. The host is gone and its PID now belongs to some
    unrelated process -- possibly this very test runner. Signalling it would
    kill a stranger, and on Windows `taskkill /T` would take that stranger's
    children too. A live PID is not permission to kill."""
    killed = []
    meta = _meta(host_pid=os.getpid())
    host.write_meta_atomic(paths, meta)
    old = time.time() - (host.STALE_HEARTBEAT_SECONDS + 120)
    os.utime(paths.meta, (old, old))

    assert host.host_state(paths, meta, alive=lambda pid: True) == HOST_PID_REUSED
    assert host.terminate_host(paths, meta, alive=lambda pid: True,
                              killer=killed.append) is False
    assert killed == [], "a reused PID was signalled -- this could kill any process"


def test_terminate_refuses_when_metadata_is_missing(tmp_path):
    """No heartbeat at all means nothing confirms the recorded host is ours."""
    killed = []
    p = SessionPaths(tmp_path, "nometa")
    p.ensure()
    assert host.terminate_host(p, _meta(host_pid=os.getpid()), alive=lambda pid: True,
                              killer=killed.append) is False
    assert killed == []


def test_terminate_signals_a_confirmed_live_host(paths):
    killed = []
    meta = _meta(host_pid=4242)
    host.write_meta_atomic(paths, meta)  # fresh heartbeat
    assert host.terminate_host(paths, meta, alive=lambda pid: True,
                              killer=killed.append) is True
    assert killed == [4242]


def test_the_destructive_primitives_are_never_self_invoked():
    """Requirement 4: orphan cleanup is explicit. `cleanup_session_dir` deletes a
    session's whole history and `terminate_host` signals a process, so neither may
    be reachable from any loop, timer or pump inside this module -- only from a
    caller that has decided. An AST check, because a future edit adding a
    convenience "tidy up while we're here" call is exactly the regression that
    would destroy a live session whose host was briefly slow to heartbeat."""
    import ast

    tree = ast.parse(Path(host.__file__).read_text())
    destructive = {"cleanup_session_dir", "terminate_host", "rmtree"}
    offenders = []
    for func in [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        if func.name in destructive:
            continue  # the primitives themselves may of course do the work
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
            if name in destructive:
                offenders.append(f"{func.name} -> {name}")
    assert offenders == [], f"destructive primitive called from inside the module: {offenders}"


# == Windows flags, asserted structurally ================================

def test_the_detach_flags_are_exactly_the_four_that_matter():
    assert host.WINDOWS_DETACH_FLAGS == (
        host.DETACHED_PROCESS | host.CREATE_BREAKAWAY_FROM_JOB
        | host.CREATE_NEW_PROCESS_GROUP | host.CREATE_NO_WINDOW)


def test_create_breakaway_from_job_is_present():
    """The one whose absence silently reintroduces the bug: without breakaway a
    job object with KILL_ON_JOB_CLOSE -- which is how a Scheduled Task tree is
    commonly configured -- kills the host with the agent regardless of how
    detached it otherwise looks."""
    assert host.WINDOWS_DETACH_FLAGS & host.CREATE_BREAKAWAY_FROM_JOB


def test_spawn_passes_detach_flags_on_windows_and_new_session_on_posix(paths, monkeypatch):
    captured = {}

    class _FakePopen:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            self.pid = 31337

    monkeypatch.setattr(host.os, "name", "posix")
    host.spawn_host(paths, ["powershell.exe"], str(paths.dir), popen=_FakePopen)
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["close_fds"] is True
    assert "creationflags" not in captured["kwargs"]
    assert "terminal_mcp.windows_session_host" in captured["command"]

    monkeypatch.setattr(host.os, "name", "nt")
    host.spawn_host(paths, ["powershell.exe"], str(paths.dir), popen=_FakePopen)
    assert captured["kwargs"]["creationflags"] == host.WINDOWS_DETACH_FLAGS


def test_spawn_never_gives_the_host_an_inherited_pipe(paths, monkeypatch):
    """An inherited pipe handle keeps the agent's objects referenced and gives
    the host a reason to die when the agent's handles close -- re-coupling the
    two lifetimes through the back door."""
    captured = {}

    class _FakePopen:
        def __init__(self, command, **kwargs):
            captured.update(kwargs)
            self.pid = 1

    host.spawn_host(paths, ["x"], str(paths.dir), popen=_FakePopen)
    assert captured["stdin"] == subprocess.DEVNULL
    assert captured["stdout"] is not subprocess.PIPE
    assert captured["stderr"] is not subprocess.PIPE


def test_a_spawn_failure_is_reported_not_swallowed(paths):
    def _boom(command, **kwargs):
        raise OSError("no such executable")

    with pytest.raises(host.HostError):
        host.spawn_host(paths, ["x"], str(paths.dir), popen=_boom)


def test_breakaway_denied_is_its_own_error_type():
    """An operator must be able to tell "the job forbids breakaway" from any
    other spawn failure -- the fix is to re-register the Scheduled Task, which
    no generic error would suggest."""
    assert issubclass(host.BreakawayDenied, host.HostError)


# == input channel =======================================================

def test_input_to_a_session_whose_directory_is_gone_fails(tmp_path):
    """A removed directory means the session is gone. write_input must not
    recreate it -- that would turn "no such session" into a write that reports
    success and is read by nobody."""
    p = SessionPaths(tmp_path / "root", "vanished")
    start = time.monotonic()
    assert host.write_input(p, "hello\n") is False
    assert not p.dir.exists(), "a failed write must not create the session"
    assert time.monotonic() - start < 2.0, "a dead session must not stall the agent"


def test_input_round_trips_through_the_spool_by_offset(paths):
    paths.input.touch()
    assert host.write_input(paths, "first\n") is True
    text, offset = host.read_input(paths)
    assert text == "first\n"
    assert host.write_input(paths, "second\n") is True
    more, _ = host.read_input(paths, offset=offset)
    assert more == "second\n", "an offset read must not replay consumed input"


def test_interleaved_writers_do_not_corrupt_the_input_spool(paths):
    paths.input.touch()
    for n in range(50):
        assert host.write_input(paths, f"cmd-{n:03d}\n") is True
    text, _ = host.read_input(paths)
    assert text.splitlines() == [f"cmd-{n:03d}" for n in range(50)]


def test_writing_empty_input_is_a_no_op(paths):
    assert host.write_input(paths, "") is True
    assert not paths.input.exists()


def test_control_request_on_an_unwritable_dir_is_false(tmp_path):
    p = SessionPaths(tmp_path / "ro", "s")
    p.ensure()
    p.dir.chmod(0o500)
    try:
        assert host.request_control(p, {"op": "resize"}) is False
    finally:
        p.dir.chmod(0o700)


def test_the_metadata_temp_name_is_unique_per_writer(paths):
    """The heartbeat thread and the resize path both publish metadata. Sharing
    one temp filename lets two writers truncate and write it concurrently, and a
    shorter payload landing over a longer one leaves trailing bytes that are
    then atomically installed -- a record that looks committed and is not.

    Asserted structurally because it is not reliably reproducible: a small
    payload usually reaches the file in a single write() syscall, so the race
    exists but rarely fires. That is a reason to make it impossible, not a
    reason to trust it. The load test below pins the observable property; this
    pins the mechanism that guarantees it."""
    import threading

    seen = []
    real_replace = os.replace

    def _spy(src, dst):
        seen.append(pathlib.Path(str(src)).name)
        return real_replace(src, dst)

    import pathlib

    threads = []
    for _ in range(4):
        threads.append(threading.Thread(
            target=lambda: host.write_meta_atomic(paths, _meta())))
    original = host.os.replace
    host.os.replace = _spy
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
    finally:
        host.os.replace = original

    assert len(seen) == 4
    assert len(set(seen)) == 4, f"writers shared a temp filename: {seen}"


def test_concurrent_metadata_writers_never_produce_a_corrupt_record(paths):
    """The observable property, under real concurrent load: a reader must always
    get a complete, valid record or nothing -- never a half or a blend of two."""
    import threading

    stop = threading.Event()
    failures = []

    def _writer(rows):
        while not stop.is_set():
            host.write_meta_atomic(paths, _meta(rows=rows, cols=rows * 2))

    def _reader():
        while not stop.is_set():
            if paths.meta.exists():
                meta = host.read_meta(paths)
                if meta is None:
                    failures.append("read a corrupt or partial record")
                elif meta.cols != meta.rows * 2:
                    failures.append(f"mixed two records: {meta.rows}/{meta.cols}")

    threads = [threading.Thread(target=_writer, args=(r,), daemon=True)
               for r in (24, 60, 120)] + [threading.Thread(target=_reader, daemon=True)]
    for thread in threads:
        thread.start()
    time.sleep(1.5)
    stop.set()
    for thread in threads:
        thread.join(timeout=5)

    assert failures == [], failures[:5]
    assert not [f for f in paths.dir.iterdir() if f.name.startswith(".meta.json")], \
        "every writer must clean up its own temp file"
