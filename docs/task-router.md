# Task Router (TMCP-TASK-ROUTER-001)

## The failure this replaces

Durable task `f807b3fb3d31438281d2cb4c8c6aea5a` was cancelled by hand after
sitting `QUEUED` while a compatible session was `IDLE` the whole time.

Nothing errored. The architecture simply had no step that asked *"is there a
runtime that could be running this right now?"* — `session` was chosen by
whoever created the task, and if that choice was absent, wrong, or pointed at
a lane whose auto-dispatch opt-in was off, the task waited forever and no
screen could say why.

## The pipeline

```
Task  ->  TaskProfile  ->  SessionMatcher  ->  claim  ->  dispatch
          (task_profile)   (session_matcher)   (queue_store)  (queue_engine)
                                  |
                                  +-- nothing eligible -> WAITING_RUNTIME + reasons
```

| Module | Owns |
| --- | --- |
| `task_profile.py` | What the task needs. Metadata first, prompt heuristics last, **no LLM call**. |
| `session_matcher.py` | Hard rejects (safety) and the scoring table (preference). Pure functions. |
| `task_router.py` | Sequencing, claiming, dispatch, spawn, and the rescue sweep. |
| `stale_sessions.py` | A cleanup **report**, plus ONE named, re-checked delete. Nothing sweeps and nothing runs on a timer. |
| `pm_recovery.py` | The PM's recovery pass: a runtime that died, released and re-routed. |

## Hard rejects vs. scoring

Two stages, different in kind. A **hard reject** is a safety fact — no amount
of affinity buys past it, and the first disqualifying fact short-circuits the
rest. **Scoring** is preference among sessions that are all genuinely usable:
fixed arithmetic, no randomness, so a decision is reproducible three days later.

Rejects: `NODE_OFFLINE`, `NODE_DRAINING`, `NODE_OVERLOADED`,
`SESSION_NOT_ACTIVE`, `INPUT_NOT_PERMITTED`, `STALE_IDENTITY`,
`WORKTREE_MISSING`, `WAITING_INPUT`, `SESSION_BUSY`, `SESSION_CLAIMED`,
`RUNTIME_MISMATCH`, `REPO_MISMATCH`, `MISSING_REQUIRED_SKILL`,
`AGENT_BOUND_ELSEWHERE`, `SESSION_BACKLOGGED`, `RUNTIME_CANNOT_RECEIVE_DISPATCH`.

Two of those were added after the first live smoke, because the router picked
sessions it then could not start work in:

* **`SESSION_BACKLOGGED`** — a lane is serial, so a session with work already
  queued cannot start this task now however idle its pane looks. This was a
  `-5` score penalty at first, which is how a backlogged session still won and
  the task landed *behind* the existing queue.
* **`RUNTIME_CANNOT_RECEIVE_DISPATCH`** — the engine wraps every prompt in a
  multi-line completion-marker template, and a plain shell executes each line
  as it arrives, so the send is refused (`MULTILINE_SHELL_SEND_REFUSED`). Only
  `claude` and `codex` buffer a multi-line prompt.

Scores: `+50` agent binding, `+30` same project, `+30` same repo, `+20`
required skills, `+20` IDLE, `+10` branch affinity, `+10` healthy node, `+10`
context `<70%`, `-20` context `>=85%`, `-40` dirty work on a different branch,
`-50` required repo unknown. A candidate must clear `MIN_ELIGIBLE_SCORE` (0)
to be dispatched to.

### Unknown is not No

The controller runs on one host. A remote session's `cwd` names a directory on
another machine, so stat-ing it here would be confidently wrong. A worktree
that cannot be checked is `None`, and `None` never rejects. Likewise an
**unknown** repo is scored down (`-50`, we are guessing); a **known-different**
repo is rejected (we are not guessing at all).

## What routing may and may not place

`auto_dispatch_enabled` is a real safety gate, and the router does not walk
through it. A task is rescuable only when routing it overrides nobody:

* it is in the **unassigned** lane — nobody chose a session for it (this is
  where the production bug lived);
