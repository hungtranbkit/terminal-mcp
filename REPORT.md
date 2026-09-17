# Session Recovery + Self-Host/Federation readiness

Audited 2026-09-11 against the running fleet. Every row below is something
that was checked on the machine, not inferred from configuration intent.
Where a thing could not be checked, it says so rather than guessing.

## Scoring

Eight criteria per node. A node scores a point per criterion it actually
meets; "unknown" never counts as met.

| # | Criterion |
| --- | --- |
| C1 | node-agent (or controller) process running and healthy |
| C2 | autostart configured (systemd / launchd / Scheduled Task) |
| C3 | starts with **no interactive login** (survives a bare reboot) |
| C4 | reachable over the Tailscale overlay |
| C5 | can run its **own** controller (self-host, not just an agent) |
| C6 | durable local state: session registry + grants + leases |
| C7 | admin reachable from the controller (SSH) for rollout |
| C8 | federation peer API / cluster view |

## Per-node result

| Node | C1 | C2 | C3 | C4 | C5 | C6 | C7 | C8 | Score | Verdict |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | --- | --- |
| **local / M910** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | n/a | ❌ | 7/7 | **PASS** (federation missing) |
| **dell-linux** | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ✅ | ❌ | 6.5/8 | **PARTIAL** |
| **hp-linux** | ✅ | ❓ | ❓ | ✅ | ❓ | ❓ | ❌ | ❌ | 2/8 | **PARTIAL — unauditable** |
| **dell-5530 (Win)** | ✅ | ✅ | ❌ | ❌ | ❌ | ❓ | ✅ | ❌ | 3/8 | **PARTIAL** |
| **macbook** | ✅ | ✅ | ❌ | ✅ | ❌ | ❓ | ✅ | ❌ | 4/8 | **PARTIAL** |

**Fleet self-host/federation readiness: ~45%.** Session-recovery core is
substantially higher (see below); federation is 0%.

### local / M910 — PASS
`terminal-mcp-http`, `terminal-mcp-tunnel`, `cloudflared-…-dashboard` all
active+enabled, `Linger=yes`, Tailscale `100.117.214.87`.
`/health/live`, `/health/ready`, `/version` all answer; version reports a
clean SHA. Restarting the controller with live sessions leaves tmux
byte-identical (name + created epoch) and all 11 grants intact — verified
again this session.

### dell-linux — PARTIAL
`terminal-node-agent` active+enabled, `Linger=yes`, `KillMode=process`
present, controller-url already on the tailnet (`100.117.214.87:8766`),
9 live tmux sessions, all 19 state DBs present locally.

⚠️ **Version skew, and it is real.** Its checkout is `ee375c1`, a commit that
does not exist on M910. It forked at `eb9f51d` and carries one local docs
commit, while M910 has seven it does not — including the whitelist removal and
every recovery fix below. This is exactly why dell-linux rows still report the
old contradictory `allowed=false` next to `effective_read=true`. It runs the
agent but cannot currently host a controller of the same generation.

### hp-linux — PARTIAL, and mostly unauditable
Agent answers `/v1/health` (`node_id=hp-linux`, agent 0.12.0) with 4 readable
sessions, on the tailnet at `100.67.53.117`.

❌ **BLOCKED: no SSH access.** The controller's key is not authorised there
(`Permission denied (publickey)` for `mesflow`/`hp`/`dell`). Autostart, linger,
local state and version are therefore **unknown, not assumed**. Nothing can be
rolled out to this node until that is fixed.

### dell-5530 (Windows) — PARTIAL
Scheduled Task `TerminalMcpNodeAgent-dell-5530` is Running with **both** a
Logon and a Boot trigger, restart bounded (3 × 1 min).

* ❌ **C3**: principal is `LogonType=Interactive`, `RunLevel=Limited` — the
  boot trigger will not actually start the agent until that user logs in. The
  repo's own `configure-windows-node-stability.ps1` already knows the fix
  (S4U); this task was not created with it.
* ❌ **C4**: **Tailscale is not installed.** This node cannot join the peer
  mesh at all, and its controller-url is still the LAN address
  `192.168.1.109:8766`, so it depends on both hosts being on the same subnet.
* `StartWhenAvailable=False`, so a missed boot trigger is not caught up.

Its three ConPTY sessions (`win1`/`win2`/`wtest`) remain the reason nothing
here may be restarted without a maintenance window.

### macbook — PARTIAL
LaunchAgent `com.terminal-mcp.node-agent.macbook.plist` with `RunAtLoad=true`
and `KeepAlive`, reachable on LAN and tailnet.

