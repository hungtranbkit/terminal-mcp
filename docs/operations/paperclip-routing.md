# Paperclip vs direct Terminal MCP

Paperclip and Terminal MCP are complementary layers, not a replacement chain.

## Routing rule

Use **Paperclip** when the unit of work is a project/issue that benefits from
assignment, heartbeats, budgets, governance, or managed workspaces/worktrees.

Use **direct Terminal MCP** when ChatGPT needs to inspect or repair a machine,
debug a live process, operate a remote node, verify a browser flow, perform a
small targeted command, or recover from an orchestration failure.

Direct Terminal MCP is intentionally independent of Paperclip. A Paperclip
outage must not remove ChatGPT's terminal, node, session, task-history, or
browser-verification capabilities.

## Direct contract

The machine-readable source is `terminal_mcp/direct_contract.py`. It defines
the canonical `terminal_turn` actions and standalone tools that must remain
available even when Paperclip is stopped.

Normal direct actions include:

- `inspect`, `send`, `send_wait`, `wait`, `resume`
- list/create/delete sessions and list nodes
- read historical task/task-batch status
- `browser_verify`, screenshot, status, and stop

Retired queue/orchestration submission actions are not part of this contract.

## Break-glass procedure

If Paperclip is unhealthy or an agent workflow is stranded:

1. Do **not** enable the retired Terminal MCP queue.
2. Use direct `terminal_turn(action=inspect, ...)` to establish current state.
3. Reuse the existing session when safe; otherwise create one direct session.
4. Use `send` or `send_wait` for the smallest recovery action.
5. Use browser tools directly when UI verification is required.
6. Record the recovery in the affected repository's `PROJECT_CONTEXT.md`.
7. Repair Paperclip separately; direct recovery must not create a Paperclip
   issue or depend on the Paperclip API.

## Regression check

Run:

```bash
pytest -q tests/test_direct_contract.py tests/test_orchestration_policy.py
```

For the full acceptance test, run the same command once with
`paperclipai.service` active and once while it is stopped, then restore the
service. The result must be identical.
