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

### P0.4 Runtime task lease surfaced as an API — ✅ DONE (2026-09-09)
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
| P0.5 verify queue | M | **Med** | ~~Changes who verifies~~ **SHIPPED** — opt-in per task, old default kept |
| P0.6 resource lock | S | Low | **SHIPPED** — pane lease generalised, hot path byte-identical |
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

## P0.5 SHIPPED 2026-09-09 — verify queue with capability routing

Verification is now claimable work routed by capability. The `verify_jobs`
satellite table (migration v7) carries required capabilities, implementer,
branch/commit, verifier, evidence and an append-only audit trail; the task
keeps its existing status vocabulary, and **P0.5 added zero task statuses and
zero task transition edges** — asserted structurally by a test against
`VALID_TRANSITIONS`, not left to review.

The medium-risk item flagged in the table below ("changes who verifies — keep
old default") was handled by making the opt-in **per task**, via each task's
own `completion_policy["verify"]`, rather than a global switch. No existing
task has that key, so in-session verification remains the default for every
lane that exists, and there is no flag to forget to leave off.

- **Routing:** AND semantics over reported facts only — probed P0.3
  capabilities plus `platform`/`session_backend`/`shell_capabilities`. Nothing
  inferred; no application name special-cased. Including `platform` makes
  `windows` route to dell-5530 without redeploying its pre-P0.3 agent.
  `macos` is deliberately NOT routable (the node agent has no Darwin branch,
  so the MacBook reports `platform=linux`) — stated rather than papered over.
- **Evidence-gated pass:** more than a self-report, and not self-contradicted
  (`exit_code != 0` / `passed: false` / `tests_failed > 0` refused).
- **Lease reuse:** P0.4 semantics exactly — token + expiry, `BEGIN IMMEDIATE`,
  and handoff rotates the token so the old holder cannot mutate the result.
- **No verifier:** a visible hold with a routability reason, never a silent
  pass and never a dropped task.
- **Duplicates:** `UNIQUE (task_id, attempt)` in the schema, so restart-safe;
  a genuine retry gets its own job and the prior verdict survives.

12 new MCP tools (156 total), one read-only dashboard route. Migration v7
verified against a copy of the real production `queue.db` (38 tasks / 246
events, every existing row byte-identical, `verify_jobs` empty). Full suite
2369 passed.

## P0.6 SHIPPED 2026-09-09 — named-resource ownership lock

`lease.ResourceLockStore`: "no two agents touch the same file / module /
branch at once", built by GENERALISING the pane lease rather than writing a
second lock.

The reusable part was never the pane — it was the atomic check-and-set in
`acquire()`, the single statement whose exact shape was arrived at by
reproducing a real race (a `SELECT`-then-write let two callers both win).
That algorithm now lives in `_LeaseTable`, parameterised by table and key
column, and both stores are thin specialisations. **`PaneLeaseStore` is
unchanged** in table, columns, method names, signatures, return types and
TTL — it is on `core.py`'s send hot path — and the generated SQL is asserted
**byte-identical to the shipped statement**, not merely assumed equivalent
because the behavioural tests still pass.

Separate `resource_locks` table in the same `leases.db`: the two have
genuinely different lifetimes (20s vs minutes) and subjects, so mixing
long-lived agent locks into the table every send contends on would be risk
for no gain — while the shared file inherits the existing `/health/ready`
check and backup procedure.

What a resource lock adds that a pane does not:
- **Project scoping** — `src/app.py` in one project is a different resource
  from `src/app.py` in another; a global key space would make unrelated
  repos block each other. Uses P0.1's canonical `project_id`.
- **Holder reporting on refusal** — a primitive that only says "no" leaves
  the caller nothing to act on. Refusals name the owner, reason and expiry.
- **All-or-nothing `acquire_many`** — the deadlock story. Two agents each
  needing `{a, b}` and taking them one at a time can finish holding one
  apiece forever; one transaction makes that impossible.
- **Audited operator override** — `force_release` is a separate verb, not a
  flag, requiring an actor and a reason and reporting whose lock was broken.

**Advisory by design.** Nothing can physically stop an agent editing a file
it did not lock; these are a durable, crash-recoverable way for cooperating
agents to agree. Deliberately **no waiter queue** — blocking inside a lock
primitive is how a fleet deadlocks.

8 new MCP tools (164 total). Migration v2 verified on a copy of the real
production `leases.db` (`pane_leases` byte-identical, `resource_locks`
empty, re-apply a no-op). 35 new tests.

## Next: P0.7 (P0.1-P0.6 shipped)

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

**P0.4 shipped 2026-09-09**: `renew_task_lease`, `release_task_claim`,
`handoff_task` and `lease_holder`, all token-gated and BEGIN IMMEDIATE.
Notably `reconcile_stale_claims`' own docstring already assumed "a healthy
engine keeps renewing ... well within the lease" -- renew is the method
that assumption was written against and which did not exist, so a
genuinely-alive worker on a long task could be reconciled out from under
itself.

`handoff_task` is deliberately distinct from the existing
`reassign_task`: that one moves a task's LANE and REFUSES an actively
claimed task (TaskAlreadyClaimedError), which is exactly the worker ->
verifier case. Handoff moves the CLAIM instead -- same task_id, fresh
token for the receiver, appended to the same migration_history trail.

**P0.5 (verify queue with capability routing) is now next**, and every
primitive it needs is in place: P0.2's bus carries VERIFY_PENDING, P0.3
routes by capability, and P0.4 lets a verifier take the task from the
worker without it round-tripping through QUEUED.

**A deployment note P0.3 surfaced:** capabilities are reported by the
NODE, so remote nodes only advertise them after their agent is redeployed.
Until then they report an empty list and are correctly treated as
"not known to be capable" rather than assumed capable.
