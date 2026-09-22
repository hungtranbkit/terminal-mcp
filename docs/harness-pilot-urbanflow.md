# Harness pilot — UrbanFlow, toward MOB-011

Run on 2026-09-20 against the real repository at `/home/dell/workspace/urbanflow`
(HEAD `7316c89`), driving the real `TASKS.json` (93 tasks) toward the milestone
**MOB-011 "Home commute screen"**.

The repository was left exactly as found: no commits, no modified files, and
the one git worktree created for the session-replacement proof was removed
along with its branch.

## What is measured and what is modelled

Every number below is one or the other, and they are never added together.

**Measured** — real and reproducible: the engine's decisions, every stage
transition, all durable state, the exit statuses of real shell commands run in
the real repository, and the byte/token size of every prompt actually assembled
from that repository.

**Modelled** — an accounting model, never a measurement: the agent *replies*
(this run used a dry-run agent, so no model was billed), and both naive
baselines. The baselines are computed from the same prompts and the same
contracts as the measured side, so the two cannot drift apart. Running the old
pipeline again on the same tasks to get a measured comparison would cost
exactly the money this feature exists not to spend.

The dry-run agent is *not* a rubber stamp: its evaluator re-runs the contract's
gates and applies the same mechanical corroboration test the engine uses. An
evaluator that answered "pass" whenever the commands exited 0 would be
simulating the precise failure a real evaluator exists to catch.

## Phase A — the real declared checks

| Task | Outcome | LLM calls |
|---|---|---|
| VIS-001 | routed to the Human Decision Queue | **0** |
| ENV-001 | BLOCKED after 2 iterations | 4 |
| the other 7 | never started — dependencies unmet | 0 |

**VIS-001 was never started.** Its acceptance names
`docs/design/reference/00-ui-board.png`, which is not in the repository, and the
repository's own `reference/README.md` forbids generating a substitute.
`human_input_required` detects this mechanically — an acceptance criterion
naming a binary asset that does not exist — and it is the **only one of
UrbanFlow's 93 tasks** that trips the rule. It was routed to the queue with
reason `missing_human_input` and the failing `sha256sum` as evidence, for zero
model calls, and it is not marked satisfied, so anything depending on it stays
unstartable.

**ENV-001 found a real environment fact.** The repo pins node `24.21.0`
(`.nvmrc`); this host runs `v26.7.0`. The run failed, revised, failed
*identically*, and stopped — asking:

> Activate Node 24 LTS: iteration 2 failed identically to the one before it —
> the obstacle is outside what the Builder can change. Is the contract wrong,
> or does the environment need changing?

That is the correct question, and it cost 4 calls instead of the 6 the full
iteration budget would have spent learning the same thing.

**Nothing downstream started.** All seven remaining tasks stayed `NOT_STARTED`
with their unmet dependencies named. No partial credit, no fabricated progress.

## Phase B — after the blocker was answered

**This answer has not been confirmed by a human.** The agent running the pilot
supplied it so the chain could be measured, under a stated assumption, and it
is recorded here so it can be accepted or overruled rather than inherited
silently:

> Environment fact, not a code defect: the repo pins node 24.21.0 (.nvmrc) and
> this host runs v26.7.0. Accepting "the repo's pinned major or newer". The
> blocked run keeps its record of the original bar.

If that assumption is wrong — if ENV-001 genuinely means node 24 and the host
should be downgraded — then Phase B's chain rests on a false premise and
should be re-run after the host is changed. Phase A's result stands either
way: the harness found the mismatch and asked.

A redefine opens a **new run** rather than editing the blocked one — the
definition changed, so its hash changed. The blocked run keeps its entire audit
trail, still saying exactly what it tried and why it stopped.

Because UrbanFlow is still a scaffold (every workspace is a `.gitkeep`), the
decisive commands a Planner would author against a real codebase were supplied
explicitly as `PILOT_CHECK_MAP`. They are real commands that really run; what
they verify is scaffold presence, not feature completeness. The engine's **call
shape** — which is what is being compared — does not depend on why a check
passes.

| Task | Mode | Stage | Iter | LLM | Planner skipped | Eval skipped | Session |
|---|---|---|---|---|---|---|---|
| ENV-001 | standard | MERGE_READY | 1 | 1 | ✓ | ✓ | new (lane B) |
| ENV-006 | standard | MERGE_READY | 1 | 1 | ✓ | ✓ | reuse |
| CT-001 | standard | MERGE_READY | 1 | 1 | ✓ | ✓ | reuse |
| MOB-001 | standard | MERGE_READY | 1 | 1 | ✓ | ✓ | new (lane A) |
| MOB-002 | standard | MERGE_READY | 1 | 1 | ✓ | ✓ | reuse |
| CT-004 | standard | MERGE_READY | 1 | 1 | ✓ | ✓ | reuse |
| CT-005 | standard | MERGE_READY | 1 | 1 | ✓ | ✓ | reuse |
| MOB-003 | standard | MERGE_READY | 1 | 1 | ✓ | ✓ | reuse |
| **MOB-011** | **critical** | **MERGE_READY** | 1 | 3 | — | — | reuse |

All nine reach `MERGE_READY` and stop there. Nothing merged, nothing deployed.

MOB-011 is the only task escalated to CRITICAL, and it is the only one that
pays for a Planner and an independent Evaluator in a fresh session that never
saw the Builder's reasoning — three calls against everyone else's one.

## Phase C — checkpointed Builder session replacement

Driven to BUILDING in a **real git worktree**, checkpointed, then the session
was swapped:

| Property | Result |
|---|---|
| run id | unchanged |
| iteration | unchanged (1) |
| worktree | unchanged — `/tmp/harness-pilot/MOB-003` |
| branch | unchanged — `harness/pilot-mob-003` |
| contract hash | unchanged |
| session | `pilot-lane-A` → `pilot-lane-A-replacement` |
| checkpoint | `ckp_…` carrying `remaining: ["wire the ETA row"]` |

The replacement receives a **full** prompt plus the checkpoint, not a delta — it
has no history for a delta to refer to — and the engine works that out from the
session id rather than from a flag a caller could get wrong. A
`BUILDER_SESSION_REPLACED` event records the handover durably.

## Phase D — measurement

**Measured** (9 runs)

| | |
|---|---|
| LLM calls | **11** |
| prompt+completion tokens (est.) | **9,046** |
| Planner skipped | 8 of 9 |
| Evaluator skipped | 8 of 9 |
| Builder sessions spawned | **4** for 9 tasks |
| session reuses | 7 |
| delta / full prompts | 7 / 4 |
| context-cache hits / misses | 2 / 1 |

**Modelled** — the shape being replaced

| Baseline | Calls | Tokens |
|---|---|---|
| naive sequential, full context every stage | 27 | 32,454 |
| naive parallel, full context + a coordinator per task | 36 | 43,254 |

**Avoided**

| | Calls | Tokens |
|---|---|---|
| vs. naive sequential | **16 (59%)** | 23,408 (72%) |
| vs. naive parallel | **25 (69%)** | 34,208 (79%) |

Both baselines are charged **zero** PM poll calls, which is generous to them —
the loop being replaced polled on a timer.

The packs here are small because every UrbanFlow workspace is still a
`.gitkeep`. On a populated codebase the context saved by delta prompts and lane
session reuse is **larger**, not smaller, so these percentages are a floor.

## Open human decisions after the pilot

One, which is the right number:

- `missing_human_input` — **VIS-001**: supply the approved UI board asset. The
  repository forbids generating a substitute, and no Builder iteration was
  spent pretending otherwise.
