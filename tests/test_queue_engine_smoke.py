"""Supervisor Queue v2 Phase 2 -- production-like smoke test (task's own
required acceptance items A-D), using REAL disposable tmux sessions, a
REAL TerminalService/ControllerService (so REAL idempotent-send dedup,
REAL git evidence collection, REAL classify_status), and the harmless
fake-worker technique below -- NOT the real `window`/`window2` sessions
on dell-5530, and NOT a real Claude/Codex agent.

WHY DISPOSABLE LOCAL SESSIONS RATHER THAN DELL-5530 (disclosed scope
decision): the task text itself explicitly allows this ("nếu hai session
đang bận project thật, tạo disposable sessions... trên Windows node...
không inject task test vào OfflinePOS đang chạy"). `window`/`window2`
were mid-task at the time this was written. Disposable sessions on THIS
(local Linux) node exercise the exact same ControllerService/NodeClient/
TerminalService code paths a remote-node session would (this project's
own architecture makes the local node's transport merely the "local"
special case of the same NodeClient protocol -- see controller.py's own
docstring) -- the one thing NOT exercised here is the actual HTTP hop to
a remote node-agent, which the rest of this codebase's own multi-node
test suite already covers independently. Faster, safer, and zero risk
to a real project.

HARMLESS FAKE WORKER: each disposable session runs a small shell one-
liner (see the `rig` fixture's own make_worker helper for the exact
command and why it's shaped the way it is) that reads one line, waits
briefly, echoes it, then execs into `cat` for everything after --
`cat`/the echoed line reproduce every line of stdin back to the pane
verbatim. Since the dispatch text this feature sends already contains,
as part of its own short completion-marker instruction (queue_engine.
build_dispatch_text), the EXACT marker line with the real task_id/
attempt/nonce already substituted in, echoing it back is a real,
working, zero-risk stand-in for "an agent that did the work and then
printed the marker" -- this is not a mocked completion signal; it goes
through the real send -> real pane -> real capture -> real regex-parse
-> real nonce-verify pipeline end to end.

SAFETY: every session/repo here is a disposable tmp_path fixture. This
file NEVER references `window`/`window2`, and does not enable
auto_dispatch on any lane (ticks are always driven explicitly, exactly
matching the "no automatic background loop is wired yet" phase 2
constraint).

NOT RUN BY DEFAULT (`pytest.mark.queue_smoke`, excluded in pyproject.toml's
own addopts, same convention as the existing live_cli/real_network/
real_ssh markers): classify_status (status.py, pre-existing, unmodified)
only leaves its own RUNNING classification once a session's tmux
activity age exceeds 60 real seconds OR its foreground command visibly
changes -- a deliberate, existing "genuine quiet window before treating
something as done" design this feature's own completion detection
reuses as-is rather than second-guessing. The harmless fake worker
below is built to change its own foreground command (not just go
quiet) specifically so this smoke test does not have to wait a real 60
seconds per task -- but SOME real wall-clock waiting is unavoidable
here (this file takes on the order of a minute to run, not
milliseconds), which is exactly why it is not part of the default fast
suite. Run explicitly: `pytest -m queue_smoke tests/test_queue_engine_smoke.py -v`."""
from __future__ import annotations

import subprocess
import time

import pytest

pytestmark = pytest.mark.queue_smoke

from terminal_mcp.audit import AuditStore
from terminal_mcp.bindings import BindingStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.controller import build_default_controller
from terminal_mcp.coordinator import CoordinatorGate
from terminal_mcp.core import TerminalService
from terminal_mcp.queue_engine import QueueEngine
from terminal_mcp.queue_store import BLOCKED, COMPLETED, QUEUED, QueueStore, RUNNING


def _service(tmp_path) -> TerminalService:
    config = AppConfig(
        PermissionsConfig(True, True), ("queue-smoke-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("queue-smoke-*",), max_text_length=4000),
    )
    return TerminalService(
        config, bindings=BindingStore(tmp_path / "bindings.db"), audit=AuditStore(tmp_path / "audit.db"),
    )


def _init_real_git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)
    return path


