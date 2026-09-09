# Project/Agent Coordination — Gap Analysis & Target Architecture (Phase B)

Companion to `PROJECT_COORDINATION_AUDIT.md` (the evidence). This document
compares what exists to the target and picks a coordination model.

## The target, restated

```
ChatGPT (product / portfolio director)
        │  goals, priorities, feedback
        ▼
┌───────────────────────────────────────────────────────────┐
│ TerminalMCP core — DETERMINISTIC                          │
│  projects · sessions · nodes · task leases · locks        │
│  event bus · queues · portfolio scheduler (no LLM)        │
└───────────────────────────────────────────────────────────┘
        │ per project
        ▼
  Project Coordinator (LLM, event-driven, wakes only to reason)
        │ decompose · prioritise · resolve dependencies
        ▼
  workers ──► verify ──► integrate ──► preview
```

## Where the gap actually is

The audit's shape is unusual and worth stating plainly: **the dangerous parts are
done and the easy parts are missing.**

Already real and production-exercised: atomic claim with `BEGIN IMMEDIATE`,
cross-process TTL leases, an explicit task state machine that refuses invalid
transitions, a **fail-closed** dispatch gate, idempotency keys, and a decision log
that feeds a review-attempt budget. Those are exactly the things that are painful
and risky to build later.

What is missing is the layer *above*: nothing is **project-scoped**
(`queue_lanes.project` is `None` for all 35 lanes), there is no **event bus**
(three unrelated append-only logs), no **cross-project scheduler**, and no
**capability routing** for verification.

### Five bottlenecks the new architecture must remove

1. **No project dimension in the runtime.** The queue is lane/session-keyed and
   its `project` column is entirely unused, so "what is project X doing" cannot be
   answered from the queue at all — only from the new backlog store.
2. **Auto-dispatch is inert.** `queue.enabled=True` but **0 of 35 lanes** opted
   in, so every state transition still needs an explicit call. This *is* the
   coordination overhead the task wants removed.
3. **Verification is not a queue.** `VERIFYING` is a state of the same task in the
   same session, so a verifier cannot be a different worker, let alone one chosen
   by capability.
4. **A complete merge queue sits unused.** `integration_store` already has
   project-scoped, leased, claimable handoffs with conflict-rework routing and 68
   tests — and 0 rows. This is pure unexploited leverage.
5. **Capability is node-shaped, not tool-shaped.** We know a node is Windows with
   PowerShell and Codex; we cannot express "can run Playwright" or "can build
   WPF/WebView2", which is precisely the routing the target needs.

## Coordination model: three options

### A. One global LLM coordinator
- **For:** single brain, global priorities, simplest mental model.
- **Against:** every decision serialises through one context; blast radius of a
  bad/prompt-injected decision is the whole fleet; context grows with N projects;
  it would have to re-derive state the DB already holds. It also contradicts
  `coordinator.py`'s own documented stance — that gate is deterministic precisely
  because safety-critical dispatch decisions are mechanically checkable and
  getting them wrong is the failure this feature exists to prevent.

### B. Per-project LLM coordinator, no deterministic core
- **For:** isolation per project, small contexts.
- **Against:** every coordinator re-implements claim/lease/priority; N LLMs racing
  on shared nodes with no arbiter; non-deterministic resource allocation. The
  audit shows the deterministic primitives already exist — discarding them to let
  an LLM do bookkeeping would be a regression in both safety and cost.

### C. **Deterministic core + per-project LLM coordinator** ← recommended
- **For:** matches what is already built. The deterministic gate stays the thing
  that decides *safe to dispatch*; a per-project LLM is woken only for
  **decompose / prioritise / resolve dependency / interpret feedback** — the
  judgment work. Blast radius is one project. Coordinator state lives in the
  store, not in a Claude session's context, so a restart loses nothing.
- **Against:** two moving parts; needs an explicit wake contract.
- **Decisive evidence:** `coordinator.py` already exposes a pluggable
  `scope_reasoner` callable for the single check that genuinely needs reasoning,
  with a conservative heuristic as the default. The seam for option C is
  *already in production code* — C is an extension, A and B are rewrites.

**Recommendation: C.** The deterministic scheduler stays LLM-free and allocates by
project priority × capability × lease. The per-project coordinator is an
event-driven consumer that never runs worker code.

## Target state flow

