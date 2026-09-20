# TMCP-HARNESS-001 — Harness Core

One execution state machine for AI work, one durable store, one scheduler, and
a cost policy that decides what *not* to send. This document is the reasoning;
the code is `terminal_mcp/harness_*.py` and the tests are
`tests/test_harness_{core,scheduler,pilot}.py`.

## The problem

"What is this task doing right now" had at least five answers that could all be
true at once: the queue's task status, the Project Agent's phase, the PM
scheduler's assignment, the bug Planner→Executor pipeline, and the dashboard's
rendering of all of them. A task could be RUNNING, `phase: implement`,
"awaiting executor" and Blocked simultaneously, and there was no fact of the
matter about which was right.

Harness replaces the **decision layer** — "AI decides the work progressed from X
to Y" — and nothing else. The durable queue, sessions, leases, worktree safety,
the resource scheduler and telemetry all stay exactly as they are and keep
being the runtime source of truth for their own concerns.

## Where the state lives, and why it is the load-bearing choice

The harness tables are appended to `QUEUE_MIGRATIONS` as **migration v15**, in
the same SQLite file as `queue_tasks`. Not a `harness.db`.

A separate store would have been easier to write and a second runtime source of
truth within a week: two files that can disagree about whether a task is
running, two backup schedules, two recovery paths, and a reconciliation loop
whose job is to guess which one was right. A `HarnessRun` and the `queue_task`
it drives commit in the same transaction. `.harness/` on disk holds only
human-readable evidence that can be deleted without losing a fact.

The migrations are listed in `queue_store.py`, not appended inside
`harness_store.py`, so opening **either** store migrates the database
identically and there is one ordered ladder regardless of which ran first.

## Five decisions worth defending

**A failing test is not a human decision.** `EVALUATING + fail → REVISING`,
always. The closed list in `harness_policy.HUMAN_DECISION_REASONS` has nine
reasons and a red test is not one. The only path from a verdict to a human is
the non-convergence guard, which is a statement about the loop rather than
about the code. The old Blocked auto-clear pipeline existed purely to undo the
opposite choice in bulk — a loop whose entire job was cancelling another loop's
decision.

**An infra failure resumes; a product failure iterates.** There is no edge from
any resumable stage back to `INIT`, so "retry" cannot decay into "restart from
the original prompt". `record_infra_failure` → `resume()` returns to the same
stage, same iteration, same checkpoint, same worktree.

**The contract is content-addressed and frozen.** Once a Builder starts, the
dataclass is frozen and the store refuses an UPDATE on that row. A scope change
is a **new version**, with the old one still joined to the iterations that ran
against it. Evidence collected against a definition that moved underneath it is
not evidence.

**`harness_events` has an INSERT path and no other.** The absence of
`update_event` is the enforcement. An event log that can be edited is a
narrative, and the one time it matters is exactly the time someone will have
"fixed" it.

**Difficulty triage is a function.** `mode_for()` is pure and deterministic; the
standalone triage service had its own store, loop, UI and opinion, and the only
consumer of that opinion was "how many review steps". Risk is delegated to
`task_classifier.EXCLUSIONS` so "what counts as risky" keeps one definition.

## Where the money goes, and the six places it stops

An LLM is invoked to **reason**, to **write code**, or to **judge evidence** —
and for nothing else. Readiness, dependencies, leases, routing, thresholds,
budgets, retries, scheduling and status are all deterministic.

| Saving | Mechanism | Condition |
|---|---|---|
| No Planner | `planner_required()` | the definition already carries scope, acceptance and runnable checks. CRITICAL never skips. |
| No Evaluator | `evaluator_required()` | LIGHT/STANDARD run the contract's declared checks and the **engine** reads the exit statuses. |
| No Evaluator on failure | `_evaluate` | a declared check exited non-zero. Nobody is paid to confirm a red test. |
| Delta revisions | `revision_prompt()` | the failed criteria only, and only to the session that built the previous attempt. |
| Cross-task lane reuse | `adjacent_task_prompt()` | the next task in the same lane, sent only its contract — the session already holds the modules. |
| No PM loop | the engine has no loop | `step()` advances one stage and returns. |

The last one is structural, and a test parses the module to prove it: no
`while True`, no `time.sleep`, no thread, no timer. The loop being replaced woke
a model every sixty seconds to ask whether anything had changed — 1,440 calls a
day whose entire output is "no".

The budget is **soft**. `budget_action()` never returns "stop": a hard cap turns
an expensive run into a wasted one. Over budget, a run checkpoints, narrows the
next prompt and escalates the *definition* as the suspect.

## Scheduling: `cost_first | balanced | speed_first`

`harness_scheduler` is pure functions over a frozen graph. "What next?" reads
like a judgement call and is not one: given a dependency graph, a set of
finished runs and a concurrency limit, the answer is a topological sort and a
longest-path calculation.

- **cost_first** (default) — one Builder per lane and one overall. Each later
  task in a lane inherits a session that already holds its modules.
- **balanced** — two lanes advance at once.
- **speed_first** — no lane cap, no cross-task reuse; pays a full context load
  per task to buy wall-clock.

The knob exists so "just run everything at once" costs a number rather than a
surprise.

A task marked `autonomous: false` is **deferred and never marked satisfied**, so
everything depending on it stays unstartable. Faking completion is the one
outcome that flag exists to make unreachable.

## Two things the pilot forced into the core

**A prose label is not a check.** Real task files declare "typecheck",
"component tests", "fixture validation". Handed to a shell those exit 127 and
get recorded as product failures of code that is fine. `partition_checks` keeps
prose out of `required_checks`, where its only effect is to make the Planner
required — the correct answer for a task with no decidable bar.

**A green check is not always a pass.** `node -v` exits 0 on every version ever
released, so "node -v reports v24.x" is not decided by it. Recording that
criterion as passing puts a false statement in the audit trail, which is worse
than having paid for an evaluator. `uncorroborated_criteria` is deliberately
narrow — dotted versions and hashes only — because a rule that fired on prose
would restore the cost of an evaluator on every task in the system.

## The MCP surface

Five tools: `terminal_harness_{start,step,status,decisions,plan}`. There is no
run-to-completion tool, for the same reason the engine has no loop.
`write_authority` defaults to `shadow`, which writes nothing outside the harness
tables — the setting under which this engine can be compared against the
existing pipeline on real work without being able to affect it.
