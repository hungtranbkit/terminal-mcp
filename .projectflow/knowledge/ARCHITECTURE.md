# Architecture

## Processes
- **Controller** -- `terminal-mcp-http.service` on m910, port 8766. Serves the
  dashboard, the HTTP API and the MCP endpoint at `/mcp`.
- **Node agents** -- `/v1/*` on port 8790, one per node. The controller talks
  to them; they are never restarted by a controller deploy.
- **Sessions** -- tmux (Linux/macOS) and ConPTY (Windows), owned by the node,
  NOT children of the controller.

## Why a controller restart is safe
`KillMode=process` plus cgroup isolation: systemd stops the controller process
without reaching into the session processes. Verified by pane PIDs being
unchanged across every restart in this project's history.

## Boundaries
- `_read_guard` = Cloudflare Access check only.
- `_mutation_guard` = mutations_enabled + Origin/CSRF + Access.
- A loopback 403 means the route is registered and the gate works. A public
  302 is the Access login challenge.

## Storage
SQLite with WAL. Migrations are tracked by `PRAGMA user_version`, additive and
idempotent (`schema.py`). Nothing destructive runs on upgrade.
