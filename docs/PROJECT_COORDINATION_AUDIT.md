# Project/Agent Coordination — Capability Audit (Phase A)

**Date:** 2026-09-09 · **Method:** source + tests + **live production DB row counts**.
Nothing below is inferred from a filename. "Production-used" means real rows in
`~/.local/state/terminal-mcp/*.db` on the live controller.

## Ground truth: what is actually exercised

| Store | Rows (live) | Verdict |
|---|---|---|
| `session_knowledge.db` | 3462 sessions / 52692 chunks / 131592 checkpoints | **heavily used** |
| `audit.db` | 12434 input_audit / 437 idempotent_sends | **heavily used** |
| `session_registry.db` | 522 records / 4014 drop_events | **heavily used** |
| `queue.db` | 37 tasks / 35 lanes / **241 events** | **used** |
| `grants.db` / `bindings.db` | 25 / 7 | used |
| `nodes.db` | 4 nodes / 4 status events | used |
| `supervisor.db` | 60 events, 13 actions, **0 watches, 0 policies** | partly used, autonomy inert |
| `backlog.db` | 1 project / 19 items | new (this session) |
| `leases.db` | **0** (TTL rows, empty at rest — expected) | used transiently |
| `integration.db` | **0 / 0 / 0 / 0** | **built + 68 tests, never used** |
| `planner_store.db` | **0** | built, never used |
| `pm_store.db` | **0 / 0** | built, never used |
| `release_store.db` | **0 / 0** | built, never used |

Two facts that reframe everything below:

1. **The Coordinator gate is real and production-exercised** — `queue_events`
   holds `COORDINATOR_DECISION` ×32, `COORDINATOR_READY` ×15,
   `COORDINATOR_NEEDS_REWORK` ×10, `COORDINATOR_NEEDS_HUMAN` ×6,
   `COORDINATOR_BLOCKED` ×1, and 15 tasks reached `COMPLETED` (= VERIFIED_DONE).
   The full enqueue → precheck → dispatch → run → verify → complete cycle has
   genuinely run.
2. **Auto-dispatch is inert.** `config.queue.enabled = True` globally, but
   **0 of 35 lanes** have `auto_dispatch_enabled=1`, and **`queue_lanes.project`
   is `None` for every lane**. Those 15 completions were driven by explicit
   calls, not by the loop.

## Capability matrix

