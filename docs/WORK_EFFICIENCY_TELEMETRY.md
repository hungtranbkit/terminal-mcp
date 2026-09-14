# Work Efficiency Telemetry -- integration contract

**Status: UNVERIFIED (unit-tested, never fed real production data).**
Branch `feat/work-efficiency-telemetry`. Nothing here has been deployed
and no real worker has written a row yet.

Schema version 3 (`PRAGMA user_version`). v3 adds the `STALE_CONTEXT`
re-entry reason, which needs a table rebuild since SQLite cannot ALTER a
CHECK constraint; rows, ids and idempotency keys are preserved, and the
rebuild is re-entrant at every crash point. v2 is additive and nullable:
a v1 database keeps every row, every idempotency key still dedupes, and
the new columns read back NULL. No backfill.

## What this is

Additive, measurement-only telemetry that exists to answer ONE empirical
question: **does the Analysis Gate plus an explicit implementation
contract actually reduce model turns and token cost per delivered task?**

It is a satellite of the queue, not part of it:

* `terminal_mcp/work_telemetry_store.py` -- schema + durable storage.
* `terminal_mcp/work_telemetry_service.py` -- queries/normalized views.
* Its own database file, `work_telemetry.db`. **`queue.db` is not
  touched at all** -- no new column, no new migration in
  `QUEUE_MIGRATIONS`, no table. A test asserts this
  (`test_the_queue_database_is_never_touched`).

It owns no decision, gates nothing, and is never on the dispatch path.
Deleting `work_telemetry.db` loses measurements and changes no
behaviour. There is **no UI, no MCP tool and no dashboard change** in
this lane.

### Why a separate database rather than columns on `queue_tasks`

`QUEUE_MIGRATIONS` is a single globally ordered list that several lanes
are editing concurrently; appending a telemetry migration there would
both conflict textually and race for a version number. The separate file
starts its own list at version 1 and cannot collide with anything.

## Identity: how a row is addressed

| field | is | source |
|---|---|---|
| `task_id` | `queue_tasks.id` | required |
| `work_id` | `outcomes.id` -- the "Work"/deliverable noun in this codebase (`outcomes.py`) | optional, may arrive late |
| `session_id` | lane/session name, e.g. `window2` | optional |
| `project_id` | `project_identity` id, e.g. `git:github.com/acme/widget` | optional |

Plain TEXT, **no foreign key into `queue.db`** (cross-database
references do not exist in SQLite). Nothing is validated against the
queue, so a task predating this store simply has no telemetry and every
query reports UNKNOWN for it.

Identity may be learned late: any call fills in fields that were NULL
and **never overwrites a known value with NULL**, and never resets
`started_at`.

## Producer API (what other lanes call)

```python
from terminal_mcp.work_telemetry_store import WorkTelemetryStore
store = WorkTelemetryStore()          # default XDG path; pass one in tests

store.start_task(task_id, work_id=..., session_id=..., project_id=...,
                 phase="ANALYSIS")

store.record_usage(task_id, phase="IMPLEMENTATION",
                   kind="CUMULATIVE", counter_id="claude:window2:pid1234",
                   idempotency_key="<your stable event id>",
                   model_id="claude-opus-5",
                   input_tokens=..., output_tokens=...,
                   cache_read_tokens=..., cache_write_tokens=...,
                   cache_write_5m_tokens=..., cache_write_1h_tokens=...,
                   turn_count=..., evidence_source="claude_usage_json",
                   confidence="MEASURED")

store.record_reentry(task_id, reason="TEST_FAILURE", idempotency_key=...)
store.record_contract_gap(task_id, idempotency_key=..., detail=...)
store.complete_task(task_id, status="COMPLETED")
```

Every method is idempotent by key and safe to call from a producer that
may crash and retry at any point.

### Phases

`ANALYSIS`, `CONTRACT`, `IMPLEMENTATION`, `VERIFICATION`, `DELIVERY`,
`UNKNOWN`.

`analysis_tokens` / `contract_tokens` are the **investment**;
`worker_*` is the **cost** it is meant to reduce, and covers
`IMPLEMENTATION + VERIFICATION + DELIVERY` only. `UNKNOWN` is reported
separately as `unattributed_tokens` and is deliberately **never** folded
into `worker_*` -- doing so would inflate the very number under test.

### Re-entry reasons (closed enum)

