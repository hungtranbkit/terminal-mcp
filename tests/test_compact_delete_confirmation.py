"""Compact delete preserves the real downstream confirmation boundary."""
from types import SimpleNamespace

import pytest

from terminal_mcp.compact_tools import CompactTerminalTools
from terminal_mcp.config import AppConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.core import TerminalService


@pytest.mark.parametrize("action", ["delete_session", "delete", "kill"])
@pytest.mark.parametrize("args", [None, {}, {"confirm": False}, {"confirm": "true"},
                                  {"confirm": "false"}, {"confirm": 1}])
def test_unconfirmed_compact_delete_cannot_reach_backend(action, args):
    service = object.__new__(TerminalService)
    service.config = AppConfig(allowed_session_patterns=("test-*",), permissions=PermissionsConfig(True, True),
                               session_lifecycle=SessionLifecycleConfig(enabled=True))
    service.audit = SimpleNamespace(record=lambda **kwargs: None)
    class NoBackendAccess:
        def __getattr__(self, name):
            raise AssertionError("unconfirmed delete reached backend")
    service.tmux = NoBackendAccess()
    tools = CompactTerminalTools(service, None, handlers={"delete_session": service.terminal_delete_session})
    result = tools.turn(action=action, target="test-compact-delete", args=args)
    assert result["status"] == "FAILED"
    error = result.get("error") or result["result"]["error"]
    assert error in {"CONFIRMATION_REQUIRED", "INVALID_ARGUMENT"}


@pytest.mark.parametrize("action", ["delete_session", "delete", "kill"])
def test_confirm_true_is_forwarded_without_coercion(action):
    calls = []
    def delete(name, *, confirm=False):
        calls.append((name, confirm))
        return {"deleted": True}
    tools = CompactTerminalTools(None, None, handlers={"delete_session": delete})
    result = tools.turn(action=action, target=" test-compact-delete ", args={"confirm": True})
    assert result["status"] == "OK"
    assert calls == [("test-compact-delete", True)]
