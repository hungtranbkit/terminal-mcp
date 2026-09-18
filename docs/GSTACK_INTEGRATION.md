# gstack integration

Audited against **github.com/garrytan/gstack**, `VERSION` **1.84.1.0**,
commit `71f6048e8ada25180e61438abc1d98cb151fe9a7` (2026-09-09), MIT licence,
repo pushed 2026-09-11. Every skill name below was read from the repository
tree, not from its prose: the README still describes "23 opinionated tools"
while the tree carries **53 directories with a `SKILL.md`**.

## What gstack is, and what it is not

gstack is a pack of **Claude Code / Codex skills** — `SKILL.md` files with
`name` / `version` / `description` / `allowed-tools` / `triggers`
frontmatter — that a coding agent loads to run a review, a QA pass, a ship
checklist. It is prompt-level work done *inside* a worker session.

It is not an orchestrator, and this integration does not let it become one.

## Architecture

```
Terminal MCP   nodes, sessions, tmux/ConPTY, permissions, recovery, federation
ProjectFlow    backlog, queue, worktrees, coordinator gate, integration
               review, verification, release            <- decides DONE
gstack         per-stage skills a worker runs           <- produces evidence
```

The boundary, stated once: **a skill reports; ProjectFlow decides.**
`verify_queue` remains the only thing that can move a task to DONE. A skill
that says "looks good" is a data point in the evidence trail, never a gate.
`terminal_mcp/skill_provider.py` enforces this shape — its evidence records
carry `decides_done: False` and there is no code path that sets it true.

## Mapping

### USE AS-IS — worker-side skills, called per stage

| Stage | gstack skill | Why |
| --- | --- | --- |
| `discovery` | `/office-hours` | Open-ended problem framing; ProjectFlow has no equivalent. |
| `plan_review` | `/plan-ceo-review` | Product/CEO-mode read of a plan. Nothing here does this. |
| `architecture_review` | `/plan-eng-review` | Eng-manager-mode read. Complements the DoR gate, does not replace it. |
| `pre_merge_review` | `/review` | Judgement-level diff reading, *after* `integration_reviewer`'s mechanical checks. |
| `verification` | `/qa` | Exploratory, human-like QA. Deterministic acceptance stays with `verify_queue`. |
| `release` | `/ship` | Release checklist. The deploy decision stays with ProjectFlow. |
| `retro` | `/retro` | Periodic retrospective; feeds the learning path below. |

### ALREADY HAVE — do not duplicate

| ProjectFlow | Overlapping gstack surface |
| --- | --- |
| `coordinator.CoordinatorGate` | pre-dispatch safety review |
| `integration_reviewer` | diff / API / schema / migration / security pre-merge checks |
| `verify_queue` | verification as claimable, capability-routed work |
| `pm_router` | deterministic skill-based worker routing |
| `backlog_service` + `queue_engine` | intent vs execution split |
| `git_worktree` | branch/worktree lifecycle |
| `session_knowledge` | transcript capture and search |

These are deliberately deterministic — *"no ML/LLM call to invent scope that
doesn't already exist"* (`planner_service`). gstack's equivalents are
judgement-level and run **beside** them, never instead of them.

### ADAPT

* **`/learn`, `/retro` → learning trail.** Correction to the original
  brief: this repo has **no Experience Store and no Failure Fingerprint
  subsystem**. What exists is `session_knowledge.py` (capture + search) and
  `dor_gate.py`. So retro/learn output is attached as task evidence and
  indexed through session knowledge. Building a separate knowledge DB is
  explicitly out of scope until something needs one.
* **`/qa` vs Agent-QA.** Use gstack's QA for exploratory, human-like
  passes. Deterministic acceptance criteria and their artefacts stay in
  ProjectFlow, which is what a release is allowed to depend on.

### SKIP

* `gstack-verify-gate` — a Claude Code **Stop hook**. Hooks bypass the
  permission system. ProjectFlow already blocks on its own verification.
* `setup-gbrain` / Supabase provisioning, `gstack-analytics`,
  `gstack-egress` receipts — external state and egress this fleet does not
  need and would have to secure separately.
* `/pair-agent`, the bundled browser, Aside integration — overlapping with
  the existing browser/QA path; revisit only if a real gap shows up.
* Auto-install of anything. See Security.

## Host support

| Host | Install location |
| --- | --- |
| Claude Code | `~/.claude/skills/gstack/<skill>/SKILL.md` |
| OpenAI Codex CLI | `${CODEX_HOME:-~/.codex}/skills/gstack-<skill>/SKILL.md` |

`GstackProvider` reads both layouts and resolves **per host**: installed for
Claude does not mean available to Codex.

## Status model

| Status | Meaning | Effect |
| --- | --- | --- |
| `READY` | installed, at or above the pinned version | stage resolves to a skill |
| `MISSING` | no install, or no `SKILL.md` under it | stage falls back |
| `DRIFT` | installed but older than the project's pin | stage falls back |
| `UNSUPPORTED` | host has no known gstack layout | stage falls back |

Falling back is a normal outcome and is recorded in the task's evidence with
its reason, so a stage that ran without gstack never looks like one that
never ran.

## Policy

`WorkflowPolicy` is per project and **nothing is enabled by default** — a
pipeline every project must run is a tax, not an integration. A project opts
individual stages in:

```python
WorkflowPolicy(project="pilot", enabled_stages=frozenset({"pre_merge_review"}))
```

## Install / update

Operator-run, never automatic:

```bash
git clone --single-branch --depth 1 https://github.com/garrytan/gstack.git \
    ~/.claude/skills/gstack
cd ~/.claude/skills/gstack && git checkout 71f6048e8ada25180e61438abc1d98cb151fe9a7
./setup                       # add --host codex for the Codex CLI
```

Pin the commit. Update by moving the pin deliberately and re-running the
node doctor, which reports `DRIFT` until the project's `minimum_version` is
raised to match.

Prerequisites gstack states: Claude Code, Git, **Bun v1.0+** (Node.js on
Windows). None of these are installed on m910 today.

## Security

* This integration **reads** the filesystem to see what is installed. It
  never installs, upgrades, registers a hook, or executes a skill.
* gstack's own `./setup` writes to `~/.claude/skills/gstack` and, per its
  README, does **not** register the `gstack-verify-gate` Stop hook for you.
  Keep it that way.
* Pin a commit, not a branch. `--depth 1` on a moving branch is not a pin.
* No token or credential is passed to a skill by this layer.

## Fallback contract

Every caller reads `Resolution.skill` and, when it is `None`, runs the stage
exactly as it did before this module existed. There is no code path in
which an absent, drifted or unsupported gstack can block ProjectFlow.

## Rollout

1. **Pilot one small project, one stage** — `pre_merge_review` is the
   cheapest to judge, since `integration_reviewer` already runs beside it.
2. Compare its findings against what the mechanical gate already caught.
3. Only after a pilot PASS, propose which stages are worth enabling more
   widely. No global default.

## Rollback

Disable per project by clearing `enabled_stages`; the provider is then never
consulted. Remove entirely with gstack's own uninstall, or by deleting
`~/.claude/skills/gstack`. Nothing in ProjectFlow depends on either.

## Current state on this fleet

| Item | State |
| --- | --- |
| gstack installed | **No** — `~/.claude/skills/gstack` absent |
| Bun (prerequisite) | **No** |
| `~/.codex` | absent |
| Stages enabled | none |
| Integration level | provider + policy + capability detection + evidence shape, with tests; no skill has been executed |
