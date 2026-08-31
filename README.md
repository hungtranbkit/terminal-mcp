# Terminal MCP

## Purpose

Terminal MCP lets an MCP client observe and, when explicitly enabled, send constrained input to whitelisted tmux sessions. It supports local STDIO and a loopback-only Streamable HTTP transport intended to sit behind an authenticated HTTPS tunnel.

## Architecture

```text
Claude / Codex / shell
        |
       tmux
        |
   Terminal MCP
        |
 MCP (STDIO or loopback HTTP)
        |
     ChatGPT
```

tmux is the source of truth. The server calls tmux with explicit argument arrays through Python `subprocess`; it never uses `shell=True`.

## Quick start

```bash
cd /home/dell/workspace/terminal-mcp
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/terminal-mcp
```

For the remote-capable, loopback-only Streamable HTTP mode:

```bash
.venv/bin/terminal-mcp-http
# MCP endpoint: http://127.0.0.1:8766/mcp
# Read-only session dashboard: http://127.0.0.1:8766/dashboard
```

The dashboard lists only whitelisted tmux sessions and shows their sanitized
status and recent output, refreshing every five seconds. It also has a text
input box per session: sending from it calls the same guarded
`terminal_send_text` path as the MCP tool, so it is still gated by
`permissions.terminal_input`, `input_policy` (allowed/denied patterns, current
pane command, max length), and is recorded in the same input audit log — the
box is simply disabled client-side (and the server still enforces it) when a
session doesn't pass those checks. Like the MCP endpoint, the dashboard remains
bound to loopback and is intended for use directly on the Dell only; the
Secure MCP Tunnel does not publish it as a general-purpose website.

The HTTP bind address is deliberately fixed to `127.0.0.1`. Do not expose it
directly or change it to `0.0.0.0`; use an authenticated HTTPS tunnel that maps
only its MCP route.

The installed user service can be managed with:

```bash
systemctl --user status terminal-mcp-http.service
systemctl --user restart terminal-mcp-http.service
```

Its unit is `~/.config/systemd/user/terminal-mcp-http.service`, runs as the
current user, and uses `Restart=on-failure`. Authentication is intentionally
not implemented as an ad-hoc MCP wrapper: it must be enforced by the HTTPS
tunnel/identity provider before traffic reaches the loopback endpoint.

For private ChatGPT Developer Mode connectivity, use OpenAI Secure MCP Tunnel.
The credential-free deployment runbook and inactive service template are in
[`deploy/secure-tunnel`](deploy/secure-tunnel). No Cloudflare/public ingress is
required for this mode.

Equivalent module command:

```bash
.venv/bin/python -m terminal_mcp.server
```

Example MCP client command configuration:

```json
{
  "command": "/home/dell/workspace/terminal-mcp/.venv/bin/terminal-mcp",
  "args": [],
  "env": {
    "TERMINAL_MCP_CONFIG": "/home/dell/workspace/terminal-mcp/config.yaml"
  }
}
```

## Config

`config.yaml` controls read/input permissions, allowed session patterns, and capture limits. Input is disabled by default.

```yaml
permissions:
  terminal_read: true
  terminal_input: false
allowed_session_patterns:
  - "claude-*"
  - "codex-*"
  - "agent-*"
  - "test-*"
max_capture_lines: 2000
default_tail_lines: 200
```

## Security model

- Sessions are denied unless their name matches a whitelist pattern.
- Sensitive names containing `root`, `ssh`, `password`, `secret`, or `database` require an exact literal whitelist entry.
- Output is sanitized for common API keys, bearer/authorization values, passwords, and tokens.
- There is no shell execution, arbitrary filesystem access, environment disclosure, process killing, scheduler, or autonomous agent.
- HTTP mode listens only on loopback and cannot change permissions or whitelist through requests.
- `terminal_send_text` uses tmux literal mode. `terminal_send_keys` accepts only a fixed V1 key allowlist.
- Errors do not reveal details about denied sessions.

## Create an agent tmux session

```bash
tmux new -s claude-mesflow
claude
```

```bash
tmux new -s codex-mesflow
codex
```

Detach with `Ctrl-b d` and reattach with `tmux attach -t SESSION`.

## Enable terminal input

Edit only the permission in `config.yaml`:

```yaml
permissions:
  terminal_read: true
  terminal_input: true
```

Restart the MCP child process after changing configuration. Enabling input lets the MCP client type into allowed sessions; review this change carefully.

## Tools

