from __future__ import annotations

import ast
import inspect

from terminal_mcp import direct_contract
from terminal_mcp.compact_tools import TURN_ACTIONS
from terminal_mcp.mcp_app import build_mcp


def _registered_tool_names(server) -> set[str]:
    manager = server._tool_manager
    tools = getattr(manager, "_tools", {})
    return set(tools)


def test_direct_contract_is_machine_readable_and_paperclip_independent():
    payload = direct_contract.contract()
    assert payload["independentOfPaperclip"] is True
    tree = ast.parse(inspect.getsource(direct_contract))
    imported = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)} | {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any(name == "paperclip" or name.startswith("paperclip.") for name in imported)


def test_required_direct_turn_actions_exist_and_retired_queue_actions_are_not_required():
    available = set(TURN_ACTIONS)
    assert direct_contract.REQUIRED_DIRECT_TURN_ACTIONS <= available
    assert direct_contract.REQUIRED_DIRECT_TURN_ACTIONS.isdisjoint(
        direct_contract.RETIRED_QUEUE_ACTIONS
    )


def test_required_direct_tools_stay_public_when_queue_is_disabled(monkeypatch):
    monkeypatch.delenv("TERMINAL_MCP_ENABLE_QUEUE", raising=False)
    server = build_mcp()
    names = _registered_tool_names(server)
    assert direct_contract.REQUIRED_DIRECT_TOOLS <= names


def test_direct_contract_has_no_paperclip_runtime_dependency():
    source = inspect.getsource(direct_contract)
    assert "import paperclip" not in source
    assert "from paperclip" not in source
