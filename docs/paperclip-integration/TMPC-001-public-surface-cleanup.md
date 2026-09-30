# TMPC-001 - Public orchestration surface cleanup

Priority: P0
Status: TODO
Depends on: none

## Purpose

Prevent ChatGPT and other MCP clients from accidentally selecting retired Terminal MCP queue/orchestration actions while preserving direct Terminal MCP execution.

The runtime already rejects new queue submission by default. The remaining problem is that some client-visible metadata can still advertise queue-first behavior.

## Scope

- Identify the authoritative controller/runtime that publishes the active `terminal_turn` schema and description.
- Make the active public description accurately state that direct session execution is supported and durable queue submission is retired by default.
- Remove or clearly suppress queue-only actions from normal client guidance:
  - `start`
  - `enqueue_task`
  - `long_task=true`
  - route/project/agent start paths backed by the old durable queue
  - queue rescue/run/resume submission paths
- Keep read-only historical task lookup if useful.
- Keep direct actions fully available:
  - `inspect`
  - `send`
  - `send_wait`
  - `wait`
  - `resume`
  - `list_sessions`
  - `list_nodes`
  - `create_session`
  - `delete_session`
  - browser verification/screenshot/status/stop
- Ensure client arguments cannot re-enable the queue.
- Preserve operator-only queue opt-in solely for controlled rollback/tests if still required.

## Important constraint

Do **not** remove direct `create_session` or direct ChatGPT access. This task removes misleading orchestration guidance, not low-level execution capability.

## Acceptance criteria

- Fresh MCP connection no longer recommends `start`/`enqueue_task` as the default workflow.
- A deliberate legacy queue submission still fails closed when operator opt-in is absent.
- Direct shell session creation + `send_wait` works.
- Direct Claude/Codex session creation behavior is unchanged by this task.
- Browser verify remains exposed.
- `list_nodes` and `inspect` remain exposed.
- Tests prove that no new durable queue row is created by default.
- Active controller is restarted/reloaded and the live schema is verified, not only source code.
- `PROJECT_CONTEXT.md` records the deployed result.

## Suggested code areas

- `terminal_mcp/mcp_app.py`
- `terminal_mcp/queue_policy.py`
- `terminal_mcp/orchestration_policy.py`
- generated/served MCP metadata
- service/controller deployment files
- tests covering `terminal_turn`

## Verification

At minimum:

- focused queue fail-closed tests,
- terminal_turn schema/description tests,
- direct session create/send/inspect tests,
- live MCP schema check after restart.

## Out of scope

- Installing Paperclip.
- Removing direct Claude/Codex access.
- Deleting historical queue data.
