# Bounded shell observation for ChatGPT

Use `terminal_turn(action="send_wait", target="node/session", text="command",
idempotency_key="unique-command-key", desired_states=["IDLE"])` for a plain
shell command. Preserve the key on retries of that same submission.

The default wait slice is 10 seconds. `MCP_LONG_CALL_MAX_SEC` can tune this
between 1 and 15 seconds. A larger requested timeout does not hold one MCP
connection for the command's entire lifetime. Sending consumes the same slice:
when sending uses the budget, the receipt immediately returns a durable PENDING
wait without another status probe.

On PENDING, retain `wait.resume_token` and continue the user-requested watch with
`terminal_turn(action="resume", resume_token=...)`. This only observes; it never
sends the command again, cancels it, or resets the session. The wait journal
survives controller restart. A requested state observed in the pane yields
MATCHED; this is not an exit-code or command-success guarantee. Another writer
can change a session, so the token identifies a session-state wait, not an
isolated shell process. Do not concurrently send unrelated commands to that pane.

Status reads (including bindings) and final tail reads share the remaining
budget. Slow reads run in bounded daemon workers, at most one per read key and
32 per controller. An outstanding read is not duplicated; a later request gets
PENDING. Completed abandoned reads are discarded to avoid stale observations.
If the requested state was observed but the tail read times out, MATCHED includes
`tail_unavailable: true`; obtain output separately when the node is responsive.

This reduces timeout exposure in wait/resume/send_wait. It does not extend
ChatGPT's own deadline, implement push notifications, or bound the initial
mutating send, database contention, or unrelated inspect/list calls. In
particular, a send transport failure still requires checking the original
submission before retrying. No claim is made that every ChatGPT message-delivery
error originates in this wait path.
