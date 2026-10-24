# HP controller contention — 2026-10-24

The HP incident has two separate signals. `capacity_status=overloaded` is justified by `disk free 6% < 10%`, even though CPU and RAM are below their thresholds. It is not proof of provider concurrency saturation.

The sampled three-hour journal window (bounded to 3,000 entries) contained 29 `sqlite3.OperationalError: database is locked` exceptions and two 30-second `BACKEND_BUSY` errors. The SQLite exceptions ran through queue inventory → `lane_status` → `_ensure_lane`. Inventory was writing every historical lane: 278 lanes, while queue tasks were 3 BLOCKED, 246 CANCELLED, 41 COMPLETED, 1 SKIPPED. Queue loop reconciliation and dashboard pending counts competed for that writer lock. This is a demonstrated contention source; the journal does not prove it caused every upstream timeout.

At the initial health sample the provider governor had 0 active, 0 reserved and 0 queued, with limits unchanged (global 4, Claude 1, Codex 2, OpenRouter 2). Memory/pid cgroup limits had no OOM or task-limit events. Memory pressure PSI was zero. Remote clients use bounded urllib requests, not a shared connection pool. The old error's assertion that timeout was “almost certainly” a provider limit was unsupported.

## Changes

- Inventory uses a consistent read-only snapshot, with no per-lane insert. Dashboard counts use a single aggregate query and the canonical pending statuses.
- Read fan-out has 16 process-wide slots, at most 2 per node/controller. Matching concurrent reads join the in-flight future. A caller timeout does not free the slot until the worker actually finishes. There is no executor backlog; excess reads receive a retry hint. This also bounds single-node calls, previously executed inline without a deadline.
- Default fleet read budget is 5 seconds. Existing per-node socket timeouts and durable node health backoff remain in effect.
- `/health/metrics` exposes read slots, limits, zero queue depth, and coalescing/rejection/timeout counters. Provider metrics remain separate.
- Timeout messages no longer infer provider saturation. Mutations are never automatically replayed; callers must reconcile task receipts before retry. Read retries should use delay plus jitter.

## Validation

Four new regressions were observed failing before their corresponding changes: single-node deadline, repeated timeout amplification, pending-count under a SQLite writer, and lane inventory under a SQLite writer. A fifth checks admission saturation and slot recovery. Related tests: 557 passed, 8 deselected; final focused check: 93 passed. Tests used the suite's isolated state/tmux fixtures.

Before deployment, ten sequential loopback MCP `terminal_turn` calls per action gave p95: list_nodes 0.181s, list_sessions 0.887s, inspect 0.464s, zero reported errors. This quiet sample does not reproduce the earlier outage. The historical tool log's three-hour sample had list_sessions p95 6.508s (38 calls); it is not directly comparable to the controlled ten-call sample.

## Deployment and rollback

Deploy only on HP with the existing Python environment and unchanged production config, from a committed worktree. Back up queue, direct task, session registry, ownership, and lease databases using SQLite backup. Record all tmux pane IDs/PIDs. Preserve `KillMode=process`; do not kill tmux or agents. Add one final systemd drop-in selecting the new source. Restart the controller alone and verify `/version`, health, MCP list_nodes/list_sessions/inspect, and pane identities.

Rollback removes only that new drop-in, reloads systemd and restarts the controller to the previous immutable release `4b9c44d64fa8`. No schema changes require database rollback. Do not restore a backup over tasks created since deployment.

Controller and sidecar were separately moved to immutable release `b3de57c` on HP. Sidecar is on loopback port 8768 (an initial probe of 8767 returned 404; the service log established the correct port). Its single `terminal_turn` tool was checked for all three read actions; each returned `status=OK`.

## Residual risks and scope

Disk pressure remains. About 259 MB of app log files are older than 30 days; that cannot recover the roughly 10 GB needed to meet the existing free-space threshold. No projects, worktrees, caches, task history or sessions were removed. Cleanup needs proof files are unused, not just detached tmux status. Three BLOCKED tasks were preserved; no stale task was declared completed. A quiet post-deploy check cannot establish long-term provider health or zero future timeouts.

Doctor work on NEC1 and the separate session-cleanup PR are untouched. This change does not add cleanup automation or alter provider concurrency. Completed results are not cached: only overlapping reads are coalesced, so serial callers must still respect backoff.

## Verified HP rollout

Controller restarted at 21:16:26 +07; sidecar separately at 21:17:24 +07. `/version` reports `b3de57c296d0544eaee31efde2c594819775d268`, clean source. Readiness passes. All 10 pane IDs/PIDs and the production config SHA256 are unchanged. Five durable databases were backed up (8,204,288 bytes); no data restore or schema migration occurred.

| Loopback action, 10 sequential samples each | Before p95 | After p95 | Errors |
|---|---:|---:|---:|
| list_nodes | 0.181s | 0.216s | 0 / 0 |
| list_sessions | 0.887s | 1.301s | 0 / 0 |
| inspect | 0.464s | 0.672s | 0 / 0 |

These quiet samples show responsiveness, not a latency improvement. A bounded four-request overlapping listing check returned in 0.847–1.193s, with 16 coalesced node reads, zero request errors and read workers back to active=0/queued=0. Provider active/reserved/queued remain zero. The first several post-deploy minutes contain no controller ERROR entry; this is not a long soak.

Queue status counts remain unchanged. Direct-task inventory includes 1 WAITING_INPUT and 26 BLOCKED records, which were preserved rather than guessed stale. No session was killed, no task status was rewritten, and no file cleanup occurred (deleted bytes: 0). Disk free remains about 1.3 GB available to this user; HP correctly remains overloaded by disk policy.

Local evidence and rollback scripts: `/home/kimex/workspace/.p0-terminal-evidence-20261024/`. Backups and raw inspect payloads stay local and are not published. Controller and sidecar each have one removable `zzzzzzzzzz-p0-contention-20261024.conf` drop-in. Remove only those drop-ins and restart the corresponding service to roll source back; preserve live databases.