@pytest.fixture
def rig(tmp_path, tmux_session_factory):
    """One real TerminalService/ControllerService/QueueStore/
    CoordinatorGate/QueueEngine, plus a helper to spin up a disposable
    delayed-echo worker session inside its own real, clean git repo."""
    service = _service(tmp_path)
    controller = build_default_controller(service)
    # The "local" node's status is DERIVED from heartbeat freshness (see
    # node_registry.py's own docstring: "status is NEVER persisted --
    # always DERIVED"), not simply "always online" -- without at least
    # one heartbeat, resolve_session's own online-node filter skips it
    # entirely and every routed call (terminal_status/terminal_send_
    # text/terminal_tail) comes back SESSION_NOT_FOUND even for a
    # real, existing session. Same one-line fix doctor.py's own
    # cmd_grants/cmd_conversations already need and use.
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=(), agent_version=None)
    store = QueueStore(tmp_path / "queue.db")
    coordinator = CoordinatorGate()  # real git evidence collector, real sensitive-pattern screen
    engine = QueueEngine(store, controller, coordinator=coordinator, lease_seconds=300)

    def make_worker(name: str) -> str:
        repo = _init_real_git_repo(tmp_path / f"repo-{name}")
        # See this module's own "HARMLESS FAKE WORKER" docstring section.
        # Two real, disclosed constraints this specific command satisfies:
        # (1) a literal multi-line tmux send-keys write lands (and,
        #     against a plain line-buffered `cat`, gets fully echoed)
        #     BEFORE this project's own send-confirmation logic takes its
        #     "pre-Enter" reference snapshot -- a real Claude/Codex Ink
        #     composer buffers multi-line paste input specially and does
        #     not have this issue (already extensively tested elsewhere in
        #     this codebase -- see adapters.py's own ClaudeAdapter
        #     docstring), but GenericShellAdapter (selected for any plain,
        #     non-agent command, exactly what this disposable worker is)
        #     uses a simple before/after-Enter diff that a bare `cat`
        #     defeats by echoing everything too early -- reading and
        #     delaying the FIRST line specifically fixes this: the
        #     observable echo lands during the real post-Enter poll
        #     window, not before it.
        # (2) classify_status's own ACTIVE_COMMANDS treats a bare "bash"
        #     foreground command as RUNNING for up to 60s of tmux
        #     activity age regardless of real progress -- `exec cat`
        #     immediately after that first delayed line makes the pane's
        #     foreground command "cat" (not in ACTIVE_COMMANDS), so this
        #     project's own classify_status correctly and promptly
        #     reports it as no-longer-RUNNING once the worker is done
        #     with its one deliberate delay, exactly like a real one-shot
        #     command completing would.
        tmux_session_factory(
            name, f"bash -c 'cd {repo} && IFS= read -r first_line && sleep 0.15 && echo \"$first_line\" && exec cat'")
        return str(repo)

    return {"service": service, "controller": controller, "store": store, "engine": engine,
           "make_worker": make_worker}


def _reheartbeat(controller):
    """The "local" node's status is DERIVED from heartbeat freshness and
    degrades after node_models.HeartbeatThresholds' own default 60s (see
    the `rig` fixture's own comment on this) -- any driving loop that
    might run longer than that in real wall-clock time must keep
    refreshing it, or every routed call starts failing SESSION_NOT_FOUND
    partway through for a session that is very much still there."""
    if controller is not None:
        controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=(), agent_version=None)


def _run_task_to_completion(engine, session, *, deadline_seconds=90, poll_interval=1.0, controller=None):
    """Drives tick() until the lane's current active task (if any)
    reaches a stable, non-transient outcome -- COMPLETED, BLOCKED, or
    PAUSED -- or deadline_seconds of real wall-clock time elapses. A
    wall-clock deadline (not a fixed tick count) because reaching
    VERIFYING from RUNNING genuinely depends on classify_status's own
    60-second quiet-window threshold in the worst case."""
    deadline = time.monotonic() + deadline_seconds
    last = None
    while time.monotonic() < deadline:
        _reheartbeat(controller)
        last = engine.tick(session)
        if last.action in ("COMPLETED", "BLOCKED", "PAUSED", "IDLE"):
            return last
        time.sleep(poll_interval)
    return last


