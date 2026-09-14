# Analysis Gate — Understand First, Code Second

Status: **VERIFIED** (2026-09-14; 108 new tests passed, plus 2 real-runtime
dogfood tests via `pytest -m queue_smoke`). `terminal_mcp/analysis_gate.py`,
enforced at the Coordinator's PRECHECK → READY gate. The full evidence
record lives in `docs/REQUIREMENTS.md` §20.6 Phase F — see §10a for why a
status claim must carry it.

This document is the **versioned source of truth** for the rule. The MCP
server's `instructions` field (`mcp_app.ANALYSIS_GATE_BOOTSTRAP`) is a
deliberately short pointer at this file and must never grow into a copy
of it — duplicated rules drift, and then nobody knows which one is real.

---

## 1. Why this exists

The failure this prevents is not "the agent wrote bad code". It is **the
agent wrote confident code for a problem nobody had actually understood
yet** — a plausible reading of an ambiguous request, implemented well,
shipped, and wrong. That failure is invisible at review time because the
code is internally consistent. It is only visible against the real
system, which is exactly where it is most expensive.

So the gate does not check code quality. It checks whether understanding
exists, and refuses to let implementation start without it.

---

## 2. The pipeline

```
REQUEST
  → CONTEXT_GATHERING
  → UNDERSTANDING
  → CLARIFICATION
  → MODEL
  → FEATURE_CONTRACT
  → CRITIC_REVIEW
  → IMPLEMENTATION_READY
  → CODE
  → LIVE_VERIFY
  → RELEASE
```

Each stage, and what it means:

| Stage | The question it answers | Done when |
|---|---|---|
| **REQUEST** | What was actually asked? | The raw ask is recorded verbatim, not yet interpreted. |
| **CONTEXT_GATHERING** | What is already true? | Evidence collected **first-hand** from MCP tools, the repo, and the live runtime. |
| **UNDERSTANDING** | What is really going on? | Current behaviour described from evidence, not from the request's own framing. |
| **CLARIFICATION** | What can only a human answer? | Every remaining unknown is either resolved by evidence or escalated as a real question. |
| **MODEL** | How does this system actually work? | State model, data flow, and ownership written down. |
| **FEATURE_CONTRACT** | What exactly are we committing to? | All contract fields (§4) filled in. |
| **CRITIC_REVIEW** | How does this break? | An adversarial second read; mandatory for the categories in §6. |
| **IMPLEMENTATION_READY** | May we code? | Gate passes: nothing missing, no unresolved high-impact assumption. |
| **CODE** | — | Implementation, within the contract's stated scope only. |
| **LIVE_VERIFY** | Does it work *for real*? | Proven on the real runtime, not only in tests. |
| **RELEASE** | Can we show it? | Release evidence recorded (§10). |

Stages may iterate. They may not be **skipped**, and CODE may not start
before IMPLEMENTATION_READY.

---

## 3. MCP-first evidence gathering

> **Get the evidence yourself before asking anyone.**

Order of resort, strictest first:

1. **MCP tools / the live runtime** — what the system actually does now.
2. **The repo** — code, tests, migrations, and the docs in this folder.
3. **The human** — *only* for business and UX decisions that genuinely
   cannot be derived safely.

Asking a human something the runtime could have answered is a defect, not
diligence: it is slow, it moves the burden of proof onto someone with
less context, and their recollection is weaker evidence than the running
system. Conversely, **guessing a business rule from code shape is also a
defect** — code tells you what happens, never what *should* happen.

Every claim in the contract must be traceable to something actually read
or run. "I assume the API returns X" is an assumption (§5). "I called it
and it returned X" is evidence.

---

## 4. The Feature Contract

The FULL profile requires all of these. None is derivable from the
others, which is why each is asked separately.

| Field | What it must say |
|---|---|
| **goal / `problem_statement`** | The real problem, in problem terms — not a restatement of the proposed solution. |
| **current behaviour** | What the system does *today*, established from evidence. |
| **expected behaviour / `user_observable_goal`** | What it will do instead, **stated as something a user can observe**. |
| **user-observable invariants** | What must stay true afterwards. Written so a person could check them without reading code. |
| **source of truth** | Which code / doc / table / runtime is authoritative when two disagree. |
| **state model** | The states, the legal transitions, and who may cause each one. |
| **input → output** | For each entry point: what goes in, what comes out, what is rejected. |
| **UI states** | Loading, empty, partial, error, stale, unauthorised, offline — each one decided, not defaulted. |
| **edge cases** | The boundaries that were actually considered, and the verdict for each. |
| **dangerous failure modes** | What could destroy data, lose work, corrupt state, or leak. For each: why it cannot happen, or what contains it. |
| **backward compatibility** | What existing callers, rows, and sessions do after this change. Explicitly including "nothing, because …". |
| **acceptance** | How we will know it works — concrete, checkable. |
| **live verification** | How it will be proven on the **real** runtime. |
| **out of scope** | What this change deliberately does not do, so scope creep is visible. |