```
                    ┌──────────────┐
 ChatGPT ──goal────►│ project_*    │  submit_goal / backlog / report
                    │  MCP APIs    │
                    └──────┬───────┘
                           ▼
                    ┌──────────────┐   TASK_CREATED
                    │  BACKLOG     │──────────────┐
                    │ (project DB) │              ▼
                    └──────────────┘      ┌───────────────┐
                                          │  EVENT BUS    │
   ┌──────────────────────────────────────┤ (project-     │
   │                                      │  scoped)      │
   │  WORKER_IDLE / VERIFY_PENDING /      └───────┬───────┘
   │  MERGE_CONFLICT / TEST_FAILED                │
   │  PREVIEW_FAILED / USER_FEEDBACK              │ wakes
   ▼                                              ▼
┌────────────────────┐                  ┌────────────────────┐
│ PORTFOLIO SCHEDULER│                  │ PROJECT COORDINATOR│
│ deterministic      │                  │ LLM, event-driven  │
│ priority×capability│                  │ decompose/prioritise│
│ ×lease, no LLM     │                  │ /resolve deps      │
└─────────┬──────────┘                  └─────────┬──────────┘
          │ claim (atomic + lease)                │ writes tasks/deps
          ▼                                       ▼
   ┌─────────────┐   VERIFY_PENDING   ┌──────────┐  READY_FOR_MERGE ┌──────────┐
   │  WORKER     │───────────────────►│ VERIFIER │─────────────────►│INTEGRATOR│
   │ (session)   │                    │(capability│                 │(handoff  │
   └─────────────┘                    │  routed) │                  │  queue)  │
                                      └──────────┘                  └────┬─────┘
                                                                         ▼
                                                                   ┌──────────┐
                                                                   │ PREVIEW  │
                                                                   └──────────┘
```

Existing pieces in that diagram: **BACKLOG** (new, done), **atomic claim + lease**,
**WORKER**, **VERIFY as a state**, **INTEGRATOR** (built, unused).
New pieces: **EVENT BUS**, **PORTFOLIO SCHEDULER**, **capability routing**,
**PREVIEW**, and making the **COORDINATOR per-project + event-driven**.

## Reuse vs build

| Need | Verdict | Basis |
|---|---|---|
| Task state machine | **REUSE** `queue_store` | Production-verified, refuses invalid transitions |
| Atomic claim + lease | **REUSE** `claim_next_task`, `PaneLeaseStore` | TOCTOU-closed, crash-recoverable |
| Dispatch safety gate | **REUSE + EXTEND** `coordinator.py` | Fail-closed; `scope_reasoner` is the LLM seam |
| Merge/integration queue | **REUSE (activate)** `integration_store/engine` | Project-scoped, leased, 68 tests, 0 rows — just unused |
| Per-task git isolation | **REUSE** `git_isolation_service` | Already wired into task creation + dispatch check |
| Project identity | **REUSE** `project_identity`, `discover_projects` | Canonical, fleet-wide, already proven |
| Project/backlog store | **REUSE** `backlog_db` | Already project-keyed, revisioned |
| Audit / decision log | **REUSE** `audit.db`, `queue_events` | Already records decisions + reasons |
| Idempotency / recovery | **REUSE** `idempotent_sends`, `recovery_engine` | Production-used |
| Node capability | **EXTEND** `nodes` table | Add a tool/runtime axis; `labels` already exists and is empty |
| **Event bus** | **BUILD (small)** | Three per-store logs cannot fan out or be subscribed to |
| **Portfolio scheduler** | **BUILD (small)** | `rebalance` is intra-project and planner-dependent |
| **Verify queue + capability routing** | **BUILD on existing states** | `VERIFYING` exists; the *queue* does not |
| **Preview queue** | **BUILD** | Nothing exists |
| **Project-level MCP APIs** | **BUILD on backlog tools** | 11 backlog tools exist; project verbs missing |

Only **five** genuinely new components — and three of them are thin.

## Implementation status (2026-09-09)

**P0.1 Project Dimension — DONE.** `queue_tasks.project_id` added by
migration v6 (nullable), `queue_lanes.project` (added v4, never populated)
**reused** rather than duplicated, and `backlog_projects` reused as the
project registry — no third store. Legacy rows stay `NULL` and behave
exactly as before.

**P0.2 Event Bus — DONE.** New `events.db` (`event_bus.py`) with
publish/list/claim/ack/release/fail/retry/stats, `BEGIN IMMEDIATE` claim,
claim-token leases, unique idempotency keys and an attempt budget. The
three existing per-store logs are untouched.

**Deliberately NOT done in P0:** no consumer loop is started, no
autonomous coordinator, no portfolio scheduling. The bus is a durable
mailbox that something must explicitly ask to read.
