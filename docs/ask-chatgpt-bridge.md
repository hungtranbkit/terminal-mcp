# `ask_chatgpt` bridge — architecture (design only, not implemented)

**Status: design only**, same posture as `docs/chatgpt-web-adapter-plan.md`
(which this document extends and supersedes for anything more specific —
that note stays as the "why later, why not yet" rationale; this one is the
concrete shape). No code in this repository implements any part of this.
No browser-automation dependency exists in `pyproject.toml`. Nothing here
is wired into `core.py`, `mcp_app.py`, or `dashboard.py`.

This document exists because `terminal_mcp/prompt_transport.py`'s
`PromptTransport` Protocol and `ChatGptWebTransport` stub were built as a
deliberately narrow extension point (P10) — the *tmux submission*
lifecycle shape, not a full semantic service. `ask_chatgpt` needs more
than that shape: turn ownership, an expiring capability, a tool-round-trip
allowlist, and a browser session that must never leak into this process.
This document is that fuller design, built as an **addition on top of**
`prompt_transport.py`, never a parallel interface.

---

## 1. What already exists and what each piece becomes here

Nothing below is new *behavior* yet — this table maps existing, already-
implemented, already-tested primitives in this codebase onto the role
they would play in `ask_chatgpt`, so Phase A is mostly composition, not
invention.

| Existing piece | File | Role in `ask_chatgpt` |
|---|---|---|
| `PromptTransport` Protocol (prepare/write/verify/activate/prove_accepted/observe/cancel) | `prompt_transport.py` | The base shape `ChatGptBridgeTransport` (§4) extends — not replaces. `write`→`submit`'s text-insertion half, `activate`→`submit`'s send-click half, `prove_accepted`→`proveAccepted`, `observe`→`observe`, `cancel`→`cancel`. |
| `ChatGptWebTransport` stub | `prompt_transport.py` | Becomes the concrete (still `NotImplementedError` until Phase D) implementation of `ChatGptBridgeTransport`. |
| `SubmissionOrigin` (`origin`/`trace_id`/`parent_turn_id`/`depth`, `.child()`) | `prompt_transport.py` | Used verbatim as the loop-protection metadata carried on every `ask_chatgpt` call and on any response re-entering a tmux session (§6). No new depth/trace concept invented. |
| `max_agent_bridge_depth` + `AGENT_BRIDGE_DEPTH_EXCEEDED` fail-closed check | `config.py`, `core.py:969-971` (also `:1639-1641`) | Copied verbatim as `ask_chatgpt`'s own first gate — same constant, same error shape, same "checked before anything happens" placement. |
| `SupervisorV2Service`'s `claimed → decided → approved → sent → observing → {completed,blocked,rejected,failed}` state machine, `cas_update` (atomic `UPDATE ... WHERE state=?`), `_expire_stale_claim`, `DEFAULT_LEASE_SECONDS` | `supervisor2.py` | The direct template for the bridge's own state machine persistence and for turn-capability claim/expiry (§5, §6) — same CAS-on-state discipline, not a new concurrency primitive. |
| `PaneLeaseStore.acquire` (one atomic `INSERT ... ON CONFLICT ... WHERE`, idempotent re-acquire by the same owner, crash-safe TTL reclaim) | `lease.py` | The direct template for the bridge capability's expiring, idempotent, single-owner claim (§6). |
| `BindingStore` (named binding → session indirection) | `bindings.py` | `ask_chatgpt`'s `source_session_or_binding` input resolves through the exact same `terminal_get_binding`/binding-resolution path `terminal_send_bound` already uses — no second binding concept. |
| `AgentAdapter` (pure, stateless, per-target evidence rules; `DELIVERY_STATES`; `TARGET_STATES`) | `adapters.py` | Structural precedent for "the DOM/pane is never trusted as a stable API — every claim of success is a specific, adapter-owned evidence rule, everything else is `UNKNOWN`." The bridge's `proveAccepted()`/`observe()` follow the same discipline against browser signals instead of pane text. |
| `text_fingerprint` (sha256), `sanitized_preview` (redacted + truncated), `redact_text` (secret-shaped regex redaction) | `audit.py`, `redaction.py` | Reused unchanged for the bridge receipt (§9) — prompt/response are hashed+previewed, never stored/logged in full, by the same mechanism `input_audit` already uses for tmux sends. |
| `AuditStore` / `input_audit` table, `PRAGMA user_version` + `Migration` pattern | `audit.py`, `schema.py` | Template for the new `bridge_turns` table's own migration list — same file-per-store convention (0700 dir, 0600 file, WAL), not a new storage layer. |
| `PermissionsConfig` (`terminal_read`/`terminal_input`/`allow_send_keys`, each independently gateable) | `config.py:12-28` | Direct precedent for adding `ask_chatgpt: bool = False` as its own, independent field (§7) — never derived from `terminal_input`. |
| `SessionLifecycleConfig` (`enabled: bool = False`, opt-in dataclass, `__post_init__` invariant enforcement) | `config.py:130-167` | Structural template for the new `AskChatGptConfig` dataclass (§7, §11) — disabled by default, same posture. |
| MCP tool registration (`@server.tool()` functions, thin wrappers calling one `TerminalService` method) | `mcp_app.py` | Template for how `ask_chatgpt` gets registered as one more tool, once Phase B is real — not a new registration mechanism. |
| Dashboard read-only status pages (`/dashboard/api/supervisor`, `/dashboard/api/supervisor2`) | `dashboard.py` | Template for a future `/dashboard/api/bridge` status/diagnostics route (Phase F) — same shape, no new auth/CSRF pattern. |
| `webterm.py`'s "control channel is a small, purpose-built loopback protocol talking to one real OS process, never a raw socket exposed past this process" design | `webterm.py` | Structural precedent (not code reuse — different transport) for the sidecar's own control channel in §11: a narrow, typed, loopback-only protocol, not "expose the sidecar's socket." |