`TEST_FAILURE`, `CONTRACT_GAP`, `IMPLEMENTATION_BUG`,
`ENVIRONMENT_FAILURE`, `USER_CHANGED_REQUIREMENT`, `DELIVERY_FAILURE`,
`MERGE_CONFLICT`, `STALE_CONTEXT`, `OTHER`.

`STALE_CONTEXT` (added in v3) is the agent reasoning from a warm cached
prefix over a repo state that has since moved. From the outside it looks
exactly like a `CONTRACT_GAP` -- rework, extra turns, a failed first pass
-- but it is a cache-coherence failure, and the difference has a
**direction**: the arm carrying more up-front analysis carries a larger
cached prefix and is more exposed to staleness. Folded into
`CONTRACT_GAP` or buried in `OTHER`, that cost is charged to analysis
quality, i.e. against the exact hypothesis this telemetry tests. It does
**not** increment `contract_gap_count`.

An unrecognised reason raises `TelemetryValidationError` -- it is **not**
coerced to `OTHER`. Silently remapping would destroy the one field the
experiment reads. Use `OTHER` explicitly, or ask for an enum value.

A `CONTRACT_GAP` re-entry increments both `reentry_count` and
`contract_gap_count`. A gap that did not force a re-entry goes through
`record_contract_gap` and increments only the latter.

### Model attribution and cost (migration v2)

**Record `model_id` whenever you know it.** Token counts cannot be
turned into cost without it, and the ratios are not close: output bills
5x input, a cache read ~0.1x input (~0.025x on `claude-fable-5-1`), and
a cache write 1.25x at the 5-minute TTL against 2x at the one-hour one
-- all per-model. A cohort whose arms used different models can show a
token "saving" that is a cost increase.

Use the exact API model string (`claude-opus-5`, `claude-sonnet-5`,
`claude-fable-5-1`), never a date-suffixed or friendly name -- a reader
prices by looking it up, so a value it cannot match is no better than
NULL. The column is nullable; usage with no model groups under
`MODEL_UNKNOWN` and stays visible rather than being dropped or
attributed to a neighbour.

One task routinely spans several models, so **price from
`model_totals` / `tokens_by_model`, not from the scope-wide total** --
pricing a mixed total against any single model is not an approximation,
it is a wrong answer.

`cache_write_5m_tokens` / `cache_write_1h_tokens` are the producer-side
`usage.cache_creation.ephemeral_5m_input_tokens` /
`ephemeral_1h_input_tokens` breakdown. They are a breakdown **of**
`cache_write_tokens` and are never added into a total -- doing so would
count every cache write three times. Report whichever you have: both
splits with no total derives the total; all three must agree or the call
is refused. A producer with only the collapsed total keeps working
unchanged and reports the TTL mix as NULL (unknown), not zero.

## The two anti-double-counting primitives

These solve two different problems and you generally want both.

**1. `idempotency_key` (UNIQUE)** -- guards against the *same* report
arriving twice: producer retry, at-least-once redelivery, a crash
between doing the work and recording it. A repeat returns
`{"applied": False, "duplicate": True, ...}` with the original row and
changes nothing -- counters and baselines included. Omit it and a key is
derived from the full content of the report, so a byte-identical replay
still dedupes; supply your own if two genuinely distinct events could
have identical content.

**2. `counter_id` + `kind`** -- guards against *different* reports of the
same climbing counter, which is how agent CLIs actually report usage.

* `kind="DELTA"` -- the values ARE the increment, stored verbatim.
* `kind="CUMULATIVE"` -- the values are a snapshot of the counter named
  by the **required** `counter_id` (typically the agent session/process
  whose usage file you are reading). Only the rise since that counter's
  last observation is stored. Snapshots of 1000, 2500, 4000 contribute
  **4000**, not 7500. A missing `counter_id` on a cumulative sample is a
  hard error, not a default.

A counter that moves **backwards** means the process restarted and is
counting from zero again, so the new value is taken in full and the row
is flagged `counter_reset = 1`; the flag surfaces as
`counter_reset_observed` in every summary so a human can distrust the
total on purpose.

One counter spanning two tasks charges each task only its own increment.

## Reader API

```python
from terminal_mcp.work_telemetry_service import WorkTelemetryService
service = WorkTelemetryService(store)

service.task_summary(task_id)                        # None if no telemetry
service.tokens_per_completed_task(project_id=..., work_id=..., since=...)
service.work_summary(work_id)
```

