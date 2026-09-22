# Work Runtime

This is the small set of CodeLocal-inspired ideas adopted into Terminal MCP without copying its architecture.

## Canonical hierarchy

`Project -> Outcome -> WorkSession -> AgentAttempt -> QueueTask -> verification evidence`

The important boundary is that an AgentAttempt is disposable. Codex, Claude, a 30B API model, or a local model may take over the same WorkSession. Provider conversation IDs and tmux panes are continuity aids, not canonical state.

## Context compiler

`context_compiler.compile_context()` accepts already-resolved lanes and produces a deterministic bounded packet. Mandatory rules fail closed on overflow. Exact duplicate rules are compacted once. Soft lanes drop whole low-priority items rather than truncate a rule halfway. The packet fingerprint is recorded on an attempt so a handoff can prove which context version it received.

Recommended lane order: mandatory policy/rules; current task/acceptance state; verification facts; verified experience; relevant code context.

## Verified experience

`verified_experiences` is intentionally not a transcript store. A record requires evidence and is redacted before persistence. Use it for a bug fix that actually passed verification, a known failure mode with reproduction evidence, or a runbook step that has been proven. Ordinary chat, guesses and terminal chatter stay in existing transcript/session-knowledge stores.

## Compatibility

The runtime adds its own tables with `IF NOT EXISTS` and does not alter QueueStore statuses, dispatch semantics, recovery, or existing tmux sessions. Existing sessions therefore behave exactly as before until a caller explicitly creates a WorkSession. This is the intended rollout path: enable it first for `*-work` lanes, then connect queue dispatch/context retrieval after live verification.

## Next integration slice

1. Add MCP/service methods for create/get/handoff/compile-context.
2. Link QueueTask metadata to `work_session_id` without changing task IDs.
3. On verified task/outcome completion, promote only evidence-backed lessons to `verified_experiences`.
4. Build context lanes from WORK_POLICY/AGENTS rules, queue/outcome state, verified experience and repowise/code retrieval.
5. Add provider profiles so Codex/Claude/30B/local adapters receive the same compiled packet.
6. Keep production auto-dispatch opt-in and preserve the current merge/deploy approval gates.