- `terminal_list_sessions`
- `terminal_tail`
- `terminal_capture`
- `terminal_status`
- `terminal_send_text`
- `terminal_send_keys`
- `terminal_bind`
- `terminal_get_binding`
- `terminal_list_bindings`
- `terminal_unbind`
- `terminal_tail_bound`
- `terminal_status_bound`
- `terminal_send_bound`
- `terminal_list_input_audit`
- `terminal_input_context`
- `supervisor_watch`, `supervisor_unwatch`, `supervisor_list_watches`,
  `supervisor_status`, `supervisor_list_events`, `supervisor_ack_event`,
  `supervisor_run_once` — see "Supervisor Loop v1" below

## Chat ↔ tmux logical binding

A logical binding gives a chat or work context a stable name without relying on
an internal ChatGPT conversation ID. Bindings persist in SQLite at
`~/.local/state/terminal-mcp/bindings.db` (or the path in
`TERMINAL_MCP_BINDINGS_DB`) and resolve to tmux, which remains the source of
truth.

Create an agent session:

```bash
tmux new -s claude-mesflow
claude
```

From ChatGPT, bind `mesflow-dev` to `claude-mesflow` with `terminal_bind`, then
requests such as “xem agent này đang làm gì”, “đọc 300 dòng cuối”, or “kiểm tra
nó đang chờ input không” can use:

```text
terminal_tail_bound(binding="mesflow-dev")
terminal_tail_bound(binding="mesflow-dev", lines=300)
terminal_status_bound(binding="mesflow-dev")
```

Binding names contain only lowercase letters, digits, `-`, `_`, or `.`, with a
maximum of 64 characters. A target must exist and pass the session whitelist;
sensitive session names are never bindable. Existing bindings require
`replace=true` for an explicit remap. If a tmux session disappears, its binding
is retained and status becomes `MISSING`.

New bindings use `read_enabled=true` and `input_enabled=false`. Bound input is
allowed only when both the local global permission and the binding permission
are true. Creating a binding never enables global terminal input.

## Known limitations

- Status detection is heuristic and intentionally returns `UNKNOWN` when evidence is weak.
- Only the active pane of each tmux session is inspected.
- Capture is line-based and capped; it does not stream incremental events.
- Redaction covers common secret shapes, not full DLP.
- HTTP mode is local-only until a separately authenticated HTTPS tunnel is configured.

## Safe Input

Terminal input is deny-by-default. Setting `permissions.terminal_input: true` only
opens the global gate; a target must also match `input_policy.allowed_session_patterns`,
must not match a denied pattern, and must pass the action and current-command guards.
Logical bindings add another independent gate and default to `input_enabled: false`.

```yaml
permissions:
  terminal_read: true
  terminal_input: true

input_policy:
  allowed_session_patterns: ["claude-*", "codex-*"]
  denied_session_patterns: ["ssh-*", "prod-shell-*"]
  allow_send_text: true
  max_text_length: 12000
```

Enable one binding explicitly with `terminal_bind(binding="mesflow-dev",
session="claude-mesflow", input_enabled=true)`. Use `terminal_input_context` to
inspect the command and last 20 sanitized lines first. Both `terminal_send_text`
and `terminal_send_bound` accept `dry_run=true`; this validates every guard,
records `DRY_RUN`, and sends nothing.

Text is passed to `tmux send-keys -l` as one literal argument. It is never parsed
as a shell command, interpolated into a command string, or executed with
`shell=True`. `press_enter=true` sends Enter separately. Key input is restricted
to the configured allowlist. `C-c` and `C-d` require
`confirm_sensitive=true`; unknown keys return `KEY_NOT_ALLOWED`.

Every successful, blocked, and dry-run input attempt is appended to
`~/.local/state/terminal-mcp/audit.db` (mode `0600` where supported). The audit
stores the full text's SHA-256, length, and a short redacted preview—never the
full prompt. `terminal_list_input_audit` returns sanitized metadata and supports
binding/session filters.

Safe Input does not add an arbitrary command or file-read facility. Input cannot
bypass the session policy, and panes whose current command is `ssh`, `mysql`,
`psql`, `sudo`, or `passwd` are denied unless locally allowed. (Supervisor Loop
v1, below, is a separate, detection-only facility — it never calls
`terminal_send_text`/`terminal_send_keys` itself.)
- tmux sessions must run under the same Unix user as Terminal MCP.

## Supervisor Loop v1

**What v1 solves:** local, automatic detection of a watched session/binding
transitioning into a state that needs attention (waiting for input, an error,
or a defensible completion signal), persisted as a durable, queryable event —
so a human (or a future automation) doesn't have to keep polling by hand.

