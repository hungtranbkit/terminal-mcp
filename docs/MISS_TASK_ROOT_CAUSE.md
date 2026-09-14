# Why a task can reach COMPLETED with an acceptance criterion missing

Traced on `feat/miss-task-contract` at `3b977e9`. Every claim below cites the
line it was read from. This is the audit required before any code is written;
the design it justifies is in `docs/REQUIREMENT_CONTRACT.md` (not yet written).

## The reported failure

A MESFlow task carried an amendment: *every Excel mismatch must show Sheet +
row/cell + formula*. The agent implemented the totals/mismatch part, deployed,
and reported done. The detailed requirement was dropped. Nothing in the system
objected.

Nothing objected because **no layer of the system ever held that requirement as
a checkable fact.** The six findings below are the reasons, in the order they
fail.

## RC1 — Completion has two doors and only one of them asks for evidence

`queue_store.py:1879` `mark_completed_with_evidence` refuses empty evidence.
Its own docstring, line 1890:

> `"mark_completed_with_evidence requires non-empty evidence -- use
> transition_task directly only for a test/legacy no-evidence path"`

`VALID_TRANSITIONS` (line ~189) permits `VERIFYING -> COMPLETED` outright, and
`_transition_locked` (line 1416) treats `COMPLETED` as an ordinary target that
merely stamps `completed_at`. So evidence is enforced **by convention at the
call site**, not by the state machine. A caller that uses `transition_task`
reaches the same terminal state with no evidence at all, and the resulting row
is indistinguishable afterwards.

## RC2 — "Evidence" is a presence check, not a coverage check

`queue_engine.py:456`:

```python
completed = self.store.mark_completed_with_evidence(
    task_id, evidence={"completion_marker": marker})
```

The evidence that satisfies the gate is **a marker string the agent itself
emitted**. Non-empty, therefore sufficient. Nothing compares it against
anything.

This is the load-bearing failure. Even with RC1 closed, the gate would still
pass, because the question it asks is "is there evidence?" and never "does the
evidence cover what was asked?"

## RC3 — There is no requirements model, so "R3 is missing" is not representable

A queue task has `prompt` (free text) and `metadata` (opaque JSON). There is no
list of requirements, no stable requirement IDs, no acceptance criteria, no
deliverables. `work_runs.done_criteria` exists but is a flat `list[str]` on the
*work*, not on the task, with no IDs and no per-criterion status.

So the sentence "requirement R3 was not delivered" has **nowhere to live**. A
gate cannot enforce a fact the schema cannot express, and a reviewer cannot see
the omission because there is no list to compare against.

## RC4 — An amendment is not an amendment

A follow-up instruction reaches a session as either a new queue task or a raw
`terminal_send_text`. Neither versions the original contract:

- a new task carries its own prompt and knows nothing of the first task's
  requirements;
- a raw send mutates what the agent is doing while the task row keeps its
  original prompt.

Nothing appends to, versions or supersedes the first contract. The amendment's
requirement is therefore invisible to completion by construction — which is
exactly the reported failure.

## RC5 — The raw-send bypass is warned and audited, but not blocked

`mcp_app.py:292` `_active_queue_task_for`, docstring:

> *"Used only to decide whether a raw terminal_send_text call gets a
> queue_conflict_warning -- **never blocks the send itself** (backward
> compatibility, item 12)."*

`mcp_app.py:376-394` sets `queue_conflict_warning`, records
`RAW_SEND_DURING_ACTIVE_QUEUE_TASK`, and calls `record_manual_dispatch`. The
send proceeds.

One correction to the reported symptom, verified: **PAUSED is not a blind
spot.** `_ACTIVE_STATUSES` (line 1249-1250) includes `PAUSED`, so a raw send to
a lane with a paused task *does* warn and *does* leave an audit trail. The
defect is not silence — it is that the prompt lands outside the durable
contract while the task row keeps saying something else. Observed live:
`RAW_SEND_DURING_ACTIVE_QUEUE_TASK` on task `ebc0ea84…` at 2026-09-13T14:28:43Z.

## RC6 — Deployment is not a state, so "deployed" reads as "done"

`grep DEPLOYED|deployable` over `queue_store.py` and `work_store.py` returns
nothing. There is no `DEPLOYED`/`DEPLOYABLE` concept anywhere in either store.

A task that was deployed to TEST has only one vocabulary available for "I got
it out there", and that vocabulary is `COMPLETED`. The conflation is structural,
not a mistake someone made once.

## RC7 — Verifiers default to none, so the trusted-verifier gate is vacuous

`supervisor.py:364`:

```python
verifiers_json = json.dumps(list(required_verifiers)) if required_verifiers is not None else "[]"
```

`required_verifiers` defaults to `[]` and `verifier_configured` is therefore
false for every watch that did not explicitly ask. A gate whose required set is
empty is satisfied by the empty set of verifications.

## The chain, stated once

1. The requirement was never stored as a requirement (RC3).
2. So the amendment could not attach to it (RC4).
3. So completion had nothing to reconcile against (RC2).
4. And the gate that would have asked was optional anyway (RC1), with no
   verifier required (RC7).
5. Deploying to TEST supplied the word "done" (RC6).
6. Any correction sent by hand landed outside the record (RC5).

Each link is individually defensible and locally documented. The failure is the
composition: **at no point does any component hold the sentence "this task
requires R1, R2, R3" in a form another component can check.** That is what the
Requirement Contract has to add, and why patching the completion gate alone
would not have caught this case.

## What this implies for the fix

- The contract must live on the **task**, with stable IDs, because that is the
  unit an agent is dispatched against.
- Completion must reconcile against the **latest contract version**, because
  the reported failure is specifically an amendment being dropped.
- The hard gate must be **deterministic** (compare required IDs against covered
  IDs). An LLM verifier may advise; it must not be what stands between an
  unfinished task and `VERIFIED_DONE`.
- `COMPLETED` already means "VERIFIED_DONE" in this codebase (`queue_store.py:94`
  says so explicitly). The agent's own "I am done" therefore needs a *different*
  word, or it will keep borrowing this one.
