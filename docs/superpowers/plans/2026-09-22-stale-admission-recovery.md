# Stale admission recovery implementation plan

**Goal:** Old tasks with missing or inactive sessions must release global admission capacity automatically, including lanes with auto-dispatch disabled.

**Architecture:** Add a dispatch-free fleet sweep to the existing queue loop. After a configurable 900-second age, query the live session: preserve actual RUNNING work and uncertain probe failures; move missing sessions to the existing WAITING_SESSION recovery path; move idle tasks without confirmed completion to BLOCKED with a timeout reason. Commit changes with a compare-and-set guard against concurrent task updates. Never fabricate completion or send a duplicate prompt during recovery.

**Spec:** User request in this session: fix tasks stuck for too long so they reset instead of holding slots forever.

**Execution:** Inline. Existing user authorization covers implementation, deployment to HP, and recovery of the four stale tasks; no additional plan approval is needed.

- [ ] Add regression tests using a real QueueStore and controlled session observations: disabled lanes free slots, young/live tasks survive, idle verification frees capacity, races do not overwrite updated tasks, transient probe errors do not reset tasks, normal queued work then dispatches.
- [ ] Run tests red, implement engine/store/loop recovery and configurable timeout, run tests green.
- [ ] Review boundary cases and run queue/governor/config tests; independent final code review.
- [ ] Back up production files/state, deploy only changed files to HP, restart the controller without terminating tmux sessions, verify stale reservations recovered and queued tasks progress.

## Review focus

Keep opted-out lanes opted out; avoid treating stale timestamps alone as proof of inactivity; preserve concurrent cancel/completion; do not redispatch uncertain work; ensure one failed status probe does not block other recovery.

## Evidence ledger

- Baseline: 16 governor/queue-loop tests passed on HP commit 2d8082f in isolated worktree.
- Production: 4 old RUNNING/VERIFYING reservations on auto_dispatch_enabled=0 lanes; all return SESSION_LOCATION_UNKNOWN; global limit 4 blocks new work.
