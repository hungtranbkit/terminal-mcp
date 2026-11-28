"""Idle Codex starts directly even when legacy queue submission is enabled."""

import pytest

from terminal_mcp.audit import AuditStore
from tests.test_compact_tools import service


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_ENABLE_QUEUE", "1")
    tools, terminal, controller = service(tmp_path / 'journal.db')
    terminal.audit = AuditStore(tmp_path / 'audit.db')
    clock = [0.0]
    tools.monotonic = lambda: clock[0]
    tools.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    controller.statuses['codex'] = [
        {'state': 'IDLE', 'agent_type': 'codex', 'exists': True},
        {'state': 'RUNNING', 'agent_type': 'codex', 'exists': True},
    ]
    controller.send_result = {'delivery_state': 'SUBMIT_CONFIRMED', 'enter_sent': True,
                              'agent_type': 'codex', 'enter_count': 1, 'submission_id': 'sub-1'}
    tools.handlers['enqueue_task'] = lambda *a, **kw: pytest.fail('direct start must never enqueue')
    tools.handlers['legacy_task_by_request_key'] = lambda key: None
    return tools, terminal, controller


def start(tools, **kwargs):
    return tools.turn(action='start', target='codex', text='inert task', timeout=1,
                      request_key='one-request', **kwargs)


def test_idle_codex_starts_without_queue_and_observes_running(rig):
    tools, _, controller = rig
    result = start(tools)
    assert result['status'] == 'TASK_STARTED'
    assert result['mode'] == 'direct'
    assert result['task_state'] == 'RUNNING'
    assert result['submission_id'] == 'sub-1'
    assert len(controller.send_calls) == 1


def test_direct_codex_does_not_require_enabling_legacy_queue(rig, monkeypatch):
    monkeypatch.delenv('TERMINAL_MCP_ENABLE_QUEUE')
    assert start(rig[0])['status'] == 'TASK_STARTED'


@pytest.mark.parametrize('state', ['RUNNING', 'WAITING_INPUT', 'UNKNOWN'])
def test_nonidle_codex_fails_without_input_or_queue(rig, state):
    tools, _, controller = rig
    controller.statuses['codex'] = {'state': state, 'agent_type': 'codex'}
    result = start(tools)
    assert result['status'] == 'FAILED'
    assert result['error'] == 'DIRECT_TARGET_NOT_IDLE'
    assert controller.send_calls == []


def test_acceptance_without_running_fails_bounded_without_resending(rig):
    tools, _, controller = rig
    controller.statuses['codex'] = {'state': 'IDLE', 'agent_type': 'codex'}
    result = start(tools)
    assert result['status'] == 'TASK_DISPATCHED'
    assert result['error'] == 'EXECUTION_UNVERIFIED'
    assert result['dispatched'] is True
    assert result['dispatch_state'] == 'CONFIRMED'
    assert result['task_state'] == 'START_UNVERIFIED'
    assert result['delivery_state'] == 'SUBMIT_CONFIRMED'
    assert result['submission_id'] == 'sub-1'
    assert len(controller.send_calls) == 1
    assert tools.monotonic() <= 1.01


