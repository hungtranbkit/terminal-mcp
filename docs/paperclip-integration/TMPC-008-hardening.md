# TMPC-008 - Observability, recovery, security, and rollout hardening

Priority: P1
Status: TODO
Depends on: TMPC-005, TMPC-006, TMPC-007

## Purpose

Make the combined Paperclip + Terminal MCP architecture operationally safe before legacy orchestration is removed.

## Observability

Provide enough evidence to answer:

- Which Paperclip issue/run caused this Terminal MCP session?
- Which node is executing it?
- Is it actually executing or merely labelled running?
- What was the last fresh evidence?
- What branch/workspace is associated?
- What error class stopped it?
- Was the action direct ChatGPT or Paperclip-managed?

## Recovery

Document and test:

- Paperclip restart,
- Terminal MCP restart,
- adapter restart,
- node temporarily offline,
- stale tmux session,
- lost browser harness,
- interrupted network/tunnel,
- failed native agent,
- failed adapter-managed run.

## Security

- no secrets in repository,
- preserve existing authorization/grants,
- adapter uses least privilege,
- Paperclip local-only until a separate security review approves exposure,
- direct ChatGPT access remains authenticated/authorized exactly as before,
- no client parameter can re-enable retired durable queue behavior.

## Performance/resource checks

Measure on Dell Linux:

- Paperclip idle and active memory/CPU,
- effect on swap and I/O,
- session count,
- duplicate process/container creation,
- log growth.

## Acceptance criteria

- Combined health check exists.
- Run/session correlation is inspectable.
- Recovery drills have documented results.
- Direct mode continues to work if Paperclip is intentionally stopped.
- No unbounded polling loop.
- No duplicate durable scheduler remains active by default.
- `PROJECT_CONTEXT.md` updated.
