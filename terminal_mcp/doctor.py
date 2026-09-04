"""`terminal-mcp-doctor` -- read-only diagnostics CLI (task: "biến kết nối
thành self-healing, có chẩn đoán rõ", extended by the multi-node feature's
own task item 13). Never restarts or mutates anything itself (that's
tunnel_watchdog.py's job, or the dashboard's node drain button) -- this
only reports.

    terminal-mcp-doctor connection            human-readable tunnel diagnostics
    terminal-mcp-doctor connection --json     machine-readable (one line)
    terminal-mcp-doctor nodes                 human-readable node fleet diagnostics
    terminal-mcp-doctor nodes --json          machine-readable (one line)

Never prints the tunnel control-plane API key, a node's bearer token, or
any other secret -- the diagnostics this reads (health endpoints,
systemctl state, DNS/TLS probe results, the persisted watchdog state
file, the node registry) never carry one in the first place, so there is
nothing to redact, by construction."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .tunnel_diagnostics import DEFAULT_STALE_THRESHOLD_SECONDS, WatchdogState, default_state_path, diagnose


def cmd_connection(args: argparse.Namespace) -> int:
    state = WatchdogState.load(default_state_path())
    result = diagnose(stale_threshold=args.stale_threshold, state=state)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        _print_human(result)
    # Exit code reflects overall health -- 0 only when every local check is
    # clean, so this is usable as a scripted precondition too, not only for
    # a human to read.
    healthy = (
        result["mcp_local"] == "healthy"
        and result["tunnel_process"] == "active"
        and result["tunnel_ready"] == "ready"
        and result["network_dns_tls"] in ("pass", "unknown")
    )
    return 0 if healthy else 1


def _print_human(result: dict) -> None:
    print("Terminal MCP connection diagnostics")
    print(f"  mcp_local:            {result['mcp_local']}  ({result['mcp_local_detail']})")
    print(f"  tunnel_process:       {result['tunnel_process']} (sub_state={result['tunnel_process_sub_state']})")
    print(f"  tunnel_ready:         {result['tunnel_ready']}")
    age = result["last_heartbeat_age_sec"]
    print(f"  last_heartbeat_age_sec: {age if age is not None else 'unknown'}")
    print(f"  network_dns_tls:      {result['network_dns_tls']}  ({result['network_dns_tls_detail']})")
    print(f"  chatgpt_side:         {result['chatgpt_side']}")
    if result["last_recovery_action"] and result["last_recovery_action"] != "none":
        print(f"  last_recovery_action: {result['last_recovery_action']} "
             f"({result['last_recovery_action_reason']}) at {result['last_recovery_action_at']}")
    else:
        print("  last_recovery_action: none")
    print(f"  recommended_action:   {result['recommended_action']}")


def cmd_nodes(args: argparse.Namespace) -> int:
    """Multi-node fleet diagnostics (task item 13): read-only against the
    real, live node registry (the same default nodes.db the running
    terminal-mcp-http service uses) plus a real, best-effort test-
    connection probe to each configured remote node. This process builds
    its OWN short-lived ControllerService (registering local + any
    configured remote nodes exactly like server_http.py's own startup
    does) rather than talking to the running service -- so it works even
    when the service is down, and its remote test-connection probes are
    always fresh, never a cached value from the running service."""
    from .agent_availability import available_agent_types
    from .config import load_config
    from .controller import ControllerService
    from .core import TerminalService
    from .node_client import LocalNodeClient
    from .node_registry import NodeRegistry

    config = load_config(args.config)
    terminal = TerminalService(config)
    registry = NodeRegistry(overload_thresholds=config.nodes.overload_thresholds,
                            heartbeat_thresholds=config.nodes.heartbeat_thresholds)
    workspace_root = (config.session_lifecycle.allowed_cwd_roots[0]
                      if config.session_lifecycle.allowed_cwd_roots else "/")
    controller = ControllerService(registry, local_client=LocalNodeClient(terminal), local_workspace_root=workspace_root)

    skipped = []
    for remote in config.nodes.remote_nodes:
        token = os.environ.get(remote.token_env)
        if not token:
            skipped.append({"node_id": remote.node_id, "reason": f"environment variable {remote.token_env} is not set"})
            continue
        controller.register_remote_node(remote.node_id, display_name=remote.display_name, hostname=remote.hostname,
                                        endpoint=remote.endpoint, token=token, max_sessions=remote.max_sessions,
                                        timeout=remote.timeout_seconds)

    try:
        items = terminal.tmux.list_sessions()
    except Exception:  # noqa: BLE001 -- a metrics refresh must never crash the CLI
        items = []
    agent_counts: dict[str, int] = {}
    for item in items:
        command = (item.pane_current_command or "").casefold()
        if command:
            agent_counts[command] = agent_counts.get(command, 0) + 1
    agent_types = available_agent_types(config.session_lifecycle.launch_commands)
    controller.refresh_local_heartbeat(tmux_session_count=len(items), agent_counts=agent_counts,
                                       agent_types=agent_types, agent_version=None)

    rows = []
    for node in controller.list_nodes():
        row = {
            "id": node.id, "display_name": node.display_name, "status": node.status,
            "capacity_status": node.capacity_status, "overload_reasons": list(node.overload_reasons),
            "draining": node.draining, "tmux_session_count": node.tmux_session_count,
            "last_heartbeat_at": node.last_heartbeat_at,
            "cpu_percent": node.cpu_percent_smoothed if node.cpu_percent_smoothed is not None else node.cpu_percent,
            "ram_percent": node.ram_percent_smoothed if node.ram_percent_smoothed is not None else node.ram_percent,
            # Multi-node Windows support -- capability report (task's own
            # explicit field list).
            "platform": node.platform, "session_backend": node.session_backend,
            "shell_capabilities": list(node.shell_capabilities), "wsl_available": node.wsl_available,
            "claude_available": "claude" in node.agent_types, "codex_available": "codex" in node.agent_types,
        }
        if node.id != controller.local_node_id:
            row["test_connection"] = controller.test_connection(node.id)
        rows.append(row)

    result = {"nodes": rows, "skipped_remote_nodes": skipped}
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        _print_nodes_human(result)

    healthy = not skipped and all(row["status"] == "online" for row in rows)
    return 0 if healthy else 1


def _print_nodes_human(result: dict) -> None:
    print("Terminal MCP node fleet")
    for row in result["nodes"]:
        marker = "*" if row["id"] == "local" else " "
        os_icon = "win" if row["platform"] == "windows" else "linux"
        print(f"  [{marker}] {row['id']} ({row['display_name']}) [{os_icon}/{row['session_backend']}]: "
             f"status={row['status']} capacity={row['capacity_status']} sessions={row['tmux_session_count']} "
             f"cpu={row['cpu_percent']} ram={row['ram_percent']} draining={row['draining']}")
        capabilities = []
        if row["claude_available"]:
            capabilities.append("claude")
        if row["codex_available"]:
            capabilities.append("codex")
        if row["wsl_available"]:
            capabilities.append("wsl")
        if row["shell_capabilities"]:
            capabilities.append("shells=" + ",".join(row["shell_capabilities"]))
        if capabilities:
            print(f"        capabilities: {', '.join(capabilities)}")
        if row["overload_reasons"]:
            print(f"        overload_reasons: {', '.join(row['overload_reasons'])}")
        if "test_connection" in row:
            tc = row["test_connection"]
            print(f"        test_connection: ok={tc.get('ok')} latency_ms={tc.get('latency_ms')} "
                 f"detail={tc.get('detail')}")
    if result["skipped_remote_nodes"]:
        print("  Remote nodes declared in config.yaml but not registered (token not set):")
        for skip in result["skipped_remote_nodes"]:
            print(f"    - {skip['node_id']}: {skip['reason']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="terminal-mcp-doctor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    connection = subparsers.add_parser("connection", help="Diagnose the OpenAI Secure MCP Tunnel connection")
    connection.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text")
    connection.add_argument("--stale-threshold", type=float, default=DEFAULT_STALE_THRESHOLD_SECONDS,
                            help="Seconds since the tunnel's last successful control-plane poll before it "
                                 "counts as stale (default: %(default)s)")
    connection.set_defaults(func=cmd_connection)

    nodes = subparsers.add_parser("nodes", help="Diagnose the multi-node fleet (registry, capacity, remote reachability)")
    nodes.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text")
    nodes.add_argument("--config", default=None, help="Path to config.yaml (default: the usual lookup)")
    nodes.set_defaults(func=cmd_nodes)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
