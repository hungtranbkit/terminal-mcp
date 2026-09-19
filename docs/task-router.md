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
| `stale_sessions.py` | A cleanup **report**. No delete path exists, not even behind a flag. |

## Hard rejects vs. scoring

Two stages, different in kind. A **hard reject** is a safety fact — no amount
of affinity buys past it, and the first disqualifying fact short-circuits the
rest. **Scoring** is preference among sessions that are all genuinely usable:
fixed arithmetic, no randomness, so a decision is reproducible three days later.

Rejects: `NODE_OFFLINE`, `NODE_DRAINING`, `NODE_OVERLOADED`,
`SESSION_NOT_ACTIVE`, `INPUT_NOT_PERMITTED`, `STALE_IDENTITY`,
`WORKTREE_MISSING`, `WAITING_INPUT`, `SESSION_BUSY`, `SESSION_CLAIMED`,
`RUNTIME_MISMATCH`, `REPO_MISMATCH`, `MISSING_REQUIRED_SKILL`,
`AGENT_BOUND_ELSEWHERE`.

Scores: `+50` agent binding, `+30` same project, `+30` same repo, `+20`
required skills, `+20` IDLE, `+10` branch affinity, `+10` healthy node, `+10`
context `<70%`, `-20` context `>=85%`, `-40` dirty work on a different branch,
`-50` required repo unknown, `-5` per already-queued task. A candidate must
clear `MIN_ELIGIBLE_SCORE` (0) to be dispatched to.

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
  default_runtime: shell
  rescue_interval_seconds: 10
  rescue_batch_size: 20
```

`enabled`/`rescue_enabled` default **on**, unlike every other autonomous
switch in this project. Routing creates nothing — it only decides where work a
caller already enqueued should run — so shipping it off would mean shipping
the bug still armed. `spawn_enabled` is off by default because creating a
session is the one routing action that changes the fleet rather than using it.

## Surfaces

| Surface | Call |
| --- | --- |
| MCP | `terminal_route_start`, `terminal_task_route`, `terminal_queue_rescue_once`, `terminal_session_cleanup_candidates` |
| Compact `turn` | `action="route_start"` (aliases: `start_auto`, `auto`, `route`) |
| Dashboard | Global Tasks cards show execution session/node, routing state, the one-line reason, and the top rejected candidates |

An unexplained `QUEUED` is no longer representable: every path that leaves a
task queued writes `WAITING_RUNTIME` plus the per-candidate rejection reasons.
