# Tunnel connection reliability — self-healing + diagnostics

**Status: implemented and live.** Addresses the "ChatGPT tool call fails
with `Tunnel-client has not been seen for 300 seconds` while tmux/
terminal-mcp are both still alive" symptom.

## Root cause (real evidence, not guessed)

`journalctl --user -u terminal-mcp-tunnel`, this deployment's own 4-day
history: **1232** WARN/ERROR log lines. The overwhelming majority (80+
occurrences alone) are `poll failed; backing off` with the underlying
error

```
dial tcp: lookup api.openai.com on 127.0.0.53:53: server misbehaving
```

— systemd-resolved's local stub DNS resolver having a bad patch — plus a
handful of `network is unreachable` errors dialing an IPv6 address for
`api.openai.com`. `tunnel-client`'s own internal backoff/retry recovers
from nearly all of these within a poll cycle or two (`"poller recovered;
polling operational"` follows every one of them in the log), which is
exactly *why* `systemctl --user show terminal-mcp-tunnel.service
-p NRestarts` read **0** across those same 4 days: the OS process never
exits, so systemd's `Restart=` policy never even engages.

A long enough run of these failures can push the tunnel's time-since-
last-successful-control-plane-poll past whatever staleness threshold
OpenAI's platform uses for "has this tunnel been seen recently" — while
the process itself stays `active (running)` the entire time. That is a
**"hung but alive"** failure mode: systemd's own process-level restart
policy is structurally unable to detect it, because nothing ever crashes.

## What was built

| Piece | File |
|---|---|
| Diagnostics + self-healing decision logic (pure, unit-tested) | `terminal_mcp/tunnel_diagnostics.py` |
| `terminal-mcp-doctor connection` — read-only CLI | `terminal_mcp/doctor.py` |
| `terminal-mcp-tunnel-watchdog` — one check-and-act cycle | `terminal_mcp/tunnel_watchdog.py` |
| Hardened tunnel systemd unit (`Restart=always`, backoff, wide `StartLimit*`) | `deploy/systemd/terminal-mcp-tunnel.service.example` |
| Watchdog service + 45s timer | `deploy/systemd/terminal-mcp-tunnel-watchdog.{service,timer}.example` |
| Dashboard health banner (`Connected`/`Recovering`/`Local OK but tunnel stale`/`Local MCP down`) | `/dashboard/api/connection-health` route in `terminal_mcp/dashboard.py` |
| Tests | `tests/test_tunnel_diagnostics.py` |

Install/rollback steps: `deploy/systemd/README.md`'s "Tunnel connection
reliability" section.

### The one thing systemd's `Restart=` can't see

`check_tunnel_ready()` reads `tunnel-client`'s own `/metrics` endpoint
(`commands_poll_last_successful_timestamp_seconds`) — the tunnel's real,
self-reported "when did I last actually talk to the control plane"
timestamp, not just "is the process running". `terminal-mcp-tunnel-
watchdog` (run every 45s by the timer) compares that timestamp's age
against a threshold (default 150s, inside the task's own requested
90-180s window) and restarts **only** `terminal-mcp-tunnel.service` when
it's stale — never touching `terminal-mcp-http.service` or tmux for a
tunnel-side event, and vice versa for a local-MCP-side event.

### Four failure domains, told apart

1. **Local MCP down** — `mcp_local: unhealthy` → restart
   `terminal-mcp-http.service` (its own independent cooldown).
2. **Tunnel-client process down/hung** — `tunnel_process` inactive, or
   active with `tunnel_ready: stale`/`unknown` → restart
   `terminal-mcp-tunnel.service` (a `SubState=failed` unit gets
   `reset-failed` first).
3. **Host/network unavailable** — `network_dns_tls: fail` (a real DNS +
   TCP + TLS probe against `api.openai.com`, independent of the tunnel's
   own error strings).
4. **OpenAI/ChatGPT-side state issue** — everything local checks out
   (`mcp_local: healthy`, `tunnel_ready: ready`, `network_dns_tls: pass`)
   → `chatgpt_side: suspected-platform-side`. This code can only ever
   *rule out* local causes; it can never confirm or deny ChatGPT's own
   connector/session state.

### No infinite restart loop

Each target (tunnel, local MCP) has its own consecutive-restart counter
and cooldown, persisted in `~/.local/state/terminal-mcp/
tunnel_watchdog_state.json`. After 3 consecutive restarts with no
recovery, the watchdog stops restarting that target for 15 minutes and
reports `*_cooldown_active` instead — a genuine platform-side or sustained
network outage can never become an unbounded local restart loop. A real
recovery immediately clears the cooldown for future, unrelated events.

### Startup grace period (a bug found and fixed during live testing)

A freshly-(re)started `tunnel-client` process legitimately has no
heartbeat yet — its Prometheus gauge reads back as `0` (not absent)
before the first poll completes, which a naive `age = now - epoch`
computation would read as a multi-decade "staleness", triggering an
immediate, self-inflicted restart the instant a *legitimate* restart has
ever happened. This was caught live (see git history for the exact
before/after): `check_tunnel_ready()` now treats an epoch below a sane
floor as "no data yet" (`ready: unknown`, not a fake huge age), and
`decide_action()` additionally gives a just-started process
(`tunnel_process_uptime_sec` under a 60s grace window) time before
treating "no heartbeat yet" as a hang worth restarting for.

## Platform-side limitation (cannot be fixed locally)

If `terminal-mcp-doctor connection` reports every local check healthy —
`mcp_local: healthy`, `tunnel_ready: ready`, `network_dns_tls: pass`,
`chatgpt_side: suspected-platform-side` — and a ChatGPT tool call still
fails, the cause is on OpenAI's side: a connector/session state issue, or
(per public reports current as of this writing) specifically inside
ChatGPT's "Work" mode. This deployment cannot restart its way out of
that. If the failure is scoped to Work mode specifically and a normal
ChatGPT conversation with the same connector works, that is the practical
fallback while the platform-side issue exists — not a local root-cause
fix.

## Known limitations of this implementation

- **Test C (simulated host-wide network failure)** was covered as a unit
  test only (`check_network_dns_tls` with `socket.getaddrinfo` mocked to
  fail) — a real, host-wide network-down simulation was not performed
  live, to avoid disrupting this same host's other live, attended tmux
  sessions and services during testing.
- **Test D (genuine platform-side outage)** cannot be simulated for real
  either (would require OpenAI's control plane to actually be
  unreachable) — covered by unit tests that exercise the real cooldown/
  backoff logic (`decide_action`/`apply_decision`) end-to-end with
  injected diagnosis inputs, proving the loop-prevention guarantee
  without needing a real outage.
- The dashboard banner's poll (`/dashboard/api/connection-health`, every
  30s per open tab) skips the external DNS/TLS probe to stay cheap —
  it reflects `mcp_local`/`tunnel_process`/`tunnel_ready` only. The
  watchdog's own 45s cycle is what exercises the full check including
  the network probe.
