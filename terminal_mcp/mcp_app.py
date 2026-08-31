from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import __version__
from .config import load_config
from .core import TerminalService
from .supervisor import SupervisorService, SupervisorStore


def build_mcp(service: TerminalService | None = None,
              supervisor: SupervisorService | None = None) -> MCPServer:
    """Build one MCP surface over the shared, transport-independent service.

    `supervisor` is always constructed and its tools always registered
    (supervisor_watch/status/events are just data-plane operations, useful
    even with the background auto-poll loop disabled) — only the *automatic*
    background thread is gated on config.supervisor.enabled, and that gating
    happens in server_http.py, not here."""
    terminal = service or TerminalService(load_config())
    supervisor = supervisor or SupervisorService(terminal, SupervisorStore())
    server = MCPServer(
        name="terminal-mcp",
        description="Whitelist-only tmux observation and controlled input",
        instructions="Only access explicitly allowed tmux sessions. Input is disabled by default.",
        version=__version__,
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
    def terminal_send_text(session: str, text: str, press_enter: bool = False,
                           dry_run: bool = False) -> dict:
        """Send literal text only when terminal_input is enabled in local config."""
        return terminal.terminal_send_text(session, text, press_enter, dry_run)

    @server.tool()
    def terminal_send_keys(session: str, keys: list[str], confirm_sensitive: bool = False) -> dict:
        """Send only allowlisted tmux keys when terminal_input is enabled in local config."""
        return terminal.terminal_send_keys(session, keys, confirm_sensitive)

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
    def terminal_send_bound(binding: str, text: str, press_enter: bool = False,
                            dry_run: bool = False) -> dict:
        """Send literal text only when global and binding input are enabled."""
        return terminal.terminal_send_bound(binding, text, press_enter, dry_run)

    @server.tool()
    def terminal_list_input_audit(limit: int = 50, binding: str | None = None,
                                  session: str | None = None) -> dict:
        """List sanitized input audit metadata; full prompts are never returned."""
        return terminal.terminal_list_input_audit(limit, binding, session)

    @server.tool()
    def terminal_input_context(session: str | None = None,
                               binding: str | None = None) -> dict:
        """Inspect the last 20 lines and effective permission before sending input."""
        return terminal.terminal_input_context(session, binding)

    # -- Supervisor Loop v1: detection + a durable event queue only. Never
    # sends input, never executes a shell command; the underlying watch/poll
    # path is the same whitelist-guarded terminal_status(_bound) above. ----

    @server.tool()
    def supervisor_watch(binding: str | None = None, session: str | None = None) -> dict:
        """Create or re-enable a watch on an allowed binding or whitelisted session."""
        return supervisor.watch(binding, session)

    @server.tool()
    def supervisor_unwatch(binding: str | None = None, session: str | None = None,
                           delete: bool = False) -> dict:
        """Disable (or, with delete=true, remove) a watch. Disabled watches stop
        polling until explicitly re-watched."""
        return supervisor.unwatch(binding, session, delete)

    @server.tool()
    def supervisor_list_watches() -> dict:
        """List all watches and their current state/iteration/failure bookkeeping."""
        return supervisor.list_watches()

    @server.tool()
    def supervisor_status() -> dict:
        """Report whether the background poll loop is running and a summary of
        watch states, including any stalled/disabled watches."""
        return supervisor.status()

    @server.tool()
    def supervisor_list_events(target: str | None = None, state: str | None = None,
                               unacknowledged_only: bool = False, limit: int = 50) -> dict:
        """List persisted supervisor events (already redacted before storage),
        optionally filtered by target, normalized state, or unacknowledged-only."""
        return supervisor.list_events(target, state, unacknowledged_only, limit)

    @server.tool()
    def supervisor_ack_event(id: int) -> dict:
        """Mark one event acknowledged. Local metadata only — never sends
        anything to the watched session."""
        return supervisor.ack_event(id)

    @server.tool()
    def supervisor_run_once() -> dict:
        """Run exactly one synchronous poll pass over all enabled watches now,
        for deterministic manual testing independent of the background loop's
        timer."""
        return supervisor.run_once()

    return server
