"""Contracts the Finish Gate pins down, each from a defect found by
exercising the running fleet rather than by reading code.
"""
from __future__ import annotations

import pytest

from terminal_mcp.controller import ControllerService
from terminal_mcp.node_models import NODE_ONLINE


class _StubClient:
    """A node agent old enough to still answer `allowed` from its own
    session-name whitelist -- which is every agent on this fleet today
    (0.12.0, contract v0)."""

    def __init__(self, rows):
        self._rows = rows

    def list_sessions(self):
        return {"sessions": self._rows}


def _controller_with(rows, tmp_path):
    controller = ControllerService.__new__(ControllerService)
    from dataclasses import replace
    from terminal_mcp.node_registry import NodeRegistry
    registry = NodeRegistry(tmp_path / "nodes.db")
    registry.register("dell-linux", display_name="dell-linux", hostname="dell", endpoint="http://127.0.0.1:8790")
    node = replace(registry.get("dell-linux"), status=NODE_ONLINE)
    controller.registry = type("R", (), {"list": staticmethod(lambda: [node])})()
    controller._clients = {"dell-linux": _StubClient(rows)}   # type: ignore[attr-defined]
    # terminal_list_sessions reaches the node list through list_nodes, which
    # asks the health service to evaluate each node. These contracts are about
    # how a REMOTE ROW's `allowed` is normalised, not about health, so the
    # stub hands the node straight back and the status stays whatever the
    # fixture set.
    controller.node_health = type("H", (), {                  # type: ignore[attr-defined]
        "evaluate": staticmethod(lambda node, client, **_kwargs: node),
        "_cached": staticmethod(lambda node: node),
    })()
    return controller


def test_a_remote_row_never_reports_allowed_false_next_to_readable(tmp_path):
    """The exact contradiction default-open exists to remove.

    Measured live before this fix: 16 of 20 sessions -- every remote one --
    came back `allowed: False` beside `effective_read: True`. The local
    listing had been normalised; this merge had not.
    """
    rows = [{"name": "mesflow", "allowed": False, "effective_read": True,
             "effective_input": True, "read_allowed": True, "input_allowed": True}]
    merged = ControllerService.terminal_list_sessions(_controller_with(rows, tmp_path))["sessions"]
    assert merged[0]["allowed"] is True
    assert merged[0]["allowed"] == merged[0]["effective_read"]


def test_normalising_allowed_never_widens_access(tmp_path):
    # A genuinely unreadable session must stay unreadable in the alias too.
    rows = [{"name": "root-shell", "allowed": True, "effective_read": False,
             "effective_input": False}]
    merged = ControllerService.terminal_list_sessions(_controller_with(rows, tmp_path))["sessions"]
    assert merged[0]["allowed"] is False


def test_a_row_without_effective_read_is_left_alone(tmp_path):
    # Nothing to restate; inventing an answer would be worse than passing
    # through whatever the node said.
    rows = [{"name": "legacy", "allowed": True}]
    merged = ControllerService.terminal_list_sessions(_controller_with(rows, tmp_path))["sessions"]
    assert merged[0]["allowed"] is True
