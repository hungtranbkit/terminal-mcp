# Terminal MCP — Finish Gate

Commit `d661ffc` · version **0.13.0** · dashboard template `sha256:0eaf9691…d2ce` · contract **v1** (local), **v0** (all four nodes).

Machine-readable twin: [`FINISH_GATE.json`](FINISH_GATE.json).

**The rule this report is written under:** PASS requires a runtime observation.
Code existing is not PASS. Where only code exists, the status says so.

## Baseline, and what happened to it

Captured through the API before any test ran, and re-checked after each
large group:

| | before | after |
| --- | --- | --- |
| tmux server PID | 26175 | 26175 |
| pane PIDs (m1/m2/terminal-mcp-main) | 26176 / 158083 / 117992 | unchanged |
| sessions (3 local + 17 remote) | 20 | 20 |
| agent generations, all 4 nodes | recorded | unchanged |
| grants / registry ACTIVE / bindings | 6 / 3 / 0 | unchanged |

**No session lost. No PID changed. No node agent restarted. No reboot.**
The only process restarted in this project's whole deploy history today is
`terminal-mcp-http`, which holds no session runtime (the tmux server lives
in a different cgroup and the unit is `KillMode=process`).

## Result by area

| # | Area | Status |
| --- | --- | --- |
| 1 | core MCP/API · list/status/read/send/bindings/supervisor | **PASS** |
| 2 | default-open permissions, no whitelist, locks still work | **PASS** |
| 3 | dashboard desktop + mobile | **PASS** |
| 4 | single-submit / bounded retry / ghost suggestion / idempotency | **PASS** |
| 5 | federation / local-first / M910 unavailable | **PASS_WITH_LIMITATION** |
| 6 | recovery · identity, tombstones, no resurrection | **PASS** |
| 7 | environment / auth / profile / fleet doctor / drift | **PASS_WITH_LIMITATION** |
| 8 | Linux / macOS / Windows · tmux and ConPTY safety | **PASS** |
| 9 | service and systemd configuration (static audit) | **PASS** |
| 10 | DB migration / integrity / concurrency | **PASS** |
| 11 | security · auth, redaction, secrets, CF, Tailscale | **PASS** |
| 12 | packaging / install / update / rollback / provenance | **PASS_WITH_LIMITATION** |
| 13 | test suites including Playwright | **PASS** |
| 14 | docs / runbook / acceptance matrix | **PASS** |

**11 PASS · 3 PASS_WITH_LIMITATION · 0 BLOCKED areas · 5 maintenance items.**

Evidence for each row is in `FINISH_GATE.json`; the limitations are:
federation has no second controller actually serving, the fleet doctor
cannot be run on remote nodes from this session, and the commits cannot be
pushed.

## Full test result

**2812 passed · 0 failed · 1 skipped · 16 deselected** across 168 files,
run in four sequential batches (this host OOM-killed a single-process run
earlier in the day). The one skip is `codex`, which is not installed here.

## Fixed during this gate

* **Legacy `allowed` contradicted itself on every remote row.** 16 of 20
  sessions reported `allowed: False` beside `effective_read: True` — the
  exact state default-open exists to remove. The local listing had been
  normalised when the whitelist was retired; this merge had not. It gated
  nothing, but the dashboard reads the field and one consumer was still
  branching on it. Normalised so the two can never disagree, and it can
  only narrow or match, never widen.
* **`version` could not show drift.** Controller and every un-upgraded node
  both reported `0.12.0`, so only `contract_version` distinguished them.
  Bumped to `0.13.0`.

Earlier the same day, and part of what this gate re-verified: the stale
identity pin that out-denied having no grant; session provenance
(`created_by_controller`) replacing an inference that let auto-recovery
respawn sessions the controller had merely observed; three dashboard
defects (permanent false attention badges, a node-group collapse that did
nothing, a selected session that scrolled out of view); the per-session
access control that was hidden on every session.