| # | Capability | Status | Evidence (file · table · test) | Real behaviour & limits |
|---|---|---|---|---|
| 1 | Project registry | **PARTIAL → improved (P0.1)** | `project_identity.py`, `backlog_db.backlog_projects`, `controller.discover_projects()`, `tests/test_backlog.py` | Canonical `project_id` from normalised git remote; 7 checkouts of terminal-mcp across 3 nodes collapse to one id. Fleet discovery works (6 projects live). But only 1 project has state, and `queue_lanes.project` is unused (`None` everywhere) — the queue does not know about projects. |
| 2 | Persistent project knowledge | **PARTIAL** | `session_knowledge.py` (3462 sessions), `_project_matches` | Rich and durable, but **session-scoped**. Project filtering is a **fuzzy substring word-match** over `cwd/repo_root/display_name` — not the canonical `project_id`. No project-level state object. |
| 3 | Task registry / runtime state | **EXISTS** | `queue_store.queue_tasks` (37), `VALID_TRANSITIONS`, `tests/test_queue_store.py` | Full explicit state machine (QUEUED→PRECHECK→READY→DISPATCHING→RUNNING→VERIFYING→COMPLETED + BLOCKED/FAILED/PAUSED/WAITING_SESSION/DISPATCH_UNCERTAIN). Invalid transitions raise. Production-verified. |
| 4 | Atomic claim + lease | **EXISTS (+ P0.4 verbs)** | `queue_store.claim_next_task` (BEGIN IMMEDIATE + `claim_token` + `lease_expires_at`), `lease.PaneLeaseStore` (`acquire/renew/release/holder/prune_expired`), `integration_store.claim_next_handoff` | Genuine TOCTOU-closing atomic claim. Pane lease is cross-process, TTL-based, crash-recoverable. **Handoff** is the only claim path with an explicit `handoff` concept; task-level handoff between workers is not modelled. |
| 5 | Worker capability registry | **PARTIAL → EXISTS (P0.3)** | `nodes.db`: `platform`, `session_backend`, `shell_capabilities`, `wsl_available`, `agent_types`, `labels` | Real per-node capability: dell-5530 = `windows`/`windows_pty`/`["powershell","cmd"]`/wsl=1/`["shell","claude","codex"]`. **Missing the tool/runtime axis** the target needs — nothing expresses "has Playwright", "can build WPF", "has WebView2". `labels` exists but is empty everywhere. |
| 6 | Availability / heartbeat / quota | **PARTIAL** | `node_registry.classify_capacity`, heartbeat 20s, `capacity_status` | Heartbeat + EWMA-smoothed, duration-aware overload heuristic (healthy/busy/overloaded) is real and live. **`max_sessions` is stored but never enforced** — no admission control anywhere. |
| 7 | Shared-file / resource ownership lock | **PARTIAL** | `lease.py` (pane), `git_worktree.py` + `git_isolation_service.py`, coordinator's `expected_cwd` check | Per-task **git worktree + branch isolation** is real and wired into task creation (`terminal_task_create_isolated`), enforced at dispatch by the existing coordinator check. **No generic named-resource lock** (e.g. "own this file/module"). |
| 8 | Event bus | **PARTIAL → EXISTS (P0.2)** | `queue_events` (241), `integration_events` (0), `supervisor_events` (60) | Three **separate append-only per-store logs**, not a bus: no subscribe, no cross-store ordering, no fan-out. Real types include `ENQUEUED/CLAIMED/DISPATCHED/STARTED/VERIFYING/VERIFIED/COORDINATOR_*/LANE_PAUSED`. **Absent from the target list:** `WORKER_IDLE`, `PREVIEW_FAILED`, `USER_FEEDBACK`, `MERGE_CONFLICT` (integration has its own equivalents but unused). |
| 9 | Verify queue by capability | **PARTIAL** | `VERIFYING`/`VERIFIED` states (15 real), `queue_engine` verification, `verification_nonce`/`verification_evidence` columns | Verification is a **state of the same task in the same session**, not a queue a separate verifier claims. No capability routing. |
| 10 | Merge / integration queue | **BUILT, UNUSED** | `integration_store.py` (pipelines/handoffs/batches/events), `claim_next_handoff(project, lease_seconds)`, `integration_engine.tick(project)`, 68 tests | A genuine **project-scoped, leased, claimable merge queue with its own state machine and conflict-rework routing** already exists — and has **0 production rows**. This is the single biggest piece of already-built leverage. |
| 11 | Preview-fast queue | **MISSING** | — | No preview concept anywhere in source or DB. |
| 12 | Per-project coordinator, event-driven | **PARTIAL** | `coordinator.py` (production-used, 32 decisions), `queue_loop.py` | Gate is **deterministic by explicit design** and **fail-closed** (any unreadable evidence → NEEDS_HUMAN, never READY). It has a **pluggable `scope_reasoner`** for the one judgment-needing check — the natural LLM seam. But it is **per-task, per-lane/session — not per-project**, and the loop **polls** rather than reacting to events. |
| 13 | Global portfolio scheduler | **MISSING** | `queue_service.rebalance(project, sessions)` | Rebalance moves tasks **between sessions inside one project label**, dry-run by default, and depends on `planner` (0 rows). No cross-project resource allocation, no priority arbitration between projects. |
| 14 | Module/outcome owner on child tasks | **PARTIAL** | `planner_store.plan_proposals` (0 rows), `parent_task_id`/`acceptance_criteria` in task `metadata` | Split-into-children exists via metadata on ordinary tasks (deliberately no new table). Never used in production. No "module owner" concept. |
| 15 | Dependency graph / DAG | **PARTIAL** | `queue_tasks.depends_on` (JSON list), enforced in `next_dispatchable_task` | **Fail-closed and cross-lane**: every `depends_on` id must be COMPLETED, and a *missing* id counts as unmet. It is a dependency **list**, not a graph object — no cycle detection, no critical path, no visualisation. |
| 16 | Project APIs for ChatGPT | **PARTIAL** | 134 MCP tools; `terminal_project_list` + 11 `terminal_backlog_*` | Backlog CRUD/dispatch/complete is strong. **Missing at project level:** `submit_goal`, `plan`, `assign`, `events`, `report`, `pause`, `resume`, `status`. Pause/resume exist only per **lane** (`LANE_PAUSED` ×6). |
| 17 | Runtime state ↔ canonical backlog sync | **PARTIAL** | `backlog_service.export_file/import_file`, `git_isolation_service` | Controller DB is authoritative; file is an export/import projection with merge-by-id. Per-task worktrees mean workers don't contend on one branch. **There is no `TASKS.json` in this repo** — the canonical backlog is `.terminal-mcp/backlog.json` (tracked). |
| 18 | Idempotency / recovery / restart | **EXISTS** | `audit.idempotent_sends` (437), `claim_token` reconcile, `recovery_engine.py`/`recovery_loop.py`, `AGENT_GENERATION` | Idempotency keys are production-used. Stale-claim reconciliation, restart-safe ticks, and an (off-by-default) auto-recovery engine exist. Node `agent_generation` distinguishes process lifetimes. |
| 19 | Audit trail / decision log | **EXISTS** | `audit.db` (12434), `COORDINATOR_DECISION` events, `queue_events` (241), `supervisor_actions` | Every send is audited with hashes (never raw prompt text). Coordinator decisions are persisted **with their reasons** and re-read to enforce a review-attempt budget. |
| 20 | Dashboard observability | **PARTIAL** | `/dashboard`, `/dashboard/nodes`, `/dashboard/tasks`, `/dashboard/backlog` | Sessions, nodes, task board and backlog are all visible. **No project → module → task → worker → verify/merge/preview drill-down**; nothing surfaces integration/preview at all. |

