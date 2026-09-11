# Self-host / M910-failure audit

**Question:** if M910 loses power, loses the network, has its controller crash,
or has Tailscale die, do Dell Linux, HP Linux, Windows and the Mac die with it?

**Answer:** their running sessions and their local data plane survive; their
control plane does not exist. Losing M910 costs the fleet its single pane of
glass, not its work — provided you can still reach each node some other way.
That last clause is where the real gaps are.

Everything below was measured on the machine or proven with a real process.
Nothing here is inferred from configuration intent.

## How the outage was simulated

No real node was touched, nothing was powered off, and no live session was
restarted. A **disposable node agent** was started on M910 itself with its own
throwaway state directory, a config carrying no session-name whitelist at all,
and `--controller-url` pointed at a port with nothing listening — which is
exactly what a dead controller looks like from a node's side.

Encoded as `tests/test_selfhost_controller_outage.py` (5 tests) so it keeps
being true.

## What survives an M910 outage — proven

| Capability | Result |
| --- | --- |
| Agent starts with the controller already unreachable | ✅ startup never contacts it |
| Agent stays up while every heartbeat fails | ✅ survived 22 consecutive failures, `/v1/health` still ok |
| Create session | ✅ `state=READY` |
| List sessions | ✅ |
| Read: `tail`, `status`, `capture` | ✅ |
| Input: `send` text + Enter | ✅ |
| Input: `send-keys` (arrows/Tab) | ✅ |
| Local session **registry** (ACTIVE → MISSING on crash) | ✅ |
| Local **recovery** (`registry-reopen`) with no controller | ✅ rebuilt the session from its own registry |
| Recovery **idempotency** | ✅ second call → `SESSION_ALREADY_EXISTS`, exactly one tmux session |
| Grants honoured locally | ✅ node's own `grants.db`, no controller lookup |

The heartbeat loop is the node's **only** outbound dependency, and its failure
is caught and retried by construction ("this loop must never die"). The node
serves 24 `/v1/*` routes locally, including registry and registry-reopen.

## What does NOT survive — the real SPOF

Every one of these lives only on M910:

| Component | Consequence when M910 is gone |
| --- | --- |
| MCP surface (`/mcp`) | ChatGPT/Claude lose the fleet entirely |
| Dashboard UI + web terminal | no browser access to any node |
| Cross-node routing / `resolve_session` | no bare-name or `node/session` addressing |
| Fleet-wide `terminal_list_sessions` | no global view |
| Recovery **loop** (`recovery_loop.py`) | nothing auto-reconciles; only manual per-node calls |
| Supervisor, queue, backlog, project state | all controller-side |
| Cloudflare tunnels (`terminal-dashboard`, MCP tunnel) | public entry points die with the host |
| `dell-5530`'s heartbeat relay | that node goes stale immediately |

So the architecture **is still hub-and-spoke**. The data plane is local-first
and always was; the control plane is centralised and has no failover.

### Reaching a node without M910

This is the part that decides whether "the node survived" is useful:

| Node | Reachable without M910? | How |
| --- | --- | --- |
| dell-linux | ✅ | tailnet `100.81.85.120`, SSH + `:8790` |
| hp-linux | ⚠️ | tailnet `100.67.53.117`, `:8790` with its token — but **no SSH** |
| macbook | ✅ | tailnet `100.104.209.93` + LAN |
| dell-5530 | ❌ | **no Tailscale**; LAN only, and its controller-url is M910's LAN address |

A node you cannot reach and cannot administer has survived in name only.

## Dependency graph

```
ChatGPT / browser
        │
        ▼
  Cloudflare tunnel ──► M910 controller ──► MCP, dashboard, routing,
        (DNS)                 │              recovery loop, supervisor,
                              │              queue, backlog
                              │ heartbeat (node → controller, retried, non-fatal)
                              │ /v1/* pulls (controller → node, token auth)
        ┌─────────────────────┼─────────────────────┬──────────────┐
        ▼                     ▼                     ▼              ▼
   dell-linux             hp-linux              macbook       dell-5530
   own agent              own agent             own agent     own agent
   own 19 DBs             own state             own state     own state
   own tmux               own tmux              own tmux      own ConPTY
   tailnet ✅             tailnet ✅            tailnet ✅     tailnet ❌
```

