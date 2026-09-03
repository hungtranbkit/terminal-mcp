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
