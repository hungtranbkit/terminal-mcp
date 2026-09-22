# Implementation Contract v1 + the Analysis Gate

**Status:** built, unit-tested, **not wired into dispatch and not deployed.**
`terminal_mcp/impl_contract.py` is a pure, deterministic, side-effect-free
module. Nothing in the runtime calls it yet — that wiring (the queue delivery
gate), the telemetry rows, the benchmark tooling and the dashboard view are
four separate pieces of work owned elsewhere. This document is the contract
those four consume.

## The problem this exists for

The expensive reasoning in this fleet is done once, by the analysis agent, and
then thrown away. A task today carries a free-text `prompt`;
`queue_engine.build_dispatch_text` appends a completion marker; every coding
agent that picks the task up re-derives the same decisions from the same prose,
sometimes differently, usually silently.

The failure that costs real time is not a coding agent that cannot code. It is a
coding agent that **guessed at a high-impact decision the analysis never actually
made**, and got it wrong in a way nobody notices until the behavior is live.

So: make the analysis produce a structured artifact, gate on whether the
high-impact parts of it are actually resolved, and give the coding agent a
first-class way to say *"this is unspecified and it matters"* instead of picking
the option that is easiest to implement.

## The decision budget

A budget in the literal sense: judgment is finite, so spend it where being wrong
is expensive.

| Impact | Rule | Rationale |
|---|---|---|
| **HIGH** | Must be `RESOLVED` before READY. An open one → `NEED_ANALYSIS`. | This is the decision that must not be made by a coding agent at 2am. The task goes back for analysis; it does not dispatch with a shrug. |
| **MEDIUM** | May stay open, but only with an explicit `default` **and** a `guardrail`. | "Decide it later" without both is not a budget, it is a deferral. The guardrail is what detects or bounds the default being wrong. |
| **LOW** | The coding agent chooses. **Never blocks READY**, at any profile, in any quantity. | Load-bearing: a gate that punishes detail teaches agents to write less of it. |

An **assumption** is a decision the analysis made without checking, so it is
budgeted the same way — by impact, not by how confident the prose sounds:

* HIGH impact + `LOW` confidence → blocking, always.
* HIGH impact + `MEDIUM` confidence → blocking **unless** a `guardrail` says how
  a wrong assumption is detected or bounded. Either you are sure, or the blast
  radius is.
* HIGH impact + `HIGH` confidence → fine.
* MEDIUM/LOW impact → never blocking, whatever the confidence.

A decision or assumption whose `impact` is missing or unreadable is treated as
**HIGH**, never as LOW — otherwise the cheapest way to defeat the gate would be
to leave the field off.

## Profiles

| Profile | Required fields | Notes |
|---|---|---|
| `FAST_FIX` | `goal`, `expected_behavior`, `acceptance` | The minimal gate. A one-line fix must not owe a state model and an out-of-scope list, or nobody writes a contract for one. |
| `STANDARD` (default) | the above + `current_behavior`, `invariants`, `edge_cases`, `dangerous_failure_modes`, `live_verify`, `out_of_scope` | |
| `HIGH_RISK` | the above + `context_pack` + a **`critic_result`** | An independent critic must have looked at the contract. |

The minimal gate is minimal about **detail**, never about **judgment**:
`FAST_FIX` still blocks on an unresolved HIGH decision, and still requires
`source_of_truth` on a stateful task. A one-line fix to the wrong store is still
a write to the wrong store.

`critic_result` must be an object with `verdict` ∈ `PASS` /
`PASS_WITH_FINDINGS` / `FAIL`. Only `FAIL` blocks — a critic that can veto by
listing nitpicks stops being read. **What** a critic measures, and how well, is a
separate contract owned elsewhere; this module never scores, re-runs or
second-guesses one.

## Stateful work

