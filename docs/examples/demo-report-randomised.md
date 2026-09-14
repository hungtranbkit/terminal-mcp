# Before/After Efficiency Benchmark

- Generated: `2026-09-14T08:36:54+00:00` (harness v1.1.0)
- Verdict: **COMPARISON_AVAILABLE**
- Cohort assignment: **randomised**
- Price table: `2026-06-24`, cache-TTL policy `split_required`
- Tasks loaded from all sources: 180
- Reporting floor: 10 matched tasks per arm, per risk class

## Sources

| Source | Path | Available | Tasks | Detail |
| --- | --- | --- | ---: | --- |
| `queue.db` | `/nonexistent/queue.db` | no | 0 | file does not exist |
| `work.db (/nonexistent)` | `/nonexistent/work.db` | no | 0 | file does not exist -- this telemetry is not produced on this host yet |
| `ai_usage.db (/nonexistent)` | `/nonexistent/ai_usage.db` | no | 0 | file does not exist -- this telemetry is not produced on this host yet |
| `work_telemetry.db` | `/home/dell/.local/state/terminal-mcp/work_telemetry.db` | no | 0 | file does not exist -- no worker has emitted telemetry on this host yet |
| `work_telemetry.db` | `/nonexistent/work_telemetry.db` | no | 0 | file does not exist -- no worker has emitted telemetry on this host yet |
| `jsonl` | `tests/fixtures/bench_demo_tasks.jsonl` | yes | 180 | read 180 record(s) |

## Risk class: HIGH_RISK

- Verdict: **COMPARISON_AVAILABLE** · confidence **MODERATE**
- Matched tasks: legacy 45 / new-pipeline 45 across 1 stratum/strata (controls: profile, complexity, project)
- Cost per completed task (new / legacy): **1.38x** — more expensive

| | Legacy | New pipeline |
| --- | ---: | ---: |
| Matched tasks | 45 | 45 |
| Complete token telemetry | 45 | 45 |
| Priceable (cost units computable) | 45 | 45 |
| First-pass success (verified only) | 24% (45 known) | 58% (45 known) |
| Re-entries from excluded reasons only | 2 | 5 |

First-pass success above is **descriptive only** — a 40%→60% shift needs roughly 100 matched tasks per arm under randomisation before a direction can be claimed.

Tasks left out: surplus in stratum 0, unknown cohort 0, unmatched stratum 0.

| Metric | Legacy | New pipeline | Conservative saving |
| --- | --- | --- | --- |
| Cost (base-input-equivalents) ⭐ decision metric | 76,182.50 [65,363.75–88,870.10] (n=45) | 105,457.15 [93,290.20–117,292.70] (n=45) | no measurable difference |
| Worker turns ◆ primary statistical metric | 7 [5–8] (n=45) | 3 [2–4] (n=45) | **28.6%** |
| Re-entries (excluded reasons removed) | 1 [0–1] (n=45) | 0 [0–0] (n=45) | no measurable difference |
| primary_cost_tokens (input + cache write) | 13,778 [11,717–15,842] (n=45) | 11,547 [9,358–13,577] (n=45) | **3.5%** |
| Input tokens (uncached remainder) | 6,259 [4,333–7,551] (n=45) | 3,260 [2,549–4,117] (n=45) | **31.3%** |
| Output tokens | 9,374 [8,009–11,762] (n=45) | 15,695 [13,173–17,677] (n=45) | no measurable difference |
| Cache read tokens | 141,503 [108,714–161,414] (n=45) | 131,848 [110,145–170,551] (n=45) | no measurable difference |
| Cache write tokens | 7,399 [5,808–9,237] (n=45) | 8,239 [6,321–10,156] (n=45) | no measurable difference |
| Total prompt tokens | 153,833 [121,149–173,349] (n=45) | 143,757 [122,588–184,296] (n=45) | no measurable difference |
| Retries | — (n=0) | — (n=0) | not stated (only 0 matched task(s) per group record this metric) |
| Duration | 1,944.0s [1,560.0s–2,272.0s] (n=45, 1 outlier) | 956.0s [656.0s–1,285.0s] (n=45) | **37.7%** |

| Re-entry reason | Legacy | New pipeline | Counted |
| --- | ---: | ---: | --- |
| `CONTRACT_GAP` | 26 | 0 | yes |
| `ENVIRONMENT_FAILURE` | 5 | 0 | no — reported separately |
| `USER_CHANGED_REQUIREMENT` | 0 | 5 | no — reported separately |

