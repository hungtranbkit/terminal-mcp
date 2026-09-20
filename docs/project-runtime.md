# Project Runtime (TMCP-PROJECT-BOOTSTRAP-001)

## The hierarchy

```
Project  ->  Agents  ->  Skills  ->  Tasks  ->  Session
durable      durable     versioned   durable    disposable
```

The first two are identities a person thinks in. The last is runtime detail,
and the dashboard treats it that way: a session appears as *where the work is
currently running*, never as the thing you operate.

## A project is a pipeline, not a standing organisation

```
INTAKE -> ANALYSIS -> PLANNING -> BUILD -> REVIEW -> TEST -> RELEASE -> OPERATE
```

Bootstrap materialises **only the first phase's team**. Later agents are
created when the project reaches the phase that needs them; agents whose phase
has passed go **DORMANT**, keeping their identity, skills and history.

Dormant matters because phases go backwards. A failed review returns to BUILD
and the original build agents *wake* — they are not recreated empty.

| State | Meaning |
| --- | --- |
| `ACTIVE` | in the current phase, may be started |
| `IDLE` | active but holding nothing (derived, never stored) |
| `DORMANT` | phase has passed; wakes if the project returns |
| `RETIRED` | done for this project's lifetime |
| `DISABLED` | an operator switched it off, independent of phase |

## The Project Manager

Every project gets exactly **one** PM, created at bootstrap unless
`policy.disable_pm` is set. It is `cross_phase=true`: no transition ever makes
it dormant, because somebody has to hold the backlog, the dependencies and the
handoffs across a team that keeps changing shape.

The PM **coordinates, it does not implement**. It is ranked last for
implementation work (`-50` when the task's capabilities do not overlap its
own), and it can never supply an approval.

## Separation of duties

Only an **assurance role** (`reviewer`, `qa`, `release`) may satisfy a
`REVIEW`, `TEST` or `RELEASE` gate.

* A **build role** may not — it wrote the thing.
* The **PM** may not — it planned the work and asked for it to be finished, so
  its approval measures its own plan.

`policy.allow_self_approval` waives the *dev* rule for a team too small to have
anyone else. It never promotes the PM into an approver, at any project size.
The collapse tables raise rather than merge across this boundary, so a future
edit cannot quietly break it.

## Right-sizing and role collapse

| Band | Agents | Detected by |
| --- | --- | --- |
| small | 2–3 | ≤2 modules |
| medium | 3–5 | 3–4 modules |
| large | 5–8 | ≥5 modules |

Modules come from the description (EN + VI patterns) and from a bounded,
read-only scan of the repository (`package.json` → node/frontend,
`pyproject.toml` → python/backend, `.github/workflows` → deploy, …), each with
the evidence recorded in `profile.signals`.

A small project collapses `analyst`/`architect`/`planner`/every build
specialist into `core`, and `reviewer`/`release` into `qa`. It still gets
**PM + core + an independent QA** — that is the floor.

## Skill injection

Bound skills are resolved to pinned `id@version` labels **at task creation**,
so a run's evidence says what was actually loaded, not what the skill says
today. At dispatch the bodies are prepended to the prompt:

* **before** the prompt — standing instructions must be read before the request
  they qualify;
* bounded by count (`MAX_SKILLS_INJECTED = 4`) and size
  (`MAX_SKILL_PREAMBLE_CHARS = 8000`), with an explicit truncation marker;
* a task with **no agent** gets no preamble, so legacy dispatch text is
  byte-identical.

Built-in skill templates are **prompt text, never code**. A proposed skill with
no template and no registry entry comes back `unresolved_skills` rather than
being invented.

## Filesystem skills stay rooted

`skills/<id>/SKILL.md` loading is unchanged from Phase B: strict slug id,
containment re-checked *after* symlink resolution, roots from config only,
bounded read.

## `project_start`

```
Project -> PM orchestration -> specialist Agent -> Router -> Session
```

Each arrow is an existing component; this adds only the agent choice. When
nobody fits you get **`NEEDS_TEAM_REVIEW`** with the per-candidate reasons —
the task is still durably created, and no session is picked at random.