## M910-OFF readiness

Two different questions, deliberately not merged into one percentage.

**Normal-use readiness — READY.** Everything a working day needs is
verified running: 20/20 sessions reachable and writable, 208 MCP tools,
dashboard on desktop and phone, recovery gated and inert, zero session
loss across two controller restarts.

**HA / failover readiness — NOT READY.** Four binary conditions, none met:

1. no second controller is actually serving — M910 is still the only
   MCP/dashboard/routing plane;
2. `dell-5530` has no Tailscale and is LAN-only;
3. all four nodes run contract v0 and cannot report the handshake a
   federated peer needs;
4. cold-boot autostart has never been observed on `hp-linux` or
   `dell-linux`.

Losing M910 today keeps every session alive — each agent serves its own
`/v1` independently, which this gate confirmed directly — and loses the
MCP surface, the dashboard, cross-node routing and the recovery loop.

## The one maintenance window

Nothing below has been done. Each step is gated on the one before it.

| Step | Node | Action | Sessions at risk | Risk |
| --- | --- | --- | --- | --- |
| M1 | macbook | upgrade agent → 0.13.0 | 1 | low |
| M2 | hp-linux | upgrade agent; bind controller to tailnet; install node-agent unit | 4 | medium |
| M3 | dell-linux | apply staged bundle; upgrade agent | 9 | medium |
| M4 | hp-linux | cold-boot acceptance | 4 | medium |
| M5 | **dell-5530** | install Tailscale; repoint controller-url; upgrade agent | 3 | **high** |

**Precheck, every step:** snapshot the fleet through the API (session
names, PIDs, generations, grants); confirm the node's unit carries
`KillMode=process`; confirm no session on it is mid-task.

**Why this order.** Fewest sessions first, so a mistake is cheapest where
it is most likely. `dell-5530` is last and separate because its ConPTY
sessions live *inside* the agent process: restarting it destroys
`win1`/`win2`/`wtest`. That is not a risk to manage, it is a certainty to
schedule — it needs those sessions genuinely idle and the user watching.

**Acceptance after each step:** node answers `/v1/health` with
`version: 0.13.0` and `contract_version: 1`; its session list matches the
precheck exactly; agent generation changed (proving the restart happened)
while session names and PIDs did not; the controller lists the node
ONLINE; `terminal_send_text` dry-run returns `would_send` for one session
on it.

**Rollback:** re-point the node at its previous checkout and restart the
agent. Nothing in the upgrade migrates node-local state, so rollback is
symmetric. On `dell-5530` there is no rollback for a lost ConPTY session —
which is the whole reason it is last.

**Estimated downtime:** per Linux/macOS node, seconds of API unavailability
for that node only; sessions unaffected. For `dell-5530`, its three
sessions end and must be reopened.

**Not scheduled here, and needing a decision first:** standing up a second
controller. Until one exists, M5 buys reachability, not failover.

## Declaring v1 production-ready

Met today:

- [x] no session lost across controller restart, twice, measured by PID
- [x] default-open access with explicit lock, verified on the running fleet
- [x] recovery cannot resurrect or duplicate, measured (0 eligible of 270)
- [x] full suite green
- [x] dashboard verified at real viewports against the real served bytes
- [x] no secret in any served surface; auth never bypassed
- [x] DB integrity and migrations clean

Still required:

- [ ] every node on 0.13.0 / contract v1 (M1–M3, M5)
- [ ] cold-boot acceptance on at least two nodes (M4)
- [ ] commits pushed to a remote, so provenance is not one machine's disk

## Declaring M910 safe-off

All of the above, plus:

- [ ] a second controller actually serving, verified by pointing a client
      at it while M910 is up
- [ ] `dell-5530` reachable over Tailscale (M5)
- [ ] one rehearsed failover: M910 stopped deliberately, fleet still
      operable from the second controller, then M910 restored
