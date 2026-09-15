# Prompt submission: architecture and reliability guarantees

This document maps the "reliable prompt submission with transaction and
proof" model (PREPARE → WRITE → VERIFY → ACTIVATE → PROVE_ACCEPTED →
TRACK → COMPLETE/FAIL) onto what this codebase actually implements, and
records the extensions added on top of it. **This is documentation of an
audit, not a rewrite**: the send pipeline described below (`core.py`'s
`_send_text_and_verify_locked` plus `adapters.py`'s `AgentAdapter`
hierarchy) already existed, already implements essentially this entire
model, and has already been hardened through multiple real, live-CLI
verification passes (see the "P0" commit history and
`tests/test_adapters_real_cli.py`). The prompt-submission reliability
upgrade this document was written for extended that existing pipeline in
place rather than building a parallel one — see "What already existed"
below for exactly why, and "What this upgrade added" for the small,
additive delta.

## The golden rule

> **Never resend a prompt's text past the activation-ambiguity boundary
> unless there is positive evidence the first activation was not
> accepted.**

Concretely: once `tmux send-keys ... Enter` (or any transport's
equivalent activation trigger) has been transmitted, this codebase never
automatically re-types and re-submits the same prompt text on its own
initiative. `tmux send-keys` succeeding only proves bytes were written to
the pty — never that the receiving program acted on them. Ambiguity after
activation is reported as `DELIVERY_UNKNOWN` (or, in a future transport's
own vocabulary, `PROMPT_SUBMISSION_UNKNOWN`), not silently upgraded to
success, and not treated as a license to try again with the original
text. The one exception, and the only thing that gets close to "trying
again", is a single, narrowly-gated Escape+Enter *activation* retry —
described below — which never re-types the prompt itself, only re-issues
the submission trigger, and only when very specific evidence says the
original text is still the one sitting in the composer.

## The acceptance gate (MANDATORY — added 2026-09-14)

> **A prompt is DELIVERED only when (1) the send receipt's own
> `delivery_state` is `SUBMIT_CONFIRMED`, AND (2) a separate, post-submit
> observation shows the target actually took it.**

Both halves are required. Neither is sufficient. Anything short of both is
NOT delivered: it must not be reported to a user as delivered, and it must
not advance a queue task to DISPATCHED/RUNNING.

`SUBMIT_CONFIRMED` means "Enter was processed and the pane moved past the
pre-Enter baseline". It does **not** mean the agent read the prompt and
started working. Those are different claims, and conflating them is how a
task ends up looking dispatched while nothing is running.

### The rule, operationally

| Outcome | What it means | What you may do |
|---|---|---|
| `DELIVERED` | `SUBMIT_CONFIRMED` **and** acceptance evidence | Report delivered. Advance the task. **Stop polling for completion** — the next scheduled check watches for the result. |
| `NOT_ACCEPTED` | submit confirmed, acceptance not observed | Do **not** report delivered. Do **not** advance. **Never resend** — the submit is confirmed, so a resend duplicates it. Re-observe; escalate to a human if it persists. |
| `UNCERTAIN` | `DELIVERY_UNKNOWN`, or any state not provably confirmed | Do **not** report delivered. Do **not** advance. Inspect `terminal_input_context` first, then re-attempt **only** under the same `idempotency_key`/`submission_id`. |
| `REFUSED` | `BLOCKED`/`ERROR`, or text sent with no Enter | Not delivered. Fix the cause, then re-attempt under the same idempotency key. |

Acceptance evidence is any one of: the target reports a working/running
state; the adapter's own `submit_ack_evidence` fires; output genuinely
advanced past the post-submit baseline. Explicitly **not** acceptance: the
composer still holding the prompt; the target waiting on a human (it has
not started *our* work); no observation available at all — unobservable is
never a pass.

### Claude specifically: never spam Enter

For a Claude target, raw/repeated Enter is forbidden. If the prompt is
still sitting in the input box, inspect (`terminal_input_context`) and
clear it, then use exactly **one** `terminal_send_text(..., press_enter=True)`.
Never a bare `terminal_send_keys(["Enter"])` to "help it along", never a
second Enter because the first looked slow. The bounded Escape+Enter
activation retry described above is the *only* re-activation this codebase
performs, and it is issued by `core.py` itself under its own evidence
gate — not by a caller.

### Where this is enforced

