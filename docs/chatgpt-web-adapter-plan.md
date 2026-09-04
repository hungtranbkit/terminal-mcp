# ChatGPT Web adapter — design note (not implemented)

**Status: design only.** No code in this repository implements any part
of this. No browser-automation dependency (Playwright, Selenium, or
otherwise) exists in `pyproject.toml`. This note exists so a future phase
has a concrete starting point, per the prompt-submission reliability
upgrade's own explicit instruction to prepare an extension point
(`terminal_mcp/prompt_transport.py`'s `PromptTransport` Protocol and
`ChatGptWebTransport` stub) without building the thing itself yet.

**See `docs/ask-chatgpt-bridge.md` for the concrete follow-on design** —
the `ask_chatgpt` semantic service, its own state machine, turn/capability
ownership, permission model, and phased rollout, built as an addition on
top of the `PromptTransport` extension point this note describes, not a
parallel design. This note stays as-is for the general "why later, not
yet" rationale and the reference-project mapping table; anything more
specific belongs in the newer document.

## Why this is a separate, later phase

Everything this repository does to Codex/Claude Code sessions today is
observation and control of a **local tmux pane** — a pty this process's
own user owns, with a clear owner, no third-party terms of service in
play, and years of hardening already behind it (see
`docs/prompt-submission.md`). A ChatGPT-Web adapter would instead be
driving a **real browser session against a hosted product's own web UI**
on the operator's behalf — a fundamentally different risk surface
(fragile DOM selectors that drift under any UI update, a real login
session held in the browser, a real ToS to respect, no pty/tmux semantics
to reuse at all). Bolting that onto the core before the tmux-side
reliability work was solid would have been a straightforward way to
destabilize a working system while adding a second, less mature one on
top — the acceptance criteria for the upgrade this note accompanies
explicitly ranked *stability* above *feature count*, which is why this
phase stops at the interface.

## Reference: `miuuyy/codex-chatgpt-web`

`https://github.com/miuuyy/codex-chatgpt-web` bridges ChatGPT's web UI
into Codex as a usable model backend without an API key. From its public
docs (fetched for this note — no code copied, no dependency added):

- **Turn-bound everything.** The project's own vocabulary is "turn-bound
  MCP token," "turn-bound browser tab," "turn-bound connector
  capability" — every request/response pair is tied to a specific,
  disposable binding, not a shared/ambient session. This is the same
  instinct this repository already applies via `correlation_id` +
  `idempotency_key` (see `docs/prompt-submission.md`), just at browser-tab
  granularity instead of tmux-pane granularity.
- **One task-bound Temporary Chat per sequential message stream**, with
  an explicit **compaction boundary**: "the retained agent writes the
  checkpoint before Codex starts a clean chat" once a conversation
  approaches a context limit — a deliberate, observable lifecycle
  transition rather than an unbounded, ever-growing thread.
- **"Attachment acceptance and send readiness are verified before the
  turn begins"** — i.e. a real readiness check gates the send action,
  conceptually the same role `AgentAdapter.can_submit_now` plays for a
  tmux target, adapted to whatever DOM/network signal a web composer
  exposes for "ready to accept this submission."
