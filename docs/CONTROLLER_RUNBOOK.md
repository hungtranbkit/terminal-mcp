# Terminal MCP — controller runbook

> **Primary controller: `m910` (`mesflow@mesflow-ThinkCentre-M910q`, 192.168.1.109)**
> since 2026-09-10. The Dell Latitude that previously ran it is **retired** —
> its units are stopped and disabled and it is no longer a dependency.

## 1. Where things live

| | Path on m910 |
| --- | --- |
| Code | `/home/mesflow/terminal-mcp` (git, clean checkout) |
| venv | `/home/mesflow/terminal-mcp/.venv` |
| Config | `/home/mesflow/.config/terminal-mcp/config.yaml` |
| Node tokens | `/home/mesflow/.config/terminal-mcp/node-agent-tokens.env` (0600) |
| Runtime state | `/home/mesflow/.local/state/terminal-mcp/*.db` |
| Tunnel creds | `/home/mesflow/.cloudflared/` (0400) |
| Tunnel profile | `/home/mesflow/.config/tunnel-client/terminal-mcp.yaml` |

**Config lives OUTSIDE the repo on purpose.** Keeping a host-specific
`config.yaml` in-tree makes the checkout permanently dirty and makes "which
commit is running?" unanswerable — `/version` reports `dirty: true` forever.

## 2. Services (all `systemctl --user`, user `mesflow`, linger enabled)

| Unit | Purpose |
| --- | --- |
| `terminal-mcp-http.service` | the controller: MCP + dashboard, `127.0.0.1:8766` + `192.168.1.109:8766` |
| `cloudflared-terminal-mcp-dashboard.service` | Cloudflare tunnel → `terminal-dashboard.mesflow.net`, `terminal-login.mesflow.net` |
| `terminal-mcp-tunnel.service` | OpenAI MCP tunnel (`tunnel-client`), local admin on `127.0.0.1:8767` |

```bash
ssh root-thinkcentre
U=$(id -u mesflow); export XDG_RUNTIME_DIR=/run/user/$U
sudo -u mesflow XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR systemctl --user status terminal-mcp-http
```

**Startup persistence:** all three are `enabled` and `loginctl show-user mesflow
-p Linger` is `Linger=yes`, so they start at boot without a login.

**`KillMode=process` is set deliberately.** The systemd default SIGTERMs the
entire cgroup on stop/restart — including any tmux server started from within
it. That is exactly how session `m1` was destroyed on this host by a routine
node-agent restart on 2026-09-09. tmux is meant to outlive whatever started it.

## 3. Health

```bash
curl -s http://127.0.0.1:8766/health/ready     # 200 + {"status":"ready"}
curl -s http://127.0.0.1:8766/health/live
curl -s http://127.0.0.1:8766/version          # commit SHA + dirty flag
curl -s http://127.0.0.1:8767/readyz           # MCP tunnel
```
`/version` **must** report `dirty: false` and a SHA that exists on `origin/main`.

## 4. Node topology

`local` **is m910 itself**. m910 must **not** appear in `config.yaml`'s
`nodes.remote` — that would make the controller register itself as one of its
own remote nodes, in a loop. Its `terminal-node-agent.service` is stopped and
disabled for the same reason.

Worker nodes point at the controller with `--controller-url
http://192.168.1.109:8766`.

## 5. Moving the controller to another host

1. **Back up** every DB with SQLite's online backup API (never `cp` a live DB):
   ```python
   s, d = sqlite3.connect(src), sqlite3.connect(dest)
   with d: s.backup(d)
   ```
   Verify each with `PRAGMA integrity_check` **before** relying on it.
2. **Deploy the same commit** and `pip install -e .` into a fresh venv.
3. **Copy only controller-canonical state.** These are node-local and must
   **not** be copied — the new host has its own, and overwriting them makes the
   new `local` node claim the old host's sessions:
   `session_registry.db`, `session_knowledge.db`, `grants.db`, `bindings.db`,
   `audit.db`, `prompt_submissions.db`, `killed_sessions.db`, `leases.db`.

   Canonical (do copy): `queue.db`, `backlog.db`, `events.db`, `nodes.db`,
   `integration.db`, `release_store.db`, `planner_store.db`, `pm_store.db`,
   `supervisor.db`, `connections.db`, `webauth.db`.
4. **Freeze the old controller before the final sync** — see §6.
5. **Repair the node registry**: delete the new host's own row from `nodes`
   (it is `local` now) and clear the old host's `local` identity so the first
   heartbeat writes the truth.
6. Move the tunnels (credentials file + config), then repoint worker nodes.

## 6. Cutover discipline — avoiding split-brain

Two controllers accepting writes is the failure to design against.

1. Prepare and verify the new host, then **stop** it again.
2. **Freeze** the old: `stop` **and** `disable` every unit.
3. **Check for timers.** A `terminal-mcp-tunnel-watchdog.timer` on the Dell
   host fired every 45s and *restarted the controller it was meant to watch*,
   silently resurrecting it ~30s after it was stopped. Stopping a service is
   not enough — `systemctl --user list-timers --all | grep terminal` and
   disable anything that can restart it.
4. **Final sync** with nothing running on either side.
5. Start the new controller; confirm `/health/ready`, `/version` and the node
   registry.
6. Wait longer than the longest timer interval and re-confirm the old host is
   still down.

## 7. Rollback

The old host's unit files are intact, only `disable`d. To roll back:

```bash
# On m910 — stop the new controller first. Never run both.
sudo -u mesflow systemctl --user stop terminal-mcp-http terminal-mcp-tunnel \
                                       cloudflared-terminal-mcp-dashboard
# On the old host
systemctl --user enable --now terminal-mcp-http terminal-mcp-tunnel \
                              cloudflared-terminal-mcp-dashboard
```

A full pre-cutover backup (all 19 DBs + configs + unit files + a manifest of
the old host's live sessions) is at
`/home/dell/terminal-mcp-migration-backup-<timestamp>/` on the Dell host.
**Copy it off that machine before powering it down** — it is the rollback
artifact and it is the only copy of the node-local stores.

## 8. Recovery after reboot

Nothing manual. Linger + `enabled` units bring the controller and both tunnels
up. Worker nodes reconnect on their own heartbeat interval (20s).

Check afterwards: `/health/ready` is 200, `/version` is clean, and every
expected node is `online` in the registry.
