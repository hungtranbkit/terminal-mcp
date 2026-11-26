"""Pinned local-terminal HTTP/WS contracts; no real shell or PTY is created."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from terminal_mcp.config import AppConfig, DashboardConfig, PermissionsConfig, SessionLifecycleConfig, SessionAccessConfig
from terminal_mcp.controller import ControllerService
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.node_client import LocalNodeClient
from terminal_mcp.node_registry import NodeRegistry


@pytest.fixture
def local_app(tmp_path, monkeypatch):
    service = TerminalService(AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=(),
        dashboard=DashboardConfig(web_terminal_enabled=True),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),)),
    ))
    controller = ControllerService(NodeRegistry(tmp_path / 'nodes.db'),
        local_node_id='hp-linux', local_client=LocalNodeClient(service))
    monkeypatch.setattr(service.tmux, 'list_sessions', lambda: [])
    monkeypatch.setattr(service.tmux, 'get_session', lambda _: SimpleNamespace(
        name='local-term-test', session_id='$42', pane_id='%4', pane_pid=42, created_epoch=123,
        pane_current_command='bash', attached=False))
    monkeypatch.setattr(controller, 'resolve_session', lambda _: {'error': 'SESSION_NOT_FOUND'})
    calls = []
    def create(name, agent_type, cwd, **kwargs):
        calls.append((name, agent_type, kwargs))
        return {'session': name, 'agent_type': agent_type, 'node_id': 'hp-linux',
                'session_id': '$42', 'created_epoch': 123, 'state': 'READY'}
    monkeypatch.setattr(controller, 'terminal_create_session', create)
    monkeypatch.setattr(service, 'terminal_web_terminal_access', lambda _: {'exists': True, 'input': True})
    monkeypatch.setattr('terminal_mcp.dashboard.WebTerminalProcess',
        lambda *a, **k: pytest.fail('must not spawn a real PTY'))
    server = build_mcp(service)
    register_dashboard(server, service, controller=controller)
    client = TestClient(server.streamable_http_app(), headers={'Origin': 'http://testserver'})
    client.get('/dashboard/api/nodes')
    return client, controller, service, calls


def payload(**overrides):
    return dict(name='local-term-test', agent_type='shell', node='hp-linux',
                expected_local_node='hp-linux', **overrides)


@pytest.mark.parametrize('field,value', [('node', 'auto'), ('node', 'worker'),
    ('expected_local_node', 'worker'), ('agent_type', 'codex')])
def test_pinned_create_rejects_wrong_identity_before_creation(local_app, field, value):
    client, _, _, calls = local_app
    body = payload(); body[field] = value
    r = client.post('/dashboard/api/session/create', json=body)
    assert r.status_code == 409
    assert r.json()['error'] == 'LOCAL_NODE_IDENTITY_MISMATCH'
    assert not calls


def test_pinned_create_receipt_keeps_default_grant_and_instance(local_app):
    client, _, _, calls = local_app
    r = client.post('/dashboard/api/session/create', json=payload())
    assert r.status_code == 200
    assert calls[0][2]['grant_mode'] == 'none'
    assert r.json()['terminal_access'] == {'exists': True, 'input': True}
    assert r.json()['terminal_instance'] == '$42@123'


@pytest.mark.parametrize('error', ['AMBIGUOUS_SESSION', 'NODE_UNREACHABLE'])
def test_pinned_create_does_not_ignore_ambiguous_or_unknown_existing_name(local_app, monkeypatch, error):
    client, controller, _, calls = local_app
    monkeypatch.setattr(controller, 'resolve_session', lambda _: {'error': error})
    r = client.post('/dashboard/api/session/create', json=payload())
    assert r.status_code == 409
    assert r.json()['error'] == error
    assert not calls


def test_pinned_create_does_not_borrow_existing_session(local_app, monkeypatch):
    client, controller, _, calls = local_app
    monkeypatch.setattr(controller, 'resolve_session', lambda _: {'node_id': 'hp-linux'})
    r = client.post('/dashboard/api/session/create', json=payload())
    assert r.status_code == 409
    assert r.json()['error'] == 'SESSION_ALREADY_EXISTS'
    assert not calls


@pytest.mark.parametrize('access', [{'error': 'ACCESS_DENIED'}, {'exists': True, 'input': False}])
def test_create_reports_actual_access_without_upgrading(local_app, monkeypatch, access):
    client, _, service, _ = local_app
    monkeypatch.setattr(service, 'terminal_web_terminal_access', lambda _: access)
    assert client.post('/dashboard/api/session/create', json=payload()).json()['terminal_access'] == access


@pytest.mark.parametrize('origin', ['', 'https://evil.invalid'])
def test_create_origin_guard_precedes_creation(local_app, origin):
    client, _, _, calls = local_app
    assert client.post('/dashboard/api/session/create', json=payload(), headers={'Origin': origin}).status_code == 403
    assert not calls


@pytest.mark.parametrize('node,instance', [('worker', '$42@123'), ('hp-linux', '$99@123'),
    ('hp-linux', '$42@124'), ('', '$42@123')])
def test_ws_expected_identity_refused_before_pty(local_app, monkeypatch, node, instance):
    client, controller, _, _ = local_app
    monkeypatch.setattr(controller, 'resolve_session', lambda _: {'node_id': 'hp-linux'})
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect('/dashboard/ws/terminal?session=local-term-test'
                f'&expected_node={node}&expected_instance={instance}'):
            pytest.fail('identity mismatch accepted')
    assert exc.value.code == 4409


@pytest.mark.parametrize('flag', ['lifecycle', 'webterm'])
def test_disabled_local_creation_has_no_side_effect(local_app, flag):
    client, _, service, calls = local_app
    if flag == 'lifecycle':
        service.config = replace(service.config, session_lifecycle=replace(service.config.session_lifecycle, enabled=False))
    else:
        service.config = replace(service.config, dashboard=replace(service.config.dashboard, web_terminal_enabled=False))
    assert client.post('/dashboard/api/session/create', json=payload()).status_code == 403
    assert not calls


def test_endpoint_replaced_or_draining_refused(local_app, monkeypatch):
    client, controller, _, calls = local_app
    node = controller.registry.get('hp-linux')
    monkeypatch.setattr(controller.registry, 'get', lambda _: replace(node, endpoint='http://other'))
    assert client.post('/dashboard/api/session/create', json=payload()).status_code == 409
    monkeypatch.setattr(controller.registry, 'get', lambda _: replace(node, draining=True))
    assert client.post('/dashboard/api/session/create', json=payload()).status_code == 409
    assert not calls


def test_create_and_ws_require_access_identity(local_app):
    client, _, service, calls = local_app
    service.config = replace(service.config, dashboard=replace(service.config.dashboard,
        cloudflare_access_team_domain='fixture.cloudflareaccess.com', cloudflare_access_audience='fixture'))
    assert client.post('/dashboard/api/session/create', json=payload()).status_code == 403
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect('/dashboard/ws/terminal?session=local-term-test&expected_node=hp-linux'):
            pytest.fail('missing identity accepted')
    assert exc.value.code == 4401
    assert not calls


def test_verified_request_identity_is_forwarded_to_create_audit(local_app, monkeypatch):
    from terminal_mcp.cf_access import AccessIdentity
    client, _, service, calls = local_app
    service.config = replace(service.config, dashboard=replace(service.config.dashboard,
        cloudflare_access_team_domain='fixture.cloudflareaccess.com', cloudflare_access_audience='fixture'))
    monkeypatch.setattr('terminal_mcp.dashboard.verify_access_assertion', lambda *a, **k:
        AccessIdentity(email='owner@example.test', subject='fixture-owner', raw_claims={}))
    assert client.post('/dashboard/api/session/create', json=payload(grant_mode='read_send'),
        headers={'Cf-Access-Jwt-Assertion': 'fixture-only'}).status_code == 200
    assert calls[0][2]['requested_by'] == 'owner@example.test'
    assert calls[0][2]['grant_mode'] == 'read_send'


@pytest.mark.parametrize('case,code', [('no-grant', 4403), ('disabled', 4403),
    ('missing', 4404), ('ambiguous', 4409), ('origin', 4403)])
def test_ws_policy_guards_still_apply_to_pinned_identity(local_app, monkeypatch, case, code):
    client, controller, service, _ = local_app
    service.config = replace(service.config, session_access=SessionAccessConfig(default_read=False, default_input=False))
    monkeypatch.setattr(service, 'terminal_web_terminal_access', TerminalService.terminal_web_terminal_access.__get__(service))
    resolution = {'node_id': 'hp-linux'}
    if case == 'missing': resolution = {'error': 'SESSION_NOT_FOUND'}
    if case == 'ambiguous': resolution = {'error': 'AMBIGUOUS_SESSION'}
    if case == 'disabled': service.config = replace(service.config, dashboard=replace(service.config.dashboard, web_terminal_enabled=False))
    monkeypatch.setattr(controller, 'resolve_session', lambda _: resolution)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect('/dashboard/ws/terminal?session=local-term-test&expected_node=hp-linux&expected_instance=$42@123',
                headers={'Origin': 'https://evil.invalid'} if case == 'origin' else {}):
            pytest.fail('blocked socket accepted')
    assert exc.value.code == code


@pytest.mark.parametrize('input_enabled', [False, True])
def test_valid_pin_attaches_unique_id_and_preserves_readonly_takeover(local_app, monkeypatch, input_enabled):
    client, controller, service, _ = local_app
    monkeypatch.setattr(controller, 'resolve_session', lambda _: {'node_id': 'hp-linux'})
    monkeypatch.setattr(service, 'terminal_web_terminal_access', lambda _: {'exists': True, 'input': input_enabled})
    calls, closed = [], []
    def proc(binary, target, **kw):
        calls.append((target, kw)); return SimpleNamespace(close=lambda: closed.append(True))
    async def pump(ws, proc):
        await ws.receive()
    monkeypatch.setattr('terminal_mcp.dashboard.WebTerminalProcess', proc)
    monkeypatch.setattr('terminal_mcp.dashboard.pump_websocket', pump)
    with client.websocket_connect('/dashboard/ws/terminal?session=local-term-test&expected_node=hp-linux&expected_instance=$42@123&takeover=1') as ws:
        assert ws.receive_json()['readonly'] is (not input_enabled)
        ws.send_text('fixture detach')
    assert calls == [('$42', {'readonly': not input_enabled, 'takeover': input_enabled})]
    assert closed == [True]


def test_existing_shell_cwd_sensitive_capacity_and_cleanup_policy(local_app, monkeypatch, tmp_path):
    _, _, service, _ = local_app
    from terminal_mcp.lifecycle import resolve_cwd
    assert service.lifecycle.validate_create('local-term-safe', 'shell') is None
    assert service.lifecycle.validate_create('ssh-secret', 'shell')['error'] == 'SENSITIVE_SESSION_NOT_CREATABLE'
    assert resolve_cwd(str(tmp_path), service.config)[1] is None
    assert resolve_cwd('/', service.config)[1]['error'] == 'CWD_NOT_ALLOWED'
    monkeypatch.setattr(service, '_session_capacity', lambda: (1, 1))
    monkeypatch.setattr(service, '_maintenance_session_count', lambda: 0)
    monkeypatch.setattr(service, '_reclaim_idle_sessions', lambda _: {'reclaimed': []})
    assert service.terminal_create_session('local-term-safe', 'shell')['error'] == 'NODE_AT_SESSION_CAPACITY'
    assert service.lifecycle.delete('terminal-mcp', protected_sessions=('terminal-mcp',))['error'] == 'SESSION_PROTECTED'


def test_actual_grants_preserve_readonly_disabled_and_stale_identity(local_app, monkeypatch):
    _, _, service, _ = local_app
    service.config = replace(service.config, session_access=SessionAccessConfig(default_read=False, default_input=False))
    access = lambda: TerminalService.terminal_web_terminal_access(service, 'local-term-test')
    assert access()['error'] == 'ACCESS_DENIED'
    assert 'error' not in service.grant_session_read('local-term-test', True, granted_by='fixture-owner')
    assert access()['input'] is False
    assert 'error' not in service.grant_session_input('local-term-test', True, granted_by='fixture-owner')
    assert access()['input'] is True
    service.config = replace(service.config, permissions=PermissionsConfig(True, False))
    assert access()['input'] is False
    service.config = replace(service.config, permissions=PermissionsConfig(True, True))
    monkeypatch.setattr(service.tmux, 'get_session', lambda _: SimpleNamespace(
        name='local-term-test', session_id='$43', pane_id='%5', pane_pid=43, created_epoch=124,
        pane_current_command='bash', attached=False))
    assert access()['input'] is False


def test_local_create_receipt_refuses_session_replaced_during_create(local_app, monkeypatch):
    client, _, service, _ = local_app
    monkeypatch.setattr(service, 'resolve_identity', lambda _: None)
    r = client.post('/dashboard/api/session/create', json=payload())
    assert r.json()['terminal_access']['error'] == 'SESSION_IDENTITY_MISMATCH'
    assert 'terminal_instance' not in r.json()
