# Deploy Map

## The agreed procedure ("cách 1")
1. Record a baseline: sessions, node health, controller PID.
2. Check for blockers. If a restart could lose a session, STOP and report.
3. Exactly ONE `systemctl restart terminal-mcp-http`.
4. Verify: routes answer, sessions intact (pane PIDs unchanged), no exceptions.
5. Roll back to the previous commit and re-verify if anything is wrong.

## Never
Never restart a node agent. Never reboot. Never drain or kill a live tmux or
ConPTY session. Never touch the Windows node agent as part of a deploy.

## Release levels
PREVIEW -> STAGING -> PRODUCTION are separate. Production needs the full gate
and an approval that covers ONE release, not a standing permission.

`deploy_restart` is registered as a PRODUCTION-risk runbook, so it is never
auto-invoked; it requires an explicit approval.