Nothing in this table requires touching `core.py`'s tmux send pipeline.
`TmuxPromptTransport`/`_send_text_and_verify_locked` stay exactly as they
are; `ask_chatgpt` is a sibling capability, not a modification of the
existing one.

---

## 2. What we learn from `codex-chatgpt-web`, and what we deliberately do differently

Re-fetched directly from the repo's public README for this document (no
code copied, no dependency added — same posture as the existing plan
note). What its docs actually say, mapped to what we take vs. don't:

| Reference project's stated mechanism | Take directly | Deliberately different here, and why |
|---|---|---|
| "One task-bound Temporary Chat"; "every available effort receives the same turn-bound MCP capability" | **Take**: one Temporary-Chat-equivalent per outer task, and one capability per turn, both disposable. | We don't call it an "MCP capability" the same way — theirs is a capability *for accessing ChatGPT's own connector/MCP surface*; ours (§6) is a capability *for calling our own `ask_chatgpt` tool and, if authorized, a small allowlisted set of our own tools on the way back* (§7). Different direction of trust. |
| "Sequential messages reuse one task-bound Temporary Chat. At the context boundary, the retained agent writes the checkpoint before Codex starts a clean chat; if that chat was closed, canonical history supplies the fallback." | **Take the shape**: a bounded lifetime per outer task, an explicit compaction/handoff boundary, not an ever-growing thread. | We do **not** implement the "retained agent writes a checkpoint" compaction step in Phase A–D at all — that is a second agent-orchestration feature layered on top of a working single-turn bridge, explicit feature creep for this design's stated priority (stability first). Left as a documented future extension, not built. |
| "Missing models, tools, or changed ChatGPT UI produce explicit errors instead of silently switching route or capability"; "no silent fallback" | **Take verbatim** — this is exactly this project's own existing golden rule (`docs/prompt-submission.md`) applied to a new surface, not a new principle. `FAILED`/`UNKNOWN` states (§5) exist specifically so nothing here ever guesses. | — |
| "Each automatic picker entry has one fixed ChatGPT mode … changing them cannot silently change the selected browser model" | **Take**: `mode`/`model`/`effort` in the `ask_chatgpt` input (§3) is either explicitly supplied or resolved from one fixed, config-declared default — never inferred from prompt content, never silently substituted if the requested one is unavailable (that is a `FAILED` with a named reason, not a fallback). | — |
| "Unexpected approval prompts fail closed unless `--auto-approve-tool-calls` is explicitly enabled; that option clicks Allow once, never a permanent grant"; connector must use "Allow all actions" rather than "Allow low-risk actions" | **Take the fail-closed-on-approval-prompt instinct** or an unexpected browser-side dialog is `UNKNOWN`/`FAILED`, never dismissed by guessing which button is "probably fine." | We do **not** adopt "Allow all actions"-style blanket connector trust. §7's tool-round-trip allowlist is the opposite instinct: an explicit, per-turn, minimal allowlist the *outer* Terminal MCP session controls, not a blanket grant the browser side negotiates with ChatGPT. This is the single biggest intentional divergence — their model trusts the connector broadly once approved; ours never trusts a "response asked to call tool X" instruction without the outer turn having pre-authorized X. |
| Separate Electron/browser profiles; dev mode gets its own isolated state directory ("separate Electron state, browser cookies/login, ChatGPT account, configuration") | **Take directly**: the sidecar (§11) gets its own OS-level profile directory, never shared with anything else on the host, and this project's core process never reads it. | We do not build an Electron app — a headless/headed Playwright browser context under a dedicated profile directory is sufficient and matches this project's existing "no unnecessary dependency" discipline (`docs/chatgpt-web-adapter-plan.md` already ruled this line). |
| Concurrency: "up to five visible task-bound browser tabs" | **Take the shape** (a small, fixed, configured concurrency bound with a queue past it), not the number — §11 leaves the exact bound operator-configurable, default conservative (1–2), since this project has no evidence yet for what this host can sustain. | — |
| Opt-in `CODEX_CHATGPT_WEB_BROWSER_DIAGNOSTICS=1` env var, scope of what it logs unclear from public docs | **Take the opt-in-only instinct**: diagnostics are off (fingerprint+preview only) by default, and even an explicit opt-in never logs full prompt/response text automatically (§9) — deliberately stricter than "unclear scope," because this project's own existing `redact_text`/`sanitized_preview` discipline already sets that bar for every other input path. | — |
| Public docs do not describe actual DOM selectors, specific readiness-verification mechanics, or the concrete turn-start evidence beyond "SSE streaming" | **Cannot take** — nothing to take. §4/§11 name these as the literal Phase D unknowns; they are not invented here either, exactly as `docs/chatgpt-web-adapter-plan.md` already stated. | — |

---

## 3. `ask_chatgpt`: a semantic service, not a browser-control primitive

