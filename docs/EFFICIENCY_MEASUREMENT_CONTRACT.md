# Efficiency Measurement Contract — does upfront analysis actually reduce downstream loops?

Status: **PLANNED** (2026-09-14). Nothing in this document is built, and no
claim in it is measured. It is a *design of record* and a *pre-registration*:
it fixes the definitions, the exclusions, and the acceptance criteria **before**
any number exists, so that a later result cannot be defined into existence.

Companion to `docs/AI_ANALYSIS_GATE.md` (the rule) — this file is the
*measurement* of whether that rule pays for itself. Per §10a of that document,
this status stays `PLANNED` until an instrument exists and has been calibrated;
it may never be written as VERIFIED on the strength of a plan.

Audience: the telemetry lane (instrumentation), the benchmark lane (analysis),
and whoever later has to decide whether the Analysis Gate stays on.

---

## 0. The claim under test, stated so it can fail

> **H1.** For gated tasks, raising `decision_budget` (more upfront analysis and a
> more complete implementation contract *before* the first worker turn) reduces
> downstream rework — fewer worker turns, fewer clarifications, fewer reentries —
> by enough to outweigh the additional tokens the analysis itself costs.

Two things about this claim deserve to be said out loud before anything is built.

**It is a cost-effectiveness claim, not a quality claim.** H1 can be false while
the Analysis Gate is still worth keeping. The gate's stated purpose
(`AI_ANALYSIS_GATE.md` §1) is to prevent *confidently wrong code shipped against
a misunderstood problem* — a tail risk whose cost is measured in incidents, not
in turns. A measurement program that reports "no turn reduction" must not be
read as "the gate is useless", and this document refuses that inference in
advance. Loop count is a **proxy for one benefit**, not a verdict on the rule.

**The most likely true result is a null.** Most process interventions of this
shape produce effects smaller than the noise of a small task sample. The design
below is built so that a null is *legible and publishable* rather than being
quietly reframed. If the instrument cannot distinguish "no effect" from "not
enough data", it is not yet an instrument.

---

## 1. Audit — what exists today, and what does not

Established first-hand on the HP host on 2026-09-14 against the running control
plane (`/version` → `0.12.0`) and the real state databases. Evidence, per claim:

| # | Finding | How established |
|---|---|---|
| A1 | **No token telemetry exists anywhere.** No table in any state DB records `input_tokens`, `output_tokens`, or either cache field. | `queue_store.py` schema (migrations v1–v9); full state-dir table enumeration |
| A2 | **`ai_usage` is not a token ledger.** It is a read-only HTTP client over a *separate* project's `/api/usage`, returning per-provider **quota-window percentages** (`used_percent`, `resets_at`) — no token counts, no cache split, no per-task attribution. | `ai_usage_client.py`, `ai_usage_service.py` |
| A3 | **There is no `ai_usage.db` on this host**, and no `~/workspace/ai-usage-monitor`. Port 8787 answers nothing. The AI Usage integration is **dark on HP**. | filesystem search; `curl 127.0.0.1:8787` |
| A4 | **`queue_tasks.attempt_count` is not a loop counter.** It bumps on reconcile-and-reclaim cycles for audit visibility, so it conflates infrastructure retries with genuine rework. | `queue_store.py` `_add_v2_coordinator_columns` docstring; `_transition_locked` |
| A5 | **`metrics.py` is in-memory, per-process, and reset on restart.** It is explicitly not persisted. It cannot support any longitudinal measurement. | `metrics.py` module docstring |
| A6 | **Turn-lineage columns already exist and are unpopulated on the real work path.** `input_audit` carries `trace_id`, `parent_turn_id`, `depth`, `correlation_id`. Only `bridge.py` writes the lineage fields; all 155 real rows on HP have `trace_id = NULL`. | `audit.py` migration 3; `bridge.py`; live query of `audit.db` |
| A7 | **`ANALYSIS_UPDATED` events are timestamped but carry no payload** (`reason=None`, no metadata). We can tell *when* a contract was written; we cannot tell *what changed* or *which verdict it produced*. Gate verdicts live only in `coordinator_decision`, which is overwritten, not appended. | `queue_store.set_task_analysis`; `_record_event_locked` |
| A8 | **The Question Ledger is documented but does not exist.** `AI_ANALYSIS_GATE.md` §5 describes it as a real mechanism; there is no table, no column, and no code path for it. Clarifications are therefore **unrecorded today**. | grep across `terminal_mcp/` |
| A9 | **There is no baseline and no retrospective path to one.** HP's `queue.db` holds **0 tasks and 0 events**, and lacks the `analysis` column entirely (migration v9 has never run against it). The real queue lives on the fed-controller (`100.117.214.87`), not here; HP is a *node*. | live query; `ps aux` (node agent → remote controller) |
| A10 | **No `decision_budget` concept exists**, and no `HIGH_RISK` task profile. The gate knows `full`/`fast_fix`/`none`; `metadata.risk_level` (LOW/MEDIUM/HIGH/CRITICAL) exists separately in `dor_gate.py`. | `analysis_gate.py`; `dor_gate.py` |

**The single most consequential finding is A9.** A before/after comparison
against history is impossible because there is no history. Every number this
program will ever produce must be collected **prospectively**, which — as §7
argues — is not a limitation but the only design that could have supported a
causal claim anyway.

**The single most useful finding is A6.** The turn graph the whole measurement
depends on is already plumbed end-to-end (`core.py` → `node_client.py` →
`node_agent.py` → `audit.py`). It is not a new subsystem; it is a set of
arguments nobody passes.