@pytest.mark.parametrize('target', ['codex', 'node/codex'])
def test_slow_healthy_preflight_is_persisted_and_uses_one_deadline(rig, target, monkeypatch):
    import hashlib

    tools, terminal, controller = rig
    request_key = 'slow-preflight'
    cache_key = 'codex-direct-probe:' + hashlib.sha256(request_key.encode()).hexdigest()
    clock = [0.0]
    tools.monotonic = lambda: clock[0]
    tools.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    controller.statuses[target] = [
        {'state': 'IDLE', 'agent_type': 'codex', 'exists': True, 'allowed': True},
        {'state': 'RUNNING', 'agent_type': 'codex', 'exists': True, 'allowed': True},
    ]
    persisted_before_probe = []
    phase_budgets = []

    def bounded_read(_key, read, seconds):
        phase_budgets.append(seconds)
        persisted_before_probe.append(terminal.audit.get_idempotent_result(cache_key))
        if seconds < 2.5:
            clock[0] += seconds
            return {'error': 'STATUS_PROBE_TIMEOUT'}
        clock[0] += 2.5
        return read()

    monkeypatch.setattr(tools, '_bounded_read', bounded_read)
    send_timeouts = []
    sends = []

    def confirmed_send(_target, _text, *, timeout, idempotency_key, require_idle):
        send_timeouts.append(timeout)
        sends.append((idempotency_key, require_idle))
        clock[0] += 0.1
        return {'status': 'SUBMIT_CONFIRMED', 'submission_id': 'sub-slow',
                'evidence': {'delivery_state': 'SUBMIT_CONFIRMED'}}

    monkeypatch.setattr(tools, 'send_task', confirmed_send)
    result = tools.turn(action='start', target=target, text='inert task', timeout=10,
                        request_key=request_key)

    assert persisted_before_probe[0] is not None
    assert persisted_before_probe[0]['result']['task_state'] == 'PREFLIGHT_PENDING'
    assert phase_budgets[0] >= 2.5
    assert result['status'] == 'TASK_STARTED'
    assert result['dispatched'] is True
    assert result['task_state'] == 'RUNNING'
    assert result['submission_id'] == 'sub-slow'
    assert send_timeouts == [pytest.approx(7.5)]
    assert len(sends) == 1 and sends[0][1] is True


def test_confirmed_send_timeout_is_truthfully_dispatched_and_same_key_is_not_replayed(rig, monkeypatch):
    tools, terminal, controller = rig
    clock = [0.0]
    tools.monotonic = lambda: clock[0]
    tools.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    controller.statuses['codex'] = {'state': 'IDLE', 'agent_type': 'codex', 'exists': True}
    sends = []
    probes = []

    def bounded_read(_key, read, seconds):
        probes.append(seconds)
        if len(probes) == 1:
            clock[0] += 0.1
            return read()
        clock[0] = 2.0
        return {'error': 'STATUS_PROBE_TIMEOUT'}

    monkeypatch.setattr(tools, '_bounded_read', bounded_read)

    def confirmed_send(_target, _text, *, timeout, idempotency_key, require_idle):
        sends.append((timeout, idempotency_key, require_idle))
        clock[0] += 0.1
        return {'status': 'SUBMIT_CONFIRMED', 'submission_id': 'sub-confirmed',
                'evidence': {'delivery_state': 'SUBMIT_CONFIRMED'}}

    monkeypatch.setattr(tools, 'send_task', confirmed_send)
    first = tools.turn(action='start', target='codex', text='inert task', timeout=2,
                       request_key='post-send-timeout')
    assert first['status'] == 'TASK_DISPATCHED'
    assert first['error'] == 'EXECUTION_UNVERIFIED'
    assert first['dispatched'] is True
    assert first['task_state'] == 'START_UNVERIFIED'
    assert first['delivery_state'] == 'SUBMIT_CONFIRMED'
    assert first['submission_id'] == 'sub-confirmed'

    replay = tools.turn(action='start', target='codex', text='inert task', timeout=2,
                        request_key='post-send-timeout')
    assert replay == first
    assert len(sends) == 1
    assert len(probes) == 2


def test_slow_send_exhausts_overall_deadline_as_ambiguous_without_replay(rig, monkeypatch):
    import threading

    tools, _, controller = rig
    controller.statuses['codex'] = {'state': 'IDLE', 'agent_type': 'codex', 'exists': True}
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    sends = []

    def slow_send(_target, _text, *, timeout, idempotency_key, require_idle):
        sends.append((timeout, idempotency_key, require_idle))
        entered.set()
        assert release.wait(2)
        try:
            return {'status': 'SUBMIT_CONFIRMED', 'submission_id': 'sub-late',
                    'evidence': {'delivery_state': 'SUBMIT_CONFIRMED'}}
        finally:
            finished.set()

    monkeypatch.setattr(tools, 'send_task', slow_send)
    first = tools.turn(action='start', target='codex', text='inert task', timeout=.05,
                       request_key='slow-send')
    assert entered.is_set()
    assert first['status'] == 'TASK_SUBMIT_UNCERTAIN'
    assert first['dispatch_state'] == 'AMBIGUOUS'
    assert first['dispatched'] is None
    release.set()
    assert finished.wait(1)
    replay = tools.turn(action='start', target='codex', text='inert task', timeout=.05,
                        request_key='slow-send')
    assert replay == first
    assert len(sends) == 1


