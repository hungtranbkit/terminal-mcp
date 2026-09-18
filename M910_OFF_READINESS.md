# Can M910 be powered off safely?

**Verdict: NO — not yet.** Two blockers make it unsafe today, and neither can
be cleared from M910 alone.

This file says exactly what is proven, what is not, and the precise remaining
steps. Nothing below is called production-ready, because the one test that
would justify that phrase — a real cold boot — has not been run on any node.

Audited 2026-09-11. Controller at `37ffbbb`.

## Gate criteria

The gate now requires **two independent remote self-host control planes**, and
every takeover node meeting its `minimum_failover_auth_set`. One surviving
control plane is not redundancy; it is the same single point of failure moved
to a different machine.

| # | Criterion | State |
| --- | --- | --- |
| 0 | ≥2 remote control planes, independent and autostarting | ❌ **FAIL** — HP has one; Dell's is installed but disabled |
| 0b | Every takeover node meets `minimum_failover_auth_set` | ❌ **UNVERIFIED** — 4 of 5 nodes cannot answer the audit yet |
| a | HP + Dell have an independent, healthy, **autostarting** control plane | ⚠️ **PARTIAL** — HP: yes (active+enabled+linger). Dell: installed but disabled |
| b | Cluster still queries/routes with M910 absent | ❌ **FAIL** — no federation; routing is M910-only |
| c | Windows/Mac workloads survive M910 and keep an alternate control path | ⚠️ **PARTIAL** — they survive; Windows has no alternate path at all |
| d | Recovery / grants / bindings / tombstones correct | ✅ **PASS** |
| e | Reconnect does not duplicate | ✅ **PASS** |
| f | Cold-boot acceptance on two alternate control planes | ❌ **NOT DONE** — needs a maintenance window |

## Environment convergence — the gap source-code convergence hid

Converging commits is not enough: a node on the right commit is still useless
at failover without a Claude login, tmux or Tailscale. `deploy/node-profile.yaml`
declares what each role needs; `terminal-mcp-node-doctor` audits a machine
against it; `terminal_fleet_environment` asks the whole fleet at once.

    terminal-mcp-node-doctor --role node --role controller
    # MCP: terminal_fleet_environment(roles="node,controller")

Live, right now:

    failover_ready_count: 1   (and that node is M910 itself)

    local        ready=True   v1
    hp-linux     ready=False  v0   environment audit unavailable (build predates it)
    dell-linux   ready=False  v0   environment audit unavailable
    dell-5530    ready=False  v0   environment audit unavailable
    macbook      ready=False  v0   environment audit unavailable

The four remote nodes are not *failing* the audit — they cannot yet *take* it.
That is reported as unavailable with the reason, never as passing, and it is
itself the signal that they need converging first.

M910's own audit, as an example of what the others will produce:

| Requirement | Status |
| --- | --- |
| python, git, tmux, tailscale, claude, cloudflared | PASS |
| terminal-mcp-http service | PASS (enabled) |
| claude_auth, tailscale_auth | PASS — **the failover-critical pair** |
| cloudflare_auth, ssh_inbound | PASS |
| codex, codex_auth | MISSING / NEEDS_AUTH (advisory) |
| github_auth | NEEDS_AUTH (advisory) |

`github_auth` being absent is exactly why the 19 commits here cannot be pushed
— and it correctly does **not** make this node unfit to carry the fleet. That
separation is the point of `minimum_failover_auth_set`.

### Secrets are never synchronised

No credential is copied between machines by any of this. Every auth probe is
existence- or status-only; a gap produces a one-time command a human runs on
that machine, usually a device/browser flow that could not be copied anyway.
`state_ownership` in the profile records what replicates (desired state,
grants, bindings, project/task metadata) and what is local-only (node tokens,
CLI credentials, SSH keys, leases, audit).

## What is proven

Evidence is reproducible; the command is given for each.

### Nodes outlive their controller — PASS

    .venv/bin/python -m pytest tests/test_selfhost_controller_outage.py -q

A real node agent, disposable state, no whitelist, `--controller-url` pointed
at a dead port. Startup never contacts the controller; the heartbeat is the
only outbound dependency and its failure is caught and retried — 22
consecutive failures, still healthy. Create, list, tail, status, send,
send-keys, the local registry, local `registry-reopen` recovery and grant
enforcement all worked with nothing upstream. Reopening a live session returns
`SESSION_ALREADY_EXISTS` and leaves exactly one tmux session.

### Failure matrix — PASS

    .venv/bin/python -m pytest tests/test_failure_matrix.py -q

Peer ages to OFFLINE keeping last-known state; duplicate heartbeats do not
duplicate a node; stale-version peer is flagged; returning node reconnects
without respawning; recovery happens exactly once across repeated passes; a
tombstone survives ten reconciles; the attempt budget actually stops a failing
loop; two OS processes writing one registry concurrently land all 80 rows.

### Version skew is now visible — PASS

    terminal-mcp-doctor nodes

`local` reports contract v1 with four capabilities. All four remote nodes
report **v0 (legacy)** and the doctor names what must not be assumed of them.
Before this, a node running completely different code was indistinguishable
from one that had merely restarted.

### Recovery correctness — PASS

    .venv/bin/python -m pytest tests/test_recovery_engine.py -q

SOFT_RECONNECT before anything spawns or spends the attempt budget;
AGENT_RESUME via a verified `--resume`; TASK_RECOVERY as an honest
metadata-only recreate; KILLED refused to the engine but still reopenable by
an explicit human; a staleness bound so a long-dead record is not resurrected.

## What is NOT proven — the blockers

### B1. One control plane autostarts; it is not reachable — P0

