<!-- WORK_POLICY_VERSION: 1.0.0 -->
# Work Policy

Canonical operating rules for Work sessions. A `-work` session loads this
before executing a task, so the rules do not depend on chat history.

- **WORK_POLICY_VERSION:** 1.0.0
- **Applies to:** sessions whose name ends in `-work`, and the tasks they run.
- **Does not apply to:** ordinary terminal sessions, which behave exactly as
  they did before this file existed.

## Policy Precedence

Highest first:

1. Current code and config -- the source of truth about what the system does.
2. Explicit current user instruction.
3. This WORK_POLICY, plus any PROJECT_POLICY delta.
4. Knowledge Map.
5. Historical memory or past chat.

A user instruction that conflicts with a safety or release guard does not remove the guard: keep the guard, say plainly that it applies, and offer the safe path.

## Bug Planner to Executor Contract

The planner produces a persistent Bug Execution Spec; the worker executes it.
The handoff is the spec, never a chat transcript.

Before editing anything, the worker does a SHORT plan verification against the
current code and reports one of:

- `PLAN_CONFIRMED` -- the spec matches what the code actually does.
- `PLAN_ADJUSTED` -- mostly right; state precisely what changed and why.
- `PLAN_MISMATCH` -- the premise is wrong; stop and hand back with the reason.

After a fix lands, update DEBUG_MAP with the real root cause, so the next bug
in this module starts from evidence rather than from nothing.

## Difficulty Triage

Every spec carries `DIFFICULTY` (EASY / MEDIUM / HARD) and
`DIFFICULTY_CONFIDENCE`. Difficulty is a separate axis from execution mode:
mode is how much process a change needs, difficulty is how much is understood.

Hard signals -- auth/session, concurrency, data consistency, multi-service,
unexplained regression, infra/security -- outrank an easy-looking surface. A
misaligned button that only misaligns after re-login is an auth bug.

## Bug Spec Levels

- `L1_EXACT_FIX` -- module, files and root cause known; the fix is prescribed.
- `L2_DIRECTED_INVESTIGATION` -- area known, cause not; search boundary given.
- `L3_UNKNOWN` -- needs reproduction first; must still carry a reproduction
  path, a search boundary and a maximum scope.

The level is derived from the evidence present, never asserted.

## Spec Completeness Gate

A spec is scored before dispatch: L1 requires >= 90%, L2 >= 75%, L3 >= 40%
plus reproduction, search boundary and max scope. Below threshold the status
is `NEEDS_REDEFINE`.

## NEEDS_REDEFINE Flow

Return `MISSING` (the fields that are absent) and `QUESTIONS_FOR_PLANNER`
(specific, answerable). Do not start a wide investigation to compensate for a
thin spec -- that converts a planning gap into token spend.

## Execution Modes

- `FAST_FIX` -- localized, low-risk, visually verifiable. Preview first.
- `NORMAL` -- ordinary change with a normal test gate.
- `SAFE` -- migrations, auth, permissions, deploy paths, data integrity.
  Full gate and approval; never route these to FAST_FIX.

Exclusions are checked before fast-path signals, so a small-looking change in
a dangerous area stays SAFE.

## Soft Token Budget

Budgets are SMALL / MEDIUM / LARGE by bug class. They are guidance, not a
kill switch: **never hard-stop mid-task**. On crossing a soft limit, report
`TOKEN_BUDGET_STATUS`, say what was learned and what remains, and either
finish or escalate with a reason -- an abandoned task costs more than the
reading it saved.

## File and Search Budget

L1 should need about 5 files and 2 search rounds; L2 and L3 scale up. An L1
that blows its budget was not an L1: hand it back for a better spec rather
than quietly turning it into an open investigation.

## Knowledge Map Usage

Investigation order: knowledge map, then git delta, then exact search, then a
narrow read, and only widen on evidence.

**The map is a map. The current code and git state are the source of truth.**
Confidence is HIGH / MEDIUM / LOW, derived from whether a module's paths have
changed since it was verified -- never a fabricated percentage. A stale entry
is a lead to check, not a fact to rely on.

## Module Context Pack

A bounded briefing per module: purpose, files, entry points, test and smoke
runbooks, known issues, past bugs. It is capped on purpose, and it states
what it does not cover so a thin pack is not mistaken for a complete one.

