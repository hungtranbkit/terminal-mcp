# Terminal MCP — Living Requirements / Feature Matrix

**This is the ONE canonical, living spec for Terminal MCP.** An agent
(human, Claude, ChatGPT, or any Coordinator-dispatched worker) reads
this file FIRST, before touching code, to know what the system already
has, its real current state, and where the actual gaps/backlog are.
Deep-dive topic docs (`docs/multi-node.md`, `docs/prompt-submission.md`,
`docs/ask-chatgpt-bridge.md`, `docs/tunnel-connection-reliability.md`,
`docs/chatgpt-web-adapter-plan.md`) still exist for implementation
detail; this file is the index and the one place that must stay
current. Do not create a second requirements/feature-matrix file
anywhere in this repo.

**If you are ChatGPT, another LLM, or any agent about to USE this
system (not modify its code) — read `docs/CHATGPT_USAGE.md` first.**
That file is the operational companion to this one: exact tool names,
call order, return-value semantics, and anti-patterns. This file stays
the technical source of truth; that one stays truthful to it and must
be updated in the same commit whenever a behavior-changing feature
here changes (same living-requirements convention as this file's own,
below).

**Status legend** (used everywhere below — never promote a wish-list
item to a stronger status than the evidence supports):

- **VERIFIED** — real, live evidence exists (real tmux/git/subprocess/
  browser run), not just unit tests against fakes.
- **IMPLEMENTED_NOT_LIVE_VERIFIED** — code + passing tests exist, but
  the specific live/production scenario hasn't been run for real yet
  (e.g. against a real remote node, or against window/window2).
- **PARTIAL** — some of the feature exists; a real, named piece is
  missing.
- **PLANNED** — requested/spec'd, no code yet.
- **DEPRECATED** — superseded; kept only for backward compatibility.

Facts below were pulled directly from this repo's own code/tests/config
at the time of writing (tool count from `tests/test_server.py`'s exact
set, route inventory from `tests/test_dashboard.py`'s exact dict, test
file count from `ls tests/*.py`), not recalled from memory.

---

## How agents should work (binding convention)

1. Read this file (and the Backlog section at the bottom) before
   writing any code for a Terminal-MCP-managed task.
2. After a task changes BEHAVIOR (not pure refactor/chore), update the
   relevant entry in this file — edit the EXISTING entry in place if one
   already covers that feature, never leave two entries describing the
   same feature differently — before that task can be marked
   `VERIFIED_DONE` (queue) or `INTEGRATED` (integration handoff).
3. **Exempt:** a task that is purely refactor/chore with no behavior/
   contract change. Mark its metadata `"docs_exempt": "refactor"` (or
   `"chore"`). A task that changes behavior but "forgot" docs is never
   exempt.
4. `queue_engine.py`'s dispatch text reminds the worker to read this
   file; `integration_reviewer.py`'s pre-merge gate checks that a
   behavior-changing Handoff's `changed_paths` includes
   `docs/REQUIREMENTS.md`, returning `REWORK_REQUIRED` if it doesn't
   (skipped when `docs_exempt` is set). See "Living-requirements
   convention itself" in the Feature Details section for the exact
   mechanism and its own tests.

---

## Feature matrix (fast scan)

