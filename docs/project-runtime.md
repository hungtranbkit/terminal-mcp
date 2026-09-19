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

## Node capability

`available_agent_types` calls `shutil.which` per configured launcher, so a node
advertises `claude`/`codex` only if the binary is on the **service's** PATH —
systemd's minimal default, not the login shell's. On hp-linux both CLIs were
installed under `~/.local/bin` and the node still reported shell-only.
Detection was right; the environment was wrong. The shipped unit examples now
set `Environment=PATH=%h/.local/bin:…`.
