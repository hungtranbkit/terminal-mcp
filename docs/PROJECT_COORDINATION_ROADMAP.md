# Project/Agent Coordination — Roadmap (Phase C, design only)

No code was written for this. Sequencing, contracts and risk only.
Evidence: `PROJECT_COORDINATION_AUDIT.md`. Rationale: `PROJECT_COORDINATION_ARCHITECTURE.md`.

## Principle

Extend the primitives that are already production-verified; build new components
only where the audit proved nothing extensible exists (event bus, portfolio
scheduler, verify/preview queues, project APIs). Every step must leave the 134
existing MCP tools working unchanged.

---

## P0 — remove coordination overhead now (highest value / lowest risk)

### P0.1 Give the runtime a project dimension — ✅ DONE (2026-09-09)
- **Change:** populate `queue_lanes.project` and `queue_tasks.metadata.project_id`
  with the **canonical** `project_id`, resolved via `project_identity` /
  `controller.resolve_project_for_session(node, session)` — not the free-text
  label that is `None` on all 35 lanes today.
- **Reuse:** `project_identity.py`, `backlog_db`, existing columns.
- **Schema:** none required (both fields already exist).
- **Migration:** backfill from each lane's session `repo_root`; unresolvable lanes
  stay `NULL` and are simply excluded from project views.
- **Risk:** low. Additive; nothing reads `project` today.

### P0.2 Event bus (new, small) — ✅ DONE (2026-09-09)
- **Change:** one `events` table — `(id, project_id, type, subject, payload,
  created_at, consumed_by, consumed_at)` — plus `publish()` / `claim_next(type[],
  consumer)` using the **same `BEGIN IMMEDIATE` + lease pattern** as
  `claim_next_task`, so a consumer cannot double-process.
- **Types (P0 subset):** `TASK_CREATED`, `TASK_READY`, `WORKER_DONE`,
  `VERIFY_PENDING`, `TEST_FAILED`, `USER_FEEDBACK`.
- **Reuse:** claim/lease semantics verbatim; existing per-store logs stay as-is
  (they are good audit records) and *emit* into the bus.
- **Why build:** `queue_events`/`integration_events`/`supervisor_events` are
  append-only per-store logs with no subscribe and no cross-store ordering.
- **Risk:** low-medium. New table, no change to existing writers.

### P0.3 Worker capability registry (tool/runtime axis) — ✅ DONE (2026-09-09)
- **Change:** extend node heartbeat with a `capabilities` list — e.g.
  `playwright`, `dotnet`, `wpf`, `webview2`, `docker`, `node`, `python` — detected
  the way `agent_types` already is (`available_agent_types` probes real launchers).
- **Reuse:** `nodes.shell_capabilities`/`agent_types` pattern; `labels` column is
  already there and empty.
- **Schema:** one nullable column (or reuse `labels`).
- **Risk:** low. Detection must be *probe-based*, never declared, matching the
  existing "capability is reported by the node, never guessed centrally" rule.

### P0.4 Runtime task lease surfaced as an API
- **Change:** expose `claim/renew/release/handoff` for tasks the way panes already
  have them, so a worker crash frees a task deterministically.
- **Reuse:** `queue_tasks.claim_token`/`lease_expires_at` already exist and are
  stamped atomically; `PaneLeaseStore` is the renewal model.
- **Gap to close:** there is no **handoff** verb for tasks (only handoffs exist,
  in the unused integration store).
- **Risk:** low.

### P0.5 Verify queue with capability routing
- **Change:** on `WORKER_DONE`, publish `VERIFY_PENDING{project, capability}`; a
  verifier claims by capability rather than verification being a state of the same
  task in the same session.
- **Reuse:** `VERIFYING`/`VERIFIED` states, `verification_nonce`/
  `verification_evidence` columns, and the completion gate all already exist.
- **Risk:** medium — this changes who verifies. Must keep the current in-session
  verification as the default until a verifier pool actually exists.

### P0.6 Ownership lock for shared resources
- **Change:** generalise the pane lease into a named-resource lease
  (`resource_key` = file/module/branch) with the same TTL + owner semantics.