| Feature | Status |
|---|---|
| Session ops (list/status/tail/capture/send/send_keys/bindings) | VERIFIED |
| Session lifecycle (create/detach/delete/kill/reopen) | VERIFIED |
| Rename Session + stable identity/alias redirect | VERIFIED |
| Persistent Session Registry (recoverable/history) | VERIFIED |
| Local Linux tmux backend | VERIFIED |
| Remote Linux node (RemoteNodeClient + node-agent) | VERIFIED |
| macOS worker node | VERIFIED |
| Windows native/ConPTY backend | VERIFIED |
| Dashboard: auth (Cloudflare Access + CSRF) | VERIFIED |
| Dashboard: session tabs, live terminal/output/input | VERIFIED |
| Dashboard: fullscreen/mobile behavior | VERIFIED |
| Dashboard: permissions (grant) UI | VERIFIED |
| Dashboard: web terminal (xterm.js) | VERIFIED |
| Dashboard: session lifecycle actions (kill/reopen/create) | VERIFIED |
| Dashboard: rename UI | VERIFIED |
| Dashboard: per-session Task Manager | VERIFIED |
| Dashboard: Global Task Inbox | VERIFIED |
| Dashboard: Supervisor/Coordinator panel | VERIFIED |
| Dashboard: Integration lane view (in Supervisor panel) | VERIFIED |
| Dashboard: AI Usage panel (read-only, local AI Usage Monitor) | VERIFIED |
| Dashboard: Requirements/Feature Matrix link | VERIFIED |
| Permissions: read/input grants + effective permissions | VERIFIED |
| Reliable prompt submission (press-enter, DELIVERY_UNKNOWN, idempotency) | VERIFIED |
| Supervisor v1 (watch/poll/state machine) | VERIFIED |
| Supervisor v2 (decision/claim/approve/execute) | VERIFIED |
| Supervisor v1/v2 in THIS deployment right now | 0 watches/policies configured (inert; see §6) |
| Persistent Task Queue v2 (state machine + CRUD) | VERIFIED |
| Persist-before-dispatch | VERIFIED |
| Coordinator Agent gate | VERIFIED |
| Coordinator: session-state/git-diverged/smoke-test checks | VERIFIED |
| Coordinator: doc-update gate (this convention) | VERIFIED |
| Auto-dispatch background loop (code+tests) | VERIFIED_LIVE (`config.queue.enabled=true` in production — the loop process is real and running) |
| Auto-dispatch enabled on any real session (incl. window/window2) | NOT ENABLED — 0 of 17 real lanes have `auto_dispatch_enabled` set (confirmed live, 2026-09-07); see Backlog for the exact blocker |
| 3-role pipeline (Coding A/B + Integration Agent) | VERIFIED (disposable only) |
| Integration Agent: event-driven WAIT/wake posture | VERIFIED (`integration_loop.py`, `config.integration_loop.enabled=false` by default — not yet turned on for a real project) |
| Task Migration / Load Balancing | VERIFIED |
| Task Migration: dedicated Move-Task UI | VERIFIED |
| Task Manager: priority-edit/reorder UI (↑/↓, queued tasks) | VERIFIED |
| Session-to-session coordination via structured handoff (not direct chat) | VERIFIED |
| Node/fleet: scheduler (`choose_node`) | VERIFIED |
| Node/fleet: LAN discovery | VERIFIED |
| Node/fleet: Cloudflare Tunnel node connect | VERIFIED |
| Node/fleet: SSH bootstrap node connect | VERIFIED |
| Watchdog (session/node drop detection) | VERIFIED |
| Session Knowledge Store (capture/search/checkpoint) | VERIFIED |
| Windows renderer: real VT100/pyte screen-state emulation | VERIFIED |
| Live disposable E2E: burst enqueue + rename + migrate + restart | VERIFIED |
| Live remote-node (dell-5530) auto-dispatch smoke test | NOT YET RUN (this task's own required next step) |
| Direct-send verification: continued-polling ack evidence (P0 fix) | VERIFIED locally; NOT YET DEPLOYED to dell-5530 |
| `terminal_create_session(initial_prompt)` double-echo (shell sessions) | FIXED, 2026-09-07 (`lifecycle.py`'s own real readiness signal) |
| AI Usage (read-only, local AI Usage Monitor integration) | VERIFIED_LIVE (real browser smoke, real live data) |
| Unified Task System §20 (Kanban/PM/Planner/git isolation/Phase A-E) | VERIFIED — see §20 itself for the exact per-slice scope |

---

## 1. Product purpose + architecture overview

Terminal MCP lets ChatGPT/Claude/an operator observe and (where
explicitly permitted) control real tmux/ConPTY terminal sessions across
a fleet of machines, through the Model Context Protocol (MCP) and a
browser dashboard, with an explicit safety/permission model throughout
(nothing is readable/sendable unless an allowlist pattern or an
explicit dashboard grant says so).

Layering (each a real, separate module):

- **MCP tool surface** (`mcp_app.py`, `server.py`) — 96 tools (see §15)
  exposed to ChatGPT/Claude Code over stdio or streamable HTTP.
- **`TerminalService`** (`core.py`) — the actual read/permission/audit/
  redaction/session-lifecycle logic, backend-agnostic (works identically
  over `TmuxClient` or `WindowsSessionBackend`).
- **`ControllerService`** (`controller.py`) — fleet-wide routing: every
  session-scoped call resolves to whichever NODE actually holds that
  session (local in-process `LocalNodeClient`, or `RemoteNodeClient`
  over HTTP+bearer-token to a remote node-agent), transparently, with
  `AMBIGUOUS_SESSION`/`SESSION_NOT_FOUND` never silently guessed.
- **Dashboard** (`dashboard.py`, 6700+ lines, single-file embedded HTML/
  CSS/JS) — the browser UI over the SAME `TerminalService`/
  `ControllerService`/`QueueService`/`IntegrationService` instances the
  MCP tools use; never a parallel implementation.
- **Nodes** (`node_registry.py`, `node_agent.py`, `windows_agent.py`) —
  a small always-on agent process per remote machine, pushing heartbeats
  and exposing the same narrow session-op HTTP surface `NodeClient`
  expects.
- **Persistence** — session registry, bindings, grants, audit,
  knowledge, queue, integration, supervisor, killed-sessions: each its
  own SQLite file (WAL, 0700/0600), never one shared database.
- **Supervisor v1/v2** (`supervisor.py`, `supervisor2.py`) — an OLDER,
  still-live watch/poll/decide/act subsystem for autonomous "is this
  session stuck/done" detection and (opt-in, gated) auto-continuation.
- **Queue/Coordinator** (`queue_store.py`, `queue_engine.py`,
  `coordinator.py`, `queue_loop.py`) — the NEWER, richer per-session
  task queue + deterministic pre-dispatch gate + (code-complete, not
  yet enabled) auto-dispatch loop. Distinct from Supervisor v1/v2; both
  currently coexist (see §6/§7 for how they differ).
- **Integration Agent** (`integration_store.py`, `integration_engine.py`,
  `integration_reviewer.py`) — the merge/test/regression/promote role
  in the 3-role pipeline (§9).

## 2. Session management

**Status: VERIFIED** (every item below has passing tests; the fleet-wide
routing/multi-node pieces have real, live evidence from earlier
sessions bringing up dell-5530/m910/macbook — see `docs/multi-node.md`).

- **Discovery/list/status/tail/capture:** `terminal_list_sessions`,
  `terminal_status`, `terminal_tail`, `terminal_capture` — real backend
  reads (tmux `capture-pane`/`#{...}` formats, or pyte-rendered VT
  screen state on Windows), redacted (`redact_text`/`redact_ansi_safe`)
  before ever leaving `core.py`.
- **Send/send_keys:** `terminal_send_text` (reliable-submission machinery,
  §5), `terminal_send_keys` (raw key sequences, sensitive-key
  confirmation gate).
- **Bindings:** `terminal_bind/get_binding/list_bindings/unbind/
  tail_bound/status_bound/send_bound` — a durable name -> session alias
  with identity pinning (tmux's own `$N`/`%N`, re-checked at use time so
  a session recreated under the same name never silently keeps a prior
  binding's trust).
- **Create/open/attach-detach/kill/reopen:** `terminal_create_session`,
  `terminal_detach_session`, `terminal_delete_session`,
  `terminal_kill_session` (destructive, server-enforced `confirm_name`,
  captures real reopen metadata before killing), `terminal_reopen_session`
  (recreates from that saved metadata — never a resurrection of the
  killed process's own memory).
- **Rename Session + stable identity/alias:** VERIFIED (see Feature
  Details) — real `tmux rename-session` / in-process re-key on Windows;
  every store that keys on session name (bindings, grants, session
  registry, queue, integration, supervisor watches) is updated in place;
  an in-memory, non-persistent old-name -> current-name redirect on
  `ControllerService` covers a stale caller.
- **Persistent registry:** `terminal_registry_list/get/search/reopen/
  purge` — `session_registry.py`, tracks ACTIVE/MISSING/KILLED/OFFLINE
  history per (node_id, session_name), survives a session being renamed/
  recreated/lost, searchable by cwd/repo/branch even once the name
  itself is gone. `terminal_registry_reopen` is fleet-aware (routed
  through `controller`, use the qualified `node_id/session` form for a
  remote node's MISSING session — see the "Windows node-agent restart
  safety (Phase 0)" Feature Details entry); `list/get/search/purge`
  remain the documented local-node-only Phase A/B posture.
- **Local Linux tmux backend:** `tmux.py`'s `TmuxClient` — the default,
  most-exercised backend.
- **Remote Linux node:** `RemoteNodeClient` (HTTP + bearer token) talking
  to `node_agent.py` (a `LocalNodeClient` wrapping ITS OWN local
  `TerminalService`) on another host — "the local node is a node like
  any other; only its transport differs" is this project's own explicit
  design note.
- **macOS worker node:** registers as `PLATFORM_LINUX` (real tmux + POSIX
  shell, no separate backend needed), onboarded via a LaunchAgent instead
  of systemd — brought up live (rumraisin's MacBook Pro, see git history
  `50c3f0d`).
- **Windows native/ConPTY backend:** `windows_backend.py`'s
  `WindowsSessionBackend` — a real ConPTY child process per session, a
  background reader thread feeding a `pyte.HistoryScreen` (real VT100
  emulation, not a naive `\r`-overwrite model — see §14), a
  self-healing reader-thread supervisor (P0 fix for a real "stale
  stream forever" bug found live), PEB-based `resume_conversation_id`
  detection (ctypes `NtQueryInformationProcess`/`ReadProcessMemory`) for
  fleet-wide Claude-conversation-collision detection
  (`terminal-mcp-doctor conversations`).

## 3. Dashboard

**Status: VERIFIED** for everything marked so in the matrix above;
**PLANNED** for the Requirements link only.

- **Auth:** Cloudflare Access (`cf_access.py`, JWT assertion
  verification, no-op when unconfigured) + CSRF (`_mutation_guard`:
  Origin/Referer must match, checked on every POST) + a separate
  `_read_guard` for GET routes (CF Access only, deliberately no Origin
  check — a plain top-level navigation doesn't reliably send one).
  WebSocket routes (web terminal) go through the same `_origin_allowed`.
- **Session tabs/cards, live terminal/output/input:** polled every 5s
  (`refresh()`), plus a real WebSocket-backed live terminal
  (`windows_webterm.py`/`webterm.py`, xterm.js) for full-fidelity
  interactive use.
- **Fullscreen/mobile:** CSS-only chrome-hide (works on iOS Safari,
  where the real Fullscreen API isn't reliably available) + progressive
  Fullscreen API enhancement elsewhere; tab strip redesigned for mobile
  reach (see git history `ecd038e`, `600c9d7`).
- **Permissions UI:** grant-read/grant-input per session, effective-
  permission display (`effective_read`/`effective_input`, folding in
  both the static whitelist and any active grant — the one place this
  decision is made, per `_read_authorized_with_grant`'s own docstring).
- **Open terminal:** the WebSocket web terminal, readonly unless input
  is granted.
- **Session lifecycle actions:** create/kill/reopen, all through the
  same routed `ControllerService` calls the MCP tools use.
- **Rename UI:** a dedicated, neutral-styled modal (term-bar "✏ Đổi tên
  session"), Enter submits/Esc cancels, real server validation surfaced
  inline, the open tab's live output continues uninterrupted across a
  rename of the currently-viewed session.
- **Task Manager / Global Inbox:** term-bar "📋 Tasks" (per-session) and
  header-menu "📥 Task Inbox" (fleet-wide) — both real, grouped
  (Running/Queued/Waiting-Dependency/Blocked-Rework/Recent), reading
  live off `queue_store.py`, never a second task store.
- **Task button pending-count badge (2026-09-07):** the term-bar "📋
  Tasks" button shows a small numeric badge (hidden when 0, `"99+"`
  above 99) for the CURRENTLY selected session's own pending task count.
  `QueueService.PENDING_STATUSES`/`count_pending()` is the ONE canonical
  definition (`QUEUED`/`PRECHECK`/`READY`/`DISPATCHING`/
  `DISPATCH_UNCERTAIN`/`BLOCKED`/`WAITING_SESSION`/`PAUSED` — excludes
  actively-executing `RUNNING`/`VERIFYING` and terminal `COMPLETED`/
  `FAILED`/`CANCELLED`/`SKIPPED`) — the frontend never re-derives this
  from task rows/DOM; `/dashboard/api/sessions` enriches every row with
  a real `pending_count` field via ONE bulk `queue.pending_counts()`
  call (never a per-session query). Reflects `config.queue.enabled`
  being off exactly as intended: the count reads persisted state
  directly, independent of whether the auto-dispatch loop is running.
  A real, live-discovered CSS bug (author `.task-pending-badge{display:
  inline-block}` silently beat the browser's own default `[hidden]`
  behavior, same specificity, no `!important` — confirmed via a live
  Playwright check showing the badge rendered even with `hidden=true`)
  was found and fixed with an explicit `.task-pending-badge[hidden]
  {display:none}` override, the same established pattern this file's
  own `.term-search[hidden]` rule already uses.
- **Supervisor/Coordinator panel:** header-menu "🧭 Supervisor /
  Coordinator" — extends the pre-existing v1/v2 watch panel with
  auto-dispatch loop status, queue depth per session, fleet-wide
  blocked/rework, Integration lane per project, merged recent-event
  timeline.
- **Integration lane view:** inside the Supervisor/Coordinator panel —
  one row per configured project, its own Waiting/Reviewing/Merging/
  Test/Regression/Rework/Blocked label (mapped from
  `integration_store.py`'s real status constants in exactly one place).
- **Requirements/Feature Matrix link:** **VERIFIED, 2026-09-07** —
  `GET /dashboard/requirements` (plain-text, read fresh off disk every
  request, `_read_guard` only), linked from the header `⋯` menu ("📄
  Requirements") and from the Task Manager modal's own header ("📄
  Docs"). Real live browser check confirmed both links present and the
  route serving real, current `docs/REQUIREMENTS.md` content.

## 4. Permissions / security

**Status: VERIFIED.**

- **Read/input grants:** `grants.py`'s `SessionGrantStore` — dashboard-
  ONLY (never reachable from a raw MCP tool, by design: "a grant widens
  what the DASHBOARD can do for one specific session, nothing else,
  ever"), read-then-input ordering enforced, identity-pinned at grant
  time and re-checked at use time (P0-2), revoking read also revokes
  input.
- **Effective permissions:** `_read_authorized_with_grant`/
  `_input_authorized_with_grant` (core.py) — the ONE place "is this
  session actually readable/sendable right now" is decided; every
  dashboard listing route and every real read/send call defers to them.
- **Origin/auth/websocket controls:** see §3's Auth bullet.
- **Audit log:** `audit.py`'s `AuditStore` — every send/kill/rename/
  grant/etc. action, `text_sha256`/`text_preview` (never raw secret
  text), idempotent-sends dedup table, redaction fingerprinting
  (`text_fingerprint`).
- **Credential/redaction:** `redaction.py` — pattern-based scrubbing of
  API keys/tokens/passwords from captured output AND from any evidence
  a Coordinator/Integration reviewer collects.
- **Legacy whitelist migration:** `allowed_session_patterns`/
  `input_policy.allowed_session_patterns` (config.yaml, glob patterns)
  remain the static floor for every session; `SENSITIVE_SESSION_WORDS`
  requires an exact (non-glob) whitelist entry even then. No separate
  "legacy" migration path currently pending — this IS the live model.

## 5. Reliable prompt submission

**Status: VERIFIED.**

- **Press-enter semantics:** `SEND_TEXT_ENTER_SETTLE_SECONDS` settle
  window between the literal-text `send-keys` and the Enter keystroke
  (a real race found live: some CLIs swallow an Enter that arrives
  before they've finished consuming the text).
- **SUBMIT_CONFIRMED/DELIVERY_UNKNOWN:** `_send_text_and_verify` (core.py)
  — a real before/after-Enter tmux-pane diff decides which; a raw
  `DELIVERY_UNKNOWN` NEVER silently retries — see §7 for how the queue
  layer reconciles it via idempotency key + observed activity before
  ever re-attempting.
- **Idempotency:** `idempotency_key` on `terminal_send_text` — the SAME
  key always returns the ORIGINAL result rather than sending twice,
  durable across a process restart (`idempotent_sends` table).
- **Input audit:** `terminal_list_input_audit`, `terminal_input_context`
  — every send's fingerprint/preview/result queryable.
- **TARGET_AWAITING_APPROVAL pre-send gate (`adapters.py`'s
  `_WAITING_PATTERNS`):** a real false positive was found and fixed live
  (2026-09-07, real attended session `window2`) — see this file's own
  Feature Details entry "TARGET_AWAITING_APPROVAL false positive on
  ordinary composer text" below for the full root cause/fix/tests.

## 6. Supervisor v1/v2

**Status: VERIFIED** (code + tests); **current production limitation:**
per this project's own persisted operational notes, autonomous send is
inert in the live deployment — 0 watches/policies currently configured,
so nothing here is actively acting on a real session right now (the
tools and background loop remain fully functional, just unused at
present).

- **Watches:** `supervisor_watch`/`unwatch`/`list_watches` — a "session"
  or "binding" kind target, states per `SUPERVISOR_STATES` (`RUNNING,
  IDLE, WAITING_INPUT, BLOCKED, FAILED, COMPLETION_CANDIDATE, VERIFYING,
  VERIFIED_DONE, ERROR, UNKNOWN`).
- **Polling:** `SupervisorLoop` — a daemon thread, gated by
  `config.supervisor.enabled` (False by default), poll interval
  configurable.
- **Completion candidate / verifier:** a real, redacted-evidence-based
  promotion path (`COMPLETION_CANDIDATE -> VERIFYING -> VERIFIED_DONE`)
  requiring either a quiet-window hold or a matched single-use nonce —
  never a bare "the pane looks done" heuristic.
- **Actions/claim/approval/execute (v2):** `supervisor2_list_actionable_
  events/claim_event/submit_decision/review_action/execute_send/
  list_actions` — a human/ChatGPT-in-the-loop decision layer over v1's
  own detection, gated by BOTH a global `v2_enabled` kill switch and
  each watch's own `policy_mode` (default `observe_only`).
- **Stop policies:** `ATTENTION_STOP_PATTERNS` (credentials, destructive
  shell commands, confirmation prompts) — content-based, reused as-is
  by the Coordinator Agent's own sensitive-prompt screen (§8).
- **Restart/idempotency:** watch state, claim tokens, and completion
  nonces all persist in `SupervisorStore` (SQLite) — a restart resumes
  correctly, never double-claims/double-sends.

## 7. Persistent Task Queue v2

**Status: VERIFIED**, including the background auto-dispatch loop's LIVE
behavior on the LOCAL node (2026-09-07 P0 QUEUE + SUPERVISOR LIVE TEST
checkpoint — see the Feature Details entry below for the full write-up).
Auto-dispatch against a REMOTE node (dell-5530) end-to-end remains
**IMPLEMENTED_NOT_LIVE_VERIFIED** (Backlog item 1, unchanged by this
pass) — this checkpoint deliberately used local/Linux disposable
sessions only, per its own explicit scope.

- **Persist-before-dispatch:** every prompt becomes a durable
  `queue_tasks` row (via `terminal_enqueue_task`/`terminal_queue_set/
  append`) BEFORE any send is attempted — see §5 for how delivery
  itself stays reliable once dispatch does happen.
- **Task IDs / state machine:** `QUEUED -> PRECHECK -> READY | BLOCKED |
  <back to QUEUED via NEEDS_REWORK> | PAUSED (NEEDS_HUMAN) ->
  DISPATCHING -> RUNNING -> VERIFYING -> COMPLETED`, plus
  `DISPATCH_UNCERTAIN`/`WAITING_SESSION`/`FAILED`/`CANCELLED`/`SKIPPED`
  branches — `VALID_TRANSITIONS` in `queue_store.py` is the single
  source of truth, enforced on every transition.
- **Dispatch reconciliation:** `reconcile_stale_claims` (a crashed
  engine's abandoned PRECHECK claim is detected via lease expiry and
  re-queued) + `reconcile_uncertain_and_waiting` (a `DISPATCH_UNCERTAIN`/
  `WAITING_SESSION` task past its grace period is resolved from real
  evidence, never blindly retried).
- **Offline/restart behavior:** every task/lane/event row is real SQLite
  (WAL) — a controller restart loses nothing; proven live in the final
  disposable E2E (burst 12 tasks, rename mid-queue, migrate 4 to a
  peer, fresh `QueueStore` instance over the same db file shows all 12
  intact, 0 dropped/duplicated).
- **Multi-task queue:** one lane per session, FIFO + `priority DESC`
  ordering, exactly one active task per lane at a time; other lanes
  progress independently (never serialized across sessions).
- **Background auto-dispatch + global kill switch/config:**
  `queue_loop.py`'s `QueueLoop` — a real daemon thread. Two independent,
  stacked gates: `config.queue.enabled` (global — **now `true`** in
  production `config.yaml` since the 2026-09-07 checkpoint below) AND
  the per-lane `queue_lanes.auto_dispatch_enabled` (`False` by default,
  per-lane opt-in). **The per-lane gate is still off for every real
  session, including window/window2/wtest** — only 4 disposable
  `claude-qtest-*` lanes were ever turned on, and all 4 were turned back
  off and their sessions killed once the live test finished. Confirmed
  live: the REAL running `terminal-mcp-http.service` process
  automatically dispatched, ran, and verified every disposable task
  with ZERO manual `tick()`/`run_once()` calls from the test itself —
  the loop's own 3s poll cycle did all of it, unattended, exactly as
  designed.
- **Task inbox / session task views:** VERIFIED — §3's Task Manager/
  Global Inbox entries.

## 8. Coordinator Agent

**Status: VERIFIED.**

`coordinator.py`'s `CoordinatorGate.review()` — deterministic (a
disclosed design choice, not an LLM call; see the module's own
docstring for why), fail-closed on any unreadable evidence. Runs, in
order, on every `PRECHECK` task:

1. Operator-declared `artificial_blocker` (explicit test/ops override).
2. Review-attempt budget (`max_review_attempts`, default 5 — forces
   `NEEDS_HUMAN` rather than looping forever).
3. Sensitive/destructive prompt screen (reused `ATTENTION_STOP_PATTERNS`
   content).
4. Scope/clarity check (pluggable `scope_reasoner`; default is a
   disclosed, crude length heuristic — a real LLM-backed reasoner is a
   deliberate, separate, not-yet-wired decision).
5. Previous-task-really-done check (independently re-verified from
   `verification_evidence`, not just trusted from the status label).
6. Session-state check (WAITING_INPUT / `input_required` / stale
   `reader_alive`) — added in the production-readiness pass.
7. Session identity/cwd/node check (`expected_cwd`/`expected_node_id`
   task metadata) — the exact fix for the real window/window2
   transcript-collision P0.
8. Cross-lane conflict (another session actively in the same cwd).
9. Repo evidence — dirty working tree (`NEEDS_REWORK`, `allow_dirty_repo`
   opt-out) and diverged-from-upstream (`NEEDS_HUMAN`, both ahead AND
   behind nonzero only; `allow_diverged_branch` opt-out) — real `git
   status`/`rev-list --left-right --count` subprocess calls.
10. Opt-in smoke test (`require_smoke_test_command` task metadata) — a
    real, bounded-timeout subprocess; failure is `NEEDS_REWORK`.
11. Doc-update gate (this convention) — enforced at the Integration
    review layer (§9), not inside `CoordinatorGate` itself (the
    Coordinator gates DISPATCH of a task; the doc-update check gates
    INTEGRATION of its result, since that's where a real diff already
    exists to inspect).

**Dependency handling:** a QUEUED task's `depends_on` (task ids, may
cross sessions) is checked at CLAIM time (`_dependencies_satisfied_locked`
in `queue_store.py`) — a task with an unmet dependency is never even
picked up for coordinator review, deliberately mechanical rather than a
judgment call.

**Output:** a structured `CoordinatorDecision` (`status` ∈ `READY |
BLOCKED | NEEDS_REWORK | NEEDS_HUMAN`, `reason`, `blockers`,
`required_actions`, `evidence`) — persisted verbatim
(`record_coordinator_decision`) and surfaced in the Task Manager/Task
Inbox UI as the "gate" for the next task in line. `NEEDS_REWORK` always
routes back to `QUEUED` in the SAME lane (never skips to a different
task) per the task's own explicit priority.

## 9. 3-role pipeline (Coding A/B + Integration Agent)

**Status: VERIFIED** (disposable E2E only — never enabled on a real
project's own coding sessions).

- **Roles:** two (or more) ordinary coding sessions (Coding A/B — no
  special role flag, just normal queue lanes) + one Integration Agent
  role, which is a pure backend engine with NO real interactive tmux
  session of its own (a disclosed limitation).
- **Handoff schema:** `Handoff` (`integration_store.py`) — immutable
  provenance (`project, task_id, origin_session, branch, commit_sha,
  base_sha, changed_paths, test_summary, artifacts`), created exactly
  once via `publish_handoff` (auto-published from a `COMPLETED` queue
  task whose metadata declares `integration_required`, via
  `publish_handoff_for_completed_task`).
- **Integration queue:** one handoff claimed/processed at a time per
  project (`_ACTIVE_HANDOFF_STATUSES`), atomic claim (`claim_next_handoff`,
  `BEGIN IMMEDIATE`), lease + stale-claim reconciliation, same
  restart-safety posture as the coding-session queue.
- **WAITING_FOR_HANDOFF/event-driven wake:** **PARTIAL** — the state
  name/posture exists (a project's Integration Agent is idle/"waiting
  for handoff" whenever nothing is claimed), but there is still NO
  automatic background loop for it (`terminal_integration_run_once`
  must be called explicitly) — the specifically-requested "sleeps, wakes
  exactly once per new handoff" event-driven mechanism is **PLANNED**,
  not built (tracked in Backlog).
- **Review/merge/targeted test/full regression:**
  `integration_reviewer.py`'s `IntegrationReviewGate` (real pre-merge
  gate: commit/base resolvable, working tree clean, real `git diff`
  sensitive-content scan, migration-path risk flags in `deep` review
  depth, branch divergence check, now also the doc-update gate) ->
  `integration_engine.py`'s `_merge` (real `git merge --no-ff`, conflict
  detection via `git diff --name-only --diff-filter=U`, `git merge
  --abort` on conflict, NEVER guesses resolution) -> `_run_targeted_test`
  -> batched `_run_full_regression` (batch size / max-wait configurable
  per project).
- **Rework routing:** a failed review/targeted-test reverts the merge
  commit first (`git revert -m 1`, a real bug found and fixed live — a
  failed test used to leave bad content silently on the integration
  branch) THEN routes a rework task back to the ORIGIN session (or a
  `session_ownership` path-prefix override), never guessed, never to a
  different worker.
- **Promotion policy:** `promote_to_main` — fast-forward preferred,
  falls back to `--no-ff`, NEVER force; only reachable once a batch is
  `MERGE_READY`; auto-promote is a per-project opt-in
  (`auto_promote_enabled`, default False) — otherwise an explicit
  `terminal_integration_promote` call.

## 10. Task migration / load balancing

**Status: VERIFIED.**

- **Eligibility:** only `QUEUED`/`WAITING_SESSION`/`BLOCKED`/`FAILED`
  tasks are ever migratable — `RUNNING` (and every other in-flight
  status) is NEVER hot-migrated by design.
- **Reassignment:** `terminal_task_reassign` — atomic
  (`BEGIN IMMEDIATE` + status re-check; a task claimed for dispatch
  between a caller's check and the reassign call fails clean with
  `TASK_ALREADY_CLAIMED`, no double-dispatch).
- **Preserving task_id/history/dependencies:** the SAME task_id/prompt/
  metadata/`depends_on` throughout — only `session` and an append-only
  `migration_history` change; `original_owner` is set once, at creation,
  and never touched by a migration (only by Rename Session, which
  updates it to follow the CURRENT name of the same session — a rename
  is not a hand-off).
- **Running-task policy:** never hot-migrated (see Eligibility) — the
  documented alternative for a RUNNING task whose session went offline
  is `mark_at_risk`, not a forced move.
- **Race/restart handling:** proven via a REAL `threading.Thread`
  claim-vs-reassign race test, and a simulated controller-restart test
  (fresh `QueueStore` instance over the same db file).
- **Planner correctness:** `plan_rebalance` simulates against a virtual,
  in-memory per-session task list (a real bug — re-querying the live,
  unmutated store mid-loop caused the same task to be selected multiple
  times in one plan — found and fixed during development, see git
  history).
- **Move-Task UI: VERIFIED, 2026-09-07.** `POST /dashboard/api/tasks/
  reassign` (thin wrapper over the already-real `queue.reassign` —
  never a second reassignment mechanism; requires read access to BOTH
  the task's current and target session) + a "↷ Move" button on every
  eligible task row (queued/blocked-rework groups) in the per-session
  Task Manager AND the fleet-wide Global Task Inbox, prompting for the
  target session and an optional reason. Real live browser check: a
  real click → real `window.prompt` dialogs → real reassign → the task
  genuinely moved sessions, confirmed via the Task Manager's own list
  shrinking by one. Priority/reorder UI: ↑/↓ buttons on each QUEUED
  task row in the per-session Task Manager (scoped there only — a
  mixed-session Global Task Inbox list has no single well-defined
  reorder meaning), wired to the already-real, already-tested
  `/dashboard/api/session/queue/reorder` route.

## 11. Session-to-session coordination

**Status: VERIFIED** for the mechanism that exists; direct chat between
coding sessions was explicitly never built (by design).

Coordination happens ONLY through structured, persisted state — never a
raw message from one session's own output into another's input:

- **Handoffs** (§9) — the coding-to-integration mailbox.
- **`depends_on`** (§8/§7) — a task's own declared prerequisite(s),
  possibly in a different session's lane, mechanically gated at claim
  time.
- **`session_ownership`** — a path-prefix -> session override for rework
  routing, another structured (not conversational) coordination point.
- **Barriers:** the Integration batch mechanism (`batch_size`/
  `batch_max_wait_seconds`) is the closest existing "wait for N handoffs
  before proceeding" barrier; there is no separate, general-purpose
  cross-session barrier primitive beyond that.

## 12. Node/fleet support

**Status: VERIFIED** for everything below (each has passing tests; LAN
discovery/Cloudflare/SSH connect flows have real code and dashboard UI,
not placeholders).

- **Windows/Linux/macOS:** see §2 — Linux (local + remote via
  node-agent) and Windows (native ConPTY backend) are both real,
  distinct backends; macOS is a real Linux-shaped node (tmux + LaunchAgent
  onboarding), no separate backend code needed.
- **Scheduler:** `scheduler.py`'s `choose_node` — used by
  `terminal_create_session(node="auto")` to place a new session on an
  eligible online node matching required agent_type/platform.
- **LAN discovery:** `lan_discovery.py`'s `DiscoveryService` — a real
  concurrent host scan (configurable concurrency/timeout/cooldown),
  dashboard-driven ("Scan LAN" flow).
- **Cloudflare Tunnel node connect:** real dashboard flow
  (`/dashboard/api/nodes/connect/*`), `tunnel_diagnostics.py`.
- **SSH bootstrap node connect:** real dashboard flow, `remote_connect.py`
  (host-key trust, connection test, bootstrap), covered by `real_ssh`-
  marked tests against a real local sshd.

## 13. Watchdog / recovery / registry / knowledge

**Status: VERIFIED.**

- **Watchdog:** `terminal_watchdog_session_events/acknowledge_session_event/
  node_events/acknowledge_node_event` — real session/node drop detection
  fleet-wide (`session_registry.py`/`node_registry.py`'s own
  `mark_missing`/`mark_node_offline`), unacknowledged-only by default,
  never deletes an event on acknowledge (kept for history).
- **Persistent registry / history:** §2's Persistent registry bullet.
- **Knowledge store:** `terminal_knowledge_search/timeline/recover/
  checkpoint` — `session_knowledge.py`, continuous output capture
  (independent of tmux's own `history_size`, which the Claude Code Ink
  TUI keeps at 0 — a real, disclosed constraint this store's own capture
  mechanism exists specifically to work around), fleet-wide search
  (queries every currently-online node and merges; an unreachable node
  just contributes zero results, never fails the whole search).

## 14. Renderer / terminal emulation

**Status: VERIFIED**, with one disclosed, deliberate simplification.

- **Linux/tmux:** real tmux `capture-pane` (`-e` for ANSI when
  requested) — tmux's own renderer already resolves everything.
- **Windows:** a real `pyte.HistoryScreen` fed every raw ConPTY output
  chunk (`_reader_loop`) — genuine VT100/xterm control-sequence
  resolution (cursor repositioning, alternate screen, etc.), replacing
  an earlier naive `\r`-overwrite line model that a real, live bug
  (repeated "Beaming…" spinner text duplication) proved incorrect.
- **Known remaining limitation (disclosed, deliberate):** the read-only
  tail/capture path returns a plain, already-resolved text snapshot —
  no raw ANSI/color is preserved there even with `ansi=True` requested
  (pyte has already flattened it). The LIVE interactive web terminal
  (`windows_webterm.py`, xterm.js) is UNAFFECTED — it gets the true raw
  byte stream directly from the reader loop, full color fidelity.

## 15. API / MCP tool inventory (96 tools, from `tests/test_server.py`'s own exact set)

Session ops: `terminal_list_sessions`, `terminal_tail`, `terminal_capture`,
`terminal_status`, `terminal_send_text`, `terminal_send_keys`,
`terminal_exit_copy_mode`.
Bindings: `terminal_bind`, `terminal_get_binding`, `terminal_list_bindings`,
`terminal_unbind`, `terminal_tail_bound`, `terminal_status_bound`,
`terminal_send_bound`.
Audit: `terminal_list_input_audit`, `terminal_input_context`.
Lifecycle: `terminal_create_session`, `terminal_detach_session`,
`terminal_delete_session`, `terminal_kill_session`,
`terminal_rename_session`, `terminal_reopen_session`,
`terminal_list_killed_sessions`.
Supervisor v1: `supervisor_watch`, `supervisor_set_verifier_policy`,
`supervisor_unwatch`, `supervisor_list_watches`,
`supervisor_get_completion_token`, `supervisor_status`,
`supervisor_list_events`, `supervisor_ack_event`, `supervisor_run_once`.
Supervisor v2: `supervisor2_set_policy`, `supervisor2_get_policy`,
`supervisor2_list_actionable_events`, `supervisor2_claim_event`,
`supervisor2_submit_decision`, `supervisor2_review_action`,
`supervisor2_execute_send`, `supervisor2_list_actions`.
Nodes: `terminal_list_nodes`, `terminal_node_status`,
`terminal_node_sessions`.
Registry: `terminal_registry_list`, `terminal_registry_get`,
`terminal_registry_search`, `terminal_registry_reopen`,
`terminal_registry_purge`.
Knowledge: `terminal_knowledge_search`, `terminal_knowledge_timeline`,
`terminal_knowledge_recover`, `terminal_knowledge_checkpoint`.
Watchdog: `terminal_watchdog_session_events`,
`terminal_watchdog_acknowledge_session_event`,
`terminal_watchdog_node_events`, `terminal_watchdog_acknowledge_node_event`.
Queue (Phase 1/2 CRUD): `terminal_queue_set`, `terminal_queue_append`,
`terminal_queue_status`, `terminal_queue_list_all`, `terminal_queue_pause`,
`terminal_queue_resume`, `terminal_queue_retry`, `terminal_queue_skip`,
`terminal_queue_cancel`, `terminal_queue_reorder`, `terminal_queue_clear`,
`terminal_queue_events`, `terminal_queue_run_once`, `terminal_queue_verify`,
`terminal_queue_set_auto_dispatch`.
Integration: `terminal_integration_configure`, `terminal_integration_status`,
`terminal_integration_list_handoffs`, `terminal_integration_run_once`,
`terminal_integration_pause`, `terminal_integration_resume`,
`terminal_integration_retry_handoff`, `terminal_integration_force_regression`,
`terminal_integration_promote`, `terminal_integration_events`.
Persist-before-dispatch: `terminal_enqueue_task`, `terminal_task_status`,
`terminal_queue_metrics`.
Task migration: `terminal_task_set_project`, `terminal_task_reassign`,
`terminal_task_assignment_history`, `terminal_task_rebalance_plan`,
`terminal_task_rebalance`.
Task Manager UI: `terminal_session_tasks`, `terminal_fleet_task_summary`.
Auto-dispatch loop: `terminal_queue_loop_status`,
`terminal_queue_loop_run_once`.
Supervisor/Coordinator panel: `terminal_queue_global_inbox`,
`terminal_queue_recent_events`, `terminal_integration_fleet_overview`.

**Dashboard HTTP routes** (58, from `tests/test_dashboard.py`'s own exact
dict) — session CRUD/grant/rename/kill/reopen, supervisor v1/v2, nodes
(list/status/drain/test-connection/onboarding/heartbeat), LAN discovery,
SSH/Cloudflare/agent-token connect, registry, knowledge, watchdog, Task
Manager/queue actions, Global Task Inbox, Supervisor/Coordinator panel
reads. Full list lives in `tests/test_dashboard.py`'s own
`test_dashboard_mobile_batch_no_unexpected_route_changes` — that test
IS the authoritative, continuously-verified route inventory; this file
summarizes it, never duplicates it verbatim (avoids drift).

## 16. Config / env / service / deploy / tunnel / startup

- **Config file:** `config.yaml` (`AppConfig`, `config.py`) — 14 nested
  config sections: `permissions`, `input_policy`, `supervisor`, `queue`
  (new), `dashboard`, `session_lifecycle`, `session_knowledge`,
  `ask_chatgpt`, `maintenance`, plus per-node/discovery/remote-connect
  sections under `nodes`.
- **Service:** `terminal-mcp-http.service` (systemd), HTTP port `8766`
  (`server_http.py`'s `HTTP_PORT`).
- **Remote nodes:** `node_agent.py` (Linux/macOS) / `windows_agent.py`
  (Windows) — a small always-on process pushing heartbeats to the
  controller's `/dashboard/api/nodes/{node_id}/heartbeat`.
- **Tunnel:** Cloudflare Tunnel fronts the dashboard for public/ChatGPT
  access (`docs/tunnel-connection-reliability.md`); LAN/direct access
  also supported.
- **Public/dashboard access model:** Cloudflare Access (when configured)
  gates every dashboard route (read AND write); without it, network
  topology (only reachable through the tunnel hostname, or LAN) is the
  boundary — see §4.
- **Startup:** `MaintenanceLoop` (SQLite WAL/retention hygiene, always
  on) and, opt-in, `SupervisorLoop`/`QueueLoop` (both currently
  `enabled: False`) start as daemon threads inside the same process,
  `atexit`-registered for a best-effort clean stop.

## 17. Testing / acceptance matrix

97 test files (`ls tests/*.py`). Full default suite (excludes
`live_cli`/`real_network`/`real_ssh`/`queue_smoke`-marked tests): **1639
passed, 1 pre-existing unrelated flake
(`test_session_lifecycle.py::test_create_initial_prompt_goes_through_reliable_submission_once`,
fails identically in isolation regardless of any change in this repo),
14 deselected**, as of commit `d345524`. Key files:

- `test_queue_store.py`/`test_queue_store_phase2.py` — Phase 1/2
  persistence + state machine.
- `test_coordinator.py` (38 tests, incl. real git repos with a bare
  remote + two diverging clones, real subprocess smoke-test commands).
- `test_queue_engine.py` / `test_queue_engine_smoke.py` (real tmux,
  `queue_smoke` marker).
- `test_queue_loop.py` (6, incl. a REAL background thread driving a
  full autonomous lifecycle with zero manual `tick()` calls).
- `test_integration_store.py` / `test_integration_engine.py` /
  `test_integration_reviewer.py` / `test_three_role_smoke.py` (real
  tmux+git, `queue_smoke` marker).
- `test_task_migration_store.py`/`_planner.py`/`_mcp_tools.py` (incl. a
  real `threading.Thread` race test).
- `test_rename_session.py` (12, real tmux + dashboard HTTP + cross-store
  propagation + simulated restart).
- `test_task_manager_ui.py` (15, incl. real `WindowsSessionBackend` +
  real concurrent-thread test).
- `test_supervisor_coordinator_panel.py` / `test_queue_service_fleet_views.py`
  (Supervisor/Coordinator panel + Global Task Inbox).
- `test_final_e2e_rename_and_task_manager.py` — the closing disposable
  E2E (burst 12 tasks, rename mid-queue, migrate 4 to a peer, simulated
  restart, 0 dropped/duplicated).
- `test_dashboard.py` — the authoritative dashboard route/fetch-count
  inventory (see §15).

## 18. Known limitations + technical debt + backlog

See the dedicated **Backlog** section at the bottom of this file — kept
in ONE place rather than scattered, so it's never re-discovered from
scratch by a future agent.

## 19. Feature history / checkpoints

See **Feature Details** below for the full per-feature entries
(template: goal, status, scope, UI, API, config, schema, tests, known
limitations, dependencies, follow-up, trace). Chronological commit
list (`main` branch):

| Commit | Feature |
|---|---|
| `47ff72e` | Supervisor Queue v2, Phase 1 |
| `04f14d0` | Supervisor Queue v2, Phase 2: Coordinator Agent gate + dispatch engine |
| `4d51782` | P0 audit/recovery: window/window2 transcript collision fix |
| `957f15a` | 3-role model: Coding A/B + Integration Agent |
| `ae85232` | P0: persist-before-dispatch |
| `c95d06e` | Task Migration / Load Balancing |
| `0944c33` | Rename Session |
| `20f6ff0` | Dashboard Task Manager UI |
| `ee479f1` | Final live disposable E2E (rename + Task Manager) |
| `fa5e661` | Coordinator production-readiness pass + auto-dispatch loop |
| `d345524` | Dashboard Supervisor/Coordinator panel + Global Task Inbox |

Dates are real commit timestamps (`git log --format=%ci`), not restated
here to avoid drift — `git log <sha> -1` is the authoritative source.

---

## 20. Unified Task System (VERIFIED — Kanban/PM/Planner/git isolation/
Phase A-E all live; see each §20.x subsection's own implementation note
for exactly what's built vs. still PLANNED within it — this header used
to say "PLANNED — architecture, not built" when this section was first
drafted; updated 2026-09-07 once the roadmap below was actually
implemented, checkpoint by checkpoint, over several sessions)

**Status: PLANNED.** Nothing in this section is callable. It exists so
a future implementer (human or agent) builds ONE coherent system
instead of re-discovering these decisions piecemeal or accidentally
building a second, parallel task store. This section supersedes an
earlier, narrower "Global Backlog Orchestrator" idea that was discussed
but never written up as its own design — everything that idea would
have covered is folded in here instead, under one name.

### 20.0 Core principle (binding on every future phase below)

**ONE canonical task row, ONE queue engine (`queue_store.py`/
`queue_engine.py`, both already real and VERIFIED), many VIEWS.** A
task that has no session assigned yet is shown in the Global/Unassigned
view; the moment it's assigned, it appears in that session's own queue
— **the same row**, never copied, never duplicated into a second store.
The Kanban board, the per-session Task Manager, the Global Task Inbox,
and any future PM/Planner/Merge-Agent surface are all different
read-shapes over this one table. Any implementation that creates a
second task/backlog table is wrong by construction, regardless of how
well it works in isolation.

### 20.1 Data model — real gap found, real reuse found

**Status of this subsection: PARTIALLY VERIFIED (2026-09-07) — see
20.1a.** The paragraph below is the ORIGINAL plan as first designed;
20.1a documents what was actually built and why it deliberately chose a
different, lower-risk mechanism for "Global/Unassigned" than the one
described here. Kept side by side (never silently rewritten) so a
future implementer sees both the original reasoning and why it changed
on contact with the real, already-battle-tested dispatch engine.

`queue_tasks.session` is currently `TEXT NOT NULL` (`queue_store.py`'s
own schema) — a task **cannot** exist without a session today. This is
the one real, necessary schema change for "Global/Unassigned" to exist
at all: `session` must become nullable (a new, additive migration —
`schema.py`'s real `Migration`/`apply_migrations` framework already
used for exactly this kind of change, see Migration 5's own real
precedent in this same file). No other existing column needs to change
shape for this.

#### 20.1a Implementation note (2026-09-07, VERIFIED live) — sentinel lane, not a nullable column

The Kanban backend slice of §20 (board/create/assign — NOT the rest of
§20: PM routing, Planner, git isolation, Merge Agent, Phase A-E are all
still PLANNED, unbuilt) is now real, tested, and live-verified. It
deliberately did **not** make `queue_tasks.session` nullable. Instead:

- `queue_store.UNASSIGNED_LANE = "__unassigned__"` — a reserved,
  ordinary lane name. An "unassigned" task is a completely normal row
  in this lane; `session` stays `TEXT NOT NULL` everywhere, unchanged.
- Why: a nullable `session` would have rippled through `queue_engine.
  py`'s dispatch loop, the Coordinator gate, and every existing test/
  caller that assumes a real session string for `session IS NOT NULL`
  reasoning. The sentinel-lane approach needs zero changes to any of
  that: `auto_dispatch_enabled` defaults `False` for every lane
  including this one, so `UNASSIGNED_LANE` is completely inert to the
  background dispatch loop — exactly the property an unassigned task
  needs (nothing should ever try to send it anywhere).
- `queue_store.MOVABLE_STATUSES = (QUEUED, BLOCKED, PAUSED,
  WAITING_SESSION)` + `QueueStore.move_task_to_session(task_id,
  new_session)`: reassigns the **same row** (id/history/metadata
  unchanged, never a duplicate) between lanes — including out of
  `UNASSIGNED_LANE` into a real session, or between two real sessions.
  Refuses (`TASK_NOT_MOVABLE`) a task currently in PRECHECK/READY/
  DISPATCHING/DISPATCH_UNCERTAIN/RUNNING/VERIFYING or any terminal
  status, to avoid racing the engine's own background tick.
- `QueueService.create_task(title, prompt, *, session=None, priority=0,
  project=None, metadata=None)` — the ONE canonical creation path:
  `session=None` creates a durable, immediately-persisted row in
  `UNASSIGNED_LANE`; `session` given creates it already assigned (same
  guarantee `enqueue()` always had). `project` is folded into
  `metadata["project"]` — no schema change needed for that one field.
- `QueueService.assign_task(task_id, session)` — validates the session
  name, then calls `move_task_to_session`.
- `QueueService.board()` — the Global Tasks Kanban's real data source:
  one bulk `list_all_lanes()` read, grouped into the 5 lifecycle columns
  the UI shows (`backlog`, `queued`, `running`, `blocked_review`,
  `done` — see the table in the base §20.1 above for which raw statuses
  land in each). `UNASSIGNED_LANE` never leaks to a caller — a task's
  `session` field reads `None` when it's actually in that lane.
- MCP tools: `terminal_task_create` (nullable `assigned_session_id`),
  `terminal_task_assign`, `terminal_task_board` (`mcp_app.py`).
- Dashboard: `/dashboard/tasks` — a new, standalone Kanban page (same
  "own page, not folded into `DASHBOARD_HTML`'s tab UI" precedent
  `SESSIONS_ADMIN_HTML`/`NODES_ADMIN_HTML` already set), 5 columns,
  session-substring filter (`?session=` deep-link), inline "Assign to
  session" action on Backlog cards (drag/drop deferred, per the task's
  own "action-based moves first"), a "+ New Task" panel. Backed by
  `GET /dashboard/api/tasks/board`, `POST /dashboard/api/tasks/create`,
  `POST /dashboard/api/tasks/assign` — same `_read_guard`/
  `_mutation_guard`/per-session `_read_authorized` gating every other
  queue route already uses, no new permission model. Linked from the
  main dashboard's `⋯` menu (`🗂 Global Tasks`, next to `⚙ Quản lý
  session`/`🖥 Nodes`). The existing per-session Task button/badge
  (`#taskManagerBtn`) still opens its own existing modal unchanged —
  this new page is an additional fleet-wide view, not a replacement,
  reachable directly or via `?session=` for a filtered look at one
  session's own cards.
- Tests: `tests/test_queue_store.py` (7, `move_task_to_session`),
  `tests/test_queue_service_fleet_views.py` (12, `create_task`/
  `assign_task`/`board`), `tests/test_queue_mcp_tools.py` (3, the new
  MCP tools through the real `server.call_tool` path), `tests/
  test_task_manager_ui.py` (7, the 3 new dashboard routes + the page
  route, through a real `TestClient` with real tmux workers + real
  grants — same rig every other dashboard queue-route test already
  uses). Full suite green apart from one pre-existing, unrelated
  timing-sensitive failure (see Backlog item 14).
- Live evidence: a disposable server (scratch config/state/queue.db, a
  separate port, never the production instance) seeded with 9 tasks
  spanning all 5 columns, checked with a real Playwright browser —
  initial render matched seeded counts exactly; clicking "Assign" on a
  Backlog card moved it into Queued in real time (counts updated on the
  next poll, no page reload); the session filter correctly narrowed to
  one session's own 3 tasks; the "+ New Task" panel created a real row
  that appeared in Backlog; mobile viewport (390×844) confirmed the
  page's own internal scroll region (not the outer document) reaches
  every column's content.
- Still PLANNED, not built in this slice: drag/drop, the PM/Orchestrator
  routing that would auto-populate Backlog assignment, dependency-tree
  visualization, and the `parent_task_id`/routing/risk columns listed
  in the ORIGINAL plan just above (20.1's base paragraph) — none of
  those were needed for a working Kanban board, so none were added
  speculatively ahead of the phase that actually needs them.

New columns needed on `queue_tasks` (one migration, additive, never
touches historical rows' meaning):
- `parent_task_id TEXT` — set by the Planner (§20.3) when a task is
  split; `depends_on` (already exists) is reused unchanged for the
  resulting child-to-child DAG, never a second dependency mechanism.
- `required_role TEXT`, `required_os TEXT`, `required_capabilities TEXT`
  (JSON list), `preferred_capabilities TEXT` (JSON list),
  `pinned_session TEXT` / `pinned_node TEXT` (an explicit human/PM hard
  constraint — see §20.2), `excluded_sessions TEXT`/`excluded_nodes
  TEXT` (JSON lists), `risk_level TEXT` (`LOW`/`MEDIUM`/`HIGH`/
  `CRITICAL`, §20.6), `created_by TEXT` (`"human"`/`"orchestrator"`/a
  specific identity), `acceptance_criteria TEXT`.
- `routing_reason TEXT` (JSON — the PM's own explainability record: what
  was scored, why this worker won — §20.2's own explicit requirement),
  `pm_decision_id TEXT`.

**State mapping (task's own explicit ask — reuse, never a parallel
vocabulary):**

| User-facing name | Real `queue_tasks.status` |
|---|---|
| UNASSIGNED / BACKLOG | any status, WHERE `session IS NULL` |
| QUEUED | `QUEUED`, `PRECHECK`, `READY`, `DISPATCHING`, `DISPATCH_UNCERTAIN` |
| RUNNING | `RUNNING` |
| BLOCKED | `BLOCKED`, `WAITING_SESSION`, `PAUSED` |
| VERIFYING | `VERIFYING` |
| REWORK | `QUEUED` with a non-empty `rework_task_id`/`rework_reason` (via the Handoff link, §20.4) — never a new status value; a rework is just a normal task re-entering the SAME state machine, tagged with why |
| DONE | `COMPLETED` (and, for a coding task, only once its Handoff also reaches `INTEGRATED` — see §20.1's own parent-completion rule below) |
| CANCELLED | `CANCELLED`, `SKIPPED` |

**Parent completion rule** (task's own explicit requirement): a parent
task with children is `COMPLETED` only when every REQUIRED child
`depends_on` it AND (for a coding parent) its own Handoff has reached
`INTEGRATED` — never when a worker merely stops reporting activity.
This reuses `_dependency_satisfied_locked`'s own existing gate logic
(already real, already used for ordinary `depends_on` — see §7), not a
new completion-detection mechanism.

### 20.2 PM / Orchestrator Agent — skill-based routing

A new role, not a new queue. Reads READY/UNASSIGNED tasks + a new
**Capability Profile** per node/session (a new store — reuse `node_
registry.py`'s own SQLite/dataclass pattern, don't invent a new one):
`os`, `runtime_tools` (JSON list — e.g. `dotnet`, `wpf`, `docker`,
`playwright`), `project_affinity`, `role` (`developer`/`qa`/
`integration`/`infra`/`docs`), `skills` (JSON list, versioned/
confidence-scored per the task's own explicit ask), `current_load`/
`queue_depth`, `online`/`recovering`/`busy`/`idle`, `permissions`
(effective read/input, reused from the existing grants model, never
re-derived independently).

**Routing algorithm, two phases, never one score blending everything:**
1. **Hard constraints (eligibility gate, deterministic, no ML/LLM):**
   OS match, required_capabilities ⊆ session's own capabilities,
   project affinity if the task declares one, permission (session must
   already have effective input), online (not OFFLINE/RECOVERING),
   not already at its 1-active-task limit, not in the excluded list, a
   `pinned_session`/`pinned_node` (if set) must itself pass every other
   check or the task goes `BLOCKED`/`NEEDS_HUMAN` with reason
   `"pinned session/node is not eligible: <why>"` — **never silently
   reroute away from an explicit human pin.**
2. **Soft scoring (only among sessions that passed phase 1):** project
   affinity strength, skill-match count, idle/load, queue depth,
   fairness (starvation prevention — a session that hasn't been picked
   in N cycles gets a small boost), locality (same node as a related
   task if relevant). The WINNING session's own score + the reasons
   that contributed become `routing_reason` (task's own explicit
   "Assigned to window2 because Windows + idle + project match"
   example) — stored on the task row, shown on its Kanban card/detail.
3. **No eligible session:** task stays `UNASSIGNED`/`BLOCKED` with
   `coordinator_reason = "NO_ELIGIBLE_WORKER"` — **never dropped**,
   retried automatically once a capability profile changes or a node
   reconnects (the existing node-reconnect/heartbeat machinery already
   real in `node_registry.py` is the trigger, not a new poll).

**Modes** (same posture as Supervisor's own `observe_only`/`suggest_
only`/`approved_auto_continue` — reuse the vocabulary, not a fourth
one): `OFF` (PM never assigns anything), `SUGGEST` (proposes a worker +
`routing_reason`, a human approves before it actually dispatches —
**the recommended production default**), `AUTO` (assigns automatically
— **only after a live disposable E2E pass**, same standing rule this
whole project already applies to Queue auto-dispatch and Supervisor
policies). Per-project/session opt-in, never a single global switch
that silently starts claiming every existing UNASSIGNED task the
moment AUTO is flipped on anywhere.

#### 20.2a Implementation note (2026-09-07, VERIFIED — capability schema + deterministic router)

**Status: VERIFIED** (unit tests + real disposable-session live E2E).
Built: the Capability Profile store, the two-phase deterministic
router, SUGGEST/AUTO modes, the append-only decision audit trail, and
the Kanban card's own `routing_reason` display. **NOT yet built**: any
PM auto-loop (every routing decision here is triggered by an explicit
call — `terminal_pm_route_task`/`terminal_pm_route_all_unassigned` —
never a background poll thread; see this section's own MODE note
above, same "SUGGEST first, prove it live, only then consider more
automation" rollout discipline as Queue auto-dispatch/Supervisor v2),
`current_load`/`queue_depth` are NOT persisted on the profile (derived
live at decision time instead — see below), and the Planner/git-
isolation/Merge-Agent/Phase A-E sections remain entirely PLANNED.

- `pm_store.py` (new) — `capability_profiles` (`(node_id, session)`
  composite key, same convention `session_registry.py`'s own
  `SessionRecord` already established) + `pm_decisions` (an APPEND-ONLY
  log, one row per routing decision ever made, never overwritten — the
  real audit trail this section's own explainability requirement asks
  for, queryable by `task_id`).
- `pm_router.py` (new) — the pure, deterministic two-phase algorithm
  (`route_task`), no I/O, unit-tested in isolation. Task-side routing
  requirements (`required_os`, `required_capabilities`,
  `required_role`, `project`, `pinned_session`, `pinned_node`,
  `excluded_sessions`) are read from **`task["metadata"]`**, NOT new
  `queue_tasks` columns — same deliberate implementation choice as
  §20.1a's own `UNASSIGNED_LANE` decision, made for the same reason:
  the router only ever READS a task and, when it decides to assign one,
  calls the EXISTING `QueueService.assign_task` — zero change to
  `queue_store.py`'s schema or `queue_engine.py`'s dispatch loop needed.
  `queue_depth` is a SOFT-scoring factor only in this checkpoint, NOT a
  hard "1-active-task" gate (that hard limit is already enforced for
  real by the dispatch engine itself, §7) — a true per-role/session WIP
  CAP as a hard routing gate is Phase A's own future work (§20.6),
  intentionally not pulled forward into this checkpoint.
- `pm_service.py` (new) — the I/O glue: assembles real `WorkerCandidate`
  rows from `PMStore` + live `QueueService.pending_counts()` (queue
  depth) + an optional `ControllerService.list_nodes()` (node-online
  check, best-effort True if no controller wired) + an optional
  injected `permission_checker(node_id, session) -> bool` closure (the
  real caller — `mcp_app.py`/`dashboard.py`/`server_http.py` — wires
  this to the local node's own `TerminalService._input_authorized`;
  defaults to True for a remote node or when unwired, since routing is
  a placement decision, not itself a security boundary — the actual
  send still goes through the real, full authorization gate regardless
  of what PM decided). `route_task` (SUGGEST computes + persists only;
  AUTO also calls `assign_task` for real), `approve_routing` (the only
  way a SUGGEST decision becomes a real assignment), `route_all_
  unassigned` (one explicit, manual sweep — not a loop), `eligible_
  workers`/`explain` (read-only explainability).
- MCP tools (`mcp_app.py`): `terminal_pm_set_capability`, `terminal_pm_
  list_capabilities`, `terminal_pm_delete_capability`, `terminal_pm_
  eligible_workers`, `terminal_pm_route_task`, `terminal_pm_approve_
  routing`, `terminal_pm_route_all_unassigned`, `terminal_pm_explain`.
- Dashboard: `/dashboard/api/tasks/board` enriches each card with its
  own latest `routing_reason`/`pm_decision_status` (one bulk
  `latest_decisions_for_tasks` read, never N+1) — the Global Tasks
  Kanban page shows it as a small line under the card's own title/meta
  chips, straight from the real, persisted decision, never a client-
  side guess. `queue_store.py`/`queue_service.py` themselves stay
  completely decoupled from `pm_store.py` (a lower layer never depends
  on a feature built on top of it) — the enrichment happens at the
  dashboard-route layer only.
- Tests: `tests/test_pm_router.py` (24, pure algorithm — hard gate per
  constraint, pin eligible/ineligible/nonexistent, NO_ELIGIBLE_WORKER
  never drops, deterministic tie-break, the explicit "Windows/WPF never
  to Linux" acceptance example), `tests/test_pm_store.py` (16,
  persistence), `tests/test_pm_service.py` (18, real I/O including
  `permission_checker`/controller-online injection), `tests/
  test_pm_mcp_tools.py` (8, the real MCP tool surface), 2 new in
  `tests/test_task_manager_ui.py` (the dashboard route's own
  enrichment). Full suite green.
- **Live evidence** (real disposable tmux sessions, real grants, the
  real MCP `server.call_tool` path — never `window`/`window2`/`wtest`):
  3 sessions with distinct real Capability Profiles (2 `linux`, 1
  `windows`, different project affinities/skills); a Windows+WPF-
  required task correctly routed ONLY to the Windows session (`local/
  pme2e-windows-a`) — the Linux sessions correctly listed as ineligible
  with the real OS-mismatch reason via `terminal_pm_eligible_workers`;
  SUGGEST mode correctly left the task in Backlog until `terminal_pm_
  approve_routing` was called, which then performed a real assignment;
  a `pinned_session` pointing at an OS-ineligible session correctly
  came back `BLOCKED` with an explicit reason, never silently rerouted
  to the otherwise-perfectly-eligible alternative also present; AUTO
  mode correctly assigned a project/skill-matched task immediately; a
  task requiring a nonexistent OS (`macos`) correctly stayed
  `NO_ELIGIBLE_WORKER`/UNASSIGNED, never dropped; `terminal_pm_explain`
  correctly showed the full 2-entry decision history (SUGGESTED then
  APPROVED_AND_ASSIGNED) for the first task. Disposable sessions/state
  cleaned up after.

### 20.3 Planner (task breaking)

Decides, for a newly-created UNASSIGNED task, whether to split it —
based on declared/estimated complexity, module count, dependency
shape, and how many eligible workers could run parts in parallel.
Small tasks are never split just because splitting is possible. When it
does split: each child gets its own `parent_task_id`, its own
`acceptance_criteria`, and a real `depends_on` DAG (reusing §7's
existing dependency mechanism) — genuinely-parallel children get no
mutual dependency; children whose Planner-estimated `changed_paths`/
module footprint plausibly overlaps get a serializing `depends_on`
instead of running concurrently (task's own explicit conflict-
prevention ask — advisory only, git itself remains the real source of
truth for an actual conflict, this is purely a *scheduling* hint to
avoid a predictable, wasteful conflict). Same three modes as the PM
(`OFF`/`SUGGEST`/`AUTO`), same SUGGEST-first production rollout rule.
A task with genuinely insufficient acceptance criteria to plan against
becomes `NEEDS_CLARIFICATION`/`NEEDS_HUMAN` — the Planner never guesses
scope into existence.

#### 20.3a Implementation note (2026-09-07, VERIFIED — split infrastructure, no auto-splitter)

**Status: VERIFIED** (unit + real disposable-session live E2E, full
parent-completion lifecycle proven end to end). **Deliberately NOT
built in this checkpoint: automatic complexity/module-count-based
splitting.** This project's own standing rule is deterministic-first,
no ML/LLM call to invent scope that doesn't already exist (same
posture as `coordinator.py`'s own disclosed `scope_reasoner`), and the
task's own explicit "không chia vụn vô nghĩa" (never split into
meaningless fragments) rules out a naive heuristic splitter that could
easily produce exactly that. What was built instead is the real, safe,
tested INFRASTRUCTURE for a split whose decomposition (child titles/
prompts/acceptance criteria/dependency shape) is supplied by the
CALLER — a human, ChatGPT, or a future smarter Planner mode — never
invented from a crude estimate.

- `planner_store.py` (new) — `plan_proposals`, an append-oriented log
  of every split ever proposed (`PROPOSED`/`APPROVED`/`REJECTED`/
  `NEEDS_CLARIFICATION`), same store pattern as every other feature in
  this project. `parent_task_id`/`acceptance_criteria` live in each
  CHILD's own `metadata` (via the existing `QueueService.create_task`)
  — same deliberate metadata-based implementation choice as §20.1a/
  §20.2a, for the same reason: zero change to `queue_store.py`'s
  schema needed.
- `planner_service.py` (new) — `propose_split` (validates: parent must
  still be `QUEUED` — `PARENT_NOT_SPLITTABLE` otherwise, which also
  correctly refuses re-splitting an already-split parent since it's
  always parked in `BLOCKED` by then; every child needs a real
  `prompt`+`acceptance_criteria` or the WHOLE proposal becomes
  `NEEDS_CLARIFICATION`, never partially applied); `SUGGEST` mode
  persists the proposal without creating anything, `AUTO` creates the
  children immediately; `approve_split` applies a pending `SUGGEST`
  proposal for real. `depends_on_indices` on a child spec wires a real
  `depends_on` to an earlier sibling's own newly-minted task_id — reuses
  §7/§8's existing, already-verified dependency mechanism (checked at
  claim time), never a second one. `children_progress`/`complete_
  parent_if_children_done` implement §20.1's own parent-completion rule
  (COMPLETED only once every non-cancelled/non-skipped child is too) —
  an explicit, manual call, no background loop in this checkpoint (same
  "no auto-loop yet" posture as PM's own §20.2a).
- **New, narrowly-additive state-machine edge:** `queue_store.py`'s
  `VALID_TRANSITIONS[BLOCKED]` now also allows `COMPLETED` — a split
  parent is parked in `BLOCKED` the moment its children are created
  (it has no more real work of its own to dispatch) and reaches
  `COMPLETED` only via this one new edge. Guarded entirely at the
  CALLER (`complete_parent_if_children_done` refuses unless `metadata.
  is_split_parent` is `True`) — every OTHER existing `BLOCKED` case (a
  real Coordinator refusal) is completely unaffected; `QUEUED`/
  `SKIPPED`/`CANCELLED` remain its only other reachable states, proven
  by a dedicated regression test.
- MCP tools (`mcp_app.py`): `terminal_task_split`, `terminal_task_
  approve_plan`, `terminal_task_children`, `terminal_task_complete_
  parent`.
- Dashboard: a split parent's Kanban card shows real `child_progress`
  (`x/y done`, from `planner.children_progress`) — enriched at the
  `/dashboard/api/tasks/board` route layer, same pattern as PM's own
  `routing_reason` enrichment (queue_service.py/queue_store.py stay
  decoupled from planner_store.py too).
- Tests: `tests/test_planner_store.py` (5), `tests/test_planner_
  service.py` (18, including the guarded `BLOCKED`->`COMPLETED` edge, a
  cancelled child not blocking completion, and dependency wiring),
  `tests/test_planner_mcp_tools.py` (4), 2 new in `tests/test_queue_
  store.py` (the state-machine edge itself), 1 new in `tests/
  test_task_manager_ui.py` (dashboard enrichment). Full suite green.
- **Live evidence** (real disposable tmux sessions, real grants, the
  real MCP `server.call_tool` path — never `window`/`window2`/
  `wtest`): a parent task split (SUGGEST, verified nothing was created;
  then approved) into 2 real children on 2 different real sessions, the
  second declaring `depends_on_indices: [0]` — confirmed its real
  `depends_on` correctly referenced the first child's own newly-minted
  task_id, and the Kanban board correctly showed 1 `blocked_review`
  (parent) + 2 `queued` (real children, in their real sessions); `pm_
  explain`-style `terminal_task_complete_parent` correctly refused
  while children were still open; both children then marked `COMPLETED`
  (direct transition — the actual dispatch/completion mechanism itself
  is separately, already VERIFIED, §7's own P0 Queue+Supervisor live
  test; this step exercises the Planner's own completion rule, not a
  re-verification of dispatch) and `terminal_task_complete_parent`
  correctly completed the parent too — final board showed all 3 tasks
  `done`. Disposable sessions/state cleaned up after.
- Still **PLANNED**, not built: any complexity/module-count-based
  AUTO-suggestion of WHAT to split into (the caller must always supply
  the decomposition); the Kanban's own dependency-tree visualization
  (only a flat `x/y done` count exists so far).

### 20.4 Git isolation policy + Integration/Merge Agent (mostly REUSE)

**Real, significant reuse found:** `integration_store.py`'s existing
`Handoff` dataclass (§9, already VERIFIED, already live-tested) already
carries `branch`, `commit_sha` (== "head_sha"), `base_sha`,
`changed_paths`, `test_summary`, `conflict_detected`, `conflict_paths`,
`rework_task_id`, `rework_reason`, `merge_commit_sha`, `origin_session`
(== "owner_session") — this is already almost exactly the git-isolation
metadata this task asks for. The ONLY genuinely new fields needed:
`worktree_path` and a `parent_task_id`/`task_id` link consistent with
§20.1's own column (Handoff already has `task_id`) — a small, additive
migration on the EXISTING `handoffs` table, not a new one.

**Policy (new, on top of existing infrastructure):**
- A coding task defaults to requiring its own isolated worktree +
  branch (`task/T<id>-<short-title>`), created from the canonical
  base/integration SHA — never a task's own worker directly on `main`/a
  shared working tree, unless the task is explicitly exempted
  (docs/chore) by policy.
- Coordinator's existing pre-dispatch gate (§8, already real —
  `git_repo_evidence`/the diverged-branch check) is the natural place
  to ALSO verify worktree/branch isolation before letting a task
  proceed: if the target session is currently on shared `main` or
  another task's own worktree, `BLOCKED`/needs repair, never "continue
  anyway".
- A worker never merges into integration/main itself — it produces the
  SAME structured Handoff this project already has, unchanged in kind.
- The Integration/Merge Agent (§9, already real) is the ONLY role
  allowed to merge, per policy — extend its own routing so a Handoff is
  specifically assigned to a session/node whose Capability Profile
  (§20.2) declares the `integration` role + `git-integration`/`test-
  integration` skills, never to an ordinary coding worker.
- Conflict handling: the Integration Agent may only auto-resolve
  mechanical conflicts (whitespace, trivial non-overlapping-intent
  merges) — a semantic conflict is `REWORK_REQUIRED` back to the
  owning task(s), never business-logic decisions made by the merge
  step itself. Rebase policy: a still-QUEUED task may refresh its own
  base before starting; a RUNNING task is never force-rebased mid-
  flight; a project may require a rebase-and-rerun-tests step
  immediately before a Handoff is accepted.

#### 20.4a Implementation note (2026-09-07, VERIFIED — worktree isolation + mechanical-only conflict retry)

**Status: VERIFIED** (unit tests with real git subprocess calls
throughout — no mocks — + a real disposable-tmux-session live E2E).
Confirms this section's own "mostly REUSE" framing was accurate: the
Coordinator's existing `expected_cwd` check (§8, unchanged, zero new
code — its own error message already said "expected worktree" before
this checkpoint ever existed) is the ENTIRE isolation enforcement
mechanism; this checkpoint only needed to create a real worktree and
point that field at it.

- `git_worktree.py` (new) — real, bounded `git` subprocess calls
  (`create_worktree`/`remove_worktree`/`worktree_status`/
  `list_worktrees`), same fail-closed/no-`shell=True` posture as every
  other git-touching module in this project. `create_worktree` resolves
  `base_ref` to a real commit SHA first (fail-closed if unresolvable),
  refuses to reuse an already-existing branch name.
- `git_isolation_service.py` (new) — `GitIsolationService.
  create_isolated_task` creates the real worktree+branch FIRST (branch
  name `task/<short-random-id>-<slug>`, not literally `task/T<task_id>-
  <slug>` — the worktree must exist before `QueueService.create_task`
  mints the real task_id, so a short random id is used for the branch
  string instead; the real correlation always lives in `metadata.
  git_isolation`, traceability is never lost), then creates the task
  with `metadata.expected_cwd` pointed at it — reusing the Coordinator's
  existing check, not extending it. Rolls back (removes) the worktree
  if task creation itself fails (e.g. an invalid session name), so a
  failed call never leaks an orphaned worktree. `worktree_status_for_
  task`/`cleanup_worktree_for_task` (explicit, manual — no auto-cleanup
  in this checkpoint, a worktree may still be genuinely needed for
  debugging after its task reaches a terminal state).
- `integration_store.py`'s `publish_handoff_for_completed_task` now
  also carries `metadata.git_isolation.worktree_path` into the
  resulting Handoff's own `artifacts["worktree_path"]` — same "carry
  task metadata into artifacts, no schema change" precedent as the
  pre-existing `docs_exempt` field.
- **Mechanical-only conflict auto-resolve** (`integration_engine.py`'s
  `_merge`): a new, project-level, OFF-by-default opt-in
  (`allow_mechanical_conflict_resolution`, additive `integration_
  pipelines` column) — on a real merge conflict, ONE retry using git's
  own `-X ignore-all-space` merge strategy (a real, well-defined git
  feature that only ever affects whitespace-only differences) before
  falling back to the pre-existing `REWORK_REQUIRED` path. A retry that
  still conflicts (a genuine content/semantic conflict) falls through
  completely unchanged — never a custom content-guessing resolution of
  this codebase's own (task's own explicit "không tự đoán business
  logic"). A successful mechanical resolution is disclosed on the
  Handoff itself (`artifacts.mechanical_conflict_auto_resolved: true`
  + the original conflicted paths) — never silently indistinguishable
  from an ordinary clean merge.
- **Not built in this checkpoint** (disclosed): PM-based routing of the
  Integration Agent to a specific capability-profiled session — the
  existing architecture's Integration Agent is "a pure backend engine
  with NO real interactive tmux session of its own" (§9), which doesn't
  cleanly map onto PM/session routing without a deeper redesign; forcing
  it now risked an ill-fitting change. The rebase-before-start policy
  (a still-QUEUED task refreshing its own base) is also not built yet.
- Tests: `tests/test_git_worktree.py` (11, real git subprocess calls),
  `tests/test_git_isolation_service.py` (9, including 2 real
  Coordinator-gate integration tests proving the reuse claim directly),
  `tests/test_git_isolation_mcp_tools.py` (4), 3 new in `tests/
  test_integration_engine.py` (real whitespace-only conflict correctly
  auto-resolved; a real content conflict still correctly routes to
  REWORK_REQUIRED even when opted in; disabled-by-default unchanged
  behavior), 2 new in `tests/test_integration_hook.py` (worktree_path
  threading). Full suite green.
- **Live evidence** (a real disposable git repo + a real disposable
  tmux session + the real MCP `server.call_tool` path — never `window`/
  `window2`/`wtest`): created a real isolated task via `terminal_task_
  create_isolated` — confirmed a real worktree directory + branch now
  exist on disk; `terminal_worktree_status` reported it `exists`/clean;
  the session's own REAL, live cwd (read via `terminal_status`) was
  still the base repo, not the worktree — the Coordinator's existing
  gate correctly returned `NEEDS_HUMAN` with the pre-existing "expected
  worktree" reason text; a real `cd <worktree_path>` was sent through
  `terminal_send_text` (`SUBMIT_CONFIRMED`), the session's own live cwd
  was re-observed and now genuinely matched; the SAME Coordinator gate
  (zero code changed) now correctly returned `READY`. Disposable
  session/repo/state cleaned up after.

### 20.5 Kanban UI (Global Tasks)

A new dashboard screen/route, reading the SAME queue store as
everything else (§20.0's own binding rule) — never a second read path.
Default columns: **Backlog | Queued | Running | Blocked/Review | Done**
(lifecycle-based, not one column per session — a fleet with many
sessions would make a per-session-column board unreadable). Each
RUNNING/QUEUED card shows its session/node/role as a real, visible
label (never buried in a tooltip) — task's own explicit "card phải cho
biết rõ đang ở session nào, node nào, role gì". Filters: project,
session/owner, node, priority, status, risk, type
(feature/bug/incident/release, once §20.6 exists), stale/blocked. Group-
by-session view shows each session as its own lane with Current +
Next, matching the "session như developer" mental model. A parent task
card shows real child progress (`x/y done`, reusing §20.1's own parent-
completion rule, never a separately-computed percentage). Click opens
task detail (history/audit/state transitions/dependency tree/git
branch+worktree info/Handoff status once relevant). The existing
per-session Task button + pending badge (§3, VERIFIED 2026-09-07) stays
exactly as-is and, when clicked, opens this same Kanban filtered to
that one session — never a second, competing task view. Drag/drop
deferred behind explicit action-based moves first (task's own
"nếu dễ gây race, dùng actions trước") — Backlog→session-queue and
reorder-within-queue as ordinary button/API actions before any drag/
drop is attempted.

### 20.6 Startup Software Operating Model (Phases A–E, PLANNED, docs-
only — no code yet, per explicit instruction)

All phases below build on §20.0–20.5 above, never a separate system.
Each phase gets its own real live-verification pass before being marked
VERIFIED — none of this is built yet.

**Phase A — Delivery discipline:**
- **Definition of Ready**: Coordinator's existing pre-dispatch gate
  (§8) extended to require goal/scope/`acceptance_criteria`/project/
  priority/dependencies/`required_capabilities`/`required_os`/
  `risk_level` before a task may leave UNASSIGNED — missing any of
  these is `NEEDS_CLARIFICATION`, never guessed.
- **Definition of Done**: a coding task is not `COMPLETED` on a
  worker's own say-so — real evidence required (test output, the
  structured completion marker already real in §7, a Handoff for
  anything that touched code, `docs/REQUIREMENTS.md`/`AGENT_GUIDE`
  update for a behavior change per the existing living-requirements
  gate). A release task (Phase C) additionally needs real deploy/health
  evidence.
- **WIP limits**: the existing one-active-task-per-session enforcement
  (§7, already real) extended to a configurable NEXT/QUEUED cap per
  role/session — PM never floods a worker's own queue past it.
- **Ownership/affinity**: PM prefers continuity (the same session/
  project pairing) over reassigning for its own sake — reassignment
  needs a real reason (offline/blocked/overload/explicit routing
  policy), recorded via `routing_reason`.
- **Risk classification**: `risk_level` (§20.1) drives stronger gates
  for `HIGH`/`CRITICAL` (auth, migrations, permissions, infra, deploy,
  security) than a small UI task — exact gate strength is a per-project
  policy, not hardcoded here.
- **Conflict prediction**: the Planner's own overlap-based serialization
  (§20.3) is this requirement, not a separate mechanism.

#### Phase A implementation note (2026-09-07, VERIFIED — DoR, WIP limits, risk-level gate)

**Status: VERIFIED** for the 3 pieces actually built (unit tests + a
real disposable live E2E). **Not built**: an "ownership/affinity
prefers continuity" scoring factor distinct from the already-real
`project_affinity` soft-score (disclosed scope cut — see below); a
project-level wiring point for `CoordinatorGate`'s new `require_
approval_for_risk_levels` constructor param (it is real and fully
tested, but no project currently opts into it by default — a future
increment would thread a per-project config value into wherever
`CoordinatorGate()` is actually constructed for a real project's own
queue_engine).

- **Definition of Ready** (`dor_gate.py`, new) — deliberately **OPT-IN
  per task** (`metadata.dor_required: true`), never a blanket
  requirement retrofitted onto every existing task: this project's own
  Kanban/PM/Planner checkpoints already created (and continue to
  create, in their own test suites) many small tasks with none of
  these fields declared — a mandatory gate would be a breaking
  behavior change with no real safety value for an opportunistic task.
  When opted in, requires `title`, `acceptance_criteria`, `project`,
  and a valid `risk_level` (`LOW`/`MEDIUM`/`HIGH`/`CRITICAL`) — missing
  any is `NEEDS_CLARIFICATION`, never guessed. **Scope cut, disclosed**:
  `required_os`/`required_capabilities` are read if present but NOT
  mandated (their absence is itself a meaningful "no specific
  requirement" declaration); `dependencies` (`depends_on`) is not
  checked either — it is always a real column, and there is no way to
  distinguish "declared none, deliberately" from "never considered"
  from the data alone. Enforced at the two real "leaving UNASSIGNED"
  points: `QueueService.assign_task` and `QueueService.create_task`
  (when created already-assigned). `terminal_task_check_dor` is a
  read-only inspection tool.
- **WIP limits** (`pm_router.py`/`pm_store.py`) — `CapabilityProfile.
  max_queued` (None = unbounded, the unchanged default) is now a HARD
  routing gate (not just PM's existing soft `idle_bonus` score): a
  candidate whose `queue_depth >= max_queued` is ineligible outright,
  same as an OS/capability mismatch — "PM never floods a worker's own
  queue past it" (task's own words). Set via `terminal_pm_set_
  capability(..., max_queued=N)`.
- **Risk classification** (`coordinator.py`) — `CoordinatorGate`
  gained a new, OFF-by-default constructor param `require_approval_
  for_risk_levels: tuple[str, ...] = ()`. A project that opts specific
  levels in (e.g. `("HIGH", "CRITICAL")`) requires an explicit
  `metadata.risk_approved: true` (a real human/PM sign-off, never
  inferred from the prompt text) before a task declaring one of those
  levels may reach READY — a task with no `risk_level` declared at all
  is never gated by this, regardless of project policy.
- **Ownership/affinity**: substantially covered by the already-real
  `project_affinity` soft-scoring factor (§20.2a) + every routing
  decision already being recorded with a real `routing_reason` — a
  distinct "prefers the SAME session that did prior related work"
  factor (beyond project affinity) was not built this pass, disclosed
  as a scope cut rather than silently claimed.
- **Definition of Done / Conflict prediction**: both already
  substantially real via existing, previously-VERIFIED mechanisms
  (§7's `mark_completed_with_evidence` evidence requirement + the
  completion-marker protocol + the living-requirements doc gate for
  Done; §20.3's own `depends_on_indices` overlap-based serialization
  for conflict prediction) — no new code needed for either, per this
  section's own explicit "extend, don't rebuild" framing.
- Tests: `tests/test_dor_gate.py` (9), 5 new in `tests/test_queue_
  service_fleet_views.py` (assign_task/create_task DoR wiring), 4 new
  in `tests/test_pm_router.py` + 1 new in `tests/test_pm_service.py`
  (WIP limit), 5 new in `tests/test_coordinator.py` (risk-level gate),
  5 new in `tests/test_dor_gate_mcp_tools.py` (the real MCP tool
  surface for all three). Full suite green.
- **Live evidence** (real disposable tmux sessions, real grants, the
  real MCP `server.call_tool` path — never `window`/`window2`/
  `wtest`): a DoR-opted, incomplete task was correctly refused
  assignment (`NEEDS_CLARIFICATION`, all 3 missing fields listed);
  re-submitted complete, `terminal_task_check_dor` correctly reported
  `READY`; assigned to a session capped at `max_queued=1` — a second
  task then correctly saw that session as the ONLY ineligible
  candidate (`"WIP limit reached: 1 queued >= max_queued 1"`) and PM
  correctly routed it to the uncapped alternative instead; a real
  `CoordinatorGate` configured with `require_approval_for_risk_levels`
  correctly returned `NEEDS_HUMAN` for the real, real-store HIGH-risk
  task with no `risk_approved` flag set. Disposable sessions/state
  cleaned up after.

**Phase B — Quality/integration:**
- The Integration Agent (§20.4) already independently builds/lints/
  tests a Handoff rather than trusting the worker's own report — this
  requirement is already substantially real (§9), extend rather than
  rebuild.
- Rework loop bounds: `max_attempts`/`coordinator_attempts` (already
  real, §7/§8) already trip to `NEEDS_HUMAN` past a threshold — reuse,
  don't reinvent, for the rework-specific loop too.
- Incident lane: `type: "incident"` + `risk_level` combination with its
  own fast-track dispatch policy (still audited, never bypassing the
  Coordinator gate entirely) — a new, small policy on top of the
  existing priority field, not a parallel queue.

#### Phase B implementation note (2026-09-07, VERIFIED — incident lane)

**Status: VERIFIED** (unit tests + a real disposable live E2E proving
the actual dispatch engine, not just the store's own SQL ordering).
Rework loop bounds and independent build/lint/test needed no new code
(already real via `max_attempts`/`coordinator_attempts`, §7/§8, and the
Integration Agent's own real pre-merge review + targeted/full
regression pipeline, §9) — reused exactly as this section's own
"reuse, don't reinvent" framing asks.

- `QueueService.create_incident_task`/`INCIDENT_PRIORITY` (new) — NOT a
  parallel queue: an incident is an ordinary task in the SAME lane,
  tagged `metadata.type="incident"`, given a priority (a fixed constant,
  `1000`, comfortably above this project's own typical small-int
  priorities — disclosed as a simple, predictable policy rather than a
  dynamically-computed "always above current max", which would be a
  moving target) that dispatches it ahead of ordinary QUEUED work via
  `queue_store.py`'s own real, already-verified `ORDER BY priority DESC,
  position ASC` claim ordering — zero new dispatch-engine code. Still
  goes through `create_task`'s own DoR gate (if a project opted in) and
  the ordinary Coordinator review — never a bypass. `risk_level` is
  folded into the SAME metadata field Phase A's own risk-approval gate
  reads, so an incident is not exempt from that gate just for being an
  incident.
- `QueueService.list_active_incidents` — every non-terminal incident
  task, fleet-wide, real audit/visibility.
- MCP tools: `terminal_task_create_incident`, `terminal_list_active_
  incidents`.
- Tests: 6 new in `tests/test_queue_service_fleet_views.py` (tagging,
  real dispatch-ordering proof via `claim_next_task`, DoR interaction,
  risk_level carry-through, active-incident listing), 3 new in `tests/
  test_incident_lane_mcp_tools.py`. Updated MCP tool-count assertions
  (115 -> 117). Full suite green.
- **Live evidence** (a real disposable git-repo-backed tmux session,
  real grants, the real MCP `server.call_tool` path — never `window`/
  `window2`/`wtest`): a normal task queued first, then a real incident
  task created SECOND — `terminal_queue_run_once` (the real Coordinator
  + dispatch engine, not a direct store call) correctly claimed the
  INCIDENT first (`CLAIMED`, `PRECHECK`), proving the fast-track policy
  works through the actual production dispatch path, not just in
  isolation. Disposable session/repo/state cleaned up after.

**Phase C — Release/environments:**
- New lifecycle states layered ON TOP of `COMPLETED`/`INTEGRATED` for a
  release-type task: `MERGED -> RELEASE_CANDIDATE -> DEPLOYING ->
  DEPLOYED -> VERIFIED_PROD / ROLLED_BACK` — a genuinely new, small
  state machine (its own `Migration`), not an overload of the existing
  task states.
- Environment model (dev/test/staging/prod) + permission boundaries —
  an ordinary coding worker has no production deploy/secret access by
  default; a dedicated Release/Deploy Agent role (own Capability
  Profile entry, §20.2) is the only one routed a release task, gated by
  an explicit human approval for prod given `risk_level`.
- A known-good artifact/commit + rollback plan is a required field on
  any production release task, not optional.

#### Phase C implementation note (2026-09-07, VERIFIED — release lifecycle state machine)

**Status: VERIFIED** (unit tests over both new modules + real MCP-tool-
surface tests driving the actual persisted state machine through the
real `server.call_tool` path). **No tmux/session dimension to this
checkpoint** (a release is not something dispatched to a session) --
the "live" bar here is the real MCP protocol + real SQLite persistence,
not a disposable tmux session; disclosed explicitly rather than forcing
an artificial session into a workflow that has none.

- `release_store.py` (new) — a genuinely new, small state machine
  (`MERGED -> RELEASE_CANDIDATE -> DEPLOYING -> DEPLOYED ->
  VERIFIED_PROD`, `ROLLED_BACK` reachable from `DEPLOYING`/`DEPLOYED`/
  `VERIFIED_PROD` — `VERIFIED_PROD` is deliberately NOT a dead end, a
  real production issue can be discovered even after verification),
  its own table + `Migration`, referencing a `task_id` for provenance
  only — never an overload of `queue_store.py`'s own task states, same
  "new concept, own table, zero ripple into the existing dispatch
  engine" discipline as every other checkpoint in this section.
- `release_service.py` (new) — the two explicit policies: (1) a `prod`
  release REQUIRES `known_good_artifact_ref` + `rollback_plan` at
  creation time (refused, `PROD_RELEASE_REQUIRES_ROLLBACK_PLAN`,
  otherwise); (2) advancing a `prod` release into `DEPLOYING` requires
  an explicit `approved_by` (a real, non-empty identity string) —
  refused (`PROD_DEPLOY_REQUIRES_APPROVAL`) otherwise, never auto-
  approved regardless of `risk_level`.
- **Environment model / "no production deploy access by default" —
  disclosed as PARTIAL:** `environment` (dev/test/staging/prod) is a
  real, validated field; the explicit-approval-string requirement above
  is real and enforced; every transition is recorded in a real,
  queryable `release_events` audit trail. What is NOT built: a
  technical, session-identity-based enforcement of WHO is allowed to
  supply that approval (a real Release/Deploy Agent role check) — this
  project's MCP tool layer has no caller-identity system wired to
  Capability Profiles for that purpose today (an MCP tool call's caller
  is ChatGPT/a human/an agent, not tied to a specific session row this
  project could check a `role` field against). `CapabilityProfile.role`
  already supports a free-string "release"/"deploy" value with zero
  schema change if/when that enforcement is ever built.
- MCP tools: `terminal_release_create`, `terminal_release_advance`,
  `terminal_release_rollback`, `terminal_release_status`, `terminal_
  release_list`.
- Tests: `tests/test_release_store.py` (21, including every valid/
  invalid transition pair and the `VERIFIED_PROD` roll-back-is-still-
  possible case), `tests/test_release_service.py` (15, both required-
  field policies), `tests/test_release_mcp_tools.py` (5, a full real
  dev lifecycle through the real MCP path, the prod-creation gate, the
  prod-deploy-approval gate, rollback, project filtering). Updated MCP
  tool-count assertions (117 -> 122). Full suite green.

**Phase D — Operations/knowledge:**
- End-to-end audit trail: this project already has real audit stores
  (`audit.py`, `queue_events`, `Handoff` history) — Phase D is mostly
  "wire the NEW transition types (split, PM routing decision, worktree/
  branch, deploy/rollback) into the SAME existing audit mechanisms",
  not a new audit system.
- Knowledge capture: `session_knowledge.py` (§13, already real) already
  captures real session output/checkpoints searchable by project — a
  post-task/incident ADR/gotcha note is a `terminal_knowledge_
  checkpoint` call, not a new store. Never persist secrets into it.
- Daily/weekly PM summary + backlog hygiene (stale/duplicate/obsolete
  detection, human controls destructive close) — a new, small
  aggregation over the existing task/handoff/node data, not a new
  source of truth.
- Resource/cost awareness reuses `host_metrics.py`'s existing real CPU/
  RAM collection (already used for node heartbeats, §12) plus queue
  depth already computed for §3's own badge.

#### Phase D implementation note (2026-09-07, VERIFIED — PM summary + backlog hygiene)

**Status: VERIFIED** for the one genuinely new piece (unit tests + real
MCP-tool-surface tests, same "no tmux/session dimension, the real MCP-
protocol tests are this checkpoint's own live evidence" posture as
Phase C). The other three Phase D items needed **NO new code** — this
is disclosed explicitly rather than silently claimed as "built":

- **End-to-end audit trail**: already real and already complete for
  every checkpoint built in this whole Unified Task System effort —
  `pm_decisions` (§20.2a), `plan_proposals` (§20.3a), `queue_events`
  (§7, including the Planner's own `SPLIT_INTO_CHILDREN`/`PARENT_
  COMPLETED_VIA_CHILDREN` event types and the git-isolation checkpoint's
  own worktree metadata riding on existing task events), `release_
  events` (§20.6 Phase C) — each subsystem's own dedicated, real,
  append-only event log, exactly the "one canonical store per concept"
  discipline this whole system has followed throughout. Deliberately
  NOT also duplicated into the project's separate, pre-existing
  `audit.py` (the send/session-action log) — that would be a second,
  parallel audit trail for the same events, which this project's own
  standing discipline explicitly avoids.
- **Knowledge capture**: already real via `session_knowledge.py` (§13)
  — a post-incident/release ADR or gotcha note is exactly a
  `terminal_knowledge_checkpoint` call against the relevant session/
  project, no new store needed. Not specifically wired to auto-fire
  after an incident/release closes in this pass (a human/PM decides
  when a checkpoint is worth recording) — disclosed as unbuilt
  automation, not a missing capability.
- **Resource/cost awareness**: already real via `ControllerService.
  list_nodes()` (§12's own CPU/RAM/capacity_status collection) +
  `QueueService.pending_counts()` (§3's own badge data source) — both
  now surfaced together in `terminal_pm_summary`'s own `node_capacity`
  field below, rather than needing any new metrics collection.
- **PM summary + backlog hygiene** (`pm_summary.py`, new) — the one
  genuinely new piece: `generate_summary` (board counts, total pending,
  active incidents, optional real per-node capacity via an optional
  `controller` param); `detect_stale_backlog_tasks` (Backlog/Queued
  tasks older than a configurable threshold, real `created_at` ages,
  never RUNNING/done tasks); `detect_duplicate_tasks` (still-open tasks
  sharing byte-for-byte identical prompt text — never a fuzzy/semantic
  guess). **"Human controls destructive close"** (task's own explicit
  words): `close_task_with_confirmation` is the ONLY action that
  changes anything, and refuses outright (`CONFIRMATION_REQUIRED`)
  unless the caller explicitly passes `confirmed=true` — there is no
  automatic close anywhere in this module. Deliberately NOT wired to a
  daily/weekly scheduler in this pass (disclosed scope cut, same "no
  auto-loop yet" posture as PM/Planner's own manual sweeps) — a caller
  gets the real, current data on every explicit call, at whatever
  cadence they choose.
- MCP tools: `terminal_pm_summary`, `terminal_pm_detect_stale_backlog`,
  `terminal_pm_detect_duplicate_tasks`, `terminal_pm_close_task_with_
  confirmation`.
- Tests: `tests/test_pm_summary.py` (15, including a real controller-
  failure-is-best-effort case and real `created_at` backdating via
  direct row mutation to prove staleness detection against actual
  elapsed time, not a mock), `tests/test_pm_summary_mcp_tools.py` (4,
  the real MCP tool surface, including the confirm-then-succeed close
  flow). Updated MCP tool-count assertions (122 -> 126). Full suite
  green.

**Phase E — Security/control plane:**
- Least privilege per task/role/environment, secret redaction (this
  project's own existing `redaction.py` reused, not reinvented),
  production credentials never granted to an ordinary coding worker's
  session by default.
- Human override controls, all explicit UI actions, several already
  real (`terminal_queue_pause`/`resume`, PM/Planner OFF/SUGGEST/AUTO
  from §20.2/20.3): Pause Auto, Run Next, Reassign, Block, Approve
  Merge, Approve Deploy, and a new **Emergency Stop** (immediately sets
  every relevant mode to OFF/paused fleet-wide — a genuinely new,
  simple, high-priority action).
- Agent failure policy: repeated identical failure / no real progress
  trips to `NEEDS_HUMAN`/reassignment rather than an unbounded retry
  loop — same posture as the existing `coordinator_attempts` cap (§8),
  extended to cover this specific "stuck in a loop" shape explicitly.

#### Phase E implementation note (2026-09-07, VERIFIED — repeated-failure
gate + Emergency Stop; least-privilege documented, no new code needed)

- **Agent failure policy** (`coordinator.py`, `CoordinatorGate`): a new
  `repeated_failure_threshold` param (default
  `DEFAULT_REPEATED_FAILURE_THRESHOLD = 3`) — deliberately **ON by
  default**, unlike every other Phase A/E opt-in gate, because it is a
  real EXTENSION of the already-always-on `max_review_attempts` cap
  (default 5), not a new behavior class: it catches the *specific*
  "exact same reason, zero progress" shape strictly sooner than the raw
  attempt budget alone would. New check (`review()`, placed right after
  the existing risk-level gate and before the raw attempt-budget check):
  once `task.coordinator_attempts >= repeated_failure_threshold`, reads
  this task's own real `COORDINATOR_DECISION` event history (via the
  existing, real `QueueStore.list_events` — no second history
  mechanism) through a new `_recent_coordinator_reasons` helper; if the
  most recent `repeated_failure_threshold` reasons are byte-for-byte
  identical, returns `NEEDS_HUMAN` with `evidence.repeated_reason`/
  `repeat_count` rather than letting the loop continue. `None` disables
  the check entirely (falls back to the raw attempt cap only).
- **Emergency Stop** (`pm_summary.py`): `emergency_stop_all_lanes(queue,
  *, reason, confirmed=False)` — pauses **every** lane at once via the
  existing, real, already-idempotent `QueueService.pause`/`QueueStore.
  pause_lane` (§10) — never a new stop/kill mechanism, and a QUEUE
  DISPATCH stop only (never touches a session's own tmux/ConPTY
  process — this project's own standing "never disrupt a real attended
  session" discipline). Same "human controls destructive action,
  refuses without `confirmed=true`" posture as Phase D's
  `close_task_with_confirmation`. A lane already paused (for any
  reason) is left untouched. `emergency_resume_all_lanes(queue)` is the
  undo — it resumes **only** lanes whose `paused_reason` was actually
  set by `emergency_stop_all_lanes` (prefixed `EMERGENCY STOP:`), so a
  lane a human had already deliberately paused for an unrelated reason
  *before* the emergency stop is left exactly as they left it, never
  guessed at. MCP tools: `terminal_emergency_stop`, `terminal_
  emergency_resume`.
- **Least privilege / secret redaction / no prod creds by default**:
  documented as **already true by construction**, no new code —
  disclosed honestly rather than built redundantly. This project has no
  credential-granting mechanism anywhere in its MCP surface at all (no
  tool exists that ever hands a session a secret/API key/deploy
  credential); the existing `redaction.py` (`redact_text`/
  `redact_ansi_safe`, pre-existing, unmodified) already strips
  credential-shaped substrings from anything this project surfaces
  (logs, captured panes); and Phase C's own release-approval gate
  (`release_service.py`) already requires an explicit human `approved_
  by` before any `prod` deploy can even start — an ordinary coding
  worker session is never handed anything beyond what it already had
  (tmux send/read + queue tools), so "not granted prod secrets by
  default" was never something to newly build.
- **Tests**: `tests/test_coordinator.py` (+5: default-on repeated-
  failure detection, differing reasons don't trip it, configurable
  threshold, disabling via `None`, and the pre-existing max-attempts
  test isolated from this new always-on check via an explicit `None`
  override so the two independently-real checks don't both fire on the
  same fixture), `tests/test_pm_summary.py` (+6: confirmation/reason
  gates, real multi-lane pause, already-paused-lane-left-untouched,
  resume-only-what-was-stopped, no-op resume), `tests/test_pm_summary_
  mcp_tools.py` (+1, the real MCP stop→status→resume→status round
  trip). **Live disposable E2E** (`tests/test_queue_engine_smoke.py`,
  `pytest -m queue_smoke`, +2, both against real disposable tmux
  sessions — never `window`/`window2`/`wtest`):
  `test_repeated_identical_coordinator_failure_forces_needs_human_for_
  real` — a real dirty git repo (an uncommitted change that is never
  fixed) drives 3 real, independent `engine.tick()` PRECHECK reviews to
  the identical real `git_repo_evidence` "uncommitted changes" reason,
  and the 4th tick's real `COORDINATOR_NEEDS_HUMAN` correctly fires
  BEFORE the raw `max_review_attempts=5` budget would have (confirmed
  via `evidence.repeat_count == 3` and the task's own persisted
  `coordinator_reason`); `test_emergency_stop_pauses_a_real_in_flight_
  lane_and_resume_lets_it_finish` — a real disposable worker session's
  task is driven to `COORDINATOR_READY`, real-paused mid-flight by
  `emergency_stop_all_lanes`, proven inert under a real `engine.tick()`
  while stopped, then `emergency_resume_all_lanes` lets the same real
  task run to a real `COMPLETED` with real verification evidence. Both
  green. Updated MCP tool-count assertions (126 -> 128). Full suite
  green (see Backlog item 25).

### 20.7 New API/tools (PLANNED names, for future implementation —
none of these exist yet)

`task_create` (canonical creation, `assigned_session_id` nullable — a
null value means "create UNASSIGNED, persist immediately, return
`task_id`", never a direct send to any terminal), `task_plan`/
`task_split`/`approve_plan`, `task_children`, `task_assign`/`task_
reassign` (already real as `terminal_task_reassign`, extend rather than
duplicate), `task_run_next` (Insert Next — queues ahead of other QUEUED
work without interrupting a RUNNING task), `worktree_status`,
`integration_handoff`/`integration_status` (mostly already real via
`terminal_integration_*`, §9 — extend, don't duplicate), `pm_status`/
`pm_explain` (the routing rationale for one task), `worker_
capabilities` (list/query the Capability Profile store). Every one of
these should be exposed as a real MCP tool once built, and this file's
own §15 inventory updated in the same commit that adds it — never
documented as available before it exists.

### 20.8 Acceptance (for whenever this is actually built — nothing
below has happened yet)

20+ backlog tasks; 3+ worker sessions with distinct Capability
Profiles; a task large enough to trigger a real Planner split (3-5
children); 2 children genuinely running in parallel on different
worktrees; 1 overlapping-change pair correctly serialized instead of
parallelized; 2 branches handed off to a real Integration Agent session
(mechanical conflict auto-resolved, semantic conflict correctly
REWORK_REQUIRED back to the right owner); a parent task that only
reaches DONE after its Handoff reaches INTEGRATED; PM in SUGGEST then
AUTO mode correctly routing a Windows/WPF task to a Windows session and
never to Linux; a pinned-but-ineligible session correctly BLOCKED with
a clear reason, never silently rerouted; a controller restart and a
node offline/reconnect cycle with 0 duplicate task rows, 0 lost tasks,
0 cross-task worktree contamination, 0 coding directly on shared main.
Every phase above gets its OWN such pass before its own status moves
off PLANNED — this file must never claim VERIFIED for something that
hasn't had one.

---

## Feature Details

*(Full per-feature entries — goal/status/scope/UI/API/config/schema/
tests/limitations/dependencies/follow-up/trace — for every checkpoint
listed in §19, plus the living-requirements convention itself. This
section is the backfill the user asked for; §1-§19 above are the fast-
scan/audit view over the SAME facts.)*

### Supervisor Queue v2 — Phase 1 (persistence + state machine + CRUD)

- **Goal / user value:** a durable, per-session task queue that survives
  a controller restart, so ChatGPT/an operator can queue work for a
  session without babysitting delivery.
- **Status:** VERIFIED (storage foundation for every entry below).
- **Scope / flow:** `session` IS the lane; tasks are appended/replaced,
  claimed one-at-a-time per lane, transitioned through an explicit state
  machine, never silently lost across a process restart (real SQLite,
  not in-memory).
- **UI route/screen:** none at this phase (added later).
- **API/tool/command:** `terminal_queue_set/append/status/list_all/
  pause/resume/retry/skip/cancel/reorder/clear/events`.
- **Config/permission:** none dedicated (session-name whitelist reused).
- **Data/schema/migration:** `queue_store.py`, migration v1
  (`queue_tasks`, `queue_lanes`, `queue_events`).
- **Acceptance/tests/evidence:** `tests/test_queue_store.py`.
- **Known limitations:** no Coordinator gate yet, no automatic dispatch.
- **Dependencies:** none (foundational).
- **Follow-up/backlog:** see every entry below.
- **Trace:** `47ff72e`.

### Supervisor Queue v2 — Phase 2 (Coordinator Agent gate + dispatch engine)

- **Goal / user value:** a task is never blindly sent to a session — a
  deterministic gate reviews readiness first, closing the exact class of
  bug behind the real window/window2 transcript-collision P0.
- **Status:** VERIFIED (extended by "Coordinator production-readiness
  pass" below — this phase's own disclosed gaps are now closed).
- **Scope / flow:** `QUEUED -> PRECHECK (atomic claim) ->
  CoordinatorGate.review() -> READY/BLOCKED/NEEDS_REWORK/NEEDS_HUMAN ->
  DISPATCHING -> RUNNING -> VERIFYING -> COMPLETED`, with
  `DISPATCH_UNCERTAIN`/`WAITING_SESSION` branches.
- **UI route/screen:** none at this phase.
- **API/tool/command:** `terminal_queue_run_once`, `terminal_queue_verify`,
  `terminal_queue_set_auto_dispatch` (flag existed, no loop yet).
- **Config/permission:** none dedicated at this phase.
- **Data/schema/migration:** `coordinator.py`, `queue_engine.py`;
  migration v2.
- **Acceptance/tests/evidence:** `tests/test_coordinator.py`,
  `tests/test_queue_engine.py`, `tests/test_queue_engine_smoke.py`.
- **Known limitations (closed since):** no automatic background loop; no
  test/build/smoke, git-diverged, or session-state checks.
- **Dependencies:** Phase 1.
- **Follow-up/backlog:** closed by "Coordinator production-readiness
  pass" below.
- **Trace:** `04f14d0`.

### P0: persist-before-dispatch

- **Goal / user value:** every ChatGPT/UI/API-originated prompt becomes
  a durable task record BEFORE it's ever sent to a session.
- **Status:** VERIFIED.
- **Scope / flow:** `enqueue()` persists first, dispatch only follows
  once the Coordinator says READY and the session is idle/eligible;
  `DELIVERY_UNKNOWN` moves to `DISPATCH_UNCERTAIN`, reconciled via
  idempotency key + observed activity before any retry.
- **UI route/screen:** none at this phase.
- **API/tool/command:** `terminal_enqueue_task`, `terminal_task_status`,
  `terminal_queue_metrics`.
- **Config/permission:** none dedicated.
- **Data/schema/migration:** migration v3 (`uncertain_or_waiting_since`).
- **Acceptance/tests/evidence:** `tests/test_queue_persist_before_dispatch.py`
  (23), `tests/test_queue_persist_mcp_tools.py`, `tests/test_queue_conflict_warning.py`.
- **Known limitations:** none beyond the general auto-dispatch gap
  (closed later).
- **Dependencies:** Phase 1/2.
- **Follow-up/backlog:** none outstanding.
- **Trace:** `ae85232`.

### 3-role model: Coding A/B + Integration Agent (merge/test pipeline)

- **Goal / user value:** two coding sessions run in parallel without
  waiting on merge/test or self-merging into `main`.
- **Status:** VERIFIED (disposable E2E only — never enabled on a real
  project's own coding sessions).
- **Scope / flow:** see §9 above for the full mechanism.
- **UI route/screen:** none at this phase (added later — Supervisor/
  Coordinator panel).
- **API/tool/command:** `terminal_integration_configure/status/
  list_handoffs/run_once/pause/resume/retry_handoff/force_regression/
  promote/events`.
- **Config/permission:** per-project, explicit `terminal_integration_configure`
  call required.
- **Data/schema/migration:** `integration_store.py`,
  `integration_reviewer.py`, `integration_engine.py`.
- **Acceptance/tests/evidence:** `tests/test_integration_store.py`,
  `tests/test_integration_reviewer.py`, `tests/test_integration_engine.py`,
  `tests/test_integration_hook.py`, `tests/test_integration_mcp_tools.py`,
  `tests/test_three_role_smoke.py`, `tests/test_integration_loop.py`.
- **Known limitations:** no real tmux session of its own (pure backend
  engine).
- **Dependencies:** Phase 1/2, persist-before-dispatch.
- **Follow-up/backlog:** no dashboard view until "Dashboard Supervisor/
  Coordinator panel" below.
- **Trace:** `957f15a`, event-driven WAIT/wake loop below.

#### Event-driven WAIT/wake background loop (2026-09-07, VERIFIED)

- **Goal:** close the one real gap the entry above disclosed — no
  automatic background loop existed, so `terminal_integration_run_once`
  had to be called explicitly for every single pipeline step.
- **Design (see `integration_loop.py`'s own module docstring for the
  full reasoning):** `claim_next_handoff`'s own query is already the
  durable, restart-safe "detection" of new work (no new wake-flag
  bookkeeping needed) — the real gap was purely the missing driver.
  `IntegrationLoop` mirrors `queue_loop.py`'s `QueueLoop` shape
  (daemon thread, `start`/`stop`/`status`/`run_one_cycle`, one project's
  exception never stops another's cycle) with ONE addition: a real
  in-process `threading.Event`, set by `IntegrationStore.publish_handoff`
  itself via a newly-injected `on_handoff_published` hook (same
  "injected callback, not a new pub/sub system" convention as
  `QueueEngine.on_completed`) — a fresh handoff wakes the loop almost
  immediately instead of waiting out a poll interval. A bounded
  fallback poll (`config.integration_loop.fallback_poll_seconds`,
  default 5.0s) remains as the cross-process/restart safety net (a
  handoff published by a different process, or one that already existed
  before the loop started, is still picked up, bounded). A cycle that
  made real progress on any project skips its own wait entirely before
  the next cycle, so a multi-step pipeline (CLAIMED -> MERGED ->
  INTEGRATED, each its own `tick()`) advances back-to-back rather than
  paying the fallback interval between every single step — only a
  cycle where every project reports `WAITING_FOR_HANDOFF`/
  `ENGINE_ERROR` goes back to waiting.
- **Idempotent/race-safe:** reuses the EXISTING, already-atomic
  `BEGIN IMMEDIATE claim_next_handoff` — the loop adds no new claiming
  logic of its own, so two loop instances (simulating two processes)
  racing the identical handoff never double-claim/double-merge (real
  git repo, live-proven).
- **Restart-safe:** the wake `Event` is purely in-memory/best-effort —
  losing it on a crash loses nothing real, since every handoff's own
  `READY_FOR_INTEGRATION`/stale-claimed row is still sitting in the
  database exactly where the next loop instance's `claim_next_handoff`
  (after `reconcile_stale_handoff_claims`) will find it, bounded by the
  fallback poll at worst.
- **Two-gate safety (same posture as `QueueLoop`):** (1)
  `config.integration_loop.enabled` (default False) — a global kill
  switch nothing starts without; (2) reuses the ALREADY-real per-project
  `configure`/`paused` gate rather than adding a new per-project opt-in
  column — a project must be explicitly configured before any Handoff
  can even exist for it, and a paused project's cycle is skipped
  entirely (never even ticked).
- **Fix/what's built:** `integration_loop.py` (new, `IntegrationLoop`),
  `integration_store.py` (`on_handoff_published` hook param + call site
  in `publish_handoff`), `config.py` (`IntegrationLoopConfig`,
  `integration_loop.enabled`/`fallback_poll_seconds`), `mcp_app.py`
  (constructs `integration.loop`, wires the wake hook — same "queue.loop"
  precedent), `server_http.py` (starts/stops it based on config, mirrors
  the `queue.enabled` gate), new MCP tools `terminal_integration_loop_
  status`/`terminal_integration_loop_run_once`.
- **Deployment status:** code + tests VERIFIED; `config.integration_loop.
  enabled` is `False` in the real production `config.yaml` (this
  checkpoint does not turn it on) — no real project has an Integration
  pipeline configured in production today either, so enabling it would
  currently be inert. Turning it on for a real project is a future,
  separate, disclosed decision, not silently bundled into this fix.
- **Tests:** `tests/test_integration_loop.py` (9, real git repos, real
  background threads — `wake()` reaction-latency proof against a
  deliberately long 30s fallback poll, a full clean-handoff-to-
  INTEGRATED lifecycle with ZERO manual ticks, a real-conflict-to-
  REWORK_REQUIRED lifecycle with ZERO manual ticks, TWO concurrent real
  loop instances racing the same handoff — exactly one merge commit,
  never duplicated, and a fresh loop instance recovering a handoff
  abandoned by a dead one — restart safety), 2 new in `tests/
  test_integration_mcp_tools.py` (the MCP tool surface). Also found and
  fixed, while re-verifying this pass: `tests/test_three_role_smoke.py`
  had 2 tests that predated the "Living-requirements convention" doc
  gate (`edd316f`, added ~12h after this file was originally written)
  and never got the `docs_exempt` artifact real disposable-content
  handoffs elsewhere in this codebase already carry — a real,
  pre-existing latent test bug, unrelated to this checkpoint's own code,
  fixed alongside it (test-only change, same fix already applied
  everywhere else). Full suite green (1 known pre-existing failure
  unrelated to this entry — see item 14's own writeup, now itself fixed
  this same session).

### Task Migration / Load Balancing

- **Goal / user value:** move a session's still-QUEUED work to a
  healthier peer without losing the task, its history, or its position.
- **Status:** VERIFIED.
- **Scope / flow:** see §10 above.
- **UI route/screen:** covered by Task Manager/Global Inbox — no
  dedicated rebalance UI yet.
- **API/tool/command:** `terminal_task_set_project/reassign/
  assignment_history/rebalance_plan/rebalance`.
- **Config/permission:** none dedicated.
- **Data/schema/migration:** migration v4 (`original_owner`,
  `migration_history`, `at_risk`, `project`, `last_rebalance_at`);
  `task_migration.py`.
- **Acceptance/tests/evidence:** `tests/test_task_migration_store.py`
  (17, real thread race), `tests/test_task_migration_planner.py` (15),
  `tests/test_task_migration_mcp_tools.py` (5, real tmux/MCP).
- **Known limitations:** no dedicated Move-Task UI (API/tool-only).
- **Dependencies:** Phase 1/2.
- **Follow-up/backlog:** Move-Task drag/drop UI (PLANNED).
- **Trace:** `c95d06e`.

### Rename Session

- **Goal / user value:** rename a session's display name without losing
  its process, tmux identity, bindings, grants, queue tasks, integration
  handoffs, supervisor watches, or history.
- **Status:** VERIFIED.
- **Scope / flow:** see §2's Rename bullet above.
- **UI route/screen:** dashboard term-bar "✏ Đổi tên session" + modal.
- **API/tool/command:** `terminal_rename_session`;
  `POST /dashboard/api/session/rename`.
- **Config/permission:** same `session_lifecycle.enabled` gate as
  create/kill; new name must pass `allowed_session_patterns`, can't
  be/become protected.
- **Data/schema/migration:** no new tables — in-place re-keys across
  bindings/grants/session_registry/queue_store/integration_store/
  supervisor watches.
- **Acceptance/tests/evidence:** `tests/test_rename_session.py` (12),
  3 `WindowsSessionBackend` unit tests, 5 tests in `test_kill_reopen.py`.
- **Known limitations:** the old-name alias redirect does NOT survive a
  real controller restart (disclosed; every store that OWNS data still
  persists correctly — only the stale-caller convenience redirect is
  lost).
- **Dependencies:** Phase 1/2, Task Migration, Integration Agent.
- **Follow-up/backlog:** none outstanding.
- **Trace:** `0944c33`.

### Dashboard Task Manager UI

- **Goal / user value:** click a session and immediately see what it's
  doing, how much is queued, what it's waiting on, what just finished/
  failed.
- **Status:** VERIFIED.
- **Scope / flow:** `session_task_board(session)` groups real
  `QueueTask` rows (Running/Queued/Waiting-Dependency/Blocked-Rework/
  Recent) + the Coordinator's own gate decision for the head-of-line
  task.
- **UI route/screen:** term-bar "📋 Tasks" -> per-session modal.
- **API/tool/command:** `terminal_session_tasks`, `terminal_fleet_task_summary`;
  `GET /dashboard/api/session/tasks`, `GET /dashboard/api/fleet-task-summary`,
  `POST /dashboard/api/session/queue/{pause,resume,enqueue,reorder}`,
  `POST /dashboard/api/task/{retry,cancel}`.
- **Config/permission:** read gated on `_read_authorized`; actions on
  `_mutation_guard`.
- **Data/schema/migration:** none new — pure aggregation.
- **Acceptance/tests/evidence:** `tests/test_task_manager_ui.py` (15);
  real Playwright desktop+mobile screenshots + real interactive
  Pause/Resume/Enqueue/Retry against a live local server.
- **Known limitations:** Cancel only offered in the UI for a QUEUED
  task, never RUNNING (deliberate; API/ChatGPT can still cancel RUNNING
  via `terminal_queue_cancel`, an audited transition).
- **Dependencies:** Phase 1/2, persist-before-dispatch, Rename Session.
- **Follow-up/backlog:** priority-edit/drag-reorder UI (PLANNED).
- **Trace:** `20f6ff0`, `ee479f1` (final disposable E2E incl.
  rename-mid-queue + migrate + simulated restart).

### Coordinator production-readiness pass + real AUTO-DISPATCH background loop

- **Goal / user value:** close the two biggest gaps disclosed at Phase
  2's own ship time.
- **Status:** IMPLEMENTED_NOT_LIVE_VERIFIED (unit + a real background-
  thread full-lifecycle proof exist; NOT yet exercised against a real
  remote node — see Backlog).
- **Scope / flow:** see §8/§7 above for the full mechanism.
- **UI route/screen:** loop status surfaced in the Supervisor/
  Coordinator panel.
- **API/tool/command:** `terminal_queue_loop_status`,
  `terminal_queue_loop_run_once`.
- **Config/permission:** new `QueueConfig` (`config.yaml`'s `queue:` —
  `enabled` default False, `poll_interval_seconds` default 3.0, minimum
  0.5).
- **Data/schema/migration:** no new tables; `terminal_mcp/queue_loop.py`
  (new file).
- **Acceptance/tests/evidence:** `tests/test_coordinator.py` (18 new,
  incl. real git repos with a bare remote + two diverging clones, real
  subprocess smoke-test commands), `tests/test_queue_loop.py` (6, incl.
  a REAL background thread driving a full autonomous lifecycle),
  `tests/test_queue_config.py`, 2 new tests in `test_queue_mcp_tools.py`.
- **Known limitations:** `config.queue.enabled` stays False in this
  project's own production `config.yaml` — not enabled against any real
  deployment yet.
- **Dependencies:** Phase 1/2, persist-before-dispatch.
- **Follow-up/backlog:** enable only after the disposable E2E + real
  dell-5530 `RemoteNodeClient` smoke test (this task batch's own next
  step).
- **Trace:** `fa5e661`.

### Dashboard Supervisor/Coordinator panel + Global Task Inbox

- **Goal / user value:** one place answers "why isn't my task moving" —
  auto-dispatch loop state, queue depth, blocked/rework, Integration
  lanes, recent events, plus a fleet-wide task inbox.
- **Status:** VERIFIED.
- **Scope / flow:** see §3 above.
- **UI route/screen:** header menu "🧭 Supervisor / Coordinator" +
  "📥 Task Inbox".
- **API/tool/command:** `terminal_queue_global_inbox`,
  `terminal_queue_recent_events`, `terminal_integration_fleet_overview`;
  4 new `GET /dashboard/api/queue/*` and `/dashboard/api/integration/*`
  routes.
- **Config/permission:** `_read_guard` only (same posture as the
  existing `/dashboard/api/sessions` listing).
- **Data/schema/migration:** `IntegrationStore.list_pipelines()` (new
  read method); no new tables.
- **Acceptance/tests/evidence:** `tests/test_supervisor_coordinator_panel.py`
  (5), `tests/test_queue_service_fleet_views.py` (6), 2 new tests in
  `test_integration_store.py`; real Playwright screenshots + a real
  interactive Retry-from-inbox click proving the fleet-wide action-
  routing path.
- **Known limitations:** none beyond the general "never enabled against
  window/window2" constraint every Queue/Coordinator feature shares.
- **Dependencies:** every entry above.
- **Follow-up/backlog:** none outstanding for this entry specifically.
- **Trace:** `d345524`.

### Direct-send verification: continued-polling ack evidence (P0 fix)

- **Goal / user value:** a ChatGPT/dashboard-originated prompt into a
  Windows Claude session must not need multiple manual re-sends — the
  original P0 report ("phải bấm/gửi vài lần mới vào").
- **Status:** VERIFIED locally (real fixture-reproduced regression +
  full default suite green); **NOT yet deployed** to the remote
  dell-5530 node-agent that window/window2/wtest actually run on — see
  Backlog item 7.
- **Root cause (live-reproduced, real evidence):** a real disposable
  `claude-diag-onb-1` Windows Claude session on dell-5530, already past
  onboarding, was sent 12 sequential real prompts through the actual
  production `ControllerService` → `RemoteNodeClient` path. The FIRST
  send after a quiet session confirmed correctly; **every subsequent
  send failed with `DELIVERY_UNKNOWN`** (11/12 in the cleanest run, 9/9
  in an earlier one) even though Claude's own responses landed correctly
  every time (confirmed via `terminal_tail`). Root cause, isolated via
  fine-grained real-time polling instrumentation against the live
  session: `_poll_for_submission` (`core.py`) returns as soon as it sees
  the FIRST pane change after Enter and hands that ONE snapshot to
  `ClaudeAdapter.submit_ack_evidence`. A real Claude Code Ink redraw is
  NOT atomic — the footer can switch to its "esc to interrupt" busy
  indicator before the just-typed prompt's own echo has rendered into
  scrollback. A single-shot check landing exactly in that transitional
  frame sees "busy, no echo yet" and reports `DELIVERY_UNKNOWN` — a pure
  false negative; the send was already correctly accepted moments
  later. This exactly explains the report: the caller sees
  `SUBMIT_UNCONFIRMED`, assumes it may not have gone through, and
  resends — risking a real duplicate on top of the original that HAD
  landed.
- **Fix:** `TerminalService._poll_for_ack_evidence` (new method,
  `core.py`) — instead of checking `submit_ack_evidence` once against
  `_poll_for_submission`'s first snapshot, it keeps polling (bounded by
  the exact SAME `verify_timeout` budget already in place — 0.6s or
  3.0s for Claude/Codex — never a longer worst case than before) until
  the adapter's own ack evidence actually passes or the deadline is
  reached. A send that genuinely never gets accepted still correctly
  times out to `DELIVERY_UNKNOWN` — this only removes the false negative
  for a send that was already on its way to being accepted. Also fixed:
  a self-contradictory `submit_reason: "confirmed"` string that used to
  accompany a `DELIVERY_UNKNOWN` result (leftover from
  `_poll_for_submission`'s own "the pane changed" reason, misleading
  once ack evidence still failed) — now says plainly that the pane
  changed but no ack evidence was found in time.
- **Scope / flow:** applies to every `press_enter=True` send through
  `terminal_send_text`/`terminal_send_bound` — local tmux and remote
  Windows/ConPTY sessions alike (the polling loop itself is backend-
  agnostic; only the Claude/Codex adapters' own busy-window echo
  requirement is what made this reachable in practice for a fast-
  responding Claude session).
- **UI route/screen:** none (backend-only fix); surfaces as fewer
  `SUBMIT_UNCONFIRMED` results in the dashboard's own send-input flow.
- **API/tool/command:** `terminal_send_text`, `terminal_send_bound` (no
  signature change — same result shape, same `delivery_state`/
  `submit_status` vocabulary).
- **Config/permission:** none — no new config, no behavior gated behind
  a flag (this is a correctness fix to existing, always-on verification).
- **Data/schema/migration:** none.
- **Acceptance/tests/evidence:** `tests/fixtures/claude_composer.py`
  (new — a real raw-tty program modeling the exact two-phase-redraw race
  found live) + 4 new tests in `tests/test_send_reliability.py`,
  including `test_claude_race_repeated_sends_all_confirm_no_false_negatives`
  (the direct regression proof: 5 sends in a row, all previously-
  reproducible-as-false-negative, all now correctly `SUBMIT_CONFIRMED`).
  Full default suite re-run clean after the fix: 1649 passed, 1
  pre-existing unrelated flake, 14 deselected. Live evidence: real
  before/after timing captured against dell-5530 (documented above) —
  the FIX ITSELF has only been verified via the local fixture tests so
  far, since it isn't deployed to dell-5530's own node-agent yet (see
  Backlog item 7) — the local fixture reproduces the exact captured
  live race faithfully, but a final live re-run against dell-5530 after
  deployment is the closing piece of evidence still needed.
- **Known limitations:** the fix does not change `SEND_TEXT_ENTER_SETTLE_SECONDS`
  (still a fixed 80ms pre-Enter settle, not adaptive) — this investigation
  found and fixed a DIFFERENT, confirmed bug in the post-Enter
  verification instead; the settle window remains a disclosed, un-
  exercised suspect for some other failure mode, not ruled out, just not
  what this specific reproducible bug turned out to be. The full 13-item
  send-reliability redesign from the original P0 ask (BUSY/QUEUED
  semantics, a redesigned result-state contract, automatic
  DELIVERY_UNKNOWN reconciliation, latency percentile metrics fields,
  the full 12-scenario test matrix) was NOT built — this fix is scoped
  to the one confirmed, live-reproduced root cause.
- **Dependencies:** `adapters.py`'s existing `ClaudeAdapter`/
  `CodexAdapter` evidence model (reused, not replaced).
- **Follow-up/backlog:** deploy to dell-5530's node-agent + live re-
  verify (Backlog item 7, the single most important remaining step);
  consider the same continued-polling treatment for
  `stuck_composer_evidence`'s own recovery-path evidence check if a
  similar transitional-frame gap is ever found there.

### TARGET_AWAITING_APPROVAL false positive on ordinary composer text

- **Goal / user value:** `terminal_send_text` must only refuse a send as
  `TARGET_AWAITING_APPROVAL` when the target is genuinely showing a
  real approval/menu/confirmation prompt — never for an ordinary,
  idle composer whose own typed text happens to contain an
  everyday word.
- **Status:** VERIFIED (real disposable-session E2E + unit tests, full
  suite green).
- **Root cause (live, real report):** a real, attended session
  (`window2`) had `current_command=claude`, `effective_input=true`,
  `pane_in_mode=false`, and a completely ordinary composer showing
  `> Làm Role/Permission step 2 custom role web đi` (this project's own
  subject matter — a permissions/roles feature) plus Claude Code's own
  normal context-usage status line (`new task? /clear to save 891k
  tokens`) — yet every `terminal_send_text` call was refused with
  `TARGET_AWAITING_APPROVAL`. `adapters.py`'s `_WAITING_PATTERNS` (the
  pre-send `identify_target_state` check `_send_text_and_verify_locked`
  consults before committing to a `press_enter=True` send — §5) used to
  include bare, un-anchored `\bapprove\b`/`\bpermission\b` word-boundary
  patterns. The composer's own perfectly ordinary use of the word
  "Permission" matched, classifying an idle composer as `TARGET_
  WAITING`. Neither bare word was ever exercised by a real regression
  fixture: `tests/fixtures/waiting_prompt.py`'s real y/n dialog matches
  via `\[y/n\]`; `tests/fixtures/menu_prompt.py`'s real AskUserQuestion-
  style numbered-menu widget (the actual shape Claude Code's own
  permission-request UI renders as) matches via the menu-chrome strings
  (`enter to select`/`tab/arrow keys to navigate`/`esc to cancel`) —
  both of Claude Code's real, observed approval shapes were already
  fully covered without the two removed words.
- **Fix:** removed the bare `\bapprove\b`/`\bpermission\b` patterns from
  `_WAITING_PATTERNS` (`adapters.py`) entirely, rather than narrowing
  them to a guessed replacement phrase — this project's own standing
  rule is to never invent unverified CLI-output phrasing (matches
  `config.py`'s `resume_capable_agent_types` docstring posture, and
  `coordinator.py`'s own disclosed-heuristic philosophy), and there is
  no real, observed Claude/Codex dialog on record using the bare word
  "approve" or "permission" outside the menu-chrome shape already
  caught by the remaining patterns. `terminal_input_context` was
  already unaffected either way (it never consulted `identify_target_
  state`/`_WAITING_PATTERNS` — its `effective_input` is derived
  independently from permissions/grants/`pane_current_command`/
  `pane_in_mode`), confirmed still correct after the fix.
- **Scope / flow:** the pre-send `TARGET_WAITING` check inside
  `_send_text_and_verify_locked`, `press_enter=True` only — identical
  scope to the original TARGET_AWAITING_APPROVAL feature (§5's own
  "URGENT bugfix" note in `core.py`), no other behavior touched.
- **UI route/screen:** none (backend-only fix); surfaces as a session
  that mentions ordinary words like "permission"/"approve" in its own
  conversation no longer being spuriously unsendable.
- **API/tool/command:** `terminal_send_text`/`terminal_send_bound` (no
  signature change — same result shape).
- **Config/permission:** none.
- **Data/schema/migration:** none.
- **Acceptance/tests/evidence:** `tests/test_adapters.py` — 2 new unit
  tests (`test_claude_adapter_normal_composer_mentioning_permission_is_
  not_waiting` reproducing the exact reported pane shape and asserting
  `TARGET_UNKNOWN`/sendable; `test_claude_adapter_still_detects_a_real_
  permission_dialog_via_menu_chrome` proving real detection is
  unweakened — a genuine permission-request menu, including the literal
  word "permission" in its own descriptive text, is still correctly
  classified `TARGET_WAITING` via the surrounding menu chrome).
  `tests/fixtures/normal_composer_permission_word.py` (new) + 1 new
  real disposable-session E2E test in `tests/test_send_reliability.py`
  (`test_claude_send_allowed_for_normal_composer_mentioning_permission`)
  — a real `exec -a claude` pty target showing the exact reported
  composer/status-line shape, confirming `terminal_input_context`
  reports `effective_input: true`/`pane_in_mode: false` (as the real
  report observed) AND `terminal_send_text` now genuinely delivers the
  text (`RECEIVED=...` echoed back by the real target process, not
  swallowed). The two pre-existing real regression fixtures (y/n
  dialog, multi-choice menu) re-verified still correctly refused. Full
  suite green.
- **Known limitations:** `_WORKING_PATTERNS` (`\bworking\b`/
  `\bthinking\b`) has a similar theoretical false-positive shape (an
  ordinary composer line that happens to contain those words) — not
  reported, not touched by this fix, tracked as a disclosed, un-
  reproduced suspect only (Backlog item 16), scoped out to avoid
  widening this fix beyond the confirmed, reported root cause.
- **Dependencies:** `adapters.py`'s existing `ClaudeAdapter`/
  `CodexAdapter`/menu-chrome pattern set (reused, not replaced).
- **Follow-up/backlog:** none required — this closes the reported
  issue; Backlog item 16 tracks the disclosed `_WORKING_PATTERNS`
  suspect for a future pass if it is ever actually reported/reproduced.
- **Trace:** see this file's own commit.

### P0 follow-up: window2 composer still stuck — 2 more real root causes fixed, 1 real anomaly NOT resolved (2026-09-07)

- **Goal / user value:** a real, attended session (`window2`) had typed-
  but-unsubmitted text sitting in its composer; fix the real cause(s),
  not a workaround, verify on a disposable session first, then
  (if safe) submit the live composer without restarting/losing the
  session.
- **Status: PARTIAL.** Two more real, confirmed root causes found and
  fixed (VERIFIED, local — see below for what "deployed" actually means
  here). The live `window2` composer itself is **STILL STUCK** — a
  real, reproducible anomaly specific to that one session that did
  **NOT** reproduce on a disposable session on the exact same real host/
  currently-deployed code, and was **NOT** resolved. This is disclosed
  honestly rather than claimed fixed.
- **Root cause #1 (a real, exact duplicate of the earlier adapters.py
  bug, found live via `terminal_status`):** `status.py`'s own
  `WAIT_PATTERNS` (used by `detect_waiting_input`/`classify_status`,
  a COMPLETELY SEPARATE code path from `adapters.py`'s `_WAITING_
  PATTERNS` — never touched by the earlier TARGET_AWAITING_APPROVAL
  fix) also had bare `\bapprove\b`/`\bpermission\b` word-boundary
  patterns. `terminal_status("dell-5530/window2")` was observed, live,
  reporting `state: "WAITING_INPUT"`, `input_required: true`, `reason:
  "recent prompt matched '\\bpermission\\b' at bottom offset 2"` for a
  completely ordinary, idle composer — the exact same false-positive
  class, independently present in a second location. **Confirmed NOT
  the cause of the stuck composer itself** (this field is purely
  informational — read for `terminal_status`/dashboard/Supervisor/
  Coordinator display, never consulted by `_send_text_and_verify_
  locked`'s own send-gating logic) — fixed anyway since it is a real,
  now-confirmed-live bug in its own right (wrong dashboard/Coordinator-
  visible status for any session whose own text happens to mention
  "permission"/"approve").
  - **Fix:** removed the same two bare-word patterns from `status.py`'s
    `WAIT_PATTERNS`, identical reasoning/precedent as the earlier
    `adapters.py` fix (no speculative replacement pattern — the real
    y/n and "press enter"/"waiting for input" patterns already cover
    every actually-observed real prompt shape).
- **Root cause #2 (a real gap in this project's own send_keys
  reliability, per this task's own explicit requirement):**
  `terminal_send_keys` previously reported `sent: true` for ANY
  successful write — including `["Enter"]`, the single most common
  real use (submitting whatever text is already sitting in the
  composer) — with ZERO acceptance verification, unlike `terminal_
  send_text`'s own already-real, already-verified press_enter path.
  - **Fix:** `core.py`'s `_send_keys_leased`/new `_send_enter_key_
    verified_locked` — a single `["Enter"]` send now reuses the EXACT
    SAME adapter-based ack-evidence verification (`_poll_for_
    submission`/`_poll_for_ack_evidence`) `terminal_send_text`'s own
    `press_enter=True` path already uses, adding real `delivery_state`/
    `submit_status`/`submit_reason` fields (`sent` keeps its exact
    prior meaning — backward compatible). A genuine implementation
    bug was caught and fixed DURING this same pass (see its own test
    failure): passing an empty `sent_text` to `submit_ack_evidence`
    would have silently defeated the exact busy-window echo-matching
    race guard this whole mechanism exists to enforce (an empty string
    is treated as trivially satisfied by `_sent_text_echoed`) — fixed
    by a new `_extract_composer_text` helper that reads the actual
    expected text straight from the composer's own pre-Enter content
    (stripping a leading `"> "` marker), never blindly empty. Every
    OTHER key combination (not exactly `["Enter"]`) is completely
    unaffected — deliberately narrow, not a general raw-key
    verification system.
- **Live disposable-session evidence (real dell-5530 host, currently-
  deployed/unpatched node-agent code, a fresh `test-winkey-diag-1`
  Claude session, cleaned up after):** typed text, then a raw, bare
  `terminal_send_keys(["Enter"])` (the SAME mechanism the report said
  didn't work) **correctly submitted** — Claude began processing
  ("Noodling…") within 0.3s and produced a real reply. This proves the
  underlying Enter-byte mechanism (`windows_backend.py`'s `KEY_BYTES["Enter"]
  = b"\r"`, delivered via `pywinpty`'s own `PtyProcess.write` — not a
  simulated keypress, not focus-dependent) is fundamentally sound on
  this real host, for a normal session.
- **The unresolved anomaly:** the SAME action (a real, careful,
  verified `terminal_send_keys(["Enter"])`, sent exactly once, then a
  SECOND time after 8+ seconds confirmed zero effect from the first)
  against the REAL `window2` session had **NO observable effect at
  all** — the composer's own text stayed byte-for-byte identical
  across two attempts and 13+ cumulative seconds of polling. `reader_
  alive: true`, `reader_restarts: 0` — the session's own output-capture
  path is healthy; this looks specifically like an INPUT-delivery gap
  for this one process, not a dead/hung session. Both send attempts
  were safety-gated (aborted if the composer's own text had changed
  from the last known-good read, so neither attempt could have
  duplicated or corrupted anything) and left `window2`'s real state
  completely unchanged — no harm done, but also no fix. **Not
  explained**: why a mechanism proven to work moments earlier, on the
  same real host and currently-deployed code, against a fresh session,
  does not work against this one, specific, long-running session
  (891k tokens, many hours). Candidate directions for a future pass
  (none tested, all speculative — disclosed as such, never guessed at
  further without real evidence): a `pywinpty`/ConPTY input-pipe
  condition specific to a very long-running child process; something
  about `window2`'s own prior interaction history (e.g. the earlier
  classifier-blocked attempts, though code-review found no write
  occurs on that path) leaving the reader/writer pairing in an
  inconsistent state that only deeper Windows-side instrumentation
  (not available from this remote diagnostic vantage point) could
  actually confirm.
- **Deliberately NOT done:** deploying either fix (or the send_keys
  verification) to `dell-5530`'s own node-agent — this project's own
  established Phase 0 finding (no ConPTY session survives ANY node-
  agent restart method) means doing so would kill `window2`'s actual
  Claude process, destroying the exact unsubmitted composer text this
  whole investigation exists to preserve. This is a real, disclosed
  catch-22 the user needs to decide on, not something to resolve
  unilaterally.
- **Scope / flow:** `status.py`'s `WAIT_PATTERNS` (informational status
  classification only); `core.py`'s `terminal_send_keys(["Enter"])`
  path (adds verification, never changes what bytes are written).
- **API/tool/command:** `terminal_status`/`terminal_input_context`
  (indirectly, via `classify_status`); `terminal_send_keys` (new
  `delivery_state`/`submit_status`/`submit_reason` fields for the
  `["Enter"]` case only).
- **Config/permission:** none.
- **Data/schema/migration:** none.
- **Acceptance/tests/evidence:** `tests/test_status.py` — 2 new (the
  exact reported pane shape no longer WAITING_INPUT; a real y/n dialog
  still correctly detected). `tests/test_send_reliability.py` — 4 new
  for the verified-Enter path (real submission confirmed; the same
  busy-footer-before-echo race survives; a genuine never-submits target
  still correctly times out to `DELIVERY_UNKNOWN`; every other key
  combination unaffected) + 5 new for `_extract_composer_text` itself.
  Full suite green. Live evidence as described above (disposable
  session: mechanism proven sound; `window2` itself: anomaly confirmed
  real and reproducible, not resolved).
- **Known limitations:** the `window2` composer itself remains stuck as
  of this entry — this is an honest, open, un-explained finding, not a
  claimed fix.
- **Dependencies:** `adapters.py`'s existing ack-evidence model (reused
  for the new `_send_enter_key_verified_locked` path, not replaced).
- **Follow-up/backlog:** Backlog item 20 tracks the unresolved `window2`
  input-delivery anomaly and the deployment decision (whether/when to
  accept a `dell-5530` node-agent restart, losing that session's own
  unsubmitted composer text, to pick up every fix accumulated in this
  file's own recent Backlog items — 14, this entry, and the earlier
  TARGET_AWAITING_APPROVAL fix — all still undeployed to that node).

**Re-verification addendum (same day, later P0 pass):** re-ran the exact
same read-only `terminal_status("window2")` check first — confirmed the
composer's own text was byte-for-byte unchanged since the entry above,
and (independent re-confirmation of Root cause #1, since `dell-5530`'s
node-agent is still undeployed) still reports `state: "WAITING_INPUT"`
via the exact same `\bpermission\b` match, proving that specific fix is
correct-but-undeployed rather than wrong. Sent **exactly one** more
`terminal_send_keys(["Enter"])`, gated the same way (aborted-if-changed
composer-marker check first), then polled for a real pane change every
5s for **150s** — more than 11x the prior pass's 13s window, specifically
to rule out "the poll window was just too short for an 891k-token
session's own processing indicator to appear" as an explanation. Result:
**zero change across all 30 polls, 150s** — no spinner tick, no footer
change, nothing. This rules out the slow-huge-context theory as the
(sole) explanation and further narrows the anomaly to a genuine input-
delivery gap specific to `window2`'s own ConPTY child, not a timing
artifact of the previous, shorter check. No further Enter attempts were
made after this one (per this task's own "don't spam Enter, stop and
report" instruction) — `window2` is left exactly as found, composer text
intact, nothing else touched. `Ctrl+M` was considered but not added to
`allow_keys`/attempted: it is byte-identical to `"Enter"`'s own mapping
(`KEY_BYTES["Enter"] = b"\r"`, ASCII 0x0D = Ctrl-M) in `windows_backend.py`
— sending it would be a literal byte-for-byte repeat of the Enter attempt
already made, not a new code path, so it carries no new diagnostic value
and was correctly left `KEY_NOT_ALLOWED` rather than widened for this.

### Windows node-agent restart safety (Phase 0)

- **Goal / user value:** understand — with real, empirical evidence, not
  guesses — whether a Windows node-agent restart is safe for its own
  sessions, make the restart mechanism itself deterministic and correct,
  and build the most honest recovery path the current architecture
  allows, all BEFORE ever restarting the real dell-5530 node-agent that
  serves window/window2/wtest.
- **Status:** VERIFIED (the audit, the fixes, and the disposable
  end-to-end proof) — the real dell-5530 restart itself is deliberately
  NOT yet done (see Backlog item 7's own "Go/no-go" note).
- **Root causes (all three empirically confirmed, live, against real
  Windows processes — never theorized only):**
  1. `schtasks /end` does not reliably terminate the process tree a
     Scheduled Task launches (`powershell.exe` wrapper → `python.exe`
     node-agent → ConPTY-spawned session children) — non-deterministic:
     reproduced leaving the port-holding agent process alive while
     separately killing its own session children, on both the real
     dell-5530 node-agent and a fully isolated disposable instance.
  2. `WindowsSessionBackend`'s liveness check trusted `pywinpty`'s own
     `PtyProcess.isalive()`, which was confirmed to keep returning
     `True` indefinitely for a ConPTY child already gone at the OS level
     (`Get-CimInstance Win32_Process`/`tasklist` showed nothing) — the
     background reader thread's own "empty read: idle or dead?"
     disambiguation (`_reader_loop`) relies on exactly this check, so it
     busy-looped forever, and `status`/`tail` served stale content with
     no error indefinitely.
  3. The registry-based, MISSING-aware reopen (`terminal_registry_
     reopen`) was local-node-only — `mcp_app.py` called it directly on
     the controller's own local `TerminalService`, never routed through
     `controller`, so it was structurally unreachable for any remote
     node. The only reopen path a remote node's HTTP surface exposed
     (`/v1/sessions/{name}/reopen`) is the older, `killed_sessions.py`-
     backed one, which needs an explicit prior Kill a restart-caused
     drop never gets.
- **Fix:**
  - `node_agent.py`: new `AGENT_GENERATION` (a random id computed once
    per process, in `/v1/health` and every heartbeat payload — "is this
    a new process instance", task's own "process generation"
    requirement); new `POST /v1/internal/shutdown` route (bearer-auth'd)
    that sets a `threading.Event` on `app.state`; new shared
    `watch_for_shutdown(app, server)` coroutine (used by both
    `node_agent.py`'s and `windows_agent.py`'s own `main()`) that
    translates that event into uvicorn's own `server.should_exit = True`
    — a deterministic, graceful self-stop that never depends on Task
    Scheduler's own process-tree semantics. Both `main()` functions also
    gained `tg.cancel_scope.cancel()` right after `server.serve()`
    returns, fixing a related latent bug: without it, the task group's
    own `__aexit__` would wait forever on the never-returning heartbeat
    loop task, silently hanging the process past what looked like a
    clean shutdown (true for the pre-existing external-signal shutdown
    path too, not only the new one).
  - `windows_backend.py`: new `_win32_pid_alive(pid)` (real
    `OpenProcess`+`GetExitCodeProcess`, POSIX `os.kill(pid, 0)` fallback
    for this project's own Linux dev/test environment) as an injectable
    `pid_alive_resolver`; `_is_alive()` now checks it FIRST — a resolver
    saying "gone" always overrules a stale `isalive()==True`; a resolver
    saying "alive" (or itself failing) still falls through to
    `isalive()` as before, so no existing caller regresses. Wired into
    both `get_session()` and `_reader_loop()`'s own liveness check.
  - `node_client.py`/`node_agent.py`/`controller.py`/`mcp_app.py`: new
    `registry_reopen` method on the `NodeClient` Protocol +
    `LocalNodeClient` + `RemoteNodeClient`; new node-agent route `POST
    /v1/sessions/{name}/registry-reopen`; new fleet-aware `Controller
    Service.terminal_registry_reopen` (uses the existing `_route` /
    `resolve_session` machinery — the qualified `node_id/session` form
    is required for a MISSING session, since bare-name resolution only
    searches currently-live sessions); the `terminal_registry_reopen`
    MCP tool now calls `controller.terminal_registry_reopen` instead of
    the local-only `terminal.terminal_registry_reopen`. Also rewired
    (already-existing, previously-unwired) fleet aggregators:
    `terminal_watchdog_session_events` MCP tool now calls `controller.
    terminal_watchdog_session_events_fleet` (asks every online node,
    merges, tolerates one node's failure); `terminal_watchdog_
    acknowledge_session_event` gained a `node_id: str = "local"` param
    (backward-compatible default) and calls `controller.
    terminal_watchdog_acknowledge_session_event(node_id, event_id)`.
- **Scope / flow:** `registry_reopen`/watchdog fleet-wiring applies to
  every node (local and remote); the liveness fix and shutdown endpoint
  apply to `WindowsSessionBackend` specifically (the Linux/tmux backend
  has no equivalent staleness bug — tmux's own server independently
  tracks pane liveness). The OTHER registry/knowledge tools
  (`terminal_registry_list/get/search/purge`, `terminal_knowledge_*`)
  remain the documented, deliberate Phase A/B local-node-only posture —
  NOT changed by this pass (out of scope: this pass fixed only the tools
  load-bearing for the restart-recovery story itself).
- **UI route/screen:** none yet — Dashboard surfacing of `agent_
  generation`/registry-reopen as an operator action is PLANNED, not
  built (the MCP tools and HTTP routes are real and tested; no button
  exists for them yet).
- **API/tool/command:** `POST /v1/internal/shutdown`, `POST /v1/sessions
  /{name}/registry-reopen` (node-agent HTTP); `terminal_registry_reopen`
  (now fleet-aware), `terminal_watchdog_session_events` (now fleet-
  aware), `terminal_watchdog_acknowledge_session_event` (now takes
  `node_id`) (MCP tools — no new tool count change, 3 existing tools
  became fleet-aware).
- **Config/permission:** none new — `/v1/internal/shutdown` and
  `/registry-reopen` use the exact same bearer-token auth every other
  node-agent route already requires.
- **Data/schema/migration:** none — reuses the existing `session_
  registry.db` schema as-is (`agent_generation` is reported live, not
  persisted anywhere yet).
- **Acceptance/tests/evidence:** `tests/test_windows_backend.py` (7 new
  tests: OS-authoritative liveness override, POSIX fallback, integration
  through `get_session`/`_reader_loop`); `tests/test_node_agent.py` (5
  new tests: generation id, shutdown auth + event-setting, `watch_for_
  shutdown` flipping `should_exit`, registry-reopen round trip against a
  REAL unexpectedly-dropped tmux session, registry-reopen auth); `tests/
  test_controller.py` (5 new tests: fleet routing via qualified name,
  bare-name-for-MISSING correctly `SESSION_NOT_FOUND`, incomplete-
  metadata propagation, unreachable node, real local end-to-end). Full
  default suite re-run clean after every change (1 pre-existing,
  unrelated flake — `test_create_initial_prompt_goes_through_reliable_
  submission_once`, confirmed via `git stash` to fail identically on
  unmodified `HEAD`, not a regression from this pass).
  **Live disposable evidence (real dell-5530, isolated instance on port
  8791 — a temporary, narrowly-scoped LAN-only firewall rule was added
  for the test and removed afterward, along with the Scheduled Task and
  state directory used):** empirically confirmed (1) `schtasks /end`'s
  non-deterministic partial-kill behavior, reproduced identically on
  both the real node-agent and this isolated instance; (2) a disposable
  session's ConPTY child does NOT survive the agent process's own exit,
  confirmed via both `taskkill /F` and the new graceful `/v1/internal/
  shutdown` — settling the previously-unverified architectural question
  definitively; (3) the liveness fix correctly flips a zombie session to
  `pane_dead: true` (previously stuck `true` forever as alive); (4) the
  full recovery cycle end-to-end: create → list (registers into the
  registry) → graceful shutdown → relaunch (new `agent_generation`) →
  list (reconcile marks MISSING, records a `session_missing` drop event)
  → `registry-reopen` (new PID, `recreated_from_registry: true`, correct
  cwd/agent_type carried forward) — repeated across multiple full
  cycles, 0 orphan processes left running after cleanup, 0 duplicate
  dispatch, 0 cross-session attach. `window`/`window2`/`wtest` read-only-
  verified completely unaffected (identical PIDs/tail content) after
  every cycle.
- **Known limitations:** (1) recovery is metadata-only — conversation
  history is genuinely lost on a real restart, not resumed (see Backlog
  item 10 for the `--resume`-wiring follow-up that would fix this
  honestly, without ever claiming OS-level survival); (2) `agent_
  generation` is reported (health + heartbeat) but not yet persisted/
  surfaced anywhere in the dashboard or session_registry; (3) the OTHER
  registry/knowledge MCP tools remain local-node-only (documented,
  unchanged scope cut, not this pass's job); (4) the real dell-5530
  node-agent has NOT been restarted with any of these fixes live yet —
  they exist on disk there (deployed for the disposable test) and are
  fully tested/proven on an isolated instance on the SAME machine, but
  not yet exercised against window/window2/wtest themselves.
- **Dependencies:** `session_registry.py` (SessionRegistryStore,
  pre-existing, unmodified — this pass exercises it far more thoroughly
  than before, finds no bugs in it); `controller.py`'s existing `_route`/
  `resolve_session` machinery (reused, not replaced).
- **Follow-up/backlog:** the real dell-5530 restart itself (Backlog item
  7); `--resume` wiring for genuine conversation continuity (Backlog
  item 10); fixing `run-node-agent.ps1`'s own Scheduled Task definition
  so a future install doesn't need this session's manual graceful-
  shutdown-then-`/run` two-step (PLANNED, not built — the two-step is
  proven correct, just not yet packaged into the installer script);
  dashboard surfacing of `agent_generation` + a registry-reopen operator
  action.
- **Trace:** see this file's own commit.

### Conversation-continuity recovery (`--resume` wiring)

- **Goal / user value:** since the 2026-09-06 Phase 0 audit proved a
  Windows node-agent restart ALWAYS ends a session's real OS process,
  no exceptions — make the honest recovery path (`registry_reopen`)
  actually continue the SAME Claude conversation where possible, not
  just recreate an empty session in the same folder. Never claims OS/
  RAM survival; always a genuinely new process, honestly disclosed.
- **Status:** VERIFIED — real mechanism, real live E2E proof (3 restart
  cycles + 1 rename-then-resume cycle) on a real Windows node. The real
  dell-5530 node-agent restart itself is still deliberately NOT done
  (see Backlog item 7's own "Go/no-go" note, unchanged by this entry).
- **Root mechanics (real, live-verified against Claude Code 2.1.258 on
  dell-5530 via `claude --help` and live behavior — never assumed):**
  `-r, --resume [value]` resumes a conversation by session id;
  `--session-id <uuid>` assigns a specific (valid-UUID) id to a NEW
  conversation at launch; conversations are stored per-project at
  `~/.claude/projects/<escaped-cwd>/<uuid>.jsonl` (confirmed via real
  file listings on dell-5530, correlated by timestamp to window/wtest's
  own real activity). An unresolvable `--resume <id>` makes Claude Code
  print `"No conversation found with session ID: <id>"` and EXIT — it
  never silently falls back to a fresh conversation; this project's own
  verification only has to detect that real, existing behavior, not
  invent an equivalent guard itself. A workspace-trust confirmation
  dialog can still appear even on `--resume` for a cwd Claude Code
  hasn't independently trust-recorded (confirmed live for a generic
  path like `~`; NOT observed for an established project directory like
  `~/terminal-mcp`) — never auto-dismissed by this project (would be a
  silent security-prompt bypass). Codex was NOT verified (not installed
  on any node this project has live access to) — see Backlog item 11.
- **Fix / what's built:**
  - `config.py`: `SessionLifecycleConfig.resume_capable_agent_types`
    (verified-only allowlist, default `("claude",)`).
  - `session_backend.py`/`tmux.py`/`windows_backend.py`: `new_session`
    gained `extra_args: tuple[str, ...] = ()`, threaded through
    `lifecycle.py`'s `create()`. Both backends now support arbitrary
    extra launcher argv tokens, not just the bare command.
  - `session_registry.py`: new `conversation_id`/`recovery_state`/
    `recovery_detail`/`recovery_updated_at` columns (Migration 2,
    `schema.py`'s real migration framework); `SessionRecord.resumable`
    property (`recoverable AND conversation_id`); `upsert_seen` gained
    `conversation_id` (COALESCE-preserved, like `launch_command`) and
    now clears `recovery_state`/`recovery_detail` back to NULL once a
    session is seen genuinely ACTIVE again; new `set_recovery_state()`
    method for the transient RESTORING/RESUMED_OK/RECOVERY_FAILED
    signal, independent of any one caller's own return value.
  - `core.py`: `terminal_create_session` gained `resume_session_id` —
    for a resume-capable agent_type, ALWAYS launches with an explicit
    project-assigned id (`--session-id <uuid4>` for a genuinely new
    conversation, `--resume <id>` when continuing one), persisted into
    the registry immediately. `terminal_registry_reopen` now: sets
    RESTORING before launch; passes the record's own `conversation_id`
    as `resume_session_id` when `record.resumable`; calls the new
    `_verify_resume_or_fail()` (bounded `RESUME_VERIFY_TIMEOUT_SECONDS`
    = 15.0s poll — see its own constant comment for why 15s and not a
    shorter value found live during this feature's own E2E test) which
    detects Claude Code's own `RESUME_FAILURE_PATTERN` / an alive pane
    with real, non-trust-dialog content / a genuine timeout; sets
    RESUMED_OK or RECOVERY_FAILED accordingly and returns `error:
    "RECOVERY_FAILED"` with `recovery_detail` on failure — NEVER
    silently reports success for an unconfirmed resume. `terminal_
    status`'s own response gained a `recovery_state` field (sourced
    from the registry) so any caller — Coordinator included — sees it.
  - `coordinator.py`: `SessionSnapshot.recovery_state`; new gate check —
    `RESTORING`/`RECOVERY_FAILED` → `NEEDS_HUMAN`, refusing a fresh
    dispatch onto a session whose own conversation continuity is still
    unresolved (task item 5's own explicit requirement). `queue_engine.
    py`'s `_review()` populates it from the routed `terminal_status`.
  - `node_agent.py`/`node_client.py`: `create_session`'s Protocol/Local/
    RemoteNodeClient/HTTP-route surface gained `resume_session_id`.
  - `dashboard.py`: `/dashboard/api/registry/reopen` FIXED to route
    through `controller` (was local-node-only, mirroring the exact same
    gap the Phase 0 pass already fixed for the MCP tool of the same
    name) — the panel's own JS now sends the qualified `node_id/session`
    form (required for a MISSING session's bare name to resolve via
    `controller`'s routing) and surfaces `resumable`/`recovery_state` in
    each row plus a distinct RECOVERY_FAILED alert (never conflated with
    an ordinary metadata error).
- **Scope / flow:** applies to session creation/reopen on any backend
  (tmux + Windows both got `extra_args` support), but the resume-
  capable allowlist currently only enables it for `agent_type=claude`.
- **UI route/screen:** existing Dashboard "🗂 Khôi phục" (Recovery)
  panel, extended in place — no new screen. Session Manager per-record
  Restore button.
- **API/tool/command:** `terminal_create_session`/`terminal_registry_
  reopen` (MCP, gained `resume_session_id`/richer response fields, no
  breaking signature change), `POST /v1/sessions` and `POST /v1/sessions
  /{name}/registry-reopen` (node-agent HTTP, gained `resume_session_id`
  body field), `POST /dashboard/api/registry/reopen` (fixed to be fleet-
  aware).
- **Config/permission:** new `session_lifecycle.resume_capable_agent_
  types` (default `("claude",)`) — no new permission gate; reuses
  `session_lifecycle.enabled`/existing lifecycle permission checks
  entirely.
- **Data/schema/migration:** `session_registry.db` Migration 2 (real,
  tracked via `PRAGMA user_version` — see `schema.py`) — 4 new nullable
  columns, applied automatically on next open, no data loss, no manual
  step.
- **Acceptance/tests/evidence:** `tests/test_session_registry.py` (29
  existing, all still pass — no new dedicated migration test added this
  pass, covered indirectly via every other new test exercising the
  store through its real, migrated schema); `tests/test_coordinator.py`
  (4 new: RESTORING/RECOVERY_FAILED → NEEDS_HUMAN, None/RESUMED_OK →
  READY); full default suite green (only the same 1 pre-existing,
  unrelated flake). **Live E2E evidence (real dell-5530, isolated
  disposable instance on port 8791, cleaned up after — window/window2/
  wtest independently confirmed unaffected via read-only checks after
  every cycle):**
  1. Created `claude-e2e-resume` in `~/terminal-mcp` (an established,
     already-trusted project dir — no trust-dialog interference),
     confirmed real `--session-id <uuid>` on the actual process command
     line via `Get-CimInstance Win32_Process`.
  2. Told it a fact ("my favorite number is 8842"), confirmed real
     acknowledgment in the pane.
  3. **Cycle 1:** graceful `/v1/internal/shutdown` → relaunch (new
     `agent_generation`, confirming a genuinely new process) → `POST
     .../registry-reopen` → `resume_verified: true` → asked "what
     number?" → real answer **"8842"**.
  4. **Cycle 2:** repeated — `resume_verified: true` → real answer
     **"8842"** again.
  5. **Cycle 3:** first attempt hit the false negative documented above
     (`resume_verified: false`, real timeout) despite the resume having
     actually succeeded (confirmed by re-checking the live pane content
     directly) — this is exactly what found and justified the 6s→15s
     timeout fix. Retested after the fix: `resume_verified: true` → real
     answer **"8842"** a third time.
  6. **Rename interop:** renamed the session mid-flight (`rename` →
     registry row correctly carries `conversation_id` under the NEW
     name, confirmed via a live listing before any restart), then
     graceful-shutdown → relaunch → `registry-reopen` under the RENAMED
     name → `resume_verified: true`, same conversation.
  7. **Explicit resume-failure signal, live-reproduced:** a bogus all-
     zero UUID produced Claude Code's own `"No conversation found..."`
     text and the process exiting — confirmed this project's own
     verification correctly reports `RECOVERY_FAILED` for a genuinely
     unresolvable resume, never a false positive.
  8. Watchdog drop events (`session_missing`) correctly recorded on each
     restart and correctly marked `recovered` once `registry_reopen`
     brought the session back (a small, related fix: `registry_reopen`
     now calls `mark_drop_events_recovered_for` itself, rather than
     waiting for the next unrelated ordinary reconcile pass).
- **Known limitations:** (1) recovery is best-effort verification, not a
  byte-for-byte transcript diff — a resume that renders SOME real
  content within 15s with no failure signal is treated as confirmed;
  (2) a genuine send-reliability edge case was found (not fixed) during
  this feature's own E2E test — see Backlog item 12; (3) Codex not
  supported (Backlog item 11); (4) the dashboard's Recovery panel itself
  is still local-node-only (Backlog item 13) even though the underlying
  action is now fleet-aware; (5) `RESTORING` is a real but genuinely
  transient state (the whole `registry_reopen` call is synchronous, a
  few seconds at most) — a concurrent poller has a real but narrow
  window to observe it; this is disclosed, not hidden, and does not
  affect correctness (Coordinator still gates on it if seen).
- **Dependencies:** the Phase 0 restart-safety pass immediately above
  (liveness fix, `AGENT_GENERATION`, fleet-aware `registry_reopen`
  routing — this entry builds directly on top of all three).
- **Follow-up/backlog:** Backlog items 11–13.
- **Trace:** see this file's own commit.

### P0 Queue + Supervisor live test (local/Linux)

- **Goal / user value:** prove the Persistent Task Queue v2 auto-dispatch
  loop and Supervisor v2's suggest_only decision pipeline actually work
  end-to-end against real, live sessions and the real production
  service — not just unit tests — before ever considering enabling
  either for window/window2/wtest.
- **Status:** VERIFIED (local/Linux disposable sessions only, per this
  checkpoint's own explicit scope — remote Windows dell-5530 auto-
  dispatch remains a separate, still-pending backlog item).
- **What was found and fixed (two real, live-discovered bugs):**
  1. **`queue.db` migration drift:** this project's own real, existing
     `queue.db` (7 lanes, including window/window2/wtest) had `PRAGMA
     user_version=4` yet was genuinely missing `dispatch_idempotency_key`
     — confirmed via `git log -S` that this column has been part of
     Migration 2's own body since the very commit that introduced it, so
     the only honest explanation is this specific file was created
     against an in-development state of that migration before the
     column existed, with `user_version` already stamped past 2 by the
     time it was added. Every read of the affected table crashed with
     `IndexError`. Fixed with a new `Migration(5, ...)` that checks
     column existence via a real `PRAGMA table_info` first (never a
     try/except) — a no-op on any fresh `queue.db` (migration 2 already
     includes the column from the start), heals the one real drifted
     case otherwise. Applied to the real production `queue.db`.
  2. **Completion-marker line-wrapping bug (independent, more
     consequential):** the structured completion marker (`###TERMINAL_
     MCP_COMPLETION ...`, ~150-160 chars on one logical line) routinely
     exceeds a normal terminal's column width, so a captured pane
     genuinely wraps it across multiple physical rows (each padded with
     trailing spaces before its own real `\n` — real tmux/pyte row-
     rendering, not hypothetical). The old regex's middle group excluded
     `\n`, so a wrapped marker was **never matched at all** — a real
     disposable Claude session correctly replied and printed the exact
     expected marker (visually confirmed in the pane), yet the task sat
     in `VERIFYING` forever. This would very likely affect ANY real
     production task dispatched into a standard-width pane, not a rare
     edge case. Fixed in `status.py`'s `COMPLETION_MARKER_RE`: excludes
     only `#` now (never `\n`) — the marker's own fields never contain
     `#`, so it still terminates correctly at the real closing `###` and
     cannot run away past it; confirmed this doesn't create a false-
     bridging risk (a stray `#` anywhere still breaks that one match
     attempt, never causes bridging to a later, unrelated marker).
- **Config change:** `config.yaml` gained `queue: enabled: true` (the
  global auto-dispatch gate) — the per-lane `auto_dispatch_enabled` flag
  (the second, independent gate) stays `False` for every existing lane,
  including window/window2/wtest; only 4 disposable `claude-qtest-*`
  lanes were ever turned on for this test, all turned back off and their
  sessions killed once finished.
- **Scope / flow:** exercises the REAL running `terminal-mcp-http.service`
  process's own background `QueueLoop` (3s poll) and `SupervisorService.
  run_once` polling — the test script only ever enqueued tasks / created
  watches / observed state; it never manually drove a dispatch tick.
- **Acceptance/tests/evidence — live, real, on the actual production
  service (all disposable sessions/lanes cleaned up after):**
  1. **5 sequential tasks, 1 session:** all `QUEUED -> PRECHECK -> READY
     -> RUNNING -> VERIFYING -> COMPLETED`, one at a time, each with a
     unique task_id/nonce/summary_sha256 in `verification_evidence`
     (confirmed no cross-task contamination).
  2. **6 tasks across 3 sessions (2 each):** all COMPLETED, running in
     genuine parallel once each session had its own separate cwd (an
     initial attempt using ONE shared cwd for all 3 correctly triggered
     the Coordinator's existing cross-lane same-repo conflict check —
     working as designed, not a bug, but revealed a test-setup mistake).
  3. **Dependency chain:** task B (`depends_on: [A]`) correctly stayed
     `QUEUED` while A was `READY`/`RUNNING`, and only started dispatching
     once A reached `COMPLETED`.
  4. **Blocked task:** `metadata.artificial_blocker` correctly produced
     `BLOCKED` with `coordinator_reason: "operator-declared blocker:
     ..."`, verified alongside the dependency-chain task completing
     normally in a sibling lane.
  5. **Node/session unavailable then back:** killed a session with a
     task already `WAITING_SESSION`; task correctly held (never lost,
     never marked FAILED); recreating the session under the same name
     let the SAME task (same id) resume through to `COMPLETED`.
  6. **Controller restart mid-QUEUED:** enqueued a task, restarted
     `terminal-mcp-http.service` immediately (before dispatch) — task
     survived (`PRECHECK` right after restart) and completed exactly
     once (`attempt_count: 1`, one row with that title) — 0 dropped, 0
     duplicated.
  7. **Supervisor v2, `observe_only`:** watch created on a disposable
     session, correctly tracked `UNKNOWN -> COMPLETION_CANDIDATE ->
     VERIFIED_DONE` via the real quiet-window promotion, with ZERO
     actionable events generated (by design — `list_actionable_events`
     explicitly filters out `observe_only` policies) and zero auto-send.
  8. **Supervisor v2, `suggest_only`, full real cycle:** a real shell
     session's own `read -p "Do you want to continue? [y/n] "` produced
     a genuine `WAITING_INPUT` state → `attention_required` actionable
     event → `claim_event` (double-claim correctly rejected:
     `ACTION_ALREADY_ACTIVE_FOR_WATCH`) → `submit_decision` (`"y"`) →
     `review_action` (approve) → `execute_send` (real send, pane
     correctly shows the prompt answered and a fresh shell prompt) →
     re-`execute_send` correctly rejected (`ALREADY_SENT_OR_NOT_APPROVED`)
     — full idempotency confirmed. `approved_auto_continue` itself
     (auto-approval without a manual `review_action` step) was NOT
     separately tested this pass — the claim/decision/send/idempotency
     mechanics it shares with `suggest_only` are already proven above;
     see Known limitations.
  9. Dashboard's real data sources (`queue.session_task_board`,
     `queue.global_inbox`) confirmed reflecting live state throughout
     (correct running/queued/blocked_rework counts, correct fleet-wide
     totals across all 10 real+disposable lanes).
- **Known limitations:** (1) `approved_auto_continue` not separately
  live-tested this pass (see above); (2) remote Windows (dell-5530)
  auto-dispatch end-to-end is still a separate, unstarted backlog item
  (item 1) — this checkpoint was local/Linux only, per its own explicit
  scope; (3) Claude Code's own UI footer (e.g. "✻ Worked for Ns · done")
  always renders after a response, which pushed an artificially-crafted
  "waiting for input" phrase out of `detect_waiting_input`'s own "last 4
  non-empty lines" window when attempted inside a Claude session directly
  — a real shell session was used instead to get a clean, natural
  `WAITING_INPUT` signal; this is a testing-methodology note, not a
  product bug (a REAL Claude Code permission/confirmation dialog, unlike
  a plain instructed reply, does behave like the shell case).
- **Dependencies:** Persistent Task Queue v2 / Coordinator Agent (§7/§8,
  unchanged), Supervisor v2 (§6, unchanged) — this entry is a live
  verification pass over existing, already-documented features plus two
  real bug fixes it found along the way.
- **Follow-up/backlog:** remote Windows queue auto-dispatch smoke test
  (Backlog item 1, unchanged); `approved_auto_continue` live test.
- **Trace:** see this file's own commit.

### Living-requirements convention itself

- **Goal / user value:** any agent reads ONE file and knows what the
  system currently has, instead of re-deriving it from source or
  drifting out of sync with reality.
- **Status:** VERIFIED.
- **Scope / flow:** see "How agents should work" at the top of this
  file. Enforcement: `queue_engine.py`'s `build_dispatch_text` prepends
  a short reminder to read this file; `integration_reviewer.py`'s
  `IntegrationReviewGate.review()` checks a behavior-changing Handoff's
  `changed_paths` for a `docs/REQUIREMENTS.md` touch, returning
  `REWORK_REQUIRED` if missing (skipped when the originating task's
  metadata declares `docs_exempt`).
- **UI route/screen:** none yet (a link inside the Dashboard Task
  Manager is PLANNED).
- **API/tool/command:** none dedicated — enforcement is inline in the
  dispatch/review paths above.
- **Config/permission:** none dedicated.
- **Data/schema/migration:** none — a plain markdown file plus two code
  checks.
- **Acceptance/tests/evidence:** `tests/test_coordinator.py` (dispatch-
  text reminder), `tests/test_integration_reviewer.py` (requirements-
  update gate incl. `docs_exempt` opt-out, real git diff).
- **Known limitations:** the "does this diff look behavior-changing"
  heuristic is intentionally simple (any changed path outside test/doc/
  refactor-marked files) — same disclosed-heuristic posture as
  `_default_scope_reasoner` elsewhere in this codebase.
- **Dependencies:** Integration Agent (the check lives in its review
  gate, since that's the one place a real diff is already inspected).
- **Follow-up/backlog:** Requirements/Feature Matrix link inside the
  Dashboard Task Manager (PLANNED).
- **Trace:** see this file's own commit.

### AI Usage (read-only integration with the local 'AI Usage Monitor')

- **Goal / user value:** Terminal MCP can show, centrally, the Codex/
  Claude/Gemini/Antigravity CLI usage/quota this HOST already tracks via
  a separate, already-installed local service — without duplicating any
  of that service's own credential-reading/provider-API-calling logic.
- **Status:** VERIFIED and live (2026-09-07). Real browser (Playwright,
  headless Chromium) smoke against a disposable local dashboard instance
  confirmed the panel renders genuinely live data end to end (see
  Backlog item 26 for the full evidence/sample values).
- **Audit of the service being integrated (done first, per the task's
  own explicit "không đoán; dùng curl/source để xác minh"):** a
  SEPARATE project ("AI Usage Monitor" 0.6.0) — source at `~/workspace/
  ai-usage-monitor` (a plain directory, no git repo), deployed to `~/
  .local/share/ai-usage-monitor/app/ai_usage_monitor.py` (single-file,
  stdlib-only `http.server`, no framework/deps), run by a real, enabled
  `ai-usage-monitor.service` user-systemd unit (auto-starts at login,
  `Restart=on-failure`) on `127.0.0.1:8787` — loopback-only, no
  authentication of its own on the HTTP API (matches this project's own
  "loopback bind is its own boundary" posture elsewhere). It reads THIS
  machine's own real local OAuth credential files (`~/.codex/auth.json`,
  `~/.claude/.credentials.json`) and calls the REAL provider backend
  usage endpoints directly (`chatgpt.com/backend-api/wham/usage`,
  `api.anthropic.com/api/oauth/usage`) — Gemini is presence/auth-only
  (no stable local/public quota endpoint exists for every auth mode, per
  that project's own `SOURCES.md`); Antigravity reads a local telemetry
  file a separate hook writes. `GET /api/usage` (the one endpoint this
  integration reads) is already a clean, well-structured, machine-
  readable JSON response — no changes needed on that project's side at
  all (confirmed live: real sample response captured and used to design
  the normalizer below), so this integration is a pure, read-only
  consumer with zero duplicate collection logic, exactly per the task's
  own "tránh duplicate logic thu thập usage" requirement. Own in-process
  cache: Codex 60s TTL, Claude 180s TTL (never forced early by this
  integration — see `ai_usage_client.py`'s own docstring).
- **Architecture chosen:** adapter/read-only provider inside Terminal
  MCP (`ai_usage_client.py` + `ai_usage_service.py`), reusing this
  project's own established `urllib.request`-based HTTP client
  convention (`node_client.py`'s `RemoteNodeClient`) rather than adding
  a new HTTP dependency. `ai_usage_client.py` is a pure, bounded-timeout
  (`config.ai_usage.timeout_seconds`, default 2.0s) GET, never raises
  past its own `AiUsageClientError`. `ai_usage_service.py` normalizes
  every provider's own (subtly different) response shape into one common
  `providers: [{provider, ok, account, plan, windows: [{label,
  used_percent, remaining_percent, resets_at, severity}], usage_
  available, usage_message, error, updated_at, warning, critical}]`
  list, adds this project's own configurable warning/critical threshold
  classification (the AI Usage Monitor itself has no such concept — its
  own UI just picks a bar color inline), and NEVER invents a number: a
  window with no real `used_percent` renders `severity: null`/an
  "unavailable" message, never a fake 0%/100% bar.
- **Degraded state (task requirement: "tuyệt đối không làm dashboard/
  session controller treo theo"):** every call is bounded by `config.
  ai_usage.timeout_seconds` and never raises. A failure after at least
  one prior success returns the LAST real snapshot marked `stale: true`
  plus `last_error` (more useful than a bare "unavailable" when recent
  real data exists); a failure with no prior success at all returns
  `available: false` with a real `error` string — never a fake number
  either way. A separate short in-memory cache (`config.ai_usage.
  cache_ttl_seconds`, default 20s) on top of the AI Usage Monitor's own
  caching avoids hammering it on every dashboard poll.
- **Session correlation (item 4):** best-effort, LOCAL sessions only —
  disclosed explicitly: the AI Usage Monitor reflects THIS machine's own
  single set of locally logged-in CLI credential files, not a fleet-wide
  concept, so a session on a remote node (dell-5530/m910/macbook) is
  never correlated (would be misleading — that node's own `claude`/
  `codex` CLI, if any, uses ITS OWN separate local credentials, not this
  host's). A local session's `pane_current_command` (`claude`/`codex`)
  is matched against the corresponding provider's `ok` state — real,
  live-verified against actual local tmux sessions (see Backlog item 26).
- **Secret handling:** no token/API key/cookie is ever read, stored, or
  surfaced by this integration — it only reads the AI Usage Monitor's
  own already-computed, already-redacted-of-secrets JSON response. The
  provider `account` field (an email or masked id) IS passed through
  unredacted, matching the AI Usage Monitor's own dashboard, which
  already shows it to the same owner this Terminal MCP dashboard is
  gated to (the same Cloudflare Access identity) — never sent to a third
  party, never logged beyond this project's own existing redaction-safe
  logging.
- **UI route/screen:** `⋯` menu → "📊 AI Usage" → a slide-out panel
  (`#aiUsagePanel`, same modal-component shape as the existing Task
  Inbox panel) — provider cards with progress bars/warning-critical
  badges, a "Session hiện tại (local)" section, manual ↻ refresh
  (`force=1`), and an auto-refresh checkbox (30s, off-by-choice per
  viewer). Never a bare iframe — real data through this project's own
  route/adapter, per the task's own explicit "Không chỉ nhúng iframe nếu
  có thể truy cập data/API thật".
- **API/tool/command:** `terminal_ai_usage_status(force: bool = False)`
  (MCP), `GET /dashboard/api/ai-usage[?force=1]` (dashboard, `_read_
  guard` only — fleet/account-level status, not one session's own
  content, same posture as `/dashboard/api/queue/global-inbox` etc.).
- **Config/permission:** `AiUsageConfig` (`config.py`) — `enabled`
  (default `True`; unlike every autonomous-background-thread config in
  this project, which defaults OFF, there is no autonomous ACTION here
  to gate, only a bounded read), `base_url` (default `http://
  127.0.0.1:8787` — the ONE value an operator repoints when this
  project's own control plane later moves to a VPS, per the task's own
  explicit requirement; never made public on the internet), `timeout_
  seconds`, `cache_ttl_seconds`, `warning_threshold_percent`/`critical_
  threshold_percent` (both configurable, not hardcoded, per the task's
  own explicit "ngưỡng config, không hard-code nếu dễ config").
- **Data/schema/migration:** none — no persistent store at all (in-
  memory cache only), matching the task's own "tránh duplicate logic
  thu thập usage" (nothing is stored redundantly here; the AI Usage
  Monitor's own state remains the only copy).
- **Acceptance/tests/evidence:** `tests/test_ai_usage_client.py` (6,
  real disposable `http.server` instance — real HTTP round trip, real
  timeout, real malformed-JSON/non-dict-response/connection-refused
  handling, never mocks `urllib` itself), `tests/test_ai_usage_service.py`
  (17, normalization of the real captured sample response shape incl.
  severity thresholds, antigravity's differently-shaped `quota_windows`,
  a missing provider key, a `None` `used_percent` never given a fake
  severity, cache TTL/force-bypass, degraded-state stale-fallback and
  no-prior-cache paths, disabled-config short-circuit, the client-error
  boundary proven not to swallow an unrelated real bug, session
  correlation incl. a broken lister never breaking real usage data),
  `tests/test_ai_usage_mcp_tools.py` (3, real MCP call path), `tests/
  test_dashboard_ai_usage.py` (5, real Starlette `TestClient` — the
  route, degraded state, `force=1`, session correlation, and the
  dashboard HTML itself containing the new panel markup). **Live browser
  smoke** (Playwright, headless Chromium, against a disposable local
  dashboard instance with NO Cloudflare Access configured — see Backlog
  item 26 for why the real gated production URL isn't directly
  browser-testable from here — `base_url` pointed at the REAL, already-
  running `127.0.0.1:8787`): real menu click → real panel open → real
  rendered provider cards with a real Critical badge (Codex 5h at the
  real captured 99% used) and real local session rows (`terminal-mcp ·
  claude`, `codex-main · codex`, etc.) — genuinely live data through the
  real client → service → route → HTML → browser DOM pipeline, not
  mocked at any layer. Tool count 131 -> updated in `tests/test_server
  .py`/`tests/test_transports.py`. Full suite green.
- **Known limitations:** (1) usage/quota is per-MACHINE (this host's own
  logged-in CLI credentials), not per-terminal-mcp-SESSION — multiple
  local sessions running `claude` share ONE quota number, disclosed
  explicitly rather than implying a false per-session breakdown; (2) no
  correlation at all for sessions on remote nodes (dell-5530/m910/
  macbook), disclosed rather than guessed; (3) the real, gated production
  dashboard URL was not directly browser-tested (would need real
  Cloudflare Access session credentials this environment doesn't have) —
  the live browser smoke above used a disposable, ungated local instance
  running the exact same route/service/UI code instead; (4) the AI Usage
  Monitor project itself was NOT modified — no commit needed there.
- **Dependencies:** none new (stdlib `urllib`/`http.server` only, no new
  `pyproject.toml` dependency).
- **Follow-up/backlog:** none currently open — the AI Usage Monitor's
  own `/api/usage` was already sufficient; the "add a minimal endpoint
  there if unstable" fallback the task allowed for was not needed.
- **Trace:** see this file's own commit.

---

## Backlog (explicitly not done yet — tracked here so it isn't re-discovered)

1. **Live remote-node auto-dispatch smoke test (dell-5530,
   `RemoteNodeClient`)** — this task batch's own required next step
   before `config.queue.enabled` can be turned on anywhere, including
   window/window2. Not started as of this file's own commit.
2. Event-driven WAIT/wake posture for the Integration Agent's own
   merge-test role.
3. ~~Move-Task drag/drop UI for Task Migration.~~ **DONE, 2026-09-07**
   — a button-based "↷ Move" UI (prompt for target session), not literal
   drag/drop — see §10's own updated implementation note.
4. ~~Priority-edit/drag-reorder UI for the Task Manager.~~ **DONE,
   2026-09-07** — ↑/↓ buttons on queued tasks, wired to the existing
   reorder route — see §10's own updated implementation note.
5. ~~Requirements/Feature Matrix link inside the Dashboard Task
   Manager.~~ **DONE, 2026-09-07** — see §3's own updated implementation
   note.
6. **Enabling auto-dispatch against a real production session/lane —
   audited 2026-09-07, decision: KEEP DISABLED, exact blocker below.**
   `config.queue.enabled` (the GLOBAL gate) is already `true` in the
   real production `config.yaml` (done in an earlier checkpoint) — the
   auto-dispatch background loop process IS really running in
   production right now. What remains genuinely NOT done, audited this
   pass: `queue_lanes.auto_dispatch_enabled` (the PER-LANE gate) is
   confirmed `0` for all 17 real lanes in the live production `queue.db`
   (`wtest`, `window`, `window2`, `win3`, `terminal-mcp`, `mesflow`,
   `codex-main`, `promptflow`, `projectflow`, `nail`, and 7 others —
   read live, 2026-09-07). **Deliberately left this way** — not because
   evidence is thin (it is, in fact, extensive: `test_queue_engine_smoke
   .py`/`test_three_role_smoke.py`'s own real disposable-tmux, real-
   restart, real-race tests all prove 0 lost/0 duplicate dispatch under
   restart+reconnect, repeatedly, across this whole session) — but
   because flipping this flag for ANY specific real session is itself a
   real, outward-facing behavior change (that session would start
   receiving automatically-dispatched tasks with no human pushing them
   each time), squarely within this project's own standing rule that
   this class of action needs an explicit, per-instance human go-ahead,
   not an inference from generic disposable-test success. The
   mechanism's own safety is real and proven; the remaining gap is a
   DECISION, not a missing capability — left for the user to make
   explicitly, for a specific session, when wanted. (The dell-5530-
   specific live remote-node smoke test from item 1 is a SEPARATE,
   still-genuinely-blocked concern — see the Internet/VPS Phase 0 gate
   audit for why.)
7. **Direct-send reliability: ROOT CAUSE CONFIRMED, LOCAL FIX SHIPPED,
   REMOTE DEPLOYMENT STILL PENDING** (task: "P0 FIX TRIỆT ĐỂ DIRECT SEND
   WINDOWS / CLAUDE INPUT" — user report: a ChatGPT-originated prompt
   into `window` sometimes needs multiple submits before it's accepted).
   See the "Direct-send verification: continued-polling ack evidence"
   entry below for the full writeup. **Critical outstanding step:** the
   fix lives in this repo's own `terminal_mcp/core.py`, but a session on
   dell-5530 (window/window2/wtest, and any other Windows session) is
   served by a SEPARATE deployment of this codebase running directly on
   that machine (`C:\Users\tranv\terminal-mcp`, its own node-agent
   process) — confirmed via SSH (`tranv@192.168.1.250`, key-based,
   already working) that this remote checkout is older than the local
   fix. **The fix has NOT been deployed there and the remote node-agent
   has NOT been restarted** — deliberately not done autonomously this
   session (a live Windows machine with many real, active sessions;
   restarting its node-agent is a real, outward-facing action that
   deserves an explicit go-ahead, the same as the earlier
   `terminal-mcp-http.service` restart was only done after the user's
   own explicit "restart an toàn"). Until that deployment happens,
   window/window2/wtest and every other dell-5530 session still run the
   OLD, buggy verification logic.
   **2026-09-06 update — fix files deployed, restart attempted, restart
   did NOT actually happen:** the full `terminal_mcp/` package (68
   files) was deployed to `C:\Users\tranv\terminal-mcp` via a safe
   staged-copy-then-atomic-swap (old code kept at
   `terminal_mcp_backup_20260906` for rollback), confirmed byte-
   identical, confirmed no impact on the already-running process (files
   on disk don't affect an already-imported Python process). A restart
   was then attempted via
   `schtasks /end /tn TerminalMcpNodeAgent-dell-5530` followed by
   `schtasks /run /tn ...` — **this did NOT restart the real process.**
   `schtasks /end` only ends Task Scheduler's own tracking of the
   `powershell.exe` wrapper; the actual `python.exe -m
   terminal_mcp.windows_agent` process it launched (PID 3352, running
   since 2026-09-05 23:06 — i.e. still the pre-fix code in memory) kept
   running and kept holding port 8790. The new instance Task Scheduler
   launched failed to bind that port (`WinError 10048`) and crashed
   immediately (exit code 1). Net effect: **window/window2/wtest were
   completely unaffected** (same PIDs — 18304/1760/1464 — same
   `created` timestamps, same live tail content, both before and after
   the attempt) — but only because nothing actually restarted, so this
   attempt is NOT evidence that a real restart is safe. A real restart
   now requires a hard `taskkill /F` on the old PID, which is the exact
   disruptive action `windows_backend.py`'s own docstring warns about
   (real risk of killing the ConPTY child sessions with no known
   reattach mechanism). Surfaced to the user via AskUserQuestion before
   attempting that; **user chose to wait for a safer window** rather
   than force it now. Fix remains undeployed-in-practice (on disk, not
   loaded) until a real restart happens.
   **2026-09-06 Phase 0 update — full restart-safety audit complete,
   DEFINITIVE finding: session survival across a node-agent restart is
   NOT achievable with the current architecture, by any method.** See
   the "Windows node-agent restart safety (Phase 0)" Feature Details
   entry below for the complete writeup (root cause, fix, disposable
   evidence). Summary:
   - **`schtasks /end` root cause, empirically confirmed (not just
     theorized):** reproduced twice — once against the real dell-5530
     node-agent, once against a fully isolated disposable instance on
     the same machine. It is non-deterministic and unsafe: in one run it
     left the actual agent process (the port-holder) completely
     untouched while separately, silently killing that process's own
     ConPTY session children; in another it did the same. It never once
     achieved the restart it was asked for. **Fixed** by a new,
     deterministic graceful self-shutdown path (`POST
     /v1/internal/shutdown`, bearer-token-auth'd) that asks uvicorn to
     stop via its own `should_exit` mechanism — no reliance on Task
     Scheduler's own process-tree bookkeeping at all.
   - **The open architectural question is now closed, empirically:** a
     disposable session's ConPTY child process (and its sibling
     `conhost.exe`) does **not** survive the node-agent process's own
     exit — confirmed identical outcome via a hard `taskkill /F` AND via
     the new fully graceful `/v1/internal/shutdown` path. This is no
     longer an unverified risk (as `windows_backend.py`'s own module
     docstring previously, honestly, flagged it) — it is a confirmed
     fact for this deployment. **Any future node-agent restart on
     dell-5530 WILL end window/window2/wtest's real OS processes,
     unconditionally, regardless of how the restart is performed.**
   - **A second, independent, real bug found and fixed in the same
     investigation:** `WindowsSessionBackend`'s liveness check
     (`proc.isalive()`, from `pywinpty`) was confirmed to keep reporting
     a genuinely-dead ConPTY child as alive indefinitely — `pane_dead`
     stayed `False`, `reader_alive` stayed `True`, `tail`/`status` kept
     serving stale cached content forever, with no error, and the
     background reader thread busy-looped forever never noticing. Fixed
     with a new, OS-authoritative liveness check
     (`_win32_pid_alive`/`OpenProcess`+`GetExitCodeProcess`) that
     overrules a stale `isalive()` — this is what makes MISSING
     detection (below) actually fire correctly at all.
   - **A third, real gap found and fixed:** the Persistent Session
     Registry's own honest, MISSING-aware reopen
     (`terminal_registry_reopen`) was **local-node-only** — unreachable
     for any remote node (dell-5530, m910) at all. The only reopen path
     a remote node's HTTP surface exposed was the OLDER, `killed_
     sessions.py`-backed one, which needs an explicit prior Kill that a
     restart-caused drop never gets — meaning, before this fix, a
     restarted remote node's MISSING sessions had **no working recovery
     path whatsoever**, not even the honest "new process, same cwd/
     agent_type" recreate. Fixed: new `/v1/sessions/{name}/registry-
     reopen` node-agent route + fleet-aware `controller.
     terminal_registry_reopen`/`terminal_watchdog_session_events_fleet`/
     `terminal_watchdog_acknowledge_session_event`, all now routed
     through `controller` like every other multi-node operation, using
     the qualified `node_id/session` form (required, since a MISSING
     session by definition isn't in any node's live listing that bare-
     name resolution searches).
   - **Disposable proof (real dell-5530, isolated instance on port 8791,
     never touching window/window2/wtest):** created real sessions,
     confirmed graceful-shutdown → process exit (port frees) → relaunch
     → `session_missing` drop event correctly recorded → `registry-
     reopen` correctly recreates (new PID, `recreated_from_registry:
     true`, same cwd/agent_type) → repeated across multiple full
     restart cycles. 0 orphan processes left running after cleanup
     (verified via `Get-CimInstance Win32_Process`/`tasklist`), 0
     duplicate dispatch, 0 cross-session attach. `window`/`window2`/
     `wtest` independently confirmed completely unaffected throughout
     (same PIDs, same tail content) via a read-only check after every
     cycle.
   - **Known, real, still-open limitation:** recovery is metadata-only
     (cwd/agent_type honestly carried forward) — **conversation history
     is genuinely lost**, not resumed. Claude Code's own `resume_
     conversation_id` is already captured live per-session (`models.py`/
     `windows_backend.py`) but not yet persisted into the registry or
     wired into `registry_reopen`'s own relaunch command — doing so
     (passing `--resume <id>` on the new process) is the single most
     valuable remaining mitigation and is NOT yet built (scoped out of
     this pass for time; see Backlog item 10).
   - **Go/no-go on the real dell-5530 restart:** deliberately NOT done
     this session. The fact pattern changed materially mid-investigation
     (from "restart might be safe, unverified" to "restart WILL end
     these processes, confirmed") — window2 had a real, unsent typed
     prompt pending and window/wtest were both actively mid-task ("esc
     to interrupt") the last time they were checked. This is exactly the
     kind of newly-material risk information this project's own standing
     rules require surfacing before acting on an earlier, now-outdated
     authorization, not proceeding on it silently.
   - **2026-09-07 update — `--resume` conversation-continuity mitigation
     now built and live-proven (disposable only)** — see the
     "Conversation-continuity recovery (--resume wiring)" Feature
     Details entry above. This materially changes the cost of a real
     restart (conversation history is no longer necessarily lost, only
     the OS process — still true and unconditional), but window/window2/
     wtest were ALL CREATED before this feature existed, so **none of
     them currently has a `conversation_id` recorded** — a real restart
     TODAY would still only recover them via the honest, metadata-only
     path (new empty conversation, same folder), not `--resume`. A real
     restart's own recovery quality can be upgraded first by simply
     having each session reconciled at least once (already true — they
     are listed regularly) is NOT enough; `conversation_id` is only ever
     set at CREATE time by this project's own code, and window/window2/
     wtest were created by an EARLIER version that never set it. Two
     honest options, not yet chosen:
     (a) accept metadata-only recovery for these three specific sessions
     if/when a real restart happens (conversation history genuinely
     lost, exactly as originally found), or
     (b) before restarting, read each session's OWN currently-loaded
     conversation id from its live `~/.claude/projects/<cwd>/*.jsonl`
     directory (the most-recently-modified file, by timestamp
     correlation — a disclosed HEURISTIC backfill, not a certainty,
     since this project never assigned that id itself) and manually
     `UPDATE`/backfill it into `session_registry.db`'s `conversation_id`
     column for exactly these three rows before the restart, so `--
     resume` becomes available for them too. Not done automatically by
     any code in this pass (a manual, one-time, disclosed-as-heuristic
     step, only worth doing immediately before an actual planned
     restart, not speculatively now).
   - **Recovery plan for window/window2/wtest, IF/WHEN a real restart is
     authorized (docs only — no restart performed by this entry):**
     1. Snapshot before: PID/created/activity for all three (matches the
        pattern already used and reported in this file's own restart-
        attempt history above) + (optional, per option (b) above) each
        session's own current `.jsonl` conversation id, backfilled into
        the registry first if conversation continuity is wanted.
     2. Graceful stop via `POST /v1/internal/shutdown` (bearer-token
        auth'd, the SAME real node token dell-5530 already uses) — never
        `schtasks /end` (proven non-deterministic and unsafe) and never
        a raw `taskkill` unless the graceful path itself fails to make
        the process exit within a reasonable wait.
     3. Poll for the port to actually free, then trigger the Scheduled
        Task's own start (`schtasks /run /tn TerminalMcpNodeAgent-
        dell-5530`) — confirms `/v1/health`'s `agent_generation` differs
        from before (proves a genuinely new process, not a stuck one).
     4. For each of window/window2/wtest: call `terminal_registry_reopen`
        with the qualified `dell-5530/<name>` form. Expect `recreated_
        from_registry: true`; if a `conversation_id` was backfilled,
        also expect `resume_verified: true` and a real answer to a
        verification question about recent, real prior context — never
        just trust the flag, ask something checkable, exactly as this
        feature's own E2E test did.
     5. Report exactly what happened for each of the three, honestly —
        conversation continued vs. genuinely lost, any RECOVERY_FAILED,
        and the real PIDs before/after — never a summary that implies
        more success than what was actually observed.
     6. Still requires the user's own explicit go-ahead before step 2 —
        this plan existing does not itself constitute that authorization.
8. **Dashboard Task Manager/Supervisor-Coordinator panel deployment
   gap (found and fixed):** the production `terminal-mcp-http.service`
   process had been running continuously since before commits `20f6ff0`/
   `fa5e661`/`d345524`/`edd316f` were written — a long-lived Python
   process keeps its already-imported module code in memory regardless
   of what's on disk, so none of the Task Manager/Global Task Inbox/
   Supervisor-Coordinator panel UI (real, tested, committed) was ever
   actually visible in production, purely because the service was never
   restarted after those commits landed. Fixed by `systemctl --user
   restart terminal-mcp-http.service` (confirmed safe: tmux sessions —
   including window/window2/wtest — are independent OS processes,
   completely unaffected by this control-plane process restarting;
   verified reachable immediately after). **Process/deploy takeaway:**
   this project has no CI/CD or auto-restart-on-deploy step yet — every
   future checkpoint that touches `dashboard.py`/`mcp_app.py`/
   `server_http.py` needs an explicit `systemctl --user restart
   terminal-mcp-http.service` (with a post-restart health/session check,
   as done here) before it's actually live, not just committed. Tracked
   here as a standing operational reminder, not a one-time fix.
9. **Internet/VPS migration roadmap** — PLANNED, phased, not started.
   See "Internet / VPS migration roadmap" section below for the full
   phase breakdown. Phase 0 (stability gates) is itself mostly backlog
   items 1 and 7 above — the scheduled-task restart bug itself is now
   FIXED (see the "Windows node-agent restart safety (Phase 0)" Feature
   Details entry) — none of Phase 1+ starts until Phase 0 is green.
10. **`--resume` wiring for `registry_reopen`** — DONE, 2026-09-07. See
    the "Conversation-continuity recovery (--resume wiring)" Feature
    Details entry below for the full writeup, live E2E evidence (3
    restart cycles + a rename-then-resume cycle, all against a real
    disposable Windows session on dell-5530), files/tests/commit.
11. **Codex `--resume` support** — PLANNED, not started. `config.session
    _lifecycle.resume_capable_agent_types` defaults to `("claude",)`
    only — Codex CLI was never installed/verified on any node this
    project has live access to (confirmed absent on dell-5530,
    2026-09-07: `where codex` found nothing), so it was deliberately
    left out rather than guessed at. Adding it is a one-line config
    change PLUS first verifying Codex's own `--help`/resume-flag
    behavior live — never assumed from Claude Code's own flag names.
12. **Real send-reliability edge case found (not the same as the
    2026-09-06 P0 fix), disclosed, not fixed in this pass** — during
    this feature's own live E2E test (2026-09-07), a `press_enter=true`
    send that returned `SUBMIT_CONFIRMED` (`evidence: ["OUTPUT_CHANGED"]`)
    against a FRESH Claude session in a project with auto-memory startup
    (reading `MEMORY.md` before the composer is interactive) twice left
    the typed text sitting unsent in the composer — a genuine false
    positive, the opposite direction from the 2026-09-06 fix (which
    closed a false NEGATIVE). A plain follow-up `Enter` keypress always
    resolved it. Root cause not yet isolated (plausibly: the composer
    accepts keystrokes into its input buffer before Claude Code's own
    startup tool-use sequence has attached its submit handler, so the
    Enter races a state the 2026-09-06 fix's polling window doesn't
    cover). Scoped out of this pass (a different investigation, real
    effort to isolate properly) — tracked here so it's never mistaken
    for a "fixed" issue or silently re-discovered from scratch.
13. **Dashboard's own Persistent Registry panel stays local-node-only**
    — the underlying `terminal_registry_reopen` action is now fleet-
    aware (this entry, item 10), but `registry_list`/`registry_search`
    (and their dashboard/MCP surfaces) were deliberately NOT changed in
    this pass, matching the same documented Phase A/B scope cut as
    `terminal_knowledge_*`. A remote node's MISSING/resumable sessions
    are recoverable via the fleet-aware API (`node_id/session` qualified
    form) but do not yet APPEAR in this specific dashboard panel/MCP
    listing tools unless queried directly.
14. **`terminal_create_session`'s `initial_prompt` send could visibly
    echo twice for a brand-new plain-shell session — FIXED, 2026-09-07.**
    Originally found live while running the full suite ahead of the
    Kanban checkpoint (`tests/test_session_lifecycle.py::
    test_create_initial_prompt_goes_through_reliable_submission_once`,
    reproduced consistently, not flaky). Evidence: the captured pane
    showed the typed command once with no prompt prefix at all (`echo
    hello-lifecycle`), then again as a normal prompt-prefixed line once
    the shell's own prompt actually rendered (`...$ echo hello-
    lifecycle`) followed by its real output.
    **Root cause, confirmed via code reading (`lifecycle.py`'s own
    `SessionLifecycleService.create`):** for `agent_type="shell"`,
    `expected_command` is always `None`, and the readiness loop's own
    condition (`if expected_command is None or ...: state = "READY";
    break`) made it return `READY` on the very FIRST loop iteration —
    zero polls, zero readiness signal, zero wait at all — the instant
    tmux merely reported the session existing, regardless of whether
    the shell had actually finished its own startup (rc-file sourcing,
    PS1 draw, readline attaching to the tty). `terminal_create_session`
    sends `initial_prompt` the instant `state == "READY"` comes back, so
    the text+Enter wrote into the pane while the shell was still mid-
    startup: the kernel tty's own raw echo of the just-written text
    (drawn with no prompt yet, since the shell hadn't drawn one) was
    followed by the shell's OWN readline redraw of that same still-
    buffered line once it finished attaching — visibly duplicating it.
    Only ONE real `send_keys`/Enter call is ever issued (confirmed by
    reading `_send_text_and_verify_locked` — not a second, extra send
    from this project's own code).
    **Fix (minimal, at the actual root):** `lifecycle.py`'s readiness
    loop, for the `agent_type == "shell"` branch only, now waits for a
    real, cheap readiness signal — the pane has drawn SOMETHING (its own
    prompt, at minimum), checked via `capture_lines` — instead of
    short-circuiting to `READY` unconditionally. Same bounded deadline
    (`create_ready_timeout_seconds`) as every other `agent_type`; a
    genuinely silent/empty-`PS1` shell (untested edge case) just falls
    back to the pre-existing `CREATED`-after-timeout behavior, never a
    regression. Backend-agnostic (`lifecycle.py` is shared by both the
    tmux and Windows backends via the same `SessionBackend` protocol) —
    the same fix applies to a Windows shell session once deployed there
    too, though that deployment is separately gated (see Backlog item
    20/the window2 investigation).
    **Tests:** the original regression test now passes consistently (5
    repeated runs, previously reliably failing); `test_session_lifecycle
    .py`'s full file (44 tests) and every other file touching session
    creation (`test_kill_reopen.py`, `test_session_registry_integration
    .py`, `test_controller.py`, `test_windows_terminal_service_
    integration.py`, `test_session_knowledge_integration.py`, `test_move
    _session.py`, `test_server.py`, `test_remote_webterm_proxy.py`,
    `test_transports.py`, `test_task_manager_ui.py` — every file that
    creates a session at all) re-verified green, 0 regressions. Full
    suite green.
    Item 12 above (the `press_enter=true` false-positive `SUBMIT_
    CONFIRMED`-but-actually-unsent race on a FRESH Claude session with
    auto-memory startup) remains a **separate, still-open** issue — same
    general "freshly-created session readiness race" family, but a
    distinct code path (Claude's own composer readiness, not a plain
    shell's prompt draw) and NOT fixed by this entry.
15. **Unified Task System §20's Global Tasks Kanban slice is now VERIFIED
    and live** (2026-09-07) — see §20.1a's own full implementation note
    (data model choice, new store/service methods, MCP tools, dashboard
    route, tests, live Playwright evidence) and §4a of `docs/
    CHATGPT_USAGE.md`. The REST of §20 (PM/Orchestrator skill routing,
    Task Planner, git worktree isolation, the Integration/Merge Agent,
    the Phase A-E Startup Operating Model) remains PLANNED, unbuilt —
    this entry marks progress on exactly one slice of a much larger,
    still-open design, not completion of §20 as a whole.
16. **`adapters.py`'s `_WORKING_PATTERNS` (`\bworking\b`/`\bthinking\b`)
    has the same theoretical false-positive shape** the now-fixed
    `_WAITING_PATTERNS` bare-word entries had (see this file's own
    "TARGET_AWAITING_APPROVAL false positive on ordinary composer text"
    Feature Details entry) — an ordinary composer line that happens to
    contain the word "working" or "thinking" could, in principle,
    misclassify `identify_target_state`/`can_submit_now` as `TARGET_
    RUNNING`. Disclosed, NOT reported, NOT reproduced — deliberately
    left untouched by that fix to avoid widening it beyond the actual
    confirmed root cause. Revisit only if a real report/repro surfaces,
    same standing rule as everywhere else in this file (never fix an
    unreproduced suspect speculatively).
17. **Unified Task System §20.2's PM/Orchestrator capability schema +
    deterministic router is now VERIFIED and live** (2026-09-07) — see
    §20.2a's own full implementation note (data model choice, new
    store/router/service modules, MCP tools, dashboard routing_reason
    display, tests, live disposable E2E evidence) and §4b of `docs/
    CHATGPT_USAGE.md`. No PM auto-loop exists yet (every decision is an
    explicit call). The REST of §20 (Planner/task-breaking, git
    worktree isolation, the Integration/Merge Agent extension, the
    Phase A-E Startup Operating Model) remains PLANNED, unbuilt.
18. **Unified Task System §20.3's Planner (task-breaking) split
    infrastructure is now VERIFIED and live** (2026-09-07) — see
    §20.3a's own full implementation note (deliberately NO automatic
    complexity-based splitter — the decomposition always comes from the
    caller; new store/service modules, the new narrowly-guarded
    BLOCKED->COMPLETED state-machine edge, MCP tools, dashboard child_
    progress display, tests, live disposable E2E evidence proving the
    full split->dependency->completion lifecycle) and §4c of `docs/
    CHATGPT_USAGE.md`. The REST of §20 (git worktree isolation, the
    Integration/Merge Agent extension, the Phase A-E Startup Operating
    Model) remains PLANNED, unbuilt.
19. **Unified Task System §20.4's git isolation policy + mechanical-only
    conflict auto-resolve are now VERIFIED and live** (2026-09-07) —
    see §20.4a's own full implementation note (confirms the section's
    own "mostly REUSE" framing: the Coordinator's pre-existing
    `expected_cwd` check needed ZERO new code; new git_worktree.py/
    git_isolation_service.py modules, the new mechanical-conflict retry
    in integration_engine.py's `_merge` — real git `-X ignore-all-space`
    behavior, never a custom content-guessing resolution; MCP tools,
    tests with real git subprocess calls throughout, live disposable
    E2E evidence with a real tmux session proving the Coordinator gate
    genuinely refuses a mismatched cwd and accepts a matching one) and
    §4d of `docs/CHATGPT_USAGE.md`. NOT built: PM-based routing of the
    Integration Agent to a specific capability-profiled session (doesn't
    cleanly map onto the existing architecture without a deeper
    redesign — disclosed, deferred) and the rebase-before-start policy.
    The REST of §20 (the Phase A-E Startup Operating Model) remains
    PLANNED, unbuilt.
20. **`window2`'s composer is STILL stuck** (2026-09-07) — see this
    file's own "P0 follow-up: window2 composer still stuck" Feature
    Details entry for the full investigation. Two more real root causes
    were found and fixed (a `status.py` duplicate of the `adapters.py`
    bare-word bug; `terminal_send_keys(["Enter"])` gained real
    acceptance verification) and proven correct on a disposable session
    on the same real host — but the exact same, carefully-verified,
    twice-attempted action had genuinely NO effect against `window2`
    itself, an anomaly NOT explained or resolved. None of this session's
    accumulated fixes (this entry, item 14, the earlier TARGET_AWAITING_
    APPROVAL fix) are deployed to `dell-5530`'s own node-agent — doing
    so would restart it, and this project's own Phase 0 finding means
    that kills `window2`'s real Claude process, losing its own
    unsubmitted composer text. This is a real, disclosed decision for
    the user: accept that loss to deploy every accumulated fix, or leave
    `window2` exactly as it is (safe, unchanged, but still stuck) until
    a further diagnosis or a deliberate decision is made.
    **2026-09-07 P0 follow-up audit — confirmed root cause for HALF the
    symptom, other half remains genuinely unresolved:** a fresh, fully
    read-only live audit (`resolve_session`/`terminal_status`/
    `terminal_input_context`/`node_sessions`/`terminal_list_sessions`
    against the real `dell-5530` node, never touching `window2` itself)
    proved `dell-5530`'s node-agent is running OLD code that predates
    TODAY's own fixes — two independent, live markers: (1) `terminal_
    status` still reports `reason: "recent prompt matched '\bpermission
    \b'"`, the EXACT bare-word pattern removed from `status.py`/
    `adapters.py` earlier today (commits `fe53f97`/`1c0a2ff`) — proves
    the deployed build predates both; (2) `terminal_list_sessions`'s row
    for `window2` has NO `resume_conversation_id` field at all, proving
    the deployed build predates the "Conversation-continuity recovery"
    feature (commit `5a93cc4`, itself from earlier this same morning) —
    matches this file's own pre-existing, explicit "Windows node-agent
    restart safety (Phase 0)" note that dell-5530's node-agent "has NOT
    been restarted with any of these fixes live yet." This DOES fully
    explain why `terminal_send_text(..., press_enter=True)` was wrongly
    refused with `TARGET_AWAITING_APPROVAL` (the composer's own text
    contains "Permission", still matching the OLD, undeployed-fix
    pattern) — a real, root-caused, already-fixed-in-repo bug, just not
    yet live. It does NOT explain the deeper anomaly: `terminal_send_
    keys(["Enter"])` never went through that classifier at all (checked
    via code reading — `_input_guard`'s `identify_target_state` check
    only runs for `terminal_send_text` with `press_enter=True`, never
    for `terminal_send_keys`), and every write path in this codebase
    that could deliver an Enter byte (`send_text`'s Enter, `send_keys`'s
    Enter, `windows_webterm.py`'s live `write_raw`) converges on the
    EXACT SAME single `entry.proc.write()` call against the SAME
    long-lived `pywinpty.PtyProcess` handle for this session (confirmed
    via direct code reading, `windows_backend.py`) — a call already
    proven, twice, to write successfully (no exception) yet have zero
    observable effect on `window2` specifically. No in-repo code change
    can alter that shared call without either corrupting the composer's
    own content (a raw text-level `\n` probe was considered and
    deliberately NOT attempted — Claude Code's composer supports literal
    newlines, so this risks polluting the exact content being preserved)
    or requiring a node-agent restart to load new code — which the Phase
    0 finding confirms kills the ConPTY child. Additionally checked (new
    this pass): `window2` has a live, ATTACHED visible desktop viewer
    (`visible_window: true`, `desktop_session_id: 1`, `attached: true`,
    `pid: 1760`, via `windows_visible_console.py`'s socket-relay
    architecture) — read-confirmed this is architecturally a separate
    relay process that never touches `entry.proc` (so it cannot itself
    explain a failed programmatic write), but is flagged as a real,
    concrete fact worth the user's own attention (a stuck/frozen-looking
    relay window is a distinct, human-visible symptom from this
    investigation's own read path, which bypasses the relay entirely).
    Also checked (new this pass): the LOCAL `session_registry.db` (the
    real store the `--resume` recovery mechanism reads from) has NO row
    at all for `window2` — its `conversation_id` was never captured, so
    the project's own "safe" resume-after-restart path would NOT recover
    this specific conversation automatically today; only a manual,
    out-of-band correlation (timestamp-matching `~/.claude/projects/
    <escaped-cwd>/*.jsonl` on `dell-5530` itself, per the "Conversation-
    continuity recovery" Feature Details entry's own "Root mechanics")
    could locate the right conversation id if a restart is ever accepted.
    Zero writes/sends were attempted against `window2` this pass —
    every call was read-only; the composer's content is unchanged.
    Decision on whether to accept a node-agent restart (and its real,
    now more precisely characterized cost) remains the user's alone.
21. **Unified Task System §20.6 Phase A (Delivery discipline) — 3 of 5
    pieces now VERIFIED and live** (2026-09-07): Definition of Ready
    (opt-in, `dor_gate.py`), WIP limits (`CapabilityProfile.max_queued`,
    a hard PM routing gate), and a risk-level human-approval gate
    (`CoordinatorGate`'s new `require_approval_for_risk_levels`) — see
    Phase A's own implementation note in §20.6 for full evidence
    (tests + a real disposable live E2E) and §4e of `docs/CHATGPT_
    USAGE.md`. Definition of Done and Conflict prediction needed no new
    code (already real via existing mechanisms, per the section's own
    "extend, don't rebuild" framing). NOT built: a distinct "ownership/
    affinity prefers continuity" scoring factor beyond the already-real
    `project_affinity` soft score, and any project-level wiring that
    actually turns on `require_approval_for_risk_levels` for a real
    project (the mechanism is real and tested; no policy currently
    activates it). Phases B-E remain entirely PLANNED, unbuilt.
22. **Unified Task System §20.6 Phase B — incident lane VERIFIED and
    live** (2026-09-07): `terminal_task_create_incident`/`terminal_
    list_active_incidents` — see Phase B's own implementation note in
    §20.6 and §4f of `docs/CHATGPT_USAGE.md`. Real dispatch/
    independent build-test/rework-bound pieces needed no new code
    (already real, reused as-is). Phases C-E remain entirely PLANNED,
    unbuilt.
23. **Unified Task System §20.6 Phase C — release lifecycle state
    machine VERIFIED and live** (2026-09-07): `release_store.py`/
    `release_service.py` (new), `terminal_release_create`/`advance`/
    `rollback`/`status`/`list` — see Phase C's own implementation note
    in §20.6 and §4g of `docs/CHATGPT_USAGE.md`. A `prod` release
    requires a known-good artifact + rollback plan at creation and an
    explicit human approval to deploy — both real, enforced, never
    optional/auto-approved. Disclosed PARTIAL: no technical, session-
    identity-based enforcement of WHO may supply that approval (no
    caller-identity system exists in this project's MCP layer to check
    against). Phases D-E remain entirely PLANNED, unbuilt.
24. **Unified Task System §20.6 Phase D — PM summary + backlog hygiene
    VERIFIED and live** (2026-09-07): `pm_summary.py` (new),
    `terminal_pm_summary`/`detect_stale_backlog`/`detect_duplicate_
    tasks`/`close_task_with_confirmation` — see Phase D's own
    implementation note in §20.6 and §4h of `docs/CHATGPT_USAGE.md`.
    End-to-end audit trail, knowledge capture, and resource/cost
    awareness needed NO new code (already real via each checkpoint's
    own event log, `session_knowledge.py`, and `ControllerService.
    list_nodes()`/`pending_counts()` respectively) — disclosed
    explicitly rather than silently claimed as newly built. "Human
    controls destructive close" is real and enforced: the only mutating
    action refuses without an explicit `confirmed=true`.
25. **Unified Task System §20.6 Phase E — security/control-plane
    VERIFIED and live** (2026-09-07): repeated-identical-failure ("stuck
    in a loop") detection in `CoordinatorGate` (ON by default,
    `repeated_failure_threshold=3`, extends the existing
    `max_review_attempts` cap rather than replacing it), and fleet-wide
    Emergency Stop/Resume (`pm_summary.emergency_stop_all_lanes`/
    `emergency_resume_all_lanes`, `terminal_emergency_stop`/`terminal_
    emergency_resume`) — see Phase E's own implementation note in §20.6
    and §4i of `docs/CHATGPT_USAGE.md`. Least privilege/secret redaction
    documented as already true by construction (no credential-granting
    mechanism exists anywhere in this project's MCP surface) — disclosed
    rather than redundantly rebuilt. Both new pieces proven against
    REAL disposable tmux sessions (never `window`/`window2`/`wtest`)
    through the real `engine.tick()` dispatch loop, not just unit tests
    — see `tests/test_queue_engine_smoke.py` (`pytest -m queue_smoke`).
    This completes the entire §20.6 Phases A-E roadmap to the extent
    each phase's own disclosed scope allows (see items 21-25 for the
    honest per-phase scope cuts — most notably: DoR's `required_os`/
    `required_capabilities`/`dependencies` fields not mandated, and no
    project has actually opted a real risk-level/`require_approval_for_
    risk_levels` policy in yet — the mechanism is real, wiring one in
    for a specific project is a future increment).
26. **AI Usage (read-only integration with the local 'AI Usage Monitor')
    — VERIFIED and live** (2026-09-07): `ai_usage_client.py`/`ai_usage_
    service.py` (new), `terminal_ai_usage_status` (MCP), `GET /dashboard
    /api/ai-usage` + a new "📊 AI Usage" dashboard panel — see this
    file's own "AI Usage" Feature Details entry above for the full audit
    (a SEPARATE, unmodified project, `ai-usage-monitor.service`, real
    provider usage/quota already computed there), architecture, and
    degraded-state design. Real live browser smoke (Playwright, headless
    Chromium, against a disposable local instance — NOT the real gated
    production URL, see that entry's own "Known limitations") confirmed
    the panel genuinely renders live data end to end: real sample values
    captured — Codex `5h: 99% used (Critical)`, `Weekly: 12% used`,
    account `tranvuhungbkit@gmail.com`, plan `plus`; Claude `5h: ~71-79%
    used (Warning)`, `Weekly: ~67% used`; Gemini `OK, usage data
    unavailable` (by that project's own design — no stable local/public
    quota endpoint for every Gemini auth mode); Antigravity `OK, no live
    telemetry yet` — and real local session correlation (`terminal-mcp ·
    claude`, `codex-main · codex`, `mesflow · claude`, and 7 others, each
    correctly tagged). This project makes ZERO changes to the AI Usage
    Monitor project itself — its own `/api/usage` was already a clean,
    sufficient, machine-readable JSON response; no separate commit was
    needed there. Tool count 130 -> 131.

---

## Internet / VPS migration roadmap (PLANNED — docs only, no code yet)

Goal: today, the Controller/Dashboard/Queue/Coordinator/Registry all run
on the `local` (Dell) machine, and every node (including Dell's own
tmux backend) is only reachable because the Dell box is on and on the
same LAN/tunnel. The target end state moves the control plane to a
small VPS reachable over the internet, with every real node (Dell,
dell-5530, m910, macbook, and future ones) connecting **outbound only**
to it — so no node needs an open inbound port, and Dell going offline
no longer takes down the other nodes' ability to be controlled.

**Rationale (for future agents / not to be re-litigated per phase):**
the VPS is a *light control plane* — it holds queue state, session
registry, task history, and routes commands; it does not run builds,
tests, or Claude/Codex itself. All actual work (tmux, ConPTY, file
edits, git, test runs) stays on the edge nodes, matching the existing
`ControllerService` → `NodeClient` (local vs remote) split already in
the codebase (see section 12, Node/fleet support) — this migration
generalizes that existing local/remote split, it does not redesign it.
Keeping queue/registry state centrally (not per-node) is what lets one
node's outage stay isolated to that node's own sessions instead of
stalling the whole fleet.

```
                    ┌─────────────────────────────┐
                    │   VPS (control plane)        │
                    │  Controller · Dashboard ·     │
                    │  Queue · Coordinator ·        │
                    │  Supervisor · Registry ·      │
                    │  audit/history (SQLite→PG)    │
                    └───────────────▲───────────────┘
                     outbound HTTPS/WSS, node token
              ┌──────────────┬──────┴───────┬──────────────┐
              │              │              │              │
        ┌─────▼─────┐  ┌─────▼─────┐  ┌─────▼─────┐  ┌─────▼─────┐
        │   Dell     │  │ dell-5530 │  │   m910    │  │  macbook  │
        │ Node Agent │  │ Node Agent│  │Node Agent │  │Node Agent │
        │ tmux/Claude│  │ ConPTY/   │  │ tmux/     │  │ tmux/     │
        │  /Codex    │  │ Claude    │  │Claude/Codex│  │Claude/Codex│
        └────────────┘  └───────────┘  └───────────┘  └───────────┘
        no inbound port   no inbound port  no inbound port  no inbound port
```

### Phase 0 — Stability gates (blocks everything below; code work stays
in the *current* repo/deployment, not the future VPS)

- Reliable direct-send on Windows actually deployed and live-reverified
  on dell-5530 (Backlog item 7 — fix shipped locally, remote deploy
  attempted 2026-09-06, real restart still pending a safe window; the
  scheduled-task restart-doesn't-kill-the-process bug found during that
  attempt should be fixed here too, since Phase 2's node-agent will need
  a trustworthy restart/reconnect story anyway).
- Task Queue / Task Manager / Supervisor dashboard verified at runtime
  end-to-end (Backlog item 1 — the live remote-node auto-dispatch smoke
  test on dell-5530 via the real `RemoteNodeClient`, not local/mock) —
  0 dropped / 0 duplicate dispatch under restart and reconnect.
- Session registry survives controller restart and node reconnect
  without duplicating or losing sessions.
- Node-agent restart has a known-safe story: either it provably does
  not lose sessions, or — if it can't be made safe (real risk on
  Windows ConPTY, per `windows_backend.py`'s own docstring, confirmed
  unresolved as of 2026-09-06) — a written, explicit recovery contract
  (what "restart" is allowed to cost, who approves it, how loss is
  reported) exists before Phase 2 makes node-agent reconnects a routine,
  automatic event over the internet instead of a rare, manually-
  approved LAN action.

#### Phase 0 gate audit (2026-09-07, no Windows/dell-5530 restart performed)

Per explicit instruction this pass: audit every gate, close what doesn't
require restarting the real dell-5530 node-agent/Windows sessions,
mark the rest BLOCKED and move on.

- **Gate 1 (reliable direct-send deployed+live-reverified on dell-5530):
  BLOCKED.** Unchanged from Backlog item 7/20 — the fix is real and
  shipped locally, but verifying it live specifically requires
  restarting the real dell-5530 node-agent, which this pass is
  explicitly forbidden from doing. No substitute closes this gate — it
  is Windows-ConPTY-specific by its own nature.
- **Gate 2 (live remote-node auto-dispatch smoke test): PARTIALLY
  BLOCKED.** The gate as written names dell-5530 specifically (Backlog
  item 1) — still BLOCKED, same reason as Gate 1. A real, equivalent
  smoke test against a DIFFERENT already-connected real remote node
  (m910, Linux — no ConPTY/Windows restart risk at all) would genuinely
  reduce the underlying architectural uncertainty (does auto-dispatch
  actually work correctly through a real `RemoteNodeClient`, restart-
  and-reconnect included) without touching anything forbidden — but was
  deliberately NOT attempted this pass: m910 is a real, shared
  production node this session has not separately inventoried for any
  currently-running real work of its own (unlike `window`/`window2`/
  `wtest`, which have an explicit, maintained protected list), and
  taking on that new production risk wasn't specifically authorized
  here. Left as a disclosed, well-scoped, low-risk next step rather than
  attempted speculatively.
- **Gate 3 (session registry survives controller restart / node
  reconnect without duplicating or losing sessions): VERIFIED_LIVE,**
  based on real, repeated evidence already accumulated THIS session —
  every checkpoint's own `systemctl --user restart terminal-mcp-http.
  service` (many times this session alone) was followed by a real
  post-restart check confirming all 3 remote nodes (dell-5530, m910,
  macbook) re-registered cleanly with fresh heartbeats and no
  duplication, plus real ongoing dashboard/session traffic resuming
  immediately (see e.g. the real `window2` 200 OK dashboard request
  logged immediately after the Phase E restart). This is the local
  control-plane process restarting, not a node-agent restart — exactly
  what this gate is actually asking about (node-AGENT restart safety is
  Gate 1/4's own separate, Windows-specific concern).
- **Gate 4 (node-agent restart has a known-safe story — either provably
  safe, or a written recovery contract): already REAL, no new work
  needed.** The "Windows node-agent restart safety (Phase 0)" Feature
  Details entry (2026-09-06) already estabished the honest answer
  (restart is NOT safe for ConPTY sessions, by design, unconditionally)
  and the "Conversation-continuity recovery (--resume wiring)" entry
  plus Backlog item 7's own explicit numbered recovery-plan sequence
  (snapshot before -> graceful `/v1/internal/shutdown` -> poll for a
  genuinely new `agent_generation` -> `terminal_registry_reopen` per
  session -> honest report) together ARE that written contract. Nothing
  new required by this pass.

**Net result: Phase 0 is still not fully green** (Gates 1 and 2's
dell-5530-specific half remain BLOCKED, unconditionally, until a real
Windows node-agent restart is separately authorized) — Gates 3 and 4
are real and green. No code changed for this audit; it is a documentation
pass over already-real evidence plus one new disclosed scope decision
(m910 smoke test deliberately deferred, not silently dropped).

#### Internet-connectivity audit (2026-09-09) — what is actually true today

Full audit of "can a PC on another internet connection become a node
today". Answer: **no, not without an overlay VPN**, and until this pass
even the overlay path was refused by the code. Nothing in the roadmap
above was found to be overstated — Phase 2 really is unimplemented — but
three specifics were being read too optimistically in practice and are
now written down in `docs/multi-node.md`'s own "Reaching a node over the
internet" section:

- **The data plane is `controller -> node`, inbound to the node.** Only
  the heartbeat is outbound. A NAT'd node is therefore *visible but
  uncontrollable*: heartbeat lands, dashboard row goes ONLINE, every real
  operation fails. A green node row is not evidence the node is usable.
- **Neither existing tunnel carries node traffic.** The Cloudflare Access
  dashboard tunnel is browser/ChatGPT -> controller (verified live: an
  unauthenticated heartbeat POST over it returns `302` to the Access
  login page, `auth_status: NONE`, never this app's own `401`), and the
  OpenAI Secure MCP Tunnel is loopback `8767`. `cloudflare_ssh` is a
  *bootstrap* transport — it installs the agent, then registers a plain
  `http://{bind_host}:{port}` endpoint that does not ride the tunnel.
- **Overlay VPN was blocked in three independent places.** Tailscale
  addresses are CGNAT `100.64.0.0/10`, which Python's `ipaddress` does
  not treat as private (verified live), so `is_lan_scannable()` refused
  them in `resolve_lan_bind`, `resolve_allowed_cidrs`, and
  `remote_connect.validate_hostname_or_ip` at once. Fixed this pass by
  the opt-in `TERMINAL_MCP_TRUSTED_VPN_CIDRS` + `is_trusted_node_address()`
  (default empty == unchanged behaviour; globally-routable entries
  refused; `is_lan_scannable()` itself deliberately untouched because it
  also gates active subnet scanning). Live-verified end-to-end against
  the real bind + CIDR-guard path on a disposable port.

Live fleet state at audit time: `local`, `dell-5530` (Windows) and `m910`
(Linux) all ONLINE with fresh heartbeats and agent `0.12.0`; `macbook`
correctly derived OFFLINE (~100 min stale). All four endpoints are
plaintext `http://192.168.1.x:8790` — LAN only.

Gaps deliberately NOT closed this pass (no acceptance evidence available
without changing production): no node self-registration (an unknown
`node_id` heartbeat is refused `NODE_NOT_FOUND`, so onboarding still
requires a controller-side operator action *and* controller -> node
reachability at onboarding time); no token rotation/revocation; no
rate-limit or lockout on node-agent bearer auth; no replay protection
(no nonce/timestamp) on heartbeats; `endpoint` scheme is unvalidated, so
a plaintext `http://` endpoint to a public host would leak the bearer
token; audit rows are written per-node, not centrally.

### Phase 1 — Controller decoupling

Separate the Controller/Dashboard/Queue/Coordinator/Registry roles from
the Dell machine so Dell becomes just another Node Agent (tmux/Claude/
Codex/workspaces), not special-cased. Controller becomes stateless-ish
process state + a persistent DB (see Phase 3 for SQLite→Postgres),
addressed by stable `node_id`/`session_id` (already largely the case —
verify no code path assumes "the controller runs where Dell's tmux
does"). Node connectivity becomes outbound-only in this phase's design
(even before the VPS exists), so Phase 2 is a transport swap, not a
re-architecture.

### Phase 2 — Internet node transport

Each Node Agent (Windows/Dell/M910/macOS) connects **outbound**
HTTPS/WSS to the central controller instead of the controller dialing
in. Per-node machine token (already exists per node via `token_env`
config — extend, don't replace). Heartbeat/reconnect/backoff so a
node's laptop sleeping, changing Wi-Fi, or losing its IP doesn't
require manual re-registration. No inbound port required on any node.
Any task enqueued while a node is offline is preserved and auto-
dispatched on reconnect (builds on the existing Persistent Task Queue
v2 durability, not a new queue).

### Phase 3 — VPS deployment

Ubuntu VPS, ~2 vCPU / 2 GB RAM / 30–40 GB SSD as the baseline sizing
target (to be confirmed against Phase 6's resource benchmark, not
assumed). Deploys Controller + Dashboard + Queue + Coordinator +
Supervisor + Registry + audit/history. HTTPS/WSS via a real domain
(e.g. `terminal.mesflow.net`) fronted by Cloudflare, same posture as
today's `dashboard.cloudflare_access_team_domain` gating. Storage:
SQLite acceptable to start (matches today's deployment), but the
schema/access layer must have a clear migration path to PostgreSQL —
Postgres becomes the production default once queue/history/fleet size
grows past what SQLite comfortably handles (no fixed threshold set yet;
Phase 6's benchmark should inform this, not a guess made now).

### Phase 4 — Remote fleet operations

From ChatGPT/dashboard: create/reopen/kill a session on any online
node; node selection by capability/OS/load (extends the existing
`nodes.overload_thresholds`/node-registry logic, not a new scheduler).
Tasks persist when their target node is offline and auto-dispatch on
reconnect (Phase 2's job, exercised here at the product level).
Dashboard shows node/session/task state fleet-wide. Explicitly: none of
this depends on Dell being online — Dell is just one more node once
Phase 1 lands.

### Phase 5 — HA / operations / security

Per-node token rotation; auth/RBAC beyond today's single-team
Cloudflare Access gate; audit trail (extends the existing audit DB
already in the codebase); TLS everywhere; DB backups; watchdog +
health metrics (extends the existing watchdog/health endpoints, see
section 13); terminal-history retention policy; rate limits; secret
redaction in logs/history; disaster-recovery procedure; controller
upgrade path that doesn't drop connected node state (ties back to
Phase 1's stateless-ish controller design).

### Phase 6 — Acceptance

Real test matrix before calling the migration done: Dell and a Windows
node on two different real internet networks (not just LAN); mobile/
browser/ChatGPT control through the VPS; node disconnect/reconnect;
controller reboot; node reboot; node IP change; baseline load (50+
sessions across a few dozen nodes); queue survives an extended node
offline period with no duplicate dispatch and no lost tasks; no cross-
session output bleed. Benchmark actual controller CPU/RAM/WebSocket-
connection-count/history-growth on the VPS sizing target to confirm the
"light control plane" assumption this whole roadmap rests on, rather
than asserting it.

**Ground rule for this roadmap:** no code changes for Phases 1–6 start
until Phase 0 is fully green (all its bullets independently verified,
not just "probably fine"). This section is docs-only as of
2026-09-06.
# P0 Codex verified-submit watchdog

Codex prompt submission uses a durable, evidence-driven state machine shared
by MCP, dashboard, queue, and supervisor paths. A submission is persisted as
`QUEUED` before text injection, text is injected exactly once, then the
adapter polls composer/output evidence at `0.4s` intervals. Retries send only
`Enter`, up to `max_enter_attempts` (default `3`), and stop immediately on
`ACCEPTED`/`RUNNING`. Pager or incomplete-buffer evidence becomes `STUCK`
without pressing Enter. Active submissions are reconciled by a `1.5s`
background sweeper after process restart; prompt text is stored only in a
0600 local SQLite submission store and is not logged or returned by status
APIs. Configuration is under `submit_watchdog` in `config.yaml`.

The retry allowlist is agent-specific: `retry_agent_types: [codex]` is the
only production setting that permits automatic Enter retries. Claude and
unknown agents use single-submit semantics—one injection and at most the
initial Enter; no watchdog/sweeper retry or prompt resend. Unconfirmed Claude
delivery is reported as `DELIVERY_UNKNOWN`/`STUCK`, never guessed as success.
All MCP, dashboard, queue, supervisor, controller, Linux tmux, and Windows
ConPTY callers converge on the same backend/agent policy.
