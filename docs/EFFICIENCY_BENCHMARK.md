# Before/After Efficiency Benchmark Harness

Read-only tooling that compares **matched** legacy and new-pipeline task
cohorts and refuses to state a result it cannot defend.

    python -m terminal_mcp.bench                    # markdown to stdout
    python -m terminal_mcp.bench --format json      # machine-readable
    terminal-mcp-bench --out /tmp/bench.md          # after `pip install -e .`

It runs today, on a host where the telemetry it is built for does not
exist yet, and says `INSUFFICIENT_DATA` — see [What the data actually
looks like today](#what-the-data-actually-looks-like-today). It becomes
a real comparison automatically once enough matched tasks exist; nothing
has to be re-enabled.

## Scope and ownership

This package owns **analysis only**:

| Owned here | Explicitly NOT owned here |
| --- | --- |
| `terminal_mcp/bench/**` (read-only readers, statistics, report) | runtime telemetry schema — what gets written, and where |
| `tests/test_bench_*.py` | Analysis Gate validators |
| this document | the delivery gate |
| | production UI / dashboard routes / MCP tools |

Every database connection is opened with SQLite's `mode=ro` URI **and**
`PRAGMA query_only = 1`. The only file this tool writes is the report
you ask for with `--out`. Nothing is deployed.

## What the data actually looks like today

Audited 2026-09-14 on `dell-linux`:

| Source | State |
| --- | --- |
| `~/.local/state/terminal-mcp/queue.db` | exists; `queue_tasks`, `queue_events`, `outcomes`, `verify_jobs` all **0 rows** |
| `~/.local/state/tmcp-fed/terminal-mcp/queue.db` | exists; same tables, **0 rows** |
| `work.db` | **does not exist, and is not coming** — there is no such store; the deliverable noun here is `outcomes`, which lives in `queue.db` |
| `ai_usage.db` | **does not exist, and cannot supply tokens** — see below |
| `work_telemetry.db` | the **real** per-task telemetry store (Task A, branch `feat/work-efficiency-telemetry`). Schema committed, tables empty — no worker has written a row |
| existing reporting utilities | `pm_summary.py` (queue aggregation, no tokens), `metrics.py` (in-process counters, not persisted), `ai_usage_service.py` (quota percentages) — none of them carries per-task token telemetry |

The brief named `ai_usage.db` and `work.db`; the audit found neither,
and both absences are structural rather than "not yet".

`terminal_mcp/ai_usage_client.py` is a read-only HTTP client over a
*separate* project's `/api/usage`, which returns **per-provider
quota-window percentages** (`used_percent`, `resets_at`) — confirmed by
reading that service's own state file, which holds exactly four
percentage entries and no token counts at all. There is no per-task
attribution and no cache split in it, so restoring or extending it
would still yield zero tokens.

`work.db` does not exist under any name. The store that actually
carries this telemetry is **`work_telemetry.db`**, and it has its own
named adapter (`terminal_mcp/bench/work_telemetry.py`) rather than
being left to generic schema discovery — see [The work_telemetry.db
adapter](#the-work_telemetrydb-adapter). The generic readers for
`work.db` / `ai_usage.db` are kept because they cost nothing, tolerate
any schema, and report their own absence honestly; nothing depends on
them.

There is also a live open question that gates the whole token slice,
raised by the measurement-contract lane and recorded here rather than
quietly assumed away: **a CLI-driven worker in a tmux pane has no API
response object to read `usage` from.** If per-turn provider usage
cannot be captured for those workers at all, the token comparison is
infeasible as specified and the programme falls back to turn-count and
wall-clock outcomes — both of which this harness already computes.

Two source paths are scanned by default because this host is a **node**
as well as running a local federation controller. Instrumenting only the
node's own queue would instrument a permanently empty database if the
real queue lives on the controller.

## The measurement contract

### Cost is weighted, not summed

The task brief specified `primary_cost_tokens = input + cache_write`.
That figure is still computed and still printed, because a reviewer
holding the brief should be able to find it — but it is **not** what any
comparison is decided on, for three reasons:

1. It adds tokens of different unit cost at parity. A 5-minute cache
   write bills at **1.25x** base input, a 1-hour write at **2x**.
2. One `cache_write` field cannot price a cache write, because the
   multiplier depends on the TTL — and the API reports the split
   (`cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_…`).
3. **Excluding output systematically favours the treatment arm.** Output
   bills at **5x** input across the current lineup (Opus 5 $5/$25,
   Sonnet 5 $2/$10, Haiku 4.5 $1/$5, Fable 5.1 $10/$50). A pipeline
   whose thesis is "think harder up front" emits that thinking *as
   output tokens*, so a metric that ignores output charges the up-front
   arm nothing for its own primary cost while charging the baseline arm
   fully for the rework it does.

`docs/examples/demo-report-randomised.md` shows this is not a
theoretical worry. On a synthetic cohort where the new pipeline shifts
spend into up-front output, `primary_cost_tokens` reports a **4.5%
saving** while cost-weighted units show the new arm is **1.27x more
expensive per completed task** — the brief's metric and the real one
point in opposite directions, and only one of them is denominated in
anything that gets billed.

`primary_cost_tokens` therefore never renders on its own: its cells
carry the `cost_units` median inline, and where the two disagree about
which arm is better the row says so. The two cannot be quoted apart.

The decision metric is therefore **`cost_units`**, in base-input-equivalents:

    cost_units = input
               + cache_write_5m × 1.25
               + cache_write_1h × 2.00
               + cache_read     × R_read      (0.1; 0.025 on Fable 5.1)
               + output         × R_out       (5.0 across the lineup)

Rates live in `terminal_mcp/bench/pricing.py` under an explicit
`PRICE_TABLE_VERSION`, so history can be **repriced** rather than
silently reinterpreted. An unknown model is *unpriceable*, never free.

`input_tokens` is the **uncached remainder**, not the prompt. Real
prompt size is `total_prompt_tokens = input + cache_write + cache_read`;
reporting the remainder as prompt size understates a cached agent loop
by an order of magnitude, so they have different names and nothing calls
the remainder "prompt".

Where telemetry records only a collapsed cache-write total, cost is
reported as **missing** by default (`--cache-ttl-policy split_required`).
`assume_5m` / `assume_1h` let an operator bound the answer from both
sides; whichever was used is printed in the report header.

### Per-task metrics

input · output · cache_read · cache_write (5m/1h split where available) ·
`total_prompt_tokens` · `primary_cost_tokens` · **`cost_units`** ·
`worker_turn_count` · re-entries by reason · `first_pass_success` ·
duration · retries.

### Worker turns never come from `attempt_count`

`queue_tasks.attempt_count` is bumped by reconcile-and-reclaim as well
as by genuine rework, so a metric derived from it scores a **flaky node**
identically to a lane with bad contracts. Turns are counted by
collapsing distinct `dispatch_idempotency_key` values — the field the
runtime already uses to tell a reclaim from a new dispatch. A regression
test pins it: *N reclaim cycles ⇒ `worker_turn_count == 1` while
`attempt_count > 1`*.

`worker_turn_count` is the **primary statistical metric** (a count has
far more power than a binary at these sample sizes). First-pass success
is the **headline outcome**, but needs a much larger sample before a
direction is claimed.

### First-pass success: recomputed, three-condition, and never quoted without its coverage

Three separate constraints, each from a gap found by the critic lane
against the committed telemetry schema.

**Recomputed, not read.** `telemetry_tasks.first_pass_success` is a
*stored* tri-state written by the reporter — the party being measured
writes its own outcome. The harness recomputes it from that task's own
terminal status and re-entry rows, uses the recomputed value, and
flags disagreement (`FPS_DISAGREEMENT`) with a per-arm count in the
report. This is the cheapest anti-gaming fix available with no upstream
change.

**Three conditions, not four.** The harness measures *completed,
verified, no counted re-entries*. The contract's fourth condition — that
the task raised no clarification — **cannot be evaluated at all today**:
there is no clarification concept anywhere in the telemetry store (no
table, no event, no column; the Question Ledger is unbuilt). The report
states that the condition is **absent rather than satisfied**, so the
definition cannot drift silently.

**Evidence is still required**, and where it is absent the task stays
`None` and leaves the denominator. "No recorded rework" is
indistinguishable from "nobody checked", and scoring the latter as a
success rewards a vague contract — the less a task promises, the easier
it is to satisfy.

**The denominator itself is a treatment effect.** This is the structural
problem, not an accident of instrumentation: the HIGH decision-budget
arm is required *by its own contract* to carry a live verification plan,
so it is systematically more likely to record the evidence that makes a
task scoreable at all. The treatment changes the probability a task can
be measured **on the very metric being compared**, and the direction is
predictable — it selects well-run control-arm tasks out of the
denominator and inflates the treatment arm's apparent rate. So:

* per-arm **scoreability is a first-class row**, printed directly under
  the rate; the rate is never rendered without it;
* the **headline metric moves to `worker_turn_count`**, whose
  denominator is every matched task regardless of treatment, whenever
  the gap is large enough to matter — and "large enough" comes from the
  arithmetic, not from a chosen number.

If an arm scores a fraction `c` of its tasks at observed rate `r`, its
true rate lies in `[r·c, r·c + (1−c)]` — at the extremes the unscored
tasks are all failures or all successes. Working that through, **a
scoreability gap of G percentage points can account for up to exactly G
points of apparent first-pass difference.** So the headline moves
whenever the gap is at least as large as the difference it would have to
explain: at that point coverage alone is a complete explanation for the
result, and the metric is uninformative however clean the rest of the
comparison is. A fixed 10-point gap remains as an absolute backstop for
the case where the observed difference is large but the gap is wide too.
The report states which of the two fired and how much of the observed
difference coverage can account for.

### Re-entry reasons

`USER_CHANGED_REQUIREMENT` and `ENVIRONMENT_FAILURE` describe the world
changing under a task, not the pipeline doing worse work. They are
**excluded** from the counted re-entry total and from first-pass
success, and reported on their own rows. A task whose *only* re-entries
carry those reasons is **flagged** (`EXCLUDED_REASON_ONLY`), never
dropped — dropping it would bias the sample toward whichever cohort
happens to run in a more stable environment.

`STALE_CONTEXT` is recognised as its own reason and **is** counted. A
warm cache can make an agent reason about a repo state that no longer
holds, and that rework looks exactly like `CONTRACT_GAP` from outside.
It is counted rather than excluded because if a bigger analysis prefix
*causes* more staleness, that is a genuine downstream cost of the
treatment — a mediator, not a confounder — and excluding it would
flatter the arm that caused it. The comparison is rendered **both ways**
(`Re-entries` and `Re-entries, also excluding STALE_CONTEXT`), because
the causal reading differs between the two and the gap between them is
itself the part of rework attributable to carrying more context.

**But the store cannot record it.** `telemetry_reentries.reason` is
CHECK-constrained to `TEST_FAILURE`, `CONTRACT_GAP`,
`IMPLEMENTATION_BUG`, `ENVIRONMENT_FAILURE`, `USER_CHANGED_REQUIREMENT`,
`DELIVERY_FAILURE`, `MERGE_CONFLICT`, `OTHER` — a stale-context re-entry
is rejected at write time and lands in the generic bucket. A zero row
for it would read as *evidence of absence* rather than absence of
evidence, so each source declares the vocabulary it can physically emit
and the report renders any unrecordable reason as **UNAVAILABLE**. Until
the reason is added upstream, no `STALE_CONTEXT` figure is trustworthy
and the report says so instead of printing a confident zero.

> Open contract item, not implemented here because it is a process
> control rather than an analysis one: adjudication of
> `USER_CHANGED_REQUIREMENT` vs `CONTRACT_GAP` should be **blind to the
> arm**. Where blinding is impossible, read the report's counted and
> excluded re-entry rows as a bound on that discretion.

### Missing is never zero

Every numeric field is `int | None`. There is no `or 0` in the package
and a test pins that. A task with no token telemetry is **absent from**
the token statistics (each cell reports its own `n`) and counted in the
coverage rows — it never enters the median as a zero.

### No double counting

Usage rows are folded through one `UsageAggregator`:

* **delta mode** sums rows but only once per turn identity. Measured on
  a real Claude Code transcript in this workspace, the same usage block
  appeared on **4 rows per turn**; naive summation reported **1,034,241**
  output tokens where the true figure was **542,382** — a 1.9x
  overstatement.
* **cumulative mode** never sums; it takes the row with the highest
  sequence, because there the last row already *is* the task total. It
  is selected only from an explicit self-description in the schema
  (a marker column, or a name containing `cumulative`/`running_total`),
  never guessed from the shape of the numbers.

### Matching

Tasks are stratified on **task profile/risk, coarse complexity, project,
and agent/model when known**, then each stratum is truncated to equal
counts in both arms — so the two arms have an identical workload mix by
construction. An **unknown** control value is its own stratum, never
folded in with a known one. `MatchResult.dropped` reports exactly how
many tasks fell out and why.

The stratification key is **joined at report time**, not snapshotted:
the telemetry store records no `decision_budget`, profile or risk class
per task, so the key is recomputed from queue metadata rather than read.
A recomputed key can in principle be recomputed *after* seeing the
outcome, so the report header names exactly where each key came from
(`queue_tasks.analysis`, `queue_tasks.metadata`, or a column) and says
plainly when no per-task snapshot exists. Snapshotting the key per task
is an upstream ask, not something this tool can fix.

**Risk classes are never pooled.** `HIGH_RISK`, `STANDARD`, `FAST_FIX`
and `UNKNOWN_RISK` each get their own comparison. There is deliberately
no overall number: the effect can run in opposite directions across
them, and pooling cancels a real result into a null.

A `MIXED_COMPLEXITY` warning fires when complexity is unrecorded for
more than 20% of matched tasks in either arm, or when complexity was not
used as a control and the two mixes differ (total-variation distance
> 0.15). It demotes the confidence band by one.

### Statistics

Medians with p25/p75, never means — agent task cost is heavy-tailed and
one outsized task dominates any mean at these sizes. Outliers (Tukey
1.5×IQR, with the degenerate-quartile case handled) are **counted and
reported, not deleted**. The bootstrap is **seeded**, so two runs over
the same data produce byte-identical reports.

A stated saving is the **lesser** of the point estimate and the lower
bound of a 90% bootstrap interval, floored at 0%. If the interval spans
zero, the report says *no measurable difference* rather than quoting a
number it cannot defend.

The **cost ratio** (median cost per completed task, new ÷ legacy) is
reported next to the turn delta, because the decision quantity is cost
per completed task: a turn reduction that costs more than it saves is a
loss, and an insignificant turn reduction bought cheaply can still be a
win.

### Sample size and confidence

10 is a **reporting floor**, not a sample size.

The floor counts **measured** tasks, not matched ones. Comparing a
0.2-coverage cohort against a 0.9-coverage one is not a comparison, and
any statistic taken over all completed tasks falls as coverage falls —
which looks exactly like an efficiency win. A group clears the floor
only when both arms have enough tasks that actually carry telemetry, and
a coverage gap wider than 20 points between arms raises
`COVERAGE_IMBALANCE` and demotes the band.

| Matched, measured tasks per arm | Label | What may be claimed |
| ---: | --- | --- |
| < 10 | `INSUFFICIENT_DATA` | nothing — distributions only, no percentage anywhere |
| 10–39 | `LOW` | descriptive only; no direction, no savings % |
| 40–99 | `MODERATE` | directional claims on **count** metrics (e.g. worker turns) |
| ≥ 100 | `HIGH` | directional claims on **binary** metrics (e.g. first-pass success) |

The bands are power-based rather than round numbers: a binary outcome
moving 40%→60% at 80% power and α=0.05 needs roughly 100 per arm
(`n ≈ 16·p(1−p)/d²`); a count metric needs roughly 40–60. The 10–39 band
is descriptive precisely because it is the region where a "significant"
bootstrap result is more likely noise than signal.

Thresholds and bands are the first constants in `report.py` so the
contract can be refined in a one-line edit.

### Randomisation

`--assignment` is a required, disclosed input, and it is the strongest
constraint in the whole contract:

* `randomised` — assigned at task **creation**, before anyone read the
  task closely enough to judge its difficulty. The only mode that
  permits a directional claim.
* `interleaved` — alternating by creation order. Weaker, but still blind
  at the moment of assignment. Descriptive.
* `observational` (**default**) — matching only. Descriptive at any
  sample size.

Matching controls make an observational study. At these sample sizes
matching cannot balance task difficulty, which is the confounder that
swamps the effect — so without randomisation every figure is labelled as
a description of what happened, not a measured effect of the pipeline.
The report always states which mode actually applied.

## Sources

| Adapter | Reads | Supplies |
| --- | --- | --- |
| `WorkTelemetryDbSource` | `work_telemetry.db` | the real per-task rollup: token samples, worker turns, re-entries with reasons, tri-state first-pass success, duration |
| `QueueDbSource` | `queue.db` | task spine: identity, project, timing, dispatch-collapsed turns, and the **cohort label + matching controls** from the Feature Contract |
| `UsageDbSource` | `work.db`, `ai_usage.db` | generic token rows, folded without double counting (neither store exists; kept as a tolerant reader) |
| `JsonlFixtureSource` | any `.jsonl` | normalised records — for tests, for reproducing a disputed number without live DB access, and for analysing an export from another host |

### The work_telemetry.db adapter

Written against the committed schema rather than discovered, because
four semantics can only be got right by name:

* `telemetry_usage_samples` keeps already-differenced deltas **and** the
  raw `reported_*` values, which **are cumulative** when
  `kind='CUMULATIVE'`. The adapter sums the delta columns and never
  touches `reported_*` — summing those would be precisely the
  double-count this harness exists to prevent.
* Worker turns are `SUM(turn_count)` over `IMPLEMENTATION`,
  `VERIFICATION`, `DELIVERY` **only**. `ANALYSIS` and `CONTRACT` are the
  investment side of the experiment and `UNKNOWN` is unattributed;
  counting either into the worker's turns would inflate the exact number
  under test. Their **tokens** still count toward cost — the investment
  is real spend, and excluding it is what biases the comparison.
* `first_pass_success` is tri-state TEXT. `UNKNOWN` leaves the rate's
  denominator; it is never coerced to failure.
* Token columns are nullable with no default, so **NULL = never
  measured** and **0 = measured as zero**. They stay different facts.

`counter_reset` rows and partially-NULL sample sets are **flagged**, not
excluded, and the weakest `confidence` on any sample propagates to the
task. **Cost is priced per sample, not per task.** Migration 2 of the store
added `model_id` and the `cache_write_5m_tokens` / `cache_write_1h_tokens`
split, so cost is computable from the store alone with no assumption.
Each sample is priced with its *own* `model_id` and the results summed,
because a task whose turns ran on more than one model — a worker
delegating to a cheaper sub-agent — cannot be priced correctly from a
single task-level model. Such a task is flagged `MIXED_MODEL` and claims
no task-level model id. One unpriceable sample makes the whole task
unpriceable: a partial cost is a wrong cost.

The adapter does not assume migration 2. An older store without the
split or `model_id` degrades to the collapsed cache-write total and an
unpriceable cost rather than failing, and `--price-model <id>` lets an
operator state an assumption that the report then prints as a caveat on
every cost figure.

### Cohort and controls are joined, not copied

Whether a task went through the Analysis Gate is a fact the gate owns.
`QueueDbSource` reads `queue_tasks.analysis` — its presence *is* the
new-pipeline signal — and pulls the task profile / risk / complexity out
of that same Feature Contract JSON. An explicit `cohort` column or
metadata key still wins where present, and `--cohort-map` overrides
everything. Nothing keeps a second, drifting copy of the label.

Every adapter **discovers** the schema it is pointed at (tables, then
the first recognised spelling from a candidate list) and reports what it
found and what it could not find via `SourceStatus`. A missing column
yields `None`. A source that is absent or unreadable is reported in the
Sources table, never silently skipped — an empty report always says
which telemetry was missing.

Telemetry that does not label its own cohort can be labelled explicitly
with `--cohort-map cohorts.json` (`{"task-id": "legacy"}`), which is
auditable, rather than by a heuristic buried in an adapter.

## Example reports

* `docs/examples/sample-report-2026-09-14.md` / `.json` — this host,
  today: `INSUFFICIENT_DATA`, with every source and its state named.
* `docs/examples/demo-report-randomised.md` — the same code over
  `tests/fixtures/bench_demo_tasks.jsonl` (180 synthetic tasks,
  `--assignment randomised`), showing the "after" shape: two risk
  classes reported separately, a stated turn saving, and a cost ratio
  that disagrees with `primary_cost_tokens`.

Reproduce the second with:

    python -m terminal_mcp.bench --state-dir /nonexistent \
      --jsonl tests/fixtures/bench_demo_tasks.jsonl --assignment randomised

## Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--state-dir` | node + fed-controller state dirs | where to look for the three databases (repeatable) |
| `--queue-db` / `--work-db` / `--ai-usage-db` | — | explicit paths |
| `--work-telemetry-db` | `$TERMINAL_MCP_WORK_TELEMETRY_DB`, else the state dirs | explicit `work_telemetry.db` path |
| `--price-model` | — | model id to price `cost_units` against where telemetry records none; disclosed in the report |
| `--jsonl` | — | normalised records, one JSON object per line |
| `--cohort-map` | — | JSON file of task id → cohort |
| `--controls` | `profile,complexity,project` | matching controls |
| `--assignment` | `observational` | `randomised` / `interleaved` / `observational` |
| `--cache-ttl-policy` | `split_required` | `assume_5m` / `assume_1h` to bound unsplit cache writes |
| `--min-matched` | `10` | reporting floor per arm per risk class |
| `--format` | `md` | `md` or `json` |
| `--out` | stdout | write the report to a file |
| `--fail-on-insufficient` | off | exit 2 on `INSUFFICIENT_DATA` (off by default — that verdict is the correct answer today, not a failure) |

## Tests

    python -m pytest tests/test_bench_sources.py tests/test_bench_analysis.py \
                     tests/test_bench_cli.py tests/test_bench_work_telemetry.py

The synthetic fixtures exist because the real telemetry does not yet, so
the only way to prove the harness correct *before* the data lands is to
build the shapes that would break it. Each one pins a mistake that would
otherwise produce a plausible-looking number rather than an error:

* repeated per-turn rows are folded once (the real 1.9x transcript shape)
* cumulative rows are taken, never summed
* absent columns and NULL values become missing, never zero
* half a cohort with no telemetry shrinks `n` instead of collapsing the median
* mixed / unrecorded complexity raises `MIXED_COMPLEXITY` and demotes confidence
* one 10,000x task does not move the median and is still flagged
* N reclaim cycles ⇒ one worker turn while `attempt_count` is N
* first-pass success without verification evidence stays unknown
* excluded re-entry reasons are separated and the task is flagged, not dropped
* below the floor, no savings figure is emitted at all
* without randomisation, every figure stays descriptive
* risk classes are reported separately and never pooled
* sources are opened read-only (a write raises)
* `reported_*` cumulative columns are never summed; delta columns are
* only worker phases count toward worker turns
* NULL stays unmeasured while a real 0 stays 0
* tri-state first-pass success keeps `UNKNOWN` out of the denominator
* a stated `--price-model` assumption is surfaced as a caveat
* the cache-write TTL split is read where present, and cost is priced
  per sample with each sample's own model
* a mixed-model task is priced per sample and flagged, not priced from
  one guessed model
* one unpriceable sample makes the whole task unpriceable, and the
  task-level fallback cannot quietly undo that
* a reason the store cannot record renders UNAVAILABLE, never 0
* a flattering stored first-pass outcome is overridden by the recomputed
  one and flagged
* first-pass success stops being the headline when its denominator
  depends on the arm
* `primary_cost_tokens` never renders without `cost_units` in the same
  row, and a divergence between them is called out inline