---

## 2. Task profiles

Three profiles, derived from fields that **already exist**. No parallel
taxonomy: a second, drifting notion of "what kind of task is this" is exactly
the duplication `AI_ANALYSIS_GATE.md` warns about for rules.

| Profile | Resolution rule (first match wins) |
|---|---|
| **HIGH_RISK** | `metadata.risk_level ∈ {HIGH, CRITICAL}` **OR** the task declares any category in `analysis_gate.CRITIC_REQUIRED_CATEGORIES` (`state-machine`, `workflow`, `auth`, `security`, `deploy`, `data-model`, `multi-agent`, `automation`, `destructive`) |
| **FAST_FIX** | gate profile resolves to `fast_fix` (i.e. `task_class ∈ {fast_fix, hotfix}`) **and** not HIGH_RISK |
| **STANDARD** | gate profile resolves to `full` **and** not HIGH_RISK |
| *(not a profile)* | gate profile resolves to `none` — legacy/unclassified, `chore`, `docs`, `research`, `incident` |

Three decisions inside that table are deliberate and are the ones to argue with:

1. **HIGH_RISK outranks FAST_FIX.** A hotfix that touches auth is HIGH_RISK, not
   FAST_FIX. Risk is a property of the blast radius, not of the diff size — and
   the one-line change to a security path is precisely the case
   `AI_ANALYSIS_GATE.md` §8 says does the damage.
2. **Ungated tasks are excluded from the study entirely** — not treated as a
   zero-analysis control arm. They are a different population selected by a
   different rule; pooling them would import selection bias wholesale.
3. **Profile is resolved and frozen at task creation**, from fields already
   present at that moment. A profile that can be recomputed later is a profile
   that can be recomputed *after seeing the outcome*. See §10.

---

## 3. `decision_budget` — the independent variable

`decision_budget` is the **treatment**: how much upfront analysis the task is
authorised and required to do before its first worker turn. It is a declared
input, not an observed output.

| Level | Required before first dispatch | Critic review |
|---|---|---|
| **HIGH** | Full Feature Contract (all seven `FULL_REQUIRED_FIELDS`), explicit `assumptions` ledger with every HIGH/CRITICAL entry RESOLVED, plus a written `critic_result` | Mandatory, regardless of declared categories |
| **MEDIUM** | Full Feature Contract (all seven fields) and explicit `assumptions` | Only when declared categories require it (current gate behaviour) |
| **LOW** | Fast-Fix fields only (`reproduce`, `root_cause`, `expected_behavior`, `invariant`, `regression_test`, `verify_fix`); no full contract required | Never |

MEDIUM is deliberately defined as **exactly today's `enforce` behaviour**, so
that the study has a control arm that is the status quo rather than an invented
strawman. LOW is a genuine reduction below current policy and must therefore be
**opt-in per lane and never applied to HIGH_RISK tasks** — measuring a
hypothesis is not a licence to ship an auth change with no contract. This is a
hard constraint, and it costs the study some statistical power in the
HIGH_RISK stratum. That is the correct trade.

**Stamping and immutability.** `decision_budget` is written into the task
**before its first dispatch** and is immutable thereafter. A task whose
`decision_budget` is absent at first dispatch is permanently `UNASSIGNED` and
excluded from analysis — never backfilled. Mutation after first dispatch voids
the task from the study and raises a data-integrity flag; it is not silently
accepted and it is not silently dropped.

---

## 4. The observable units

Every definition below has to survive one test: *could two honest people,
looking at the same event log, disagree about the count?* Where they could, the
definition is wrong.

### 4.1 Worker turn

> A **worker turn** is one prompt delivered to a worker agent that the worker
> actually received and acted on.

Counted from `input_audit`, not from `attempt_count` (A4):

- **Include** rows where `action ∈ {send_text, send_keys}`, `result` indicates
  real delivery (`SENT` / submit-confirmed), and the row's `trace_id` resolves
  to the task under study.
- **Collapse** all rows sharing a `dispatch_idempotency_key` into **one** turn.
  This is the existing primitive that already distinguishes "the same prompt
  re-sent after a crash" from "a new prompt". It is the correct dedup key
  precisely because the system already treats it as one.
- **Exclude** rows whose delivery never confirmed and which were superseded by a
  re-send (`DELIVERY_UNKNOWN` → re-send): that is one turn, attempted twice.
- **Exclude** operator-typed input that is not a dispatch (`origin` distinguishes
  these once populated).

A task's `worker_turn_count` is the number of distinct turns between its first
dispatch and its terminal status.

### 4.2 Clarification

> A **clarification** is a worker-raised question that blocks progress and can
> only be answered by a human.

This is **unrecorded today** (A8) and is the largest new instrumentation
requirement. It requires the Question Ledger that `AI_ANALYSIS_GATE.md` §5
already promises: a `CLARIFICATION_RAISED` / `CLARIFICATION_ANSWERED` event pair
carrying the question, its impact, and its resolution.

Two boundaries that decide the count:

- A question the agent **resolved itself from evidence** (MCP, repo, runtime) is
  **not** a clarification. That is the gate working as designed (§3 of the rule
  doc), and counting it would penalise exactly the behaviour we want.
- A `NEEDS_CLARIFICATION` **gate verdict** is *not* a clarification event. It is
  a pre-dispatch block — it happens before any worker turn and belongs to the
  treatment, not the outcome. Conflating the two would let a stricter gate
  inflate its own rework metric.