## Score

`EXISTS = 1`, `PARTIAL = 0.5`, `BUILT-UNUSED = 0.75`, `MISSING = 0`:

- EXISTS (4): items 3, 4, 18, 19
- BUILT-UNUSED (1): item 10
- PARTIAL (13): 1, 2, 5, 6, 7, 8, 9, 12, 14, 15, 16, 17, 20
- MISSING (2): 11, 13

**≈ 11.25 / 20 ≈ 56 % at audit time (2026-09-09, pre-P0).**

**After P0.1-P0.4 (implemented 2026-09-09): ≈ 13 / 20 ≈ 65 %.**
Item 8 (event bus) moved PARTIAL → EXISTS; item 1 (project registry) gained
a real runtime dimension (`queue_tasks.project_id`, `queue_lanes.project`
now populated-capable) on top of the existing `backlog_projects` registry.
Item 5 (worker capability) moved PARTIAL → EXISTS: the tool/runtime axis
it lacked is now probed per node and queryable with AND semantics. Item 4
gained the post-claim verbs it was missing (renew/release/handoff) —
including renew, which `reconcile_stale_claims` already assumed existed.
Items 9/11/13 are unchanged — P0 deliberately did not touch verification
routing, preview, or portfolio scheduling.

The weighting matters more than the number: the *hard, safety-critical* primitives
(atomic claim, lease, state machine, fail-closed gate, idempotency, audit) are the
ones that EXIST. What is missing is mostly **orchestration above them** — project
scoping, routing, and a real event bus.
