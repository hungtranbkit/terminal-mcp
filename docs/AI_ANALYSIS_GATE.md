# AI Analysis Gate

Source of truth for what a Work run must have thought through before the
runtime is allowed to drive it.

Evaluator: `terminal_mcp/analysis_gate.py`. Enforcement point:
`WorkStore.transition_run`, on the edge into `READY`.

## Why a gate at all

Every defect this project has paid for twice had the same shape: the work
started before someone could say what "working" would look like. The gate does
not ask for more documents. It asks for the handful of answers whose absence
has actually produced a defect here — and it refuses the transition rather than
warning, because a warning in a log is a warning nobody reads.

## Where it is enforced, and why there

`work_store.py` already centralises which lifecycle edges exist, with its own
stated reason: *"a transition rule trusted to each caller is a rule that is
wrong in one of them."* The gate hooks the same chokepoint, immediately after
the edge is found valid and before the row is written. Every caller —
`work_service.control`, the dashboard, the MCP tools, the work loop — reaches
`READY` through that one function, so there is exactly one place to audit.

The gate is a **pure function over `metadata`**. It opens no database and reads
no config, which is what lets it be called inside the open transaction without
widening it, and exhaustively unit-tested without fixtures.

`IMPLEMENTATION_READY` is not a state in this codebase. `WORK_STATES` is
`DRAFT, PLANNING, READY, RUNNING, VERIFYING, WAITING_APPROVAL, BLOCKED, PAUSED,
FAILED, CANCELLED, COMPLETE`. The gate attaches to the existing `READY` rather
than adding a state, because a new state propagates into the transition table,
the dashboard's grouping and every string comparison, and buys only a name.

## Opting in, and how legacy runs stay working

A run opts in by carrying the block in its `metadata`:

```json
{"analysis_gate": {"version": 1, "profile": "full", ...}}
```

- **No block** → `NOT_ENFORCED`, allowed. A run created before this existed
  behaves exactly as it did. This is what makes the gate deployable at all.
- **Block present** → fully evaluated. Opting in and then omitting fields is a
  failure, because the run asserted the gate applies to it.
- **Deployment-wide strictness** → `evaluate(..., require_gate=True)` turns a
  missing block into `MISSING_GATE`. Off by default.

No schema migration is required: `work_runs.metadata` already exists
(`TEXT NOT NULL DEFAULT '{}'`), so a legacy row reads as `{}` and takes the
`NOT_ENFORCED` path. If a dedicated column is later wanted, `WORK_MIGRATIONS`
takes an additive `Migration(2)` under the discipline `work_store.py` already
documents: *"additive migrations tracked by PRAGMA user_version, nothing
destructive, and a schema that an older build can still read."*

## Profiles

### `full` — required fields

| Field | What it has to answer |
|---|---|
| `problem_statement` | what is wrong, in terms of observed behaviour |
| `user_observable_goal` | what the user will be able to see that they cannot now |
| `source_of_truth` | which file/endpoint/table decides correctness |
| `state_model` | the states and edges involved, or an explicit n/a |
| `invariants` | what must remain true afterwards |
| `assumptions` | each with `confidence`, `impact`, `resolution` |
| `edge_cases` | the inputs and orderings that are not the happy path |
| `dangerous_failure_modes` | how this hurts someone if it goes wrong |
| `acceptance_tests` | the checks that decide done |
| `live_verification` | how it will be observed working for real |
| `critic_result` | the outcome of an adversarial read |

`state_model` is the only field that may be answered
`{"n/a": true, "reason": "..."}`. The reason is mandatory — "n/a" with no
justification is indistinguishable from "not done". Nothing else takes n/a.

Empty is not filled: `""`, `[]`, `{}` and `null` all count as missing. An empty
`edge_cases` list must not read as "edge cases considered".

### `fast_fix` — required fields

`reproduce`, `root_cause`, `expected_behavior`, `invariant`,
`regression_test`, `live_verify`.

Deliberately short. A one-line regression fix that had to write eleven
analysis fields would be routed around, and a gate that gets routed around
protects nothing. The fast-fix profile does **not** evaluate assumptions.

## Assumptions

Each entry needs all three of `confidence`, `impact`, `resolution`.

- `impact` ∈ `high | medium | low`. Anything else is **malformed**, not
  "probably low".
- `resolution` counts as resolved when it is `resolved`, `verified` or
  `confirmed`.
- **A `high` impact assumption that is not resolved blocks the transition.**
  The reason it was written down is that being wrong about it would be
  expensive; carrying it unresolved into implementation is the failure mode
  the gate exists to stop. Resolve it, or lower its impact and say why.

## Fail-closed

Unknown evidence is not permission — the same rule `work_eligibility.evaluate`
already states for dispatch. On an opted-in run:

| Situation | Verdict |
|---|---|
| `version` not an integer | `UNSUPPORTED_VERSION` |
| `version` newer than this build | `UNSUPPORTED_VERSION` — refused, not approximated |
| `profile` unrecognised | `UNKNOWN_PROFILE` |
| `analysis_gate` not an object | `MALFORMED_GATE` |
| assumption missing a key | `MALFORMED_ASSUMPTION` |

An older controller must never silently accept a payload written against rules
it cannot evaluate.

## Verdict reasons

`PASS`, `NOT_ENFORCED`, `MISSING_GATE`, `MISSING_FIELDS`,
`UNRESOLVED_HIGH_IMPACT_ASSUMPTION`, `MALFORMED_ASSUMPTION`, `MALFORMED_GATE`,
`UNKNOWN_PROFILE`, `UNSUPPORTED_VERSION`.

Every refusal carries `detail`, and where relevant `missing_fields` /
`blocking_assumptions`, so the operator is told what to fix rather than that
something was refused.

## Rollout

1. **Ship evaluator + tests only.** Nothing calls it; behaviour unchanged.
2. **Hook `transition_run` with `require_gate=False`.** Only runs that opt in
   are judged. Every existing run is unaffected.
3. **Author runs with the block.** New work carries `analysis_gate`.
4. **Optional strict mode.** Flip `require_gate=True` per deployment once
   enough runs carry the block. Reversible.

Rollback is step-wise: unhooking `transition_run` restores prior behaviour
exactly, and the metadata already written is inert.
