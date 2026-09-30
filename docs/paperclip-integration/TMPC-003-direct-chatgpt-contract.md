# TMPC-003 - Direct ChatGPT compatibility contract

Priority: P0
Status: DONE
Depends on: TMPC-001

## Purpose

Make direct ChatGPT -> Terminal MCP support an explicit tested product contract so later Paperclip integration cannot accidentally remove it.

## Required direct capabilities

The direct surface must support, subject to normal authorization:

- fleet/node listing and health,
- session listing and inspection,
- shell session create/delete,
- direct send and send+wait,
- bounded wait/resume when explicitly requested,
- browser verify/screenshot/status/stop,
- remote-node execution,
- difficult interactive diagnostics,
- emergency recovery when Paperclip is unavailable.

### Direct AI sessions

Claude/Codex sessions may continue to be available directly for expert/manual use. Paperclip should become the **default durable project route**, not the only possible route.

If a later task introduces a restriction on direct AI session creation, it must provide an explicit operator/expert override that is:
- disabled only by policy, not by architectural removal,
- visible in documentation,
- testable,
- independent of Paperclip health.

## Deliverables

- a machine-readable or testable capability contract,
- regression tests for all required direct actions,
- routing-policy documentation that says when to use Paperclip vs direct Terminal MCP,
- a break-glass runbook.

## Acceptance criteria

- With Paperclip process stopped, the direct contract test suite passes.
- With Paperclip running, the same suite passes.
- No direct action silently creates a Paperclip issue.
- No Paperclip dependency is imported into core direct execution code.
- Direct browser and remote-node operations remain functional.
- `terminal_turn` help text explains both normal direct use and the separate role of Paperclip.
- `PROJECT_CONTEXT.md` updated.

## Out of scope

- Implementing the Paperclip adapter.
- Forcing normal project work through Paperclip at the transport layer.

## Implementation evidence — 2026-09-30

- Added `terminal_mcp/direct_contract.py`, a pure-data capability contract with no Paperclip runtime imports.
- Contract pins the canonical direct `terminal_turn` actions and standalone terminal/browser tools that must remain usable independently of Paperclip.
- Added `docs/operations/paperclip-routing.md` with the routing rule and break-glass procedure.
- Orchestration policy is now v1.3.0 and explicitly states that Paperclip is a separate optional orchestration layer, not a dependency of direct inspection, remote execution, browser verification, or recovery.
- Regression suite `tests/test_direct_contract.py tests/test_orchestration_policy.py` passed 25/25 with `paperclipai.service` active.
- The exact same suite passed 25/25 with `paperclipai.service` stopped and port 3100 closed.
- While Paperclip was stopped, direct Terminal MCP commands continued to execute successfully; Paperclip was then restored healthy on `127.0.0.1:3100`.
- Required browser and remote/session tools are asserted in the public MCP tool registry with the queue disabled.
