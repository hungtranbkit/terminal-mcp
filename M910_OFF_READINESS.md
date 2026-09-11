# Can M910 be powered off safely?

**Verdict: NO — not yet.** Two blockers make it unsafe today, and neither can
be cleared from M910 alone.

This file says exactly what is proven, what is not, and the precise remaining
steps. Nothing below is called production-ready, because the one test that
would justify that phrase — a real cold boot — has not been run on any node.

Audited 2026-09-11. Controller at `37ffbbb`.

## Gate criteria

| # | Criterion | State |
| --- | --- | --- |
| a | HP + Dell have an independent, healthy, **autostarting** control plane | ⚠️ **PARTIAL** — HP: yes (active+enabled+linger). Dell: installed but disabled |
| b | Cluster still queries/routes with M910 absent | ❌ **FAIL** — no federation; routing is M910-only |
| c | Windows/Mac workloads survive M910 and keep an alternate control path | ⚠️ **PARTIAL** — they survive; Windows has no alternate path at all |
| d | Recovery / grants / bindings / tombstones correct | ✅ **PASS** |
| e | Reconnect does not duplicate | ✅ **PASS** |
| f | Cold-boot acceptance on two alternate control planes | ❌ **NOT DONE** — needs a maintenance window |

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
* `session_access` defaults are closed, matching pre-change behaviour, and the
  whitelist→grants migration only ever ADDS grants;
* auto-recovery remains globally off;
* to undo the controller-side changes entirely:
  `git reset --hard eb9f51d && ./.venv/bin/pip install -e . && systemctl --user restart terminal-mcp-http`
  (record the current sha first; the 15 commits are local-only and unpushed).

## Do not power off M910 today

Doing so right now would leave: no MCP surface for ChatGPT, no dashboard, no
cross-node routing, no auto-recovery anywhere, and `dell-5530` unreachable
entirely. The sessions would survive. Almost nothing else would.