`stateful: true` requires `source_of_truth` at **every** profile, and
`state_model` at `STANDARD`/`HIGH_RISK`. A task that changes state and does not
say where the truth lives is the highest-yield thing this gate catches: two
stores that disagree is a class of bug no amount of careful coding prevents,
because the coding agent picks whichever store it read first.

## Contract fields (v1)

```jsonc
{
  "protocol": "terminal-mcp-impl-contract/v1",   // required; skew degrades loudly
  "profile": "STANDARD",                          // FAST_FIX | STANDARD | HIGH_RISK
  "enforcement": "advisory",                      // advisory (default) | enforcing
  "stateful": false,

  "goal":              "one sentence: what is true after this ships",
  "current_behavior":  "what happens today",
  "expected_behavior": "what must happen instead",

  "source_of_truth": "the one authoritative store; what derives from it",
  "state_model":     { "states": [...], "transitions": [...], "writer": "..." },

  "invariants":              ["must still hold after the change"],
  "assumptions":             [{ "id": "A1", "statement": "...",
                                "confidence": "HIGH|MEDIUM|LOW",
                                "impact":     "HIGH|MEDIUM|LOW",
                                "guardrail":  "how a wrong one is detected" }],
  "edge_cases":              ["..."],
  "dangerous_failure_modes": ["what must not happen, even once"],
  "acceptance":              ["this is what done means"],
  "live_verify":             "run this for real, not just the unit tests",
  "out_of_scope":            ["do not do these, even if they look easy"],

  "context_pack": [{ "ref": "path/doc/url/commit", "kind": "file",
                     "why": "read it instead of searching from scratch" }],

  "decision_budget": [{ "id": "D1", "question": "...",
                        "impact": "HIGH|MEDIUM|LOW",
                        "status": "RESOLVED|OPEN",
                        "decision":  "required when RESOLVED",
                        "default":   "required when MEDIUM + OPEN",
                        "guardrail": "required when MEDIUM + OPEN" }],

  "critic_result": { "verdict": "PASS", "critic": "...", "summary": "..." }
}
```

An unrecognised field is reported (`UNKNOWN_FIELD`, **non-blocking**) rather than
dropped in silence — a misspelled `acceptance_criteria` that vanishes is how a
gate passes an empty task.

## Legacy compatibility — three explicit layers

Nothing retrofits onto the existing free-text tasks. Same opt-in posture as
`dor_gate.py`'s `metadata.dor_required`.

1. **No contract** → verdict `SKIPPED`, `blocks_ready: false`, zero findings.
   This is every task that exists today.
2. **`enforcement: "advisory"` (the v1 default)** → findings are computed and
   reported in full, `verdict` is honest about what *would* have happened, and
   `blocks_ready` is `false`. This is the rollout mode: real verdicts, zero
   refusals, so the finding rate can be measured before anything is enforced.
3. **`enforcement: "enforcing"`** → a blocking finding means `NEED_ANALYSIS` and
   `blocks_ready: true`. Opted into per task or per project, never globally.

A caller may pass `enforcement=` to `evaluate_contract()` to enforce a contract
that still declares itself advisory (a project policy, a rollout flag).
Precedence: caller > contract > advisory. An **unreadable** value falls back to
advisory, never to enforcing — a typo must not start refusing work.

**Version skew degrades loudly.** A contract declaring an unknown `protocol` (or
none) is `UNSUPPORTED_VERSION` and is **never partially validated** against v1's
rules, because a rule applied to a format it does not describe produces a
confident wrong answer.

## The prompt: reference, don't restate

`build_contract_prompt()` has one rule: the prompt **references** the contract
and its context pack, it does not restate them. It carries only what the agent
must act on —

* goal and the behavior delta,
* the decision budget split into the three things the agent must do differently
  with it (*already decided — do not re-litigate* / *deferred, use the default
  and implement its guardrail* / *yours to choose, do not ask*),
* invariants, dangerous failure modes, edge cases,
* acceptance, live-verify, out-of-scope,
* the context pack as **refs**,
* the `NEED_ANALYSIS` escape hatch.

