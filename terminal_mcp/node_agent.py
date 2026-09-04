"""`terminal-node-agent` -- the lightweight process a WORKER node (e.g. a
future M910) runs. Wraps ONE local TerminalService (the exact same class
Dell's own controller uses for its local node -- zero duplicated tmux/
permission/audit logic) and exposes ONLY the narrow operation set
node_client.py's NodeClient Protocol defines (task item 2: "Agent chỉ
expose các operation cần thiết... Không expose arbitrary shell
endpoint") -- no raw shell, no arbitrary command execution, no tmux
socket access from outside this process.

Every route (except /v1/health, a bare liveness probe) requires
`Authorization: Bearer <token>` matching this agent's own configured
shared secret (env `TERMINAL_MCP_NODE_TOKEN`, or `--token-file`) --
mirrors this project's existing CONTROL_PLANE_API_KEY-via-env convention
(tunnel-client) rather than inventing a new auth style. A missing/wrong
token is refused (401) before touching TerminalService at all.

Also runs a background heartbeat loop: every `heartbeat_interval_seconds`
(default 20s), POSTs this node's own collected metrics
(host_metrics.collect) to the controller's heartbeat-receiving route
(dashboard.py's /dashboard/api/nodes/{node_id}/heartbeat), authenticated
with the SAME shared token. If the controller is unreachable, this loop
just keeps retrying -- tmux/Claude/Codex sessions on THIS node are
completely unaffected either way (task item 2: "Nếu controller mất kết
nối, tmux/Claude/Codex trên node vẫn tiếp tục sống" -- this process
never blocks any session operation on the heartbeat loop's own success).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import anyio
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__, host_metrics
from .config import load_config
from .core import TerminalService
from .node_client import LocalNodeClient

_log = logging.getLogger(__name__)


def _read_token(token: str | None, token_file: str | None) -> str:
    if token:
        return token
    env_token = os.environ.get("TERMINAL_MCP_NODE_TOKEN")
    if env_token:
        return env_token
    if token_file:
        return Path(token_file).expanduser().read_text().strip()
    raise SystemExit("no node token configured -- set TERMINAL_MCP_NODE_TOKEN, or pass --token/--token-file")


def _auth_ok(request: Request, expected_token: str) -> bool:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return False
    presented = header[len("Bearer "):]
    # Constant-time comparison -- a timing side-channel on this specific
    # check would leak the shared secret one byte at a time to a network
    # attacker; every other auth comparison in this project (webauth.py)
    # already uses the same discipline for the same reason.
    import hmac
    return hmac.compare_digest(presented, expected_token)


def build_node_agent(*, node_id: str, terminal: TerminalService, token: str,
                     workspace_root: str = "/") -> Starlette:
    client = LocalNodeClient(terminal)

    def require_auth(request: Request) -> JSONResponse | None:
        if not _auth_ok(request, token):
            return JSONResponse({"error": "UNAUTHORIZED"}, status_code=401)
        return None

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "node_id": node_id, "version": __version__})

    async def metrics(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        result = await anyio.to_thread.run_sync(lambda: client.metrics())
        return JSONResponse(result)

    async def list_sessions(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        result = await anyio.to_thread.run_sync(client.list_sessions)
        return JSONResponse(result)

    async def session_status(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        name = request.path_params["name"]
        result = await anyio.to_thread.run_sync(lambda: client.status(name))
        return JSONResponse(result)

    async def session_tail(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        name = request.path_params["name"]
        lines_raw = request.query_params.get("lines")
        lines = int(lines_raw) if lines_raw not in (None, "", "None") else None
        ansi = request.query_params.get("ansi") == "1"
        result = await anyio.to_thread.run_sync(lambda: client.tail(name, lines, ansi=ansi))
        return JSONResponse(result)

    async def session_capture(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        name = request.path_params["name"]
        start_raw = request.query_params.get("start_line")
        start_line = int(start_raw) if start_raw not in (None, "", "None") else None
        result = await anyio.to_thread.run_sync(lambda: client.capture(name, start_line))
        return JSONResponse(result)

    async def session_send(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        name = request.path_params["name"]
        try:
            body = await request.json()
        except ValueError:
            body = {}
        result = await anyio.to_thread.run_sync(lambda: client.send_text(
            name, body.get("text", ""), bool(body.get("press_enter", False)), bool(body.get("dry_run", False)),
            idempotency_key=body.get("idempotency_key"), origin=body.get("origin"),
            trace_id=body.get("trace_id"), parent_turn_id=body.get("parent_turn_id"),
            depth=int(body.get("depth") or 0),
        ))
        return JSONResponse(result)

    async def session_send_keys(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        name = request.path_params["name"]
        try:
            body = await request.json()
        except ValueError:
            body = {}
        result = await anyio.to_thread.run_sync(
            lambda: client.send_keys(name, list(body.get("keys", [])), bool(body.get("confirm_sensitive", False)))
        )
        return JSONResponse(result)

    async def input_context(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        session = request.query_params.get("session")
        binding = request.query_params.get("binding")
        result = await anyio.to_thread.run_sync(lambda: client.input_context(session, binding))
        return JSONResponse(result)

    async def create_session(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        result = await anyio.to_thread.run_sync(lambda: client.create_session(
            body.get("name", ""), body.get("agent_type", "shell"), body.get("cwd"),
            initial_prompt=body.get("initial_prompt"), grant_mode=body.get("grant_mode", "none"),
            binding=body.get("binding"), requested_by=body.get("requested_by"),
        ))
        return JSONResponse(result)

    async def detach_session(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        result = await anyio.to_thread.run_sync(lambda: client.detach_session(request.path_params["name"]))
        return JSONResponse(result)

    async def delete_session(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        result = await anyio.to_thread.run_sync(lambda: client.delete_session(request.path_params["name"]))
        return JSONResponse(result)

    async def kill_session(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        result = await anyio.to_thread.run_sync(lambda: client.kill_session(
            request.path_params["name"], body.get("confirm_name", ""), requested_by=body.get("requested_by"),
        ))
        return JSONResponse(result)

    async def reopen_session(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        result = await anyio.to_thread.run_sync(lambda: client.reopen_session(
            request.path_params["name"], agent_type=body.get("agent_type"), cwd=body.get("cwd"),
            grant_mode=body.get("grant_mode", "none"), requested_by=body.get("requested_by"),
        ))
        return JSONResponse(result)

    async def killed_sessions(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        result = await anyio.to_thread.run_sync(client.list_killed_sessions)
        return JSONResponse(result)

    routes = [
        Route("/v1/health", health, methods=["GET"]),
        Route("/v1/metrics", metrics, methods=["GET"]),
        Route("/v1/sessions", list_sessions, methods=["GET"]),
        Route("/v1/sessions", create_session, methods=["POST"]),
        Route("/v1/sessions/{name}/status", session_status, methods=["GET"]),
        Route("/v1/sessions/{name}/tail", session_tail, methods=["GET"]),
        Route("/v1/sessions/{name}/capture", session_capture, methods=["GET"]),
        Route("/v1/sessions/{name}/send", session_send, methods=["POST"]),
        Route("/v1/sessions/{name}/send-keys", session_send_keys, methods=["POST"]),
        Route("/v1/input-context", input_context, methods=["GET"]),
        Route("/v1/sessions/{name}/detach", detach_session, methods=["POST"]),
        Route("/v1/sessions/{name}", delete_session, methods=["DELETE"]),
        Route("/v1/sessions/{name}/kill", kill_session, methods=["POST"]),
        Route("/v1/sessions/{name}/reopen", reopen_session, methods=["POST"]),
        Route("/v1/killed-sessions", killed_sessions, methods=["GET"]),
    ]
    return Starlette(routes=routes)


async def _heartbeat_loop(*, node_id: str, terminal: TerminalService, controller_url: str, token: str,
                          workspace_root: str, interval_seconds: float) -> None:
    """Runs forever (until the process exits) -- a single failed push is
    logged and retried next cycle, never raised past this loop, so a
    controller outage can never crash the node agent (task item 2's own
    "controller mất kết nối, node vẫn sống" guarantee applies to THIS
    process's own survival too, not only to tmux underneath it)."""
    url = f"{controller_url.rstrip('/')}/dashboard/api/nodes/{node_id}/heartbeat"
    while True:
        try:
            metrics = host_metrics.collect(workspace_path=workspace_root)
            sessions = await anyio.to_thread.run_sync(terminal.terminal_list_sessions)
            session_rows = sessions.get("sessions", [])
            agent_counts: dict[str, int] = {}
            for row in session_rows:
                command = (row.get("pane_current_command") or "").casefold()
                if command:
                    agent_counts[command] = agent_counts.get(command, 0) + 1
            agent_types = ["shell"] + [agent_type for agent_type, _command in terminal.config.session_lifecycle.launch_commands]
            body = json.dumps({
                "metrics": metrics.__dict__, "tmux_session_count": len(session_rows),
                "agent_counts": agent_counts, "agent_types": agent_types,
                "agent_version": __version__, "labels": [],
            }).encode()
            request = urllib.request.Request(url, data=body, method="POST")
            request.add_header("Authorization", f"Bearer {token}")
            request.add_header("Content-Type", "application/json")
            await anyio.to_thread.run_sync(lambda: urllib.request.urlopen(request, timeout=10))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _log.warning("heartbeat push to controller failed (will retry): %s: %s", type(exc).__name__, exc)
        except Exception:  # noqa: BLE001 -- this loop must never die
            _log.exception("unexpected error in heartbeat loop -- will retry next cycle")
        await anyio.sleep(interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="terminal-node-agent")
    parser.add_argument("--node-id", required=True, help="This node's own id, as registered on the controller")
    parser.add_argument("--controller-url", required=True, help="e.g. http://192.168.1.10:8766")
    parser.add_argument("--config", default=None, help="Path to this node's own config.yaml")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address -- LAN/private only, never 0.0.0.0 "
                        "without a firewall/VPN boundary in front of it (task item 2)")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--token", default=None)
    parser.add_argument("--token-file", default=None)
    parser.add_argument("--heartbeat-interval-seconds", type=float, default=20.0)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = _read_token(args.token, args.token_file)
    config = load_config(args.config)
    terminal = TerminalService(config)
    app = build_node_agent(node_id=args.node_id, terminal=terminal, token=token,
                           workspace_root=(config.session_lifecycle.allowed_cwd_roots[0]
                                          if config.session_lifecycle.allowed_cwd_roots else "/"))

    workspace_root = (config.session_lifecycle.allowed_cwd_roots[0]
                      if config.session_lifecycle.allowed_cwd_roots else "/")

    async def _heartbeat_task() -> None:
        # start_soon only supports positional args -- this closure is
        # just that adapter, keeping _heartbeat_loop's own signature
        # keyword-only (clearer at every OTHER call site, e.g. tests).
        await _heartbeat_loop(node_id=args.node_id, terminal=terminal, controller_url=args.controller_url,
                              token=token, workspace_root=workspace_root,
                              interval_seconds=args.heartbeat_interval_seconds)

    async def run() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_heartbeat_task)
            server_config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
            server = uvicorn.Server(server_config)
            await server.serve()

    anyio.run(run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
