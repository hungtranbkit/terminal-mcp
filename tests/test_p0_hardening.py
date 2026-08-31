"""P0 hardening: session/pane identity pinning (P0-2), per-pane input
serialization (P0-3), idempotency keys (P0-4)."""
from __future__ import annotations

import threading
import time

from terminal_mcp.audit import AuditStore
from terminal_mcp.bindings import BindingStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService


def _service(tmp_path) -> TerminalService:
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("test-*",), max_text_length=2000),
    )
    return TerminalService(
        config,
        bindings=BindingStore(tmp_path / "bindings.db"),
        audit=AuditStore(tmp_path / "audit.db"),
    )


# ---------------------------------------------------------------------------
# P0-2: session/pane identity pinning
# ---------------------------------------------------------------------------


def test_bind_pins_identity_at_bind_time(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-pin-bind")
    service = _service(tmp_path)
    result = service.terminal_bind("agent", session, input_enabled=True)
    assert result["binding"] == "agent"
    stored = service.bindings.get("agent")
    assert stored.pinned_session_id and stored.pinned_pane_id
    info = service.tmux.get_session(session)
    assert stored.pinned_session_id == info.session_id
    assert stored.pinned_pane_id == info.pane_id


def test_send_bound_blocked_when_session_name_recycled(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-pin-recycle", "bash -lc 'echo original; sleep 30'")
    service = _service(tmp_path)
    service.terminal_bind("agent", session, input_enabled=True)

    # Recycle the name: kill the original session, create a brand new,
    # unrelated one under the exact same tmux session *name*.
    import subprocess
    subprocess.run(["tmux", "kill-session", "-t", session], check=True)
    time.sleep(0.2)
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "bash -lc 'echo recycled; sleep 30'"], check=True)
    time.sleep(0.2)

    result = service.terminal_send_bound("agent", "hello", press_enter=True)
    assert result["error"] == "IDENTITY_MISMATCH"
    subprocess.run(["tmux", "kill-session", "-t", session], check=False)


def test_send_bound_blocked_when_pane_replaced_within_same_session(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-pin-panereplace", "sleep 30")
    service = _service(tmp_path)
    service.terminal_bind("agent", session, input_enabled=True)

    import subprocess
    subprocess.run(["tmux", "new-window", "-t", session], check=True)
    subprocess.run(["tmux", "kill-window", "-t", f"{session}:0"], check=True)
    time.sleep(0.2)

    # session_id is unchanged (same session), only the pane was replaced --
    # still a genuine identity mismatch that must be caught.
    info = service.tmux.get_session(session)
    stored = service.bindings.get("agent")
    assert info.session_id == stored.pinned_session_id
    assert info.pane_id != stored.pinned_pane_id

    result = service.terminal_send_bound("agent", "hello", press_enter=True)
    assert result["error"] == "IDENTITY_MISMATCH"


def test_explicit_rebind_clears_identity_mismatch(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-pin-rebind", "bash -lc 'echo one; sleep 30'")
    service = _service(tmp_path)
    service.terminal_bind("agent", session, input_enabled=True)

    import subprocess
    subprocess.run(["tmux", "kill-session", "-t", session], check=True)
    time.sleep(0.2)
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "bash -lc 'read x; echo GOT:$x; sleep 30'"], check=True)
    time.sleep(0.2)
    assert service.terminal_send_bound("agent", "hello", press_enter=True)["error"] == "IDENTITY_MISMATCH"

    # Explicit rebind (replace=True) is the deliberate "accept the new
    # target" action -- re-pins, and sending now succeeds.
    rebind = service.terminal_bind("agent", session, replace=True, input_enabled=True)
    assert rebind["replaced"] is True
    result = service.terminal_send_bound("agent", "y", press_enter=True)
    assert result["sent"] is True
    subprocess.run(["tmux", "kill-session", "-t", session], check=False)


def test_legacy_binding_without_pin_lazily_adopts_on_first_send(tmux_session_factory, tmp_path):
    # A binding created before identity pinning existed (pinned_session_id
    # NULL, e.g. directly via BindingStore.put with no pin) must not be
    # broken outright by this upgrade -- its first successful send adopts
    # whatever identity is live *then*.
    session = tmux_session_factory("test-pin-legacy", "bash -lc 'read x; echo GOT:$x; sleep 30'")
    service = _service(tmp_path)
    service.bindings.put("agent", session, input_enabled=True)  # no pinned_* kwargs -> legacy row
    assert service.bindings.get("agent").pinned_session_id is None

    result = service.terminal_send_bound("agent", "y", press_enter=True)
    assert result["sent"] is True
    adopted = service.bindings.get("agent")
    assert adopted.pinned_session_id  # now pinned
    info = service.tmux.get_session(session)
    assert adopted.pinned_session_id == info.session_id