Everything else — current behavior at length, the state model, resolved
decisions' rationale, the assumptions ledger — is **named and pointed at**, never
pasted. Every field is capped at 600 characters; a field that hits the cap is
prose the agent should open the contract for, and the prompt says exactly that.

It deliberately does **not** append a completion marker:
`queue_engine.build_dispatch_text` already does, and two markers in one prompt is
an ambiguity, not a belt-and-braces.

## NEED_ANALYSIS — the agent's answer instead of a guess

Same marker shape, and the same parsing posture, as `status.py`'s completion
marker, deliberately, so the two protocols are read by the same kind of code:

```
###TERMINAL_MCP_NEED_ANALYSIS protocol=terminal-mcp-need-analysis/v1 task_id=<id> attempt=<n> nonce=<nonce> status=need_analysis impact=HIGH decision_id=<slug>###
```

An ambiguous or incomplete marker is the same as no marker — never partially
trusted. The last well-formed marker in the output wins. The prompt states in as
many words that **returning NEED_ANALYSIS is a correct outcome, not a failed
task** — an agent only stops instead of guessing if stopping is obviously
allowed.

## Interfaces (what telemetry / the runtime consume)

```python
from terminal_mcp.impl_contract import (
    evaluate_contract, extract_contract, build_contract_prompt,
    contract_summary, contract_digest,
    parse_need_analysis_marker, build_need_analysis_marker,
    check_decision_budget, check_assumptions, check_state_declarations, check_critic,
)
```

The four `check_*` helpers are callable on their own, so a caller that already
has just a decision budget (a benchmark harness scoring analysis output, say)
does not have to synthesise a whole contract around it:

```python
check_decision_budget(decisions)                      -> list[finding]
check_assumptions(assumptions)                        -> list[finding]
check_state_declarations(contract, *, profile)        -> list[finding]
check_critic(contract, *, profile)                    -> list[finding]
```

| Function | Signature | Returns |
|---|---|---|
| `extract_contract(task)` | task dict **or** `QueueTask`-shaped object | the contract dict off `metadata.impl_contract`, or `None` (the legacy path) |
| `evaluate_contract(contract, *, enforcement=None)` | | `ContractGateResult` |
| `build_contract_prompt(contract, *, task_id, attempt, nonce, contract_ref="")` | | `str` — the coding agent's prompt |
| `contract_summary(contract)` | | flat `dict` of counters, below |
| `contract_digest(contract)` | | 16-hex-char `str`, order/whitespace-stable |
| `parse_need_analysis_marker(output)` | agent output | field `dict` or `None` |
| `build_need_analysis_marker(*, task_id, attempt, nonce, impact=HIGH, decision_id="")` | | the exact line the agent prints |

`ContractGateResult` (a JSON-shaped dict; `.to_dict()` for an explicit copy):

```python
{
  "verdict":         "READY" | "NEED_ANALYSIS" | "SKIPPED" | "UNSUPPORTED_VERSION",
  "blocks_ready":    bool,        # <-- the ONLY field a delivery gate branches on
  "enforcement":     "advisory" | "enforcing",
  "profile":         "FAST_FIX" | "STANDARD" | "HIGH_RISK" | "",
  "findings":        [{"code": str, "blocking": bool, "detail": str,
                       "field": str, "ref": str}],
  "reason":          str,
  "contract_digest": str,
  "protocol":        "terminal-mcp-impl-contract/v1",
}
```

> **Branch on `blocks_ready`, never on `verdict`, and never re-derive "did
> anything block" from `findings`.** `blocks_ready` already folds in the
> advisory/enforcing rollout mode; a caller that re-derives it will silently
> start refusing work the moment advisory mode is on. `verdict` is for the
> record and for telemetry.

