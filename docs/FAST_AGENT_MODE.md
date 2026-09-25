# Fast Agent Mode

Fast Agent Mode submits coding work once through the existing durable queue and
lets the server advance it. It keeps the queue's coordinator, delivery
acceptance, completion evidence, authorization, and restart-recovery gates.

## Invoke it

Use one `terminal_turn` call with `action="start"`, a target session, and the
complete coding goal in `text`:

```json
{"action":"start","target":"coding-worker","title":"Fix parser edge case",
 "text":"Reproduce the parser bug, inspect relevant code, make the smallest fix, run the focused tests, and report changed files and verification."}
```

The response contains a durable `task_id`, current queue state, and whether
server-side following started. Do not call inspect/wait repeatedly to advance
shell steps. Use `action="task_status"` with that id when the user asks for a
status check; its `outcome` is a compact structured result with status,
summary, root cause, changed files, tests, verification, commit, blocker, and
`requires_user_action`. Full terminal output remains available through the
existing task/session diagnostics.

The queue task stores a bounded `checkpoint` with the goal, findings, files
read/changed, tests, failures, next action, and repository identity. A worker or
caller can update it with `terminal_turn` action `task_checkpoint`, passing
`task_id` and `args: {"checkpoint": {...}}`. Partial updates retain fields
from the prior checkpoint. It stores summaries and paths, not pane logs.

`action="send"` with `long_task=true` also uses the existing persistent queue.
Short one-shot shell commands still use `send` or `send_wait`.

## Outcome and safety behavior

`COMPLETE` is emitted only from the queue's verified completion evidence.
`BLOCKED`, `NEEDS_DECISION`, and `FAILED` are surfaced as stopped outcomes.
`DELIVERY_UNKNOWN` means submit acceptance was not proved: the same task stays
in `DISPATCH_UNCERTAIN`, is inspected without a blind resend, and asks for a
human decision if bounded reconciliation cannot resolve it. A queue claim,
injected text, or Enter transmission is not execution evidence.

Use the task runner or require explicit human review for destructive changes,
privileged operations, production deployments, broad scope, or work whose
permissions exceed the target session's configured grants. Fast Agent Mode
does not grant those permissions. Keep the goal, repository/session, scope,
verification commands, and completion conditions explicit in the task prompt
and task metadata. Browser verification remains a separate explicit action
when the change has a browser-visible acceptance condition.

## Round-trip benchmark

The audit baseline for ordinary coding work was 10–30 orchestration calls per
task, with a model doing small inspect/send/wait steps. The `start` path is one
submit call and returns a durable id; server ticks and the bounded task
follower perform dispatch and progress without client polling. A user-requested
final status read is one additional call, so the expected interaction is 1
call to submit or 2 calls including a final result read. This is a call-count
comparison, not a claim that terminal execution time or model work disappears.
After an HTTP server restart, explicitly marked Fast Agent Mode/`long_task`
rows are reattached to the bounded follower from the durable queue. Ordinary
queued rows are not resumed by that task-specific recovery path.

| Path | Client orchestration calls/task | Who advances the task |
| --- | ---: | --- |
| Before: inspect/send/wait loop | 10–30 (audit estimate) | ChatGPT between calls |
| After: submit once | 1 | Server queue engine/follower |
| After: submit + requested result | 2 | Server queue engine/follower, then one status read |

The `tests/test_one_call_start.py` suite exercises the one-call submission,
bounded start sequence, follower progression, restart reattachment, terminal
stop states, and the no-polling receipt. The queue store remains the source of
truth across process restart; only marked tasks resume on this follower path.
When the target session is on the local node, start also caches a compact
RepoContext (branch, commit, dirty state, likely package manager/runtime,
known test/lint/build commands, and confidently detected service port) against
the task. Remote paths are not inspected on the central host. Project metadata
and git changes invalidate the in-process cache conservatively. Capability,
health, draining, repository locality, and queue-load routing continue through
the existing TaskRouter; pinned `start` targets remain hard affinity.

See [the round-trip benchmark](FAST_AGENT_MODE_BENCHMARK.md) for the call-count
comparison and disposable E2E evidence.
