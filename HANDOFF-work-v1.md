# HANDOFF — feat/work-generic-v1 (worktree /home/mesflow/wt-work-v1)

Written 2026-09-14 because the shell died mid-task. Everything below is on
disk. Nothing here needs re-deriving — it needs *running*.

## Why this file exists

Every Bash invocation on m910 returns exit 1 with no output — `echo` included,
sandboxed and unsandboxed, in a subagent, and in a second Claude session
(`workspace-53`) on the same host. Filesystem tools (Read/Write/Edit) still
work, which is why this file could be written at all.

Diagnosed by `workspace-53`: a full pytest run of this repo (~2500 tests that
spawn real tmux/uvicorn/node/pwsh) was killed by the harness at 600s, its
children were never reaped, and they hold the slice's PID budget. Every
fork/exec since then fails. It had not recovered after ~1h.

**Do not read an empty result as a negative result.** `printf 'X\n'; exit 0`
returns nothing, so stdout is not coming back at all. "No orphan pytest found"
was never established — it could not be checked.

## State: what is committed

Branch `feat/work-generic-v1`, forked from `main` at `11c6e87`.

| SHA | What | Tests |
|---|---|---|
| `4347301` | `work_spec.py` — planning contract for 6 task types | 42 green |
| `12af25d` | `test_selection.py` — fast lane + FULL_VERIFY, fails closed | 16 green |
| `de65cac` | MCP: `work_spec_create/update/gate/get/list`, `work_test_selection` | 9 green |
| `a85ebbc` | Full DoD-A field set, budget on spec, policy version/hash bound at plan time | 48 green |
| `066ad6c` | `work_reuse.py` — REUSE/EXTEND/NEW over prior specs, knowledge, runbooks | 14 green |

Those test counts were observed green before the shell died.

## State: what is NOT committed

### Verified green, commit pending

- `terminal_mcp/work_planning.py` (new) — the pipeline: capture → classify →
  knowledge → similar → git delta → reuse → spec → gate. Persists
  NEEDS_REDEFINE reason/missing/count; `redefine()` resumes the same spec id.
- `tests/test_work_planning.py` (new) — **15/15 green**, run immediately
  before the shell died.

### Written, NEVER RUN — no evidence at all

- `terminal_mcp/mcp_app.py` (modified) — adds `work_plan` and
  `work_plan_redefine` tools.
- `tests/test_work_spec_mcp_tools.py` (modified) — 3 appended tests for those
  two tools.
- `terminal_mcp/planner_service.py` (modified) — **one-line change**: pass
  `request_key=child.get("request_key")` through `_apply_split` to
  `queue.create_task`. Without it a re-applied split silently doubles the DAG.
  A caller supplying no key behaves exactly as before.
- `terminal_mcp/work_decompose.py` (new) — adapter turning `WorkSpec.subtask_dag`
  into `PlannerService.propose_split` children: topological sort, cycle /
  unknown-dep / duplicate-id / missing-acceptance refused *before* any row is
  created, deterministic `request_key` per subtask for idempotent re-runs.
- `tests/test_work_decompose.py` (new) — written, unrun.
- `tests/test_dogfood_work_v1.py` (new) — the P0 dogfood, written, unrun. Plans
  a real BUG (the Work-UI occupancy defect) and a real FEATURE
  (`work_spec_export`) through the actual pipeline against this repo, using the
  repo's REAL knowledge map if it has one and asserting the degrade path if not.
  Measures only counts — how many files the handoff names versus how many are in
  the package — and asserts that no token estimate is ever published.

Treat everything in this second list as unproven. The first list is the only
part with evidence behind it.

## Exact commands to finish

Run them one at a time. Do NOT run the full suite — that is what broke the
host.

```sh
cd /home/mesflow/wt-work-v1

# 1. targeted, sequential — never combined, never -n auto
PYTHONPATH=$PWD /home/mesflow/terminal-mcp/.venv/bin/python -m pytest tests/test_work_planning.py -q
PYTHONPATH=$PWD /home/mesflow/terminal-mcp/.venv/bin/python -m pytest tests/test_work_spec_mcp_tools.py -q
PYTHONPATH=$PWD /home/mesflow/terminal-mcp/.venv/bin/python -m pytest tests/test_work_decompose.py -q
PYTHONPATH=$PWD /home/mesflow/terminal-mcp/.venv/bin/python -m pytest tests/test_planner_service.py -q   # the one-line change above
PYTHONPATH=$PWD /home/mesflow/terminal-mcp/.venv/bin/python -m pytest tests/test_dogfood_work_v1.py -q
```

