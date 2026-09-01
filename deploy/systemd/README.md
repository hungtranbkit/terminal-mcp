# terminal-mcp-http.service (P1 hardening item #12)

`terminal-mcp-http.service.example` is the systemd user unit for the
loopback HTTP/dashboard server (`terminal-mcp-http`), with resource limits
and sandboxing added on top of the original minimal unit. See the comments
in the file itself for what each directive does.

**Important, found by testing this live**: this unit's own deployment
runs inside a nested/sandboxed container where systemd's own capability-
dropping is restricted (`Failed to drop capabilities: Operation not
permitted`) -- so `CapabilityBoundingSet=` and most of the `Protect*`/
`Restrict*`/`LockPersonality=` family (they implicitly restrict
capabilities too, not only the ones that look capability-related by
name, per systemd.exec(5)) had to be left out here; only `NoNewPrivileges=`
plus the pure-mount-namespace `ProtectSystem=`/`ProtectHome=` survived.
If your host is NOT itself nested in another container/sandbox, the extra
directives are very likely safe to add -- see the file's own comments for
exactly which ones and how to test them one at a time.

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