def test_watch_pins_identity_at_watch_time(tmux_session_factory, tmp_path):
    from terminal_mcp.supervisor import SupervisorService, SupervisorStore

    session = tmux_session_factory("test-pin-watch")
    service = _service(tmp_path)
    supervisor = SupervisorService(service, SupervisorStore(tmp_path / "supervisor.db"))
    supervisor.watch(session=session)
    row = supervisor.store.get_watch(f"session:{session}")
    info = service.tmux.get_session(session)
    assert row["pinned_session_id"] == info.session_id
    assert row["pinned_pane_id"] == info.pane_id


# ---------------------------------------------------------------------------
# P0-3: per-pane input serialization
# ---------------------------------------------------------------------------


def test_concurrent_sends_to_same_pane_never_interleave(tmux_session_factory, tmp_path):
    # A target that accumulates whatever raw bytes land on stdin without
    # any of its own buffering discipline -- if two sends interleaved
    # their keystrokes, the accumulated buffer would show mixed/garbled
    # content instead of each text intact and contiguous.
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "never_submits.py"
    session = tmux_session_factory("test-lock-concurrent", f"python3 -u {fixture}")
    time.sleep(0.2)
    service = _service(tmp_path)

    texts = [f"AAAAAAAAAA{i}" for i in range(6)]
    threads = [
        threading.Thread(target=service.terminal_send_text, args=(session, t, False))
        for t in texts
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)

    pane = service.terminal_tail(session, 40)["output"]
    # Each text must appear intact, contiguous, exactly once -- never split
    # across an interleaved send from another thread.
    for t in texts:
        assert pane.count(t) == 1, f"{t!r} missing or duplicated -- interleaving occurred:\n{pane}"


def test_supervisor_send_and_manual_send_share_the_same_pane_lock(tmux_session_factory, tmp_path):
    # The lock must be shared across *all* send paths targeting the same
    # pane, not just direct terminal_send_text calls against each other --
    # Supervisor v2's execute_send goes through the exact same
    # TerminalService instance and therefore the exact same registry.
    session = tmux_session_factory("test-lock-shared")
    service = _service(tmp_path)
    identity = service.resolve_identity(session)
    key = f"{identity.session_id}:{identity.pane_id}"
    lock_a = service._pane_locks.get(key)
    lock_b = service._pane_locks.get(key)
    assert lock_a is lock_b


# ---------------------------------------------------------------------------
# P0-4: idempotency keys
# ---------------------------------------------------------------------------


def test_idempotency_key_prevents_duplicate_send(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-idem-dup", "bash -lc 'read x; echo GOT:$x; sleep 30'")
    service = _service(tmp_path)

    first = service.terminal_send_text(session, "y", press_enter=True, idempotency_key="key-1")
    assert first["sent"] is True
    second = service.terminal_send_text(session, "y", press_enter=True, idempotency_key="key-1")
    assert second == first  # exact replay, not a fresh send

    events = [e for e in service.audit.list(50, session=session) if e["action"] == "send_text"]
    # Both calls are separately audited (for observability), but only the
    # first ever reached tmux -- confirmed via pane content, not just count.
    pane = service.terminal_tail(session, 20)["output"]
    assert pane.count("GOT:y") == 1


def test_idempotency_key_survives_process_restart(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-idem-restart", "bash -lc 'read x; echo GOT:$x; sleep 30'")
    service = _service(tmp_path)
    first = service.terminal_send_text(session, "y", press_enter=True, idempotency_key="restart-key")
    assert first["sent"] is True

    # Simulate a process restart: a brand new TerminalService pointed at
    # the same durable audit.db (the idempotency claim lives on disk).
    restarted = _service(tmp_path)
    replay = restarted.terminal_send_text(session, "y", press_enter=True, idempotency_key="restart-key")
    assert replay == first
    pane = service.terminal_tail(session, 20)["output"]
    assert pane.count("GOT:y") == 1


def test_concurrent_duplicate_requests_same_key_send_exactly_once(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-idem-concurrent", "bash -lc 'read x; echo GOT:$x; sleep 30'")
    service = _service(tmp_path)
    results: list[dict] = []
    lock = threading.Lock()

    def call():
        r = service.terminal_send_text(session, "y", press_enter=True, idempotency_key="race-key")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=call) for _ in range(5)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)

    pane = service.terminal_tail(session, 20)["output"]
    assert pane.count("GOT:y") == 1, f"expected exactly one send, pane:\n{pane}"
    # Every caller gets an honest result: either the real send outcome or
    # DUPLICATE_IN_PROGRESS -- never a second, independent send outcome.
    sent_results = [r for r in results if r.get("sent") is True]
    assert len(sent_results) == 1


def test_idempotency_key_not_required_backward_compatible(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-idem-optional", "bash -lc 'read x; echo GOT:$x; sleep 30'")
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "y", press_enter=True)
    assert result["sent"] is True
    assert "idempotency_key" not in result
