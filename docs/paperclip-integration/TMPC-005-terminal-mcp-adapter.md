# TMPC-005 - External Paperclip adapter for Terminal MCP

Priority: P1
Status: TODO
Depends on: TMPC-002, TMPC-003, TMPC-004

## Purpose

Allow Paperclip to delegate low-level execution to Terminal MCP without making Terminal MCP dependent on Paperclip.

## Design rule

Implement the integration as an external adapter/package where practical. Avoid forking Paperclip and avoid adding Paperclip imports into Terminal MCP core execution code.

Target package/repository name:

`paperclip-adapter-terminal-mcp`

## Adapter responsibilities

Translate a Paperclip run into a bounded Terminal MCP execution request.

Supported initial fields should cover:
- controller endpoint,
- node,
- session/agent type,
- working directory,
- grant/access mode,
- prompt/command,
- timeout,
- session lifecycle strategy,
- browser verification intent if used.

## Adapter must not

- create a second durable task queue,
- poll unboundedly,
- infer completion from a label without execution evidence,
- own Git merge policy,
- hide Terminal MCP errors,
- require Terminal MCP to be reachable through a public internet endpoint.

## Result mapping

Define stable mappings such as:

- Terminal MCP `IDLE` after confirmed execution -> run completed only when completion evidence exists.
- `WAITING_INPUT` -> blocked/input-required.
- transport/node unavailable -> infrastructure failure/retryable as appropriate.
- authorization failure -> non-retryable until policy changes.
- timeout/PENDING -> bounded continuation token, not infinite polling.

Exact mapping must be documented and tested.

## Acceptance criteria

- Paperclip can invoke Terminal MCP on local Dell through the adapter.
- At least one remote-node execution path is testable when a node is online.
- Results and errors are visible in Paperclip without losing original Terminal MCP evidence.
- Direct ChatGPT -> Terminal MCP calls work in parallel and are not routed through the adapter.
- Adapter has unit tests with mocked Terminal MCP responses.
- One live end-to-end adapter smoke test passes.
- `PROJECT_CONTEXT.md` updated.

## Out of scope

- Worktree ownership cutover.
- Deleting legacy queue tables.