HP runs a **healthy full controller**: `/health/live`, `/health/ready` and
`/version` all answer on `127.0.0.1:8766`, alongside its node agent on the
tailnet, with 19 local state DBs and `Linger=yes`.

**Correction to an earlier reading in this audit.** A first pass reported "no
systemd units on HP". That was wrong: the query ran in a session spawned by
the node agent, which has no `XDG_RUNTIME_DIR`/`DBUS_SESSION_BUS_ADDRESS`, so
`systemctl --user` could not reach the user bus and its error text was
mis-read as empty output. Re-queried with the runtime dir set:

    terminal-mcp-http     active=active   enabled=enabled
    terminal-node-agent   active=inactive enabled=not-found

So HP's controller **does** come back after a reboot (enabled + linger). Two
real gaps remain:

* It binds **loopback only**, so it cannot serve the cluster even though it
  survives.
* HP's **node agent** has no unit at all — it is running from a manual launch
  and would not come back.

Dell Linux has the complete code and all 19 DBs but runs only the node agent;
its `terminal-mcp-http` is installed and **disabled**.

### B2. No federation — P0

There is no peer protocol, no cluster view, no routing between nodes. With
M910 off, each node is an island: reachable directly and individually, with no
single place to ask "what is running anywhere?". The MCP surface, the
dashboard, cross-node routing, the recovery loop, supervisor, queue and
backlog all exist only on M910.

### B3. dell-5530 has no path off the LAN — P0

Tailscale is not installed on the Windows node and its controller-url is
M910's LAN address. If M910 is off and the two are not on one subnet, that
node is unreachable by anything — its ConPTY sessions keep running with no way
to reach them.

### B4. Version convergence is blocked on delivery — P0

The canonical build is bundled at
`~/terminal-mcp-artifacts/terminal-mcp-<sha>.bundle` with a sha256 and
`CONVERGE.md`. It has **not been delivered to any node**:

* no git push credential on M910;
* serving it over HTTP from M910 was refused by this environment's own
  guardrails, and was not worked around;
* HP's `/home/kimex/workspace/terminal-mcp` is **not a git checkout** at all
  (a plain copy), so even with the bundle in hand it needs a decision, not a
  `git merge`.

Delivery needs either a credential or an operator action.

## Per-node verdict

| Node | Survives M910 off | Usable during | Autostart | Control plane | Verdict | % |
| --- | --- | --- | --- | --- | --- | --- |
| **hp-linux** | ✅ | ✅ tailnet + own controller | ✅ controller; ❌ agent | ✅ healthy, loopback-bound | **PARTIAL** | ~75% |
| **dell-linux** | ✅ | ✅ tailnet + SSH | ✅ agent only | ❌ disabled | **PARTIAL** | ~55% |
| **macbook** | ✅ | ✅ tailnet | ⚠️ login, not boot | ❌ | **PARTIAL** | ~45% |
| **dell-5530** | ✅ sessions only | ❌ off-LAN | ⚠️ boot trigger, Interactive principal | ❌ | **FAIL** | ~30% |

**Fleet readiness for an M910 power-off: ~55%.**

## Exactly what remains before M910 can be powered off

In order. Steps 1–3 are the minimum for a *safe* power-off; 4–5 are needed
before anyone may say production-ready.

1. **Bind HP's controller to its tailnet address, and give HP's node agent a
   unit.** The controller unit already exists and is enabled — what is missing
   is reachability (loopback-only today) and an autostarting agent beside it.
   Neither needs a reboot: install and `enable` without `--now`, and the
   running processes keep serving until the next boot picks the units up.
2. **Deliver and apply the bundle** to HP and dell-linux, then confirm
   `contract_version: 1` on both. Blocked on a credential or an operator.
3. **Windows: install Tailscale and repoint its controller-url to a tailnet
   address.** Needs a maintenance window — `win1`/`win2`/`wtest` are live and
   its agent cannot be restarted without losing them.
4. **Federation V1** — only after 1–3, because a peer protocol across nodes
   that cannot be reached, cannot be updated, and do not share a contract is
   how skew becomes permanent.
5. **Cold-boot acceptance** on HP and dell-linux, in a maintenance window.
   Until that has actually run, autostart is a configuration claim, not a
   verified behaviour.

## Rollback

Everything added on M910 is additive and reversible:

* the contract columns default to 0 and older agents simply do not report them;
* `session_access` defaults are now OPEN (`default_read`/`default_input` true):
  absence of a grant record means ALLOW, and the whitelist→grants migration is
  inert while they are open, because the rows it used to write (`read=1`,
  `input=0`) could only ever NARROW what the default already permits — that is
  the bug it now refuses to reintroduce. Setting either default to false
  restores the old closed behaviour without touching any stored grant;
* auto-recovery is ON (`auto_recovery.enabled: true`) with
  `managed_sessions_only: true` and `max_missing_age_seconds: 3600`, so it will
  only ever recreate a session this controller was asked to create, that is not
  tombstoned, and that has been missing for under an hour.

  "Asked to create" is now recorded (`created_by_controller`), not inferred.
  It previously keyed on `launch_command`, which a discovery pass also writes —
  it classifies whatever the pane is running — so 26 sessions the controller
  had merely observed, two of them inside the recovery window, were eligible
  for respawn. Measured after the fix: 0 of 270 records carry provenance, so
  nothing is auto-recoverable until this controller itself creates a session;
* to undo the controller-side changes entirely:
  `git reset --hard eb9f51d && ./.venv/bin/pip install -e . && systemctl --user restart terminal-mcp-http`
  (record the current sha first; the commits are local-only and unpushed).

## Do not power off M910 today

Doing so right now would leave: no MCP surface for ChatGPT, no dashboard, no
cross-node routing, no auto-recovery anywhere, and `dell-5530` unreachable
entirely. The sessions would survive. Almost nothing else would.