**Finding codes are stable strings** — telemetry groups by them, so they are part
of this interface and do not get reworded:
`UNRESOLVED_HIGH_DECISION`, `MEDIUM_DECISION_WITHOUT_DEFAULT`,
`MEDIUM_DECISION_WITHOUT_GUARDRAIL`, `HIGH_IMPACT_LOW_CONFIDENCE_ASSUMPTION`,
`HIGH_IMPACT_UNGUARDED_ASSUMPTION`, `MISSING_SOURCE_OF_TRUTH`,
`MISSING_STATE_MODEL`, `MISSING_REQUIRED_FIELD`, `MISSING_CRITIC_RESULT`,
`CRITIC_VERDICT_FAIL`, `MALFORMED_ENTRY`, `UNKNOWN_FIELD`, `UNKNOWN_PROFILE`,
`LOW_DECISION_DELEGATED` (non-blocking; lists what the agent may choose).

`contract_summary(contract)` — flat counters, no opinion about where they are
stored:

```python
{"has_contract": bool, "protocol": str, "profile": str, "enforcement": str,
 "contract_digest": str, "stateful": bool,
 "decisions_total": int, "decisions_high_open": int, "decisions_high_resolved": int,
 "decisions_medium_open": int, "decisions_low": int,
 "assumptions_total": int, "assumptions_high_impact": int,
 "has_critic_result": bool, "context_pack_refs": int}
```

On a legacy task it returns exactly `{"has_contract": False}` and nothing else.

## Disclosed scope cuts

Not silent omissions — each is a deliberate line this module does not cross.

* **`context_pack` refs are validated for shape only.** This module never opens a
  file, resolves a repo path or fetches a URL. A gate that touches the filesystem
  cannot be run on the analysis agent's side, where the contract is authored and
  where catching a bad ref is worth the most.
* **No field's content is judged for quality.** `"acceptance": "it works"` is
  present, and this gate passes it. Detecting a vacuous field is a reasoning
  task — that is the critic's job on `HIGH_RISK`, or a human's.
* **`live_verify` is required as a declaration but never executed here.** Running
  it is the delivery gate's job.
* **`stateful` is declared, never detected.** An analysis that forgets to set it
  on genuinely stateful work escapes the source-of-truth rule entirely, and
  nothing here can catch that — deciding whether a change touches state is
  exactly the reasoning this gate refuses to fake. The mitigation is a policy
  upstream (a project that defaults `stateful: true` for anything touching a
  migration or a store path), not a heuristic in the gate.
* **The gate is deterministic, not an LLM call** — same posture, and the same
  reason, as `coordinator.py`'s gate and `dor_gate.py`: "is this HIGH-impact
  decision marked resolved" is mechanically checkable, and a gate that is itself
  a guess cannot credibly refuse a guess.
* **Nothing is wired.** No queue/coordinator/dashboard/telemetry file is touched
  on this branch, on purpose: those are other owners' surfaces, and a contract
  format is worth agreeing on before it has four callers.

## Tests

`tests/test_impl_contract.py` — 66 pure unit tests, no I/O. The five behaviors
the task named are pinned by name:

| Behavior | Test |
|---|---|
| high-impact unresolved blocks READY | `test_unresolved_high_decision_blocks_ready` |
| LOW detail does not block | `test_low_impact_detail_never_blocks_and_is_reported_as_delegated` |
| missing source-of-truth on a stateful task blocks | `test_stateful_task_without_source_of_truth_blocks`, `test_source_of_truth_is_required_even_at_the_fast_fix_minimal_gate` |
| FAST_FIX minimal gate | `test_fast_fix_needs_only_goal_expected_behavior_and_acceptance`, `test_the_same_minimal_contract_at_standard_is_not_ready`, `test_fast_fix_still_blocks_on_an_unresolved_high_decision` |
| agent returns NEED_ANALYSIS rather than guessing | `test_need_analysis_marker_round_trips`, `test_the_prompt_carries_the_escape_hatch_the_agent_is_supposed_to_use`, and the four negative-parse tests around them |
