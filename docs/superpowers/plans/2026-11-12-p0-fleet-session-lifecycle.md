# P0 Fleet Session Lifecycle Implementation Plan

> **For agentic workers:** Implement this plan task by task. The user explicitly authorized native end-to-end execution, testing, PR, and safe rollout.

**Goal:** Safely preview and periodically clean only proven-finished sessions across nodes, distinguish recoverable path permissions from human approvals, expose useful mobile controls, and make direct-start guarded dispatch work remotely without duplicate submissions.

**Architecture:** Extend the existing `SessionReconciler` and lifecycle store, keeping preview as the default and deletion behind repeated identity, state, and ownership checks. Add a guarded-only node-agent endpoint for remote starts. Present lifecycle states and explicit safe cleanup controls in the existing dashboard. Cleanup runs in a bounded, independently scheduled fleet pass and never depends on queue draining.

**Tech Stack:** Python, Starlette node-agent routes, SQLite lifecycle store, tmux, existing dashboard HTML/JS, pytest, Ruff.

**Spec:** User request in this task.

## Global Constraints

- Never delete attached, dirty, unmerged, protected, `UNKNOWN`, `RUNNING`, `WAITING_INPUT`, active-task, supervised, watched, leased, or bound sessions.
- Only `COMPLETED_CLEAN` and provably absent worktrees can be deleted.
- A delete requires two matching tmux identities and fresh guard checks immediately before deletion.
- Permission retries stay inside configured allowed roots and are bounded/idempotent; never auto-approve risky or secret access.
- Cleanup is preview-first, bounded, backoff-controlled, and independent of `queue.enabled`.
- Remote direct-start never silently drops an idle guard and never resends an uncertain submission.
- Do not deploy through `terminal-mcp-http.service`; only the verified fed-controller unit may be restarted.

## Review Focus

- Session replaced between preview and delete: require identical `session_id` and `created_epoch` on both reads and reclassify.
- Task/lease/binding appears during a cleanup pass: fresh deletion preflight refuses.
- Worker does not support guarded dispatch: return unsupported before sending any text.
- Worker crashes after accepting a start: durable idempotency receipt prevents a duplicate retry.
- Fleet node is slow or unavailable: bounded per-node work and backoff preserve other nodes' service.

---

### Task 1: Remote guarded direct-start

**Files:** `terminal_mcp/node_agent.py`, `terminal_mcp/node_client.py`, `terminal_mcp/core.py`, `terminal_mcp/controller.py`; tests in `tests/test_codex_direct_dispatch.py` and node-agent transport tests.

- [x] Add `POST /v1/sessions/{name}/send-guarded`, forwarding `require_idle=True` and the caller's idempotency/trace metadata to the existing core guard.
- [x] Make `RemoteNodeClient` call only this route when `require_idle=True`; old workers return unsupported without a normal-send fallback.
- [x] Test guarded remote route and idempotent receipt behavior.

### Task 2: Safe fleet cleanup and lifecycle audit

**Files:** `terminal_mcp/session_reconciler.py`, `terminal_mcp/config.py`, `terminal_mcp/server_http.py`, controller/node-client/node-agent lifecycle routes, related tests.

- [x] Add tombstone TTL, bounded node concurrency, and exponential per-node retry backoff configuration with strict validation.
- [x] Add node-local read-only lifecycle preview and identity lookup; controller fans out with a fixed worker bound and isolates per-node failures.
- [x] Keep cleanup preview-first; only `COMPLETED_CLEAN` / `ORPHAN_WORKTREE_MISSING` are eligible.
- [x] Before deleting, recheck matching `(session_id, created_epoch)`, reclassify at the delete boundary, and repeat active task, supervised task, watch, lease, binding, protected, attachment, and durable-delete guards.
- [x] Persist lifecycle tombstones with reasons and timestamps, retained for configured TTL.
- [x] Test state exclusion, identity races, cross-node failures, backpressure, queue-independent scheduling, bounded concurrency, and TTL. Windows-specific execution was not available on this Linux host.

### Task 3: Permission-aware waiting state and scoped recovery

**Files:** `terminal_mcp/status.py`, existing submit/direct-task recovery logic, tests.

- [x] Classify visible permission requests as outside allowlist, scoped read, sensitive/risky, or human approval; never approve from the classifier.
- [x] Preserve `WAITING_INPUT` and prevent supervised tasks from automatically repeating a permission prompt after it disappears.
- [x] Mark a retry eligible only for a read path resolved within configured roots; require human approval and keep risky/secrets/non-read prompts ineligible. Retries are explicit owner actions, not automatic approvals.
- [x] Test allowed-root classification, outside-root refusal, secret-path refusal, and repeated-prompt suppression.

### Task 4: Capacity audit and dashboard

**Files:** `terminal_mcp/controller.py`, `terminal_mcp/dashboard.py`, `terminal_mcp/live_sessions_page.py` as needed; tests.

- [x] Add read-only per-node capacity/cleanup preview and create-time capacity audit without force deletion.
- [x] Show idle, blocked, protected counts, skip reasons, and `Cleanup Preview` / confirmed `Cleanup Safe` controls in the live-session dashboard.
- [x] Bind cleanup to fresh server-side candidate derivation, identity checks, and deletion preflight.
- [x] Test render, API payload, and route registration; real Chromium mobile/navigation smoke passed.

### Task 5: Verify, hand off, and prepare PR/rollout

**Files:** `PROJECT_CONTEXT.md`, implementation files, plan tracking.

- [x] Run focused safety/race/cross-node tests, full pytest, real Chromium smoke, narrowed Ruff checks, and `git diff --check`. Full pytest has existing unrelated failures; broad Ruff has existing repository lint debt.
- [x] Update `PROJECT_CONTEXT.md` with code, tests, runtime evidence, rollout state, and blockers.
- [x] Commit and push the isolated branch (`837a413`); open PR #117.
- [ ] Deploy only if the actual `terminal-mcp-fed-controller.service` is present and pins the reviewed worktree via `PYTHONPATH`; this unit was absent, so no service was restarted.

