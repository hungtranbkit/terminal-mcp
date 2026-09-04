# terminal-mcp-http.service (P1 hardening item #12)

`terminal-mcp-http.service.example` is the systemd user unit for the
loopback HTTP/dashboard server (`terminal-mcp-http`), with resource limits
and sandboxing added on top of the original minimal unit. See the comments
in the file itself for what each directive does.

**Important, found by testing this live, precisely (not guessed)**: this
host has `apparmor_restrict_unprivileged_userns` enabled (`cat /proc/sys/
kernel/apparmor_restrict_unprivileged_userns` → `1`), which blocks an
unprivileged systemd `--user` instance from creating the mount/user
namespaces most `Protect*=`/`Restrict*=`/`LockPersonality=` directives
need -- confirmed independent of systemd too (`unshare --mount` fails
with "Operation not permitted" for this user). `CapabilityBoundingSet=`
and that whole family failed identically here (`Failed to drop
capabilities: Operation not permitted`; systemd.exec(5) documents that
most of them implicitly restrict the capability bounding set as part of
their enforcement, not only the ones that look capability-related by
name). `NoNewPrivileges=`, the pure-mount-namespace `ProtectSystem=`/
`ProtectHome=`, and (unlike the above) `SystemCallFilter=@system-service`
(seccomp via NNP -- a different kernel mechanism, needs no new namespace)
all work fine on this host and are included. If your host does not set
that AppArmor restriction, or runs systemd `--system` (root) rather than
`--user`, the rest of the `Protect*`/`Restrict*` family is very likely
safe to add too -- see the file's own comments for exactly which ones and
how to test them one at a time.

`SystemCallFilter=@system-service` specifically was validated on a
disposable staging systemd unit (same binary, isolated port/state dir)
before being adopted on the real service -- see the file's comment for
exactly what was exercised.

Install/update:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/terminal-mcp-http.service.example \
   ~/.config/systemd/user/terminal-mcp-http.service
# Edit WorkingDirectory/Environment/ExecStart paths if this repo isn't at
# /home/dell/workspace/terminal-mcp.
systemctl --user daemon-reload
systemctl --user restart terminal-mcp-http.service
systemctl --user status terminal-mcp-http.service
```

After restarting, verify tmux access specifically survived the sandboxing
(the one failure mode that would otherwise be silent until something tried
to observe/control a session):

```bash
curl -s http://127.0.0.1:8766/health/ready   # {"status":"ready", "checks": {"tmux": {"ok": true, ...
curl -s http://127.0.0.1:8766/dashboard/api/sessions
```

If `tmux` ever reports `ok: false` after adopting this unit, the most
likely cause is the real tmux socket directory (commonly
`/tmp/tmux-$UID/`) has moved outside what `ProtectSystem=full`/
`ProtectHome=read-only` leave reachable in your environment -- check
`tmux display-message -p '#{socket_path}'` and, if it differs from the
default assumption here (a plain `/tmp` path, which this unit's
filesystem restrictions never touch), adjust or drop the offending
directive rather than guessing.

`cloudflared-*.service` and `terminal-mcp-tunnel.service` (the two tunnel
processes) are third-party binaries outside this project's own codebase
and were deliberately left un-sandboxed here -- this pass is scoped to
Terminal MCP's own service.

## Rollback

If `SystemCallFilter=@system-service` (or anything else in this unit)
ever causes a functional regression, drop just that one line first --
everything else in the file was already independently live-verified
before it was added:

```bash
sed -i '/^SystemCallFilter=/d' ~/.config/systemd/user/terminal-mcp-http.service
systemctl --user daemon-reload
systemctl --user restart terminal-mcp-http.service
```

To roll back to the version of this unit from immediately before
`SystemCallFilter=` was added (commit `61a7413`, resource limits +
`ProtectSystem`/`ProtectHome`/`NoNewPrivileges` only, no seccomp filter):

```bash
git show 61a7413:deploy/systemd/terminal-mcp-http.service.example > ~/.config/systemd/user/terminal-mcp-http.service
systemctl --user daemon-reload
systemctl --user restart terminal-mcp-http.service
```

To roll back to the original, unhardened unit (before any of item #12,
commit `cf517f4` -- resource limits, sandboxing, and `SystemCallFilter=`
all removed):

```bash
cat > ~/.config/systemd/user/terminal-mcp-http.service <<'EOF'
[Unit]
Description=Terminal MCP loopback Streamable HTTP server
After=default.target