No shared database, no shared filesystem, no cross-node file locks. Each node's
grants/registry/leases are its own — verified: the disposable agent used its
own `grants.db` under its own `XDG_STATE_HOME` and answered read/input from it
with no controller in the picture.

## Failure matrix

| Failure | Running sessions | Local read/input | Local recovery | Fleet view | Notes |
| --- | --- | --- | --- | --- | --- |
| M910 controller process crashes | survive | ✅ per node | ✅ manual per node | ❌ | agents keep retrying; reconnect is idempotent |
| M910 powered off | survive | ✅ (except dell-5530, unreachable) | ✅ | ❌ | same |
| M910 off the network | survive | ✅ | ✅ | ❌ | same |
| Tailscale down **on M910** | survive | ✅ | ✅ | ❌ | LAN nodes could still reach it if on-subnet |
| Tailscale down **on a node** | survive | LAN only | ✅ | node goes stale | dell-5530 is already in this state permanently |
| A node reboots | lost, then recovered per policy | ✅ after autostart | tombstones respected | — | macOS/Windows need a login first |
| M910 returns | untouched | ✅ | ✅ | restored on next heartbeat | no duplicate: proven |

## Per-node verdict

| Node | Survives M910 loss | Usable during it | Verdict | % |
| --- | --- | --- | --- | --- |
| **dell-linux** | ✅ | ✅ SSH + local API over tailnet | **PARTIAL** | ~70% |
| **hp-linux** | ✅ | ⚠️ local API only, no SSH | **PARTIAL** | ~50% |
| **macbook** | ✅ | ✅ over tailnet | **PARTIAL** | ~60% |
| **dell-5530** | ✅ (sessions keep running) | ❌ unreachable off-LAN | **FAIL** | ~30% |
| **local/M910** | n/a | n/a | reference | — |

No node is PASS, because no node can host a control plane today.

**Fleet self-host readiness ≈ 50%** — data plane local-first and proven;
control plane single-homed and unproven anywhere else.

## P0 blockers

1. **dell-5530 has no Tailscale and no off-LAN path.** If M910 goes down while
   they are not on the same subnet, that node is unreachable by anything.
   Needs a maintenance window (`win1`/`win2`/`wtest` are live).
2. **hp-linux has no SSH from the controller.** It can be read over `:8790`
   with its token but cannot be administered, updated, or recovered by hand.
   Needs a credential/authorisation on that host.
3. **Version skew.** dell-linux runs `ee375c1`, a commit M910 has never seen
   (forked at `eb9f51d`; M910 has 7 commits it lacks). Activating any
   federated protocol across nodes on different generations is how skew
   becomes permanent. **Converge versions before federation.**
4. **No second control plane anywhere.** Only M910 runs `terminal-mcp-http`.
   dell-linux is the only node with both a full checkout and complete local
   state, so it is the only realistic candidate — and it is blocked by (3).

## Core fix made during this audit

`allowed_session_patterns` (and its `input_policy` twin) were still validated
as **non-empty**, even though they no longer authorize anything after the
whitelist retirement — they are migration input only. A node configured the way
a post-whitelist node should be configured therefore **refused to start**:

```
ValueError: allowed_session_patterns must be a non-empty list of strings
```

Found by this harness on its first run. Both now accept an empty list, which is
what a deployment that has finished migrating actually has.

## Not production-ready — what is missing for that claim

* no federation, no peer protocol, no cluster view, no failover controller;
* three of four remote nodes cannot host a control plane (no checkout, no
  service, or unreachable);
* auto-recovery is still globally off (see REPORT.md for why: the registry
  cannot yet tell a disposable session from a real one);
* a real reboot has never been performed on any node — autostart is verified by
  configuration inspection plus a controlled service restart, never by a cold
  boot. A real reboot acceptance test needs a maintenance window.
