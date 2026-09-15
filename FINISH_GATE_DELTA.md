# Finish Gate — delta, 2026-09-15

`FINISH_GATE.md` is the report of record from commit `d661ffc`. This file
says only what has changed since, and what is still unmet, measured
against live state rather than re-read from that report.

Measured on m910 at `21608d0`. Node facts come from the live node
registry (`~/.local/state/terminal-mcp/nodes.db`), not from the report.

## What the fleet actually looks like now

| node | status | agent | contract | endpoint |
| --- | --- | --- | --- | --- |
| dell-linux | online | **0.13.0** | **1** | tailnet `100.81.85.120` |
| hp-linux | online | 0.12.0 | 0 | tailnet `100.67.53.117` |
| dell-5530 | online | 0.12.0 | 0 | **LAN only** `192.168.1.250` |
| macbook | **offline** since 2026-09-14T00:11Z | 0.12.0 | 0 | LAN `192.168.1.138` |
| local (m910) | online | — | 1 | local |

The report said "contract **v0** (all four nodes)". That is no longer
true: **M3 has happened** — dell-linux is on 0.13.0 / contract 1. The
other three have not moved.

`macbook` still reports `platform: linux`, which is
`blg_20dc778df7ac`, not a new finding.

## v1 production-ready — exact unmet conditions

Three bullets were open in the report. All three are still open, but two
of them are now narrower:

1. **every node on 0.13.0 / contract v1** — unmet, and now specifically
   M1 (macbook), M2 (hp-linux), M5 (dell-5530). M3 is done.
   *macbook must come back online before M1 can even start; it has been
   offline for ~29h.*
2. **cold-boot acceptance on at least two nodes** — unmet, zero of two
   observed. Nothing in this pass touched it.
3. **commits pushed to a remote** — unmet, but no longer unexplained.
   `main` is **192 commits** ahead of `origin/main` (`695b31c`). m910
   holds no push credential and none was created. A host that can push
   has been identified and both hops are dry-run verified — see
   `blg_d7582428037c`. It is ready to run and deliberately not run:
   publishing 192 commits outward is not covered by any standing
   approval.

## M910 safe-off — exact unmet conditions

All of the above, plus:

4. **a second controller actually serving** — unmet. m910 still runs the
   only `terminal-mcp-http`. This is a decision, not a task.
5. **dell-5530 reachable over Tailscale** — unmet, and directly visible
   in the table above: its endpoint is a `192.168.x` LAN address while
   every other remote node is on `100.x`.
6. **one rehearsed failover** — unmet, and gated on 4.

## One thing that got in the way of measuring, worth knowing

`hp-linux` could not be reached from m910 during this pass:
`Host key verification failed`. That has to be fixed before M2 or M4 can
be attempted, and it is not a node fault — m910's known_hosts is the
side that is wrong.

The real-fleet test suites (`test_supervisor_v2`, `test_transports`)
share `~/.local/state/terminal-mcp`. Running them back-to-back from two
different checkouts makes the second run fail on the first run's
leftovers — 26 spurious failures, all of which pass alone. Any future
gate run must run these suites one checkout at a time, or it will
measure its own tooling instead of the code.
