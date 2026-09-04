"""`terminal-mcp-doctor` -- read-only connection diagnostics CLI (task:
"biến kết nối thành self-healing, có chẩn đoán rõ"). Never restarts
anything itself (that's tunnel_watchdog.py's job) -- this only reports.

    terminal-mcp-doctor connection            human-readable
    terminal-mcp-doctor connection --json     machine-readable (one line)

Never prints the tunnel control-plane API key or any other secret -- the
diagnostics this reads (health endpoints, systemctl state, DNS/TLS probe
results, the persisted watchdog state file) never carry one in the first
place, so there is nothing to redact, by construction.
"""
from __future__ import annotations

import argparse
import json
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="terminal-mcp-doctor")
    subparsers = parser.add_subparsers(dest="command", required=True)

    connection = subparsers.add_parser("connection", help="Diagnose the OpenAI Secure MCP Tunnel connection")
    connection.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text")
    connection.add_argument("--stale-threshold", type=float, default=DEFAULT_STALE_THRESHOLD_SECONDS,
                            help="Seconds since the tunnel's last successful control-plane poll before it "
                                 "counts as stale (default: %(default)s)")
    connection.set_defaults(func=cmd_connection)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