`task_summary` returns: `task_id`, `work_id`, `session_id`,
`project_id`, `phase`, `analysis_tokens`, `contract_tokens`,
`worker_input_tokens`, `worker_output_tokens`, `cache_read_tokens`,
`cache_write_tokens`, `worker_turn_count`, `cache_write_5m_tokens`,
`cache_write_1h_tokens`, `model_ids`, `model_totals`,
`has_model_attribution`, `unattributed_tokens`,
`total_tokens`, `reentry_count`, `reentry_reasons`,
`contract_gap_count`, `first_pass_success`, `started_at`,
`completed_at`, `duration_seconds`, `terminal_status`,
`evidence_source`, `evidence_sources`, `confidence`,
`lifecycle_confidence`, `counter_reset_observed`, `sample_count`,
`has_token_telemetry`.

## NULL/UNKNOWN is never zero

A missing measurement and a measurement of zero are different facts.

* Token columns are nullable with **no default**. An unreported kind
  stores NULL; a passed `0` is kept as a real measured `0`.
* Per-task totals are **not** denormalized onto the rollup row -- they
  are `SUM()`ed from the samples, which returns NULL over zero rows.
  `test_the_rollup_row_carries_no_token_columns` is a structural guard:
  the moment a `DEFAULT 0` token column appears there, "never measured"
  and "measured zero" become indistinguishable.
* `tokens_per_completed_task` divides by **measured** tasks, not all
  completed ones, and reports `telemetry_coverage` alongside. Dividing
  by all of them would make per-task cost fall whenever coverage fell --
  an efficiency "win" produced entirely by losing data.
* Aggregate `confidence` is the **weakest** of its inputs
  (`MEASURED` > `DERIVED` > `ESTIMATED` > `UNKNOWN`).

## `first_pass_success` is a real tri-state

`'UNKNOWN'` until a terminal status resolves it. Derived once, in
`complete_task`, and nowhere else:

| terminal status | `reentry_count` | verdict |
|---|---|---|
| `COMPLETED` | 0 | `TRUE` |
| `COMPLETED` | > 0 | `FALSE` |
| `FAILED` | any | `FALSE` |
| `CANCELLED` | any | stays `UNKNOWN` |

`CANCELLED` stays UNKNOWN because a cancelled task never got its first
pass -- both verdicts would describe something that did not happen, and
folding it into `FALSE` would bias the comparison toward whichever arm
happened to have more cancellations.

Contract gaps do **not** falsify it: a gap absorbed without a re-entry
cost no extra round trip, which is what this flag measures.
`contract_gap_count` stays alongside as the explanatory variable.

Completing twice keeps the first verdict and timestamp. Use
`reopen_task()` to clear it explicitly if delivery reopens a task.

## Do not use input + cache_write as a cost figure

Flagged by the benchmark-harness lane and worth repeating here, because
anything reading this store could make the same mistake: a "primary
cost" metric of `input_tokens + cache_write_tokens` **omits output**,
which bills 5x input. It therefore charges an up-front-analysis pipeline
nothing for its own largest per-token cost, and it moves in the opposite
direction to real cost -- that lane's demo shows a 4.5% "saving" on that
metric against cost per completed task being 1.27x HIGHER on the same
cohort.

Nothing in this store defines or exposes such a metric: `total_tokens`
and every per-phase total sum all four billing kinds, output included.
Keep it that way.

## Cohort labelling (legacy vs new pipeline)

**Not carried by this store, on purpose.** Whether a task went through
the Analysis Gate is a fact the gate itself owns; copying it here would
create a second, drifting source of truth. Join instead:

* `queue_tasks.analysis IS NOT NULL` (migration v9 on
  `feat/analysis-gate`) is the real signal, joined on `task_id`.
* Failing that, a cohort label can be passed per sample in
  `record_usage(metadata={...})`, which is stored as JSON and is not
  interpreted by this module.

Matching controls (task profile, risk tier, complexity) likewise live on
the queue/analysis side and are joined by `task_id` -- this store
deliberately holds no copy.

## What is NOT here

No `ai_usage.db` and no `work.db` exist on this host. `ai_usage_*.py` is
an HTTP client for the external AI Usage Monitor, which reports
**percent-of-limit only, never token counts**, so it cannot be a token
source for this metric. The queue's own "work" tables live in
`queue.db`.