### 4.3 Reentry

> A **reentry** is the task returning to an earlier pipeline stage after having
> reached a later one.

Mechanically: any transition into `PRECHECK` / `READY` / `DISPATCHING` from
`RUNNING` / `VERIFYING` / `COMPLETED`, plus any new worker turn following a
failed verification. Every reentry **must** carry a typed reason:

| Reason | Counts as rework? |
|---|---|
| `CONTRACT_GAP` | Yes — the contract was wrong or silent |
| `IMPLEMENTATION_DEFECT` | Yes — the contract was right, the code was not |
| `VERIFICATION_FAILED` | Yes |
| `USER_CHANGED_REQUIREMENT` | **No** — excluded, reported separately (§6) |
| `ENVIRONMENT_FAILURE` | **No** — excluded, reported separately (§6) |
| `INFRA_RETRY` | **No** — not a reentry at all; collapsed by idempotency key |

An untyped reentry is **not** counted as zero. It is counted as
`UNCLASSIFIED_REENTRY` and, per §6, makes the task ineligible for the primary
analysis while remaining visible in the coverage line. Missing data must never
be indistinguishable from good news.

### 4.4 Contract gap

> A **contract gap** is a defect attributable to something the implementation
> contract should have stated and did not.

This is the causally interesting quantity and the easiest one to fake, so it
carries the strictest recording rule: **a contract gap must name the specific
contract field that was absent or wrong.** A gap that cannot name a field is not
a contract gap; it is an implementation defect.

Taxonomy (closed set — an unlisted gap is filed as `OTHER` with prose, and a
growing `OTHER` bucket is itself a finding):

`MISSING_INVARIANT`, `WRONG_SOURCE_OF_TRUTH`, `ASSUMPTION_FIRED` (a recorded
assumption turned out false), `SCOPE_AMBIGUITY`, `MISSING_EDGE_CASE`,
`MISSING_UI_STATE`, `BACKCOMPAT_MISS`, `OTHER`.

`ASSUMPTION_FIRED` deserves its own treatment: it is the only gap type that is
**predicted in advance by the instrument itself**. A HIGH-impact assumption that
was marked RESOLVED and then fired is direct evidence about the quality of the
resolution, and it is the single most informative event this program can
capture. It should be reported on its own line, not pooled.

### 4.5 First-pass success

> A task is a **first-pass success** if, between first dispatch and terminal
> `COMPLETED`, it required exactly one worker turn, raised no clarification,
> had no rework-typed reentry, and reached `COMPLETED` with real verification
> evidence.

All four conditions, conjunctively. The fourth is not decoration: without it,
"first-pass success" degrades into "nobody checked", which would reward the
exact failure `AI_ANALYSIS_GATE.md` §10 exists to prevent. `COMPLETED` as a
status label is not evidence; `verification_evidence` is.

**FPS is the headline metric but not the primary statistical one.** See §8 — a
binary outcome on a small sample is the weakest test available, and
`worker_turn_count` (a count) carries several times the information per task.
Report FPS because it is what a human wants to know; *power the study* on turn
count.

---

## 5. Token accounting

### 5.1 There is no data source (A1–A3)

This section specifies a formula for data that **does not exist and cannot
currently be collected**. Neither the queue nor the audit store records tokens;
`ai_usage` reports quota percentages, not tokens, and is dark on this host.
Anything reported as a token saving before §11's acceptance criteria are met is
fabricated. This is stated bluntly because a plausible-looking token number is
the most likely way this program produces a false result.

### 5.2 The formula — and why `input + cache_write` is the wrong one

A proposal in circulation defines `primary_cost_tokens = input_tokens +
cache_write_tokens`, with output and cache reads reported but excluded. **That
definition is biased in favour of H1** and should not be adopted. Three reasons,
in increasing order of severity:

1. **It sums tokens of different unit costs at parity.** Cache writes bill at
   **1.25× base input for the 5-minute TTL and 2× for the 1-hour TTL**; cache
   reads bill at **0.1×** (0.025× on Claude Fable 5.1). Adding a 2× token to a
   1× token and calling the result a cost is a unit error.
2. **A single `cache_write` field is insufficient.** The write multiplier
   depends on TTL, and the API already reports the split —
   `usage.cache_creation.ephemeral_5m_input_tokens` and
   `ephemeral_1h_input_tokens`. Collapsing them discards the multiplier.
3. **Excluding output tokens biases the result toward the hypothesis.** Output
   bills at **5× input** across the current lineup ($5/$25 for Opus 5, $2/$10
   for Sonnet 5, $1/$5 for Haiku 4.5, $10/$50 for Fable 5.1). Upfront analysis
   produces its deliverable — the contract — *as output tokens*. A metric that
   excludes output therefore charges the HIGH `decision_budget` arm **nothing
   for its own primary cost** while charging the LOW arm fully for the rework it
   does. This is not a conservative simplification; it systematically favours
   the conclusion the study is meant to test.

**Use cost-weighted tokens, normalised to base-input-equivalents:**

```
cost_units =  input_tokens
            + cache_write_5m  × 1.25
            + cache_write_1h  × 2.00
            + cache_read      × R_read      # 0.1 normally; 0.025 on Fable 5.1
            + output_tokens   × R_out       # 5.0 across the current lineup
```

Rules that make it honest:

