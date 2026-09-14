# Before/After Efficiency Benchmark

- Generated: `2026-09-14T08:53:07+00:00` (harness v1.1.0)
- Verdict: **INSUFFICIENT_DATA**
- Cohort assignment: **observational**
- Price table: `2026-06-24`, cache-TTL policy `split_required`
- Tasks loaded from all sources: 0
- Reporting floor: 10 matched, measured tasks per arm, per risk class
- Stratification key: **joined at report time** — no per-task snapshot of the key exists. The telemetry store records no decision_budget, profile or risk class per task, so the key is recomputed rather than read, and a recomputed key can in principle be recomputed after seeing the outcome.

> **INSUFFICIENT_DATA — no efficiency claim is made anywhere in this report.** No risk class reached 10 matched tasks in both arms. Whatever distributions exist are shown below so the shape of the data is visible; none of them is a result. This report becomes a comparison automatically once the threshold is met — nothing needs to be re-enabled.

## Sources

| Source | Path | Available | Tasks | Detail |
| --- | --- | --- | ---: | --- |
| `queue.db` | `/home/dell/.local/state/terminal-mcp/queue.db` | yes | 0 | table present but empty -- no tasks have been queued on this host yet |
| `queue.db` | `/home/dell/.local/state/tmcp-fed/terminal-mcp/queue.db` | yes | 0 | table present but empty -- no tasks have been queued on this host yet |
| `work.db (state/terminal-mcp)` | `/home/dell/.local/state/terminal-mcp/work.db` | no | 0 | file does not exist -- this telemetry is not produced on this host yet |
| `ai_usage.db (state/terminal-mcp)` | `/home/dell/.local/state/terminal-mcp/ai_usage.db` | no | 0 | file does not exist -- this telemetry is not produced on this host yet |
| `work.db (tmcp-fed/terminal-mcp)` | `/home/dell/.local/state/tmcp-fed/terminal-mcp/work.db` | no | 0 | file does not exist -- this telemetry is not produced on this host yet |
| `ai_usage.db (tmcp-fed/terminal-mcp)` | `/home/dell/.local/state/tmcp-fed/terminal-mcp/ai_usage.db` | no | 0 | file does not exist -- this telemetry is not produced on this host yet |
| `work_telemetry.db` | `/home/dell/.local/state/terminal-mcp/work_telemetry.db` | no | 0 | file does not exist -- no worker has emitted telemetry on this host yet |
| `work_telemetry.db` | `/home/dell/.local/state/tmcp-fed/terminal-mcp/work_telemetry.db` | no | 0 | file does not exist -- no worker has emitted telemetry on this host yet |

## Risk classes

No tasks were loaded, so there is nothing to stratify.

## How to read this

- **Risk classes are never pooled.** There is no overall number on purpose: the effect can run in opposite directions across HIGH_RISK / STANDARD / FAST_FIX, and pooling cancels a real result into a null.
- Figures are **medians** with a p25–p75 spread, over tasks matched pairwise within a risk class. A single extreme task cannot move them; outliers are counted, not deleted.
- **Cost (base-input-equivalents)** is the metric decisions are made on: every token converted at its real billing ratio (cache-write 1.25x/2x by TTL, cache-read ~0.1x, output 5x). `primary_cost_tokens` is shown because the brief named it, but it is a raw diagnostic — it sums tokens of different unit cost and omits output entirely.
- **worker_turn_count** is the primary statistical metric (a count has far more power than a binary at these sizes); first-pass success is the headline outcome but needs ~100 matched tasks per arm before any direction is claimed.
- A saving is the **lesser** of the point estimate and the lower bound of a 90% seeded bootstrap interval, floored at 0%, and is only ever stated **under randomisation**. Assignment here was `observational` — so every figure is descriptive, whatever the sample size.
- A missing measurement is counted as missing, never as zero. Each cell's `n` is the number of tasks that actually recorded that metric.
- Sources listed as unavailable are reported, not silently skipped — an empty comparison always says which telemetry was missing.

