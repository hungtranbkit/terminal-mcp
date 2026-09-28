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

### Deployment/merge state
- Work is implemented on `fix/disable-terminal-queue-20260928`.
- Before considering this task complete: run `git diff --check`, commit, fast-forward/merge into `main`, push, restart the active Terminal MCP service, then live-verify a mistaken queue submission is rejected while direct `create_session` + `send` still works.
