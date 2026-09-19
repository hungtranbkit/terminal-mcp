import asyncio
import builtins
import ipaddress
import json
import os
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from terminal_mcp.browser_gateway import BrowserGateway
from terminal_mcp.browser_safety import UrlPolicy, UrlRejected, scrub, validate_url
from terminal_mcp.browser_script import parse_task
from terminal_mcp.config import BrowserGatewayConfig, _load_browser_gateway_config


@pytest.mark.parametrize('url', [
    'file:///etc/passwd', 'javascript:alert(1)', 'data:text/html,test', 'ftp://example.org',
    'http://169.254.169.254', 'http://169.254.1.1', 'http://[fe80::1]',
    'http://metadata.google.internal', 'http://100.100.100.200', 'http://[fd00:ec2::254]',
    'http://[::ffff:169.254.169.254]', 'http://0.0.0.0', 'http://224.0.0.1',
    'http://127.0.0.1', 'http://[::1]', 'http://localhost', 'http://10.0.0.1',
    'http://[::ffff:127.0.0.1]', 'http://[::ffff:10.0.0.1]', 'http://100.64.0.1',
    'http://user:password@example.org', 'http://example.org\\@127.0.0.1',
    'http://[broken', 'http://example.org:bad', 'http://local%68ost', 'http://a\x01b',
])
def test_url_policy_rejects(url):
    with pytest.raises(UrlRejected):
        validate_url(url, UrlPolicy(resolve_dns=False))


def test_patterns_cannot_grant_private_or_metadata():
    for url in ['http://10.0.0.1/allowed', 'http://169.254.169.254/allowed']:
        with pytest.raises(UrlRejected):
            validate_url(url, UrlPolicy(allow_patterns=('allowed',), resolve_dns=False))
    assert validate_url('http://localhost:8888', UrlPolicy(allow_loopback=True, resolve_dns=False))
    with pytest.raises(UrlRejected):
        validate_url('http://127.0.0.1/deny', UrlPolicy(allow_loopback=True, deny_patterns=('deny',)))


def test_dns_fails_closed_and_checks_all_answers(monkeypatch):
    from terminal_mcp import browser_safety as safety
    monkeypatch.setattr(safety, '_resolve', lambda _: [])
    with pytest.raises(UrlRejected, match='URL_DNS_FAILED'):
        validate_url('https://example.org')
    monkeypatch.setattr(safety, '_resolve', lambda _: [ipaddress.ip_address('8.8.8.8')]*16 +
                        [ipaddress.ip_address('127.0.0.1')])
    with pytest.raises(UrlRejected, match='URL_LOOPBACK_BLOCKED'):
        validate_url('https://example.org')


def test_proxy_connects_to_checked_numeric_address(monkeypatch):
    from terminal_mcp import browser_network as network
    monkeypatch.setattr(network, '_resolve', lambda _: [ipaddress.ip_address('8.8.8.8')])
    assert network.destination('example.org', 443, UrlPolicy()) == '8.8.8.8'
    monkeypatch.setattr(network, '_resolve', lambda _: [ipaddress.ip_address('127.0.0.1')])
    with pytest.raises(UrlRejected):
        network.destination('example.org', 443, UrlPolicy())


def gateway(runner=None, **kwargs):
    return BrowserGateway(BrowserGatewayConfig(enabled=True, **kwargs), runner=runner)


def test_scrub_before_truncation_and_url_secrets():
    for text in ['token=very-secret', 'https://user:very-secret@example.org',
                 'https://example.org/?%74oken=very-secret', 'Authorization: Bearer very-secret']:
        assert 'very-secret' not in scrub(text)
    assert 'very' not in scrub('token=very-secret', 10)


def test_bounded_redacted_result_and_parse_errors():
    observation = {'status': 200, 'title': 'token=very-secret',
                   'console_errors': ['token=very-secret ' + 'x'*5000]*1000,
                   'network_errors': [{'status': 500, 'url': 'http://x/?key=very-secret'}]*1000}
    service = gateway(lambda *_: {'ok': True, 'observation': observation})
    result = service.verify('https://8.8.8.8', ['no errors', 'status is: nonsense'])
    assert result['status'] == 'ERROR'
    assert 'very-secret' not in json.dumps(result)
    assert len(json.dumps(result)) < 30000
    assert result['messages_omitted'] == 1960