`terminal_mcp/delivery_gate.py` is the single decision point. It is pure:
it reads a receipt plus post-submit observations and returns a
`DeliveryVerdict`; it cannot send, press Enter, or retry (asserted by a
test that walks its AST for call names). Call sites enforce; the gate
decides, once.

**Why a new module rather than a fix at each call site** — the audit found
every send path making the same decision independently, and every one of
them used a **denylist**:

- `queue_engine._dispatch`: `if error -> BLOCKED`; `if delivery_state ==
  "DELIVERY_UNKNOWN" -> DISPATCH_UNCERTAIN`; then fell through to
  `transition_task(RUNNING)` **unconditionally**. Any delivery state that
  was not that one literal string — present or future — meant RUNNING.
- `supervisor2.execute_send`: `if submit_status == "SUBMIT_UNCONFIRMED" ->
  blocked`; then fell through to `state='observing'`. Same shape.

Neither was *actively* broken when audited: every `BLOCKED`/`ERROR` receipt
in `core.py` also sets `error`, and `TEXT_SENT` is only produced when
`press_enter=False`, so the denylists happened to cover the states that
actually occur. That is a coincidence of the current implementation, not a
guarantee — `adapters.py` documents `DELIVERY_STATES` as extensible, and an
idempotent replay can return an older receipt shape. The gate inverts this
to a positive allowlist, so an unrecognised state degrades to UNCERTAIN
instead of being promoted to delivered.

Neither path checked acceptance at all.

### Rollout: advisory → enforce

`config.prompt_delivery.mode` defaults to **`advisory`**, mirroring
`supervisor2.POLICY_MODES`' escalation shape. In advisory the verdict is
computed, recorded to the task's `metadata.delivery_verdict` (plus a
bounded 5-entry history) and available to an operator, while **every
existing transition behaves exactly as before** — zero behaviour change.

One half is enforced in *both* modes, deliberately: the positive
activation allowlist (gate 1). It can only ever refuse a dispatch the old
code would have wrongly advanced, and for every state that actually occurs
today it produces the identical outcome — so it is pure hardening with no
behavioural delta. Gate 2 (acceptance) changes outcomes — a confirmed
submit with no acceptance evidence becomes `DISPATCH_UNCERTAIN` instead of
`RUNNING` — so it requires `mode=enforce`.

`require_acceptance=false` keeps gate 1 while skipping gate 2, for a
deployment that wants the denylist fixed without the extra observation.

### Stop polling once delivered

Once a verdict is `DELIVERED`, the delivery question is settled — stop
polling for completion. Watching for the *result* is a separate concern
owned by the completion watcher / verify queue on its own schedule. The
acceptance check is deliberately **one** extra observation, not a poll
loop, for the same reason.

### Not yet enforced (tracked, not assumed)

`supervisor2.execute_send` and the direct MCP send tools still make their
own decision. Wiring them through `delivery_gate` needs their own state
machines considered (an action's `sent`/`observing`/`blocked` CAS chain,
and a caller-facing receipt field for the MCP tools) — see the backlog
item referenced in REQUIREMENTS rather than guessing at it here. A direct
`terminal_send_text` caller (ChatGPT, Claude Code) cannot be enforced
in-process at all: for them this section **is** the rule.

## What already existed (the audit)

Before this upgrade, the following was already live, already tested
against real Codex/Claude Code CLI sessions, and already covered the vast
majority of P1–P5, P7:

### The lifecycle, mapped