* it is **`WAITING_SESSION`** — the session it was pinned to is gone, so the
  original choice cannot be honoured however much we respect it;
* it is **router-owned** — the router placed it, so re-placing it changes
  nothing a human decided.

A task a caller deliberately put in a named lane stays there.

## Hard affinity is preserved

`turn(action="start", target=...)` and `route_start(target=...)` are **hard
affinity**: that session or a clear refusal, never a silent reroute. Omitting
the target is what invites routing. `start` without a target still returns
`TARGET_REQUIRED` — a caller that forgot one gets an error, not a surprise.

## Dispatch is driven, and reported honestly

`QueueEngine.tick` makes at most **one** transition per call, and makes none
at all when the admission governor declines, a lane is paused or a dependency
is unmet. So binding plus one tick can never produce a running task. The
router drives the same bounded sequence `turn(action="start")` uses, with the
same constants, and derives `dispatched` from the task's real status — never
from "tick did not raise".

Driving those ticks does **not** touch `auto_dispatch_enabled`. That flag
governs the background `QueueLoop`'s lane sweep and is left exactly as the
operator set it; every tick still passes the coordinator gate and the governor.

Three outcomes, three different truths:

| Outcome | Binding | Receipt |
| --- | --- | --- |
| Engine advanced it | kept | `dispatched: true` |
| Engine declined (governor, paused lane, …) | **released**, task re-matched | `WAITING_RUNTIME` + the engine's own reason |
| `dispatch_budget_seconds` ran out | **kept** — the engine is mid-flight | `budget_exhausted: true`, server keeps driving |

The last row is why the budget exists: a submission must not become a long
poll. `dispatch_budget_seconds` (default 12s) and `probe_limit` (default 3
round trips) bound the synchronous half; only the caller's wait ends, never
the work.

## Concurrency

`QueueStore.bind_task_to_session` takes SQLite's write lock (`BEGIN
IMMEDIATE`) *before* the eligibility read. Scoring is deterministic, so two
routers looking at the same fleet reach the same answer at the same moment —
a feature everywhere except here, where it would mean both claiming the one
idle session. The loser gets `SESSION_ALREADY_CLAIMED`. Re-binding a task to
the runtime it already holds is an idempotent no-op.

## Queue Rescue

Runs as a **step of the existing `QueueLoop` cycle**, never a second thread,
on its own interval (`router.rescue_interval_seconds`, default 10s) because a
tick reads one lane while a sweep lists the whole fleet. A lane reporting
`IDLE` sets a flag that makes the next cycle sweep immediately instead of
waiting out the window. Restart-safe: every input is a durable row.

## Configuration

```yaml
router:
  enabled: true              # routing decisions + route_start
  rescue_enabled: true       # the periodic reconcile
  spawn_enabled: false       # create a session when nothing is eligible
  max_spawned_sessions: 4
  default_runtime: claude
  rescue_interval_seconds: 10
  rescue_batch_size: 20
  dispatch_budget_seconds: 12   # wall-clock ceiling on the synchronous half
  probe_limit: 8                # live status probes before committing
  snapshot_budget_seconds: 4    # of that ceiling, the most the FLEET LOOK may take
  stale_snapshot_max_age_seconds: 60   # fallback view when a listing comes back empty
