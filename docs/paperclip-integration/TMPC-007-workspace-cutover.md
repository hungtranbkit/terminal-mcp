# TMPC-007 - Default workspace/worktree ownership cutover

Priority: P1
Status: TODO
Depends on: TMPC-004, TMPC-005, TMPC-006

## Purpose

Make Paperclip the default owner of isolated coding workspaces for durable project work and retire overlapping Terminal MCP orchestration behavior.

## Scope

For Paperclip-managed coding tasks:

- start from updated main,
- create a fresh isolated workspace/worktree,
- create a task-specific branch,
- run agent work only in that workspace,
- verify diff/test/build,
- update `PROJECT_CONTEXT.md`,
- commit and follow project merge/release policy.

Terminal MCP remains capable of executing arbitrary Git commands directly; only its **automatic project-work orchestration ownership** is retired.

## Compatibility

Direct ChatGPT work may still use a manually selected working directory/session. Direct mode must not be forced into Paperclip worktree ownership.

## Acceptance criteria

- Paperclip-managed task has exactly one intended branch/worktree unless explicitly split.
- Concurrent project tasks do not collide.
- Workspace cleanup does not delete direct/manual worktrees.
- Project context update is enforced before completion.
- Merge/deploy remains separately controlled.
- Existing Terminal MCP automated worktree scheduler, if any, is disabled only after equivalent Paperclip behavior passes tests.
- `PROJECT_CONTEXT.md` updated.

## Out of scope

- Production auto-deploy.
- Removing direct Git access.