| This doc's stage | Existing implementation |
|---|---|
| **PREPARE** | `core.py`: resolve session identity + `pane_current_command`, select an `AgentAdapter` (`adapters.select_adapter`) — done *twice*, immediately before the text write and again immediately before Enter (see "PREPARE, twice" below). |
| **WRITE** | `tmux.send_text(session, text, press_enter=False)` — text only, no submission attempt yet. |
| **VERIFY** (pre-activation) | A pre-send check: if the pane's current tail looks like a menu/approval/confirmation prompt (`AgentAdapter.identify_target_state(...) == TARGET_WAITING`), the send is refused *before* either the text or Enter goes out (`TARGET_AWAITING_APPROVAL`) — sending would risk answering a prompt that isn't this request's own composer. |
| **ACTIVATE** | `tmux.send_keys(session, ["Enter"])` — but only after a second identity/command revalidation (see below). |
| **PROVE_ACCEPTED** | `AgentAdapter.submit_ack_evidence(before, after, sent_text)` — adapter-specific, evidence-based, never a bare "did the pane change" check (a live-redrawing Ink UI's own spinner/cursor/timer tick changes every capture on every tick regardless of whether anything was actually submitted). |
| **TRACK** | `correlation_id` (a fresh UUID per send attempt) ties an attempt to its audit-log row; `idempotency_key` (caller-supplied, optional) makes a retried *request* — not just a retried keystroke — safe. |
| **COMPLETE / FAIL** | `delivery_state` ∈ `{TEXT_SENT, SUBMIT_CONFIRMED, DELIVERY_UNKNOWN, BLOCKED, ERROR}` (`adapters.DELIVERY_STATES`) is the authoritative outcome; `submit_status` is a strictly-derived legacy alias kept for existing callers. |

**PREPARE, twice.** Identity and foreground command are resolved and
compared *twice*: once immediately before the text write, once again
immediately before Enter. If either has changed in between — the pinned
tmux session/pane identity no longer matches, or the foreground command
has changed (the process that was about to receive Enter already exited,
or the name now answers for an unrelated pane) — Enter is withheld
entirely (`IDENTITY_CHANGED_MID_SEND`, `delivery_state: BLOCKED`). The
text that was already written stays written; only the Enter is withheld.
This never retargets by session name.

### Evidence, not heuristics — and never one heuristic for every CLI

`adapters.py`'s `AgentAdapter` is exactly the "per-agent-type adapter
with its own evidence rules" this upgrade's design called for
(`CodexAdapter`, `ClaudeAdapter`, `GenericShellAdapter` — the fallback for
any other command). Each adapter answers five questions as a pure
function of pane content it is handed (never touches tmux itself, which
is what makes the whole hierarchy trivially unit-testable against fixture
text and separately exercised against real CLI sessions):

- `identify_target_state(lines)` — `composer | running | waiting | final | unknown`.
- `can_submit_now(lines)` — false while the target is already actively working (a stray Enter there could be misinterpreted).
- `submit_ack_evidence(before, after, sent_text)` — true only with genuine, adapter-specific evidence this *exact* attempt was processed.
- `stuck_composer_evidence(before, after)` — true only for a *reproduced* composer-swallow failure signature specific to that adapter (Codex has one; Claude and the generic shell adapter do not, and always return `False`, so no recovery path is ever attempted for a failure mode never observed for them).
- `safe_recovery_allowed(lines)` — false whenever the target already shows active-work evidence (Escape could genuinely interrupt real work).

Codex and Claude share the same underlying "Ink-rendered CLI" working/
waiting footer patterns (`esc to interrupt`, `working`, `thinking` for
running; `[y/n]`, `press enter`, `enter to select`/`tab/arrow keys to
navigate` for an open menu/approval dialog), but their `submit_ack_
evidence` and recovery eligibility genuinely differ — Claude additionally
requires the sent text to be demonstrably echoed back whenever the target
was already busy at either end of the verification window (closing a real
evidentiary gap an ordinary busy-target spinner tick could otherwise
coincidentally satisfy), a requirement Codex's own adapter does not need
and does not have.

### The one allowed retry: bounded, evidence-gated, never a resend

The pipeline's *only* automatic retry is a single Escape-then-Enter
*activation* retry — never a second copy of the prompt text — and it only
fires when **all** of the following hold, checked fresh immediately
before the retry (not reused from the original decision, which could
already be stale by the time the retry would fire):

1. The specific adapter reports `stuck_composer_evidence` for this exact
   attempt's before/after pair (a known, previously-reproduced signature —
   currently only `CodexAdapter` ever returns `True` here).
2. `safe_recovery_allowed` — the target does not currently show active-work evidence.
3. Re-checked, right before the retry: identity/command still match this
   attempt's pinned values, the composer still shows the stuck pattern,
   *and* this attempt's own sent text is still visibly present
   (`_sent_text_echoed`) — positive proof the pending draft is still
   *this* attempt's own, not a different one (a real user's edit, or
   another caller's send).

If the retry's own evidence still doesn't confirm, the result is
`DELIVERY_UNKNOWN` — never a false `SUBMIT_CONFIRMED`, and never a further
retry.

### Fail-closed, already

- Session doesn't exist → `SESSION_NOT_FOUND`.
- Pane in tmux copy-mode → refused outright for every input path (a
  scrollback/search overlay swallows every keystroke, text or key alike,
  invisibly to the foreground process).
- Two concurrent senders to the same pane → the durable, cross-process
  pane lease (`lease.py`) serializes them; a loser gets `PANE_BUSY`
  (`delivery_state: BLOCKED`), never interleaved keystrokes.