[Service]
Type=simple
WorkingDirectory=/home/dell/workspace/terminal-mcp
Environment=TERMINAL_MCP_CONFIG=/home/dell/workspace/terminal-mcp/config.yaml
ExecStart=/home/dell/workspace/terminal-mcp/.venv/bin/terminal-mcp-http
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user restart terminal-mcp-http.service
```

After any rollback, re-verify with the same two commands from "After
restarting" above (`/health/ready` and `/dashboard/api/sessions`).

---

# Tunnel connection reliability (terminal-mcp-tunnel + watchdog)

Real, evidence-based root cause (this host's own `journalctl --user -u
terminal-mcp-tunnel` history) for the "ChatGPT tool call fails with
`Tunnel-client has not been seen for 300 seconds` while tmux/terminal-mcp
are both still alive" symptom: intermittent local DNS resolution failures
against the systemd-resolved stub (`dial tcp: lookup api.openai.com on
127.0.0.53:53: server misbehaving`, 80+ times in 4 days) plus occasional
IPv6 route-unreachable errors reaching `api.openai.com`. `tunnel-client`'s
own internal backoff/retry recovers from nearly all of these within a
poll cycle or two -- the OS process never exits, so `NRestarts` stayed 0
across the same 4 days and systemd's `Restart=` policy never even
engaged. A long enough run of these CAN push the tunnel's time-since-
last-successful-control-plane-poll past whatever staleness threshold
OpenAI's platform uses, while the process itself stays `active (running)`
the entire time -- a "hung but alive" failure mode process-level restart
policy is structurally unable to detect. See
`terminal_mcp/tunnel_diagnostics.py`'s own module docstring for the full
evidence and reasoning; this section is just the deploy/rollback steps.

Two independent pieces:

1. **`terminal-mcp-tunnel.service.example`** -- the existing tunnel unit,
   hardened for fast, bounded, non-permanent restart recovery from an
   actual crash/exit (`Restart=always`, exponential backoff via
   `RestartSteps=`/`RestartMaxDelaySec=`, and a much wider
   `StartLimitIntervalSec=`/`StartLimitBurst=` window so a burst of
   restarts during a bad network patch can never leave the unit
   permanently `failed`, requiring a human to run `systemctl --user
   reset-failed`).
2. **`terminal-mcp-tunnel-watchdog.service.example` +
   `.timer.example`** -- a lightweight, independent watchdog that runs
   every 45s and checks APPLICATION-level liveness (the tunnel's own
   last-successful-poll timestamp via its `/metrics` endpoint, not just
   "is the process running") -- the one thing systemd's `Restart=` can
   never see, since the process never exits during a hang. Restarts
   *only* the affected unit (`terminal-mcp-tunnel.service` for a stale/
   dead tunnel, `terminal-mcp-http.service` for a locally-unhealthy MCP
   server -- never both for the same event) and never touches tmux at
   all. Independently cooldown-limited per target (default: after 3
   consecutive restarts with no recovery, stop restarting for 15 minutes
   and report `cooldown_active`/`suspected-platform-side` instead) so a
   genuine OpenAI-side or sustained network outage can never become an
   unbounded local restart loop.

Install/update:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/terminal-mcp-tunnel.service.example \
   ~/.config/systemd/user/terminal-mcp-tunnel.service
cp deploy/systemd/terminal-mcp-tunnel-watchdog.service.example \
   ~/.config/systemd/user/terminal-mcp-tunnel-watchdog.service
cp deploy/systemd/terminal-mcp-tunnel-watchdog.timer.example \
   ~/.config/systemd/user/terminal-mcp-tunnel-watchdog.timer
# Edit the watchdog .service's ExecStart path if this repo isn't at
# /home/dell/workspace/terminal-mcp (same convention as terminal-mcp-http
# .service.example above).
systemctl --user daemon-reload
systemctl --user restart terminal-mcp-tunnel.service
systemctl --user enable --now terminal-mcp-tunnel-watchdog.timer
```

Verify:

```bash
systemctl --user status terminal-mcp-tunnel.service terminal-mcp-tunnel-watchdog.timer
terminal-mcp-doctor connection            # human-readable
terminal-mcp-doctor connection --json     # machine-readable, exit 0 iff every local check passed
```

`terminal-mcp-doctor connection` never restarts anything itself (read-only
diagnostics); `terminal-mcp-tunnel-watchdog` (invoked by the timer, or
manually with `--dry-run` to see what it WOULD do without acting) is the
only piece that ever runs `systemctl --user restart/start/reset-failed`,
and only on `terminal-mcp-tunnel.service`/`terminal-mcp-http.service`.

**Platform-side limitation** (task-documented, not fixable locally): if
`terminal-mcp-doctor connection` reports every local check healthy
(`mcp_local: healthy`, `tunnel_ready: ready`, `network_dns_tls: pass`) —
`chatgpt_side` will read `suspected-platform-side` — and ChatGPT still
can't reach the tool, the cause is on OpenAI's side (a connector/session
state issue, or -- per public reports as of this writing -- specifically
inside ChatGPT's "Work" mode) and not something this deployment can
restart its way out of. If the failure is scoped to Work mode specifically
and a normal ChatGPT conversation with the same connector works, that is
the practical fallback -- not a local root-cause fix, just a workaround
while the platform-side issue exists.

Rollback (tunnel unit only -- to the original, unhardened version):

```bash
cat > ~/.config/systemd/user/terminal-mcp-tunnel.service <<'EOF'
[Unit]
Description=OpenAI Secure MCP Tunnel for Terminal MCP
After=network-online.target terminal-mcp-http.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.config/terminal-mcp/tunnel.env
ExecStart=%h/.local/bin/tunnel-client run --profile terminal-mcp
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user restart terminal-mcp-tunnel.service
systemctl --user disable --now terminal-mcp-tunnel-watchdog.timer
```
