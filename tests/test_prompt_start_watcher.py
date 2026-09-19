import json
import threading

import pytest

from terminal_mcp import prompt_start_watcher as psw
from terminal_mcp.prompt_start_watcher import PromptStartWatcher, load_watcher_config
from terminal_mcp.submit_watchdog import SubmissionStore


class FakeApp:
    """Stands in for TerminalService.

    The submission store is the REAL one (on tmp_path), because the durable
    compare-and-increment in `SubmissionStore.reserve_enter` is precisely the
    thing these tests are about; only the tmux-touching recovery is faked.
    """

    def __init__(self, store: SubmissionStore) -> None:
        self.submissions = store
        self.recovered: list[str] = []
        self.sweeper_stopped = False

    def recover_submission(self, record) -> None:
        self.recovered.append(record.submission_id)

    def stop_submission_sweeper(self) -> None:
        self.sweeper_stopped = True


@pytest.fixture
def watcher_env(tmp_path, monkeypatch):
    """A watcher wired to a real store and a fake controller."""
    store = SubmissionStore(tmp_path / "prompt_submissions.db")
    app = FakeApp(store)
    monkeypatch.setattr(psw, "load_config", lambda *a, **k: object())
    monkeypatch.setattr(psw, "TerminalService", lambda _cfg: app)
    return app, store, tmp_path


def _write_config(tmp_path, **values):
    path = tmp_path / "watcher.json"
    path.write_text(json.dumps(values))
    return path


def _submission(store, *, key="k1", session="codex1", agent_type="codex", enters=0):
    record, _ = store.create(idempotency_key=key, session=session,
                             agent_type=agent_type, prompt="do the thing")
    for _ in range(enters):
        assert store.reserve_enter(record.submission_id, cap=psw.MAX_ENTERS_CAP,
                                   action="test_seed") is not None
    return store.get(record.submission_id)


def _run(tmp_path, config_path):
    return PromptStartWatcher(config_path=config_path,
                              state_path=tmp_path / "state.json").run_once()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def test_config_defaults_and_bounds(tmp_path):
    path = tmp_path / "watcher.json"
    path.write_text(json.dumps({"max_enters": 99, "interval_seconds": 1,
                                "include_sessions": ["codex1"]}))
    cfg = load_watcher_config(path)
    assert cfg["max_enters"] == 6
    assert cfg["interval_seconds"] == 3
    assert cfg["include_sessions"] == ["codex1"]


def test_default_max_enters_is_the_one_advertised_retry_cap():
    """The watcher, the shared watchdog and the server-level MCP instructions
    must all be bounded by the same six -- not three independent sixes that can
    drift apart."""
    from terminal_mcp.orchestration_policy import PROMPT_RETRY_CAP
    from terminal_mcp.submit_watchdog import WatchdogConfig
    assert psw.DEFAULTS["max_enters"] == PROMPT_RETRY_CAP
    assert WatchdogConfig().max_total_enters == PROMPT_RETRY_CAP
    assert load_watcher_config("/nonexistent/watcher.json")["max_enters"] == PROMPT_RETRY_CAP


def test_disabled_cycle_is_read_only(tmp_path):
    config = tmp_path / "watcher.json"
    config.write_text(json.dumps({"enabled": False}))
    state = tmp_path / "state.json"
    result = PromptStartWatcher(config_path=config, state_path=state).run_once()
    assert result["enabled"] is False
    assert result["tracked_count"] == 0
    assert json.loads(state.read_text())["enabled"] is False


# ---------------------------------------------------------------------------
# The Enter budget is real, not decoration
# ---------------------------------------------------------------------------

def test_a_submission_under_the_cap_is_recovered(watcher_env):
    app, store, tmp_path = watcher_env
    record = _submission(store, enters=1)
    result = _run(tmp_path, _write_config(tmp_path))
    assert app.recovered == [record.submission_id]
    assert result["tracked_count"] == 1
    assert result["capped_count"] == 0


def test_a_submission_at_the_cap_is_not_handed_to_recovery(watcher_env):
    """`max_enters` used to be loaded, clamped -- and then never read, so the
    watcher had no budget of its own at all."""
    app, store, tmp_path = watcher_env
    _submission(store, enters=psw.MAX_ENTERS_CAP)
    result = _run(tmp_path, _write_config(tmp_path))
    assert app.recovered == []
    assert result["capped_count"] == 1
    assert result["tracked_count"] == 1


def test_a_lowered_max_enters_actually_lowers_the_budget(watcher_env):
    app, store, tmp_path = watcher_env
    _submission(store, enters=2)
    result = _run(tmp_path, _write_config(tmp_path, max_enters=2))
    assert app.recovered == []
    assert result["capped_count"] == 1
    assert result["max_enters"] == 2


def test_a_capped_submission_is_left_for_the_controller_to_conclude(watcher_env):
    """The watcher declines to send more Enter; it does not also decide the
    record is terminal.  That transition belongs to the controller's own
    cap/TTL logic, which already owns it."""
    _app, store, tmp_path = watcher_env
    record = _submission(store, enters=psw.MAX_ENTERS_CAP)
    _run(tmp_path, _write_config(tmp_path))
    after = store.get(record.submission_id)
    assert after.ack_state == record.ack_state
    assert after.enter_count == psw.MAX_ENTERS_CAP


