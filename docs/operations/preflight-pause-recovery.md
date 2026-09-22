# Preflight pause recovery

The controller checks recoverable, never-dispatched coordinator pauses every
30 seconds, probing at most eight candidates per pass in rotating order.
A dirty-repository refusal (including its exhausted review budget) or
`AGENT_NOT_RUNNING` can return to QUEUED only after a fresh successful preflight.
The normal claim and cross-lane conflict review still run before dispatch.
This does not enable auto-dispatch on an opted-out lane, bypass approvals,
change files, cancel other tasks, or clear operator/project pauses.

AI task prompts must target an agent session. A plain-shell target returns
`AGENT_NOT_RUNNING` before any prompt is sent. Launch the intended agent in
that session; the pause can then recover automatically. Intentional shell
tasks must declare `metadata.execution_mode: shell`.

Task-status receipts for queued work include `dispatch_blocker` when a paused
or occupied lane prevents dispatch, including the blocking task and reason.
Queue position alone does not mean the lane is eligible to run.

Generated QA artifacts are not automatically ignored or deleted. Use the
existing task-scoped `allow_dirty_repo` setting only when that working tree
has been reviewed and work with its existing changes is authorized.