def test_failed_task_details_redacted_and_no_fill_echo():
    service = gateway(lambda *_: {'ok': True, 'steps': [
        {'action': 'click', 'status': 'FAILED', 'detail': 'token=very-secret'}]})
    result = service.run_task('fill #password with very-secret; click #submit', url='https://8.8.8.8',
                              allow_mutations=True)
    assert result['status'] == 'FAILED'
    assert 'very-secret' not in json.dumps(result)


def test_disabled_and_parse_failure_never_launch():
    def forbidden(*_):
        pytest.fail('worker should not run')
    assert BrowserGateway(runner=forbidden).verify('https://8.8.8.8')['error'] == 'BROWSER_GATEWAY_DISABLED'
    assert gateway(forbidden).run_task('figure out the login')['error'] == 'TASK_NOT_PLANNABLE'
    assert gateway(forbidden).run_task('open file:///etc/passwd')['error'] == 'URL_SCHEME_BLOCKED'
    assert parse_task('x'*16001)[1]


def test_dependency_unavailable(monkeypatch):
    from terminal_mcp.browser_worker import run_job
    original = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name.startswith('playwright'):
            raise ImportError('not installed')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', unavailable)
    assert run_job({})['error'] == 'BROWSER_DEPENDENCY_UNAVAILABLE'


@pytest.mark.parametrize('code, expected', [
    ('import time; time.sleep(30)', 'BROWSER_TIMEOUT'),
    ('import sys; sys.stdout.write("x"*300000); sys.stdout.flush()', 'BROWSER_OUTPUT_LIMIT'),
    ('print("[]")', 'BROWSER_WORKER_ERROR'),
])
def test_hard_timeout_output_and_cleanup(monkeypatch, code, expected):
    real_popen = subprocess.Popen
    children = []
    def popen(_cmd, **kwargs):
        proc = real_popen([sys.executable, '-c', code], **kwargs)
        children.append(proc)
        return proc
    monkeypatch.setattr(subprocess, 'Popen', popen)
    started = time.monotonic()
    assert gateway()._spawn_worker({}, .3)['error'] == expected
    assert time.monotonic() - started < 3
    assert children[0].poll() is not None
    assert children[0].stdout.closed and children[0].stderr.closed


def test_cleanup_kills_surviving_grandchild(monkeypatch, tmp_path):
    pidfile = tmp_path / 'child.pid'
    real_popen = subprocess.Popen
    code = ('import subprocess,sys; p=subprocess.Popen([sys.executable,"-c",'
            '"import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"],'
            'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
            f'open({str(pidfile)!r},"w").write(str(p.pid)); print("{{\\"ok\\":true}}")')
    monkeypatch.setattr(subprocess, 'Popen', lambda _cmd, **kw: real_popen([sys.executable, '-c', code], **kw))
    assert gateway()._spawn_worker({}, 2)['ok']
    pid = int(pidfile.read_text())
    for _ in range(30):
        try:
            state = open(f'/proc/{pid}/stat').read().split()[2]
        except FileNotFoundError:
            break
        if state == 'Z':
            break
        time.sleep(.02)
    else:
        pytest.fail('grandchild survived cleanup')


def test_env_config_and_timeout_caps(monkeypatch):
    monkeypatch.setenv('TERMINAL_MCP_BROWSER_ENABLED', '1')
    monkeypatch.setenv('TERMINAL_MCP_BROWSER_ALLOW_LOOPBACK', 'true')
    service = BrowserGateway.from_config()
    assert service.config.enabled and service.config.allow_loopback
    assert not BrowserGatewayConfig().allow_loopback
    assert not BrowserGatewayConfig().screenshots_enabled
    for requested in [1, 10000, float('inf'), float('nan')]:
        assert service._budget(requested)[1] <= service.config.hard_timeout_seconds
    for raw in [{'allow_loopback': 'false'}, {'hard_timeout_seconds': float('nan')}]:
        with pytest.raises(ValueError):
            _load_browser_gateway_config(raw)
    monkeypatch.setenv('TERMINAL_MCP_BROWSER_ENABLED', 'maybe')
    with pytest.raises(ValueError):
        BrowserGateway.from_config()


def test_artifact_retention(tmp_path):
    for index in range(5):
        (tmp_path / f'{index}.png').write_bytes(b'png')
    gateway(artifact_dir=str(tmp_path), keep_artifacts=2)._prune_artifacts()
    assert len(list(tmp_path.glob('*.png'))) == 2