- **Never hardcode the multipliers.** Record `model_id` on every row and
  resolve the coefficients from a versioned table **at report time**, pinning
  the `price_table_version` in the report header. Prices change; a re-analysis
  must be able to reprice history rather than inherit a number baked in at
  collection time. The measurement store therefore records what was *consumed*
  (tokens, by model) and never prices it — a price copy inside it would be a
  second source of truth that drifts silently and retroactively falsifies rows
  already written. Pricing is the reader's job; the table version is a property
  of the report, not of the measurement.
- **Report the four raw counts alongside `cost_units`, always.** The weighted
  figure is for comparison; the raw counts are what let a later reader
  re-derive it under different assumptions.
- **`input_tokens` is the uncached remainder only.** Total prompt size is
  `input + cache_creation + cache_read`. Reporting `input_tokens` as "the
  prompt size" understates a cached agent loop by an order of magnitude.
- **Report cache-read volume separately and prominently**, not because it is
  expensive (it is not) but because it is the **stale-context detector** of §7.3.

### 5.3 Attribution

Tokens must be attributable to a task and to a phase (`ANALYSIS` vs
`IMPLEMENTATION` vs `VERIFICATION`), or the central quantity — "did analysis
cost less than the rework it avoided?" — cannot be computed at all. Per-host,
per-provider quota percentages (A2) can never supply this. Attribution requires
the turn graph of A6 plus a per-turn usage record.

---

## 6. Exclusion rules

Pre-registered, and — this is the part that matters — **adjudicated blind to
arm** wherever a human judgement is involved. An exclusion rule applied after
seeing which arm a task is in is not an exclusion rule; it is a way of choosing
the answer.

| Excluded | Rule | Treatment |
|---|---|---|
| Environment failure | Reentry typed `ENVIRONMENT_FAILURE` | Turn not counted; task retained if it has ≥1 valid turn |
| Test-artifact failure | Failure attributable to fixture/harness, not to the change | As above |
| Infra retry | Same `dispatch_idempotency_key` | Collapsed into one turn (§4.1) |
| Requirement change | Reentry typed `USER_CHANGED_REQUIREMENT` | Task **flagged and reported separately**, never silently dropped |
| Incomplete telemetry | Any required field missing | Excluded from the affected statistic; counted in the **coverage line** |
| Post-hoc mutation | `decision_budget` or profile changed after first dispatch | Task voided; data-integrity flag raised |
| Concurrent-agent contamination | >1 agent held a lease on the task's resources during its run | Excluded from primary; reported in a contamination line (§7.7) |

Two standing rules govern all of them:

- **Missing is never zero.** A task with no token data is not a cheap task.
  Every statistic publishes its own denominator and its own coverage percentage.
- **A task excluded from one statistic is not excluded from all.** A task with
  broken token telemetry still contributes to turn counts. Exclusions are
  per-metric, and each metric reports its own N.

---

## 7. The critic's case — seven ways this measurement lies

Each of these is a way the program produces a confident number that is wrong.
The guard is listed with the failure, because a failure mode named without a
guard is just a disclaimer.

### 7.1 More analysis tokens, no turn reduction

**The likeliest outcome.** Analysis costs real tokens up front; if turns do not
fall, the intervention is a pure loss on this metric.

The trap is not the null itself — it is what happens next: the temptation to
re-slice until some subgroup shows an effect. Guard: **the primary metric, the
primary stratum, and the direction of effect are fixed in this document before
data exists.** Subgroup findings are exploratory, are labelled as such, and
never carry a confidence band. A null on the primary metric is reported as a
null on the primary metric.

Second guard: report the **cost ratio**, not just the turn delta. If analysis
costs 8k cost-units and saves 0.4 turns at 12k cost-units each, that is a win
*even with a statistically insignificant turn reduction*. Conversely a
significant turn reduction that costs more than it saves is a loss. The decision
quantity is cost per completed task, not turns.

### 7.2 Mixed task complexity

The dominant confounder, and large enough to manufacture any result the analyst
wants. Task difficulty varies by far more than the effect being measured, so any
imbalance in difficulty between arms swamps the treatment.

Guard: **stratified randomised assignment (§9), not observational comparison.**
Plus a pre-registered complexity covariate recorded at creation (before
assignment, blind to arm). Any group whose complexity distribution differs
materially between arms is downgraded a confidence band and flagged
`MIXED_COMPLEXITY`.

Note what this rules out: **a before/after comparison cannot support a causal
claim here and must not be run as the primary design.** Calendar time is
confounded with the codebase changing, with the operator learning the system,
and with the task mix shifting. Before/after is available only as a descriptive
secondary.

### 7.3 Stale cached context

An agent operating from a warm cache may be reasoning about a repo state that no
longer holds — producing rework that looks like a contract gap but is actually a
cache-coherence failure. This would be **attributed to the treatment**, and
since the HIGH arm carries more context, it would be attributed disproportionately
to the arm we are testing.

Guard: record `cache_read_input_tokens` per turn (§5.2) and the repo `HEAD` at
turn start. A reentry whose turn read a large cached prefix written before an
intervening `HEAD` change is typed `STALE_CONTEXT`, not `CONTRACT_GAP`. Without
this, cache staleness is silently scored as an analysis failure.

### 7.4 User requirement changes

A requirement change produces exactly the signature of a contract gap: rework,
extra turns, a failed first pass. It is not one — nobody could have contracted
for it.

