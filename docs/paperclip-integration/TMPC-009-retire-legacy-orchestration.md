# TMPC-009 - Retire legacy Terminal MCP orchestration

Priority: P2
Status: TODO
Depends on: TMPC-001 through TMPC-008 complete

## Purpose

Remove or permanently quarantine redundant orchestration only after Paperclip replacement paths and direct ChatGPT compatibility are proven.

## Candidate retirement scope

Review and retire, where fully replaced:

- durable queue submission paths,
- dispatcher/follower behavior used only for project task orchestration,
- queue rescue scheduling,
- project/agent task ownership abstractions duplicated by Paperclip,
- automatic worktree ownership duplicated by Paperclip,
- obsolete watcher loops,
- client-facing queue-first help/schema,
- dead configuration and operational scripts.

Keep where still valuable:

- read-only migration/history inspection,
- low-level session execution,
- direct ChatGPT session control,
- node-agent transport,
- remote execution,
- browser harness,
- health/resource inspection,
- logs/audit evidence needed for operations.

## Required deletion discipline

Do not delete a component simply because its name contains "queue" or "scheduler". Trace call sites and prove it is not required by direct execution, browser operations, node recovery, or compatibility.

## Migration data

Before deleting queue/history storage:
- document retention requirement,
- export or archive if necessary,
- verify no current runtime reads it,
- provide one final migration note.

## Acceptance criteria

- No duplicate orchestration engine runs by default.
- Public Terminal MCP surface is compact and direct-execution focused.
- Paperclip owns durable project orchestration.
- Direct ChatGPT contract suite passes with Paperclip on and off.
- Existing operational/browser/remote-node tests pass.
- Migration/rollback notes updated.
- `PROJECT_CONTEXT.md` updated.
- Final architecture diagram reflects reality, not intended future state.