❌ **C3**: a LaunchAgent runs at **user login**, not at boot. After a cold
reboot the node stays down until someone logs into the desktop. A LaunchDaemon
(or `loginwindow` auto-login) is the fix; neither is configured.

## Session Recovery — what exists, what was fixed

Most of this was already built and is good: `recovery_engine.py` (lease-locked,
generation-counted, policy-gated), `recovery_loop.py`, and a
`session_records` schema that already persists node_id, stable_session_id,
conversation_id, cwd/repo_root/git_branch/worktree, agent_type, launch command,
grants, binding names, recovery_state/attempts/generation, per-session
`auto_recovery_enabled`, and `killed_at`/`deleted_at`/`offline_at` tombstones.
`drop_events` is the event log. None of that needed rebuilding.

Three real defects found and fixed this session:

| Fix | Commit | What was wrong |
| --- | --- | --- |
| Tombstones honoured | `8f9d03a` | `RECOVERABLE_STATUSES` includes KILLED so a human can press Reopen — but `reconcile_node` walks the same set, so the background pass would undo an operator's deliberate stop. KILLED is now `RECOVERY_TOMBSTONED` to the engine; `force=True` still reopens. |
| SOFT_RECONNECT tier | `783be94` | A node-agent restart does not kill tmux, so a session marked MISSING is usually still alive when the node returns. The engine respawned it, hit `SESSION_ALREADY_EXISTS`, and recorded a FAILED recovery for a healthy session while burning an attempt. It now reconciles the record instead, before the attempt budget. |
| Staleness bound | `ccdec63` | Enabling auto-recovery would have spawned **136 real processes** — measured, not estimated. |

### The recovery tiers now

| Tier | State | Trigger |
| --- | --- | --- |
| SOFT_RECONNECT | `RECONNECTED` | runtime session still live on that node → record reconciled, nothing spawned |
| AGENT_RESUME | `RESUMED_OK` | gone, but a real `conversation_id` is on record → reopened with `--resume`, verified before success is reported |
| TASK_RECOVERY | `RECOVERY_DEGRADED` | gone, no resume id → honest metadata-only recreate in the right cwd/branch |
| refused | `RECOVERY_TOMBSTONED` / `RECOVERY_STALE` / `RECOVERY_BLOCKED` | intentional stop, too old, or policy/attempt budget |

One caveat worth stating plainly: the liveness probe must not use
`controller.resolve_session` — that answers from a TTL'd location cache and
reports a just-killed session as alive. The first version of SOFT_RECONNECT
did exactly that and silently skipped a real recovery; the existing live MCP
round-trip test caught it. It now takes a fresh fleet listing, once per
reconcile pass.

## Why auto-recovery is still OFF here

`auto_recovery.enabled` is `False` on this deployment and should stay that way
for now. The registry holds 207 MISSING records; 136 satisfied "recoverable".
The staleness bound cuts that to ~2, but 2 is not 0, and both are disposable
test sessions from today. The real fix is a registry that knows which sessions
were disposable — it currently keeps a durable row for every session that ever
existed, including everything pytest creates. Per-session opt-in
(`auto_recovery_enabled`) already works and is the safe way to use this today.

## Federation — 0%

There is no federation code: no peer protocol, no capability handshake, no
cluster index, no `cluster_status`/`route` API, no second controller. Verified
by inspection, not assumed.

**Failure matrix if M910 goes offline — everything stops.** Every node is an
agent pointed at one controller; the MCP surface, the dashboard, routing,
session discovery and recovery all live there. dell-linux is the only node
with both a full checkout and complete local state, so it is the only
realistic second controller today — and it is on a divergent commit.

## What must happen next, in order

1. **Unblock hp-linux** — authorise the controller key so the node can be
   audited and rolled out to at all. (Needs a credential/an action on that
   host; not something to do silently.)
2. **Converge versions** — dell-linux is on a commit M910 has never seen.
   Nothing federated should be built while nodes run different generations of
   the protocol.
3. **Windows autostart + tailnet** — switch the Scheduled Task principal to
   S4U, install Tailscale, repoint controller-url to the tailnet address. All
   three need a maintenance window because of `win1`/`win2`/`wtest`.
4. **macOS boot-start** — LaunchDaemon or auto-login, otherwise the node is
   down after every cold boot.
5. **Registry disposability signal**, then global auto-recovery can be turned
   on safely.
6. **Federation V1** only after 1–3. Building a peer protocol across nodes
   that cannot be reached, cannot be updated, and do not share a version is
   how the version skew above becomes permanent.
