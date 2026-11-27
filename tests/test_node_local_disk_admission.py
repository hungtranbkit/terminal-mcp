from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from terminal_mcp import lifecycle
from terminal_mcp.config import AppConfig, SessionLifecycleConfig, PermissionsConfig
from terminal_mcp.host_metrics import local_agent_disk_block_reason

GIB = 1024 ** 3


@pytest.mark.parametrize("used,free,inodes,available,blocked", [
    (224129048576, 12899766272, 100, 84, True),
    (108939894784, 8996671488, 100, 82, True),
    (40*GIB, 13*GIB, 100, 88, True),
    (180*GIB, 20*GIB, 100, 80, True),
    (40*GIB, 100*GIB, 100, 10, True),
    (40*GIB, 20*GIB, 100, 11, False),
    (40*GIB, 100*GIB, 0, 0, True),
])
def test_fresh_target_path(monkeypatch, tmp_path, used, free, inodes, available, blocked):
    from terminal_mcp import host_metrics as hm
    observed = []
    def probe(path):
        observed.append(path)
        return used+free, used, free
    monkeypatch.setattr(hm, "disk_usage", probe)
    monkeypatch.setattr(hm.os, "statvfs", lambda path: SimpleNamespace(
        f_files=inodes, f_ffree=available, f_favail=available))
    assert bool(local_agent_disk_block_reason(str(tmp_path))) is blocked
    assert observed == [str(tmp_path)]


def test_unknown_disk_fails_closed(monkeypatch, tmp_path):
    from terminal_mcp import host_metrics as hm
    monkeypatch.setattr(hm, "disk_usage", lambda _: (None, None, None))
    assert local_agent_disk_block_reason(str(tmp_path))


def test_node_local_block_before_launcher_or_backend(monkeypatch, tmp_path):
    config = AppConfig(permissions=PermissionsConfig(True, True), allowed_session_patterns=("capacity-*",), session_lifecycle=SessionLifecycleConfig(
        enabled=True, allowed_cwd_roots=(str(tmp_path),)))
    backend = Mock()
    backend.get_session.return_value = None
    service = lifecycle.SessionLifecycleService(config, backend)
    service.launch_command_for = Mock(return_value="codex")
    checked = Mock(return_value="low space")
    monkeypatch.setattr(lifecycle, "local_agent_disk_block_reason", checked)
    result = service.create("capacity-local-test", "codex", str(tmp_path))
    assert result["error"] == "NODE_DISK_CAPACITY_BLOCKED"
    checked.assert_called_once_with(str(tmp_path.resolve()))
    service.launch_command_for.assert_not_called()
    backend.new_session.assert_not_called()


def test_recovery_shell_does_not_probe_agent_gate(monkeypatch, tmp_path):
    from terminal_mcp.tmux import TmuxError
    config = AppConfig(permissions=PermissionsConfig(True, True), allowed_session_patterns=("capacity-*",), session_lifecycle=SessionLifecycleConfig(
        enabled=True, allowed_cwd_roots=(str(tmp_path),)))
    backend = Mock()
    backend.get_session.return_value = None
    backend.new_session.side_effect = TmuxError("test stops before spawning")
    service = lifecycle.SessionLifecycleService(config, backend)
    checked = Mock(return_value="low space")
    monkeypatch.setattr(lifecycle, "local_agent_disk_block_reason", checked)
    service.create("capacity-recovery-test", "shell", str(tmp_path))
    checked.assert_not_called()
    backend.new_session.assert_called_once()