**What v1 deliberately does not do:** it never sends text/keys to a watched
session, never executes a shell command, and never bypasses
`terminal_input`/`input_policy`/binding/confirmation/audit — those gates are
completely unchanged. It also does not itself wake up or message ChatGPT; see
"v2" below for what's still needed for that.

Disabled by default. Enable in `config.yaml`:

```yaml
supervisor:
  enabled: true
  poll_interval_seconds: 20   # minimum enforced: 5
  idle_threshold_seconds: 45
  max_iterations: 20          # a watch auto-disables itself after this many polls
  same_failure_limit: 2       # ...or after this many *identical* consecutive errors
  event_retention: 500
  watched_session_patterns: ["claude-*"]   # matched against currently allowed sessions each poll
  watched_bindings: ["mesflow-dev"]        # must already exist via terminal_bind
```

Restart `terminal-mcp-http` after changing `supervisor.enabled` — the background
poll thread is only started (as a daemon thread inside that process) when it is
`true`, and only for the HTTP service (not the per-client STDIO server, which
would start/stop a loop with every client connection). One loop per process;
`supervisor_status` reports whether it is actually running.

Watches can also be created dynamically at any time via `supervisor_watch`,
independent of the config-seeded patterns/bindings above, and work even with
`supervisor.enabled: false` (only the automatic timer is gated — the tools
themselves, including `supervisor_run_once` for a single manual/deterministic
pass, are always available). A watch can never be created for, or continue
polling, a session outside the existing whitelist.

**State machine.** Reuses `classify_status()` (the same heuristic
`terminal_status` already applies) for `RUNNING`/`IDLE`/`WAITING_INPUT`/
`UNKNOWN`, and layers two more states on top from explicit evidence only:
`DONE` (an explicit completion marker — never inferred from ordinary silence,
which maps to `IDLE` via `idle_threshold_seconds` instead) and `ERROR` (a
traceback/fatal/exit-code-style marker). An event is persisted only on a
*meaningful transition* — identical repeated state/output is deduplicated,
never re-alerted.

**Stop policy.** A watch auto-disables itself (an event with
`event_type: "stalled"` is recorded) when either limit is hit, and stays
disabled until explicitly resumed (call `supervisor_watch` again for the same
target):
- `same_failure_limit` — the same `ERROR` with unchanged output repeats this
  many times in a row.
- `max_iterations` — a hard poll-count ceiling per watch, regardless of state.

A denied, since-excluded, or vanished session/binding emits
`event_type: "watch_target_missing"` and also auto-disables the watch rather
than retrying it.

**Event types:** `state_changed`, `attention_required` (entering
`WAITING_INPUT`), `completed` (entering `DONE`), `error_detected` (entering
`ERROR`), `stalled`, `watch_target_missing`.

**Event schema** (also in `terminal_mcp/supervisor.py`'s `EVENT_SCHEMA_VERSION`
docstring — the stable JSON shape a future v2 webhook forwarder can build
against):

```json
{
  "schema_version": 1, "id": 1, "timestamp": "2026-...Z",
  "watch_key": "session:claude-mesflow", "kind": "session", "target": "claude-mesflow",
  "previous_state": "RUNNING", "state": "WAITING_INPUT",
  "event_type": "attention_required",
  "reason": "recent prompt matched ... at bottom offset 0",
  "output_preview": "Do you want to continue? [y/N]",
  "output_hash": "sha256...", "iteration_count": 3,
  "acknowledged_at": null, "metadata": {"source": "manual"}
}
```

`output_preview` is redacted (the same `redact_text`) and truncated *before*
it is ever written to SQLite — never the full/raw pane output.

Persisted in SQLite at `~/.local/state/terminal-mcp/supervisor.db` (or
`TERMINAL_MCP_SUPERVISOR_DB`), same pattern as `bindings.db`/`audit.db`: a
`watches` table (state/iteration/failure bookkeeping per target) and a
`supervisor_events` table.

The dashboard shows a compact "🛰" badge (hidden entirely when there are zero
watches) with per-state counts and unacknowledged events in a small overlay
panel; acknowledging an event from there only stamps `acknowledged_at` in
SQLite — it is not a terminal-input path.

**v2 (not built here):** an external wake-up — a webhook/relay that notices a
newly queued `attention_required`/`error_detected` event via
`supervisor_list_events(unacknowledged_only=true)` (or a future push
mechanism) — invoking ChatGPT with a human-reviewed, approved prompt, and
only then (through the existing, unchanged Safe Input path) sending it. v1
intentionally stops at "detect and queue"; no insecure outbound callback is
implemented here.
# terminal-mcp