Scoring: `+30` per capability overlap, `+40` owns the current phase, `+20`
idle, `−10` per in-flight task (capped), `−50` PM on implementation work. Hard
rejects: not startable, wrong project, and the approval rule above.

## Surfaces

**`terminal_turn` actions** (aliases in brackets): `project_plan` (`plan`),
`project_bootstrap` (`bootstrap`, `new_project`), `project_list` (`projects`),
`project_get` (`project`), `project_update`, `project_archive`,
`project_phase_status` (`phase`), `project_advance` (`advance`),
`project_reconcile_team` (`reconcile`), `project_start`.

`target` fills each action's first required argument and `text` its second, so
`turn(action="bootstrap", target="my-app", text="<description>")` works.

**MCP tools:** the same set, prefixed `terminal_project_*`. Note
`terminal_project_registry_list` is deliberately distinct from the
pre-existing `terminal_project_list`, which auto-detects git projects from
sessions — the two answer different questions and collapsing them would make
"project" mean two things.

**Dashboard:** `/dashboard/projects` — overview cards, a four-step New Project
wizard (info → analyze → suggested team with per-agent reasons and
enable/disable → create), and a detail view with the phase pipeline, agent
team, upcoming phases, and phase history with handoffs. Global Tasks cards
gained a project chip beside the existing agent and skill chips.

Project detail also carries a **Runtime health** panel — the two admin
actions a stalled project needs, on the page where you notice it is stalled
rather than in a different client:

* **Stalled runtimes**, as a dry run. Opening the panel moves nobody's work.
  Each row names the session the task is bound to, the agent that owns it,
  and why the binding is no longer worth holding. One button recovers them.
* **Stale sessions**, with the report's own evidence beside each one and a
  per-session cleanup button whose precondition is re-derived from a fresh
  fleet read before anything is deleted. Sessions that are busy, waiting for
  input, holding tasks or protected are excluded by construction and are
  counted, not hidden.

Every page under `/dashboard` now carries the same global navigation bar —
see `docs/dashboard-navigation.md`. Project detail adds a breadcrumb
(Projects / `<name>`) rather than a back button.

## Node capability

`available_agent_types` calls `shutil.which` per configured launcher, so a node
advertises `claude`/`codex` only if the binary is on the **service's** PATH --
systemd's minimal default, not the login shell's. On hp-linux both CLIs were
installed under `~/.local/bin` and the node still reported shell-only.
Detection was right; the environment was wrong. The shipped unit examples now
set `Environment=PATH=%h/.local/bin:...`.

Re-verified live on hp-linux (2026-09-20): `claude 2.1.278` and
`codex-cli 0.154.0` both resolve on the running service's own PATH
(`/home/kimex/.local/bin` is first on it), and the node advertises
`agent_types: ["shell", "claude", "codex"]`. The capability is real.

`agent_type_evidence` now answers the follow-up question that
`agent_types: ["shell"]` could not: **why not**. Per agent type it reports
`available`, the configured `launcher`, and a `detail` that distinguishes
"no launcher is configured", "the launcher does not resolve on this node's
effective PATH" and "resolved to `<absolute path>`" -- the last of which is
the difference between believing a capability and being able to check it.
`terminal_node_capabilities` carries it for the local node (another node's
PATH is not readable from here, and inventing an answer for it is exactly
what this module exists not to do) alongside `router_may_spawn`, which is
`router.spawn_enabled` AND the node having any runtime: "the node has claude"
and "the router will create a claude session here" are different facts and
conflating them is how the second gets assumed.

## PM recovery

`project_recover` (MCP `terminal_project_recover`, `turn` aliases `recover` /
`pm_recover`, and the Runtime health panel on project detail) is the PM
acting rather than reporting: a task whose execution session or node has gone
away has its **runtime binding alone** released and is handed back to the
Agent/Session router, with project, agent, pinned skills and evidence intact
and every decision appended to the task's durable handoff history. See
`docs/task-router.md` for the rules it will not bend -- in particular that
live work is never moved on a timer and an unreadable fleet is never treated
as an empty one.