### Machine-readable form

The gate reads a structured object (`queue_tasks.analysis`, with a
fallback to `metadata.analysis`). Required keys for `profile: "full"`:

```jsonc
{
  "profile": "full",
  "problem_statement":    "...",
  "user_observable_goal": "...",
  "source_of_truth":      "...",
  "evidence":             ["what was actually read/run, per claim"],
  "invariants":           ["what must stay true, user-observable"],
  "assumptions":          [ /* see §5; [] is a valid, explicit answer */ ],
  "acceptance_tests":     ["..."],
  "live_verification":    "how this is proven on the real runtime",

  // required only for the categories in §6
  "categories":    ["state-machine"],
  "critic_result": "what the adversarial review found"
}
```

The prose fields in the table above (state model, UI states, edge cases,
dangerous failure modes, backward compatibility, out of scope) belong in
the same object and are reviewed by the critic; the gate mechanically
enforces the keys listed in the JSON block, because those are the ones
whose absence is unambiguously detectable. **A gate that cannot detect
something does not excuse omitting it.**

---

## 5. Assumptions, and the rule that matters most

> **An unresolved HIGH or CRITICAL impact assumption blocks
> implementation. No exceptions.**

Every assumption is recorded as:

```jsonc
{
  "statement":  "inbound orders always have a numeric order_number",
  "confidence": "LOW | MEDIUM | HIGH",
  "impact":     "LOW | MEDIUM | HIGH | CRITICAL",
  "status":     "OPEN | RESOLVED",
  "resolution": "how it was settled, and with what evidence"
}
```

Rules the gate enforces mechanically:

- **Impact, not confidence, decides whether it blocks.** A HIGH-confidence
  guess about something critical is still a guess. Confidence is recorded
  because it tells a reviewer where to look — it never unblocks anything.
- **No declared impact is treated as blocking.** "I didn't say how bad
  this could be" is itself an unresolved high-impact unknown.
- **`status: RESOLVED` without a `resolution` does not resolve anything.**
  That is the "tick the box to get past the gate" failure mode, and it is
  refused.
- **`assumptions: []` is a valid answer** — but it must be *said*.
  Silence is indistinguishable from never having looked.

### UNKNOWN > guess

When you do not know, the correct output is `UNKNOWN` and a question —
never the most plausible value. A wrong `UNKNOWN` costs one round trip.
A wrong guess ships.

### Question Ledger

Open questions are tracked, not remembered. Each entry: the question, why
it matters, its impact, who can answer it, and its answer once given.
The ledger is how a HIGH-impact assumption becomes RESOLVED, and it is
what makes "we asked and nobody answered" visible instead of quietly
becoming a guess.

---

## 6. Critic review

An adversarial second read is **mandatory** when the task declares any of:

`state-machine`, `workflow`, `auth`, `security`, `deploy`, `data-model`,
`multi-agent`, `automation`, `destructive`