```

### The budget is split, and that is the point

Measured live on hp-linux (2026-09-20), before this split: `project_start`
returned after **12.5s** carrying `dispatch_ticks: 0` and
`budget_exhausted: true`. The task was bound to a session and had never been
started — the caller got `TASK_ACCEPTED`/`QUEUED` for work that looked
assigned and was not running.

The whole 12s had gone into `terminal_list_sessions`, which walked five nodes
**in series**. Repeated timings of that one call: 3.6s, 11.8s (one node timed
out), 25.4s, 12.3s, 5.2s. Individual `terminal_status` probes, by contrast,
cost 0.05–0.15s each.

Three changes, all of them about bounding rather than hurrying:

1. `ControllerService.terminal_list_sessions` and `list_nodes` fan out across
   nodes **concurrently under one wall clock**. The cost is the slowest node,
   not the sum of them, and a node that misses the budget is reported
   `status: "timeout"` in `unreachable_nodes` rather than extending the call.
2. `snapshot_budget_seconds` caps what the fleet look may spend — at that
   value **or half the dispatch budget, whichever is smaller**. The rest is
   left for the dispatch ticks that actually start the task.
3. The router's live probe uses `terminal_status_bounded` with whatever is
   left of the route budget, so `probe_limit` probes can no longer consume a
   clock they were never given.

A fresh listing that comes back empty falls back to the last snapshot, if it
is younger than `stale_snapshot_max_age_seconds`. An empty fleet and an
unreadable one have the same shape, and treating the second as the first
defers the whole queue on one slow node.

`enabled`/`rescue_enabled` default **on**, unlike every other autonomous
switch in this project. Routing creates nothing — it only decides where work a
caller already enqueued should run — so shipping it off would mean shipping
the bug still armed. `spawn_enabled` is off by default because creating a
session is the one routing action that changes the fleet rather than using it.

## Surfaces

| Surface | Call |
| --- | --- |
| MCP | `terminal_route_start`, `terminal_task_route`, `terminal_queue_rescue_once`, `terminal_session_cleanup_candidates`, `terminal_session_cleanup`, `terminal_project_recover` |
| Compact `turn` | `action="route_start"` (aliases: `start_auto`, `auto`, `route`); `action="project_recover"` (aliases: `recover`, `pm_recover`) |
| Dashboard | Global Tasks cards show execution session/node, routing state, the one-line reason, and the top rejected candidates. Project detail carries a **Runtime health** panel: stalled runtimes (dry run) with a recover action, and stale sessions with their evidence and a re-checked cleanup action. |

## PM recovery

A task bound to a session on a node that has gone offline is invisible to both
existing sweeps: `routable_tasks` skips it (its `routing_state` is `BOUND`, so
by definition it is not waiting for a runtime) and `bound_unstarted_tasks`
re-drives a lane that is no longer there. It simply stops, looking perfectly
assigned — which is how a project silently stalls with everything green.

`pm_recovery.ProjectPMRecovery` is the missing step. It releases **only** the
runtime binding and re-runs the router:

| released | preserved |
| --- | --- |
| `execution_session`, `execution_node_id`, `routing_state` | `project_id`, `agent_id`, `skill_ids`, prompt, priority, lane position, verification evidence, coordinator history |

That split is why it reuses `release_execution_binding` rather than writing
its own UPDATE: those three columns are exactly what that method touches.

Rules it will not bend:

* **Live work is never moved on a timer.** A `RUNNING`/`VERIFYING` task is
  recovered only when its runtime is demonstrably gone. A model mid-turn looks
  exactly like a stall from outside, and re-dispatching it would run the same
  work twice on two sessions.
* **A fleet read that failed is not absence evidence.** If any node was
  unreachable, the sweep reports `fleet_evidence_usable: false` and only acts
  on facts that do not depend on the listing.
* **No double dispatch.** The task is re-read immediately before releasing and
  skipped if the binding changed since the scan; the authoritative guard
  remains `bind_task_to_session`'s own `BEGIN IMMEDIATE`.
* **Every decision is durable.** `QueueStore.record_pm_decision` appends to the
  task's append-only `migration_history` and emits a `TASK_PM_DECISION` queue
  event, so a task that moved at 3am is explainable at 9am from the row.

## Stale-session cleanup

The report is unchanged and still refuses to act on its own. What it grew is
one named, single-session delete (`cleanup_session`) that **re-derives the
whole candidacy decision from a freshly refreshed fleet read** immediately
before deleting. The report an operator is looking at is seconds old at best,
so the click authorizes deleting a session that is *still* a candidate, never
one that merely was. Busy, `WAITING_INPUT`, task-holding, claimed and
protected/admin sessions are excluded by construction and cannot be removed
through it at all.

An unexplained `QUEUED` is no longer representable: every path that leaves a
task queued writes `WAITING_RUNTIME` plus the per-candidate rejection reasons.
