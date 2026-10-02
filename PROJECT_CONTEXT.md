# PROJECT_CONTEXT.md

## Verified current state — 2026-09-28

Terminal MCP durable queue submission is retired for normal coding work.

### Operational workflow
- Normal coding dispatch is direct: `create_session` -> `send` / `send_wait`.
- Do not use `terminal_turn(action="start")`, `enqueue_task`, `route_start`, `agent_start`, `project_start`, aliases such as `run` / `dispatch` / `enqueue`, or `send(long_task=true)`.
- Queue/task history and cleanup remain available so historical or already-running tasks can be inspected/cancelled safely.
- Finished task sessions/worktrees/branches should be cleaned up after verification and merge.

### Queue disable implementation
- `terminal_mcp/queue_policy.py` is the shared fail-closed policy.
- Queue-producing compact actions are blocked in `terminal_mcp/compact_tools.py`.
- Native MCP submit/route/manual-dispatch entrypoints are guarded in `terminal_mcp/mcp_app.py`.
- Default failure code: `QUEUE_DISABLED_USE_DIRECT_SESSION`.
- Only a server operator can deliberately re-enable legacy submission with `TERMINAL_MCP_ENABLE_QUEUE=1`; client arguments cannot enable it.
- Queue DB/schema/engine are intentionally retained for historical tasks and compatibility tests.

### Documentation
- `terminal_mcp/orchestration_policy.py` policy version is 1.2.0 and directs clients to direct session send.
- `docs/CHATGPT_ORCHESTRATION_POLICY.md` is regenerated from that source.
- `terminal_turn` help text now describes direct-session workflow and queue retirement.

### Verification
- Compact/direct/session + queue-policy suite: 138 passed.
- Post-main-sync compact/native/policy/queue suite: 215 passed, 8 deselected.
- Native fail-closed regression proves terminal_enqueue_task and terminal_route_start create no queued row when server opt-in is absent.
- Legacy queue engine tests opt in with `TERMINAL_MCP_ENABLE_QUEUE=1`; only inside tests.

### Live deployment/merge state
- Queue-disable implementation was merged to local `main` and pushed; Terminal MCP HTTP was restarted from `/home/dell/workspace/terminal-mcp`.
- `terminal-mcp-http.service` is active and its unit/environment does not set `TERMINAL_MCP_ENABLE_QUEUE`.
- Live mistaken `terminal_turn(action=start)` returns `QUEUE_DISABLED_USE_DIRECT_SESSION` with `next_action=create_session_then_send`.
- Queue DB row count stayed `21 -> 21` across that rejected live submission, proving no row was persisted.
- The last historical queue record (`memory-knowledge-complete-claude-0928`) became `QUEUED` after restart and was cancelled; active queue records are now `0`. The Memory session itself was not killed.
- Live direct flow was verified with a disposable shell: `create_session` followed by direct `send_wait` returned `DIRECT_OK`; the disposable session was then exited.
- The unrelated root worktree modification `.projectflow/knowledge/KNOWLEDGE_STATE.json` was intentionally left untouched.
- Temporary queue-disable branch/worktree/session are removed after this context update is merged; `main` is the canonical code line.

## Direct-session lifecycle hotfix — 2026-09-30

### Root cause
- IDLE/composer was the only completion signal for direct work: after every shell command or agent turn the classifier (correctly) reports IDLE, `send_wait`/`wait` MATCHED on it, and nothing on the server kept a long task going. "Turn ended" was read as "task done".
- Every `wait`/`send_wait` created a durable `wait_*` continuation (`run_journal.db`, project `terminal-wait`) that was only finalized by a resume observing MATCHED/FAILED. Clients that started a new wait instead of resuming left the old one PENDING for its 7-day TTL (expiry was checked lazily, never written). 521 were PENDING live (288 on `offlinepos-test-deploy-0930`), and `deletion_preflight` counted them as active runs -> `SESSION_HAS_ACTIVE_RUN` blocked delete.

