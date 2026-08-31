from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .config import load_config
from .core import TerminalService


def build_mcp(service: TerminalService | None = None) -> MCPServer:
    """Build one MCP surface over the shared, transport-independent service."""
    terminal = service or TerminalService(load_config())
    server = MCPServer(
        name="terminal-mcp",
        description="Whitelist-only tmux observation and controlled input",
        instructions="Only access explicitly allowed tmux sessions. Input is disabled by default.",
        version="0.4.0",
    )

    @server.tool()
    def terminal_list_sessions() -> dict:
        """List whitelisted tmux sessions without exposing denied session details."""
        return terminal.terminal_list_sessions()

    @server.tool()
    def terminal_tail(session: str, lines: int = 200) -> dict:
        """Return sanitized recent output from an allowed tmux session."""
        return terminal.terminal_tail(session, lines)

    @server.tool()
    def terminal_capture(session: str, start_line: int | None = None) -> dict:
        """Return a larger sanitized scrollback capture, capped by configuration."""
        return terminal.terminal_capture(session, start_line)

    @server.tool()
    def terminal_status(session: str) -> dict:
        """Classify an allowed tmux session with an explicit heuristic reason."""
        return terminal.terminal_status(session)

    @server.tool()
    def terminal_send_text(session: str, text: str, press_enter: bool = False) -> dict:
        """Send literal text only when terminal_input is enabled in local config."""
        return terminal.terminal_send_text(session, text, press_enter)

    @server.tool()
    def terminal_send_keys(session: str, keys: list[str]) -> dict:
        """Send only allowlisted tmux keys when terminal_input is enabled in local config."""
        return terminal.terminal_send_keys(session, keys)

    @server.tool()
    def terminal_bind(binding: str, session: str, replace: bool = False,
                      read_enabled: bool = True, input_enabled: bool = False) -> dict:
        """Persist a logical binding to an existing, allowed tmux session."""
        return terminal.terminal_bind(binding, session, replace, read_enabled, input_enabled)

    @server.tool()
    def terminal_get_binding(binding: str) -> dict:
        """Return binding metadata and current effective permissions."""
        return terminal.terminal_get_binding(binding)

    @server.tool()
    def terminal_list_bindings() -> list[dict]:
        """List persistent logical bindings and current session state."""
        return terminal.terminal_list_bindings()

    @server.tool()
    def terminal_unbind(binding: str) -> dict:
        """Delete a logical binding without changing its tmux session."""
        return terminal.terminal_unbind(binding)

    @server.tool()
    def terminal_tail_bound(binding: str, lines: int = 200) -> dict:
        """Return sanitized output after resolving a logical binding."""
        return terminal.terminal_tail_bound(binding, lines)

    @server.tool()
    def terminal_status_bound(binding: str) -> dict:
        """Classify the tmux session resolved by a logical binding."""
        return terminal.terminal_status_bound(binding)

    @server.tool()
    def terminal_send_bound(binding: str, text: str, press_enter: bool = False) -> dict:
        """Send literal text only when global and binding input are enabled."""
        return terminal.terminal_send_bound(binding, text, press_enter)

    return server
