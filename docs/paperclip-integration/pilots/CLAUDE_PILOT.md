# Native Claude pilot (Paperclip TER-2)

Low-risk integration pilot proving a native Claude agent run driven by Paperclip
can read this repository's context, execute verification, reconcile with a
concurrent agent run, and land a commit without touching production services.

## Result

Executed successfully.

## Observed environment

- Date: 2026-09-30
- Paperclip issue: `TER-2` — "TMPC native Claude pilot"
- Agent: Claude Pilot (`84c7bbdd-311e-44ec-ae91-06c0652ffa87`)
- Workspace: `/home/dell/workspace/terminal-mcp`
- `PAPERCLIP_WORKSPACE_STRATEGY`: `project_primary`
- Branch observed: `main`
- Git HEAD observed at pilot start: `8c4ff347e8635a8968e70b1b0f7fadedc00d03cf`
  (`feat(lifecycle): {TMCP_TASK_ID} placeholder so shell steps can self-complete`)
- Git HEAD this pilot commit was rebased onto: `575afb923d6ea0d9157bee8c2e36f486d5c0e28f`
  (`feat(paperclip): preserve direct Terminal MCP contract`)

### Concurrency: this is a shared workspace, not an isolated one

The issue asked for an isolated workspace, but Paperclip provisioned this run with
`PAPERCLIP_WORKSPACE_STRATEGY=project_primary` — the primary checkout, held
concurrently by run `24ad3b3c-0cbf-4da3-a014-5a01e2b6a756` (issue `TER-1`).

That run pushed TMPC-002 and TMPC-003 (`beed9da`, `575afb9`) to `origin/main`
mid-pilot, which rejected the first push and produced a `PROJECT_CONTEXT.md`
conflict. The pilot resolved it by rebasing onto `origin/main` and keeping both
handoff sections, staging only its own two files and leaving the pre-existing
dirty entries (`.projectflow/knowledge/KNOWLEDGE_STATE.json`, untracked
`.claude/`) alone. Coordination worked, but only because it was done by hand.

Follow-up for the integration epic: if pilots are meant to run isolated, the
Paperclip project/repository config needs a worktree- or clone-based workspace
strategy rather than `project_primary`. Filed against TMPC-007 (workspace cutover).

## Verification

`tests/test_direct_contract.py` did **not** exist at the pilot's starting commit
`8c4ff34` — the requested command aborted with
`ERROR: file or directory not found: tests/test_direct_contract.py` and ran
nothing. It was created by the concurrent TER-1 run in `575afb9` alongside
`terminal_mcp/direct_contract.py`. After rebasing onto that commit, the command
from the issue ran as written:

```
.venv/bin/python -m pytest -q tests/test_direct_contract.py tests/test_orchestration_policy.py
# 25 passed in 2.16s
```

Against the pre-rebase tree the pilot had also run the nearest existing
direct-path equivalents, to avoid reporting an unverified pilot:

```
.venv/bin/python -m pytest -q \
  tests/test_orchestration_policy.py \
  tests/test_direct_task_lifecycle.py \
  tests/test_contract_handshake.py
# 44 passed in 2.58s
```

Notes for future agent runs on this host:

- Bare `python` and `timeout` are not on the agent shell's PATH; use
  `.venv/bin/python`.
- `tests/conftest.py` repoints `XDG_STATE_HOME` at a fresh temp directory during
  `pytest_configure`, so this run did not read or migrate the live
  `~/.local/state/terminal-mcp/*.db` state.

## Production safety

No deployment, no service restart, no configuration change. `terminal-mcp-http`
and the node agents were not touched, and no queue rows were created — the
legacy durable queue stays retired per `PROJECT_CONTEXT.md`.
