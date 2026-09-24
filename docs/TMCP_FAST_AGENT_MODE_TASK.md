# TMCP-FAST-AGENT-001 — Fast Agent Mode

Status: READY
Priority: P0
Scope: terminal-mcp
Created: 2026-09-24

## Goal
Reduce perceived coding latency by minimizing ChatGPT↔terminal round trips while preserving durable queueing, restart safety, permissions, auditability, and browser verification.
Target: representative coding tasks should need about 2–4 orchestration round trips instead of 10–30 small inspect/send/wait loops.

## Audit findings
- MCP transport is already fast: list_sessions ~0.17s; simple send_wait internal wait ~0.07s. Main latency is orchestration/model round trips.
- State detector is too uncertain: audit sample 8 sessions, 5 UNKNOWN; some visibly had completion markers or idle agent prompts.
- Workload placement is imbalanced: dell-linux had 8 tmux sessions while hp-linux and m910 were effectively idle.
- compact inspect still returns many repeated null/UNKNOWN fields.
- Long work must stay durable/server-driven; do not solve this by increasing synchronous MCP timeouts.

## P0-A — Session state detector v2
Strong signals must beat weak heuristics.
- explicit Terminal MCP completion marker => COMPLETED/IDLE
- visible idle coding-agent prompt with no active child work => IDLE
- restored shell prompt with no active child process => IDLE
- explicit approval/question/permission prompt => WAITING_INPUT
- active child process/recent execution evidence => RUNNING
- UNKNOWN only as last-resort fallback
Do not rely primarily on tmux current-command.
Tests: completed agent, idle agent, shell completion, long process, waiting input, stale foreground metadata, legitimately ambiguous UNKNOWN.
Acceptance: known idle/completed fixtures never UNKNOWN; safety/permission behavior unchanged; batch inspect compatible.

## P0-B — Autonomous coding worker loop / Fast Agent Mode
Add durable bounded loop: inspect/reproduce -> diagnose -> edit -> test -> retry -> verify -> finish/blocker.
Reuse existing durable task/queue primitives; do not create a parallel scheduler.
Task contract must support: goal, repo/cwd, scope, allowed/forbidden ops, verification, completion conditions, bounded retry/iterations.
Safe stop states: DONE, BLOCKED, NEEDS_DECISION, WAITING_INPUT, FAILED.
Destructive actions, production deploys, privileged operations, and out-of-policy actions must keep existing authorization gates.

## P0-C — Structured completion result
Return compact machine-readable result containing: status, summary, root_cause, changed_files, tests, verification, commit, blocker, requires_user_action.
Keep full logs addressable by task_id; do not return full logs by default.

## P1-A — Compact inspect v2
When compact=true omit repeated empty/null/UNKNOWN subtrees unless operationally relevant.
Preferred fields: session, state, node, cwd, branch, dirty, last_activity_s, input_required.
Keep detailed/full diagnostics and preserve compatibility through versioning or feature gating if required.

## P1-B — RepoContext cache
Cache repo path, branch, package manager, test/lint/build commands, framework/runtime, confidently detected service/dev port, dirty state, and invalidation timestamp/hash.
Invalidate conservatively when project metadata changes.

## P1-C — Delta checkpoint
Persist goal, findings, files read/changed, tests, failures, next action, repo/branch/commit, task_id.
Resume from checkpoint + current git delta instead of replaying full terminal history.

## P1-D — Capability/load-aware routing
Route using agent availability, repo/data locality, OS/platform, browser/docker/service capabilities, health, load/session count, draining/offline state.
Do not migrate repo-bound work to a node that cannot safely access the workspace.
Tests: capability mismatch, offline node, overloaded vs idle equivalent nodes, repo-locality constraint.

## Non-goals / safety
- Do not replace existing durable queue with a second queue.
- Do not increase long synchronous MCP timeout as primary fix.
- Do not weaken auth, cwd, protected-session, permission, or destructive-action guards.
- Do not rewrite unrelated Archify/dashboard code.
- Do not modify .projectflow/knowledge/KNOWLEDGE_STATE.json.
- Do not clean unrelated dirty work.

## Compatibility
Existing terminal_turn actions must continue working: start, inspect, send, send_wait, wait, resume, list_sessions, list_nodes, task/task_batch_status, browser_verify/browser_screenshot/browser_status.

## Telemetry
Measure where practical: orchestration round trips/task, task duration, UNKNOWN classifications, retry count, selected node+routing reason, compact result payload size. Avoid sensitive/high-cardinality payload logging.

## Delivery slices
1. State detector v2 + tests
2. Structured result contract
3. Autonomous worker loop on existing durable task primitives
4. Compact inspect v2
5. RepoContext + delta checkpoint
6. Capability/load-aware routing
7. Telemetry + docs + benchmark

## Definition of done
- One coding task can be submitted once and progress server-side without ChatGPT polling every shell step.
- Common success returns one compact structured result.
- Real blockers surface as BLOCKED/NEEDS_DECISION rather than indefinite UNKNOWN.
- Known idle/completed agent sessions classify deterministically.
- Queue durability/restart recovery and authorization protections still pass.
- Focused and appropriate broad tests pass.
- Docs explain how ChatGPT invokes Fast Agent Mode and when not to use it.
- Before/after benchmark demonstrates fewer orchestration round trips.

## Agent instructions
Read this entire task before editing. Inspect current queue/task/state abstractions first and reuse them.
Main repo may be dirty: use a dedicated branch/worktree and preserve unrelated changes exactly.
For each slice: add tests, avoid unrelated refactors, checkpoint progress, commit cleanly where practical.
Stop only on a genuine external blocker.
Final report: commits, changed modules, test results, before/after benchmark, remaining risks/follow-ups.