## Risk class: STANDARD

- Verdict: **COMPARISON_AVAILABLE** · confidence **MODERATE**
- Matched tasks: legacy 45 / new-pipeline 45 across 1 stratum/strata (controls: profile, complexity, project)
- Cost per completed task (new / legacy): **1.27x** — more expensive

| | Legacy | New pipeline |
| --- | ---: | ---: |
| Matched tasks | 45 | 45 |
| Complete token telemetry | 45 | 45 |
| Priceable (cost units computable) | 45 | 45 |
| First-pass success (verified only) | 18% (45 known) | 51% (45 known) |
| Re-entries from excluded reasons only | 2 | 5 |

First-pass success above is **descriptive only** — a 40%→60% shift needs roughly 100 matched tasks per arm under randomisation before a direction can be claimed.

Tasks left out: surplus in stratum 0, unknown cohort 0, unmatched stratum 0.

| Metric | Legacy | New pipeline | Conservative saving |
| --- | --- | --- | --- |
| Cost (base-input-equivalents) ⭐ decision metric | 77,476.10 [71,688.35–87,780.20] (n=45) | 98,082.05 [91,150.10–113,377.40] (n=45) | no measurable difference |
| Worker turns ◆ primary statistical metric | 6 [5–8] (n=45) | 4 [3–5] (n=45) | **33.3%** |
| Re-entries (excluded reasons removed) | 0 [0–1] (n=45) | 0 [0–0] (n=45) | — |
| primary_cost_tokens (input + cache write) | 13,580 [11,716–15,307] (n=45) | 11,383 [10,071–13,292] (n=45) | **4.5%** |
| Input tokens (uncached remainder) | 5,350 [4,130–6,526] (n=45) | 3,182 [2,826–4,206] (n=45) | **21.3%** |
| Output tokens | 9,978 [7,931–11,756] (n=45) | 13,986 [12,361–17,463] (n=45) | no measurable difference |
| Cache read tokens | 137,019 [110,691–160,817] (n=45) | 143,326 [132,160–168,629] (n=45) | no measurable difference |
| Cache write tokens | 7,836 [6,794–9,376] (n=45) | 8,008 [6,845–9,763] (n=45) | no measurable difference |
| Total prompt tokens | 152,663 [124,879–172,093] (n=45) | 154,567 [140,011–182,706] (n=45) | no measurable difference |
| Retries | — (n=0) | — (n=0) | not stated (only 0 matched task(s) per group record this metric) |
| Duration | 1,824.0s [1,320.0s–2,056.0s] (n=45, 1 outlier) | 988.0s [824.0s–1,495.0s] (n=45) | **24.2%** |

| Re-entry reason | Legacy | New pipeline | Counted |
| --- | ---: | ---: | --- |
| `CONTRACT_GAP` | 21 | 0 | yes |
| `ENVIRONMENT_FAILURE` | 5 | 0 | no — reported separately |
| `USER_CHANGED_REQUIREMENT` | 0 | 5 | no — reported separately |

## How to read this

- **Risk classes are never pooled.** There is no overall number on purpose: the effect can run in opposite directions across HIGH_RISK / STANDARD / FAST_FIX, and pooling cancels a real result into a null.
- Figures are **medians** with a p25–p75 spread, over tasks matched pairwise within a risk class. A single extreme task cannot move them; outliers are counted, not deleted.
- **Cost (base-input-equivalents)** is the metric decisions are made on: every token converted at its real billing ratio (cache-write 1.25x/2x by TTL, cache-read ~0.1x, output 5x). `primary_cost_tokens` is shown because the brief named it, but it is a raw diagnostic — it sums tokens of different unit cost and omits output entirely.
- **worker_turn_count** is the primary statistical metric (a count has far more power than a binary at these sizes); first-pass success is the headline outcome but needs ~100 matched tasks per arm before any direction is claimed.
- A saving is the **lesser** of the point estimate and the lower bound of a 90% seeded bootstrap interval, floored at 0%, and is only ever stated **under randomisation**. Assignment here was `randomised`.
- A missing measurement is counted as missing, never as zero. Each cell's `n` is the number of tasks that actually recorded that metric.
- Sources listed as unavailable are reported, not silently skipped — an empty comparison always says which telemetry was missing.

