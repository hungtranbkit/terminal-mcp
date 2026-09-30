# TMPC-004 - Paperclip native Claude/Codex pilot

Priority: P1
Status: DONE
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
## Final acceptance evidence — 2026-10-01

The first TER-1/TER-2 native pilot proved both adapters could execute, test, update context, and commit, but it also exposed two acceptance failures: isolated workspaces were globally disabled, so both agents used the primary checkout; and Codex was manually invoked after assignment had already auto-woken it, producing a duplicate run. Those runs are retained only as diagnostic evidence.

Root-cause correction:
- instance setting `enableIsolatedWorkspaces=true`,
- `enableIsolatedWorkspacesByDefault=false` so unrelated projects are unchanged,
- pilot project policy `defaultMode=isolated_workspace`,
- strategy `git_worktree` from `origin/main`,
- branch template `paperclip/{{issue.identifier}}-{{slug}}`,
- no manual heartbeat invoke after assignment.

Clean isolated reruns:
- Codex issue `TER-3`, run `aee5ef6f-31f1-44f5-b966-910931c2c60a`: `succeeded`, exit 0. Worktree branch `paperclip/TER-3-tmpc-native-codex-isolated-rerun`, starting at `origin/main@92bb768`, tests 25/25 passed, commit `f0227dc`. Exactly one new Codex run was created for TER-3.
- Claude issue `TER-4`, run `afb108c9-3c43-47f3-a718-77e8d873aff1`: `succeeded`, exit 0. Worktree branch `paperclip/TER-4-tmpc-native-claude-isolated-rerun`, starting at `origin/main@92bb768`, tests 25/25 passed, commit `5c55759`. Exactly one new Claude run was created for TER-4.
- Direct ChatGPT -> Terminal MCP inspection remained available during both runs.
- Neither isolated rerun pushed, merged, or deployed from its task branch.

TMPC-004 acceptance is satisfied. This authorizes Paperclip as the default route for new durable local Claude/Codex project work, while preserving direct ChatGPT Terminal MCP access for inspection, remote operations, browser verification, recovery, and hard tasks.

