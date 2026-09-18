from terminal_mcp.controller import ControllerService
from terminal_mcp.node_registry import NodeRegistry
from terminal_mcp.server_http import local_node_identity


def test_local_identity_defaults(monkeypatch):
    monkeypatch.delenv("TERMINAL_MCP_LOCAL_NODE_ID", raising=False)
    monkeypatch.delenv("TERMINAL_MCP_LOCAL_NODE_NAME", raising=False)
    assert local_node_identity() == ("local", "Local")


def test_explicit_identity_propagates_to_controller(monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_LOCAL_NODE_ID", "dell-linux")
    monkeypatch.setenv("TERMINAL_MCP_LOCAL_NODE_NAME", "dell-linux (Dell Latitude 5511)")
    node_id, node_name = local_node_identity()
    controller = ControllerService(NodeRegistry(), local_node_id=node_id, local_display_name=node_name)
    assert controller.local_node_id == "dell-linux"
    assert controller.node_status("dell-linux").display_name == "dell-linux (Dell Latitude 5511)"
