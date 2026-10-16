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
    assert result['status'] == 'FAILED'
    assert result['error'] == 'DIRECT_START_NOT_RUNNING'
    assert result['submission_id'] == 'sub-1'
    assert len(controller.send_calls) == 1
    assert tools.monotonic() <= 1.01


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


def test_remote_guard_refuses_before_any_http_request(monkeypatch):
    from terminal_mcp.node_client import RemoteNodeClient
    client = RemoteNodeClient("http://node.test:8790", "inert-test-token")
    monkeypatch.setattr(client, '_request', lambda *a, **kw: pytest.fail('must not send HTTP'))
    result = client.send_text('codex', 'inert', True, require_idle=True)
    assert result['error'] == 'IDLE_GUARD_UNSUPPORTED'
    assert result['sent'] is False