def test_session_allowlist_and_denylist_filter_before_recovery(watcher_env):
    app, store, tmp_path = watcher_env
    wanted = _submission(store, key="a", session="codex1")
    _submission(store, key="b", session="codex2")
    _run(tmp_path, _write_config(tmp_path, include_sessions=["codex1"]))
    assert app.recovered == [wanted.submission_id]

    app.recovered.clear()
    _run(tmp_path, _write_config(tmp_path, exclude_sessions=["codex1", "codex2"]))
    assert app.recovered == []


# ---------------------------------------------------------------------------
# One cycle at a time, and one state file
# ---------------------------------------------------------------------------

def test_a_second_concurrent_cycle_is_skipped_not_queued(watcher_env):
    """The timer fires every 10s; a slow cycle must not stack up behind
    itself and turn recovery into a polling storm."""
    import fcntl
    app, store, tmp_path = watcher_env
    _submission(store, enters=0)
    watcher = PromptStartWatcher(config_path=_write_config(tmp_path),
                                 state_path=tmp_path / "state.json")
    watcher.lock_path.parent.mkdir(parents=True, exist_ok=True)
    with watcher.lock_path.open("w") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = watcher.run_once()
    assert result["skipped"] == "cycle_already_running"
    assert app.recovered == []


def test_the_lock_follows_the_state_path(tmp_path):
    """A redirected state directory gets its own mutex instead of silently
    contending on the real controller's."""
    watcher = PromptStartWatcher(config_path=tmp_path / "w.json",
                                 state_path=tmp_path / "state.json")
    assert watcher.lock_path == tmp_path / "state.lock"
    assert PromptStartWatcher().lock_path == psw.LOCK_PATH


def test_state_file_is_valid_json_and_carries_the_status_contract(watcher_env):
    _app, store, tmp_path = watcher_env
    _submission(store, enters=1)
    result = _run(tmp_path, _write_config(tmp_path))
    state = json.loads((tmp_path / "state.json").read_text())
    assert state == result
    for key in ("enabled", "tracked_count", "started_count", "waiting_approval_count",
                "stuck_count", "capped_count", "max_enters", "recoveries", "last_run_at"):
        assert key in state
    assert not list(tmp_path.glob("state.tmp")), "state write must land atomically"


# ---------------------------------------------------------------------------
# The actual duplicate-prompt risk: two writers, one submission
# ---------------------------------------------------------------------------

def test_two_concurrent_writers_cannot_exceed_the_shared_enter_cap(tmp_path):
    """The real duplicate-Enter path.

    The timer-driven watcher cycle and the in-controller SubmissionSweeper are
    separate writers against the same durable row, and the flock only
    serializes watcher cycles against each other -- it does nothing about the
    sweeper running inside the controller process.  What actually makes a
    duplicate impossible is `reserve_enter`'s single compare-and-increment
    UPDATE, and that is what this exercises: many threads, one row, and the
    cap must hold exactly.
    """
    store = SubmissionStore(tmp_path / "prompt_submissions.db")
    record, _ = store.create(idempotency_key="shared", session="codex1",
                             agent_type="codex", prompt="only once")
    cap = psw.MAX_ENTERS_CAP
    granted: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def writer(name: str) -> None:
        start.wait()
        for _ in range(cap + 2):
            reserved = store.reserve_enter(record.submission_id, cap=cap, action=name)
            if reserved is not None:
                with lock:
                    granted.append(name)

    threads = [threading.Thread(target=writer, args=(f"writer{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(granted) == cap, f"expected exactly {cap} reservations, got {len(granted)}"
    assert store.get(record.submission_id).enter_count == cap
    # And no further Enter is ever grantable once the budget is spent.
    assert store.reserve_enter(record.submission_id, cap=cap, action="late") is None


def test_the_prompt_text_is_never_reinjected_by_a_reservation(tmp_path):
    """A reservation only ever buys an Enter.  The stored prompt is what proves
    a retry must not re-type it, so it must survive every reservation
    unchanged -- same text, same digest, same idempotency key."""
    store = SubmissionStore(tmp_path / "prompt_submissions.db")
    record, created = store.create(idempotency_key="k", session="codex1",
                                   agent_type="codex", prompt="rm -rf nothing")
    assert created is True
    for _ in range(psw.MAX_ENTERS_CAP):
        store.reserve_enter(record.submission_id, cap=psw.MAX_ENTERS_CAP, action="enter")
    after = store.get(record.submission_id)
    assert after.prompt == record.prompt
    assert after.prompt_sha256 == record.prompt_sha256
    assert after.idempotency_key == record.idempotency_key


def test_replaying_the_same_idempotency_key_reuses_the_one_submission(tmp_path):
    """A watcher cycle that races the controller's own submit must not be able
    to create a second row for the same request_key."""
    store = SubmissionStore(tmp_path / "prompt_submissions.db")
    first, created_first = store.create(idempotency_key="same", session="codex1",
                                        agent_type="codex", prompt="p")
    second, created_second = store.create(idempotency_key="same", session="codex1",
                                          agent_type="codex", prompt="p")
    assert created_first is True and created_second is False
    assert first.submission_id == second.submission_id
    with pytest.raises(ValueError, match="IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PROMPT"):
        store.create(idempotency_key="same", session="codex1", agent_type="codex",
                     prompt="a different prompt")


def test_the_watcher_always_stops_the_sweeper_it_started(watcher_env):
    """The cycle must not leave a background sweeper thread behind in a
    oneshot process that systemd is about to consider finished."""
    app, store, tmp_path = watcher_env
    _submission(store)
    _run(tmp_path, _write_config(tmp_path))
    assert app.sweeper_stopped is True