`ask_chatgpt` is one new MCP tool (Phase B) backed by one new
`TerminalService`-sibling method — **not** a way for a Codex/Claude
session to drive a browser or open a raw WebSocket itself. The distinction
matters structurally: every existing input path in this project
(`terminal_send_text`, `terminal_send_keys`, `terminal_send_bound`) is a
*generic* capability an authorized caller aims at any whitelisted target.
`ask_chatgpt` is the opposite shape on purpose — one fixed target (this
deployment's own bridge/sidecar), a narrow, typed input, and no way for
the caller to supply anything resembling a selector, a raw key sequence,
or a URL. The permission that gates it (§7) is deliberately **not**
`terminal_input`-derived for the same reason `allow_send_keys` is already
independent of `terminal_input` (config.py:15-28): "can send text to a
tmux pane" must never imply "can drive a browser against a third-party
product," even though both are, mechanically, "sending text somewhere."

### Proposed input schema

```
ask_chatgpt(
    source_session: str | None = None,      # exactly one of these two
    binding: str | None = None,              # required (XOR) -- resolved
                                              # through BindingStore, same
                                              # as terminal_send_bound
    prompt: str,                             # required, non-empty,
                                              # length-capped (reuses
                                              # input_policy.max_text_length
                                              # as the starting bound)
    trace_id: str | None = None,             # SubmissionOrigin field
    parent_turn_id: str | None = None,       # SubmissionOrigin field
    depth: int = 0,                          # SubmissionOrigin field --
                                              # checked against
                                              # max_agent_bridge_depth
                                              # BEFORE anything else (§6)
    mode: str | None = None,                 # explicit ChatGPT "mode"
                                              # (config-declared enum);
                                              # None resolves to the one
                                              # configured default, never
                                              # inferred from `prompt`
    model: str | None = None,                # same explicit-or-configured-
                                              # default rule as `mode`
    effort: str | None = None,               # same rule
    deliver_to: dict | None = None,          # optional: {"binding": ...}
                                              # or {"session": ...} -- see
                                              # §8. None (default) means
                                              # "return the result as this
                                              # tool call's own return
                                              # value," never an implicit
                                              # re-send anywhere.
    timeout_seconds: float,                  # required, bounded
                                              # [config min, config max]
    idempotency_key: str,                    # required (not optional,
                                              # unlike terminal_send_text's
                                              # optional one) -- see §6,
                                              # this is what makes a retry
                                              # of the exact same outer
                                              # call safe by construction
) -> dict
```

`source_session`/`binding` being mutually exclusive-and-required (not
"either or neither, whatever") is deliberate: `ask_chatgpt` always has
exactly one calling session it is answering to, because that identity is
what the capability (§6) binds to and what a `deliver_to` re-entry (§8)
targets. `idempotency_key` being **required**, not optional like
`terminal_send_text`'s, is deliberate too — the reference project's own
strongest warning (a duplicate message in a hosted, non-recoverable
conversation being strictly worse than a stray local keystroke,
`docs/chatgpt-web-adapter-plan.md` §"Fail-closed rules") means this
surface should never offer an easy way to accidentally omit the one
mechanism that makes a client-side retry safe.

`mode`/`model`/`effort` are three independent, optional-with-a-named-
default fields, never a single "profile" string — matches "each automatic
picker entry has one fixed ChatGPT mode" from §2: a caller can pin any
subset explicitly, and whichever ones are omitted resolve to *this
deployment's own configured default for that field specifically*, which
is itself named in the receipt (§9) so "what was actually used" is never
ambiguous after the fact.

---

## 4. Interface: extending `PromptTransport`, not replacing it

`prompt_transport.py` gains a second Protocol, `ChatGptBridgeTransport`,
structurally derived from `PromptTransport` but with the turn/session
concepts `PromptTransport`'s tmux-shaped methods don't need:

```python
class ChatGptBridgeTransport(Protocol):
    """Extends PromptTransport's prepare/write/verify/activate/
    prove_accepted/observe/cancel shape (unchanged, still the base
    vocabulary) with the turn-lifecycle and response-collection concepts
    a hosted, stateful, third-party conversation needs and a local tmux
    pane never did. A concrete implementation (ChatGptWebTransport,
    Phase D) composes an inner PromptTransport-shaped object for the
    write/verify/activate/prove_accepted steps -- this Protocol is the
    outer turn lifecycle around it, not a competing vocabulary."""

    def prepareTurn(self, context: BridgeTurnContext) -> BridgeTurnHandle:
        """Resolve (open or reuse) the task-bound Temporary Chat for this
        outer task, per the compaction-boundary rule in docs/prompt-
        submission.md's golden-rule spirit: never silently reuse a DIFFERENT
        task's chat. Corresponds to PromptTransport.prepare(), widened."""

    def submit(self, handle: BridgeTurnHandle, prompt: str,
               metadata: SubmissionOrigin) -> BridgeSubmission:
        """write() + verify() (read-back integrity) + activate(), as one
        sequenced operation with the SAME never-resend-past-ambiguity rule
        prompt_transport.py's module docstring already states -- this
        method either returns a BridgeSubmission with state ACCEPTED/
        FAILED, or raises/returns UNKNOWN; it never internally retries a
        submit whose first attempt's outcome is unproven."""

    def proveAccepted(self, submission: BridgeSubmission) -> bool:
        """Positive, structural evidence only (§5's ACCEPTED gate) --
        composer-cleared-on-click is necessary but never sufficient alone,
        exactly per docs/chatgpt-web-adapter-plan.md's existing mapping
        table row for this stage."""

    def observe(self, submission: BridgeSubmission) -> str:
        """One of the bridge STATE values (§5), polled -- the direct
        analogue of PromptTransport.observe()/status.py's
        identify_target_state, aimed at the composer/turn's own DOM or
        network signal instead of pane text."""

    def collectResponse(self, submission: BridgeSubmission) -> BridgeResponse:
        """Only callable once observe() reports COMPLETED. Returns the
        full response text ONLY to the caller that holds the matching
        capability (§6) -- this method itself does no redaction; that is
        the bridge SERVICE's job (§9) before anything is persisted/logged,
        never the transport's."""

    def cancel(self, submission: BridgeSubmission) -> bool:
        """Best-effort real cancel via the web UI's own stop-generating
        control, where present -- unlike TmuxPromptTransport.cancel()
        (honestly `False`, tmux has no such primitive), this one CAN be
        real (§2's "worth implementing for real here" note)."""

    def close(self, handle: BridgeTurnHandle) -> None:
        """Releases whatever the sidecar holds for this turn (closes the
        tab, drops the Temporary Chat reference) -- always called on
        COMPLETED, FAILED, CANCELLED, or capability-expiry revocation
        (§6), never left to a browser-side timeout."""
```

`BridgeTurnContext`/`BridgeTurnHandle`/`BridgeSubmission`/`BridgeResponse`
are new dataclasses in `prompt_transport.py`, sized the same minimal way
`SubmissionOrigin`/`SubmissionReceipt` already are. `ChatGptWebTransport`
(today's stub) becomes the Phase D implementation of this Protocol; its
constructor keeps raising `NotImplementedError` until then — Phase A–C add
everything *around* it (state machine, capability, permission, mock
transport) without touching that stub's behavior.

---

## 5. State machine

```
PREPARING -> COMPOSER_READY -> WRITING -> VERIFIED -> ACTIVATING -> ACCEPTED -> RESPONDING -> COMPLETED
   |              |               |          |            |
   +--------------+---------------+----------+------------+---> FAILED
   |                                                              |
   +-> UNKNOWN  (ambiguous evidence at ACTIVATING/ACCEPTED only)  |
   |                                                              |
   +-> CANCELLED (explicit cancel(), any state before COMPLETED) -+
```

Transition rules, each corresponding to a real `ChatGptBridgeTransport`
call:

| From | Call | To (success) | To (failure) |
|---|---|---|---|
| — | `prepareTurn` | `PREPARING` → `COMPOSER_READY` once the Temporary Chat/tab is resolved and the composer is confirmed present+enabled | `FAILED` (`reason=PREPARE_FAILED`) — never silently retried with a *different* chat/tab |
| `COMPOSER_READY` | `submit` (write half) | `WRITING` → `VERIFIED` once read-back matches sent text exactly | `FAILED` (`reason=VERIFY_MISMATCH`) — this is the browser equivalent of the plan doc's §"verify()" row; a mismatch is refused **before** any click, so there is nothing ambiguous to recover from yet |
| `VERIFIED` | `submit` (activate half) | `ACTIVATING` → (via `proveAccepted`) `ACCEPTED` | `FAILED` if the send control never became ready within `timeout_seconds`; **`UNKNOWN`** if the click/keyboard-trigger happened but `proveAccepted` found no positive evidence within the window — this is the activation-ambiguity boundary; see the golden rule below |
| `ACCEPTED` | `observe` (polled) | `RESPONDING` → `COMPLETED` once the bound turn's own state reports finished | `FAILED` if the web UI itself reports an error for this turn (rate limit, disconnected, refused) — a *server-side* failure, distinct from an evidence gap |
| any pre-`COMPLETED` state | `cancel` | `CANCELLED` | — |
| — | capability expiry (§6) mid-flight | `CANCELLED` (`reason=CAPABILITY_EXPIRED`) — `close()` still runs | — |

**Golden rule, restated for this transport** (already the rule in
`prompt_transport.py`'s module docstring and
`docs/chatgpt-web-adapter-plan.md`'s "fail-closed rules" section — not a
new rule, just made state-machine-explicit here): once `submit`'s
activate half has executed the click/keyboard-trigger, **no caller of
this service may cause a second `submit` for the same
`idempotency_key`** unless the state is observably `FAILED` with a reason
that positively proves the first activation never reached the server
(e.g. `SEND_CONTROL_NEVER_ENABLED`, caught *before* any click attempt). A
result of `UNKNOWN` is terminal for that idempotency key from this
service's point of view — the caller gets `UNKNOWN` back and must start a
genuinely new call (new `idempotency_key`) if they want to try again,
exactly like a human would open a new message rather than trust a
"maybe it sent" state. This is enforced by the capability/idempotency
store (§6), not by transport-layer good behavior alone — a second
`ask_chatgpt` call with the same `idempotency_key` **always** returns the
stored receipt for the original attempt, whatever state it is in, and
**never** re-invokes `submit`.

---

## 6. Turn ownership / capability

Modeled directly on `supervisor2.py`'s claim/CAS pattern and
`lease.py`'s atomic idempotent-acquire, in one new table
(`bridge_turns`, same db-file-per-store convention, own `Migration` list,
0700/0600, WAL):

```
bridge_turns(
    bridge_turn_id TEXT PRIMARY KEY,     -- generated server-side, never
                                          -- caller-supplied
    idempotency_key TEXT NOT NULL UNIQUE,-- the CALLER's key; UNIQUE is
                                          -- what makes claim() idempotent
    source_session TEXT,                 -- resolved from source_session
    binding TEXT,                        -- OR binding, whichever was given
    trace_id TEXT,
    parent_turn_id TEXT,
    depth INTEGER NOT NULL,
    allowed_tools TEXT NOT NULL,         -- JSON array, §7 -- frozen at
                                          -- claim time, never widened later
    mode TEXT, model TEXT, effort TEXT,  -- resolved (post-default) values
    state TEXT NOT NULL,                 -- §5's states
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,            -- fixed TTL from claim time,
                                          -- same fixed-TTL-no-renewal-
                                          -- thread posture as PaneLeaseStore
    prompt_sha256 TEXT NOT NULL,         -- text_fingerprint(prompt)
    prompt_preview TEXT NOT NULL,        -- sanitized_preview(prompt)
    response_sha256 TEXT,                -- filled on COMPLETED
    response_preview TEXT,
    error_stage TEXT,
    revoked_at TEXT                      -- non-null once explicitly closed
)
```

- **`claim(idempotency_key, ...)`**: one atomic
  `INSERT ... ON CONFLICT(idempotency_key) DO NOTHING` (simpler than
  `PaneLeaseStore.acquire`'s `DO UPDATE`, since a `bridge_turn_id` is
  never reassigned to a different owner the way a pane lease can be
  reclaimed) — if the row already existed, `claim()` returns the
  *existing* row (idempotent replay, §5's golden rule), never a new one.
- **Binding**: `source_session`/`binding` + `trace_id` + `depth` +
  `allowed_tools` are captured **once**, at claim time, from the outer
  caller's own identity/context — never re-derived later, and never
  reusable by a *different* session even with the same `bridge_turn_id`
  (there is no "transfer ownership" operation).
- **Expiry**: `expires_at` is a fixed TTL sized like
  `DEFAULT_LEASE_SECONDS`/`create_ready_timeout_seconds` precedent
  (config-driven, generous over the real worst-case turn time, no
  renewal thread). A background sweep (same shape as
  `_expire_stale_claim`, run from `terminal_bridge_status`/an
  equivalent poll, not a new thread) transitions any `bridge_turns` row
  still non-terminal past `expires_at` to `CANCELLED
  (reason=CAPABILITY_EXPIRED)` and calls `close()` on the transport
  handle if one was ever opened.
- **Revoke on completion/abort/timeout**: `close()` (§4) always runs
  exactly once per claimed turn — on the success path (after
  `COMPLETED`), on any `FAILED`/`CANCELLED` transition, and from the
  expiry sweep above. `revoked_at` being set is what makes a second
  `close()` call a safe no-op (mirrors `WebTerminalProcess.close()`'s own
  `self._closed` idempotency guard, `webterm.py:146-148`).
- **Never reused across sessions**: `source_session`/`binding` on the row
  is checked on every state-observing/response-collecting call — a
  caller presenting a `bridge_turn_id` that does not match their own
  resolved session/binding identity gets `FORBIDDEN`, not the turn's
  data. (Same shape as `_read_authorized_with_grant`'s per-session
  identity check, `core.py`, applied to a bridge turn instead of a tmux
  pane.)

---

## 7. Permission model

**New, independent field** — `PermissionsConfig.ask_chatgpt: bool = False`
(config.py:12-28's exact pattern: a plain, independently-defaulted bool
next to `terminal_read`/`terminal_input`/`allow_send_keys`, never derived
from any of them). Default `False` matches every other opt-in capability
this project has added (`terminal_input`, `session_lifecycle.enabled`,
`dashboard.web_terminal_enabled`) — a fresh deployment's behavior is
unchanged until an operator explicitly turns this on.

```python
@dataclass(frozen=True)
class PermissionsConfig:
    terminal_read: bool = True
    terminal_input: bool = False
    allow_send_keys: bool = True
    ask_chatgpt: bool = False   # NEW -- independent of the three above
```

`require_ask_chatgpt(config)` (new function in `core.py`, same shape as
the existing `require_read`/`require_session_lifecycle` helpers) is the
one gate every code path into this feature checks first, before even
`max_agent_bridge_depth` — a deployment with this off has zero surface
area for the whole feature, not just a blocked tool call.

### Tool-round-trip allowlist (§7's other half)

If a ChatGPT response can itself request a Terminal MCP tool call back
(a real capability only if Phase E is ever built — §10), that call is
checked against `bridge_turns.allowed_tools`, **frozen at claim time**
from a small, config-declared allowlist
(`AskChatGptConfig.round_trip_allowed_tools`, default **empty tuple** —
no tool round-trip capability exists until an operator both enables the
feature globally AND names specific tools). `terminal_send_keys` is
**never** eligible for this allowlist regardless of config — enforced in
code (not just documented), the same way `allow_send_keys` already exists
specifically so raw key-injection can be independently disabled; a
capability born from a browser-driven, third-party-hosted conversation is
exactly the caller this project should trust *least* with raw keys. This
is the direct answer to §2's biggest intentional divergence from the
reference project's "Allow all actions" connector trust model — the
allowlist is opt-in, per-turn, minimal-by-default, and structurally
excludes the one capability (`send_keys`) most likely to matter if it
were ever abused.

---

## 8. Loop protection

Entirely reuses existing, already-implemented metadata — no new concept:

- Every `ask_chatgpt` call requires a `depth` (§3); `depth >
  config.max_agent_bridge_depth` is refused
  (`AGENT_BRIDGE_DEPTH_EXCEEDED`) **before** `claim()` is ever called —
  identical placement/shape to `core.py:969-971`'s existing check on
  `terminal_send_text`.
- A response re-entering a tmux session (`deliver_to`, §9) is sent via
  `terminal_send_text(..., origin="chatgpt", trace_id=..., parent_turn_id=
  bridge_turn_id, depth=depth+1)` — literally `SubmissionOrigin.child(
  origin="chatgpt", turn_id=bridge_turn_id)`'s already-implemented depth-
  increment-never-reset logic (`prompt_transport.py:84-93`), reused
  unchanged.
- **New**: self-loop/cycle detection — `claim()` additionally refuses
  (`CYCLE_DETECTED`, fail-closed, never a guessed "probably fine") a
  request whose `trace_id` matches a **currently non-terminal**
  `bridge_turns` row already bound to the *same* `source_session`/
  `binding`. This catches the shape `max_agent_bridge_depth` alone
  cannot: not a deepening chain, but the same session re-asking mid-turn
  (a bug, not a deep chain, still worth refusing outright rather than
  silently allowing two concurrent asks under one trace).

---

## 9. Diagnostics / receipt

Minimal receipt returned by `ask_chatgpt` and persisted in `bridge_turns`
(§6's columns already carry all of this — no separate table needed):

```
bridge_turn_id, trace_id, source_session (or binding), state,
mode, model, effort,
acceptance_evidence: bool,            # did proveAccepted() find positive evidence
activation_attempts: int,             # always 0 or 1 -- see §5's golden rule;
                                       # never allowed to be >1 for one bridge_turn_id
created_at, prepared_at, written_at, verified_at,
activated_at, accepted_at, completed_at,  # each null until reached
response_length: int | None,
response_sha256: str | None,          # text_fingerprint, never the text itself
error_stage: str | None,              # which §5 transition failed, if any
```

**Never logged/persisted by default**: the full prompt or the full
response. Both go through `sanitized_preview`/`text_fingerprint`
(`audit.py`, reused unchanged) exactly like every existing
`input_audit` row already does for tmux sends — this is not a new
redaction mechanism, it is the existing one applied to a new table. A
future, explicit, config-gated `diagnostics_verbose` flag (mirroring the
reference project's opt-in `CODEX_CHATGPT_WEB_BROWSER_DIAGNOSTICS=1`, §2)
could someday store more, but **is not part of any phase in this design**
— naming it here only so it is not silently added later without a
decision.

**Cookies, tunnel ids, and any sidecar-internal browser state are never
in this table, never in any log line this core process writes, and never
readable by this core process at all** (§11) — there is nothing to
redact because the core process never receives them in the first place.

---

## 10. Phased rollout

| Phase | Scope | Depends on |
|---|---|---|
| **A** | `prompt_transport.py`: `ChatGptBridgeTransport` Protocol + dataclasses. New `bridge.py`: `bridge_turns` store (§6) + state machine (§5) + `require_ask_chatgpt` gate (§7) + loop protection (§8), all driven by a `MockBridgeTransport` (deterministic, in-process, no browser, no network — the Phase A/B analogue of `TmuxPromptTransport`) that can be told to simulate every state/failure in §5's table on command. Config: `PermissionsConfig.ask_chatgpt`, new `AskChatGptConfig` dataclass. Tests: §12, the ones markable "no browser". | Nothing external — pure composition of existing primitives (§1). |
| **B** | `ask_chatgpt` MCP tool registered in `mcp_app.py`, backed by a real `TerminalService`-sibling method in `core.py` (or a new `BridgeService` composed by `TerminalService`, mirroring how `SessionLifecycleService`/`SupervisorService` are composed today) — end-to-end through the real MCP tool-call path, still against `MockBridgeTransport`. This is the point `ask_chatgpt` becomes visible in real MCP tool discovery (STDIO/HTTP/tunnel) for the first time. | Phase A. |
| **C** | Browser sidecar **prototype**: separate process, own profile dir, loopback-only control channel (§11), login flow, composer-presence smoke check only — no submission, no evidence rules yet. Proves process isolation and the control channel shape before any submission logic is written against it. | Phase A (the sidecar talks `ChatGptBridgeTransport`-shaped requests over the control channel), independent of Phase B. |
| **D** | Real `ChatGptWebTransport`: actual Temporary-Chat-equivalent submission, read-back `verify()`, `proveAccepted()`/`observe()` evidence rules built against the real failure-mode catalogue (§12's browser-specific tests) — this is where `pyproject.toml` gains an **optional extra** (`playwright`, never core install) and the `NotImplementedError` stub is finally replaced. | Phase C, plus the security/ToS review `docs/chatgpt-web-adapter-plan.md` §"done" item 3. |
| **E** | Tool round-trip / capability broker (§7's allowlist enforcement path) — **only if a concrete need is demonstrated**, not built speculatively. | Phase D live and stable. |
| **F** | Dashboard: minimal read-only `/dashboard/api/bridge` status (active/recent `bridge_turns`, redacted previews only, same auth/CSRF pattern every other dashboard route already uses) — no new dashboard action initiates a turn; observation only. | Phase B (data exists) — can land any time after B, does not need D. |

Nothing past **Phase A** is implied by this design turn. Phase A itself
is not started by this document either — per the task's own instruction,
this is the design, not the patch.

---

## 11. Browser adapter (Phase C/D design, not built)

- **Separate process**, not a thread or import inside `terminal-mcp-http`
  or the STDIO server — a crash, hang, or memory blowup in a real browser
  automation stack must never be able to take down tmux observation/
  control, which has years of hardening behind it and zero reason to
  share a fault domain with a brand-new, much less mature subsystem
  (`docs/chatgpt-web-adapter-plan.md`'s own stated risk framing).
- **Own OS-level browser profile directory**, created and owned by the
  sidecar process alone, never shared with any other browser use on the
  host, never read by the core process (§9's "nothing to redact because
  it's never received" is enforced structurally here, not just by
  policy).
- **Control channel**: a small, typed, loopback-only protocol between
  core and sidecar (the concrete transport — local Unix socket vs.
  loopback HTTP/WS — is a Phase C implementation choice, not decided
  here; either way, never routed through `terminal-mcp-tunnel.service` or
  `cloudflared-terminal-mcp-dashboard.service`, and never sharing a port
  or process with either). The messages on it are exactly
  `ChatGptBridgeTransport`'s methods (§4), nothing more — no generic
  "run this JS" or "click this selector" escape hatch, matching
  `webterm.py`'s own "the only tmux subcommand this module can invoke is
  attach-session" discipline (`webterm.py:13-16`) applied to browser
  actions instead of tmux ones.
- **One Temporary-Chat-equivalent per outer task**, never reused across
  a *different* outer task (§2's cross-turn-leakage concern) — the
  sidecar, not the core process, is the source of truth for which
  browser tab/chat maps to which `bridge_turn_id`.
- **Bounded concurrency**: a small, config-driven max concurrent tabs
  (default conservative — §2 explicitly declines to copy the reference
  project's "five" without evidence this host should sustain the same),
  with a FIFO queue past that bound; a queued `ask_chatgpt` call sits in
  `PREPARING` until a slot frees, never silently drops or spawns past the
  configured bound.
- **Fail-closed on selector/UI drift, unconditionally**: any expected
  structural element (composer, send control, turn-completion signal)
  not found/verified is `UNKNOWN` or an outright pre-submission refusal —
  never a best-effort fallback to a different element. This is
  `AgentAdapter`'s own discipline (`adapters.py`: every evidence method
  is `False`/`UNKNOWN` unless positively proven) transplanted to a DOM
  instead of pane text, and is the literal restatement of this task's
  own final instruction: **the DOM is never treated as a stable API.**

---

## 12. Response path (§8 in the task's own numbering)

Two delivery modes, both from the **same** completed `BridgeResponse` —
never two different code paths that could diverge:

1. **Default — direct tool result.** `ask_chatgpt` is a normal,
   synchronous-shaped MCP tool call (like every other tool in this
   project): the caller's `ask_chatgpt(...)` invocation blocks (bounded
   by `timeout_seconds`) and its return value **is** the receipt (§9)
   plus the response text. This is correct and sufficient whenever the
   calling agent (Codex/Claude, via MCP tool call) is itself waiting on
   the result — which is the expected, default shape.
2. **Explicit opt-in — re-entry into a tmux session.** Only when the
   caller supplies `deliver_to: {"session": ...}` or `{"binding": ...}`
   (§3) — meaning the semantic flow specifically wants the response
   *injected into a different, ongoing pane* rather than returned to the
   MCP caller directly (e.g. a supervised/asynchronous flow where the
   original asker is not the one polling). This path calls the existing,
   unmodified `terminal_send_text` (or `terminal_send_bound`) with the
   `origin="chatgpt"`/incremented-depth metadata from §8 — reusing the
   real, already-hardened send-and-verify pipeline exactly as-is,
   **never** a new injection mechanism. Because `terminal_send_text`
   already enforces the golden rule (never resend past its own
   activation-ambiguity boundary) for *that* delivery, and `ask_chatgpt`
   independently enforces it for the ChatGPT-side submission (§5), there
   is no path in this design where a duplicate can arise from response
   delivery — each hop owns its own non-duplication guarantee, not a
   guarantee threaded through both.

No implicit resend loop exists anywhere: a failed §12-mode-2 delivery
(the tmux target vanished, was denied, etc.) is reported back in the
`ask_chatgpt` receipt as `error_stage=DELIVERY_FAILED` — the response
text itself is **not** lost (it is already in the receipt/§9 table), but
this design does not auto-retry delivery, since a retry policy for "the
ChatGPT call succeeded but re-injecting its answer failed" is a separate,
not-yet-designed decision, not something to improvise here.

---

## 13. Tests needed (mapped to phase)

All Phase A/B tests run with no network/browser dependency
(`MockBridgeTransport`), matching this project's existing split between
fast, always-run tests and the explicitly-marked, opt-in
`tests/test_adapters_real_cli.py`-style real-target tests.

**Phase A/B (deterministic, `MockBridgeTransport`):**
- Idempotent retry: two `ask_chatgpt` calls with the same
  `idempotency_key` return the identical receipt; `submit()` is invoked
  by the mock **exactly once**.
- Capability expiry: a claimed turn past `expires_at` is swept to
  `CANCELLED (CAPABILITY_EXPIRED)`; `close()` is called exactly once.
- Explicit `revoke`/abort path calls `close()` exactly once, is itself
  idempotent (second revoke is a no-op, not an error).
- Permission denied: `ask_chatgpt` with `permissions.ask_chatgpt=False`
  refuses before `claim()` — no `bridge_turns` row is created at all.
- Depth exceeded: `depth > max_agent_bridge_depth` refuses
  (`AGENT_BRIDGE_DEPTH_EXCEEDED`) before `claim()`.
- Cycle detected: a second call with the same `trace_id` + same
  `source_session`/`binding` while the first is still non-terminal is
  refused (`CYCLE_DETECTED`).
- Activation-ambiguous → `UNKNOWN`, never a duplicate: mock reports an
  ambiguous `proveAccepted()`; a same-`idempotency_key` retry returns the
  stored `UNKNOWN` receipt, `submit()` still invoked exactly once total.
- Outer-turn tool allowlist enforcement (once §7's round-trip exists,
  Phase E-adjacent but the allowlist *check itself* is testable in A/B
  with a mock caller): a tool not in `allowed_tools` is refused
  regardless of what the mock "response" requests.
- Secrets not logged: construct a prompt containing a recognizable
  secret shape (matching `redaction.py`'s own existing patterns) and
  assert the persisted `bridge_turns` row contains neither the secret
  nor the raw prompt — only `prompt_sha256`/redacted `prompt_preview`.
- Response bound to the correct source session/binding: a second
  session/binding presenting a valid-looking `bridge_turn_id` it does
  not own is refused (`FORBIDDEN`), never served the response.
- Bounded concurrency/queue: N+1th concurrent `ask_chatgpt` call (mock
  configured to never complete) sits in `PREPARING` until a slot frees,
  never exceeds the configured bound, never silently drops.
- Explicit model/effort selection: an unavailable `mode`/`model`/`effort`
  is `FAILED` with a named reason, never silently substituted; an
  omitted field resolves to the configured default and that resolved
  value appears in the receipt.

**Phase C/D (real sidecar, explicitly marked/opt-in, disposable account —
same posture as `tests/test_adapters_real_cli.py`):**
- Selector/UI drift fail-closed: a deliberately-broken selector (test
  fixture) produces `FAILED`/`UNKNOWN`, never a guessed alternate
  element, never a crash that leaks state past `close()`.
- Send-button not ready: submission refused/`UNKNOWN` while the control
  is disabled/obscured, never a forced click.
- Prompt read-back mismatch: `verify()` catches a simulated
  autocomplete/autoformat mutation before any click.
- Temporary Chat isolation: two sequential outer tasks never see each
  other's chat history/composer state.
- Sidecar crash/reconnect: core process detects a dead sidecar
  connection, marks in-flight turns `UNKNOWN`/`FAILED` (never silently
  hangs on `timeout_seconds`), and a fresh `ask_chatgpt` call after
  sidecar restart works without requiring a core-process restart.

---

## 14. Security boundaries (summary)

- `permissions.ask_chatgpt` (default `False`) is the single global
  on/off switch, independent of every other permission.
- No caller can supply a selector, raw browser command, or arbitrary
  URL — the input schema (§3) has no field shaped like one.
- `terminal_send_keys` is structurally excluded from the round-trip
  allowlist (§7), not just excluded by default configuration.
- The allowlist itself is frozen per-turn at claim time (§6) — a
  response cannot expand its own authority mid-turn.
- Cookies/browser-session/tunnel material never cross the sidecar→core
  boundary (§11) — nothing to leak because nothing is received.
- Full prompt/response text is never persisted/logged by default (§9) —
  same `redact_text`/`sanitized_preview`/`text_fingerprint` mechanism
  every other input path already uses.
- The public OpenAI MCP tunnel and the Cloudflare dashboard tunnel never
  route to the sidecar's control channel (§11) — it is loopback-only,
  full stop.
- Every failure mode this document names resolves to `FAILED`/`UNKNOWN`/
  a named error, never a guess, never an automatic retry past the
  activation-ambiguity boundary (§5), never an implicit resend on
  delivery failure (§12).

---

## 15. Blockers / unknowns

- **Actual DOM structure**: unknown and unknowable from public docs
  alone (§2) — Phase D requires directly inspecting a real ChatGPT
  session's composer/send-control/turn-completion markup, which this
  design deliberately does not invent or guess at.
- **Concurrency bound this host can actually sustain**: no evidence yet
  (§2, §11) — left operator-configurable with a conservative default
  rather than copying the reference project's number.
- **ToS/security review** (`docs/chatgpt-web-adapter-plan.md` §"done"
  item 3): not started, blocks Phase D regardless of how ready the code
  is.
- **Whether Phase E (tool round-trip) is ever actually needed**:
  genuinely open — this design specifies its security boundary (§7) so
  that *if* it's needed the shape is already settled, but building it
  speculatively would be exactly the feature creep this task's own
  priorities rule out.
- **Compaction/checkpoint handoff** (the reference project's
  retained-agent-writes-a-checkpoint mechanism, §2): explicitly out of
  scope for every phase in this document — named here only so a future
  reader knows it was considered and deferred, not overlooked.