@pytest.mark.parametrize('blocked', [
    {'state': 'IDLE', 'agent_type': 'codex', 'allowed': False, 'exists': True},
    {'state': 'WAITING_INPUT', 'agent_type': 'codex', 'input_required': True, 'exists': True},
    {'error': 'NODE_UNREACHABLE', 'reason': 'node offline'},
])
def test_preflight_permission_input_or_offline_denial_is_persisted_without_send(rig, blocked):
    import hashlib

    tools, terminal, controller = rig
    request_key = 'blocked-preflight'
    cache_key = 'codex-direct-probe:' + hashlib.sha256(request_key.encode()).hexdigest()
    controller.statuses['codex'] = blocked
    result = tools.turn(action='start', target='codex', text='inert task', timeout=1,
                        request_key=request_key)
    assert result['dispatched'] is False
    assert controller.send_calls == []
    assert terminal.audit.get_idempotent_result(cache_key) is not None


def test_fresh_send_guard_rejects_target_that_became_busy_after_idle_observation(rig, monkeypatch):
    tools, _, controller = rig
    controller.statuses['codex'] = {'state': 'IDLE', 'agent_type': 'codex', 'exists': True}
    guarded = []

    def became_busy(_target, _text, *, timeout, idempotency_key, require_idle):
        guarded.append((timeout, idempotency_key, require_idle))
        return {'status': 'BLOCKED', 'error': 'DIRECT_TARGET_NOT_IDLE',
                'evidence': {'delivery_state': 'BLOCKED'}}

    monkeypatch.setattr(tools, 'send_task', became_busy)
    result = start(tools)
    assert result['status'] == 'FAILED'
    assert result['dispatched'] is False
    assert result['delivery_state'] == 'BLOCKED'
    assert len(guarded) == 1 and guarded[0][2] is True
    assert controller.send_calls == []