def test_public_registration_and_compact_dispatch(monkeypatch):
    from terminal_mcp import mcp_app
    from terminal_mcp.compact_tools import CompactTerminalTools
    from terminal_mcp.chatgpt_sidecar import CATALOG
    captured = {}
    original = mcp_app.turn_handler_map
    def capture(**handlers):
        captured.update(handlers)
        captured['compact'] = CompactTerminalTools(None, None, handlers=handlers)
        return original(**handlers)
    monkeypatch.setattr(mcp_app, 'turn_handler_map', capture)
    app = mcp_app.build_mcp()
    names = {tool.name for tool in asyncio.run(app.list_tools())}
    expected = {'browser_verify', 'browser_run_task', 'browser_status', 'browser_screenshot',
                'browser_stop'}
    assert {name for name in names if name.startswith('browser_')} == expected
    assert CATALOG == ('terminal_turn',)
    for action in expected:
        result = captured['compact'].turn(action=action, target='https://8.8.8.8', text='click #ok')
        assert result.get('error') != 'ACTION_UNAVAILABLE'
        assert 'result' in result
    result = captured['compact'].turn(action='browser_status', args={'bad': 1})
    assert result['error'] == 'UNKNOWN_ARGS'
    assert result['unknown'] == ['bad']
    assert result['allowed'] == ['probe']
    assert captured['compact'].turn(action='browser_status', args=['bad'])['error'] == 'INVALID_ARGS'


@pytest.mark.parametrize('action, args, expected', [
    ('browser_verify', {'assertions': ['text contains: Ready']},
     {'url': 'http://localhost:3000', 'assertions': ['text contains: Ready']}),
    ('browser_run_task', {'timeout_seconds': 5},
     {'url': 'http://localhost:3000', 'task': 'click #ok', 'timeout_seconds': 5}),
    ('browser_screenshot', {'full_page': True},
     {'url': 'http://localhost:3000', 'full_page': True}),
    ('browser_status', {'probe': True}, {'probe': True}),
])
def test_main_handler_map_forwards_browser_arguments(action, args, expected):
    from terminal_mcp.compact_tools import CompactTerminalTools
    calls = []
    def handler(**kwargs):
        calls.append(kwargs)
        return {'status': 'PASS'}
    compact = CompactTerminalTools(None, None, handlers={action: handler})
    result = compact.turn(action=action, target='http://localhost:3000',
                          text='click #ok', args=args)
    assert calls == [expected]
    assert result == {'status': 'PASS', 'action': action, 'result': {'status': 'PASS'}}


# ---------------------------------------------------------------------------
# Mutations are opt-in, and `stop` is not a no-op verb.
# ---------------------------------------------------------------------------

def test_page_mutating_steps_require_explicit_authorization():
    """A read-only-looking call must not be able to click the button.

    This surface is reachable from a chat client: "check the cart renders"
    must not empty the cart as a side effect.
    """
    launched = []
    service = gateway(lambda *a: launched.append(a) or {'ok': True, 'steps': []})
    result = service.run_task('fill #qty with 2; click #apply', url='https://8.8.8.8')
    assert result['error'] == 'BROWSER_MUTATION_NOT_ALLOWED'
    assert result['mutating_steps'] == ['click', 'fill']
    assert launched == [], 'a refused task must never reach the browser'


def test_read_only_steps_need_no_authorization():
    service = gateway(lambda *_: {'ok': True, 'steps': [], 'observation': {}})
    result = service.run_task('open https://8.8.8.8; wait for #main; assert text contains: hi',
                              url='https://8.8.8.8')
    assert result.get('error') != 'BROWSER_MUTATION_NOT_ALLOWED'


def test_authorized_mutations_are_echoed_for_audit():
    service = gateway(lambda *_: {'ok': True, 'steps': [], 'observation': {}})
    result = service.run_task('fill #qty with 2; click #apply', url='https://8.8.8.8',
                              allow_mutations=True)
    assert result['mutations'] == ['click', 'fill']


def test_stop_is_idle_when_nothing_is_running():
    result = gateway(lambda *_: {'ok': True, 'steps': []}).stop()
    assert result['status'] == 'IDLE'
    assert result['terminated'] == 0


def test_stop_terminates_an_in_flight_worker_group():
    """The case the per-call deadline cannot cover: a worker still alive
    after its caller has gone."""
    import subprocess, sys as _sys
    service = gateway(lambda *_: {'ok': True, 'steps': []})
    proc = subprocess.Popen([_sys.executable, '-c', 'import time; time.sleep(30)'],
                            start_new_session=True)
    service._live.add(proc)
    result = service.stop()
    assert result['status'] == 'STOPPED'
    assert result['terminated'] == 1
    assert proc.poll() is not None, 'the worker process group must be gone'
    assert service.stop()['status'] == 'IDLE'