Guard: the `USER_CHANGED_REQUIREMENT` reentry type, excluded from rework counts
and reported on its own line. **The adjudication must be blind to arm**, because
"was that a requirement change or a contract gap?" is precisely the judgement an
invested analyst gets wrong in a consistent direction. If blind adjudication is
not operationally possible, the honest fallback is to report both — the metric
with these reentries excluded, and with them included — and let the gap between
the two bound the analyst's discretion.

### 7.5 Retries caused by infra

Already the most concrete trap in the codebase: `attempt_count` bumps on
reconcile-and-reclaim (A4), so the obvious loop metric counts crashes as
rework. A lane with a flaky node would score as a lane with bad contracts.

Guard: never use `attempt_count`; count turns via `dispatch_idempotency_key`
collapse (§4.1). This is not a new mechanism — the system already uses that key
to make re-dispatch idempotent, so the measurement inherits a definition the
runtime already enforces.

### 7.6 Partial completion

A task marked `COMPLETED` with 80% of the contract delivered is a first-pass
success by any status-based definition, and it is the failure mode that
**rewards weak contracts**: the less the contract promised, the easier it is to
satisfy. Left unguarded, this alone could produce a spurious win for the LOW arm
*or* for a HIGH arm that learned to write vague acceptance criteria.

Guard: first-pass success requires real `verification_evidence` (§4.5), and
acceptance must be evaluated against the contract's **own** `acceptance_tests` —
which, being recorded pre-dispatch and immutable, cannot be retrofitted to match
what was built. A contract whose acceptance criteria were edited after first
dispatch voids the task (§6).

### 7.7 Concurrent agents

Multiple agents on overlapping ground produce rework caused by *collision*, not
by understanding — the `AI_ANALYSIS_GATE.md` §7 "permission ≠ occupancy"
failure. With several lanes live on one host, this is not hypothetical.

Guard: record lease/lock holders per turn. A task whose run overlapped another
agent's lease on the same resources is excluded from the primary analysis and
reported in a contamination line. If the contamination rate is high, that is a
finding about the orchestration, and it must be reported as one rather than
absorbed into the treatment effect.

---

## 8. Sample size and confidence

The uncomfortable arithmetic, stated plainly because it determines whether this
program can produce a result at all.

**Binary first-pass success.** Detecting a rise from 40% to 60% at 80% power,
α = 0.05, two-sided, needs roughly

```
n ≈ 16 · p̄(1 − p̄) / δ²  =  16 · 0.25 / 0.04  ≈  100 per arm
```

**Worker-turn count.** A count outcome carries more information per task. For a
~30% reduction in mean turns (2.0 → 1.4) with realistic overdispersion, the
requirement is roughly **40–60 per arm** — the reason §4.5 designates turn count
as the primary statistical metric even though FPS is the headline.

**Therefore: 10 matched tasks per group is a reporting floor, not a sample
size.** Endorsed as a floor — below it, publish no figure at all — but it must
not be mistaken for sufficiency. At n = 10 per arm the study can detect only
effects far larger than any plausible true effect, which means a "significant"
result at that size is more likely to be noise than signal.

| Label | N per arm | What may be said |
|---|---|---|
| `INSUFFICIENT_DATA` | < 10 | No figure. Coverage and counts only. |
| `LOW` | 10–39 | Descriptive only. **No directional claim**, no savings %. |
| `MODERATE` | 40–99 | Directional claim permitted for turn count, with interval. |
| `HIGH` | ≥ 100 | Directional claim permitted for FPS and turn count. |

This is one band stricter than the proposal in circulation, in exactly the
region where over-claiming is easiest — 10–19 tasks is not a basis for a savings
percentage, however conservatively it is computed.

**Time to power.** At `T` eligible gated tasks per day split across two arms,
reaching the `MODERATE` band takes about `2 × 40 / T` days. The honest
implication of A9 — the queue is empty and the instrument does not exist — is
that **the near-term deliverable of this program is a calibrated instrument and
a pre-registration, not a result.** Any report produced before the `MODERATE`
band is reached should say `INSUFFICIENT_DATA` in its headline and nowhere
imply a direction.

**Reporting conventions.** Medians with p25/p75, never means — turn counts are
right-skewed and a single 9-turn task moves a mean of ten tasks by a full turn.
Every figure carries its N, its coverage percentage, and its band. Any savings
figure is the **less favourable** of the point estimate and a bootstrap lower
bound, floored at zero, and is always labelled an **observed difference under
randomisation**, never a causal claim about a mechanism.

---

## 9. Assignment and cohort matching

**Randomise; do not match observationally.** Within each task profile
(§2), assign `decision_budget` by a pre-registered rule at task creation —
before anyone has read the task closely enough to judge its difficulty.
Observational matching on a small sample cannot balance the confounder that
matters most (§7.2), because difficulty is only partly observable and the
unobserved part is exactly what drives rework.

Constraints on assignment:

- **Stratify by task profile.** HIGH_RISK, STANDARD and FAST_FIX are analysed
  separately and never pooled — the effect could plausibly run in *opposite*
  directions across them, and pooling would cancel a real result into a null.
- **HIGH_RISK is never assigned LOW.** A safety floor beats statistical power
  (§3). HIGH_RISK compares HIGH against MEDIUM only.
- **Record the assignment and the assigner**, so that deviations are visible.
  A lane that quietly re-assigns its own tasks converts the experiment back into
  an observational study without anyone noticing.
- **Unknown covariate ⇒ its own stratum.** Never pool an unknown into a known
  bucket; an `unknown` model or project is its own group, reported as such.