def _drive_concurrently(engine, sessions, *, deadline_seconds=90, poll_interval=0.5, stop_when=None, controller=None):
    """Ticks EVERY session in `sessions` once per round, interleaved, for
    up to deadline_seconds -- proves genuine per-session-lane parallelism
    (each lane advances on its OWN schedule, sharing only this loop's
    wall-clock budget, not a global serialized queue) while also sharing
    ONE real-time wait across every lane instead of paying it once per
    lane sequentially. `stop_when(store)` (optional) ends the loop early
    once it returns True."""
    deadline = time.monotonic() + deadline_seconds
    results = {}
    while time.monotonic() < deadline:
        _reheartbeat(controller)
        for session in sessions:
            results[session] = engine.tick(session)
        if stop_when is not None and stop_when():
            break
        time.sleep(poll_interval)
    return results


# ---------------------------------------------------------------------------
# Item B: two independent queues, strict per-lane ordering, real completion.
# ---------------------------------------------------------------------------

def test_two_queues_run_independently_and_task2_never_dispatches_before_task1_completes(rig):
    rig["make_worker"]("queue-smoke-a")
    rig["make_worker"]("queue-smoke-b")
    store = rig["store"]
    engine = rig["engine"]

    ids_a = store.set_tasks("queue-smoke-a", [
        {"prompt": "print marker A1 please"}, {"prompt": "print marker A2 please"},
    ])
    ids_b = store.set_tasks("queue-smoke-b", [{"prompt": "print marker B1 please"}])

    # Drive BOTH lanes CONCURRENTLY (interleaved ticks, one shared
    # wall-clock wait) -- proves genuine per-lane parallelism, not
    # accidental serialization through a shared global queue, while
    # never paying the ~60s quiet-window wait twice.
    seen_a_task2_early = False

    def check_ordering():
        nonlocal seen_a_task2_early
        if store.get_task(ids_a[1]).status != QUEUED:
            seen_a_task2_early = True
        return (store.get_task(ids_a[0]).status == COMPLETED
                and store.get_task(ids_b[0]).status == COMPLETED)

    _drive_concurrently(engine, ["queue-smoke-a", "queue-smoke-b"], controller=rig["controller"],
                       deadline_seconds=90, poll_interval=0.5, stop_when=check_ordering)

    assert not seen_a_task2_early, "lane A's task 2 dispatched/moved before task 1 reached COMPLETED"
    assert store.get_task(ids_a[0]).status == COMPLETED
    assert store.get_task(ids_a[0]).verification_evidence  # real evidence, not just the label
    assert store.get_task(ids_b[0]).status == COMPLETED  # lane B progressed independently, in parallel

    # NOW (only after task 1 completed) task 2 becomes claimable in lane A.
    engine.tick("queue-smoke-a")  # CLAIMED
    assert store.get_task(ids_a[1]).status == "PRECHECK"


# ---------------------------------------------------------------------------
# Item B: artificial blocker -> BLOCKED, never stops the OTHER queue.
# ---------------------------------------------------------------------------

def test_artificial_blocker_blocks_only_its_own_queue(rig):
    rig["make_worker"]("queue-smoke-a")
    rig["make_worker"]("queue-smoke-b")
    store = rig["store"]
    engine = rig["engine"]

    (blocked_id,) = store.set_tasks("queue-smoke-a", [
        {"prompt": "this one has an artificial test blocker", "metadata": {"artificial_blocker": "simulated CI outage"}},
    ])
    (healthy_id,) = store.set_tasks("queue-smoke-b", [{"prompt": "a perfectly normal harmless task"}])

    result_a = None
    for _ in range(5):
        result_a = engine.tick("queue-smoke-a")
        if result_a.action == "COORDINATOR_BLOCKED":
            break
    assert result_a.action == "COORDINATOR_BLOCKED"
    assert store.get_task(blocked_id).status == BLOCKED
    assert store.lane_status("queue-smoke-a")["paused"] is False  # BLOCKED stops the TASK; lane isn't separately paused

    # Lane B proceeds completely normally.
    result_b = _run_task_to_completion(engine, "queue-smoke-b", controller=rig["controller"])
    assert store.get_task(healthy_id).status == COMPLETED


# ---------------------------------------------------------------------------
# Item B: restart mid-dispatch -- no duplicate real keystroke.
# ---------------------------------------------------------------------------

