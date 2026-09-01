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
