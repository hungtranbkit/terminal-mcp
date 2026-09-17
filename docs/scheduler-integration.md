# Scheduler fix — integration plan (blg_8d65afc1b38b)

Integration lane. Phase 1 (watch reconciliation + the pure state/refill
model) is done and committed. Phase 2 (worker discovery + actual dispatch)
is **hp3-work's lane** and is not duplicated here.

## What phase 1 is

**Branch `fix/scheduler-refill` @ `02efdf5`**, worktree `/home/mesflow/wt-scheduler`.

| file | |
|---|---|
| `terminal_mcp/scheduler_health.py` | new — worker state model, `classify_worker`, `may_reclaim`, `watch_recovery_action`, `evaluate_refill`, `utilization_snapshot` |
| `terminal_mcp/supervisor.py` | +138 — `reconcile_watches()`, `_target_alive()`, store `reenable_watch`/`reset_iterations`/`note_reconcile_attempt`, `reconcile_attempts` column, `watch_reconciled` event, enriched `status()` |
| `tests/test_scheduler_health.py` | 35 tests |
| `tests/test_supervisor_reconcile.py` | 9 tests, real tmux |

Integration artifacts added on top (this lane, no implementation):
`tests/test_scheduler_integration.py`, `scripts/verify-scheduler-integration.sh`,
this document.

## Conflict surface — measured, not guessed

`git diff --name-only main...<branch>` across every live branch:

**`fix/scheduler-refill` is the only branch touching `terminal_mcp/supervisor.py`.**
Phase 1 therefore has **zero conflict** with anything currently in flight.

The real risk is phase 2, and it is concentrated in three places:

| file | who else wants it | likelihood | note |
|---|---|---|---|
| `terminal_mcp/queue_engine.py` | **`feat/prompt-delivery-gate`** rewrites `_dispatch` (L336–406) and adds `_delivery_verdict` | **HIGH** | phase 2's dispatch lands in the same function |
| `terminal_mcp/queue_store.py` | `feat/prompt-delivery-gate` (+42) | medium | schema additions on both sides |
| `terminal_mcp/scheduler_health.py` | phase 2 extends it | medium | additive if phase 2 *adds* functions rather than editing `evaluate_refill` |
| `terminal_mcp/supervisor.py` | phase 2 auto-creates watches | low–medium | `reconcile_watches` and a discovery pass are adjacent, not overlapping |

`feat/system-report` adds `terminal_mcp/system_report.py` ("classify a
session by what it is doing") — a **semantic** overlap with
`classify_worker`, not a file one. Worth a look before phase 2 duplicates
a third session classifier, but it blocks nothing.

## Merge order

```
1. main                      (baseline, 29c062c)
2. fix/scheduler-refill      phase 1 — no conflicts, merge first
3. feat/prompt-delivery-gate BEFORE phase 2 — it owns queue_engine._dispatch
                             and the SUBMIT_CONFIRMED verdict phase 2 must call
4. hp3-work phase 2          rebase onto 2+3, resolve in _dispatch only
```

Putting **3 before 4** is the one ordering decision that matters. If phase 2
lands first it will write its own delivery-confirmation logic into
`_dispatch`, and the delivery gate will then have to be merged *through*
it — two implementations of "was the prompt accepted?" in the same
function. The gate already exposes `DELIVERED / NOT_ACCEPTED / UNCERTAIN /
REFUSED` plus activation and acceptance reason codes; phase 2 should call
that rather than re-derive it.

If phase 2 is already written against today's `_dispatch`, rebase it onto
the gate and map its own confirmation check onto `DELIVERED`.

## Contract phase 2 must satisfy

`tests/test_scheduler_integration.py` encodes it. The four phase-2 symbols
it probes are a *suggested* surface, not a mandate — if hp3-work names
them differently, change the probe, not the assertion:

| symbol | what the check asserts |
|---|---|
| `discover_workers(service)` | a live worker with **no** watch gains coverage (phase 1 can only revive an existing watch) |
| `dispatch_refill(workers, ready_tasks)` | 3 idle + 3 READY ⇒ 3 dispatched in one pass |
| `claim_task(session, task_id)` | two racing claims ⇒ exactly one winner |
| `scheduler_status()` | exposes `last_dispatch_at`, `non_dispatch_reason`, `reclaimed_stalled_count` |

Before phase 2 lands these skip **naming the missing symbol**, so a skip
is a to-do list rather than a silent pass. 12 checks assert
unconditionally today, so the file is never fully inert.

## Integration checklist

- [ ] 1. Rebase `fix/scheduler-refill` onto current `main`; merge (fast-forward expected — nothing else touches `supervisor.py`)
- [ ] 2. Merge `feat/prompt-delivery-gate` **before** phase 2
- [ ] 3. Rebase phase 2 onto both; resolve conflicts in `queue_engine._dispatch` only
- [ ] 4. Point phase 2's delivery confirmation at `delivery_gate` (`DELIVERED`), do not re-derive
- [ ] 5. Update the four probe names in `test_scheduler_integration.py` if phase 2's API differs
- [ ] 6. `scripts/verify-scheduler-integration.sh` — all suites green, **zero** "phase 2 not landed" lines
- [ ] 7. **Redeploy staging 8777** onto the merged commit (see the known gap below), re-run the script
- [ ] 8. Staging evidence: `supervisor_status` shows `recoverable_disabled_count` / `intentionally_excluded_count`; a `watch_reconciled` event exists; `last_dispatch_at` advances
- [ ] 9. Before/after utilization captured on staging
- [ ] 10. Production deploy — **separate approval**, not part of this lane

## Known gap the script already catches

Staging currently runs `main` (`29c062c`), which predates phase 1, so
`supervisor_status` has no `recoverable_disabled_count` and check 3 fails
with *"staging is running an older commit"*. That is correct behaviour:
**redeploy staging before trusting a staging result.**

```bash
kill $(ss -ltnp | grep 127.0.0.1:8777 | grep -oP 'pid=\K[0-9]+')
cd ~/terminal-mcp-staging && nohup ./run-staging.sh > staging.log 2>&1 &
```

`run-staging.sh` deploys from `/home/mesflow/terminal-mcp` (i.e. `main`),
so merge first, then restart staging.

## Commands

```bash
# full verification (tests + production before-evidence + staging probes)
cd /home/mesflow/wt-scheduler && ./scripts/verify-scheduler-integration.sh

# tests only, no staging required
./scripts/verify-scheduler-integration.sh --tests-only

# just the integration gap list
.venv/bin/python -m pytest tests/test_scheduler_integration.py -q -p no:randomly -rs \
  | grep "phase 2 not landed"
```

## Result as of 02efdf5 (phase 2 not landed)

```
scheduler_health (pure model)     35 passed
supervisor reconcile (real tmux)   9 passed
integration contract              12 passed, 4 skipped
supervisor regression             84 passed
queue/lease regression            42 passed
production before-state           watch_count=4, enabled_watch_count=0, stalled_count=2
staging                           FAIL — running an older commit (expected, see above)
```
