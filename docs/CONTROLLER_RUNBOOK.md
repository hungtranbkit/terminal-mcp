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

LAN worker nodes (`dell-5530`, `macbook`) point at the controller with
`--controller-url http://192.168.1.109:8766`.

`dell-linux` is **off-LAN** since 2026-09-10 and reaches the controller over
the Tailscale overlay instead: `--controller-url http://100.117.214.87:8766`,
with its own `endpoint` on `100.81.85.120:8790`. The controller binds both
addresses at once (`30-tailnet-overlay.conf` drop-in) so the two groups
coexist; no inbound port-forward is involved on either side. Full setup and
node-recovery steps: `docs/multi-node.md`, "dell-linux over the Tailscale
overlay".

### Current fleet (2026-09-10)

| node_id | host | role |
| --- | --- | --- |
| `local` | m910 itself | controller + local tmux |
| `macbook` | 192.168.1.138 | macOS worker (LaunchAgent) |
| `dell-5530` | 192.168.1.250 | Windows worker (ConPTY) |
| `dell-linux` | 192.168.1.132 | Linux worker -- the retired controller host |

**`dell-linux` is the old Dell Latitude 5511, demoted to a plain worker.**
Only `terminal-node-agent` runs there; its `terminal-mcp-http`,
`terminal-mcp-tunnel`, `cloudflared-terminal-mcp-dashboard` and
`terminal-mcp-tunnel-watchdog.timer` units stay **stopped and disabled**
(§6). Its 15 long-lived tmux sessions (`terminal-mcp`, `codex-main`,
`mesflow`, `promptflow`, ...) predate the agent and are never restarted by
it -- that is what `KillMode=process` in the unit protects.

### ⚠ `dell-linux` and the IP-takeover plan are mutually exclusive

`docs/CONTROLLER_RESTORE_NOTES.md` describes m910 adopting `192.168.1.132`
once the Dell host is powered off, so `dell-5530` reconnects without the
restart that would destroy `win1`/`win2`/`wtest`. That plan and the
`dell-linux` node above **cannot both be live**:

* The takeover assumes `192.168.1.132` is permanently free. Keeping
  `dell-linux` means that address stays occupied, so the takeover's guard
  correctly refuses forever and `dell-5530` never reconnects that way.
* Worse, if m910 ever adopts `192.168.1.132` **and the Dell host later comes
  back**, both hosts hold one address -- the ARP conflict the guard exists to
  prevent, only now triggered from the other direction, since the guard is
  only checked before adoption and never re-checked afterwards.

Pick one before arming any takeover timer. If the takeover is chosen,
**remove the `dell-linux` entry from `nodes.remote` first** -- otherwise the
controller ends up polling `http://192.168.1.132:8790`, which would then be
m910's own address.

### `dell-5530`'s heartbeat relay (temporary)

`terminal-mcp-heartbeat-relay-dell-5530.service` on m910 exists because that
node's agent has `--controller-url http://192.168.1.132:8766` baked into its
command line and its ConPTY sessions are **direct children of the agent
process** (verified: `win1`/`win2`/`wtest` all have PPID 12284, the agent) --
so restarting it to repoint it destroys live work. Unlike tmux, which is a
separate server that outlives its parent, `WindowsSessionBackend` holds
sessions in an in-process dict; there is no reattach path.

The node is otherwise fine, so `deploy/node-heartbeat-relay.py` pulls its real
`/v1/health`, `/v1/metrics` and `/v1/sessions` and posts them to the heartbeat
endpoint on its behalf. It never invents liveness: if any pull fails it posts
nothing and the node ages out to offline normally, and it refuses outright if
the endpoint reports a different `node_id` than the one being relayed.

**This is a bridge, not a fixture.** `run-node-agent.ps1` on the node has
already been corrected to `192.168.1.109`, so the next time its Scheduled Task
(`TerminalMcpNodeAgent-dell-5530`) starts, the node heartbeats for itself and
the relay is redundant:

```bash
systemctl --user disable --now terminal-mcp-heartbeat-relay-dell-5530
```

A checkpoint taken before any of this -- the three sessions' `claude
--session-id` values, so they can be brought back with `claude --resume` --
is at `~/terminal-mcp-migration-backup/dell5530-checkpoint-*/`.

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

`ss -ltn | grep 8766` must show **three** sockets — loopback, `192.168.1.109`
and `100.117.214.87`. If the tailnet one is missing, `dell-linux` cannot
heartbeat at all; check that the `30-tailnet-overlay.conf` drop-in survived
(`systemctl --user show terminal-mcp-http -p Environment`).
