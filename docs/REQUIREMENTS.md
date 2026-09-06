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
| Dashboard: Requirements/Feature Matrix link | PLANNED |
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
| Auto-dispatch background loop (code+tests) | IMPLEMENTED_NOT_LIVE_VERIFIED |
| Auto-dispatch enabled on any real session (incl. window/window2) | NOT ENABLED (`config.queue.enabled=False` everywhere) |
| 3-role pipeline (Coding A/B + Integration Agent) | VERIFIED (disposable only) |
| Integration Agent: event-driven WAIT/wake posture | PLANNED |
| Task Migration / Load Balancing | VERIFIED |
| Task Migration: dedicated Move-Task UI | PLANNED |
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
  itself is gone.
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
- **Supervisor/Coordinator panel:** header-menu "🧭 Supervisor /
  Coordinator" — extends the pre-existing v1/v2 watch panel with
  auto-dispatch loop status, queue depth per session, fleet-wide
  blocked/rework, Integration lane per project, merged recent-event
  timeline.
- **Integration lane view:** inside the Supervisor/Coordinator panel —
  one row per configured project, its own Waiting/Reviewing/Merging/
  Test/Regression/Rework/Blocked label (mapped from
  `integration_store.py`'s real status constants in exactly one place).
- **Requirements/Feature Matrix link:** **PLANNED**, not built.

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

**Status: VERIFIED** for everything except the background auto-dispatch
loop's LIVE enablement, which is **IMPLEMENTED_NOT_LIVE_VERIFIED**.

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
  `queue_loop.py`'s `QueueLoop` — a real daemon thread, code-complete
  and unit/integration-tested (incl. a REAL background thread driving a
  full lifecycle with zero manual `tick()` calls). Two independent,
  stacked gates: `config.queue.enabled` (new `QueueConfig`, global,
  `False` by default in this project's own `config.yaml`) AND the
  existing per-lane `queue_lanes.auto_dispatch_enabled` (`False` by
  default). **Neither gate is on for any real session right now,
  including window/window2** — this is IMPLEMENTED_NOT_LIVE_VERIFIED
  specifically because it has not yet been run against a real remote
  node (dell-5530) end to end; that live smoke test is this task
  batch's own explicit next step (see Backlog).
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
  `tests/test_three_role_smoke.py`.
- **Known limitations:** no real tmux session of its own (pure backend
  engine); no automatic background loop (event-driven wake is PLANNED,
  see Backlog).
- **Dependencies:** Phase 1/2, persist-before-dispatch.
- **Follow-up/backlog:** event-driven WAIT/wake posture (PLANNED); no
  dashboard view until "Dashboard Supervisor/Coordinator panel" below.
- **Trace:** `957f15a`.

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

---

## Backlog (explicitly not done yet — tracked here so it isn't re-discovered)

1. **Live remote-node auto-dispatch smoke test (dell-5530,
   `RemoteNodeClient`)** — this task batch's own required next step
   before `config.queue.enabled` can be turned on anywhere, including
   window/window2. Not started as of this file's own commit.
2. Event-driven WAIT/wake posture for the Integration Agent's own
   merge-test role.
3. Move-Task drag/drop UI for Task Migration.
4. Priority-edit/drag-reorder UI for the Task Manager.
5. Requirements/Feature Matrix link inside the Dashboard Task Manager.
6. Enabling `config.queue.enabled` and `queue_lanes.auto_dispatch_enabled`
   against any real production session, including window/window2 —
   blocked on item 1.
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
   OLD, buggy verification logic. Next step: pull/copy the fix to
   `C:\Users\tranv\terminal-mcp`, restart the node-agent process there
   (never the tmux-equivalent sessions themselves), and re-run the exact
   same live disposable-session repro to confirm 0 false negatives on
   the ACTUAL deployment those sessions use.
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