- **Fail-closed on UI drift, explicitly**: the README's own framing is
  that a UI change should "fail explicitly instead of silently switching
  model or transport" — the same posture this repository already applies
  everywhere (`DELIVERY_UNKNOWN` rather than a guessed success, refusing
  before sending when the target's state is ambiguous).
- Public docs do not describe the actual DOM selectors, plain-text vs.
  rich-text insertion mechanics, or the specific signals used to confirm
  a submitted message actually started a response turn — those are
  implementation details this note does not have and does not invent.

## Mapping onto `PromptTransport` (`terminal_mcp/prompt_transport.py`)

| Lifecycle stage | tmux transport (today, live) | ChatGPT Web transport (future, not implemented) |
|---|---|---|
| `prepare()` | Resolve session identity + `pane_current_command`, select an `AgentAdapter`. | Resolve/open the correct browser tab (a turn-bound tab, per the reference project's own model) for this specific task's Temporary Chat; confirm it is logged in and pointed at the composer, not some other page/dialog. |
| `write()` | `tmux.send_text(..., press_enter=False)` — literal bytes to the pty. | Insert the prompt as **plain text** into the composer's actual input element via its structural selector (never innerHTML/rich paste, which risks the target application's own formatting/markdown reinterpreting the content) — same "no raw-HTML insertion" discipline this project's own dashboard JS already follows for its own DOM writes, applied to a different DOM. |
| `verify()` | *(not a separate step — see docs/prompt-submission.md)* | **Read the composer's own current value back** immediately after insertion and diff it against what was sent, character-for-character — the direct browser equivalent of this project's own pre-Enter `typed_snapshot` baseline. A mismatch (autocomplete/autoformat mutated the text, a race with the page's own JS) is a `verify()` failure, refused before ever clicking send — never "close enough." |
| `activate()` | `tmux.send_keys(session, ["Enter"])`, gated on the two identity/command revalidation checks. | Click (or keyboard-trigger) the send control, gated on an explicit readiness check for that control — enabled/not-disabled, not obscured by an overlay, not mid-animation — the same spirit as "attachment acceptance and send readiness are verified before the turn begins" above. |
| `prove_accepted()` | `AgentAdapter.submit_ack_evidence` — adapter-specific pane-content evidence. | Requires **positive, structural** evidence the turn actually started: the composer clearing itself is necessary but never sufficient on its own (a client-side clear on click, before the network round-trip even completes, would false-positive); a new assistant-turn DOM node appearing, bound to this specific request (matching the reference project's own turn-binding discipline), is the closer analogue to this project's own `submit_ack_evidence`. |
| `observe()` | `status.py` classification of ongoing pane content. | Poll the bound turn's own state (generating / complete / errored) via whatever DOM/network signal the web UI exposes for it — this project's `identify_target_state`'s `running`/`waiting`/`final` vocabulary maps directly. |
| `cancel()` | Not supported (`TmuxPromptTransport.cancel()` returns `False` honestly). | A web UI's own "stop generating" control, if present, is a real cancel primitive tmux never had — worth implementing for real here, unlike the tmux side. |

## Fail-closed rules this adapter would inherit, unconditionally

The same golden rule this project already enforces
(`docs/prompt-submission.md`) applies without exception to a browser
transport too, and arguably matters *more* there:

- **Never resend prompt text past the activation-ambiguity boundary**
  unless there is positive evidence the first click/submit was not
  accepted. A ChatGPT Temporary Chat that silently received the same
  prompt twice because a network hiccup made a `verify()`/`prove_
  accepted()` call time out would be a strictly worse failure mode than
  this project's own tmux composer-swallow bug ever was — a duplicate
  message in a hosted conversation cannot be un-sent the way a stray
  keystroke in a local pty pane sometimes can be recovered from.
- **Fail closed on any UI drift**, per the reference project's own
  stated philosophy: if the composer's expected selector, the send
  control, or the turn-completion signal cannot be found/verified, this
  is `PROMPT_SUBMISSION_UNKNOWN` (or an outright refusal before writing
  anything), never a best-effort guess at an alternate element.
- **`max_agent_bridge_depth` (already implemented, `config.py`)**
  applies here directly: a Codex/Claude session asking this hypothetical
  adapter a question, whose answer then re-enters a Codex/Claude session,
  is exactly the one-hop bridge this depth guard already exists to bound.
  `origin="chatgpt"` and `SubmissionOrigin.child(...)` (already
  implemented, `prompt_transport.py`) are the metadata this future
  adapter would attach to that re-entry.
- **Never a raw `send_keys`-shaped capability** for this transport
  without the same `permissions.allow_send_keys`-style separation this
  upgrade already introduced for tmux — a `send_prompt`-only ChatGPT-Web
  permission (the `ask_chatgpt` permission concept the original request
  named, still unimplemented) should exist before any raw browser
  key-injection capability ever does, if one is ever added at all.

## What "done" would need to look like before this ships

1. A real, disposable-account-based integration test suite (this
   project's own established pattern: `tests/test_adapters_real_cli.py`
   runs against real, disposable CLI sessions, never mocked) — here,
   against a real, disposable ChatGPT Temporary Chat, gated the same way
   (`-m` marker, skipped by default, explicit opt-in, real cost/latency
   acknowledged).
2. `verify()`/`prove_accepted()` evidence rules proven against **at
   least** the same failure-mode catalogue this project already built
   for tmux (composer not accepting text, send control briefly disabled,
   a slow/delayed turn start, an already-in-progress turn, a UI dialog/
   overlay blocking the composer) — see `tests/test_send_reliability.py`
   and `tests/test_adapters.py` for the shape that catalogue should take.
3. An explicit security/ToS review — separate from and in addition to
   this project's existing security posture — before any credential or
   browser-session material for a third-party product is ever held or
   driven by this process.
4. Only after (1)–(3): a Playwright (or equivalent) dependency added,
   scoped to an optional extra rather than the core install, and a real
   `ChatGptWebTransport` implementation replacing today's
   `NotImplementedError` stub.

None of this is scheduled. This document exists so that when it is taken
up, the interface it plugs into (`PromptTransport`) and the discipline it
must uphold (the golden rule, fail-closed evidence, bounded bridge depth)
are already settled rather than re-litigated under time pressure.
