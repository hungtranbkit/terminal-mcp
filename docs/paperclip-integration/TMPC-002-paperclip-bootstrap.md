# TMPC-002 - Local Paperclip bootstrap

Priority: P0
Status: DONE
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
## Implementation evidence — 2026-09-30

- Paperclip `2026.916.1` is installed as a managed install under `~/.paperclip/cli/`.
- Local instance `default` is configured at `127.0.0.1:3100`; no LAN/Cloudflare exposure was added.
- `paperclipai.service` is installed/enabled and reports server health `ok` with version `2026.916.1`.
- Created company `Terminal MCP Pilot` and project `TMPC Integration Pilot` with the Terminal MCP repository as its primary workspace.
- Service restart changed the server PID and the pilot project remained readable afterwards, proving persistence across restart.
- Stopping Paperclip closed port 3100; a direct Terminal MCP `send_wait` still succeeded (`TMCP_DIRECT_OK_WHILE_PAPERCLIP_STOPPED`), then Paperclip restarted successfully.
- Codex CLI `0.157.1` and Claude Code `2.1.285` were detected with existing host login state; no credentials were copied into this repository.
- Warm idle measurement after startup: Paperclip Node about 517 MB RSS / 1.6% RAM; embedded Postgres child about 27 MB RSS. CPU was still settling (~11.5% process-average at ~68 s uptime), so future optimization should watch steady-state cost before broad rollout.
- Loopback health check returned HTTP 200.