## Similar Bug Retrieval

Before investigating, check whether this bug has been seen. A strong match is
offered as `REUSED_BUG_SPEC` with its source commit attached; every path it
names is verified against current code before editing. A weaker match is
offered as reading, not as a conclusion.

## Procedural Memory and Runbook Registry

Repeated operations -- test, build, deploy, smoke, health check -- run from
registered scripts in `scripts/agent/`. Look up the registry before doing
such an operation by hand. **Reuse existing scripts; never duplicate one.**
Read a script's body only when it fails or is stale. Scripts are idempotent,
fail fast, and print a one-line verdict. Knowledge stores the metadata and a
link, never the script body.

## Verification Output Discipline

On success, print one concise `PASS` line. Pull the full log only on `FAIL`,
and then only the relevant window. Test gates are dependency-aware: run what
the change can affect, cached by commit and path.

## Release Levels

`PREVIEW` -> `STAGING` -> `PRODUCTION` are separate and explicit. A preview
is not a deploy. Production requires the full gate and approval, and the
approval covers one release, not a standing permission.

## Developer Assist

The user is a developer. For a HARD bug the planner analyses FIRST, then asks
1-3 high-value technical questions -- each one specific enough to eliminate a
branch, sent together with the current findings and hypotheses.

Vague questions are forbidden. "Can you give more detail?" wastes the one
resource this section exists to spend carefully.

If no answer arrives, proceed on the best available evidence within the
spec's boundary. Never block waiting.

## Human Hints

`HUMAN_HINTS[]` records developer guidance with its provenance, and
`HUMAN_ASSIST_STATUS` tracks whether help was needed, requested, received or
unavailable. A hint is guidance, not truth: verify it against the current
code, and if it turns out wrong, correct the spec and say so.

## Telemetry

Record per task: token usage (exact when the runtime reports it, otherwise
clearly marked `estimated` or `unknown`), files read, search rounds, runbook
hits and misses, redefine count, and time to first preview.

**Never fabricate precision.** An unavailable number is reported as
unavailable; an invented one corrupts every efficiency decision made from it.

## Backward Compatibility

Ordinary terminal sessions are not Work sessions and this policy does not
apply to them. Work features are opt-in via the `-work` suffix, and existing
sessions, APIs and tools keep working unchanged.

## VERIFIED PROMPT START — MANDATORY

After `terminal_send_text`/submit of any coding or task prompt, especially a
long prompt, do **not** consider the task started from `SUBMIT_CONFIRMED` or
`ACCEPTED` alone. Immediately inspect the target session status/tail and
require execution evidence: a new Working/Thinking state, Ran/Edited/tool
activity, a pane-output delta, or equivalent evidence. If the prompt remains
visible in the composer or no execution evidence appears, retry Enter with
bounded recovery, checking status/tail after each press and allowing at most
six total Enter presses for that submission across manual and watcher
recovery combined. If the target is
`WAITING_APPROVAL`/`input_required`, do not spam Enter; handle approval
explicitly. Report `task started/running` only when `execution_started=true`
or equivalent evidence exists. If execution still cannot be verified after
the bounded retries, report `SUBMIT_FAILED`/`STUCK` and never pretend it is
running. Record or mention `enter_count` and execution evidence in
orchestration status where available. Before ending a ChatGPT turn that
handed off work, verify that the target prompt has actually begun executing.
The user-level verified prompt-start watcher enforces this same persisted
six-Enter cap automatically for recent tracked submissions.

This rule applies to every project, including NovaRetail, MESFlow, Terminal
MCP, and PromptFlow.

## Secrets

Never store secret values in a policy, spec, knowledge document, runbook or
telemetry record. Environment variable NAMES only. Never print or echo a real
credential. Never copy a private key, password, token or passphrase between
nodes.

## Changelog

- **1.0.0** -- initial canonical policy: planner/executor contract, difficulty
  triage, spec levels and completeness gate, execution modes, soft token and
  file/search budgets, knowledge map usage, context packs, similar-bug
  retrieval, runbook registry, verification discipline, release levels,
  developer assist, human hints, telemetry, compatibility and secrets.