Where randomisation is genuinely impossible (a lane with one operator who must
choose), the fallback is **interleaved alternating assignment by creation
order** — weaker, but still blind to the task's content at the moment of
assignment, which is the property that matters.

---

## 10. Anti-gaming rules

Every metric here is written by the agents being measured. That is the
structural problem, and it is worth being explicit that these rules assume good
faith and are designed to make *drift* visible — not to defeat a determined
adversary, which is not achievable when the measured party writes the record.

| Rule | The gaming it prevents |
|---|---|
| `decision_budget` and profile are stamped **before first dispatch** and immutable | Relabelling a task's arm after seeing how it went |
| Contract **fields** are immutable after first dispatch; later edits are appended, not overwritten | Retrofitting acceptance criteria to match what was built (§7.6) |
| `ANALYSIS_UPDATED` events must carry **what changed** and **the resulting gate verdict** (A7 fixes this) | Silently weakening a contract until it passes |
| Contract written **after** first dispatch ⇒ task excluded, flagged `BACKFILLED_CONTRACT` | Doing the work, then writing the analysis that "predicted" it |
| A contract gap must **name a contract field** (§4.4) | Reclassifying every gap as an implementation defect to protect the analysis metric |
| Verification evidence must be **real runtime evidence**, per `AI_ANALYSIS_GATE.md` §10 | "Tests pass" standing in for "it works" |
| Exclusion adjudication is **blind to arm** (§6, §7.4) | Excluding inconvenient tasks |
| Gate verdicts are **appended**, never overwritten (A7) | Losing the record of how many attempts it took to pass |
| Per-metric **coverage percentage** published with every figure | Improving a statistic by losing data |

One rule deserves emphasis because it inverts the obvious incentive:
**a high `NEEDS_CLARIFICATION` rate at the gate is not a bad outcome and must
never be reported as one.** It is the gate doing its job pre-dispatch. If the
program ever rewards lanes for passing the gate on the first try, it will have
built an incentive to write contracts that pass rather than contracts that are
true — and that is a worse outcome than never having measured anything.

---

## 11. Acceptance criteria

### 11.1 Telemetry lane

The instrument is complete when **all** of the following hold. Each is
checkable; none is satisfied by code existing.

- **T1 — Turn graph populated on the real work path.** `trace_id`,
  `parent_turn_id` and `depth` are written by dispatch, not only by
  `bridge.py` (A6). Acceptance: for a real dispatched task, every turn is
  reachable from the task id, verified on the live runtime.
- **T2 — Turn counting matches the definition.** A test proves that N
  reconcile-and-reclaim cycles on one dispatch yield `worker_turn_count == 1`
  while `attempt_count > 1` (A4). This is the regression test for §7.5.
- **T3 — Per-turn token usage recorded**, with all four counts, the TTL split
  on cache writes, and `model_id` — **not** a price or a price-table version,
  which belong to the report (§5.2). Acceptance: a
  real turn's recorded totals reconcile against the provider's own usage
  numbers — not merely that the columns are non-null.
- **T4 — Phase attribution.** Every token row carries `ANALYSIS` /
  `IMPLEMENTATION` / `VERIFICATION` (§5.3).
- **T5 — Question Ledger exists.** `CLARIFICATION_RAISED` /
  `CLARIFICATION_ANSWERED` events, with the question, impact and resolution
  (A8). This closes a gap between what `AI_ANALYSIS_GATE.md` §5 claims and what
  the code does — worth doing on its own merits, independent of this study.
- **T6 — Typed reentries.** Every reentry carries a reason from §4.3;
  untyped reentries are recorded as `UNCLASSIFIED_REENTRY`, never dropped.
- **T7 — Append-only gate verdicts and contract history** (A7, §10).
- **T8 — Stale-context inputs.** `cache_read` per turn and repo `HEAD` at turn
  start (§7.3).
- **T9 — Concurrency inputs.** Lease/lock holders per turn (§7.7).
- **T10 — Persistence.** Nothing in the measurement path depends on
  `metrics.py`'s in-memory registry (A5).
- **T11 — Backward compatible.** Every existing task and query behaves exactly
  as before; no backfill, no mass reclassification. Same constraint the
  Analysis Gate itself accepted, and for the same reason.

### 11.2 Benchmark lane

- **B1** — Computes every §4 unit strictly from recorded events; **no metric is
  derived from `attempt_count`** or from any status label alone.
- **B2** — Reports `INSUFFICIENT_DATA` with **no** savings figure below the §8
  floor, and no directional claim below `MODERATE`. Today, against an empty
  queue, this is the only correct output, and producing it is a passing result.
- **B3** — Every figure carries N, coverage %, and confidence band.
- **B4** — Exclusions are applied per-metric, reported per-metric, and never
  reduce a denominator silently (§6).
- **B5** — `USER_CHANGED_REQUIREMENT` and `ENVIRONMENT_FAILURE` tasks are
  flagged and reported, never dropped.
- **B6** — Token figures use §5.2's cost-weighted formula with the raw four
  counts alongside; the multipliers are resolved from a versioned price table,
  not hardcoded.
- **B7** — Savings are reported as the less favourable of point estimate and
  bootstrap lower bound, floored at zero, labelled an observed difference under
  randomisation.
