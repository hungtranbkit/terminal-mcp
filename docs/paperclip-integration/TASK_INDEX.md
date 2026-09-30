# Paperclip integration task index

| ID | Priority | Task | Depends on | Status |
| --- | --- | --- | --- | --- |
| TMPC-001 | P0 | Public orchestration surface cleanup | - | TODO |
| TMPC-002 | P0 | Local Paperclip bootstrap | TMPC-001 recommended | TODO |
| TMPC-003 | P0 | Direct ChatGPT compatibility contract | TMPC-001 | TODO |
| TMPC-004 | P1 | Native Claude/Codex pilot | TMPC-002, TMPC-003 | TODO |
| TMPC-005 | P1 | External Terminal MCP adapter | TMPC-002, TMPC-003, TMPC-004 | TODO |
| TMPC-006 | P1 | Deterministic session lifecycle | TMPC-005 | TODO |
| TMPC-007 | P1 | Workspace/worktree cutover | TMPC-004, TMPC-005, TMPC-006 | TODO |
| TMPC-008 | P1 | Hardening and recovery | TMPC-005, TMPC-006, TMPC-007 | TODO |
| TMPC-009 | P2 | Retire legacy orchestration | TMPC-001..008 | TODO |

## Recommended parallelism

Safe early parallel work:
- TMPC-002 and TMPC-003 can run in parallel after TMPC-001 direction is stable.

Do not parallelize:
- TMPC-005 before native agent pilot evidence exists.
- TMPC-009 with any earlier task.

## Agent start checklist

Before taking a task:

1. Read repository-root `PROJECT_CONTEXT.md`.
2. Read `docs/paperclip-integration/README.md`.
3. Read this index and the chosen task file.
4. Update local `main` from origin.
5. Create a fresh branch and worktree for that one task.
6. Inspect current implementation before editing; do not assume the plan is still accurate.
7. Preserve unrelated local changes.
8. Add/adjust tests for the changed capability.
9. Verify live behavior where the task explicitly requires deployment/runtime evidence.
10. Update `PROJECT_CONTEXT.md` before completion.
11. Commit, merge according to repository policy, and push only verified work.

## Direct access reminder

Paperclip becoming the default durable orchestration path does not remove direct ChatGPT -> Terminal MCP usage. Any task that breaks direct inspect/send/session/browser/node recovery is incomplete.
