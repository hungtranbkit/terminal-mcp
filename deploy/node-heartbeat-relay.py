#!/usr/bin/env python3
"""Relay one node's heartbeat to the controller on that node's behalf.

Exists for a specific, real situation: a node agent whose `--controller-url`
is baked into its command line and whose sessions are **children of the agent
process**, so it cannot be repointed without destroying live work. The Windows
agent (windows_agent.py + windows_backend.py) is exactly that shape --
`WindowsSessionBackend._sessions` is an in-process dict of live ConPTY
handles, unlike tmux, which is a separate server that outlives its parent.

Such a node is still perfectly *reachable*: the controller holds its token and
its `/v1/*` API answers normally. Only the outbound heartbeat goes to the old
address, and heartbeat freshness is what `status=online` means. So the node
reads fine by qualified name (`node/session`) while bare-name resolution and
`terminal_list_sessions` skip it as offline.

This process closes that gap without touching the node: it PULLS the node's
own real health/metrics/sessions over the API it already serves, and PUSHES
them to the controller's heartbeat endpoint as that node.

It is a bridge, not a fixture. Once the node is restarted with the right
controller URL (its launcher already updated), this becomes redundant and
should be stopped and removed.

**It never invents liveness.** If any pull fails, nothing is posted, so the
controller's own staleness detection marks the node offline exactly as it
would have. And the node's `/v1/health` must report the SAME node_id being
relayed -- pointed at the wrong endpoint, it refuses rather than reporting
some other machine's health under this node's name.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request

_log = logging.getLogger("node-heartbeat-relay")

# Reported verbatim to the controller. These describe the node's own platform
# and do not change between heartbeats, so they are configured rather than
# re-probed -- the node's real agent is the only thing that can probe them,
# and it is reporting to the wrong controller, which is why this exists.
_STATIC_FIELDS = ("platform", "session_backend", "shell_capabilities",
                  "wsl_available", "capabilities")


def _get(url: str, token: str, timeout: float, method: str = "GET") -> dict:
    request = urllib.request.Request(url, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def relay_once(*, node_id: str, node_endpoint: str, controller_url: str, token: str,
               static: dict, timeout: float = 10.0) -> bool:
    """One pull-then-push cycle. Returns True only if a heartbeat was
    actually accepted by the controller."""
    base = node_endpoint.rstrip("/")
    try:
        health = _get(f"{base}/v1/health", token, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log.warning("node %s unreachable (%s: %s) -- posting nothing, "
                     "controller will age it out on its own", node_id, type(exc).__name__, exc)
        return False

    reported = health.get("node_id")
    if reported != node_id:
        # Never report one machine's health under another's name.
        _log.error("refusing to relay: %s reports node_id=%r, expected %r",
                   base, reported, node_id)
        return False

    try:
        metrics = _get(f"{base}/v1/metrics", token, timeout)
        sessions = _get(f"{base}/v1/sessions", token, timeout).get("sessions", [])
        caps = _get(f"{base}/v1/capabilities/refresh", token, timeout, method="POST")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log.warning("node %s partial failure (%s: %s) -- posting nothing",
                     node_id, type(exc).__name__, exc)
        return False

    body = {
        "metrics": metrics,
        "tmux_session_count": len(sessions),
        # The real loop derives these from each row's pane_current_command,
        # which this node's listing does not carry; an empty map is what it
        # has always reported, not a value invented here.
        "agent_counts": {},
        "agent_types": list(caps.get("agent_types") or ()),
        "agent_version": caps.get("agent_version") or health.get("version"),
        "agent_generation": health.get("agent_generation"),
        "labels": [],
        **static,
    }
    url = f"{controller_url.rstrip('/')}/dashboard/api/nodes/{node_id}/heartbeat"
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            _log.info("relayed heartbeat for %s (%d sessions, HTTP %d)",
                      node_id, len(sessions), response.status)
            return True
    except (urllib.error.URLError, OSError) as exc:
        _log.warning("controller rejected/unreachable for %s (%s: %s)",
                     node_id, type(exc).__name__, exc)
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node-heartbeat-relay", description=__doc__)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--node-endpoint", required=True, help="e.g. http://192.168.1.250:8790")
    parser.add_argument("--controller-url", default="http://127.0.0.1:8766")
    parser.add_argument("--token-env", required=True,
                        help="Name of the env var holding this node's bearer token -- "
                             "the value is never taken on the command line, where it "
                             "would be visible in every process listing.")
    parser.add_argument("--interval-seconds", type=float, default=20.0)
    parser.add_argument("--platform", default="windows")
    parser.add_argument("--session-backend", default="windows_pty")
    parser.add_argument("--shell-capabilities", default="", help="comma-separated")
    parser.add_argument("--wsl-available", action="store_true")
    parser.add_argument("--capabilities", default="", help="comma-separated")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get(args.token_env)
    if not token:
        print(f"error: {args.token_env} is not set", file=sys.stderr)
        return 2

    def _split(raw: str) -> list[str]:
        return [item.strip() for item in raw.split(",") if item.strip()]

    static = {
        "platform": args.platform,
        "session_backend": args.session_backend,
        "shell_capabilities": _split(args.shell_capabilities),
        "wsl_available": bool(args.wsl_available),
        "capabilities": _split(args.capabilities),
    }
    assert set(static) == set(_STATIC_FIELDS)

    if args.once:
        return 0 if relay_once(node_id=args.node_id, node_endpoint=args.node_endpoint,
                               controller_url=args.controller_url, token=token,
                               static=static) else 1
    while True:
        try:
            relay_once(node_id=args.node_id, node_endpoint=args.node_endpoint,
                       controller_url=args.controller_url, token=token, static=static)
        except Exception:  # never let one bad cycle kill the relay
            _log.exception("unexpected error in relay cycle")
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    sys.exit(main())