- **Reuse:** `lease.PaneLeaseStore` is already `pane_key`-generic in everything but
  its name.
- **Risk:** low.

### P0.7 Project APIs for ChatGPT
- **New tools:** `terminal_project_status`, `_submit_goal`, `_events`, `_report`,
  `_pause`, `_resume`, `_assign`.
- **Reuse:** the 11 `terminal_backlog_*` tools cover backlog/plan already;
  `terminal_project_list` exists. Pause/resume exist per **lane** and generalise.
- **Risk:** low; additive tools.

**P0 outcome:** ChatGPT can ask "what is project X doing", submit a goal, and let
the system route work — without a human pushing every transition.

---

## P1 — make it autonomous and per-project

- **P1.1 Per-project event-driven coordinator.** Wrap `CoordinatorGate` in a
  per-project consumer that wakes on bus events instead of the polling
  `QueueLoop`. Keep the deterministic gate for *dispatch safety*; the LLM is woken
  only to decompose/prioritise/resolve dependencies — via the existing
  **`scope_reasoner` seam**, which is already pluggable.
- **P1.2 Activate the integration queue.** `integration_store`/`integration_engine`
  are complete and 68-test-covered with **0 production rows**. Turning them on for
  one real project is the cheapest large win available.
- **P1.3 Enable auto-dispatch for one real lane.** Requires the pre-existing gate
  (REQUIREMENTS Backlog item 1: 0 dropped / 0 duplicate across restart+reconnect).
- **P1.4 Dependency DAG.** Promote `depends_on` (already fail-closed and
  cross-lane) into a graph view with cycle detection.
- **P1.5 Dashboard drill-down:** project → module → task → worker → verify/merge.

## P2 — portfolio and polish

- **P2.1 Portfolio scheduler** — deterministic allocation across projects by
  priority × capability × lease. Today's `rebalance` is intra-project and depends
  on the unused planner.
- **P2.2 Preview-fast queue** — nothing exists; needs its own runner and events.
- **P2.3 Module/outcome owners** on child tasks (planner exists, unused).
- **P2.4 Fleet-wide audit aggregation** (audit is per-node today).

---

## Concurrency, TTL and idempotency contracts

- **Claim:** `BEGIN IMMEDIATE` before read, claim token + lease stamped in the same
  transaction — already proven in `claim_next_task`.
- **TTL:** task lease 300 s (current default); pane lease sized above one
  send+verify; **no renewal thread** — renewal is explicit, matching `lease.py`'s
  documented reasoning.
- **Expiry:** an expired lease is reclaimable; reconciliation pushes the row back
  to its previous state rather than inventing one.
- **Idempotency:** reuse `audit.idempotent_sends` (437 live rows) for every
  externally-triggered action; merges are naturally idempotent (`git merge` of an
  ancestor is a no-op) as `integration_engine` already documents.
- **Fail-closed:** any unreadable evidence ⇒ `NEEDS_HUMAN`, never `READY`. This is
  an existing invariant and must not be relaxed for autonomy.

## Security / approval boundaries

- Autonomy stays behind the two existing stacked gates: global
  `config.queue.enabled` **and** per-lane `auto_dispatch_enabled` (both currently
  off in effect). Do not collapse them into one.
- LLM coordinator output is **untrusted input**: it may propose tasks and
  priorities; it must never bypass the deterministic dispatch gate, and its
  proposals must be audited like any other write.
- Backlog/coordinator text is agent-written — already marked `untrusted_fields` in
  the brief; preserve that when it reaches new surfaces.

## Canonical backlog vs runtime ownership

This repo has **no `TASKS.json`**; the canonical backlog is
`.terminal-mcp/backlog.json`, tracked, with the controller DB authoritative and the
file an export/import projection. That already solves the stated git-conflict
problem, for a reason worth keeping: **runtime ownership never touches the file.**
Claims, leases and status live in the DB; the file is written only on explicit
export. Combined with per-task **git worktrees** (`git_isolation_service`), two
workers never contend on one branch or one file.