- **B8** — Medians with p25/p75; no means (§8).
- **B9** — Strata are never pooled across task profiles (§9).
- **B10** — The report states the assignment mechanism actually used. If
  assignment was not randomised, the report says so and **downgrades every claim
  to descriptive** — no causal language, whatever the N.

### 11.3 Not in scope

No production deploy. No change to Analysis Gate validators, the delivery gate,
or production UI. The `delivery-gate` branch referenced in the task brief **does
not exist on this remote** (checked 2026-09-14; the only feature branch newer
than `main` is `feat/analysis-gate`), so nothing here is written against it.

---

## 12. Open questions

Recorded rather than guessed, per §5 of the rule document. Each blocks something
specific.

| # | Question | Impact | Blocks |
|---|---|---|---|
| Q1 | Which host owns the measurement store? HP is a node; the real queue is on the fed-controller (A9). | HIGH | T1–T10 — the instrument cannot be built in the wrong place |
| Q2 | Can per-turn provider usage actually be captured for CLI-driven workers (Claude Code / Codex in tmux), where there is no API response object to read `usage` from? | **CRITICAL** | All of §5. If the answer is no, token accounting is not merely unbuilt but **not feasible as specified**, and the program must fall back to turn-count and wall-clock outcomes only. |
| Q3 | Is randomised assignment operationally acceptable to the operator, or is the fallback of §9 required? | HIGH | §9, and therefore whether any causal claim is ever licensed |
| Q4 | Is the AI Usage Monitor intended to be restored on HP, and if so does its own store hold real token counts rather than quota percentages (A2, A3)? | MEDIUM | Whether §5.3 attribution has any second source |

**Q2 is the one to resolve first.** It is the assumption whose failure would
invalidate the largest part of this design, and — per the rule this document is
measuring — an unresolved CRITICAL-impact assumption blocks implementation.
Nothing in §5 should be built until Q2 is answered with evidence.

---

## 13. Cross-lane conformance (2026-09-14)

Checked first-hand against the telemetry lane's committed schema
(`feat/work-efficiency-telemetry` @ `473a629`: `work_telemetry_store.py`,
`work_telemetry_service.py`). Recorded here because the contract is only
useful if it is checked against what is actually being built.

