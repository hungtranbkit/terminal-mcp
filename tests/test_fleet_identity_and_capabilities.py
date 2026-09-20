"""Two things an operator must be able to believe about the fleet.

1. WHICH NODE AM I. A controller that gets named later leaves its old,
   never-heartbeated `local` row behind forever -- a permanently OFFLINE node
   with no client, standing in every health summary as a blocker and
   answering NODE_UNREACHABLE for anything that resolves onto it.

2. WHY CAN'T THIS NODE RUN CLAUDE. `agent_types: ["shell"]` is not an answer:
   it does not distinguish "not installed" from "installed where this
   process's PATH cannot see it" -- which is the usual cause, because a
   systemd user unit does not inherit a login shell's PATH.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from terminal_mcp.agent_availability import agent_type_evidence, available_agent_types
from terminal_mcp.controller import LOCAL_NODE_ID, ControllerService
from terminal_mcp.node_registry import NodeRegistry


# ---------------------------------------------------------------------------
# The ghost `local` node.
# ---------------------------------------------------------------------------

class _LocalClient:
    def health(self):
        return {"status": "ok"}

    def list_sessions(self, *, timeout_seconds=None):
        return {"sessions": []}


def _controller(tmp_path, node_id, *, client=None):
    return ControllerService(NodeRegistry(tmp_path / "nodes.db"), local_node_id=node_id,
                             local_display_name=node_id, local_hostname="host",
                             local_client=client or _LocalClient())


def test_a_named_controller_retires_the_placeholder_row_it_left_behind(tmp_path):
    # Yesterday: unnamed, so it registered itself as `local`.
    _controller(tmp_path, LOCAL_NODE_ID)
    registry = NodeRegistry(tmp_path / "nodes.db")
    assert registry.get(LOCAL_NODE_ID) is not None

    # Today: TERMINAL_MCP_LOCAL_NODE_ID=hp-linux.
    controller = _controller(tmp_path, "hp-linux")

    assert controller.registry.get("hp-linux") is not None
    assert controller.registry.get(LOCAL_NODE_ID) is None, (
        "the placeholder row survived -- it is permanently OFFLINE and has no client, "
        "so it blocks the health summary and answers NODE_UNREACHABLE")


def test_a_single_host_install_that_really_is_called_local_is_untouched(tmp_path):
    controller = _controller(tmp_path, LOCAL_NODE_ID)
    assert controller.registry.get(LOCAL_NODE_ID) is not None
    assert controller.retire_legacy_local_node()["retired"] is False


def test_a_remote_node_someone_named_local_is_never_deleted(tmp_path):
    controller = _controller(tmp_path, "hp-linux")
    controller.registry.register(LOCAL_NODE_ID, display_name="a real remote host",
                                 hostname="10.0.0.9", endpoint="http://10.0.0.9:8790")

    outcome = controller.retire_legacy_local_node()

    assert outcome["retired"] is False
    assert "endpoint" in outcome["reason"]
    assert controller.registry.get(LOCAL_NODE_ID) is not None


def test_retiring_is_idempotent(tmp_path):
    controller = _controller(tmp_path, "hp-linux")
    assert controller.retire_legacy_local_node()["retired"] is False
    assert controller.retire_legacy_local_node()["retired"] is False


# ---------------------------------------------------------------------------
# Runtime capability evidence.
# ---------------------------------------------------------------------------

LAUNCHERS = (("claude", "claude"), ("codex", "codex"),
             ("nonesuch", "terminal-mcp-definitely-not-a-real-binary"))


def test_evidence_agrees_with_the_capability_list_it_explains():
    evidence = agent_type_evidence(LAUNCHERS)
    available = set(available_agent_types(LAUNCHERS))
    for agent_type, row in evidence.items():
        assert row["available"] is (agent_type in available), agent_type


def test_a_missing_launcher_names_the_binary_and_the_likely_cause():
    row = agent_type_evidence(LAUNCHERS)["nonesuch"]
    assert row["available"] is False
    assert "terminal-mcp-definitely-not-a-real-binary" in row["detail"]
    assert "PATH" in row["detail"]


def test_a_resolved_launcher_reports_where_it_resolved_to():
    """The difference between believing a capability and being able to check
    it: a path an operator can `ls`."""
    import shutil

    if shutil.which("sh") is None:  # pragma: no cover -- no POSIX shell
        pytest.skip("no sh on this host")
    row = agent_type_evidence((("shellish", "sh"),))["shellish"]
    assert row["available"] is True
    assert row["detail"].startswith("resolved to /")


def test_an_unconfigured_launcher_says_so_rather_than_blaming_the_binary():
    row = agent_type_evidence((("ghost", ""),))["ghost"]
    assert row["available"] is False
    assert "no launcher is configured" in row["detail"]


def test_shell_is_always_available_and_says_why():
    assert agent_type_evidence(())["shell"]["available"] is True