- A grant's pinned identity no longer matches the session currently
  answering to that name → `IDENTITY_MISMATCH`, never a silent send to an
  unvetted, recreated pane.
- None of this ever falls back to "the last active session" or any other
  session than the one explicitly named.

### Idempotency (already P7)

`idempotency_key` (optional, caller-supplied) makes a *retried request* —
not just a retried keystroke inside one send — safe: the first caller to
successfully claim a given key is the only one that ever actually sends.
A repeat call with the same key returns the original stored result
(durable across a process restart — the claim is on disk), never sends
twice. A concurrent caller that loses the claim race while the winner is
still mid-flight gets `DUPLICATE_IN_PROGRESS` (unless the original
claimant crashed before storing a result, in which case the claim is
reclaimed rather than blocking forever).

## What this upgrade added

Everything below is **additive and backward compatible** — no existing
field was renamed or removed, no existing caller's behavior changed, and
every new config key defaults to reproducing today's exact behavior.

### P6/P8 — receipt enrichment (`core.py`: `_enrich_receipt`)

Every result from `terminal_send_text`, `terminal_send_text_granted`, and
`terminal_send_bound` (all three route through the same
`_send_text_and_verify`) now additionally carries:

- `submission_id` — an alias of the existing `correlation_id` field (kept
  as a second key, not a rename, so no existing reader of
  `correlation_id` needs to change).
- `agent_type` — the selected adapter's name (`codex` / `claude` /
  `generic`).
- `evidence` — a short list of evidence codes. Deliberately **one honest
  code per case** rather than a richer taxonomy
  (`INPUT_CLEARED`/`AGENT_RUNNING`/`TURN_CREATED`/`PROMPT_ECHOED`) the
  adapters cannot actually distinguish today: every adapter's
  `submit_ack_evidence` ultimately answers one yes/no question — did
  genuine pane output move past the pre-Enter baseline — so
  `OUTPUT_CHANGED` is the only claim that is always true of what was
  actually checked (plus `TEXT_SENT` for a plain, unconfirmed-by-design
  text append, and `RECOVERY_ESCAPE_ENTER` appended when the bounded
  recovery path fired). Never invents evidence that wasn't really
  observed.
- `activation_attempts` — `0` (Enter never sent), `1` (sent once), or `2`
  (the one bounded recovery retry also ran). Never more than `2`.
- `stage` — set only on a failure/ambiguous outcome, one of `WRITE` /
  `ACTIVATE` / `ACCEPTANCE`, for quick diagnosis of *where* a send
  stopped making progress. Omitted entirely on a confirmed or plain
  `TEXT_SENT` result (nothing to diagnose there).

### P9 — permission-model normalization (`config.py`, `core.py`)

The three concepts this upgrade's design asked for map onto existing
controls, with one genuine gap closed:

| Concept | Existing control |
|---|---|
| `read` | `permissions.terminal_read` |
| `send_prompt` | `permissions.terminal_input` (gate) + `input_policy.allow_send_text` (the verified, adapter-guarded text-composition path) |
| `send_keys` | `permissions.terminal_input` (gate) + `input_policy.allow_keys` / `sensitive_keys_require_confirmation` (a fixed, already-restrictive key vocabulary — `Enter`/`Escape`/arrows/`Tab`, plus confirmation-gated `C-c`/`C-d`) |

What was missing: a way to disable raw `send_keys` specifically while
keeping `send_prompt` enabled — both were gated *only* by the single
`terminal_input` flag. Added: `permissions.allow_send_keys` (default
`True` — every existing `config.yaml` is unaffected). Set to `False` to
disable `terminal_send_keys` while `terminal_send_text`/
`terminal_send_bound` keep working.

No `ask_chatgpt` permission exists yet — deliberately out of scope for
this phase (see `docs/chatgpt-web-adapter-plan.md`).

### P11 — loop-protection metadata (`config.py`, `audit.py`, `core.py`)

`terminal_send_text`/`terminal_send_text_granted` accept four new,
**optional, keyword-only** parameters: `origin`, `trace_id`,
`parent_turn_id`, `depth` (default `0`). No current caller (any MCP tool,
the dashboard, Supervisor v2) passes any of them, so nothing changes for
anything that exists today. They exist so a future agent-bridge (most
concretely: a ChatGPT-Web adapter turn re-entering a Codex/Claude session)
has a schema to carry provenance through, from day one, without a later
breaking change.