**Conformant, and better than this document specified:** the separate
telemetry DB (not bolted onto `QUEUE_MIGRATIONS`, so lanes cannot race on a
version number); `IF NOT EXISTS` on every v1 statement (found by a crash test,
not by reasoning — Python's sqlite3 does not wrap DDL in a transaction);
the v2 `cache_write_5m_tokens` / `cache_write_1h_tokens` split with `model_id`
(§5.2 satisfied at source); `reported_*` columns held beside derived values;
and a `confidence` tri-state (`MEASURED`/`DERIVED`/`ESTIMATED`/`UNKNOWN`) that
implements §6's "missing is never zero" at the schema level.

On `price_table_version`: the telemetry lane records **no price and no price
table**, deliberately, and is right to. §5.2 originally asked for a
`price_table_version` on every row; that was wrong and has been corrected at
source rather than patched here. A price copy inside a measurement store is a
second source of truth that drifts silently and retroactively falsifies rows
already written. The store records what was consumed; the report prices it and
pins the table version.

**Four gaps, each blocking a specific clause:**

| # | Gap | Blocks |
|---|---|---|
| C1 | `telemetry_reentries.reason` has no `STALE_CONTEXT`. The CHECK constraint admits only `TEST_FAILURE`, `CONTRACT_GAP`, `IMPLEMENTATION_BUG`, `ENVIRONMENT_FAILURE`, `USER_CHANGED_REQUIREMENT`, `DELIVERY_FAILURE`, `MERGE_CONFLICT`, `OTHER` — so a stale-context reentry is **rejected at the store and lands in `OTHER`**, the generic bucket. The benchmark lane names `STALE_CONTEXT` in its report; there is no source for it. | §7.3 |
| C2 | **No clarification concept anywhere** — no table, no event, no column. The Question Ledger (`AI_ANALYSIS_GATE.md` §5, A8) is still unbuilt, so the "raised no clarification" condition of first-pass success **cannot be evaluated**, and §4.2 is unmeasurable. | T5, §4.2, §4.5 |
| C3 | `telemetry_tasks.first_pass_success` is a **stored tri-state written by the reporter**, not derived from turns, reentries and evidence. The measured party writes its own outcome directly. §4.5 defines FPS as a *derivation*; storing it as an input is an anti-gaming hole regardless of who currently writes it. Fix: keep the column as a cache if useful, but have the report **recompute** from the underlying rows and flag disagreement. | §4.5, §10 |
| C4 | **No `decision_budget`, task profile or risk class** on `telemetry_tasks`. Stratification (§9) and the never-pool rule (B9) therefore depend on an unstated join back to queue metadata. Whatever that join is, it must be explicit and recorded per task — a stratification whose key is recomputed at report time can be recomputed after seeing the outcome. | §2, §3, §9, B9 |

### 13.1 Differential measurement on first-pass success

A structural bias, not a data-quality warning, and it needs stating before any
FPS number is produced.

FPS requires real `verification_evidence` (§4.5, correctly). Tasks without it
resolve to `UNKNOWN` and are **excluded from the FPS denominator**. But the
`decision_budget = HIGH` arm is *required by its own contract* to carry a
`live_verification` plan — so it is systematically **more likely to record the
evidence that makes a task scoreable at all**.

The treatment therefore changes the probability that a task can be measured on
the very metric being compared. That is differential measurement, and its
direction is predictable: it inflates the HIGH arm's apparent FPS by selecting
the well-run tasks in the control arm out of the denominator.

A coverage-imbalance warning catches gross cases but frames this as an
accident. It is not. The guard is to **report FPS coverage per arm as a
first-class result**, and to treat any material imbalance on FPS specifically
as a finding about the metric rather than a caveat on it. Where coverage
differs by arm, the honest headline is the turn-count comparison (§4.5), whose
denominator does not depend on the treatment.

### 13.2 The exact coverage test — why the *gap* is the wrong quantity

The benchmark lane derived a correct partial-identification bound: if an arm
scores a fraction `c` of its tasks at observed rate `r`, its true rate lies in

```
[ r·c ,  r·c + (1 − c) ]
```

because the unscored tasks are, at the extremes, all failures or all
successes. That is right, and the interval width is `(1 − c)` — it is governed
by **missingness level**, not by the gap between arms.

It then concluded that a scoreability gap of `G` points can account for up to
`G` points of apparent first-pass difference, and moved the headline off FPS
when `gap ≥ difference` (requiring both to be non-zero, since either zero makes
the test vacuously true). **`G` is an upper bound, not the governing quantity,
and the non-zero guard inverts the result in the regime this project is
actually in.** Checked numerically:

| Case | Gap `G` | Observed diff `D` | `gap ≥ diff` fires? | Can coverage alone explain it? |
|---|---|---|---|---|
| One arm at full coverage (the case checked) | 10 | 10 | **yes** | **no** — conservative, errs safe |
| **Both arms at 50% coverage, equal** | **0** | **100** | **no** | **yes** — rule misses it entirely |
| Both arms at 70% coverage, equal | 0 | 85 | no | no — correctly not explained |
| Small gap, large effect | 3 | 60 | no | no — correctly not moved |

The second row is the one that matters. Arm A: 100 tasks, 50 scored, all 50
succeeded (`r = 1.00`, `c = 0.50`, true rate ∈ [0.50, 1.00]). Arm B: 100 tasks,
50 scored, none succeeded (`r = 0.00`, `c = 0.50`, true rate ∈ [0.00, 0.50]).
The gap is **zero**, the observed difference is **100 points**, and both arms'
true rates could be exactly 0.50. Coverage explains the entire result, and a
gap-based rule with a non-zero guard reports it as a directional finding.

That is not an exotic configuration. It is *equal, moderate coverage in both
arms* — the expected early state of this program, since the telemetry store
records `first_pass_success` as `UNKNOWN` by default and evidence capture is
the last thing to come online.

**Use the interval-overlap test directly:**

```
explained_by_coverage  =  intervals [r_A·c_A, r_A·c_A + (1−c_A)]
                          and       [r_B·c_B, r_B·c_B + (1−c_B)]
                          overlap
```

If they overlap, equal true rates are consistent with the data and **no
directional claim is licensed**, whatever the gap and whatever the N. This is
exact rather than a bound, needs no degenerate-case guards (both zero-gap and
zero-difference fall out correctly), and subsumes the `gap ≥ difference` rule
and the fixed-points backstop as special cases.

The wider point for §8: **coverage is a partial-identification problem, not a
confidence problem.** A wider sample does not shrink these intervals — only
recording the evidence does. Reporting an interval that spans the null
alongside a tight confidence band is not a contradiction; it means the
uncertainty is in what was *measured*, not in how *much* was measured.

#### 13.2.1 Two refinements, and why both warnings stay

**The overlap test requires at least one interval to have width.** Two fully
scored arms produce point intervals; two coinciding points mean the rates are
genuinely equal — an *identified null*, not an identification failure. With
`c = 1` there is no missingness for coverage to explain, so the rule must not
fire regardless of whether the points coincide. Verified:

| Case | Intervals | Fires |
|---|---|---|
| Both fully scored, equal rates | `0.60` / `0.60` | no — identified null |
| Both fully scored, differing rates | `0.80` / `0.50` | no |
| One full, one partial, overlapping | `0.55` / `0.45–0.55` | yes |
| Equal 90% coverage, **zero** observed difference | `0.54–0.64` / `0.54–0.64` | **yes** |

The fourth row is worth keeping rather than guarding away. A null observed
under incomplete coverage is **not an identified null** — the data are equally
consistent with a real difference. Firing there is correct and informative, so
the zero-difference guard is rightly gone: the rule should be as willing to
refuse a null as to refuse an effect.

**The gap-based warning is not a duplicate of the overlap test and should
stay.** They answer different questions:

| | Question | What it is about |
|---|---|---|
| Interval overlap | Given worst-case assumptions about *which* tasks went unscored, is the direction determined at all? | **Identification** — may a claim be made? |
| Coverage gap by arm | Is scoreability itself treatment-dependent? | **Selection** — is the point estimate inside the interval trustworthy? |

The overlap test is deliberately agnostic about the missingness *mechanism*:
it assumes the worst and is therefore always valid, but weak. The gap warning
says when missingness is plausibly non-random with respect to the treatment —
which is exactly the situation in which the point estimate inside the interval
cannot be taken as "probably about right", and the worst-case bound is all you
actually have. An effect can survive identification while the instrument that
produced it was still corrupted by the treatment; that is §13.1's original
concern and it does not disappear when the interval happens to be narrow.

So both stay, under **distinct labels** — `IDENTIFICATION` and `SELECTION` —
so that a reader cannot mistake one fact stated twice for two independent
problems, or vice versa.
