# Paperclip integration epic

Status: PLANNED
Owner: Terminal MCP maintainers
Created: 2026-09-30
Target: migrate orchestration responsibilities to Paperclip without removing direct ChatGPT -> Terminal MCP access.

## Goal

Use Paperclip as the default control plane for durable project work while keeping Terminal MCP as a first-class direct execution surface for ChatGPT.

The migration must reduce duplicate schedulers, queues, task ownership, and worktree orchestration. It must **not** turn Terminal MCP into a Paperclip-only private backend.

## Architectural invariant

Two supported entry paths remain after migration:

```text
Path A - normal durable project work

ChatGPT / operator
        |
        v
    Paperclip
        |
        +--> codex_local / claude_local
        |
        +--> terminal_mcp adapter
                 |
                 v
          Terminal MCP execution
```

```text
Path B - direct expert / break-glass work

ChatGPT
   |
   v
Terminal MCP
   |
   +--> inspect / send / send_wait / wait / resume
   +--> list/create/delete sessions
   +--> node and remote-machine execution
   +--> browser verification / screenshots
   +--> direct shell, Claude, or Codex session when explicitly appropriate
```

Path B is intentionally retained for difficult diagnostics, recovery, remote-machine operations, interactive investigation, and cases where routing through Paperclip would add friction or hide useful low-level evidence.

## Ownership after migration

| Capability | Owner |
| --- | --- |
| Durable project/task/issue lifecycle | Paperclip |
| Project-level assignment and dependency graph | Paperclip |
| Default Claude/Codex durable work | Paperclip |
| Budget/cost/governance | Paperclip |
| Default isolated execution workspace/worktree | Paperclip |
| tmux / ConPTY / node-agent transport | Terminal MCP |
| Direct shell I/O | Terminal MCP |
| Direct expert ChatGPT session control | Terminal MCP |
| Remote machine access | Terminal MCP |
| Browser verify / screenshot | Terminal MCP |
| Session/resource/machine health | Terminal MCP |
| Legacy durable queue / dispatcher | Retired from Terminal MCP |
| Repository handoff | `PROJECT_CONTEXT.md` |
| Source of truth for code | Git/GitHub |

## Non-negotiable compatibility rules

1. `terminal_turn` must remain directly callable by ChatGPT.
2. Direct read/inspect/debug operations must never require Paperclip to be online.
3. Direct session creation remains available. If agent types such as Claude/Codex are later restricted for normal work, there must still be an explicit, documented direct expert path.
4. Browser verification and remote node operations stay available directly.
5. Queue/orchestrator retirement must be capability-specific. Do not disable unrelated Terminal MCP execution features.
6. Paperclip outage must not prevent emergency access to Terminal MCP.
7. Do not introduce a second hidden durable queue inside the Paperclip adapter.
8. Existing queue history may remain read-only during migration; no new durable Terminal MCP queue rows should be created by default.
9. Every implementation task must update `PROJECT_CONTEXT.md` before completion.
10. Each implementation task uses a fresh branch/worktree from updated `main`, with tests before merge.

## Default routing policy

Use Paperclip by default when the request is durable project work involving one or more of:

- tracked feature/bug work,
- multi-step coding,
- project issue ownership,
- agent assignment,
- dependency tracking,
- long-running work that should survive a chat turn,
- controlled workspace/worktree creation,
- budget/governance,
- repeatable project automation.

Use direct Terminal MCP when the request is primarily:

- inspect machine/session state,
- diagnose a stuck or broken agent,
- recover a failed deployment,
- investigate OS/process/network/container state,
- interact with a remote node,
- run browser verification,
- perform a difficult one-off operation where low-level control matters,
- emergency/break-glass work,
- explicitly requested direct terminal/Claude/Codex interaction.

For ambiguous cases, prefer Paperclip for project ownership but allow Terminal MCP as the execution/debug side channel.

## Migration gates

No Terminal MCP capability is removed merely because a Paperclip equivalent exists. A capability can be retired from the default public orchestration path only after:

1. a Paperclip replacement is installed,
2. an end-to-end pilot succeeds,
3. failure/recovery behavior is verified,
4. direct ChatGPT compatibility tests still pass,
5. rollback instructions exist,
6. the corresponding feature flag/default is documented.

## Task order

1. `TMPC-001` - clean public orchestration surface without harming direct access.
2. `TMPC-002` - install/bootstrap Paperclip locally and document operations.
3. `TMPC-003` - formalize and test the direct ChatGPT compatibility contract.
4. `TMPC-004` - pilot Paperclip native Claude/Codex project work.
5. `TMPC-005` - implement external `terminal_mcp` Paperclip adapter.
6. `TMPC-006` - deterministic session lifecycle and result mapping.
7. `TMPC-007` - cut over default workspace/worktree ownership.
8. `TMPC-008` - observability, recovery, security, and rollout hardening.
9. `TMPC-009` - retire legacy orchestration code only after all gates pass.

See each task file in this directory for scope, dependencies, acceptance criteria, and explicit out-of-scope items.

## Rollback principle

Migration is reversible by layer.

- Paperclip may be stopped without disabling Terminal MCP direct access.
- Adapter failures must fall back to an explicit direct Terminal MCP procedure.
- Legacy queue code may remain dormant behind an operator-only flag during the migration window, but it must not be advertised to clients or enabled by client arguments.
- Do not delete migration history or queue history until the final retirement task.
