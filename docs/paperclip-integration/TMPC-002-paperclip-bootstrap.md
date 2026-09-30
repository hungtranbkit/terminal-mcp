# TMPC-002 - Local Paperclip bootstrap

Priority: P0
Status: TODO
Depends on: TMPC-001 recommended, not strictly required

## Purpose

Install a controlled local Paperclip instance on Dell Linux and establish a repeatable operator runbook without exposing it publicly.

## Scope

- Install Paperclip using the supported installation path.
- Bind initially to loopback only.
- Choose and document persistent data location.
- Add a service definition or a reproducible start/stop command.
- Record health check, logs, backup, upgrade, and rollback procedures.
- Configure initial projects corresponding to active development projects.
- Confirm existing host Codex and Claude authentication can be used by native local adapters without copying secrets into the repository.
- Keep heartbeat/automatic agent execution conservative during pilot.
- Do not add Cloudflare/public exposure in this task.

## Security rules

- No credentials committed to Git.
- No plaintext API keys in task docs.
- Paperclip UI/API is local-only during pilot.
- File permissions for runtime state and credentials must be documented.
- Terminal MCP remains independently available if Paperclip is stopped.

## Acceptance criteria

- Paperclip starts on Dell Linux after reboot or through documented operator command.
- Health/UI responds locally.
- Stop/restart does not corrupt state.
- One test project can be created.
- Codex/Claude adapter prerequisites are detected.
- Terminal MCP direct `inspect`/`send` still works while Paperclip is running and while it is stopped.
- Resource use is measured so Paperclip does not materially worsen current machine pressure.
- `PROJECT_CONTEXT.md` updated.

## Deliverables

- bootstrap/install script or deployment definition,
- example env/config with secrets omitted,
- `docs/operations/paperclip.md` runbook,
- focused smoke test or health check script.

## Out of scope

- Project-wide task cutover.
- External Terminal MCP adapter.
- Public internet exposure.
