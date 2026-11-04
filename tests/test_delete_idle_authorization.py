"""Owner-authorized cleanup never queues or overrides active-work protection."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from terminal_mcp.config import AppConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.core import TerminalService


@pytest.fixture
def service(monkeypatch):
    service = object.__new__(TerminalService)
    service.config = AppConfig(allowed_session_patterns=("*",), permissions=PermissionsConfig(True, True), session_lifecycle=SessionLifecycleConfig(enabled=True))
    service.audit = Mock()
    service.tmux = Mock()
    service.tmux.get_session.return_value = SimpleNamespace(pane_current_path='/repo', windows=1)
    service.tmux.capture_lines.return_value = ['$ ']
    service._session_delete_runtime_blocker = Mock(return_value=None)
    service._session_delete_local_references = Mock(return_value={})
    service.lifecycle = Mock()
    service.lifecycle.delete.return_value = {'deleted': True}
    service._cleanup_after_session_gone = Mock()
    service._delete_tombstone = Mock(return_value={})
    monkeypatch.setattr('terminal_mcp.core.classify_status', lambda *a: ('IDLE', False, 'prompt'))
    monkeypatch.setattr('terminal_mcp.core.probe_git_state', lambda *a, **kw: {'dirty': False})
    return service


def test_owner_authorized_idle_delete_needs_no_extra_confirmation(service):
    assert service.terminal_delete_session('idle', requested_by='mcp')['deleted'] is True


def test_risky_session_requires_one_explicit_confirmation(service, monkeypatch):
    monkeypatch.setattr('terminal_mcp.core.classify_status', lambda *a: ('UNKNOWN', False, 'unknown'))
    first = service.terminal_delete_session('risky', requested_by='mcp')
    assert first['error'] == 'CONFIRMATION_REQUIRED'
    assert first['deleted'] is False
    service.lifecycle.delete.assert_not_called()
    assert service.terminal_delete_session('risky', requested_by='mcp', confirm=True)['deleted'] is True


@pytest.mark.parametrize('confirm', [False, True])
def test_running_session_cannot_be_confirmed_away(service, monkeypatch, confirm):
    monkeypatch.setattr('terminal_mcp.core.classify_status', lambda *a: ('RUNNING', False, 'busy'))
    assert service.terminal_delete_session('busy', requested_by='mcp', confirm=confirm)['error'] == 'SESSION_RUNNING'
    service.lifecycle.delete.assert_not_called()


@pytest.mark.parametrize('confirm', [False, True])
def test_dirty_git_cannot_be_confirmed_away(service, monkeypatch, confirm):
    monkeypatch.setattr('terminal_mcp.core.probe_git_state', lambda *a, **kw: {'dirty': True})
    assert service.terminal_delete_session('dirty', requested_by='mcp', confirm=confirm)['error'] == 'GIT_DIRTY_CHECKPOINT_REQUIRED'
    service.lifecycle.delete.assert_not_called()


def test_runtime_blocker_precedes_idle_authorization(service):
    service._session_delete_runtime_blocker.return_value = 'SESSION_LEASED'
    assert service.terminal_delete_session('leased', requested_by='mcp')['error'] == 'SESSION_LEASED'
    service.lifecycle.delete.assert_not_called()


def test_confirmation_is_not_remembered_for_next_delete(service, monkeypatch):
    monkeypatch.setattr('terminal_mcp.core.classify_status', lambda *a: ('UNKNOWN', False, 'unknown'))
    assert service.terminal_delete_session('first', requested_by='mcp', confirm=True)['deleted']
    service.lifecycle.delete.reset_mock()
    assert service.terminal_delete_session('second', requested_by='mcp')['error'] == 'CONFIRMATION_REQUIRED'
    service.lifecycle.delete.assert_not_called()


def test_unknown_git_requires_confirmation(service, monkeypatch):
    monkeypatch.setattr('terminal_mcp.core.probe_git_state', lambda *a, **kw: {'dirty': None})
    assert service.terminal_delete_session('unknown', requested_by='mcp')['error'] == 'CONFIRMATION_REQUIRED'
    service.lifecycle.delete.assert_not_called()


@pytest.mark.parametrize('confirm', [None, True])
def test_compact_delete_returns_result_without_queueing(service, confirm):
    from terminal_mcp.compact_tools import CompactTerminalTools

    queue = Mock()
    queue.enqueue.side_effect = AssertionError('delete must never queue')
    tools = CompactTerminalTools(service, queue, handlers={
        'delete_session': lambda name, **kw: service.terminal_delete_session(
            name, requested_by='mcp', **kw),
    })
    result = tools.turn(action='delete_session', target='idle', confirm=confirm)
    assert result['status'] == 'OK'
    assert result['result']['deleted'] is True
    queue.enqueue.assert_not_called()


@pytest.mark.parametrize('dirty', [False, True])
def test_live_git_preflight(service, monkeypatch, tmp_path, dirty):
    import subprocess
    from terminal_mcp.session_resource import probe_git_state

    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    if dirty:
        (tmp_path / 'unsaved.txt').write_text('keep this work')
    service.tmux.get_session.return_value.pane_current_path = str(tmp_path)
    monkeypatch.setattr('terminal_mcp.core.probe_git_state', probe_git_state)
    result = service.terminal_delete_session('git-session', requested_by='mcp')
    if dirty:
        assert result['error'] == 'GIT_DIRTY_CHECKPOINT_REQUIRED'
        service.lifecycle.delete.assert_not_called()
        assert (tmp_path / 'unsaved.txt').read_text() == 'keep this work'
    else:
        assert result['deleted'] is True
