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
| Notes / Ideas store (kho ghi chú: MCP `note_*` + `/dashboard/notes`) | VERIFIED |
| Notes surface application-layer auth (webauth session or verified CF Access) | VERIFIED |
| Dashboard: Requirements/Feature Matrix link | VERIFIED |
| Worktree Janitor (reclaim isolated task worktrees) | CONTRACT ONLY — no executor, nothing deletes yet |
| Read-only repo access for external agents (`repo_*` MCP tools) | VERIFIED (V1, read-only) |
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
| Work efficiency telemetry (per-task counters + aggregation/savings) | IMPLEMENTED_NOT_LIVE_VERIFIED (runtime-driven rows + tests; not yet attached in a live deployment — the attach call is the coordinator's central wiring) |
| Token-efficiency benchmark (18 real bugs from git + 2 synthetic) | MEASURED, 2026-09-14 — headline verdict **FAIL** against its own pre-registered bar; see `docs/TOKEFF_BENCHMARK.md` |
| Unified Task System §20 (Kanban/PM/Planner/git isolation/Phase A-E) | VERIFIED — see §20 itself for the exact per-slice scope |
| Work Mode: a planner claim briefs itself (similar-bug retrieval + module context pack, paths verified) | VERIFIED (V1) |
| Work Mode: the budget and the gate constrain a REAL run (dogfood, `dogfood` runbook) | VERIFIED (dogfood; see its own "what is not claimed" note) |

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
- **Reading a repo that lives on another node:** `GET /v1/repo/{op}` on
  the node agent (read-only, GET-only, dispatched through
  `repo_read.OPERATIONS` — a fixed table of ten read functions, so the
  endpoint cannot be asked to write/checkout/fetch regardless of input),
  reached through `NodeClient.repo_op` and routed by
  `repo_service.RepoService`. Each node enforces its OWN
  `repo_read`/`session_lifecycle` allowlist over its own paths, which is
  the correct owner of that decision. `/v1/repo-evidence` (metadata only,
  for the Coordinator gate) is its older sibling — **and had never once
  worked before 2026-09-14**: see the Feature Details entry below.

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

## 15. API / MCP tool inventory (from `tests/test_server.py`'s own exact set)

The count in this heading used to be pinned at 96 and had drifted; the
authoritative set is and always was `tests/test_server.py`'s own
assertion, which fails the moment a tool is added or removed. This
section names the GROUPS, never a total.

Session ops: `terminal_list_sessions`, `terminal_tail`, `terminal_capture`,
`terminal_status`, `terminal_send_text`, `terminal_send_keys`,
`terminal_exit_copy_mode`.
Bindings: `terminal_bind`, `terminal_get_binding`, `terminal_list_bindings`,
`terminal_unbind`, `terminal_tail_bound`, `terminal_status_bound`,
`terminal_send_bound`.
Audit: `terminal_list_input_audit`, `terminal_input_context`.
Notes / Ideas (the ONLY tools not `terminal_`-prefixed — they touch no
terminal/session/node; see `docs/notes.md`): `note_create`, `note_get`,
`note_search`, `note_list`, `note_update`, `note_delete`, `note_restore`,
`note_add_attachment`, `note_remove_attachment`, `note_link_to_project`,
`note_mark_applied`, `note_facets`.
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
Read-only repo access (V1, READ-ONLY — `repo_read.py`/`repo_service.py`):
`repo_status`, `repo_head`, `repo_branches`, `repo_remotes`, `repo_tree`,
`repo_read`, `repo_search`, `repo_diff`, `repo_log`, `repo_show_commit`.
There is deliberately NO write counterpart (no `repo_write`/
`repo_checkout`/`repo_commit`/`repo_push`/`repo_reset`/`repo_clean`), and
`tests/test_repo_read_mcp_tools.py` asserts that absence at the MCP
surface — the read-only guarantee is pinned by a test, not by convention.
See the "Read-only repository access for external agents" Feature Details
entry for the full contract.

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

- **Config file:** `config.yaml` (`AppConfig`, `config.py`) — nested config
  sections: `permissions`, `input_policy`, `supervisor`, `queue`,
  `dashboard`, `session_lifecycle`, `session_knowledge`, `session_access`,
  `ask_chatgpt`, `maintenance`, `fleet_sync`, `work`, `repo_read`,
  `submit`, `submit_watchdog`, `integration_loop`, `ai_usage`,
  `auto_recovery`, plus per-node/discovery/remote-connect sections under
  `nodes`. (The count that used to head this line had drifted and is
  dropped rather than re-pinned; `AppConfig`'s own fields are the list.)
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

### The budget and the gate, proven against a real run (dogfood) — 2026-09-14

- **Goal / user value:** `bug_spec.gate` and `bug_spec.budget_check` already
  had unit tests, which prove the arithmetic: given these numbers, is the
  verdict right. That is a different sentence from "the budget constrains a
  run". A verdict nothing consults constrains nothing, and a soft budget that
  is only ever computed is advice. This entry is the end-to-end evidence that
  a worker is actually stopped.
- **Status: VERIFIED (dogfood).** `tests/test_dogfood_budget_gate.py` (25) and
  `tests/test_dogfood_work_v1.py` (8 + 1 opt-in skip), run together by the new
  `dogfood` runbook. Real file reads and real `git grep` against THIS
  checkout, a real `QueueService`, a real `InboxService`, a real
  `BugSpecStore` and a real `TelemetryStore`. No fixture repository: a
  dogfood over a convenient fake proves the fake.
- **What is claimed, and what is deliberately NOT.** Claimed: a worker that
  routes its reads and searches through the shipped contract is stopped at the
  declared limits, hands the task back instead of widening it, and cannot pass
  the budget without leaving a record of why. **Not claimed:** that an
  unmediated agent obeys a budget it merely read in a prompt. No test can
  establish that, and asserting it here would manufacture exactly the false
  confidence the rest of this system refuses. The harness
  (`tests/dogfood_worker.py`) is therefore the dogfood's worker, not a new
  product surface — it re-implements no limit, and every verdict in it comes
  from `bug_spec`.
- **Permission is asked BEFORE the work, not after.** `budget_check` is asked
  about the count the next operation WOULD reach, so the sixth file of a
  five-file budget is never opened. Checking afterwards would report the
  overrun accurately and permit it anyway.
- **The five properties, and where each is proven:**
  1. **L1 stays within 5 source files and 2 search rounds.**
     `FILE_SEARCH_BUDGET[L1]` is asserted to be exactly `{max_files: 5,
     max_search_rounds: 2}`, then five real package files are read and the
     sixth is refused (`worker.opened` has five entries; `files_read` is 5,
     not 6), and two real `git grep` rounds run before the third is refused.
     The refusal's action is `NEEDS_REDEFINE` with the contract's own reason
     ("an L1 that runs out of budget was not actually an L1"). A companion
     test records the narrowing as a count: the run opened ≤ 5 of the 142
     files in `terminal_mcp/`.
  2. **Past the budget only by explicit, recorded escalation.**
     `bug_spec.escalation` is the record; `TaskTelemetry.budget_escalations`
     and a `budget escalation: <why>` note make it auditable, and
     `record_budget_escalation("")` is refused outright — an unexplained
     overrun and a justified one must not be the same row. The escalation
     report carries `TASK_CONTINUES: True` and the same `bug_id`, so the
     planner adds detail to the SAME task.
  3. **An incomplete spec ⇒ `NEEDS_REDEFINE`, and the SAME task resumes.**
     A thin spec blocks the session; the worker hands back
     `reply_to_planner` with `MISSING` and `QUESTIONS_FOR_PLANNER` and the
     hand-back is counted (`redefine_count`). Refinement is proven to resume
     rather than restart in three places: the same `bug_id` re-gates READY,
     the real queue board still holds exactly one task with the same
     `queue_task_id`, and `work_planning.redefine()` returns the same
     `spec_id` with `source_commit` from the first pass intact.
  4. **HARD ⇒ `NEEDS_USER_HINT` with 1–3 precise questions, same issue
     resumes, prior analysis preserved.** `triage` returns HARD with
     `assist_recommended`; `developer_assist_request` yields at most
     `MAX_ASSIST_QUESTIONS` questions, each ending in `?`, none of them a
     vague "more detail", with the current findings and hypotheses attached
     and `never block` in `if_unavailable`. The parked issue is **not**
     offered to a second planner (`NOTHING_TO_CLAIM`), a hint request with no
     question is refused, and after `attach_human_hint` the SAME `issue_id`
     comes back claimable with `findings_before_hint`, the question asked, and
     the hint all still on it.
  5. **`PLAN_CONFIRMED` / `PLAN_ADJUSTED` / `PLAN_MISMATCH` recorded as
     telemetry.** All three land on the telemetry row AND on the spec, in the
     same imported vocabulary; an invented fourth verdict raises; the verdict
     survives `TelemetryStore` and `TaskTelemetry.from_dict` (so a restart
     does not lose it); `summarise()` reports `plan_outcomes` with
     `unreported` kept apart from the three verdicts, and a `mismatch_rate`
     over zero verdicts is `None` rather than `0` — zero would read as "our
     specs are always right" when it means nobody checked.
- **Worker must not start a broad repo audit on an incomplete spec.** Proven
  as a fact rather than an instruction: on a blocked session both `read()` and
  `search()` raise `SpecNotExecutable`, `worker.opened == []`, and
  `files_read == search_rounds == 0`, against a package the test also counts
  so the size of the audit that did NOT happen is on the record.
- **New runbook:** `dogfood` (`scripts/agent/dogfood.sh`), declared in
  `.projectflow/knowledge/PROCEDURE_STATE.json`, `risk: read_only`, same
  PASS/FAIL + stage + summary + log-path output contract as the others. It
  resolves its interpreter through the main checkout's venv when run from a
  worktree (`git rev-parse --git-common-dir`) and pins `PYTHONPATH` to the
  checkout it was invoked in, so the dogfood measures the code in front of
  you rather than whichever checkout owns the interpreter. Verified through
  the registry itself: `ProcedureRegistry.run("dogfood")` returns
  `PASS dogfood :: run :: PASS stage=dogfood summary="33 passed, 1 skipped"`.
- **Scope boundary held:** `mcp_app.py` and `dashboard.py` were not touched,
  `bug_spec.py` was not edited at all (every property above is proven against
  it as it already ships), and nothing was deployed.
- **Known limitations:** (1) the harness proves enforcement for a worker that
  routes through it — an agent using its own editor tools is invisible to
  these counters, the same gap `work_telemetry` already records as an empty
  `signal_sources` rather than a confident zero; (2) the escalation path
  permits continuing after a recorded reason, which is what "unless an
  explicit, recorded escalation" asks for, but `bug_spec.escalation`'s own
  docstring describes the report as something sent INSTEAD of widening — both
  readings are legitimate and the dogfood pins the permissive one, with the
  record as the price; (3) the run is single-process, so the budget it proves
  is per worker session, not per task across retries.
- **Trace:** `tests/dogfood_worker.py` (new), `tests/test_dogfood_budget_gate.py`
  (new), `scripts/agent/dogfood.sh` (new),
  `.projectflow/knowledge/PROCEDURE_STATE.json`, `terminal_mcp/work_telemetry.py`
  (`plan_status`, `plan_note`, `record_plan_outcome`, `budget_escalations`,
  `record_budget_escalation`, `_summarise_plan_outcomes`).

### Runbook registry is the DEFAULT path for test/build/deploy/smoke — 2026-09-14

- **Goal / user value:** `procedures.py` already held the runbooks (status
  VERIFIED/STALE/BROKEN, dependency-fingerprint caching, one-line output),
  but it could only be reached by a caller who already knew a procedure
  id — so a worker that had not been *told* about the registry simply
  re-derived the command, which is the exact cost the registry exists to
  remove. Now the word a worker would use anyway (`test`, `build`,
  `deploy`, `smoke`, `health`) IS the address, and the registry populates
  itself on the way through.
- **Status:** VERIFIED (unit + contract tests; the repo's own
  `scripts/agent/*` runbooks resolve through it).
- **Scope / flow:** `procedures.Operation`/`OPERATIONS` name the five
  operations and their spellings (`tests`, `pytest`, `regression`,
  `restart`, `healthcheck`, …; `resolve_operation` is case- and
  hyphen-insensitive). `ProcedureRegistry.run_operation(target)` is the
  entry point and accepts an operation name OR a registered procedure id:
  it resolves, calls `ensure_operations()` if nothing serves that
  operation yet, then runs. `ensure_operations()` is idempotent and
  ordered so nothing can drift: (1) an already-registered id wins outright
  (a declared `depends_on`/`risk` is never overwritten by discovery), (2)
  then what the repository ALREADY runs (`make test`, `scripts/ci.sh`, …
  via `discover_existing`), (3) only then the conventional
  `scripts/agent/<op>.sh`. **Nothing is generated** — an operation the
  repository cannot perform is reported `absent`, never filled with an
  invented script. Auto-registered test/build procedures get a default
  `depends_on` (top-level source/test trees, capped at 8); a discovered
  `deploy_preview`/`deploy_staging` keeps its own risk level rather than
  the operation's production default.
- **Output contract (the token-economy half):** `ProcedureResult`
  gained `operation`, `status` (the registry's verdict BEFORE the run) and
  `inspect`. `as_context()` is what callers spend context on: a PASS is
  one line plus a log path and **nothing else**; a FAIL adds stage, exit
  code, the failing region and `inspect {script, command, log_path, why}`.
  The script path is therefore returned only on failure — the "inspect
  only when it fails" rule expressed as data, not as an instruction to
  remember. A VERIFIED+fresh result is reused (`from_cache`), a STALE one
  is re-run rather than read, and a BROKEN one names the vanished script
  without running anything.
- **Two correctness fixes shipped with it:** (a) a procedure with an
  EMPTY `depends_on` was keyed on the commit alone, so an uncommitted edit
  left the fingerprint identical and a stale PASS could be reused; it is
  now keyed on the working tree's dirty state (`git status --porcelain`
  plus each listed path's mtime/size, with the registry's own
  `.projectflow/` bookkeeping excluded so a recorded run cannot invalidate
  the next one). (b) `_script_exists` passed a relative script path to
  `shutil.which`, which resolves against the SERVER's working directory —
  a deleted script looked present whenever a same-named path existed under
  wherever the process happened to run. A relative path is now only ever
  resolved against the repository.
- **UI route/screen:** unchanged — `/dashboard/api/procedures` still
  lists, never runs, and shows the new registrations like any other.
- **API/tool/command:** `work_procedures` unchanged in name and count (no
  new MCP tool). Naming a target now implies `action="run"`
  (`work_procedures(procedure_id="test")`), an empty call still lists, and
  `list` also reports the operation names. New `action="ensure"` registers
  without running anything. The run path returns `as_context()`.
  `work_planning.plan()` calls `ensure_operations()` before its reuse
  stage, so "is there already a way to do this?" is asked against a
  populated registry (failure there is a recorded gap, not a planning
  error). `task_classifier`'s gate step and `AGENT_KNOWLEDGE_POLICY` now
  name the call instead of describing it.
- **Config/permission:** none new. Risk policy is unchanged and still
  binding: `deploy` resolves to `deploy_restart` (risk `production`) and
  is REFUSED with stage `policy` unless the caller passes `allow_risky` —
  routing a deploy through the registry made it repeatable, never
  automatic.
- **Data/schema/migration:** none. `PROCEDURE_STATE.json` keeps
  `schema_version` 1; auto-registered entries are ordinary rows with
  `source: "discovered"`, and run evidence stays machine-local as before.
- **Acceptance/tests/evidence:** `tests/test_procedures.py` — new "the
  registry as the DEFAULT path" section: conventional scripts register
  themselves, a repo-native `make test` beats them, a declared procedure
  is never overwritten, re-ensuring keeps the green result, five spellings
  of "test" all resolve, a pass hands back one line and no script, a
  failure names script+log+region, STALE is re-run not read, VERIFIED is
  reused, BROKEN names the vanished script, deploy is routed but still
  refused without approval, an undeclared dependency set does not cache
  across an edit, and a relative script is looked for in the repository
  rather than the caller's directory. `tests/test_work_surfaces.py` pins
  the MCP routing (target implies run; `run_operation`; `as_context()` not
  `as_dict()`).
- **Known limitations:** the working-tree fallback keys on `git status`
  output, so an untracked DIRECTORY is tracked by its own mtime rather
  than per-file; discovery still recognises only the entry points in
  `DISCOVERY_RULES` (make/just/`scripts/*.sh`), so a project that runs its
  tests some other way registers nothing and is told so rather than
  guessed at; `build` has no runbook in THIS repository and is correctly
  reported absent.
- **Dependencies:** Project Knowledge (the registry is stored beside the
  knowledge map and uses its lock), Work Policy §"Procedural Memory and
  Runbook Registry" (unchanged — it already carried the rule; this change
  supplies the mechanism).
- **Follow-up/backlog:** a `build` runbook for this repo if one is ever
  wanted; teaching `discover_existing` about `npm`/`cargo`/`go` entry
  points for non-Python projects.
- **Trace:** `terminal_mcp/procedures.py`, `terminal_mcp/mcp_app.py`
  (`work_procedures`), `terminal_mcp/work_planning.py`,
  `terminal_mcp/task_classifier.py`; see this file's own commit.

### The knowledge map is LOADED at task start, not merely present — 2026-09-14

- **Goal / user value:** `project_knowledge.py` (the map), its per-module
  confidence and its freshness rules all existed and were tested, and the
  planning path used them only to pick module NAMES. A name is not a
  briefing: the worker still opened the repository and re-derived what the
  map already said. This is the wiring that makes the map pay for itself —
  the modules a spec names are loaded at the moment the task starts and
  travel inside the worker's own handoff.
- **Status: VERIFIED.** `tests/test_project_knowledge.py` (46),
  `tests/test_context_pack.py` (34), `tests/test_work_planning.py` (22) and
  `tests/test_dogfood_work_v1.py` (11, including the opt-in strict pass with
  `TERMINAL_MCP_DOGFOOD_STRICT=1`) — all against real `git init`
  repositories and this repo's own map. No mocked git: what a freshness
  claim is worth depends entirely on what `git diff --name-only` and
  `git status --porcelain` actually report.
- **`ProjectKnowledge.rebuild()` — incremental re-verification, driven by
  the git delta.** For each module it asks git one question: has anything
  under this module's paths moved between the commit it was verified at and
  HEAD? If nothing has, and the working tree is clean under it, the entry is
  as true now as when it was written, so its `last_verified_commit` is
  advanced and it stops reporting MEDIUM — which is precisely the value that
  sends a worker off to re-read code that did not change. If something did
  move, it is **never** advanced: it is returned in `needs_review` with the
  path that moved, which is the only thing a re-index has to look at.
  - It **re-verifies; it never re-derives.** No summary is rewritten. Only a
    reader can say whether prose is still true, and a machine that rewrote it
    would be inventing the one thing this map may not invent.
  - An advanced entry records `last_refreshed_at` and says so in its own
    confidence reason, so "a reader checked this" and "git proved nothing
    moved" cannot wear each other's name.
  - A module naming a path that no longer exists, or with uncommitted edits
    under it, is never advanced — the working tree outranks the commit graph.
  - The map as a whole is marked indexed at HEAD only when **every** module
    is. `rebuild(modules=[...])` re-verifies a named subset and reports any
    name the map does not have.
  - Degrades rather than raises: no HEAD, no map, or an unreadable working
    tree are each reported as themselves. It runs on the planning path, and
    a refresh that failed must not take a plan down with it.
- **Loading only what the spec names.** `ProjectKnowledge.module_state()`
  and `load_modules()` answer about named modules without listing the map —
  `module_states()` runs a `git diff` per module, so answering "what do we
  know about the two modules this task names" by walking every module pays
  for each one nobody asked about. `context_pack.load_task_knowledge(spec)`
  builds the briefing from `likely_module` + `relevant_modules`, best first,
  deduplicated and **capped at 3** (`MAX_TASK_MODULES`): a briefing that
  grows with the project is the repository again under another name. It is
  duck-typed across `WorkSpec` and `BugSpec` — a briefing that worked for
  only one of them would be absent exactly half the time.
- **Honest about what it did not load.** A module the spec names and the map
  has never indexed is reported in `unknown` and named in the rendered
  briefing, because that is exactly where a worker DOES have to read code.
  No map, no module named, and a map that raised are three different
  situations and each is reported as itself. The briefing takes the
  **weakest** loaded module's confidence, never the best one's: a worker acts
  on the whole briefing, not on its strongest part.
- **Where it reaches the worker.** The rendered briefing is persisted on the
  spec (`knowledge_brief`, `knowledge_modules`, alongside the existing
  `knowledge_confidence`/`knowledge_last_verified_commit`) and
  `WorkSpec.handoff()` emits it as `KNOWLEDGE` with the commit it was
  verified at. On the SPEC rather than on the planning result, because the
  spec is what is read back later: a redefine, a restart or a second worker
  each build a handoff from it, and a result-only attachment would leave all
  of those empty. It is a snapshot with its commit recorded beside it, never
  a claim about the repository as it stands now — and the payload repeats the
  rule the map is subordinate to: the map says where to look, the current
  code is the truth.
- **Counters.** `knowledge_hits` (modules that MATCHED, unchanged meaning)
  is now joined by `knowledge_modules_loaded` and `knowledge_refreshed`.
  Collapsing the first two would let a match that briefed nobody be counted
  as a saving. The telemetry `knowledge_hits`/`context_pack_hits` signals
  stay where they were — at the retrieval call site, one per pack that
  actually carried a summary or files, so an empty entry is never counted as
  a hit.
- **Behaviour change worth knowing:** planning now WRITES to
  `.projectflow/knowledge/KNOWLEDGE_STATE.json` when re-verification can
  advance a module (and only then — nothing to advance means no write). The
  map is the shared canonical one (`canonical_root`), so a planner running in
  a worktree advances the main checkout's map under the existing knowledge
  lock. The test suite must not leave a diff in the repository it planned
  against, so `tests/conftest.py` snapshots the canonical state file once per
  session and restores it at the end (the dogfood fixture does the same per
  test) — several tests drive the real pipeline with no project path, and
  without this a plain `pytest` run left another lane's committed file dirty.
- **Proof that a fresh worker does not re-read the repo.**
  `test_a_fresh_worker_is_briefed_without_reading_the_module_source` watches
  every `Path.read_text` for the duration of a real plan against a real git
  repository, then reads the spec back out of the store as a worker would:
  the handoff carries where the code lives (`app/export.py`), what to look at
  (`render_csv`) and what is known to be wrong with it, and **no file under
  the module's own directory was opened**. It also asserts the watcher
  recorded reads at all, so the negative is a finding rather than a broken
  probe. The dogfood repeats the claim against this repository's own map.
- **No secrets, even from a hand-edited map.** The briefing now rides in the
  spec payload, which is long-lived and rarely re-read. `record_module`
  already refuses a credential at write time; if one is put into the state
  file by hand, `WorkSpecStore.save`'s own scrub refuses the spec and
  planning raises `SecretInKnowledge` rather than persisting it — refused,
  never silently stripped, and nothing reaches the store to be read back.
- **Known limitations.** Re-verification is path-level, not symbol-level: a
  commit that touches a module's file advances nothing even when the change
  cannot affect what the summary says — the conservative direction, since the
  cost of a needless re-read is smaller than the cost of a confident wrong
  map. Entry points and runbooks come from the pack only when the map records
  them. And the briefing rides in the spec payload, so a very large map entry
  is capped (`MAX_BRIEF_CHARS`) rather than paged.
- **Not touched, deliberately:** `mcp_app.py` and `dashboard.py`. The surface
  integration is the coordinator's to make centrally; everything here reaches
  a worker through data that already flows — `work_plan`'s result and the
  spec's own handoff — so no MCP or route change was needed.
- **Dependencies:** Project Knowledge, `context_pack` (the pack this briefing
  is assembled from), Work Spec (the handoff), `work_telemetry_runtime` (the
  hit counters).
- **Follow-up/backlog:** entry points and past-bug history in the briefing
  need a `BugSpecStore` on the planning path, which `work_planning.plan()`
  does not take yet; symbol-level re-verification.
- **Trace:** `terminal_mcp/project_knowledge.py` (`rebuild`, `module_state`,
  `load_modules`, `ModuleState.last_refreshed_at`),
  `terminal_mcp/context_pack.py` (`TaskKnowledge`, `load_task_knowledge`,
  `spec_modules`), `terminal_mcp/work_planning.py` (the knowledge stage),
  `terminal_mcp/work_spec.py` (`knowledge_brief`, `handoff`).

### Retrieval before investigation — a planner claim now briefs itself (2026-09-14)

- **Goal / user value:** make the SECOND bug in a module cost less than the
  first. `context_pack.retrieval_result` (has this been seen before?) and
  `context_pack.build_context_pack` (the bounded briefing for one module)
  were implemented and tested, and **nothing called either of them**. Built
  but unwired is worse than absent: the capability reads as done while every
  planner still starts from an empty repository, and the audit that finds it
  has to be run twice — once to notice the code exists, once to notice it is
  unreachable.
- **Status: VERIFIED.** `tests/test_context_pack.py` (24),
  `tests/test_work_inbox.py` (43) and `tests/test_project_knowledge.py` (34),
  all against real `git init` repositories, a real `git worktree`, a real
  `BugSpecStore` and a real built MCP server. No mocked `git`: what a path
  check is worth depends entirely on what `git diff --name-only` and
  `git status --porcelain` actually report, and a mock of them proves nothing.
- **Where it runs, and why there:** inside
  `work_inbox.InboxService.claim_for_planning` — the last moment before a
  planner starts looking. Retrieval that runs afterwards has already let the
  cost it exists to avoid be paid in full, so it cannot be left to a planner
  to remember to ask for. The claim reply gains two keys beside `issue`:
  - `retrieval` — `REUSED_BUG_SPEC` (start from that spec's root cause and
    fix strategy), `RELATED_BUGS_FOUND` (read them, assume nothing),
    `NO_SIMILAR_BUG`, or one of `RETRIEVAL_UNAVAILABLE` / `RETRIEVAL_FAILED`.
  - `context_pack` — the bounded module briefing (purpose, files, entry
    points, runbooks, known issues, past bugs, and what it does **not**
    cover), built for `issue.likely_module` or, failing that, for the module
    the best match names.
- **Every path a reused spec names is verified BEFORE it is offered.**
  `context_pack.verify_reused_paths` gives each path its own verdict against
  the working tree and the git delta from the spec's `source_commit`:
  `UNCHANGED`, `CHANGED` (rewritten since — including an uncommitted edit,
  because the working tree outranks the commit graph), `MISSING` (gone), or
  `UNVERIFIED`. A path that cannot be checked is **never** reported
  `UNCHANGED`: "I checked and nothing moved" and "I could not check" demand
  opposite next steps. The paths that moved are named in the guidance text a
  worker actually reads, not only in a structure it might.
- **The downgrade rule.** A strong match whose *every* named path is gone is
  returned as `RELATED_BUGS_FOUND` with `downgraded_from: REUSED_BUG_SPEC`,
  and without a `reused_bug_id`. The symptom really did match, so the root
  cause is worth reading — but a fix strategy for code that no longer exists
  points at nothing, and offering it as a starting point would spend the
  saving reuse exists to produce.
- **Which repository gets asked.** `project_knowledge.worktree_root()` (new,
  `git rev-parse --show-toplevel`), deliberately not `canonical_root()`.
  Canonical root resolves every worktree to the one shared map, which is
  right for a map that gets *written*; it is wrong here, because a worker in
  a worktree edits that worktree, and the main checkout would report files
  the worker has already rewritten as untouched — exactly the false
  confidence this check exists to prevent. Found by a real smoke run against
  this repo's own worktree, where it reported precisely that.
- **Stored vs returned.** The issue row keeps `retrieval_status` plus a
  compact record (statuses, match ids and scores, per-path verdicts, gaps);
  the root causes, fix strategies and file lists travel to the claiming
  planner once. Writing them into the row as well would move the same
  paragraphs twice in a feature whose whole purpose is to move fewer of them.
  The status is kept on the issue, not only in the reply, so an expired lease
  does not lose what the last planner was told.
- **Degrades, never fails.** `spec_store` and `knowledge` are both optional on
  `InboxService`. Without a spec store the claim still succeeds and says
  `RETRIEVAL_UNAVAILABLE` — "nobody searched" is not "nothing was found".
  Without a repository the paths come back `UNVERIFIED` with the reason. A
  raised lookup is caught and reported as `RETRIEVAL_FAILED`: a claim that
  failed because the history could not be read would block real work over a
  lookup.
- **Two things it deliberately does not do.** (a) The throwaway spec built to
  *ask* the question is never saved — persisting it would put an unanswered
  query into the very history the next query reads. (b) The matched spec's
  module is **not** written back onto the issue: a module inferred from a
  fuzzy match would score the next retrieval higher for no new evidence, and
  the system would grow confident by talking to itself.
- **Known limitation (by design, not omission).** A captured issue carries no
  module until something triages it, and without a module no past bug can
  score above `STRONG_MATCH` — so a raw `NEW` issue gets `RELATED_BUGS_FOUND`
  at best. That is the threshold refusing to claim more than the evidence
  supports. Set `likely_module` on the issue (a `work_inbox_transition` field)
  and the same claim reaches `REUSED_BUG_SPEC`.
- **Trace:** `terminal_mcp/context_pack.py` (`named_paths`,
  `verify_reused_paths`, `retrieval_result`), `terminal_mcp/work_inbox.py`
  (`InboxService.brief_for_planning`, `_attach_briefing`,
  `claim_for_planning`, `Issue.retrieval_status`),
  `terminal_mcp/project_knowledge.py` (`worktree_root`),
  `terminal_mcp/mcp_app.py` (`work_inbox_claim`, `work_inbox_list`).

### Read-only repository access for external agents (`repo_*` MCP tools) — V1, 2026-09-14

- **Goal / user value:** let an external agent reaching this server over
  MCP (ChatGPT, specifically) read Git and source **directly**. Before
  this, it could not: reading a file meant asking a Claude session to open
  it and paste the content back, which is slow, lossy, and puts a second
  agent's paraphrase between the reader and the code. Nothing in the
  previous 240-tool surface returned file content or a diff at all.
- **Status: VERIFIED (V1, READ-ONLY).** 97 tests across
  `tests/test_repo_read.py` (66), `tests/test_repo_read_node.py` (26) and
  `tests/test_repo_read_mcp_tools.py` (7)*, all against real `git init`
  repositories, a real node-agent ASGI app and a real built MCPServer —
  no mocked `git`, because the containment/secret/truncation rules ARE the
  product and a mock of `git` proves nothing about what `git grep`'s
  pathspec handling or `Path.resolve()`'s symlink following actually do.
  (*counts as of this commit; the pinned sets, not the totals, are what
  the tests assert.)
- **Scope — the ten operations:** `repo_status` (branch/HEAD/dirty/
  ahead-behind/project identity), `repo_head`, `repo_branches`,
  `repo_remotes`, `repo_tree`, `repo_read`, `repo_search`, `repo_diff`,
  `repo_log`, `repo_show_commit`. Declared ONCE in
  `repo_read.OPERATIONS`, which the MCP tools, the node endpoint and
  `LocalNodeClient` all dispatch through — so local and remote cannot
  drift, and an operation absent from that table does not exist anywhere.
- **Read-only, structurally rather than by convention.** Three
  independent locks, each pinned by a test:
  1. `repo_read.READ_ONLY_GIT_SUBCOMMANDS` — `_run_git` RAISES on any
     subcommand outside it. `checkout`/`switch`/`reset`/`clean`/`commit`/
     `push`/`fetch`/`pull`/`merge`/`rebase`/`stash`/`apply`/`add`/`rm`/
     `config`/`worktree`/`update-ref` are all absent, so a later careless
     edit inside this module fails loudly instead of shipping a write.
  2. No tool, endpoint or client method accepts a git subcommand, a shell
     string or an argv list. There is no arbitrary-exec path to widen.
  3. The node endpoint is **GET-only** (`POST` → 405) and dispatches only
     through the operation table.
  `tests/test_repo_read_mcp_tools.py` additionally asserts that no
  `repo_*` name beyond the ten reads is registered — adding a write
  capability later requires editing that assertion on purpose.
- **Security boundary, in the order a request meets it:**
  1. `repo_read.enabled` (config gate).
  2. `allowed_roots` — absolute-path allowlist; symlinks resolved BEFORE
     containment is checked (`lifecycle.resolve_cwd`'s own rule), so a
     symlink inside an allowed root pointing outside it is refused.
  3. **The repo ROOT is checked too, not just the requested path.** Without
     this, allowing only `<repo>/src` would expose the whole repository
     through relative paths — a real hole, covered by its own test.
  4. Every in-repo path is re-resolved and re-contained (PATH_OUTSIDE_REPO),
     which is what stops `../..` and an absolute path outside the repo.
  5. Secret paths denied by name/glob BEFORE any byte is read
     (SECRET_PATH_DENIED), seeded from the list this project already
     maintains (`redaction.CREDENTIAL_FILE_NAMES`) and extended with
     `.env*`/`*.pem`/`*.key`/`id_*`/`.git-credentials`/`.npmrc`/
     `*credentials*`/`node-agent.env`/... plus never-descended directories
     (`.git`, `.ssh`, `.gnupg`, `.aws`, `.terminal-mcp`). `.git` is denied
     for a concrete reason: `.git/config` can hold a tokened remote URL,
     and `.git/objects` would let a caller reconstruct any file the path
     rules just denied.
     - The denial covers every route a secret could take out: `repo_read`
       refuses it, `repo_search` DROPS hits inside it (and names the file
       in `secret_paths_skipped`, so the omission is visible rather than
       silent — otherwise a secret is readable one grep line at a time),
       `repo_diff`/`repo_show_commit` EXCLUDE its hunks
       (`secret_paths_excluded`), and `repo_log --file` refuses it.
     - A denied path answers the same whether or not it exists — otherwise
       the error itself is an oracle for "does this box have an
       `id_ed25519`".
     - A denied path is still LISTED by `repo_tree`, flagged
       `"denied": true`: hiding it would make an agent conclude the file is
       absent. Same posture `redaction.CREDENTIAL_FILE_NAMES` already
       documents ("the PATH stays visible").
  6. Everything that survives is run through `redaction.redact_output`
     anyway — a token pasted into a README or a password in a
     `config.sample.yaml` is caught by no path rule. The returned
     `redaction` report carries counts and rule NAMES only, never a matched
     value, so it is safe to return and to log.
  7. Caps on every axis (bytes/lines/results/tree entries/log entries/diff
     bytes/timeout), and **a caller cannot argue past a configured cap** —
     `max_bytes=10_000_000` against a 2 KB policy still yields 2 KB.
     Truncation is always REPORTED, never silent, and a byte-truncated read
     is cut back to the last whole line so quoted line numbers stay
     trustworthy. `repo_read` reports it as TWO distinct fields, which is a
     correctness matter and not cosmetic: `has_more` means the file
     continues past the returned window (the field to page on), while
     `truncated` means a cap interfered — the line limit narrowed the
     caller's own window, or the byte cap cut it short. With one flag, a
     fully-satisfied window of lines 2-2 in a 500-line file reports
     truncated=True and a caller paging on it never terminates.
  8. Argument safety: refs match a strict pattern and may not begin with
     `-`; pathspecs may not begin with `-` or contain `..`; pathspecs are
     always passed after `--`. So `--upload-pack=...` or
     `--output=/etc/passwd` is a rejected value, never a flag.
  9. Every git call runs with `GIT_TERMINAL_PROMPT=0`/`GIT_ASKPASS=`/
     `SSH_ASKPASS=` (a read can never block on a credential prompt — which
     is what makes GIT_AUTH_REQUIRED a fast clean answer instead of a
     stall) and `GIT_OPTIONAL_LOCKS=0` (a read never takes the index lock,
     so it cannot interfere with a session actually working in the repo).
- **Node-aware — reads happen where the repo lives.** The same lesson
  `coordinator.node_aware_repo_evidence` learned for metadata, applied to
  content: a repo at `C:\Users\tranv\project` or `/home/dell/workspace/x`
  does not exist on the controller, and running git against that path
  locally produces an answer about a path that means nothing here.
  `repo_service.RepoService` LOCATES first and executes second:
  `node`+`path` (explicit), `session` (via the controller's own
  session→node resolution plus that node's registry record — works for a
  remote session whose cwd the controller cannot stat), `project` (a
  `project_identity` id, via the checkouts the fleet already reports), or
  `path` alone (local). Every response carries `node_id` and `located_by`,
  so an answer that came from the wrong machine is impossible to mistake
  for the right one.
  - A project with checkouts on two nodes returns **AMBIGUOUS_REPO with the
    candidates listed, never a guess** — seven checkouts of this very repo
    across three nodes is the real state of this fleet.
  - "Could not look" is never conflated with "the repo said no":
    NODE_UNREACHABLE (transport/timeout/404) and NODE_LACKS_REPO_READ (an
    agent predating the endpoint) are distinct from every path/secret
    refusal, which arrive as a 200 carrying an error code.
  - Each node enforces its OWN allowlist over its own paths. That is the
    correct owner of the decision, and it means a controller cannot talk a
    node into reading something the node's operator did not allow.
- **Git auth (V1): a local repo never needs it.** No read path touches the
  network — `repo_remotes(check_auth=False)`, the default, is pure local
  config. `check_auth=True` opt-in probes read access with `ls-remote` and
  reports GIT_AUTH_REQUIRED **inside `auth`**, not as a top-level error,
  because the remotes themselves were read fine and a caller that only
  wanted the URL must not be failed because the network was down. Remote
  URLs are normalised through `project_identity.normalise_git_remote`
  (which already strips credentials) and redacted; a failed probe's stderr
  is redacted too, since it can echo a tokened URL.
  **Audited on the controller host, 2026-09-14:** `origin` is
  `https://github.com/hungtranbkit/terminal-mcp.git`, anonymous HTTPS read
  works (`ls-remote` exit 0, no credential helper configured), so **no SSH
  key and no GitHub Deploy Key was needed or created.** Nothing was written
  to `~/.ssh`, no key material exists in this repo or its config.
- **Audit:** every invocation records `repo_<op>` + node + outcome +
  latency through the EXISTING `AuditStore` (never a second database).
  It deliberately records **no content** — no file text, no patch, no
  matched search line, no redaction sample, and `text` is left unset so
  `AuditStore` does not fingerprint/preview it. This log is the one an
  operator greps freely; a read-audit that stored the secret would defeat
  the denial it is recording. Pinned by a test that asserts the secret
  value is absent from the serialized rows.
- **Config:** new `repo_read` section (`config.RepoReadConfig`) — `enabled`
  (default **true**), `allowed_roots`, the six caps, `timeout_seconds`,
  `extra_secret_globs`. `enabled` defaults ON, unlike
  `session_lifecycle`/`work`, and the difference is deliberate: those gates
  guard capabilities that CREATE something (a real process, an
  auto-dispatched prompt), while this one only reads and has no write
  primitive to lose control of. The real boundary is `allowed_roots`, not
  the flag. `allowed_roots` empty (the default) reuses
  `session_lifecycle.allowed_cwd_roots` — the allowlist this deployment has
  already curated — falling back to the server's home directory, never to
  `/`. `extra_secret_globs` is **additive only**: there is no config key
  that removes a built-in secret glob, so no config edit can reopen a path
  the code closed. An out-of-range limit is a load-time ValueError, not a
  silent clamp — a caps section nobody can trust is worse than no caps.
- **Limitations / explicitly NOT in V1:**
  - No write capability of any kind (that is the point, not a gap).
  - No `fetch`/`clone`/`pull`, so a private remote this host cannot already
    read is out of scope; an already-cloned repo is fully readable.
  - `repo_search` uses `git grep`, so it searches tracked + untracked files
    in a work tree, not arbitrary history. Searching history would need
    `log -S`/`grep <rev>` and is not exposed.
  - Binary files are refused (BINARY_FILE), never returned.
  - `session`-based location depends on the owning node's registry record
    having `repo_root`/`cwd`; a record predating project-info backfill
    falls back to `cwd`, and a session in no repo answers
    SESSION_NOT_IN_A_REPO.
  - Reading a repo on a remote node requires that node's agent to be
    running THIS version (`/v1/repo/{op}`); an older agent answers
    NODE_LACKS_REPO_READ. Rolling the new agent out to dell-5530/hp is a
    separate, explicitly-approved deployment step — see §Backlog.
- **Trace:** `terminal_mcp/repo_read.py` (engine + operation table),
  `terminal_mcp/repo_service.py` (node-aware routing + audit),
  `terminal_mcp/node_agent.py` (`GET /v1/repo/{op}`),
  `terminal_mcp/node_client.py` (`repo_op` on the protocol,
  `LocalNodeClient`, `RemoteNodeClient`), `terminal_mcp/config.py`
  (`RepoReadConfig`, `_load_repo_read_config`), `terminal_mcp/mcp_app.py`
  (the ten tools), `tests/test_repo_read*.py`, `tests/test_server.py` (the
  pinned tool set).

### `/v1/repo-evidence` had never worked — NameError on every request (fixed 2026-09-14)

- **Found while** adding the content-bearing `/v1/repo/{op}` sibling
  endpoint next to it.
- **The bug:** the handler called `resolve_cwd(cwd, config)`, but `config`
  is not defined anywhere in `build_node_agent`'s scope (`node_agent.py`
  imports `load_config`, the FUNCTION, and the only `config` binding in the
  module is a local inside `main()`). So **every single request to
  `/v1/repo-evidence` raised NameError and answered HTTP 500**, from the
  day the endpoint was written (commit `10d2566`, "Collect pre-dispatch
  repo evidence where the session actually lives").
- **Blast radius:** the Coordinator's pre-dispatch gate reads repo evidence
  for a session through `node_aware_repo_evidence`, which reports any
  non-200 as `RepoEvidenceUnavailable` — "we could not look" — and fails
  closed. So **nothing was ever wrong-but-believed**: no dispatch decision
  was made on bad evidence. The capability was simply never working, and
  silently: every remote-node dispatch fell back to the
  unavailable/NEEDS_HUMAN path (or an `allow_unverified_repo` waiver)
  instead of getting real evidence. The fail-closed design is exactly what
  kept this from becoming an incident, and is also what hid it.
- **Why it survived:** `tests/test_coordinator_remote_repo_evidence.py`
  covers the collector and the client protocol thoroughly, but with a stub
  client — nothing ever exercised the ROUTE end-to-end through a real ASGI
  app. The unit tests were green the whole time.
- **Fix:** use `terminal.config` (the node's own config, genuinely in
  scope). One-line change; the endpoint's logic was otherwise correct.
- **Regression cover:** `tests/test_repo_read_node.py` —
  `test_repo_evidence_endpoint_actually_answers` (real request, real repo,
  asserts branch/HEAD/clean), `test_repo_evidence_endpoint_enforces_the_
  cwd_allowlist` (403 PATH_NOT_ALLOWED), and
  `test_repo_evidence_reaches_the_controller_gate_through_the_node_client`
  (the full collector → client → HTTP → node → git chain the gate really
  uses).
- **Deployment note:** the fix only takes effect on a node once THAT node's
  agent is restarted on this version. Until then, remote repo evidence
  keeps behaving as it has: unavailable, fail-closed.

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

### Windows detached session hosts (survive node-agent restart)

- **Goal / user value:** a Windows node-agent update or restart must stop
  being an outage for that node's Claude/Codex/PowerShell sessions. The
  node-agent becomes control plane only; a session's process is no longer
  a child whose lifetime depends on it.
- **Status:** IMPLEMENTED and TESTED on Linux against real processes; NOT
  YET PROVEN on real Windows, and NOT deployed. `--detached-sessions` is
  OFF by default so shipping the code cannot change a running node's
  behaviour. The live dell-5530 agent has not been restarted or touched.
- **Design of record:** `docs/WINDOWS_SESSION_HOST.md` (architecture,
  crash/reboot matrix, the adoption-impossibility proof, and the
  zero-loss rollout).
- **Root cause this fixes (from "Windows node-agent restart safety
  (Phase 0)" above):** a session's ConPTY child was spawned inside the
  node-agent process and the only registry was an in-memory dict, so the
  child died with the agent (measured live, both via `taskkill /F` and
  via the graceful `/v1/internal/shutdown`) and no on-disk state existed
  to reconnect with. Phase 0's "NOT achievable" conclusion was scoped to
  that architecture; this entry replaces the architecture.
- **Fix:**
  - `windows_session_host.py` (new): a detached per-session HOST process
    that owns the PTY. Atomic `meta.json` (temp + fsync + `os.replace`,
    its mtime doubling as the heartbeat), append-only `out.log`/`in.log`
    offset spools, `ctl.json` control requests consumed exactly once,
    `host.log`. Liveness is `ALIVE`/`GONE`/`PID_REUSED` — a live PID with
    a stale heartbeat is treated as reuse, never as a session, because
    Windows recycles PIDs and the cost of a false ALIVE is writing an
    operator's keystrokes into a void while reporting health. POSIX
    liveness additionally rejects zombies, since `kill(pid, 0)` succeeds
    on an unreaped process and Windows has no such state.
  - Spawned with `DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB |
    CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`; breakaway denial raises
    `BreakawayDenied` rather than silently retrying without the flag (a
    host spawned without it looks identical and then dies with the agent
    — the exact bug being fixed).
  - `windows_detached.py` (new): `HostProcessProxy`, a `PtyProcessLike`
    over the spool, so the existing reader thread, pyte VT parser,
    history buffer, resize and kill paths are untouched. `pid` is the
    CHILD's pid (the backend feeds it to `_win32_foreground_command`;
    the host's pid there would make every session report the wrapper);
    `read()` sleeps briefly on an empty spool so `_reader_loop` does not
    spin. `adopt_sessions()` is a pure read returning a verdict per
    session.
  - `windows_backend.py`: new optional `session_process_factory`
    (`(name, argv, cwd) -> PtyProcessLike`) alongside the unchanged
    2-arg `ProcessFactory`, and `adopt_detached_sessions()` /
    `_register_adopted()`. The duplicate-name check is re-done inside the
    registry lock, so a create racing an adoption cannot produce two
    hosts behind one id.
  - `windows_agent.py`: `--detached-sessions` (default OFF) and
    `--session-state-root`; adoption runs BEFORE serving, so a request
    arriving early cannot be told a live session does not exist and then
    have a create spawn a second host for it.
- **Scope / flow:** Windows nodes only, opt-in. Linux/tmux is untouched
  (asserted by test). Sessions created before the flag is enabled are
  NOT migratable — see the impossibility proof below.
- **Adoption impossibility (proved, as the task required):** a 0.12.0
  session's `HPCON` and pipe handles live in the running agent's handle
  table; Windows exposes no way to enumerate or re-open a pseudoconsole
  from another process, and `DuplicateHandle` needs a cooperating source
  that 0.12.0 does not contain. Even a duplicated handle would not help:
  when the owning process exits, ConPTY signals the client and
  `conhost.exe` tears the console down (measured in Phase 0). And the old
  agent must exit to be replaced (the code, and port 8790's single
  listener). Therefore every path that ends with the 0.12.0 agent gone
  ends with its sessions gone; no bridge exists.
- **Rollout (zero-loss, not yet executed):** Option A = deploy files
  only, capture each live session's scrollback + cwd + `--session-id`,
  quiesce, prove survival on a disposable second agent first, restart
  once with the flag, recreate with `--resume`, then restart again to
  demonstrate the property. Option B (recommended, loses nothing and
  waits for nothing) = run a second agent side by side on another port
  with its own state root and node id, prove it there, put new work on
  it, and retire the old agent only when its sessions are finished.
- **API/tool/command:** no new MCP tools. New agent CLI flags
  `--detached-sessions`, `--session-state-root`; new host entry point
  `python -m terminal_mcp.windows_session_host`.
- **Config/permission:** none new. `TERMINAL_MCP_SESSION_PTY_FACTORY`
  exists so the host entry point itself can be exercised on Linux CI; it
  is unset in production, which selects the real pywinpty path.
- **Data/schema/migration:** no DB change. New on-disk per-session state
  directory; no migration, since existing sessions cannot be adopted.
- **Acceptance/tests/evidence:** `tests/test_windows_session_host.py`
  (52 tests: atomic metadata, corrupt-metadata-as-absent, zombie and
  PID-reuse protection, spool rotation keeping the recent tail on a line
  boundary, control requests consumed once, the exact creation flags, and
  a spawner-death survival test verified by negative control — the same
  scenario without detachment freezes, with it keeps running);
  `tests/test_windows_detached_sessions.py` (19 tests: a real agent
  process SIGKILLed with its whole process group, both host and child
  confirmed still alive afterwards, pre-restart history readable and
  post-restart input answered, four concurrent sessions adopted with no
  crossed spools, host crash reported as an orphan and never adopted,
  stale-record-with-live-pid refused, claude.exe-style argv round-tripped
  verbatim including `--session-id`, backend create→restart→adopt→kill,
  double adoption producing no duplicate, and the plain Linux factory
  path unchanged). Existing `tests/test_windows_backend.py` (86) passes
  unchanged.
- **Known limitations:** ConPTY and the Win32 creation flags are NOT
  measured on Windows — asserted structurally and reviewed against the
  Win32 contract; rollout step 4 is what measures them, on a disposable
  session. A machine reboot loses every session (only `out.log` survives)
  and this is documented rather than papered over. Orphans are never
  auto-deleted, because a `PID_REUSED` verdict can also be a live host
  that was briefly slow to heartbeat.
- **Follow-up/backlog:** execute the rollout (Option B) on dell-5530;
  package `--detached-sessions` into `run-node-agent.ps1`'s Scheduled
  Task definition; surface adoption verdicts/orphans in the dashboard.
- **Trace:** branch `feat/windows-detached-sessions`.

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

### Work efficiency telemetry — runtime-driven, provenance-preserving

- **Goal / user value:** answer "what did this task actually cost, and is
  the fleet getting cheaper" from the runtime's own record, per task, per
  module and per time period — without any number in the answer having
  been invented.
- **Status:** IMPLEMENTED_NOT_LIVE_VERIFIED (2026-09-14). Code + unit/
  integration tests against a REAL `QueueStore` (real transitions, real
  event hook) are green; no live deployment has attached it yet, because
  the attach call belongs to the central MCP/dashboard wiring which this
  lane deliberately did not touch.
- **What changed (the contract):** telemetry rows used to exist ONLY when
  a worker called `work_telemetry_report` about its own finished task —
  a self-selected, retrospective sample. Rows are now opened and closed
  by the runtime itself:
  - `work_telemetry_runtime.py` (new) subscribes to `QueueStore`'s own
    post-commit event hook (`event_sink`, the same one `event_wiring.py`
    uses for the event bus — `attach()` composes with whatever sink is
    already installed and never displaces it).
  - `DISPATCHING` opens the row (or REOPENS the task's existing row —
    one row per task, so a retry stays one task rather than becoming two
    cheap-looking ones); `RUNNING` records that the agent started;
    `VERIFYING` is first preview; `COMPLETED`/`FAILED`/`BLOCKED`/
    `SKIPPED`/`CANCELLED` finish it.
  - A transition for a task this recorder never saw dispatched opens
    NOTHING: a row whose `started_at` is "whenever the process attached"
    would make every duration in the table wrong.
- **First preview is a stated DEFINITION, not a guess:** this runtime has
  no separate preview state, so the row records `preview_basis` — by
  default the transition into `VERIFYING`, the first moment a reviewable
  result exists. A caller with a truer signal (deploy preview URL,
  screenshot artifact) calls `RuntimeTelemetry.mark_preview(basis=...)`,
  and the basis is REQUIRED: a preview time nobody can trace to an event
  is a number nobody can check.
- **Per-task record:** `files_read`, `search_calls` (stored under the
  original field name `search_rounds`; both names are the same number in
  every payload), `runbook_hits`/`runbook_misses`, `knowledge_hits`,
  `context_pack_hits`, `similar_bug_hits`, `redefine_count` (read from
  the planner's own `WorkSpec.redefine_count`, never re-counted here),
  `time_to_preview_seconds`, `first_pass_success`, plus the dispatch
  bookkeeping (`dispatch_count`, `reopened`, `failed_excursions`).
  Attributes carried at open: task id, work id, module, execution mode,
  spec level, difficulty, lane — resolved from the `WorkSpec` bound to
  the queue task and the queue row's own metadata; a task with no spec
  gets NULLs, never a classified guess.
- **`first_pass_success` is three-valued** — True / False / unknown. A row
  whose dispatches were never observed (a worker-reported row) reports
  `None` with a stated basis, and aggregates exclude it from the rate
  rather than counting a gap in instrumentation as a failure.
- **Counters come from the real call sites:** `repo_read.repo_read` (one
  `files_read` per file actually read — a refused read is not a read),
  `repo_read.repo_search` (one `search_calls` per search CALL, whatever
  it found), `context_pack.py` (`context_pack_hits`/`knowledge_hits`
  when a pack was actually served, `similar_bug_hits` per past spec
  offered), `work_reuse.analyse` (knowledge/prior-spec candidates) and
  `procedures.run` (`runbook_hits` when a registered procedure was
  called, `+cache_hits` when its green result was reused instead of
  re-run, `runbook_misses` when there was no usable procedure to call).
  All report through `work_telemetry_runtime.note()`, which does nothing
  at all unless a recorder has been made active for a task
  (`observing(recorder, task_id)`). So an un-instrumented process
  behaves exactly as before, and a zero counter on a row with an empty
  `signal_sources` means "nobody reported", not "it never happened".
  Attaching the same recorder to a queue store twice is a no-op, so a
  double-wire cannot count two dispatches where there was one.
- **Provider usage counters — recorded only when reported:**
  `ProviderUsage` holds `input_tokens`/`output_tokens`/
  `cache_read_tokens`/`cache_write_tokens`/`total_tokens`, each present
  only because a runtime reported it, each carrying who reported it.
  Anything unreported is `UNAVAILABLE`, never `0` (zero is itself a
  measurement, and a false one). A value that is not a plain
  non-negative integer — including `True` — is IGNORED and named in
  `ignored`. A total nobody reported is `input + output` labelled
  `ESTIMATED` (the estimate label) with its derivation in `method`;
  exact arithmetic is still a derived figure. Usage is NOT back-filled
  from the local AI Usage Monitor: those figures are per-MACHINE daily
  quota and cannot be attributed to one task without inventing the
  attribution. This telemetry reads and writes no credential material of
  any kind.
- **Aggregation API:** `summarise(rows)`, `aggregate(rows, by=...)` over
  `task` / `module` / `work` / `project` / `lane` / `execution_mode` /
  `difficulty` / `spec_level` / `day` / `week` / `month`; rows that
  cannot be placed on the chosen axis are counted and named as
  `ungrouped`, never dropped into an "other" bucket that reads as a real
  group. `TelemetryStore.query()` filters by task/work/project/module and
  a HALF-OPEN `[since, until)` time window, so two adjacent windows can
  never double-count a task. `TelemetryStore.aggregate()`/`report()` are
  the read surfaces; `WorkService.telemetry_for_run(work_id)` gives one
  run's own rows.
- **Baseline and savings — shown only when admissible:** a `Baseline` is
  `MEASURED` (computed from real recorded rows, carrying its window and
  task count, with a `min_tasks` floor) or `STATED` (supplied WITH its
  definition — a definition-less one is refused outright), otherwise
  `UNAVAILABLE`. With no admissible baseline, `savings()` returns
  availability `False` and a reason and NO numbers at all. Every saving
  figure is labelled `ESTIMATED` with its derivation, because it is
  derived from two windows rather than measured.
- **API/tool/command:** no new MCP tool or route in this lane (see
  "Integration note" below). New Python surfaces:
  `work_telemetry_runtime.install(queue_store=..., telemetry_store=...,
  spec_store=...)` (the one call that wires it to a running queue),
  `attach`/`fan_out`/`observing`/`note`, `RuntimeTelemetry.note/
  record_provider_usage/mark_preview/row`, `WorkService.enable_telemetry()`
  and `WorkService.telemetry_for_run()`, `WorkSpecStore.by_queue_task()`,
  and in `work_telemetry.py`: `ProviderUsage`, `aggregate`, `period_key`,
  `Baseline`/`measure_baseline`/`stated_baseline`/`savings`,
  `TelemetryStore.for_task/query/aggregate/report`, and (2026-09-14)
  `TaskTelemetry.plan_status`/`plan_note`/`record_plan_outcome()`,
  `TaskTelemetry.budget_escalations`/`record_budget_escalation()`, plus
  `plan_outcomes` and `budget_escalations` in `summarise()` — and so, via
  `summarise`, in every `aggregate()` group as well. The PLAN_* vocabulary is
  IMPORTED from `bug_spec`, never restated, so the verdict on the spec and the
  verdict on the row are literally the same strings and can be compared. All
  additive: the row is a JSON payload, older rows read back with
  `plan_status=None`, and `None` is counted as `unreported` rather than as a
  confirmation. The existing
  `work_telemetry_report`/`work_telemetry` MCP tools are unchanged and
  still work: a worker adds what only it can see, on top of a lifecycle
  the runtime now records by itself.
- **Integration note (deliberate scope boundary):** `mcp_app.py` and
  `dashboard.py` were NOT touched — the coordinator integrates those
  surfaces centrally. Until that wiring calls `install(...)`, nothing is
  attached and behaviour is byte-identical to before; this is why the
  status above is not VERIFIED.
- **Storage:** same `work_telemetry.db` (SQLite/WAL/0700 state dir).
  Migration v2 is additive: indexes on `module` and `started_at` for the
  two aggregation axes. Older builds read the same rows.
- **Acceptance/tests/evidence:** `tests/test_work_telemetry_runtime.py`
  (34 — real `QueueStore`/`QueueService`, real transitions incl. the
  retry path `DISPATCHING→FAILED→QUEUED→DISPATCHING→…→COMPLETED`, real
  `ProjectKnowledge`/`ProcedureRegistry`/`WorkSpecStore`/`repo_read` over
  a real git repo; asserts the
  existing sink still receives every event, and that an exploding
  telemetry store cannot disturb a real transition),
  `tests/test_work_telemetry.py` (50, up from 23 — provenance,
  three-valued first-pass, grouping, half-open windows, baseline
  admissibility, derived-savings labelling), and
  `tests/test_dogfood_budget_gate.py` for the plan-verdict and
  budget-escalation fields, which are asserted where they are actually
  produced — in a real run — rather than only as setters.
- **Known limitations:** (1) not attached in any live deployment yet (see
  the integration note); (2) the counters only see reads that go through
  THIS process — a worker reading files with its own editor/agent tools
  is invisible here, which is why an empty `signal_sources` is recorded
  rather than a confident zero, and why the worker-reported path stays;
  (3) first preview means "a reviewable result exists", not "a human
  looked at it"; (4) provider usage depends entirely on a runtime that
  reports counters — with none, every token figure in every aggregate
  stays `UNAVAILABLE`, by design.

### Token-efficiency benchmark and acceptance — the measured answer (2026-09-14)

- **Goal / user value:** three capabilities shipped claiming the same
  benefit — a runbook registry, retrieval before investigation, and runtime
  telemetry — all asserting that a worker now carries less context and does
  less re-analysis. This is the attempt to find out whether that is true on
  this repository's own real bugs, built so a disappointing answer is as
  reportable as a flattering one.
- **Status: MEASURED. The headline verdict is FAIL** against the acceptance
  bar the benchmark itself pre-registered (`efficiency_benchmark.ACCEPTANCE`,
  published inside every result file). This is a real outcome, not a blocked
  task: the instrument works, the corpus is real, and the honest reading is
  below.
- **What was measured:** 20 cases — **18 REAL bugs recovered from this
  repository's git history** across ui / simple_logic / backend / auth /
  session / unknown, plus **2 SYNTHETIC** cases that say so in their own
  `origin` and `note` and are reported apart from the headline. Each real
  case carries its commit, its parent, the subject line as the symptom, and
  the source files its fix actually touched (tests and docs excluded — a
  benchmark about locating a defect must not be scored on finding the test
  that noticed it). Corpus: `benchmarks/tokeff_corpus.json`, regenerated by
  `scripts/benchmark/mine_corpus.py`.
- **Baseline methodology, stated before anything was measured** (and
  published in every report): no old model is re-run — git's record of each
  fix is better evidence than a re-enactment and costs nothing. The baseline
  is the UNASSISTED LOCATING SURFACE: source files matching terms derived
  mechanically (never hand-picked per case) from the bug's own words, found
  by really running `git grep` against the repository AS IT WAS at each
  fix's parent commit. The headline compares against the baseline's BEST
  case — the single most selective term, the hardest baseline to beat — not
  the union of every term tried.
- **The measured figures** (`docs/TOKEFF_BENCHMARK.md`,
  `benchmarks/tokeff_result.json`, regenerated by
  `scripts/benchmark/run_tokeff_benchmark.py`):
  - mean unassisted locating surface **5.17 files** (ESTIMATE — a proxy for
    what a worker would triage, not a count of files anyone read)
  - mean assisted surface **0.61 files** (REAL — what the shipped briefing
    actually named)
  - searches: **3.94 real greps per case** vs **0** (REAL both sides)
  - elapsed: 0.05s vs 0.07s (REAL — wall clock of the two retrieval paths.
    A worker's own elapsed time is UNAVAILABLE: nothing recorded it)
  - re-analysis depth, assisted: 3 REUSE_PRIOR / 3 READ_RELATED / 12
    FULL_ANALYSIS
  - provider usage delta: **UNAVAILABLE** — the host's real telemetry
    database was opened and asked, and no provider reported counters for any
    of these cases, which predate the telemetry runtime. No token saving is
    claimed anywhere in this work.
- **The honest reading, in order of importance:**
  1. **Coverage, not retrieval, is the binding constraint.** In 11 of 18
     real cases the knowledge map covers none of the files the fix touched,
     so no briefing built from it could have named the site. The map holds 9
     modules / 19 paths against ~150 source files.
  2. **Where the map does cover the fix site** (7 cases) the briefing named
     it in 5 of 7 and beat the luckiest single grep in 2 of 5 — reported
     with an **UNDERPOWERED** warning by the report generator itself, because
     seven cases can produce any percentage at all. A direction, not a result.
  3. **The assisted path depends on the report naming the module.** The
     synthetic mirror pair measures this directly: the same defect worded as
     its commit subject reached `work_ui` (score 0.483, SHRUNK); worded as a
     user would report it, nothing scored above the module-choice floor
     (0.025, MISSED). Every rate in the report, measured from commit
     subjects, is therefore optimistic about real bug reports.
  4. Search rounds move unconditionally (0 vs 3.94) — and that holds whether
     or not the briefing was useful, which is exactly why it is worth little
     on its own.
- **What stops this flattering itself:** a briefing that does not contain a
  real fix path is a MISS however small it was; cases neither path could
  locate are counted apart rather than as losses; the module is chosen from
  the symptom alone by the SHIPPED scorer at the SHIPPED threshold; prior
  bugs are seeded leave-one-out with a fresh spec database per case; every
  figure carries REAL / ESTIMATE / UNAVAILABLE; and the acceptance verdict
  is computed from the numbers with the token claim explicitly barred from
  influencing it.
- **Two instrument corrections made during development, neither of which
  improved the result** (both disclosed in the report's own methodology):
  (a) the harness chose a module by argmax with no confidence floor, so it
  "chose" modules on scores of 0.03 — measuring a decision procedure the
  system does not use; applying `work_reuse.MENTION_THRESHOLD` cost the
  assisted side cases it had been credited with. (b) It reused one spec
  database across cases, so a case could meet its own spec; each case now
  gets its own. That changed no number, verified by re-running both ways and
  diffing rather than assumed — the shipped matcher already refuses to match
  a spec against itself by id.
- **API/tool/command:** no MCP tool and no route — this is an offline
  measuring instrument. `terminal_mcp/efficiency_benchmark.py` (corpus,
  baseline, assisted measurement, aggregation, pre-registered acceptance,
  map provenance, derived conclusions, markdown rendering);
  `scripts/benchmark/mine_corpus.py`; `scripts/benchmark/run_tokeff_benchmark.py`;
  `scripts/knowledge/index_modules.py`.
  Nothing is deployed and nothing outside the given output paths is written;
  a telemetry database is never CREATED by the run, because creating an
  empty one and reading zero out of it would turn "nothing was recorded"
  into a measurement.
- **Acceptance/tests/evidence:** `tests/test_efficiency_benchmark.py` (41) —
  including the map's own guards (every package file claimed exactly once,
  every indexed path still exists, provenance recorded beside the
  measurement) —
  validates every REAL case against real git, asserts synthetic cases are
  marked, and pins the anti-flattery rules (a miss earns nothing, leave-one-
  out holds, a weak module match is no match, usage is UNAVAILABLE until a
  provider really reports it and REAL the moment one does, the token claim
  never decides the verdict, and the published artifacts describe the
  committed corpus).
- **Known limitations:** (1) the locating surface is a proxy for files read,
  not a measurement of them — labelled ESTIMATE everywhere it appears;
  (2) strata under ten cases are underpowered and flagged as such;
  (3) the runbook
  registry's own claim (calling a procedure instead of re-deriving a
  command, and a one-line PASS instead of a log) is NOT measured here — the
  four metrics this task names do not capture it, and inventing a weak
  number for it would have been worse than saying so (see Backlog);
  (4) the corpus is one repository's history, so nothing here generalises
  beyond it.

## Backlog (explicitly not done yet — tracked here so it isn't re-discovered)

0a. **Worktree Janitor — CONTRACT ONLY as of 2026-09-14. No executor exists;
   nothing deletes anything.** The specification is
   `docs/WORKTREE_JANITOR.md` (state model, the nine AUTO_SAFE predicates,
   invariants I1-I8, failure modes F1-F13, multi-node ownership, audit shape,
   rollout ladder, and the P0/P1/P2 acceptance tests). What exists in code
   today is ONLY the pre-existing manual path: `terminal_worktree_cleanup` ->
   `GitIsolationService.cleanup_worktree_for_task`, which is human-invoked,
   accepts `force=True`, takes no lock, and writes NO audit row (verified:
   `audit.db` has zero rows for any worktree action, and neither
   `git_worktree.py` nor `git_isolation_service.py` contains an audit call).
   Implementation is tracked as the P0-P7 backlog items tagged
   `worktree-janitor`; the contract item is `blg_349fb9e08b4f`.
   Two findings from the audit that the contract encodes and that must not be
   re-litigated by an implementer:
   - There is no `FAILED_FINAL` status. `TERMINAL_STATUSES` is
     `(COMPLETED, SKIPPED, CANCELLED)`; `FAILED`/`BLOCKED` are retryable. The
     trigger is those three, or `FAILED` with `attempt_count >= max_attempts`.
     Cleaning up on a bare `FAILED` deletes the retry's own working directory.
   - The lifecycle hook belongs in `queue_store._transition_locked`, NOT on
     `QueueService.on_completed`/`QueueEngine.on_completed` -- those have TWO
     call sites, so neither is a chokepoint.
   Not urgent: measured 2026-09-14, the worktree filesystem (`/dev/sda3`) was
   26% used with 83G free. The disk pressure that actually broke tooling was on
   tmpfs `/tmp`, which worktree removal cannot relieve (different filesystem).

0. **Repo Read V1 — the two things it deliberately does not do yet
   (2026-09-14).**
   a) **Rolling the new node agent out to dell-5530 / hp / any other
      remote node.** `repo_service` routing, `NodeClient.repo_op` and the
      `/v1/repo/{op}` endpoint are all implemented and tested, but a
      remote node can only serve repo reads once ITS agent runs this
      version; an older one correctly answers NODE_LACKS_REPO_READ. The
      same restart is what activates the `/v1/repo-evidence` NameError fix
      on that node. Not done autonomously: restarting the dell-5530 agent
      is the exact disruptive action item 7 below documents at length (a
      real `taskkill /F` with no known ConPTY reattach), and it needs the
      same explicit per-instance go-ahead. **Local/controller-side reads
      work today with no restart of anything remote.**
   b) **Write capability (checkout/commit/branch/push/apply).** Out of
      scope for V1 by design, not by omission — see the read-only locks in
      the Feature Details entry. Any future write surface is a new,
      separately-audited capability with its own gate; it must not be
      added by widening `repo_read.READ_ONLY_GIT_SUBCOMMANDS`.
   Also not done, and smaller: searching git HISTORY (`log -S`,
   `grep <rev>`) rather than a work tree; `fetch`/`clone` of a private
   remote this host cannot already read (no key was needed or created —
   anonymous HTTPS read against `origin` works today).

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

27. **The knowledge map covers ~12% of this package — DONE, 2026-09-15, and
    it did not produce the improvement it was expected to.** The package is
    now indexed completely: 33 modules / 147 paths, every `terminal_mcp/*.py`
    claimed exactly once, enforced by `scripts/knowledge/index_modules.py`
    and by `tests/test_efficiency_benchmark.py`. Re-running the benchmark
    (`docs/TOKEFF_BENCHMARK.md`, "How this has moved") measured: the briefing
    now names the fix site in 8 of 17 comparable cases instead of 5, and
    leaves a worker doing full re-analysis in 7 cases instead of 12 — but it
    beats the luckiest single grep LESS often (1/8 vs 2/5), because a
    briefing drawn from a real module is 4.67 files where the near-empty map
    produced 0.61. The headline verdict is still FAIL. Two consequences are
    now the open work, below (items 30 and 31). A side effect worth naming:
    `test_dogfood_work_v1.py`'s strict test (`TERMINAL_MCP_DOGFOOD_STRICT=1`)
    passes for the first time — the planning pipeline really does consult the
    map now.

28. **Module choice depends on the report naming the module (same
    benchmark).** The synthetic mirror pair is the evidence: one defect,
    worded as its commit subject, scored 0.483 and reached the right module;
    worded as a user would report it, scored 0.025 and reached nothing. A
    real bug report looks like the second. Candidate directions, none
    attempted here: score against the module's own known-issues and past
    bug symptoms as well as its identifiers, or let triage set
    `likely_module` before retrieval runs (the shipped path already accepts
    one — the benchmark deliberately measured the harder case where nothing
    does).

29. **Measure the runbook registry's own token claim (not covered by the
    2026-09-14 benchmark).** That lane's claim -- a registered procedure is
    called instead of a command being re-derived, and a PASS returns one line
    instead of a log -- is about command re-derivation and output size, which
    the four metrics that benchmark measures (files read, search rounds,
    re-analysis, elapsed) do not capture. A real measurement needs recorded
    runs with their logs: `as_context()` bytes against the log bytes it
    replaces, over real runs on a real machine. Deliberately NOT approximated
    in the benchmark: the logs available on this host are healthcheck-sized
    (41-297 bytes) and a saving computed from them would have been a number
    that looked like evidence.

---

## Notes / Ideas store (kho ghi chú dùng chung) — IMPLEMENTED

Full design + every tool with JSON examples: `docs/notes.md`. ChatGPT-facing
how-to: `docs/CHATGPT_USAGE.md` §7b.

Answers "the user saw something good mid-chat and said *lưu lại*". Distinct
from both neighbours it could be confused with:

- **Project Backlog** answers *what a project intends to do* and lives in
  one repo's own file. A note is captured BEFORE anyone knows which project
  it belongs to, so it must not be repo-scoped — `project_id`/`project_name`
  are nullable and attached later (`note_link_to_project`).
- **Session Knowledge** captures what a session *emitted*, automatically,
  with a retention cap. This holds what a human deliberately *kept*, with no
  retention cap at all — an idea is never evicted to save space.

Shape:

- `terminal_mcp/notes_store.py` — SQLite (one controller-side `notes.db`) +
  FTS5/bm25 over title/summary/original_content/analysis/tags, tracked
  migration via `schema.apply_migrations`/`PRAGMA user_version`, soft delete
  (`deleted_at`, which also leaves the search index).
- `terminal_mcp/notes_service.py` — attachment BYTES on the filesystem
  (`notes_attachments/YYYY/MM/<attachment-uuid>.<ext>`), MIME decided by
  magic number, atomic write + fsync + `os.replace`, containment re-checked
  on every read and unlink. The DB holds metadata only — never a base64
  blob.
- 12 `note_*` MCP tools (§15) and `/dashboard/notes` (gallery / list /
  Kanban, Vietnamese, mobile) with 11 JSON routes behind the existing
  `_read_guard`/`_mutation_guard` boundary.
- `NotesConfig` (`notes:` in config.yaml). Defaults ON and need no
  configuration; `notes.attachment_source_roots` is EMPTY by default, which
  is what keeps the `source_path` attachment transport refused until an
  operator grants specific directories.

Authentication (hardening pass, same day): the whole Notes HTTP surface sits
behind `notes.require_auth` (default true), satisfied by a webauth session
cookie OR a verified Cloudflare Access assertion — the repo's two existing
identities, no third mechanism. Edge-only Access was insufficient because
cloudflared connects over loopback, making tunnel traffic indistinguishable
from local traffic inside this process. Fails closed when no store is wired.
The `note_*` MCP tools remain transport-authenticated only, like the other 202
tools; see `docs/notes.md` for why a notes-only MCP credential would be
theater next to `terminal_send_text`.

V1 is fully local and deterministic — no embedding service, no LLM, no new
infrastructure — which is a hard requirement, not a simplification: the kho
must work with the machine offline. Semantic search could be layered behind
the same `note_search` contract later.

Known V1 limitations (all deliberate, all in `docs/notes.md`): single-user
(this dashboard has one operator identity, so there is no owner column and
no half-built multi-tenancy); controller-local (an idea has no node); no
automatic screenshot capture of a saved URL (no browser service in this
project — the model or the user attaches the image); image attachments only
(png/jpeg/webp/gif — the types the UI can actually preview).

Tests: `tests/test_notes_store.py`, `tests/test_notes_attachments.py`,
`tests/test_notes_mcp_tools.py`, `tests/test_notes_dashboard.py`,
`tests/test_notes_config.py`, `tests/test_notes_auth.py` (254 tests), plus the
two existing inventory guards updated in the same commit (`tests/test_server.py`'s exact tool set
and `tests/test_dashboard.py`'s exact route set).

---
30. **Module choice scores a shared identifier as strongly as a
    discriminating one (found by the 2026-09-15 benchmark re-run).**
    `work_reuse._score_module` awards 0.45 for any identifier overlap, so a
    token that appears in many modules' file names counts as much as one that
    names a single module. With 9 modules this rarely bit; with 33 it
    dominates. Measured examples from the real corpus: `client-side CSI regex
    leak` in `dashboard.py` matched `nodes` on the token "client"
    (`node_client.py`); two dashboard bugs went to `webauth`, which ties with
    `work_ui` at 0.477/0.483 because `webauth_dashboard.py` also contains the
    token "dashboard", and the shipped ordering `(-score, name)` puts
    `webauth` first alphabetically. `engine`, `loop`, `registry`, `service`,
    `session`, `store` and `work` are each shared by three or more modules
    today. Candidate direction, not attempted here (it would have been tuning
    the scorer against the corpus that found it): weight an identifier match
    by how few modules contain that token, and report a tie as ambiguous
    rather than resolving it alphabetically.

31. **Six modules carry no summary because their largest file has no module
    docstring** (`config`, `http_api`, `mcp_surface`, `security`,
    `session_ops`, `work_ui` — 2026-09-15). The indexer generates summaries
    mechanically from the code's own docstrings, deliberately, so that no
    summary is written to match a bug report's wording after the fact. The
    honest consequence is that these six are found by identifier alone. The
    fix belongs in the code: give `core.py`, `dashboard.py`, `config.py`,
    `mcp_app.py`, `server_http.py` and `audit.py` real module docstrings, and
    re-index. That is a change to the source, not to the map, and the
    benchmark will measure whether it moves anything. A second, smaller
    limitation of the same rule, recorded rather than quietly accepted: the
    summary is the LARGEST file's first sentence, so one file speaks for the
    whole module (`auth` is described by `enrollment.py`, `capabilities` by
    `host_metrics.py`). Combining the top few files' sentences would describe
    more of each module; it was not done in this pass because every further
    rule variant chosen by its benchmark score fits the instrument a little
    more tightly to this one corpus.

## Project Backlog (planning layer) — IMPLEMENTED

Full design: `docs/backlog.md`. Example file: `docs/examples/backlog.example.json`.

Answers "what does THIS PROJECT intend to do", as opposed to the Task
Queue's "what is executing now". Source of truth is a file **inside the
project repo** — `.terminal-mcp/backlog.json` — so the plan is portable,
versionable, reviewable in git, and shared by every session on that repo
regardless of which subdirectory each one sits in.

**Audit first, per the task's own instruction.** The pre-implementation
audit found that `queue_store.queue_tasks` is the ONE canonical task
table and that planner/PM/incident/release all layer on it via `metadata`
rather than adding tables — so this feature adds **no** second task
engine. It also found the only existing notion of "project" was
`queue_lanes.project`, a free-text label set per session lane, which
cannot key a shared backlog. Hence `project_identity.py`.

- **Identity** (`project_identity.py`): from the REPO, never the cwd
  string. `git remote origin` normalised (all URL styles collapse to one
  id; embedded credentials stripped so a token can never land in a
  committed file) → `PROJECT.yaml`'s `project.code` → real repo root
  path (flagged `is_portable: false`). A non-repo directory is refused
  `NOT_A_PROJECT` instead of getting a backlog somewhere meaningless.
- **Storage (REVISED 2026-09-09, after measuring the fleet)**: the
  SOURCE OF TRUTH is a controller-side SQLite store keyed by the
  canonical `project_id` (`backlog_db.py`); the repo file is an
  export/import projection. The original file-as-truth design was
  disproven by measurement: `terminal-mcp` lives in **7 checkouts across
  3 nodes** and `offline-pos` in 3 across 2, and the controller answered
  `PATH_NOT_ALLOWED` for every remote checkout path because those paths
  do not exist on it. A backlog in one checkout was invisible to every
  other node working the same project. Identity was already correct (all
  7 checkouts collapse to one id) — only storage was wrong. A project is
  now addressable by `project_id`, by `project_node_id`+`project_session`
  (resolved from the OWNING node's registry, so a remote session's
  backlog is reachable without the controller touching that filesystem),
  or by a local `path`. `terminal_project_list` auto-detects the fleet's
  git projects from data nodes already record.
- **File format** (`backlog_store.py`): JSON chosen over YAML deliberately
  (byte-exact round-trip, loud parse failure, no `yes→True` surprises);
  merge-friendliness handled by a fixed key order + one item per
  line-block. Atomic write (temp + `os.replace`), `fcntl.flock` on a
  separate `.lock` file, and a `revision` counter.
- **Concurrency**: a per-PROJECT in-process lock (the controller is the
  single writer) plus SQLite's transaction, with `expected_revision`
  still guarding the cross-AGENT race — a stale write is refused
  `REVISION_CONFLICT`, never silently clobbered. Two projects never block
  each other. The old `fcntl.flock` on a repo file no longer governs
  anything and was removed rather than left as decoration.
- **Verified-done gate**: `backlog_complete` requires real evidence
  (commit/test/deploy) OR a linked queue task that reached `COMPLETED`
  (which `queue_store.py` itself defines as the spec's `VERIFIED_DONE`).
  `backlog_update` cannot set DONE at all. An agent asserting completion
  is not accepted.
- **Traceability**: `backlog_dispatch` creates a real queue task through
  the existing canonical `QueueService.create_task` and links both ways
  (`item.queue_task_id`, queue `metadata.backlog_id`), giving
  backlog_id → queue task_id → session → commit/test.
- **Security**: every path goes through `lifecycle.resolve_cwd` — the
  same `allowed_cwd_roots` + symlink gate session creation uses — run
  BEFORE any git introspection, with the discovered repo root re-checked
  (walking up out of an allowed subdirectory would otherwise escape).
  Every write is audited; dashboard writes also pass `_mutation_guard`.
- **Surface**: 9 MCP tools (`terminal_backlog_*`) whose descriptions
  teach the workflow `get → analyse → add/update → dispatch → verify →
  complete`, plus `GET /dashboard/api/backlog` and
  `POST /dashboard/api/backlog/{add,update,dispatch,complete}`.
- **Git posture**: the file is NEVER auto-committed. Tracked is the
  recommended default (reviewable, reaches other nodes); ignoring
  `.terminal-mcp/` makes it local scratch. Tradeoff documented in
  `docs/backlog.md`.

**Project Brief integration — WIRED.** A session's recovery brief
(`terminal_knowledge_recover`) now carries a `project_backlog` field and a
readable block in `recovery_brief_text` listing the project's open items,
with `[unrun]` marking those never dispatched. It reads the backlog file
each time (no cached second copy, pinned by a test), is never fatal (a
non-repo cwd, a repo outside `allowed_cwd_roots`, a missing or corrupt
file each degrade to `available: false` with a reason), and is added to
the brief's `untrusted_fields` because backlog text is agent-written.

**Dashboard panel — SHIPPED** (`/dashboard/backlog`). Built as its own
page in the `/dashboard/nodes` + `/dashboard/tasks` family rather than a
new tab inside the ~3.6k-line main template, whose fetch() surface is
pinned by an exact-count test — the only edit there is one nav link. Per
project: counts/filters, add, status moves, Dispatch, Complete (which
prompts for evidence, so the verified-done gate is visible in the UI),
and a queue chip linking a dispatched item to Global Tasks. Every write
sends `expected_revision`, so the UI cannot bypass optimistic
concurrency. Because backlog text is agent-written and rendered in a
browser, the page builds content with `textContent` only — no
`innerHTML`/`outerHTML` assignment, no inline `on*=` handlers — pinned by
`tests/test_backlog_panel.py`, whose assertion matches the assignment
rather than the word so it cannot rot into a weak test.

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

# 2026-09-07 Codex capability refresh + submit reliability checkpoint

- Capability detection now resolves launchers against the live Windows user
  PATH (including `HKCU\\Environment\\Path`) and returns an absolute launcher
  path for actual spawning. The node-agent exposes authenticated
  `POST /v1/capabilities/refresh`; the controller/dashboard proxy is
  `POST /dashboard/api/nodes/{node_id}/refresh-capabilities`. The refresh
  updates only capability fields, not liveness or metrics.
- Prompt submission has a shared agent-specific profile. Production
  `config.yaml` uses Codex `max_enter_attempts: 3`, `enter_interval_ms: 180`,
  and `verify_after_each_enter: true`; each retry requires the Codex adapter
  to still prove the same draft is pending. Generic/Claude remain single-
  Enter by default. `fixed_enter_count` is disabled unless explicitly set.
- This policy is fail-closed by agent type across MCP, dashboard, queue,
  supervisor, controller, Linux tmux, and Windows ConPTY paths: Codex alone
  may send bounded Enter retries. Claude injects once and sends at most its
  initial Enter; it never receives watchdog/sweeper Enter retries. Unknown
  agents use the single-submit default and unresolved delivery is reported as
  `DELIVERY_UNKNOWN`/`STUCK` rather than guessed or duplicated.
- Receipts add `enter_count`, `attempts`, `evidence`, and
  `submit_latency_ms`; prompt content is never logged by this telemetry.
- Live dell-5530 agent generation `a10b3f91e292b679` is still version 0.12.0
  and returns HTTP 404 for the new refresh endpoint. `wtest` is live and
  attached, so no node-agent restart was performed; dashboard Codex
capability remains pending deployment/restart-safe refresh.

# 2026-09-09 P0.5 Verify Queue + capability routing

Verification becomes **claimable work routed by capability** instead of a
state only the implementing session can leave. Previously a task reaching
`VERIFYING` could be advanced by exactly one actor: the queue engine's tick,
reading the same session the work ran in, looking for a completion marker.
That cannot express "verify this on a Windows node with .NET, and not by the
agent that wrote the code".

**No new task state, no new task transition edge.** A `verify_jobs` row
(migration v7) is a *satellite* of a `queue_tasks` row, never a parallel copy.
Every verify outcome lands on an edge that already existed:

| verify job status | task transition | evidence required |
| --- | --- | --- |
| `VERIFIED_PASS` | `VERIFYING -> COMPLETED` | yes — gated |
| `VERIFIED_FAIL` | `VERIFYING -> FAILED` | structured failure summary |
| `NEEDS_REWORK` | `VERIFYING -> FAILED` | structured failure summary |
| `VERIFY_BLOCKED` | `VERIFYING -> BLOCKED` | structured failure summary |
| `VERIFY_CANCELLED` | none | n/a |

`VERIFIED_FAIL` and `NEEDS_REWORK` deliberately share one task status: the
task vocabulary has no separate "needs rework" state and inventing one would
duplicate the state machine. The distinction is kept on the job, and `FAILED`
is the status with an existing way out (`terminal_queue_retry` -> `QUEUED`).
This mirrors how `record_coordinator_decision` already maps the Coordinator's
four-value vocabulary onto existing task statuses in one place.

**Job lifecycle:** `VERIFY_PENDING -> VERIFY_CLAIMED -> VERIFY_RUNNING ->`
one of the terminal results above. Release and lease expiry both return a job
to `VERIFY_PENDING` in identical shape. `VERIFY_BLOCKED` is recoverable
(`terminal_verify_requeue`), not terminal.

**Backward compatibility is structural, not a flag.** Nothing creates a verify
job unless a task's own `completion_policy["verify"]` asks for one. No existing
task carries that key, so every existing lane behaves exactly as before, and
in-session verification remains the default. There is no global switch to
forget to leave off.

**Capability routing** uses AND semantics over a node's *reported* facts only:
probed tool capabilities (P0.3) plus `platform`, `session_backend` and
`shell_capabilities`. Nothing is inferred — a Windows box is not assumed to
build WebView2. Arbitrary capabilities (`webview2`, `browser`) become routable
when a node probes them via `TERMINAL_MCP_CAPABILITY_PROBES`; no application
name is special-cased. Including `platform` means `windows` routes to
dell-5530 today even though that node still reports an empty probed list.
**Known limitation:** `macos` is not routable — the node agent has no Darwin
branch (only `windows_agent.py` sets a platform), so the MacBook reports
`platform=linux`. Fixing that changes what `choose_node(required_platform=...)`
matches for existing callers and is therefore out of P0.5's scope.

**Evidence gate.** `VERIFIED_PASS` requires evidence that is more than an agent
self-report (`summary`/`message`/`note`/... alone are refused) and is not
contradicted by itself (`exit_code != 0`, `passed: false`, `tests_failed > 0`
are all refused as `EVIDENCE_REJECTED`, with the reason; the job stays claimed
so real evidence can be supplied). A negative verdict requires a structured,
non-empty `failure_summary`, every string of which is redacted before storage.

**Lease ownership.** Verify jobs reuse P0.4's semantics: `claim_token` +
`lease_expires_at`, every mutating verb matching on the current token under
`BEGIN IMMEDIATE`. `terminal_verify_handoff` rotates the token, so a previous
holder cannot mutate the result after a handoff. `terminal_verify_reconcile`
returns expired leases to the pool and closes jobs whose task left `VERIFYING`
— as `VERIFIED_PASS` carrying the task's own recorded evidence when the
in-session path completed it, never by the reconciler's fiat.

**No capable verifier** is a visible hold, never a silent pass: with
`fallback: "in_session"` (default) the existing marker path keeps running; with
`fallback: "hold"` in-session completion is suppressed and the job waits with a
`routability` reason ("no online node reports dotnet+windows", or "the only
capable node is the implementer"). Neither path can auto-pass or drop a task.

**Duplicate prevention** is `UNIQUE (task_id, attempt)` in the schema, not an
in-process guard, so it survives restart. A genuine retry bumps `attempt_count`
and gets its own job, preserving the previous attempt's verdict and reasons.

**Traceability:** `terminal_verify_trace(task_id)` returns
`backlog_id -> task -> implementer -> branch/commit -> verify job -> verifier ->
evidence -> result` plus an append-only audit trail (actor, time, reason) of
every state change. `claim_token` is never returned by any read.

**12 MCP tools** (`terminal_verify_request/list/claim/start/renew/release/
handoff/complete/fail/requeue/trace/reconcile`, total 156) and one read-only
dashboard route, `GET /dashboard/api/verify/queue` (status counts, verifier,
required capabilities, age, block reason).

# 2026-09-09 P0.6 Named-resource ownership lock

`lease.ResourceLockStore` answers "no two agents touch the same file / module
/ branch at once", built by **generalising the pane lease**, not by adding a
second lock.

**What was actually reusable** was not the pane but the atomic check-and-set
in `acquire()`: one `INSERT ... ON CONFLICT DO UPDATE ... WHERE` whose exact
shape was arrived at by reproducing a real race (a `SELECT`-then-write let two
concurrent callers both believe they had won). That algorithm now lives in
`_LeaseTable`, parameterised by table and key column; `PaneLeaseStore` and
`ResourceLockStore` are both thin specialisations of it.

**The pane lease is unchanged** — table, columns, method names, signatures,
return types and the 20s TTL. It is on `core.py`'s send hot path, the most
safety-critical path in the codebase, so the generated SQL is asserted
**byte-identical** to the statement that shipped rather than assumed
equivalent because behavioural tests still pass.

`resource_locks` is a **separate table in the same `leases.db`**. Separate,
because the two have different lifetimes (20s vs minutes) and different
subjects, and mixing long-lived agent locks into the table every send contends
on would be risk for no gain. Same file, because `leases.db` is already
covered by `/health/ready` and the documented backup procedure.

Semantics match the pane lease — owner-scoped, TTL'd, crash-recoverable,
idempotent re-acquire — plus what a shared resource needs and a pane does not:

| Addition | Why |
| --- | --- |
| **Project scoping** (P0.1 `project_id`) | `src/app.py` in one project is a different resource from `src/app.py` in another; a global key space would make unrelated repos block each other |
| **Holder returned on refusal** | A primitive that only says "no" leaves the caller nothing to act on; refusals name owner, reason and expiry |
| **`acquire_many`, all-or-nothing** | Two agents each needing `{a, b}` and taking them one at a time can finish holding one apiece forever; one transaction makes that impossible. Keys are sorted, so differing call orders contend identically |
| **`release_all(owner)`** | A finished worker never leaves a resource pinned until TTL |
| **`force_release(actor, reason)`** | A separate verb, not a flag: breaking someone else's lock must be impossible by accident, and is reported with whose lock was broken |

Default TTL is **300s**, matching `queue_store.renew_task_lease`, so a worker
renews its task and its locks on one cadence. **A holder that stops renewing
is treated as gone** and its lock becomes reclaimable; renewing an
already-expired lock fails rather than silently extending.

The composite primary key is `project_id + \x1f + resource_key`; both parts
reject control characters, so the separator can never appear inside a part and
collide two distinct resources. `lock_key` is internal and never returned.

**Advisory, not enforcement.** Nothing here can physically stop an agent
editing a file it did not lock — these give cooperating agents a durable,
crash-recoverable way to agree. Claiming otherwise would be a false guarantee.
Deliberately **no waiter queue**: a caller that cannot get a lock is told who
holds it and decides for itself whether to wait, switch work, or escalate;
blocking inside a lock primitive is how a fleet deadlocks.

**8 MCP tools** (`terminal_resource_lock`, `_lock_many`, `_renew`, `_unlock`,
`_unlock_all`, `_holder`, `_locks`, `_force_unlock`; total 164). Expired rows
are pruned by the existing `maintenance` cycle alongside pane leases.

# 2026-09-09 P0.7 Project APIs — P0 complete

`project_service.ProjectService` gives a project a single addressable view.
Everything it reports already existed, but only one layer at a time: lanes are
per session, events are on the bus, verify jobs are their own queue, locks are
their own store, plans are in the backlog. Answering "what is project X doing"
took six calls and knowing which six.

**No new state.** No table, no migration, no background loop — asserted by a
test that the module contains neither `CREATE TABLE` nor `Migration(`, and that
exercising it leaves the queue schema and `user_version` unchanged. Every value
is read live from the store that owns it:

| Section | Source |
| --- | --- |
| lanes / tasks / workers | `queue_store` (`project_id`, P0.1; leases, P0.4) |
| events | `event_bus` (P0.2) + `queue_events`, derived |
| capability routing | `node_registry` (P0.3) |
| verification | `verify_queue` (P0.5) |
| resource locks | `lease.ResourceLockStore` (P0.6) |
| plan / goals | `backlog_service` |

A section reads `null` when that subsystem is not wired, which is deliberately
distinct from `0` — "not wired" and "wired and empty" are different answers.

**`queue_events` has no project column**, so a project's queue events are
*derived*: by the lane's project, by a task in that lane, or by the event's own
task. The last arm matters on its own — a task moved between lanes keeps its
events attributed to the project rather than to whichever lane it sat in.

**Submitting a goal never starts work.** `terminal_project_submit_goal` writes
a backlog item — an intent — and does not create or dispatch a queue task.
Autonomous dispatch stays behind its existing two-gate opt-in, and a goal API
that quietly queued work would be exactly that bypass. `terminal_backlog_dispatch`
remains the explicit crossing point.

**Pause and resume are not symmetric, on purpose.** Pausing a project pauses
every lane it owns, leaving an already-paused lane and its reason untouched.
Resuming un-pauses only the lanes *this project's pause* paused, matched on a
`project-pause[<project_id>]` marker written into `paused_reason`; a lane paused
by an operator or by a coordinator `NEEDS_HUMAN` decision is **skipped and
reported with its reason**. Silently undoing a deliberate pause would be the
most dangerous thing in this layer. `force=true` overrides and says so in the
result — an explicit decision to override someone, never a convenience default.

**`terminal_project_assign`** moves a task only when given an explicit
`session`. With `capabilities` it resolves candidate nodes and stops, because
choosing a lane on a remote node is not a decision a facade should make
silently. It refuses a task belonging to a different project. Matching is AND
over reported facts (probed tools plus platform) via the shared
`match_nodes_by_capability` — renamed from `match_verifier_nodes` in P0.5, since
P0.7 is the second caller proving it was never verifier-specific.

**`terminal_project_report`** counts state **transitions** in a window, not
current statuses: a task that completed and was later retried is still a
completion that happened. `current` is returned alongside so both readings are
visible. Note `queue_events.timestamp` is second-granularity, so windows are
meaningful at second resolution, not finer.

**7 MCP tools** (`terminal_project_status`, `_submit_goal`, `_events`,
`_report`, `_pause`, `_resume`, `_assign`; total 171). **This completes P0
(P0.1–P0.7).**

# 2026-09-10 Orchestration V1 — P0-A foundations

Full architecture: **`docs/ORCHESTRATION_ARCHITECTURE.md`**.

A six-domain audit of the runtime established the starting point: the
primitives are broad and well-built, but **inert and not wired to each other**.
Production held 0 queue tasks, 0 verify jobs, 0 resource locks, 0 capability
profiles, 0 integration rows and 1 event. The bus's own vocabulary
(`TASK_CREATED`, `VERIFY_PENDING`, `WORKER_DONE`, `MERGE_CONFLICT`) named
exactly the signals the queue, verify queue and locks produce — and none of
them called `publish()`.

**Six defects fixed**, each with a regression test proven to fail without the
fix: `handoff_task` destroyed a task's migration provenance; no dependency
cycle detection existed anywhere (a cycle silently deadlocked a lane forever,
with no event and no alarm); the event bus never dead-lettered, so a poison
event stayed `PENDING` and invisible; `events.db` was never maintained;
`force_release` recorded nothing; and the stdio tool surface silently lagged
HTTP by 19 tools that consequently had no contract test.

**The OUTCOME layer** (migration v8, additive and nullable). Backlog→task was
welded 1:1 and one-shot, so a deliverable spanning several tasks was
inexpressible. An outcome is the user-visible unit, and the rule that makes it
worth having is that **an outcome is not done because its children are done** —
the rollup structurally cannot reach `DONE`; `AWAITING_ACCEPTANCE` is the
state a naive implementation would have called done, and completion requires
evidence named against **each** acceptance criterion.

**Events wired.** Optional sinks on `QueueStore`/`VerifyQueue`, mapped in
`event_wiring.py` so the stores stay ignorant of the bus. Delivery is
at-least-once by construction (different databases, no cross-store
transaction), covered by idempotency keyed on the `queue_events` row id. Bus
migration v2 adds `actor`, `causation_id` and `event_cursors` — a durable
per-consumer high-water mark that reads non-destructively and never rewinds.
**Nothing consumes the stream automatically**; autonomy stays behind its
existing gates.

**The WORKER view** — five real roles (`WORKER`/`VERIFIER`/`INTEGRATOR`/
`DEPLOYER`/`COORDINATOR`, previously free text compared by string equality), a
worker may hold several, and declared capability is never merged with probed
capability. Composition over `pm_store` + node registry + queue; no new table.

Tool surface: **202**, one surface for stdio and HTTP.

# 2026-09-10 Controller migrated to m910

The Dell Latitude that ran the controller was being powered off, so the
controller/orchestrator moved to **m910** (`mesflow@192.168.1.109`). Full
procedure, rollback and the split-brain discipline: **`docs/CONTROLLER_RUNBOOK.md`**.

**m910 is now primary.** Same commit as verified `origin/main`, `dirty: false`,
services `enabled` with linger so they survive reboot. Both tunnels moved with
their existing IDs, so `terminal-dashboard.mesflow.net`, `terminal-login.mesflow.net`
and the OpenAI MCP endpoint are **unchanged for clients**.

**Canonical vs node-local state.** Only controller-canonical stores were
migrated (`queue`, `backlog`, `events`, `nodes`, `integration`, `release_store`,
`planner_store`, `pm_store`, `supervisor`, `connections`, `webauth`). The
session-scoped stores (`session_registry`, `session_knowledge`, `grants`,
`bindings`, `audit`, `prompt_submissions`, `killed_sessions`, `leases`) are
**node-local** and were deliberately not copied: the new host has its own, and
overwriting them would make m910's `local` node claim the old host's 528
sessions. Project identity, backlog counts and revision are preserved exactly.

**`local` means the host the controller runs on.** m910 was removed from
`nodes.remote` and its own node agent stopped and disabled — leaving either in
place would have made the controller register itself as one of its own remote
nodes, in a loop.

**Split-brain: one real trap found.** `terminal-mcp-tunnel-watchdog.timer` fired
every 45 s and *restarted the controller it was watching*, silently resurrecting
the old one ~30 s after it was stopped. Stopping services is not enough — timers
that can restart them must be disabled too. No canonical divergence resulted
(every store was byte-identical to the final sync; only heartbeat rows differed).

**Config now lives outside the repo** (`~/.config/terminal-mcp/config.yaml`).
In-tree host config makes the checkout permanently dirty and makes "which commit
is running" unanswerable.

# 2026-09-10 Dashboard: node-grouped session lists + interactive keys

Two behaviour changes to the dashboard UI. Both are user-visible, so they are
recorded here rather than only in the commit log.

## Session lists are grouped by node

Previously both session lists (the main dashboard's tab strip and
`/dashboard/sessions`) were flat, and a session's node showed only as a small
per-row badge that was **hidden when `node_id` was `local`** — so on a fleet
the local node was indistinguishable from "no node information at all".

Now, on both surfaces:

* one group per node, with a header carrying display name, `node_id` (when it
  differs from the name), status and session count;
* nodes sort **online → recent → offline**; within a node, attention-first,
  then most-recent activity, then name;
* groups collapse/expand, remembered in `localStorage` per page
  (`tmNodeCollapse:<page>`), and the group holding the currently-viewed
  session is always revealed;
* an **online** node with no sessions still gets a group with a small empty
  state, so an idle node reads as "up and free" rather than missing — except
  while a filter is active, where a group with no matches is suppressed;
* the main dashboard gained a session filter box that searches across groups.

`degraded` (the registry's own term for "heartbeat stale but not yet offline")
is surfaced to the operator as **recent**. An unknown status sorts with
offline — never ahead of a node the registry has positively vouched for.

The grouping logic lives in exactly one place (`dashboard.NODE_GROUP_JS`),
injected into both pages; node identity is never hardcoded, it is read from
`node_id`/`node_name` on the existing `/dashboard/api/sessions` rows and from
`/dashboard/api/nodes` for status and for session-less nodes.

**Layout change:** the main dashboard's tab strip no longer scrolls
horizontally. Tabs wrap inside their node group and the strip scrolls
vertically with a height cap, so no session is reachable only by discovering a
sideways gesture. This supersedes the earlier horizontal-drag fix.

## Interactive key sends (arrows / Tab / Esc / Enter)

The dashboard could send text but not keys, so a session sitting on an agent's
numbered menu could not be answered from the dashboard at all — an escape
sequence typed into the composer is typed, not pressed.

* New route `POST /dashboard/api/session/keys`, calling the **existing**
  `terminal_send_keys` (same allowlist, same sensitive-key confirmation, same
  durable pane lease as a text send). Remote sessions resolve through
  `controller.resolve_session` exactly as `session/input` does.
* Key sends remain their own capability: `permissions.allow_send_keys` plus
  `input_policy.allow_keys`. `/dashboard/api/sessions` now reports
  `send_keys_enabled`, `allowed_keys` and `sensitive_keys` so the UI **disables
  unavailable keys with the reason** instead of offering a control that fails.
* On-screen pad (↑ ↓ ← → Tab Esc ⏎) — on a phone this is the only way to send
  these at all, so the buttons carry a 44px touch target. On desktop an opt-in
  "bắt phím" mode routes those keys from the composer to the terminal;
  Escape always leaves the mode and modified presses (Alt/Ctrl/Meta,
  Shift+Tab) stay with the browser, so the keyboard is never trapped.
* Works on tmux and on Windows ConPTY: `WindowsSessionBackend.KEY_BYTES`
  already maps every key the pad offers, asserted by a test so a future pad
  addition cannot silently no-op on Windows.

## Remote composer mirror

The dashboard now shows what the **agent** currently has on screen as an
interactive choice — a numbered menu, or a composer line with text already in
it — read from the same pane tail the output view already polls (no second
capture, which has a real side effect on the pane).

It is rendered **separately from the operator's draft**, never merged: a poll
that overwrote a half-typed message is the data loss this separation exists to
prevent. Copying the mirrored content into the composer is an explicit button,
and it confirms first when a draft is in progress; a remote change arriving
mid-draft is highlighted, not applied. Draft dirtiness follows the existing
per-session `drafts` map rather than a second notion of "what the user typed".

Detection is a heuristic over pane text and is deliberately conservative
(≥2 numbered lines for a menu; a prompt line with non-empty content for a
composer). It was validated against real Claude output on a live Windows
ConPTY session — both a 3-option menu with its selected line, and a composer
holding real typed text.

# 2026-09-10 Session whitelist REMOVED — grants are the source of truth

## What changed

The session-name whitelist (`allowed_session_patterns` /
`input_policy.allowed_session_patterns`) **no longer authorizes anything**.
Access to a session is decided by:

1. an explicit user grant (`grants.db`), else
2. `session_access.default_read` / `session_access.default_input`,

with a small set of hard floors that survive unchanged (below).

### Why

Deciding access from a session's NAME produced a state operators reported as a
bug: a row showing `allowed=false` beside `effective_read=true` /
`effective_input=true`. Both fields were "correct" — `allowed` was the static
whitelist result, `effective_*` was whitelist-OR-grant — but they look like
they must agree, and every consumer had to know which one was the real gate.
It also meant that granting access to one session required editing config.yaml
and restarting the service, which is precisely what a per-session grant exists
to avoid.

### `allowed` is deprecated

`allowed` is now an **alias of the real read authorization** in both
`terminal_list_sessions` and `dashboard_list_sessions`, so it can never
contradict `effective_read` again. Nothing in the runtime reads it to make a
decision. Existing callers keep working; new ones should read
`effective_read` / `effective_input`.

## Config

```yaml
session_access:
  default_read: false          # a session nobody has granted: content not readable
  default_input: false         # ...and it accepts no input
  migrate_whitelist_on_start: true
```

Defaults are CLOSED, deliberately: opening reads by default would be a weaker
posture than the whitelist it replaces. Discovery is unaffected and always was
— session name/size/activity are `tmux ls` metadata, never pane content.

## Migration — nobody loses access

`TerminalService.migrate_whitelist_to_grants()` runs at startup on **every**
node type (controller, Linux node agent, Windows agent). For each session that
exists right now and that the retired whitelist would have authorized, it
writes a real grant. It is strictly additive and idempotent:

* a session that already has a grant is left alone in either direction — a
  user who deliberately REVOKED read on a still-whitelisted session does not
  have it handed back;
* only running sessions are converted, because an input grant pins the
  session's current identity and there is nothing to pin for a name that is
  not running;
* nothing is ever revoked, and a failure never blocks startup.

## Security boundaries that did NOT change

Removing the whitelist is not open access. Still enforced:

* account/webauth/Cloudflare Access on the dashboard, and node bearer tokens
  between controller and node agents;
* the **sensitive-name floor** — a session whose name contains `root`, `ssh`,
  `password`, `secret` or `database` is refused regardless of any grant or
  default policy. This is the one name-based rule kept, because it guards
  against a careless default, not against a naming convention;
* `input_policy.denied_session_patterns` — a config-level DENY list, which a
  grant may not override;
* the global `permissions.terminal_read` / `terminal_input` /
  `allow_send_keys` switches;
* input grants still pin session identity and re-validate it at send time, so
  a session recreated under the same name never inherits input authorization.

## Code paths updated

`core.py` (`_read_authorized_with_grant`, `_input_authorized_with_grant`, both
list builders, rename target check), `supervisor.py` (watch sync now asks the
canonical gate — readability is the supervisor's real prerequisite, since it
watches by capturing output), `dashboard.py` and `webauth_dashboard.py`
(session detail / input / status-tail routing).

`permissions.session_allowed` / `input_session_allowed` remain **only** as the
migration's input and are documented as such. They must not be reintroduced
into an enforcement path.

## Known limitation at time of writing

The controller and its local node are fully converted. **Remote node agents
still run the previous build**, so rows they report continue to show the old
`allowed=false` beside `effective_read=true`. Those nodes pick up the new
semantics when their agent is redeployed; on `dell-5530` that redeploy is
gated on the ConPTY session-loss constraint documented in
CONTROLLER_RUNBOOK.md.
