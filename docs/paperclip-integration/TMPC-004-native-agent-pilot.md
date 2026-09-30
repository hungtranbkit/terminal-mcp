# TMPC-004 - Paperclip native Claude/Codex pilot

Priority: P1
Status: TODO
Depends on: TMPC-002, TMPC-003

## Purpose

Prove that Paperclip can own a real durable coding task using its native Claude/Codex path before any Terminal MCP orchestration code is retired.

## Pilot shape

Use a low-risk repository/task with a clear acceptance test.

Required flow:

```text
Paperclip issue
  -> agent assignment
  -> isolated workspace
  -> fresh branch
  -> Claude or Codex execution
  -> test/build
  -> PROJECT_CONTEXT.md update
  -> commit
  -> issue/run result
```

Run at least:
- one Codex pilot,
- one Claude pilot.

## Evidence to capture

- issue/task id,
- agent/run id,
- workspace path,
- branch name,
- start/end status,
- logs,
- test result,
- commit SHA,
- failure/retry behavior,
- ability to resume after Paperclip restart if supported.

## Acceptance criteria

- Both native agent pilots complete without using the legacy Terminal MCP queue.
- Each pilot uses a fresh branch/worktree from updated main.
- `PROJECT_CONTEXT.md` is updated before completion.
- No duplicate agent session is created for the same run.
- Failed run is clearly distinguishable from blocked/waiting/completed.
- Operator can still directly inspect the host using Terminal MCP during the run.
- Rollback procedure is documented.

## Cutover gate

Passing this task allows documentation to state:
"Paperclip is the default route for new durable local Claude/Codex project work."

It does **not** authorize removal of direct ChatGPT Claude/Codex access.

## Out of scope

- Remote nodes through Paperclip.
- Browser adapter integration.
- Deleting Terminal MCP orchestration code.
