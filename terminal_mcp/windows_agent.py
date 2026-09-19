"""`terminal-windows-node-agent` -- the Windows-side equivalent of
`terminal-node-agent` (node_agent.py). Runs ON a Windows worker node
(e.g. the Lenovo M910), builds the exact SAME `TerminalService` (core.py)
and the exact same HTTP/WebSocket surface `build_node_agent` already
defines -- the ONLY difference from the Linux agent is which
SessionBackend `TerminalService` is constructed with
(`WindowsSessionBackend`, windows_backend.py, instead of the default
`TmuxClient`) and the capability values this process reports in its own
heartbeat (platform="windows", session_backend="windows_pty",
shell_capabilities, wsl_available).

No duplicated business logic, no duplicated HTTP routes, no duplicated
heartbeat loop -- `build_node_agent`/`_heartbeat_loop` (node_agent.py)
are reused completely unmodified; see session_backend.py's own module
docstring for why TerminalService itself needs no Windows-specific code
at all.

    terminal-windows-node-agent --node-id m910 --controller-url http://<dell>:8766 [options]

**Not live-verified on real Windows** -- see windows_backend.py's own
module docstring for exactly what that means and why (no real Windows
host reachable from this development environment). Everything reachable
without a real Windows OS -- argument parsing, config loading, capability
detection's own logic (via shutil.which, which works identically on any
OS), wiring into build_node_agent/the heartbeat loop -- IS exercised for
real by this project's own test suite; only the actual pywinpty/ConPTY
process spawn and the real win32 metrics API calls are not.
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

import anyio
import uvicorn

from .config import load_config
from .core import TerminalService
from .node_agent import (AgentCredential, _heartbeat_loop, _read_token, build_node_agent,
                         watch_for_shutdown)
from .node_models import PLATFORM_WINDOWS, SESSION_BACKEND_WINDOWS_PTY
from .windows_backend import WindowsSessionBackend

_log = logging.getLogger(__name__)

# Real launcher-binary lookups, via shutil.which -- identical mechanism
# agent_availability.py already uses for claude/codex, applied here to
# this node's own interactive shell options. Every candidate name is
# tried; `shutil.which` on Windows already searches PATH + PATHEXT
# (.exe/.cmd/.bat/...) the platform-native way, no separate Windows-
# specific lookup needed.
_SHELL_CANDIDATES = (
    ("powershell", "powershell.exe"),
    ("pwsh", "pwsh.exe"),  # PowerShell 7+, if installed alongside/instead of Windows PowerShell
    ("cmd", "cmd.exe"),
)


def detect_shell_capabilities() -> tuple[str, ...]:
    found: list[str] = []
    for label, binary in _SHELL_CANDIDATES:
        if shutil.which(binary) is not None and label not in found:
            found.append(label)
    return tuple(found)


def detect_wsl_available() -> bool:
    """WSL is an OPTIONAL capability this node MAY expose (task's own
    "Nếu Windows có WSL... có thể dùng tmux trong WSL như capability
    riêng, nhưng không được bắt buộc WSL") -- reported here purely as an
    informational flag (dashboard/doctor visibility); this agent's own
    SessionBackend is always the native WindowsSessionBackend regardless
    of this value. A `wsl_tmux` backend (running actual tmux inside WSL)
    is a real, separate future SessionBackend implementation this flag
    would gate eligibility for -- not built here, since it would need
    its own live verification against a real WSL install this
    environment cannot provide either."""
    return shutil.which("wsl.exe") is not None or shutil.which("wsl") is not None


def _default_session_state_root(config) -> Path:
    """Under the node's own workspace root, so session state lives on the same
    disk as the work it belongs to and is covered by whatever the operator
    already backs up there."""
    roots = config.session_lifecycle.allowed_cwd_roots
    base = Path(roots[0]) if roots else Path.home()
    return base / ".terminal-mcp" / "win-sessions"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="terminal-windows-node-agent")
    parser.add_argument("--node-id", required=True, help="This node's own id, as registered on the controller")
    parser.add_argument("--controller-url", required=True, help="e.g. http://192.168.1.10:8766")
    parser.add_argument("--config", default=None, help="Path to this node's own config.yaml")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address -- LAN/private only, never 0.0.0.0 "
                        "without a firewall/VPN boundary in front of it (task item 2)")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--token", default=None)
    parser.add_argument("--token-file", default=None)
    parser.add_argument("--heartbeat-interval-seconds", type=float, default=20.0)
    parser.add_argument("--shell", default="powershell.exe",
                        help="Default interactive shell for agent_type=shell sessions (default: powershell.exe)")
    parser.add_argument("--history-lines", type=int, default=2000,
                        help="Per-session scrollback buffer size this agent keeps in memory")
    # Detached session hosts (docs/WINDOWS_SESSION_HOST.md). OFF by default, on
    # purpose: enabling it changes how every session on this node is spawned, and
    # the live 0.12.0 node's existing sessions are direct children that cannot be
    # migrated into it (see that doc's adoption-impossibility section). Default-off
    # means the new code can be DEPLOYED to a node without altering the behaviour
    # of the agent currently running, so the rollout is a separate, deliberate
    # step rather than a side effect of copying files.
    parser.add_argument("--detached-sessions", action="store_true",
                        help="Spawn sessions in detached per-session host processes so they "
                             "survive this agent being restarted or updated")
    parser.add_argument("--session-state-root", default=None,
                        help="Directory holding detached session state "
                             "(default: <workspace>/.terminal-mcp/win-sessions)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if sys.platform != "win32":
        _log.warning("terminal-windows-node-agent is designed to run on Windows -- sys.platform is %r here. "
                    "The HTTP/heartbeat surface will start, but WindowsSessionBackend's real session spawn "
                    "(pywinpty) will fail on any actual create_session call on this platform.", sys.platform)

    token = _read_token(args.token, args.token_file)
    # Same shared-credential discipline as the Linux agent: rotation
    # must cost a Windows node no more than it costs a Linux one --
    # no restart, since a restart here takes every session with it.
    credential = AgentCredential(token, token_file=args.token_file)
    config = load_config(args.config)
    session_state_root = None
    session_process_factory = None
    if args.detached_sessions:
        from . import windows_detached

        session_state_root = Path(args.session_state_root or _default_session_state_root(config))
        session_state_root.mkdir(parents=True, exist_ok=True)
        session_process_factory = windows_detached.session_process_factory(session_state_root)
        _log.info("detached session hosts ENABLED, state root=%s", session_state_root)
    backend = WindowsSessionBackend(shell=args.shell, history_lines=args.history_lines,
                                    session_process_factory=session_process_factory)
    if session_state_root is not None:
        # Rediscover BEFORE serving: a request that arrives before adoption has
        # finished would be told the session does not exist, and a create for that
        # name would then spawn a second host for a session that is already
        # running. Adoption is a directory read, so this costs startup nothing.
        try:
            verdicts = backend.adopt_detached_sessions(session_state_root)
            if verdicts:
                _log.info("adopted detached sessions: %s", verdicts)
            else:
                _log.info("no detached sessions to adopt under %s", session_state_root)
        except Exception:  # noqa: BLE001 -- a failed adoption must not stop the agent
            _log.exception("detached session adoption failed -- sessions left running, "
                           "unadopted; they are NOT lost, retry by restarting this agent")
    # `registry_node_id` is this agent's OWN id, the same one it registers and
    # heartbeats under. Without it the durable session registry falls back to
    # the `local` placeholder, and on a host that also runs a controller both
    # processes share one session_registry.db -- so the agent kept re-creating
    # the legacy-ownership rows the controller's migration had just retired.
    terminal = TerminalService(config, tmux=backend, registry_node_id=args.node_id)
    # Retired session-name whitelist -> real grants, same as the controller
    # does at its own startup. Every node type runs this so a fleet cannot end
    # up with one machine still honouring a whitelist the others dropped.
    try:
        _migration = terminal.migrate_whitelist_to_grants()
        if _migration.get("read_granted") or _migration.get("input_granted") or _migration.get("errors"):
            _log.info("session-access migration: read=%s input=%s errors=%s",
                      _migration.get("read_granted"), _migration.get("input_granted"), _migration.get("errors"))
    except Exception:  # noqa: BLE001 -- never block startup on a migration
        _log.exception("session-access migration failed -- grants left unchanged")
    # Default-open model: clear deny rows the SYSTEM wrote as bookkeeping so
    # they stop reading as a security decision. A deny an actual person
    # authored is preserved.
    try:
        _deny_migration = terminal.migrate_deny_records_to_default_open()
        if _deny_migration.get("cleared") or _deny_migration.get("errors"):
            _log.info("session-access migration: cleared system deny rows=%s preserved=%s errors=%s",
                      _deny_migration.get("cleared"), _deny_migration.get("preserved_user_denies"),
                      _deny_migration.get("errors"))
    except Exception:  # noqa: BLE001 -- never block startup on a migration
        _log.exception("deny-record migration failed -- grants left unchanged")
    workspace_root = (config.session_lifecycle.allowed_cwd_roots[0]
                      if config.session_lifecycle.allowed_cwd_roots else "/")
    app = build_node_agent(node_id=args.node_id, terminal=terminal, token=credential, workspace_root=workspace_root)

    shell_capabilities = detect_shell_capabilities()
    wsl_available = detect_wsl_available()
    _log.info("terminal-windows-node-agent starting: node_id=%s shell_capabilities=%s wsl_available=%s",
             args.node_id, shell_capabilities, wsl_available)

    async def _heartbeat_task() -> None:
        await _heartbeat_loop(node_id=args.node_id, terminal=terminal, controller_url=args.controller_url,
                              token=credential, workspace_root=workspace_root,
                              interval_seconds=args.heartbeat_interval_seconds,
                              platform=PLATFORM_WINDOWS, session_backend=SESSION_BACKEND_WINDOWS_PTY,
                              shell_capabilities=shell_capabilities, wsl_available=wsl_available)

    async def run() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_heartbeat_task)
            server_config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
            server = uvicorn.Server(server_config)

            async def _shutdown_watch() -> None:
                await watch_for_shutdown(app, server)

            tg.start_soon(_shutdown_watch)
            await server.serve()
            # See node_agent.py's own main()/run() for why this cancel is
            # required -- identical reasoning, shared heartbeat-loop shape.
            tg.cancel_scope.cancel()

    anyio.run(run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
