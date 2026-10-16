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


@pytest.mark.parametrize("confirm", ["true", "false", 1, 0, True, False, None])
def test_mcp_delete_confirmation_does_not_coerce(confirm):
    """Exercise schema validation, which runs before CompactTerminalTools."""
    import asyncio
    from terminal_mcp.mcp_app import build_mcp

    server = build_mcp()
    calls = []
    server.compact_tools.handlers["delete_session"] = (
        lambda name, **kwargs: calls.append((name, kwargs)) or {"deleted": True}
    )
    def call():
        return asyncio.run(server.call_tool("terminal_turn", {
            "action": "delete_session", "target": "test-confirm-boundary",
            "confirm": confirm, "screenshot": "on_failure",
        }))
    if type(confirm) is not bool and confirm is not None:
        from mcp.server.mcpserver.exceptions import ToolError
        with pytest.raises(ToolError, match="valid boolean"):
            call()
    else:
        call()
    if confirm is True:
        assert calls == [("test-confirm-boundary", {"confirm": True})]
    elif type(confirm) is not bool and confirm is not None:
        assert calls == []
    else:
        assert all(kwargs.get("confirm") is not True for _, kwargs in calls)