These are the categories where a plausible-looking change does damage
that testing the happy path will not reveal. The critic's job is not to
approve — it is to find the case that breaks it, and `critic_result` must
record what was actually found (including "nothing, having checked X, Y,
Z").

---

## 7. Permission ≠ occupancy

Being *allowed* to change something is not the same as being the right
one to change it, and an unlocked file is not an unowned one. Before
editing shared ground, establish that nobody else is mid-change in it.
This project already encodes the same principle in the Coordinator's
cross-lane conflict check and in the named-resource ownership lock — the
gate states it because the reflex has to exist before the tooling catches
it.

---

## 8. Fast Fix — the minimum gate

Small fixes get a lighter profile (`profile: "fast_fix"`), never no gate:

| Field | Meaning |
|---|---|
| `reproduce` | The bug was actually reproduced. Not inferred from a report. |
| `root_cause` | Why it happens. Not "where the exception surfaced". |
| `expected_behavior` | What it should do instead. |
| `invariant` | What must not break while fixing it. |
| `regression_test` | The test that fails before and passes after. |
| `verify_fix` | How the fix was confirmed on the real runtime. |

A one-line change is exactly where an unreproduced "obvious" fix does its
damage. If you cannot reproduce it, you do not yet know what you are
fixing — that is an ANALYSIS outcome, not a Fast Fix.

---

## 9. Testing: behaviour, not implementation

- **Test what a user can observe.** A test asserting internal call order
  passes when the feature is broken and fails when it is merely
  refactored — it is worse than no test, because it is trusted.
- **Mock ≠ live proof.** Mocks prove the code does what its author
  expected. They cannot prove the real system agrees. Anything that
  crosses a process, a network, a filesystem, or a schema needs at least
  one real-runtime check.
- A green suite is evidence about the suite. `LIVE_VERIFY` is evidence
  about the system.

---

## 10. Release evidence

A change is releasable when there is a record of it working **on the real
target**: what was run, where, when, and what was observed — not "tests
pass", not a screenshot of a mock, not a deploy that exited 0. If the
evidence would not convince someone who did not want to believe you, it
is not evidence yet.

---

## 10a. Status claims are themselves evidence claims

> **A document may not say VERIFIED until the evidence exists and is
> written down next to the claim.**

Writing "verified" while the tests are still unwritten is the same defect
as shipping a guess: it is a confident statement that outran its
evidence, and it is worse than silence because everyone downstream now
believes it. This happened during this very feature's own
implementation — the Phase F note was written as "unit + dogfood
verified" before a single test had been run — which is precisely why the
rule is stated here and enforced by a test.

The allowed status ladder, in order:

| Status | Means |
|---|---|
| `PLANNED` | Not built. |
| `IMPLEMENTED — UNVERIFIED` | Code exists. Nothing has been proven. |
| `TESTING` | Verification in progress; partial results only. |
| `VERIFIED` | Tests **and** live/dogfood evidence exist **and are recorded with the claim** — exact counts, what was actually run, what was observed. |

A `VERIFIED` claim with no recorded evidence is a defect to be fixed by
downgrading the claim, never by adding the evidence afterwards from
memory. `tests/test_docs_status_claims.py` enforces this mechanically for
this document and for the §20.6 Phase F note.

---

## 11. Never silently reinterpret a requirement

If the contract is **missing, ambiguous, or high-impact**, the correct
output is `ANALYSIS` / `NEEDS_CLARIFICATION` — the work stops and says
what it needs. It does not proceed on the most reasonable reading.

An agent may resolve ambiguity **itself** only when it can do so from
evidence (MCP, code, runtime) and record how. Business and UX decisions
that cannot be derived safely go to the human, every time.

---

## 12. How the gate is enforced

- **Module:** `terminal_mcp/analysis_gate.py` — pure, deterministic, no
  LLM, never raises. Same posture as `dor_gate.py`.
- **Enforcement point:** exactly one — check `2b` in
  `CoordinatorGate.review()`, the PRECHECK → READY transition. A task
  that fails becomes `BLOCKED` with the machine-readable verdict in
  `coordinator_decision.evidence.analysis_gate` and the human-readable
  account in `reason`.
- **Not enforced at create/assign.** A task *is* the request; requiring a
  finished contract to file one would invert the pipeline.
- **Inspect and record:** `terminal_task_check_analysis` and
  `terminal_task_set_analysis` (MCP), `QueueService.check_analysis` /
  `.set_analysis` (Python).
- **Storage:** `queue_tasks.analysis` (nullable JSON, migration v9), with
  a read fallback to `metadata.analysis`.

### Scope — which tasks are gated

A task is gated only when it resolves to a real profile:

- explicitly, via `analysis.profile` / `metadata.analysis_profile`; or
- by class, via `metadata.task_class` (or `metadata.type`):
  `implementation`/`feature`/`fix`/`bugfix`/`refactor`/`migration` →
  **full**; `fast_fix`/`hotfix` → **fast_fix**.

Anything else — **including every task created before this gate
existed** — resolves to `profile: "none"` and passes untouched. That is
deliberate: retroactively requiring contracts would block a real queue
full of legitimate work overnight.

`chore`, `docs`, `research` and `incident` are intentionally ungated. An
incident is triaged under time pressure and has its own lane (§20.6
Phase B); forcing a Feature Contract onto it would do harm.

### Rollout

| Policy | Effect |
|---|---|
| `enforcement="advisory"` | Runs every check, reports the verdict, blocks nothing. Use this to measure a real backlog before switching on. |
| `enforcement="enforce"` (default) | Blocks gated tasks that fail. |
| `require_classification=True` | Unclassified tasks are treated as **full** instead of ungated. The intended end state — a deliberate, reviewable flip, never the default. |
| `enforcement="off"` | Disabled entirely. |

`GATE_VERSION` is stamped into every verdict, so a stored result is
always attributable to the rules that produced it.
