"""`terminal-mcp-tunnel-watchdog` -- one check-and-act cycle, run every
30-60s by terminal-mcp-tunnel-watchdog.timer (systemd --user). Detects
the "tunnel-client process alive but its control-plane poll has gone
stale" failure mode systemd's own Restart= policy structurally cannot
see (see tunnel_diagnostics.py's module docstring for the real log
evidence behind this), and restarts ONLY the affected service --
terminal-mcp-tunnel.service for a stale/dead tunnel, terminal-mcp-
http.service for a locally-unhealthy MCP server, NEVER both for the same
event, and NEVER anything that touches tmux (this module holds no tmux
reference at all -- restarting either systemd unit is already confirmed
non-disruptive to tmux sessions, see terminal_mcp-production-ops
knowledge and this task's own live verification).

Logging is deliberately rate-limited (task item 8: "log rotation/size
nhỏ"): a quiet run (nothing changed, nothing acted on) prints nothing at
all; a run that takes action, or that transitions between healthy/
unhealthy, prints exactly one JSON line -- readable via `journalctl
--user -u terminal-mcp-tunnel-watchdog.service`.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .tunnel_diagnostics import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_COOLDOWN_TRIGGER_COUNT,
    DEFAULT_MCP_UNIT,
    DEFAULT_STALE_THRESHOLD_SECONDS,
    DEFAULT_TUNNEL_UNIT,
    WatchdogState,
    apply_decision,
    decide_action,
    default_state_path,
    diagnose,
)


def run_once(*, stale_threshold: float = DEFAULT_STALE_THRESHOLD_SECONDS,
            cooldown_trigger_count: int = DEFAULT_COOLDOWN_TRIGGER_COUNT,
            cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
            tunnel_unit: str = DEFAULT_TUNNEL_UNIT, mcp_unit: str = DEFAULT_MCP_UNIT,
            state_path=None, dry_run: bool = False) -> dict:
    """The whole cycle as one importable, directly-testable function --
    tunnel_watchdog.main() is a thin argparse+print wrapper around this.
    Returns a summary dict (also what gets logged) regardless of whether
    anything noteworthy happened, so callers/tests can always inspect the
    outcome; only main()'s own printing is rate-limited."""
    state_path = default_state_path() if state_path is None else state_path
    state = WatchdogState.load(state_path)
    previous_action = state.last_action

    diag = diagnose(stale_threshold=stale_threshold, state=state)
    decision = decide_action(diag, state, cooldown_trigger_count=cooldown_trigger_count,
                             cooldown_seconds=cooldown_seconds)

    if dry_run:
        summary_state = state
        acted = False
    else:
        run_fn = None  # apply_decision defaults to real subprocess.run
        state = apply_decision(decision, state, tunnel_unit=tunnel_unit, mcp_unit=mcp_unit,
                               cooldown_trigger_count=cooldown_trigger_count,
                               cooldown_seconds=cooldown_seconds, run=run_fn)
        state.last_heartbeat_age_sec = diag["last_heartbeat_age_sec"]
        state.save(state_path)
        summary_state = state
        acted = decision.action != "none"

    was_unhealthy = diag["mcp_local"] != "healthy" or diag["tunnel_ready"] != "ready"
    transitioned = (previous_action != "none") or (decision.action != "none") or was_unhealthy

    return {
        "diagnosis": diag,
        "decision": {"action": decision.action, "reason": decision.reason},
        "acted": acted,
        "noteworthy": transitioned,
        "state": {
            "tunnel_restart_count": summary_state.tunnel_restart_count,
            "mcp_restart_count": summary_state.mcp_restart_count,
            "consecutive_tunnel_restarts": summary_state.consecutive_tunnel_restarts,
            "consecutive_mcp_restarts": summary_state.consecutive_mcp_restarts,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="terminal-mcp-tunnel-watchdog")
    parser.add_argument("--stale-threshold", type=float, default=DEFAULT_STALE_THRESHOLD_SECONDS)
    parser.add_argument("--cooldown-trigger-count", type=int, default=DEFAULT_COOLDOWN_TRIGGER_COUNT)
    parser.add_argument("--cooldown-seconds", type=float, default=DEFAULT_COOLDOWN_SECONDS)
    parser.add_argument("--tunnel-unit", default=DEFAULT_TUNNEL_UNIT)
    parser.add_argument("--mcp-unit", default=DEFAULT_MCP_UNIT)
    parser.add_argument("--dry-run", action="store_true", help="Diagnose and decide, but never actually restart anything")
    parser.add_argument("--verbose", action="store_true", help="Always print the outcome, even a fully quiet/healthy one")
    args = parser.parse_args(argv)

    result = run_once(
        stale_threshold=args.stale_threshold, cooldown_trigger_count=args.cooldown_trigger_count,
        cooldown_seconds=args.cooldown_seconds, tunnel_unit=args.tunnel_unit, mcp_unit=args.mcp_unit,
        dry_run=args.dry_run,
    )
    if result["noteworthy"] or args.verbose or args.dry_run:
        print(json.dumps({"time": time.time(), **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