def test_concurrent_same_key_preflight_has_one_probe_and_one_send(rig, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    tools, _, controller = rig
    entered = threading.Event()
    release = threading.Event()
    probe_calls = []

    def bounded_read(_key, read, _seconds):
        probe_calls.append(1)
        entered.set()
        assert release.wait(2)
        return read()

    monkeypatch.setattr(tools, '_bounded_read', bounded_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(start, tools)
        assert entered.wait(2)
        concurrent = pool.submit(start, tools).result(timeout=2)
        probes_while_owner_blocked = len(probe_calls)
        release.set()
        completed = first.result(timeout=2)

    assert concurrent['error'] == 'DIRECT_DISPATCH_IN_PROGRESS'
    assert completed['status'] == 'TASK_STARTED'
    assert probes_while_owner_blocked == 1
    assert len(probe_calls) == 2  # one preflight and one post-send verification
    assert len(controller.send_calls) == 1


def test_stale_idle_observation_is_not_reused_after_an_inflight_probe(rig, monkeypatch):
    tools, _, controller = rig
    controller.statuses['codex'] = {'state': 'IDLE', 'agent_type': 'codex'}
    observed = []

    def bounded_read(_key, _read, seconds):
        observed.append(seconds)
        # Model CompactTerminalTools' existing rule: an older in-flight
        # observation cannot be reused as fresh IDLE for this request.
        return {'error': 'STATUS_PROBE_TIMEOUT'}

    monkeypatch.setattr(tools, '_bounded_read', bounded_read)
    result = start(tools)
    assert result['status'] == 'FAILED'
    assert result['error'] == 'STATUS_PROBE_TIMEOUT'
    assert result['dispatched'] is False
    assert observed == [pytest.approx(1)]
    assert controller.send_calls == []


def test_preflight_failure_receipt_survives_restart_without_a_second_probe(rig, monkeypatch):
    from terminal_mcp.compact_tools import CompactTerminalTools

    tools, terminal, controller = rig
    probes = []
    monkeypatch.setattr(tools, '_bounded_read', lambda *_: probes.append(1) or
                        {'error': 'STATUS_PROBE_TIMEOUT'})
    first = start(tools)
    restarted = CompactTerminalTools(terminal, controller, handlers=tools.handlers)
    replay = start(restarted)
    assert first == replay
    assert len(probes) == 1
    assert controller.send_calls == []


def test_failed_enter_never_claims_running_even_if_status_says_running(rig):
    tools, _, controller = rig
    controller.send_result = {'delivery_state': 'TEXT_SENT', 'enter_sent': False,
                              'agent_type': 'codex', 'submission_id': 'sub-1'}
    result = start(tools)
    assert result['error'] == 'DIRECT_SUBMIT_NOT_ACCEPTED'
    assert result['dispatched'] is False
    assert len(controller.send_calls) == 1


def test_legacy_request_is_not_replayed_or_migrated(rig):
    tools, _, controller = rig
    tools.handlers['legacy_task_by_request_key'] = lambda key: {'id': 'legacy-1', 'status': 'QUEUED'}
    result = start(tools)
    assert result['error'] == 'EXISTING_QUEUE_TASK'
    assert result['task_id'] == 'legacy-1'
    assert controller.send_calls == []


def test_replay_across_restart_returns_original_receipt_without_retyping(rig):
    from terminal_mcp.compact_tools import CompactTerminalTools
    tools, terminal, controller = rig
    first = start(tools)
    restarted = CompactTerminalTools(terminal, controller, handlers=tools.handlers)
    assert start(restarted) == first
    assert len(controller.send_calls) == 1


@pytest.mark.parametrize("prior", [
    {"status": "TASK_STARTED", "dispatched": True,
     "delivery_state": "SUBMIT_CONFIRMED", "task_state": "RUNNING"},
    {"status": "TASK_DISPATCHED", "dispatched": True,
     "delivery_state": "SUBMIT_CONFIRMED", "task_state": "START_UNVERIFIED"},
    {"status": "TASK_SUBMIT_UNCERTAIN", "dispatched": None,
     "delivery_state": "SUBMIT_UNKNOWN", "task_state": "SUBMIT_UNVERIFIED"},
    {"status": "FAILED", "dispatched": False,
     "delivery_state": "NOT_SUBMITTED", "task_state": "PREFLIGHT_PENDING"},
])
def test_prior_version_direct_receipt_is_read_before_any_new_probe(rig, monkeypatch, prior):
    import hashlib
    import json

    tools, terminal, controller = rig
    request_key = "legacy-direct-receipt"
    cache_key = "codex-direct-start:" + hashlib.sha256(request_key.encode()).hexdigest()
    digest = hashlib.sha256(json.dumps(["codex", "inert task"], ensure_ascii=False).encode()).hexdigest()
    assert terminal.audit.claim_idempotency_key(cache_key)
    terminal.audit.store_idempotent_result(cache_key, {
        "request_digest": digest,
        "result": {"action": "start", "mode": "direct", "session": "codex",
                   "request_key": request_key, "idempotency_key": cache_key, **prior},
    })
    probes = []
    monkeypatch.setattr(tools, "_bounded_read", lambda *_: probes.append(1) or
                        {"error": "STATUS_PROBE_TIMEOUT"})

    result = tools.turn(action="start", target="codex", text="inert task", timeout=1,
                        request_key=request_key)

    assert result["status"] == prior["status"]
    assert result["task_state"] == prior["task_state"]
    assert result["dispatched"] == prior["dispatched"]
    assert probes == []
    assert controller.send_calls == []
    assert terminal.audit.get_idempotent_result("codex-direct-probe:" +
        hashlib.sha256(request_key.encode()).hexdigest()) is None


@pytest.mark.parametrize(("target", "text"), [
    ("codex-renamed", "inert task"),
    ("shell", "inert task"),
    ("codex", "changed prompt"),
])
def test_prior_version_direct_receipt_changed_digest_conflicts_before_probe(rig, monkeypatch, target, text):
    import hashlib
    import json

    tools, terminal, controller = rig
    request_key = "legacy-target-conflict"
    cache_key = "codex-direct-start:" + hashlib.sha256(request_key.encode()).hexdigest()
    digest = hashlib.sha256(json.dumps(["codex", "inert task"], ensure_ascii=False).encode()).hexdigest()
    assert terminal.audit.claim_idempotency_key(cache_key)
    terminal.audit.store_idempotent_result(cache_key, {
        "request_digest": digest,
        "result": {"status": "TASK_DISPATCHED", "dispatched": True,
                   "delivery_state": "SUBMIT_CONFIRMED", "task_state": "START_UNVERIFIED"},
    })
    probes = []
    monkeypatch.setattr(tools, "_bounded_read", lambda *_: probes.append(1) or
                        {"error": "STATUS_PROBE_TIMEOUT"})

    result = tools.turn(action="start", target=target, text=text, timeout=1,
                        request_key=request_key)

    assert result["error"] == "IDEMPOTENCY_CONFLICT"
    assert probes == []
    assert controller.send_calls == []
    assert terminal.audit.get_idempotent_result("codex-direct-probe:" +
        hashlib.sha256(request_key.encode()).hexdigest()) is None


def test_post_submit_verification_exception_keeps_confirmed_start_unverified(rig, monkeypatch):
    tools, _, controller = rig
    calls = []

    def read(_key, operation, _seconds):
        calls.append(1)
        if len(calls) == 1:
            return operation()
        raise RuntimeError("verification transport failed after confirmed send")

    monkeypatch.setattr(tools, "_bounded_read", read)
    result = start(tools)

    assert result["status"] == "TASK_DISPATCHED"
    assert result["dispatched"] is True
    assert result["delivery_state"] == "SUBMIT_CONFIRMED"
    assert result["task_state"] == "START_UNVERIFIED"
    assert result["execution_state"] == "UNVERIFIED"
    assert result["error"] == "EXECUTION_UNVERIFIED"
    assert len(controller.send_calls) == 1


def test_same_key_different_prompt_fails_without_input(rig):
    tools, _, controller = rig
    start(tools)
    result = tools.turn(action='start', target='codex', text='different task',
                        request_key='one-request', timeout=1)
    assert result['error'] == 'IDEMPOTENCY_CONFLICT'
    assert len(controller.send_calls) == 1


def test_idle_only_guard_is_forwarded_to_mutation(rig):
    tools, _, controller = rig
    start(tools)
    assert controller.send_calls[0][-1]['require_idle'] is True


def test_crash_after_input_leaves_durable_no_replay_receipt(rig, monkeypatch):
    tools, _, controller = rig
    original = controller.terminal_send_text
    def interrupted(*a, **kw):
        original(*a, **kw)
        raise KeyboardInterrupt('simulated process loss after injection')
    monkeypatch.setattr(controller, 'terminal_send_text', interrupted)
    with pytest.raises(KeyboardInterrupt):
        start(tools)
    result = start(tools)
    assert result['error'] == 'DIRECT_DISPATCH_IN_PROGRESS'
    assert len(controller.send_calls) == 1


def test_concurrent_distinct_starts_recheck_idle_under_the_pane_lease(tmp_path, monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from tests.test_codex_paste_settle import ComposerTransport, submit_service

    service = submit_service(tmp_path, ComposerTransport([]))
    state = {'state': 'IDLE', 'exists': True}
    injected = []
    monkeypatch.setattr(service, '_status_payload', lambda session: dict(state))
    def activate(session, text, press_enter, **kwargs):
        injected.append(text)
        time.sleep(.03)
        state['state'] = 'RUNNING'
        return {'sent': True, 'enter_sent': True, 'delivery_state': 'SUBMIT_CONFIRMED'}
    monkeypatch.setattr(service, '_send_text_and_verify_locked', activate)
    def send(key):
        return service.terminal_send_text('test-paste', key, press_enter=True,
                                         idempotency_key=key, require_idle=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, ['first', 'second']))
    assert len(injected) == 1
    assert sum(r.get('error') == 'DIRECT_TARGET_NOT_IDLE' for r in results) == 1


def test_stalled_status_probe_has_a_bound_and_never_sends(rig, monkeypatch):
    import threading
    import time
    tools, _, controller = rig
    tools.monotonic, tools.sleep = time.monotonic, time.sleep
    release = threading.Event()
    monkeypatch.setattr(controller, 'terminal_status_bounded', lambda *a: release.wait(2))
    try:
        began = time.monotonic()
        result = tools.turn(action='start', target='codex', text='inert', timeout=.03)
        assert result['error'] == 'STATUS_PROBE_TIMEOUT'
        assert time.monotonic() - began < .5
        assert controller.send_calls == []
    finally:
        release.set()


def test_real_disposable_tmux_direct_start_and_replay(tmp_path, tmux_session_factory):
    import hashlib
    import shlex
    import time
    from pathlib import Path

    from terminal_mcp.compact_tools import CompactTerminalTools
    from terminal_mcp.tmux import TmuxClient
    from tests.test_codex_paste_settle import PROMPT, submit_service

    fixture = Path(__file__).parent / 'fixtures' / 'codex_bracketed_paste.py'
    command = f'exec -a codex python3 -u {shlex.quote(str(fixture))}'
    session = tmux_session_factory('test-direct-start', f'bash -c {shlex.quote(command)}')
    terminal = submit_service(tmp_path, TmuxClient())
    limit = time.monotonic() + 3
    while 'codex fixture' not in '\n'.join(terminal.tmux.capture_lines(session, 30)):
        assert time.monotonic() < limit
        time.sleep(.02)
    tools = CompactTerminalTools(terminal, terminal)
    tools.handlers['enqueue_task'] = lambda *a, **kw: pytest.fail('must not enqueue')
    result = tools.turn(action='start', target=session, text=PROMPT, request_key='live-once', timeout=2)
    assert result['status'] == 'TASK_STARTED', repr(dict(result))
    assert result['task_state'] == 'RUNNING'
    assert result['send']['evidence']['enter_count'] == 1
    replay = CompactTerminalTools(terminal, terminal).turn(
        action='start', target=session, text=PROMPT, request_key='live-once', timeout=2)
    assert replay == result
    pane = '\n'.join(terminal.tmux.capture_lines(session, 30))
    assert 'SUBMITTED[1]' in pane and 'SUBMITTED[2]' not in pane
    assert hashlib.sha256(PROMPT.encode()).hexdigest() in pane


def test_remote_guard_uses_a_guarded_endpoint_with_the_idempotency_key(monkeypatch):
    from terminal_mcp.node_client import RemoteNodeClient
    client = RemoteNodeClient("http://node.test:8790", "inert-test-token")
    calls = []
    monkeypatch.setattr(client, '_request', lambda *a, **kw: calls.append((a, kw)) or {"sent": True})
    result = client.send_text('codex', 'inert', True, require_idle=True, idempotency_key='once')
    assert result == {"sent": True}
    assert calls[0][0][:2] == ('POST', '/v1/sessions/codex/send-guarded')
    assert calls[0][1]['body']['idempotency_key'] == 'once'


def test_remote_unguarded_send_keeps_the_legacy_endpoint(monkeypatch):
    from terminal_mcp.node_client import RemoteNodeClient
    client = RemoteNodeClient("http://node.test:8790", "inert-test-token")
    calls = []
    monkeypatch.setattr(client, '_request', lambda *a, **kw: calls.append((a, kw)) or {"sent": True})
    client.send_text('codex', 'inert', True, require_idle=False)
    assert calls[0][0][:2] == ('POST', '/v1/sessions/codex/send')


def test_explicit_idempotency_key_takes_precedence_over_legacy_request_key(rig):
    tools, _, controller = rig
    first = start(tools, idempotency_key='explicit')
    replay = tools.turn(action='start', target='codex', text='inert task', timeout=1,
                        request_key='changed-legacy', idempotency_key='explicit')
    assert replay == first
    assert len(controller.send_calls) == 1
