# Work Runtime V1 — audit, reuse mapping, and what is actually new

## The finding that shaped this design

The approved template asks for a durable work runtime: goal → plan/DAG →
worker → verifier → revision → approval → resume → complete, with a
persistent queue, atomic leases, guarded dispatch and restart recovery.

Audited before writing any code. **Most of that already exists in this
repository under different names.** Building a second one would have been the
single worst outcome available: two queues that disagree about which task is
running, two dispatchers racing to send the same prompt, and a second state
machine to keep in sync with the first.

So Work Runtime V1 is a **binding layer**, not a new engine. The table below
is the contract for that: anything with a REUSE verdict is not reimplemented
here, and a future change to it belongs in the owning module.

## EXISTING PRIMITIVE → REUSE FOR WORK

| Template requirement | Existing primitive | Verdict |
|---|---|---|
| Persistent queue, FIFO by priority/created_at, durable, restart-safe | `queue_store.py` — one lane per session, validated state machine, durable SQLite | **REUSE** |
| Task states QUEUED/CLAIMED/RUNNING/BLOCKED/… | `queue_store.VALID_TRANSITIONS` — centralised, refuses unlisted edges | **REUSE** |
| Idempotent enqueue key, no double-dispatch after restart | `queue_store` claim + `core.terminal_send_text` idempotency_key + `audit.idempotent_sends` | **REUSE** |
| Task DAG + cycle detection | `queue_store.dependency_cycle()` + `depends_on` | **REUSE** |
| Planner / decomposition | `planner_service.py` + `planner_store.py` | **REUSE** |
| Outcome contract (deliverable ≠ task count) | `outcomes.py` — acceptance criteria over N tasks | **REUSE** |
| Verifier as claimable work, routed by capability | `verify_queue.py` — a satellite of a task, adds zero task states | **REUSE** |
| Pre-dispatch safety gate | `coordinator.py` — deterministic, never sends | **REUSE** |
| Worker descriptor: node, session, caps, freshness, affinity | `worker_registry.py` + `pm_router.py` | **REUSE** |
| Atomic lease with expiry + reclaim | `lease.ResourceLockStore` — the atomic check-and-set that already won a real race | **REUSE** |
| Guarded transactional send PREPARE→…→COMPLETE | `core.terminal_send_text` + `adapters.py` delivery_state | **REUSE** |
| Coordinator loop shape | `MaintenanceLoop` / `RecoveryLoop` / `FleetSyncLoop` | **REUSE** |
| Append-only events | `event_bus.py` + queue events | **REUSE** |
| Secret redaction on any surfaced output | `redaction.redact_output` | **REUSE** |
| Node/session freshness and permission | `fleet_service`, `grants`, `session_registry` | **REUSE** |

## What is genuinely missing — and is therefore what V1 builds

1. **The `-work` suffix rule.** Nothing in the repository knows about it. This
   is the highest-risk requirement in the template, because today's queue
   engine will dispatch into *any* session that has a lane. Until this exists,
   "Work Runtime is opt-in" is not true.
2. **`work_runs`** — the missing top-level noun. `outcomes.py` is the closest
   thing and is scoped to a backlog item, not to a user goal with its own
   plan, approvals and lifecycle.
3. **Approval gates as first-class objects.** The queue has PAUSED and
   NEEDS_HUMAN; it has no approval record with a requester, an approver and a
   decision, and nothing prevents an agent from resolving its own gate.
4. **A work coordinator loop** binding the above to the existing engine.
5. **MCP `work_*` surface, HTTP routes, and a Work UI mode** with the `WORK`
   badge, kept separate from raw Terminal mode.

## The isolation rule, stated once

A session is eligible for automatic Work scheduling **only** if its name ends
in `-work`. This is checked in exactly one place (`work_eligibility.py`) and
every scheduling path consults it. A session without the suffix is never
claimed, never prompted and never has its state changed by the Work runtime —
regardless of how idle it looks, and regardless of what any other component
would otherwise permit.

Existing queue lanes on non-`-work` sessions keep working exactly as they do
today: a human or ChatGPT driving them directly is unaffected. What changes is
that the *Work* coordinator will not touch them.

## Safety posture inherited, not re-decided

Work adds no new authorization path. Dispatch goes through the same
`effective_input` / grant / node-permission checks every send already passes;
a stale node is not trusted; worker output is untrusted data and can never be
read as a coordinator instruction; and approvals cannot be self-forged.