- `depth` is the one value actually **enforced**: a call with
  `depth > config.max_agent_bridge_depth` (default `2`) is refused
  fail-closed (`AGENT_BRIDGE_DEPTH_EXCEEDED`) before anything is sent —
  this is what bounds an agent-to-agent forwarding chain once one exists,
  rather than allowing it to recurse unboundedly.
- `origin`, `trace_id`, `parent_turn_id` are recorded to the audit log
  (`input_audit.origin`/`.trace_id`/`.parent_turn_id`/`.depth` — new,
  nullable columns, `AUDIT_MIGRATIONS` migration 3) purely for future
  cross-system trace reconciliation. Never exposed in any MCP tool schema
  or dashboard UI in this phase (none is needed yet).
- `prompt_transport.SubmissionOrigin` is the equivalent dataclass for a
  future non-tmux transport to construct and pass along; its `.child()`
  method is how a bridge hop would increment `depth` while carrying
  `trace_id` forward.

### P10 — `PromptTransport` extension point (`terminal_mcp/prompt_transport.py`, new file)

A `Protocol` (`prepare` / `write` / `verify` / `activate` /
`prove_accepted` / `observe` / `cancel`) describing the lifecycle shape a
future, non-tmux transport would need — most concretely a ChatGPT-Web
browser adapter. **Nothing in this project calls through this Protocol
today** — `core.py`'s tmux pipeline is completely unchanged and remains
the only live path; every MCP tool and dashboard route keeps calling
`TerminalService.terminal_send_text(_granted)` directly, exactly as
before.

- `TmuxPromptTransport` is a thin, unused-in-production wrapper proving
  the Protocol actually fits the existing tmux implementation (its
  `activate`/`prove_accepted` honestly raise `NotImplementedError` with an
  explanation: the real tmux pipeline performs write+Enter+verification as
  one atomic, pane-locked operation specifically so nothing can interleave
  between them, which a split `activate()`/`prove_accepted()` call pair
  cannot preserve without re-implementing that locking here too — not
  worth the duplication for a proof-of-shape wrapper nothing calls).
- `ChatGptWebTransport` is a stub whose constructor always raises
  `NotImplementedError`. **No Playwright/browser-automation dependency
  exists anywhere in this project as a result of this file** — see
  `docs/chatgpt-web-adapter-plan.md` for the actual design that class
  would eventually need.

### P13 — dashboard feedback (`dashboard.py`, presentation only)

The send composer now shows a brief **"Đang gửi…"** ("Sending...") note
while a request is in flight, and — new — a persistent (non-auto-clearing)
note when a send's `delivery_state` comes back `DELIVERY_UNKNOWN`, so an
operator does not miss a genuine "did this actually run?" case merely
because the HTTP call itself returned `200`. A confirmed send
(`SUBMIT_CONFIRMED`) or a plain, nothing-to-confirm text append
(`TEXT_SENT`) still clears the note as before. No client-side retry was
added or changed — a retry is an ordinary, fresh `sendInput()` call, made
safe against an accidental double-submit by the existing
`idempotency_key` mechanism, not by any new frontend logic.

## Not done in this phase

- No `ask_chatgpt` MCP tool, no ChatGPT-Web browser automation, no
  Playwright/Electron dependency — see
  `docs/chatgpt-web-adapter-plan.md` for the design note this phase
  produced instead.
- `origin`/`trace_id`/`parent_turn_id` are not surfaced in any MCP tool
  schema or dashboard UI yet — schema and audit-log storage only, per the
  original request ("không cần expose UI nếu chưa cần").
- The `evidence` field's vocabulary stays intentionally coarse
  (`OUTPUT_CHANGED`/`TEXT_SENT`/`RECOVERY_ESCAPE_ENTER`) rather than the
  fuller `INPUT_CLEARED`/`AGENT_RUNNING`/`TURN_CREATED`/`PROMPT_ECHOED`
  taxonomy a browser-DOM-based adapter could eventually support — the
  current tmux-pane-content adapters have no way to distinguish those
  cases today, and inventing evidence codes that don't correspond to a
  real, checked signal would be worse than not having them.

---

## The acceptance contract (P1, 2026-09-15)

### Text delivered is not a submission

Three states, and they are not interchangeable:

| State | Means |
|---|---|
| `TEXT_SENT` | the bytes reached the composer. Nothing was submitted, and nothing is claimed. |
| `SUBMIT_CONFIRMED` | positive evidence ties **this** attempt to a real change of state in the target. |
| `DELIVERY_UNKNOWN` | Enter went out, the outcome is unproven. Never silently upgraded. |
| `SUBMIT_STALLED` | the prompt is still in the composer and the pane never moved. Specific, and retryable. |

