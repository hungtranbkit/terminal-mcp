# Agent Runtime (TMCP-AGENT-RUNTIME-001 Phase B)

## The thesis

**An Agent is a durable identity. A Session is a disposable runtime.**

Before Phase B the fleet had only the second one. An agent existed as whatever
session happened to be running its work, so replacing the session destroyed
the identity, and "which agent owns this task" had no answer at all.

| | Agent | Session |
| --- | --- | --- |
| Lifetime | outlives every runtime | created, replaced, killed freely |
| Owns | tasks, skills, run history | nothing durable |
| Identity | `agents.id`, stable forever | `execution_session`, moves at will |

`queue_tasks.agent_id` is written once at creation and **never** changes.
`execution_session` is released and re-bound as often as the fleet requires.

## Why this is a store when `worker_registry` deliberately is not

`worker_registry.py` opens by explaining that a "worker" is a `(node_id,
session)` pair whose every attribute already lives in three other places, so a
`workers` table would duplicate all three and drift. That reasoning is correct
*and it is about sessions*. An Agent's identity, skills and history have no
other home, which is exactly when a new store is justified.

## Modules

| Module | Owns |
| --- | --- |
| `agent_registry.py` | `agents`, `skills`, `agent_skills`, `agent_runs` + migrations |
| `skill_packages.py` | reading `skills/<id>/SKILL.md` safely |
| `agent_service.py` | CRUD, capacity, `agent_start` over the router |

## There is no second scheduler

`agent_start` turns the agent's durable identity into the metadata
`TaskProfile` already reads (project, repo, workspace, runtime), resolves its
skills to pinned `id@version` labels, and hands everything to
`TaskRouter.route_start`. Every eligibility rule, the atomic claim, the bounded
dispatch, the truthful receipt and Queue Rescue come from Phase A unchanged.

Building placement logic here would have meant two schedulers disagreeing
within a week about which sessions are eligible — one with the
deleted-worktree check and one without.

## Skills are versioned

A skill is a prompt. Its behaviour changes when the text changes, so "the agent
ran the review skill" only means something if the version is recorded.

* Re-registering **identical** content under a version is a no-op.
* Re-registering **different** content under the same version is an error —
  silently redefining a version invalidates every evidence trail citing it.
* A binding may **pin** a version or float to `latest`. Resolution happens at
  start time, so the run records what was actually loaded.
* **BASE** skills load for every task; **TASK** skills are available but
  applied only when a task asks, so an agent's default prompt does not grow
  without bound.

## Filesystem loading: three independent rules

Reading `skills/<id>/SKILL.md` puts a caller-supplied identifier into a path
join — the classic traversal sink.

1. **The id is a slug.** Lowercase letters, digits, `-` and `_` only. `..`,
   `/`, `\`, NUL and absolute paths are rejected before a path is built.
2. **Containment is re-checked after resolution.** Root and candidate are both
   fully resolved, symlinks included. Checking *before* resolution is the
   standard bug: a symlink inside an approved root pointing at `/etc` passes a
   textual prefix test and fails this one.
3. **Roots come from configuration**, never from the caller, so a request
   cannot widen its own search path.

Reads are bounded (`MAX_SKILL_BYTES`, 256 KiB) and an oversized file is refused
by name — never truncated, because half a prompt is a different prompt. Nothing
is auto-registered; `discover` reports, registration is always explicit.

## `max_sessions` (default 1)

An agent is one identity with one train of thought. Two concurrent runtimes is
how two sessions end up editing one worktree — the collision the coordinator
gate already refuses at dispatch time, but only *after* a task has been bound,
ticked and paused.

The check is installed as a **router hook**, not an `if` in `agent_start`,
because Queue Rescue re-routes tasks nobody re-submits. A limit that only ran
on the submission path would be bypassed by the reconcile ten seconds later.

A task whose owner is at capacity, or disabled, is durably queued as
`WAITING_RUNTIME` with that stated as the reason.

## Surfaces

| Surface | Calls |
| --- | --- |
| MCP | `terminal_agent_start`, `terminal_list_agents`, `terminal_get_agent`, `terminal_create_agent`, `terminal_update_agent`, `terminal_list_skills`, `terminal_get_skill`, `terminal_register_skill`, `terminal_discover_skills`, `terminal_bind_agent_skill`, `terminal_unbind_agent_skill` |
| Compact `turn` | `agent_start` (`run_agent`), `list_agents` (`agents`), `get_agent` (`agent`), `create_agent`, `update_agent`, `list_skills` (`skills`), `register_skill` (`skill`), `bind_agent_skill` (`bind`, `bind_skill`), `cleanup_candidates` (`cleanup`, `stale_sessions`) |
| Dashboard | `/dashboard/agents` — project, state, current task, queue depth, runtime session + node, model, context, skills, recent runs and failures |

`turn` reads the surface's two conventional positionals: `target` is the agent
(or skill) id, `text` is the prompt. `bind_agent_skill` is the one action
naming two ids, so there `target` is the agent and `text` is the skill.

## Configuration

```yaml
agents:
  enabled: true
  skill_roots:                       # empty = repo skills/ + ~/.claude/skills
    - /home/kimex/workspace/terminal-mcp/skills
```

## Legacy behaviour is unchanged

`start(target=...)` is still hard affinity and still returns `TARGET_REQUIRED`
without a target. `route_start` is unchanged. A task with no `agent_id` is
never touched by the capacity rule — Phase A behaviour is identical for
everything that predates agents.
