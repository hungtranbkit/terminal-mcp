> **Legacy queue note (2026-09-28):** durable queue submission/dispatch is retired and disabled by default. These actions are compatibility/admin surfaces only and require server-side `TERMINAL_MCP_ENABLE_QUEUE=1`; normal work uses `create_session` then direct `send`/`send_wait`.

# Authorized single queue step — 2026-09-28

Expose the existing terminal_queue_run_once as terminal_turn action queue_run_once. Require an explicit target and session input authorization. Use the same QueueEngine.tick, including coordinator, governor, leases and completion checks. No new queue, direct state edits, automatic approvals, changes to concurrency limits, or changes to Memory production.

Evidence: existing compact action baseline 86 passed. Eight new routing/target/authorization tests failed before the patch; final scoped run 334 passed (exit 0). Fresh Claude diff review: NO_BLOCKERS; reviewer did not execute tests. New native-handler tests isolate XDG_STATE_HOME. Full repository test invocation hit the 120-second limit (exit 124); not a full-suite pass.

Evidence logs on Dell: /tmp/memory-step-{baseline,red,final,review,full}.log. Recovery must use original Memory task and record an actual claim/review/dispatch result, not just QUEUED.