**A pane diff is not evidence.** A Claude Code redraw, a spinner tick, or an
elapsed-timer update changes the pane without anything having been submitted.
Confirmation requires the adapter's own ack rule to pass.

### The pipeline, and the ordering that matters

```
PREPARE -> WRITE -> VERIFY_TEXT -> ACTIVATE -> PROVE_ACCEPTED -> TRACK
```

`VERIFY_TEXT` runs **before** `ACTIVATE`, not after. An activate-only send
(`terminal_send_text("", press_enter=True)`) into an empty composer has nothing
to submit, so the Enter is **withheld** — the call returns `DELIVERY_UNKNOWN`
with `submit_outcome: NOTHING_TO_SUBMIT`, `stage: VERIFY_TEXT` and
`activation_attempts: 0`.

Getting that ordering wrong is not theoretical. Measured live on `hp-linux`
(2026-09-15), before this change:

```
composer empty, agent idle
terminal_send_text("", press_enter=True)
  -> {"delivery_state": "SUBMIT_CONFIRMED", "submit_reason": null}
pane afterwards: byte-identical
```

and three consecutive activate-only calls during a running turn each returned
`SUBMIT_CONFIRMED` while the pane only ever showed
`Press up to edit queued messages`.

**Root cause.** `_sent_text_echoed` treats an empty `sent_text` as trivially
satisfied — correct, for a caller with genuinely nothing to attribute. An
activate-only send *does* have something to attribute: the composer's own
content. Passing `""` removed the busy-window guard entirely, and a spinner
tick was then sufficient to confirm. An activate-only send now attributes the
composer, the same rule the bare-Enter path already used.

### Claude vs Codex

Separated by adapter capability, never by a shared default:

- **Claude** — exactly one Enter. `ACTIVATION_ADAPTERS == {"claude"}` adds a
  cursor-move nudge before it, chosen because it cannot alter what is about to
  be submitted. Never a second Enter: two Enters submit twice.
- **Codex** — its multi-enter/debounce retry profile is gated on
  `adapter.name == "codex"` and is never applied to Claude. A test pins this so
  a later edit cannot widen it silently.

### Remote sends

A remote failure is classified rather than collapsed into one error string:

| State | Retryable | Means |
|---|---|---|
| `REMOTE_TIMEOUT` | yes | reached the node, no answer in time |
| `REMOTE_TRANSPORT_FAILED` | yes | the call itself did not complete |
| `REMOTE_NODE_UNREACHABLE` | yes | the node is not answering at all |
| `REMOTE_SESSION_GONE` | **no** | the node answered: no such session |
| `REMOTE_INPUT_DENIED` | **no** | the node answered: not permitted |
| `REMOTE_UNKNOWN` | **no** | answered, unrecognised — not knowing what happened is not evidence that retrying is safe |

`node_id` and `session` travel with the verdict, so a caller never has to guess
which target a failure belongs to. Prompt content never reaches this path.

### One pipeline, one vocabulary

`prompt_transport.py`'s `PromptTransport` Protocol names the same stages this
contract does, and nothing calls through it — `TmuxPromptTransport.activate()`
and `.prove_accepted()` raise `NotImplementedError`. The live path is `core.py`
alone, and the stage names in this document are the ones it emits. The dead
Protocol is tracked for retirement or implementation; until that is settled,
treat any state it declares that does not appear in the tables above as not
real.

### Troubleshooting

| Symptom | Read this |
|---|---|
| `SUBMIT_STALLED` | the prompt is still in the composer and nothing started. Safe to retry **while** the prompt is unchanged and the target has not begun working. |
| `DELIVERY_UNKNOWN` | bytes went out, outcome unproven. Inspect the pane before retrying — a retry may submit twice. |
| `NOTHING_TO_SUBMIT` | the composer was empty. No Enter was sent. Write the text first. |
| `SUBMIT_CONFIRMED` with `submit_reason` naming the composer | an activate-only send; the echo was attributed to the composer's content, not to an empty sent text. |
| `REMOTE_*` | see the table above; only the three transport classes are worth retrying. |

Audit metadata carried on every send: `submission_id`, `activation_attempts`,
`enter_count`, `evidence`, `stage`, `submit_latency_ms`, `submit_reason`.
None of them contain prompt text.