Recommendation: keep this model. If a `TASKS.json` is wanted for human editing,
make it another export target — never a second writable source of truth.

## Complexity / risk

| Item | Complexity | Risk | Note |
|---|---|---|---|
| P0.1 project dimension | S | Low | Additive backfill |
| P0.2 event bus | M | Low-Med | New table, reuses claim semantics |
| P0.3 capability axis | S | Low | Must be probe-based |
| P0.4 task lease API | S | Low | Columns exist |
| P0.5 verify queue | M | **Med** | Changes who verifies — keep old default |
| P0.6 resource lock | S | Low | Generalise pane lease |
| P0.7 project APIs | S | Low | Additive tools |
| P1.1 per-project coordinator | L | **Med-High** | LLM in the loop; keep gate deterministic |
| P1.2 activate integration | M | **Med** | Real merges into real branches |
| P2.1 portfolio scheduler | M | Med | Needs P0.1 + P0.3 first |
| P2.2 preview queue | M | Low | Greenfield |

## Proposed MCP APIs

```
terminal_project_status(project_id)          -> tasks by state, workers, blockers
terminal_project_submit_goal(project_id, goal, priority)
terminal_project_events(project_id, since, types[])
terminal_project_report(project_id, window)  -> throughput, failures, verify/merge
terminal_project_pause(project_id) / _resume(project_id)
terminal_project_assign(project_id, task_id, node_id|capability)

terminal_task_claim(task_id, worker, ttl)  / _renew / _release / _handoff
terminal_event_publish(project_id, type, subject, payload)
terminal_event_claim(types[], consumer, ttl)
terminal_resource_lock(resource_key, owner, ttl) / _unlock
terminal_verify_claim(capability, worker)
terminal_node_capabilities(node_id)          -> tool/runtime axis
```

Existing and unchanged: all 11 `terminal_backlog_*`, `terminal_project_list`, the
20 `terminal_queue_*`, 16 `terminal_task_*`, 13 `terminal_integration_*`.

## Start here — DONE

**P0.1 + P0.2 shipped 2026-09-09**, additive and backward-compatible:
migration v6 verified against a copy of the real production `queue.db`
(37 tasks / 241 events preserved, every legacy row `project_id IS NULL`),
and the bus proven with two real OS processes claiming 30 events with zero
duplicates and zero losses. Full suite 2276 passed.

## Next: P0.4 (P0.3 shipped)

**P0.3 shipped 2026-09-09**: probed tool/runtime capabilities
(git/node/npm/python/docker/dotnet/playwright/tmux/rustc/go/java) now ride
the heartbeat, stored in a dedicated `nodes.capabilities` column and
queryable with AND semantics via `terminal_node_capabilities`. It was
built for these reasons, which still describe why it mattered:

1. It is the **last P0 prerequisite for routing.** P0.5 (verify queue) and
   P2.1 (portfolio scheduler) both need to answer "which node can do this
   kind of work"; today the fleet only knows platform/shell/agent_types.
2. It is **low risk and additive** — one nullable column (or the already
   empty `labels`), populated by probe the way `available_agent_types`
   already probes launchers. Nothing routes on it until something asks.
3. The fleet **already proves the need**: dell-5530 is Windows/ConPTY,
   m910 and macbook are POSIX/tmux, and macbook has claude+codex while
   m910 has claude only — but nothing expresses "can run Playwright" or
   "can build WPF/WebView2", which is exactly the Linux-HTML vs
   Windows-WPF split the target architecture routes on.

**P0.4 (task lease API) is now next** and is nearly free: the
`claim_token`/`lease_expires_at` columns already exist and are stamped
atomically by `claim_next_task`; only the renew/release/handoff verbs are
missing. With P0.1-P0.3 in place it is the last primitive P0.5's verify
queue needs before a verifier other than the worker can safely hold a
task.

**A deployment note P0.3 surfaced:** capabilities are reported by the
NODE, so remote nodes only advertise them after their agent is redeployed.
Until then they report an empty list and are correctly treated as
"not known to be capable" rather than assumed capable.