def test_restart_mid_dispatch_never_duplicates_the_real_send(rig, tmp_path):
    repo = rig["make_worker"]("queue-smoke-a")
    store = rig["store"]
    controller = rig["controller"]
    engine1 = rig["engine"]

    (task_id,) = store.set_tasks("queue-smoke-a", [{"prompt": "print marker RESTART-TEST please"}])
    engine1.tick("queue-smoke-a")  # CLAIMED
    engine1.tick("queue-smoke-a")  # COORDINATOR_READY
    dispatch_result = engine1.tick("queue-smoke-a")  # DISPATCHED -- a REAL terminal_send_text happened
    assert dispatch_result.action == "DISPATCHED"
    assert store.get_task(task_id).status == RUNNING
    time.sleep(0.3)

    # Baseline occurrence count after the FIRST real send -- a real
    # terminal's own local echo (what was typed, shown as it's typed)
    # PLUS the worker's own separate echo means a single logical send
    # legitimately appears MORE than once in the pane; the point of this
    # test is that this count never grows from a SECOND logical send,
    # not that it equals exactly 1.
    baseline_capture = controller.terminal_tail("queue-smoke-a", 500)
    baseline_occurrences = baseline_capture["output"].count("print marker RESTART-TEST please")
    assert baseline_occurrences >= 1

    # Simulate "the engine process crashed right after the real send
    # went through, before it recorded RUNNING" -- force the row back to
    # DISPATCHING with an expired lease (the one state no public API can
    # reach directly from RUNNING -- see test_queue_engine.py's own
    # identical technique for why raw SQL is the honest way to simulate
    # this).
    with store._connection() as connection:
        connection.execute(
            "UPDATE queue_tasks SET status = 'DISPATCHING', lease_expires_at = '2000-01-01T00:00:00Z' "
            "WHERE id = ?", (task_id,))

    # A brand NEW QueueStore + QueueEngine instance, same db file and
    # same real ControllerService/TerminalService (same in-memory
    # idempotent_sends store this process holds, exactly as a
    # restarted terminal-mcp process would rebuild from the SAME durable
    # audit.db on disk) -- simulating "Terminal MCP restarted".
    store2 = QueueStore(tmp_path / "queue.db")
    engine2 = QueueEngine(store2, controller, coordinator=CoordinatorGate())
    reconciled = store2.reconcile_stale_claims("queue-smoke-a")
    assert task_id in reconciled

    engine2.tick("queue-smoke-a")  # CLAIMED again
    engine2.tick("queue-smoke-a")  # COORDINATOR_READY again
    redispatch_result = engine2.tick("queue-smoke-a")  # re-dispatch attempt, SAME idempotency_key
    assert redispatch_result.action == "DISPATCHED"
    time.sleep(0.3)

    # The real, decisive proof: the occurrence count after the RECLAIMED
    # re-dispatch is UNCHANGED from the baseline taken right after the
    # first send -- core.py's own idempotent_sends store deduped the
    # second attempt (returned the original result instead of actually
    # sending again), so no NEW occurrence of the prompt text was ever
    # written to the pane.
    capture = controller.terminal_tail("queue-smoke-a", 500)
    occurrences = capture["output"].count("print marker RESTART-TEST please")
    assert occurrences == baseline_occurrences, (
        f"expected the same occurrence count as the first send ({baseline_occurrences}), "
        f"found {occurrences} after the reclaimed re-dispatch -- a real second send happened"
    )


# ---------------------------------------------------------------------------
# Item B: stale/duplicate enqueue never runs twice.
# ---------------------------------------------------------------------------

def test_stale_duplicate_queue_set_never_runs_the_superseded_batch(rig):
    """Simulates ChatGPT being unsure whether its first terminal_queue_set
    call went through and calling it again with an equivalent task list
    before anything was ever dispatched -- replace_pending=True (the
    default) must supersede the FIRST batch entirely; only the SECOND
    (latest) batch may ever actually run."""
    rig["make_worker"]("queue-smoke-a")
    store = rig["store"]
    engine = rig["engine"]

    first_ids = store.set_tasks("queue-smoke-a", [{"prompt": "stale batch task"}])
    second_ids = store.set_tasks("queue-smoke-a", [{"prompt": "print marker LATEST please"}])

    assert store.get_task(first_ids[0]).status == "CANCELLED"  # superseded, never runs

    result = _run_task_to_completion(engine, "queue-smoke-a", controller=rig["controller"])
    assert store.get_task(second_ids[0]).status == COMPLETED
    assert store.get_task(first_ids[0]).status == "CANCELLED"  # still never ran
