# Structured status API — P0

Goal: read-only JSON status for nodes, sessions, and tasks through existing authorized Terminal MCP controller. Reuse access checks. Exclude terminal pane text, prompts, secrets, and arbitrary filesystem paths. Unknown status must remain UNKNOWN until verified.

Rollout: Dell Linux canary, HP Linux, then m910. Require unit/security tests, health checks and rollback before each step. For session cleanup, record node, session, activity, task, worktree/branch status, ownership and protection flags; delete only after idle and clean preflight, and verify the session disappeared. Never delete by age alone.

Status: specification only; not deployed.
