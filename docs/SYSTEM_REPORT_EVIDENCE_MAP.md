# SYSTEM REPORT / CAPACITY — what already exists, and what this feature adds

Written before any code, at `main@29c062c`. Every row was read from the file
named, not assumed.

## Reuse, not rebuild

| Need | Existing | Why it fits |
|---|---|---|
| Is a session actually working? | `work_service._occupancy(row, status)` | Returns `(occupied, evidence)` from two independent signals — a foreground command that is not a plain shell, and an active session state. Hands back the evidence so a caller never takes the boolean on trust. This is the exact primitive the "detached != idle" rule needs. |
| BUSY / RUNNING_MANUAL / IDLE / OFFLINE | `work_service.workers()` lines 482-509 | Already distinguishes *the queue owns this* from *a human is working here* via `busy_untracked` + `occupancy`. |
| OFFLINE / UNAVAILABLE and why | `work_eligibility.evaluate()` | Returns a reason code, and already states the rule this feature depends on: *"Unknown evidence is NOT treated as permission."* |
| Node inventory, status, capacity | `node_registry.NodeRegistry.list()`, `node_models.Node` | Carries `status`, `draining`, `capacity_status`, `overload_reasons`, `platform`, `agent_types`, `max_sessions`, `tmux_session_count`, heartbeat age. |
| Placement / compatibility rules | `scheduler.py` | Pure, deterministic scoring with an explicit `reason` and an `excluded: (node_id, why)` list. Already gates on `platform`, `agent_types`, `capacity_status`, `draining`. `suggested_moves[]` reuses this rather than inventing a second notion of eligibility. |
| Queue counts and lane state | `queue_store.lane_status()`, `ALL_STATUSES`, `_ACTIVE_STATUSES` | `_ACTIVE_STATUSES` already includes `PAUSED`/`BLOCKED`/`WAITING_SESSION`, so "occupying a lane" is already a settled concept. |
| Project / work association | `work_store.work_runs` (`project_id`, `lane`), `queue_tasks.project_id`, bindings, session `cwd`/worktree | Strongest-first precedence; session-name matching is fallback only. |
| Progress with evidence | `work_service` progress (`total_weight`/`done_weight`), `work_store.done_criteria`, `queue_tasks.requirement_contract`/`evidence_matrix` | Weighted task completion and the requirement contract's covered-vs-required ratio are both countable. Everything else is `null`. |
| Not inventing numbers | `work_telemetry.py` | Its provenance model — `EXACT` / `ESTIMATED` / `UNAVAILABLE`, aggregates refusing to mix silently — is the discipline this report needs, stated in that module as *"a number we do not have is reported as not having it."* |
| Persistence with migrations | `work_telemetry.py`, `queue_store.py` via `schema.Migration` / `apply_migrations` | SQLite, WAL, `PRAGMA user_version`, additive-only. |
| Background 15-minute loop | `queue_loop.QueueLoop` (`start` / `stop` / `run_one_cycle`) | A plain daemon thread, the shape this codebase already uses for `SupervisorLoop` / `MaintenanceLoop`, because `server_http.py` has no asyncio lifespan hook to attach a coroutine to. |
| Temp files | `ephemeral_state.ephemeral_state_dir` | Already merged. No new `mkdtemp`; `test_no_module_still_calls_mkdtemp_directly` will fail the build if one appears. |

## The production SHA trap, verified

Requirement H is real and measured on this host:

```
# base unit
/home/mesflow/.config/systemd/user/terminal-mcp-http.service
    WorkingDirectory=/home/mesflow/terminal-mcp

# drop-in, last wins
…/terminal-mcp-http.service.d/40-pin-production-commit.conf
    WorkingDirectory=/home/mesflow/terminal-mcp-prod
```

Reading the base unit gives the **wrong** directory. The report must read the
*effective* configuration (`systemctl --user show`, or the last-wins merge of
`systemctl --user cat`) and resolve the SHA from whatever directory that
actually names. `/home/mesflow/terminal-mcp-prod` is a detached worktree; at
the time of writing it happens to be `29c062c`, the same as `main`, which is
exactly the coincidence that makes assuming `main == production` look correct
until it isn't. When the effective directory cannot be resolved, `prod_sha` is
`null` with an evidence note — never a guess.

## Gaps this feature has to add

1. **`workers()` only covers `-work` sessions**, by deliberate design: an
   ordinary session must never appear in a list labelled "Workers". The system
   report needs *every* session, so it reuses `_occupancy` directly rather
   than widening `workers()` and changing what that surface means.
2. **No report/history store exists.** `work_telemetry` is per-task cost, not
   whole-system snapshots. New store, same conventions.
3. **No 15-minute bucket scheduler.** Loops exist; a bucketed, idempotent,
   restart-safe one does not.
4. **No `STALE` / `UNKNOWN` session state.** `workers()` has four states and
   no way to say *the evidence is ambiguous*. The report adds them rather than
   forcing ambiguity into `IDLE`, which is the failure this whole feature
   exists to avoid.

## Decisions taken without asking

- **Utilization denominator = eligible capacity**, not visible sessions.
  `busy / (busy + idle)` over sessions the runtime could actually dispatch to.
  `OFFLINE` and `UNAVAILABLE` are excluded from the denominator and reported
  separately, because a node that is down is not a node being wasted. The
  denominator is carried in the payload so a reader never has to infer it.
- **`RUNNING_MANUAL` counts as busy** for utilization. The machine is occupied;
  whether this runtime scheduled the work does not change that.
- **`STALE` is not `IDLE`.** A session whose evidence is older than the
  staleness threshold is reported `STALE` with its age, and excluded from the
  utilization denominator.
- **No LLM anywhere in the 15-minute path.** Every insight is a stated rule
  with a named reason, evaluated deterministically.