The dogfood is the one that decides whether V1 is real. If it passes, a BUG and
a FEATURE both plan to READY against this repo and the handoff points a worker
at a handful of files instead of the package. Read its failures as findings
about the pipeline, not as tests to loosen.

Only if all four are green:

```sh
git add terminal_mcp/work_planning.py tests/test_work_planning.py
git -c user.name="terminal-mcp-sync" -c user.email="sync@localhost" commit -m \
"Wire the pipeline that was only ever a list of parts" -m \
"project_knowledge, context_pack, work_reuse, task_classifier, work_policy and work_spec all existed. None of them called each other. An audit found bug_spec and context_pack had no production caller at all -- reachable only from their own tests -- which is worse than absent: the capability reads as done on a roadmap while every worker still starts from an empty repository." -m \
"work_planning is the wiring, and the ORDER is the saving. capture, classify, knowledge, similar prior work, git delta, reuse, spec, gate: cheapest evidence first, each stage narrowing the next. The delta runs AFTER the knowledge map on purpose -- the map says where to look, git says what has moved under it since, so a stale claim is caught before it reaches the spec instead of after a worker has trusted it." -m \
"Every stage reports what it could NOT do alongside what it found. Without that a planner cannot tell 'this module has no known issues' from 'there is no knowledge map', and those demand opposite next steps." -m \
"The spec is saved whatever the verdict, so NEEDS_REDEFINE resumes rather than restarts: same spec id, same queue task, refusal reason and missing fields persisted. Counters are counted -- no token estimate is invented here."

git add terminal_mcp/mcp_app.py tests/test_work_spec_mcp_tools.py
git -c user.name="terminal-mcp-sync" -c user.email="sync@localhost" commit -m \
"Expose the planning pipeline to an external agent" -m \
"work_plan runs the whole pass and work_plan_redefine resumes the same spec id, so ChatGPT can plan without the worker re-deriving anything. A redefine is a resume, not a restart: the work already done stays on the spec."

git add terminal_mcp/work_decompose.py terminal_mcp/planner_service.py tests/test_work_decompose.py
git -c user.name="terminal-mcp-sync" -c user.email="sync@localhost" commit -m \
"Decompose a feature into the queue that already exists" -m \
"Not a second planner, queue or dependency mechanism. PlannerService already owns validation, parent/child linking, a real depends_on DAG and the parent-completion rule; queue_service.create_task already owns request_key idempotency and cycle validation. This is the adapter, and the translation is the part that is not trivial." -m \
"_apply_split links children by depends_on_indices -- positions in the list as it is created. A spec names dependencies by subtask id in whatever order the planner wrote them, so handing that list over unsorted produces indices that do not exist yet, which resolve to nothing: the dependency is silently DROPPED and the subtask runs with none of its prerequisites done. No error, just a DAG that quietly became a flat list. So it topologically sorts first, and refuses any DAG it cannot sort." -m \
"A cycle, an unknown dependency id, a duplicate id or a subtask with no acceptance criteria stops the whole decomposition before a single row exists. Half a DAG in the queue is worse than none: the created half runs against prerequisites that will never exist, and nothing records that the rest was meant to follow." -m \
"planner_service gains one line: request_key pass-through, so a re-applied split returns the existing rows instead of a second DAG. Callers that supply no key are unaffected."
```

## What is still missing for V1 (nothing below is started)

P0 — dogfood: one BUG and one FEATURE_NEW planned through the real pipeline on
this repo, to READY, with the evidence recorded. No token-saving number should
be published until this runs; there is no measurement yet and inventing one
would be worse than having none.

P1 — knowledge write-back after a task; telemetry counters joined to
`work_telemetry` (measured vs unavailable, never fabricated); Work UI showing
spec/reuse/planner state.

P2 — scaffold/template registry and local-model routing. Both are MISSING
entirely (`grep` finds zero references). Post-V1.

## Housekeeping owed once the shell is back

- Delete `~/.claude/projects/-home-mesflow-workspace/memory/m910-full-pytest-run-kills-the-shell.md`
  (note the `-run-`). It is a duplicate of `m910-full-pytest-kills-the-shell.md`
  written minutes apart by `workspace-53`; that session reduced it to a pointer
  but has no shell to remove it.

## Do not repeat

- Never run the full suite on this host to prove a small change. That is what
  killed the shell. `terminal_mcp/test_selection.py` (committed, `12af25d`)
  exists precisely to pick the narrow set instead.
- Never treat empty shell output here as a finding.