### Fix (branch `fix/session-lifecycle-continuation-20260930`)
- `terminal_mcp/direct_task.py` (new): supervised direct task. `terminal_turn action=supervise target=<session> text=<goal>` (or `args.steps=[...]`) dispatches directly, then a server-side loop (`DirectTaskLoop`, 3s, started unconditionally in `server_http.py`; acts only on explicitly started tasks) re-dispatches after each settled IDLE: next step, else a bounded continuation prompt (agent mode; default 6, cap 30, `args.max_continuations`). Ends only on explicit `TMCP-DONE:<task_id>` / `TMCP-BLOCKED:<task_id> reason` marker line in the pane, `supervise_complete`, `supervise_cancel`, WAITING_INPUT (pauses; resumes when the prompt clears), send failures (3 -> BLOCKED), or the continuation limit (FAILED `CONTINUATION_LIMIT_REACHED`). Shell mode never sends "continue"; when steps run out it holds WAITING_INPUT `AWAITING_EXPLICIT_COMPLETION`. Steps may contain `{TMCP_TASK_ID}`, so a final shell step `printf 'TMCP-%s:%s\n' DONE {TMCP_TASK_ID}` self-completes.
- Instruction text never contains the contiguous marker, so an echoed prompt cannot complete its own task. Mode auto-detects agent (claude/codex in status reason) vs shell; override `args.mode`.
- State in its own DB `~/.local/state/terminal-mcp/direct_tasks.db` (prompts stored there, never in the content-free run journal). One active supervised task per target; `idempotency_key` dedupes start. All sends/status/tail go through CompactTerminalTools' guarded, bounded paths.
- `inspect`/`wait`/`send_wait`/`resume` responses on a target with an active supervised task carry `supervised_task` + `business_complete: false`.
- `run_journal.py`: new wait supersedes older PENDING waits on the same target (`SUPERSEDED`); `reap_stale_waits()` expires PENDING waits untouched for 15 min (TTL now 1h) — run by the loop every 60s and by deletion preflight; passive waits excluded from `active_for_session`/`active_session_index`; `cancel_waits_for_target()`.
- `session_deletion.py`: preflight reaps stale waits, blocks only on real work (queue task, non-wait run, active supervised task -> `SESSION_HAS_ACTIVE_SUPERVISED_TASK`), and on success cancels the session's PENDING waits (`references.cancelled_waits`).
- Queue stays retired; no queue rows are created. Dashboard delete routes (`dashboard.py`, `webauth_dashboard.py`) get the wait fix but do not yet check supervised tasks (they don't receive the supervisor) — follow-up.

### Verification
- `tests/test_direct_task_lifecycle.py` 13 passed (IDLE-not-DONE + resume until marker, echo safety, limit, WAITING_INPUT, BLOCKED, shell steps, send-failure cap, restart survival, wait supersede/reap, delete preflight with 300 orphan waits).
- Focused suites (compact tools/turn actions/delete safety/run journal/sidecar/orchestration policy): 298 passed.
- Pre-existing on `main` 677729b, unrelated: `test_server.py::test_server_registers_v1_and_binding_tools`, `test_transports.py::test_stdio_real_handshake_and_tools`, `::test_http_real_handshake_tools_and_security` (instructions-text assertion) fail identically without this change.

## Planned Paperclip control-plane integration — 2026-09-30

A task epic was added under `docs/paperclip-integration/` to migrate default durable project orchestration to Paperclip while preserving Terminal MCP as a first-class direct ChatGPT execution surface.

### Architectural decision

- Paperclip is the planned owner of durable project/issue lifecycle, default agent assignment, budget/governance, and default isolated coding workspaces.
- Terminal MCP remains independently and directly usable by ChatGPT for low-level execution, inspection, difficult diagnostics, recovery, remote nodes, browser verification, and explicit expert/manual sessions.
- The migration must not make Terminal MCP dependent on Paperclip availability.
- Direct `terminal_turn` access is a compatibility contract, not a temporary migration artifact.
- Legacy durable queue/dispatcher behavior remains retired by default and must not be reintroduced inside the Paperclip adapter.
- Capability retirement is gated: install replacement, prove end-to-end behavior, prove recovery, preserve direct compatibility, then disable only the overlapping orchestration capability.

### Planned task sequence

See `docs/paperclip-integration/TASK_INDEX.md` and `docs/paperclip-integration/README.md`.

The planned slices are TMPC-001 through TMPC-009: public surface cleanup, Paperclip bootstrap, direct ChatGPT contract, native agent pilot, Terminal MCP adapter, deterministic session lifecycle, workspace cutover, hardening/recovery, then final legacy-orchestration retirement.

No Paperclip runtime integration is claimed complete by this planning commit.

## Paperclip integration progress — 2026-09-30

TMPC-001 public orchestration cleanup is implemented on main. Direct ChatGPT -> Terminal MCP remains a first-class path. Normal clients no longer receive queue-first guidance, and the public MCP catalog suppresses retired queue submission/runner tools when the server operator has not opted the queue back in. The legacy implementation remains available only for controlled rollback/tests via `TERMINAL_MCP_ENABLE_QUEUE=1`. Focused contract suite: 175 passed. Next slice: TMPC-002 local Paperclip bootstrap.

## Paperclip TMPC-002 bootstrap — 2026-09-30

TMPC-002 is live on Dell Linux. Paperclip `2026.916.1` uses instance `default` and `paperclipai.service`, binds only to `127.0.0.1:3100`, and has a pilot company/project (`Terminal MCP Pilot` / `TMPC Integration Pilot`) pointing at this repository. Restart persistence was verified. Direct ChatGPT -> Terminal MCP execution was explicitly verified while Paperclip was stopped, so Paperclip is not a dependency of Terminal MCP. Host Codex and Claude prerequisites/auth state are present. Operational scripts and runbook live in `scripts/paperclip-bootstrap.sh`, `scripts/paperclip-health.sh`, `config/paperclip.env.example`, and `docs/operations/paperclip.md`. Next integration slice is TMPC-003 direct-compatibility contract, followed by TMPC-004 native agent pilot.

## Paperclip TMPC-003 direct contract — 2026-09-30

TMPC-003 establishes an explicit independence boundary between Paperclip and Terminal MCP. `terminal_mcp/direct_contract.py` is the machine-readable contract for direct actions/tools; `docs/operations/paperclip-routing.md` documents routing and break-glass recovery. Orchestration policy v1.3.0 says Paperclip is an optional separate layer for project/issue orchestration, while direct Terminal MCP remains first-class for inspection/debugging, remote-machine operations, browser verification, targeted commands, and recovery. The same direct-contract/policy suite passed 25/25 both with Paperclip active and with Paperclip stopped. Paperclip was restored healthy after the stopped-mode test. Next slice is TMPC-004 native Claude/Codex pilot through Paperclip.

## Native Claude pilot (Paperclip TER-2) — 2026-09-30

A native Claude agent driven by the Paperclip control plane ran end-to-end
against this repository and succeeded. Full record:
`docs/paperclip-integration/pilots/CLAUDE_PILOT.md`.

- Observed branch `main`, HEAD `8c4ff34` at pilot start; rebased onto `575afb9`
  after the concurrent TER-1 run pushed TMPC-002/TMPC-003 mid-pilot.
- Verification: `pytest -q tests/test_direct_contract.py tests/test_orchestration_policy.py`
  -> **25 passed** on the rebased tree. Before the rebase,
  `tests/test_direct_contract.py` did not exist (it arrived with `575afb9`), so the
  pilot had also run the nearest equivalents — `tests/test_orchestration_policy.py`,
  `tests/test_direct_task_lifecycle.py`, `tests/test_contract_handshake.py`
  -> 44 passed.
- Nothing was deployed; no service, config, or queue row was touched.

### Facts future agents must preserve

- Paperclip provisioned this run with `PAPERCLIP_WORKSPACE_STRATEGY=project_primary`,
  i.e. the **shared primary checkout** at `/home/dell/workspace/terminal-mcp`, held
  concurrently by another Paperclip run. It is *not* an isolated workspace: TER-1 and
  TER-2 ran in the same tree at the same time and collided on `PROJECT_CONTEXT.md`.
  Agents on this path must stage only files they authored, fetch before pushing, and
  rebase rather than force-push.
- Unfinished work: to get real isolation for Paperclip coding tasks, the project's
  repository config needs a worktree/clone workspace strategy. Tracked as a
  follow-up for TMPC-007 (workspace cutover).
- Bare `python` and `timeout` are not on the Paperclip agent shell's PATH; use
  `.venv/bin/python`.
- `tests/conftest.py` repoints `XDG_STATE_HOME` at a temp dir in `pytest_configure`,
  so running the suite no longer migrates live `~/.local/state/terminal-mcp/*.db`.
- The unrelated dirty entries `.projectflow/knowledge/KNOWLEDGE_STATE.json` and
  untracked `.claude/` were deliberately left uncommitted, as before.
## Paperclip TMPC-004 native-agent gate — 2026-10-01

TMPC-004 is accepted after isolated reruns. Paperclip native Codex (TER-3/run aee5ef6f..., commit f0227dc) and Claude (TER-4/run afb108c9..., commit 5c55759) each completed on a dedicated git worktree based on origin/main, passed 25/25 focused tests, updated PROJECT_CONTEXT.md, and committed without pushing/merging/deploying. Root cause of the earlier shared-checkout pilots was instance-level enableIsolatedWorkspaces=false; it is now true while enableIsolatedWorkspacesByDefault remains false, so only projects with explicit isolation policy change behavior. Assignment already auto-wakes agents; do not also invoke heartbeat manually or duplicate runs can occur. Paperclip may now be treated as the default route for durable local Claude/Codex project work, but direct ChatGPT -> Terminal MCP remains first-class and independent. Next integration slice is TMPC-005 external Terminal MCP adapter.


## Agent-session lifecycle cleanup — 2026-10-02

### Root causes
- Nothing ever closed a finished **agent** pane: the shell reaper (`core._idle_reap_candidates`, external `~/.local/bin/session_guard.py` timer) only closes detached panes at a plain shell prompt, so merged/handed-off claude/codex sessions stayed resident for days (Dell: 22/31 GB RAM, 16/24 GB swap).
- Delete confirmation mismatch: `terminal_turn(action=delete_session)` help never mentioned `args={"confirm": true}` and the legacy `terminal_delete_session` sidecar translation dropped `confirm`, so most compact deletes hit `CONFIRMATION_REQUIRED` (audit shows repeated refusals). Internal callers were also broken: `_reclaim_idle_sessions` (capacity reaper) and `stale_sessions.cleanup_session` called delete without `confirm` and could never succeed.

### Fix
- `terminal_mcp/session_reconciler.py` (new): `classify()` + `SessionReconciler` + `SessionReconcileLoop`. Closes only `COMPLETED_CLEAN` (detached, IDLE at composer per `status.classify_status`, idle >= `idle_hours`, no active queue task/supervised task/run/lease, clean tree, HEAD contained in origin/HEAD|origin/main|origin/master|main|master, linked worktree — primary checkout only with `close_clean_primary_checkouts`) and `ORPHAN_WORKTREE_MISSING` (cwd proven absent). Preserved with reason: PROTECTED, ATTACHED, SERVICE (any non-agent command: pnpm, cloudflared, ssh…), SHELL, ACTIVE_TASK, AGENT_RECENT, AGENT_NOT_IDLE, UNKNOWN_CWD, NO_REPO, DIRTY_WORKTREE, NO_BASE_REF, UNMERGED_BRANCH, IDLE_PRIMARY_CHECKOUT. Each close re-classifies fresh, runs `deletion_preflight`, saves scrollback to `~/tmux-reaped/`, deletes via `terminal_delete_session(confirm=True, requested_by="session-reconciler")`, unwatches supervisor, audits `reconcile_agent_session`. Idempotent; bounded by `max_closes_per_run`.
- Pane state, not tmux `activity_epoch`, is the real guard: Ink agent CLIs can show a 15h-old activity stamp while actively "Working".
- Config: `session_lifecycle.agent_cleanup` (`AgentCleanupConfig` in `config.py`; default disabled + dry_run). Loop started in `server_http.py` only when enabled.
- Inspection/actions: MCP tools `terminal_session_lifecycle(session=None)` (read-only classification) and `terminal_session_reconcile(dry_run=True, confirm=False)` (real pass needs `dry_run=false` + `confirm=true`); compact `terminal_turn` actions `session_lifecycle` (target -> session) and `session_reconcile` (args `dry_run`, `confirm`).
- Confirmation: help documents `args={"confirm": true}`; `CONFIRMATION_REQUIRED` now returns `next_action`; sidecar maps legacy `confirm` -> `args.confirm`; capacity reaper and `stale_sessions.cleanup_session` pass `confirm=True`. Confirmation never bypasses attached/lease/recovery/protected/active-work blockers.

### Verification
- `tests/test_session_lifecycle_reconciler.py` (18): clean completion closed, dirty/unmerged preserved, active/protected/attached/service/shell/recent/thinking preserved, orphan closed, primary-checkout opt-in, non-repo, dry run, idempotency, preflight refusal, per-run cap, config parsing, confirmation paths (core hint, reaper, sidecar, compact routing).
- Related suites (sidecar, direct contract, policy, direct task, session lifecycle/delete safety, compact, server, transports, observer, capacity, stale cleanup): all pass except 3 pre-existing failures identical on unmodified main 4a998d0 (`test_transports.py` x2 instructions text, `test_observer.py::test_observer_exposes_no_tool_that_can_change_anything` terminal_enqueue_task).

### Deployment + live verification (Dell, 2026-10-02)
- Commit `e3ab229` fast-forwarded to `main` and pushed. `terminal-mcp-http.service` restarted (KillMode=process; tmux server is NOT in its cgroup — it lives in `terminal-node-agent.service`'s cgroup, so the node agent was deliberately NOT restarted). `/version` = `e3ab229…`, `/health/ready` ready, tunnels + node agent active, all 15 sessions kept their created timestamps.
- Effective config is `~/.config/terminal-mcp/runtime-config.yaml` (systemd drop-in overrides repo `config.yaml`); backup `runtime-config.yaml.bak-20261002-lifecycle`. Added `session_lifecycle.agent_cleanup: {enabled: true, dry_run: false, idle_hours: 2, interval_seconds: 900, max_closes_per_run: 10, agent_commands: [claude, codex], close_clean_primary_checkouts: false}`. Journal: `agent session reconciler started (dry_run=False, idle_hours=2.0, interval=900.0s)`.
- Live MCP (`127.0.0.1:8766/mcp`): disposable `tmcp-confirm-verify2-1002` — unconfirmed delete -> `CONFIRMATION_REQUIRED` + `next_action`; attached + `args.confirm=true` -> `SESSION_ATTACHED`; detached + confirm -> `deleted: true`; repeat -> `already_gone`. `session_reconcile` without confirm -> `CONFIRMATION_REQUIRED`.
- Live dry run classified 15 sessions; only `nf68-refresh-0930` and `nf73-refresh-0930` (novafactory linked worktrees, clean, branch contained in origin/main) were `COMPLETED_CLEAN`. Real pass closed exactly those two (audit `reconcile_agent_session` actor `session-reconciler`; scrollback `~/tmux-reaped/2026-10-02T09380{4,5}-nf*-refresh-0930.log`); immediate re-run closed nothing. Their worktrees were left in place (novafactory project, not ours).
- Resources: sessions 15 -> 13; RAM used 22423 -> 21664 MB (available 8757 -> 9516 MB); swap used 15776 -> 15736 MB (the two claude trees were ~233 + ~207 MB RSS).

### Open follow-ups
- HP controller (`hp-net`, `/home/kimex/workspace/terminal-mcp`) still runs `f40e6a2`, 15 commits behind main (also missing queue retirement / direct-task lifecycle / Paperclip contract). Not deployed here: its tmux server is inside `terminal-mcp-http.service`'s cgroup (KillMode=process) with live sessions; needs a deliberate deploy decision. agent_cleanup defaults off there.
- Dell node agent process still runs pre-`e3ab229` code (only affects the `next_action` hint on node-agent-routed deletes); restart only when tmux cgroup placement is handled.
- Preserved-but-idle sessions worth a human look: `nf21-run-0930`, `nf32-run-0930`, `nf52-run-0930`, `offlinepos-disabled-controls-finish-1001` (IDLE_PRIMARY_CHECKOUT, clean/merged), `memory-knowledge-complete-claude-0928` (DIRTY_WORKTREE), `social-commerce-p0-1001` (NO_REPO). Set `close_clean_primary_checkouts: true` only if primary-checkout agents should also auto-close.
- Dashboard delete routes still don't check supervised tasks (from 2026-09-30).
