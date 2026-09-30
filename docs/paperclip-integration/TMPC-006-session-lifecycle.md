# TMPC-006 - Deterministic session lifecycle for Paperclip runs

Priority: P1
Status: TODO
Depends on: TMPC-005

## Purpose

Prevent Paperclip integration from recreating the old session-sprawl problem.

## Requirements

- Deterministic session identity for adapter-managed runs.
- Reuse/resume only when the run identity matches.
- No accidental cross-project session reuse.
- Explicit cleanup/tombstone behavior.
- Bounded retry.
- Detect stale/dead session and create a replacement only with evidence.
- Preserve logs long enough for audit.
- Do not delete user-created/direct ChatGPT sessions merely because Paperclip does not know them.

Suggested naming form:

`pc-<project>-<issue-or-run>-<agent>`

The exact format may differ, but it must be stable and collision-safe.

## Session classes

Track at least:

1. `paperclip_managed`
2. `direct_chatgpt`
3. `operator_manual`
4. `legacy`

Cleanup policy must never treat these classes identically.

## Acceptance criteria

- Same Paperclip run cannot create uncontrolled duplicate sessions.
- New run can create a clean isolated session when required.
- Direct ChatGPT sessions are excluded from Paperclip cleanup.
- Cleanup is idempotent.
- Session ownership is visible in inspect/status metadata where practical.
- Crash/restart test demonstrates safe recovery.
- `PROJECT_CONTEXT.md` updated.
