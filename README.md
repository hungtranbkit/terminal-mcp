# Terminal MCP

## Purpose

Terminal MCP lets an MCP client discover every tmux session on the host, then observe and, when explicitly enabled, send constrained input to whitelisted (or explicitly per-session-granted -- see "Dashboard session grants" below) tmux sessions. It supports local STDIO and a loopback-only Streamable HTTP transport intended to sit behind an authenticated HTTPS tunnel.

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

The dashboard lists every real tmux session on the host (not only whitelisted
ones -- discovery is not access) and shows sanitized status and recent output
for the ones it can actually read, refreshing every five seconds. A session
outside the static whitelist is still listed (name/attached/windows/activity
only, never content) but shows as restricted until an operator explicitly
grants it read (and, separately, input) from the dashboard itself -- see
"Dashboard session grants" below. It also has a text input box per readable
session: sending from it calls the same guarded `terminal_send_text`(-shaped)
path as the MCP tool, so it is still gated by `permissions.terminal_input`,
`input_policy` (allowed/denied patterns, current pane command, max length),
and is recorded in the same input audit log — the box is simply hidden
client-side (and the server still enforces it) when a session doesn't pass
those checks. Like the MCP endpoint, the dashboard remains bound to loopback
and is intended for use directly on the Dell only; the Secure MCP Tunnel does
not publish it as a general-purpose website.

### Dashboard session grants

A session outside `allowed_session_patterns`/`input_policy.allowed_session_
patterns` is still discoverable everywhere (the dashboard, and
`terminal_list_sessions` for any MCP client) but starts fully restricted: no
content, no input. From the dashboard, an operator can explicitly grant it
**read** (its output becomes visible immediately, no restart) and, separately,
**input** (requires read already granted; still gated by the global
`permissions.terminal_input`, `input_policy`'s deny patterns, and the same
sensitive-current-command check every other input path uses). An input grant
pins the session's tmux identity at grant time and re-verifies it on every
send, exactly like a binding — a session recreated under the same name never
silently keeps a prior grant; re-grant explicitly to accept the new identity.
Both are revocable independently (revoking read also revokes input). Every
grant/revoke is audited. This mechanism is dashboard-only: there is no MCP
tool to grant or revoke — an MCP client only ever sees the *result* (a
session's `read_allowed`/`read_granted`/`input_allowed`/`input_granted`
fields in `terminal_list_sessions`), never a way to create one itself.

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

- Discovery (`terminal_list_sessions`, the dashboard's session list) shows every real tmux session on the host -- name/attached/windows/created/activity only, tmux metadata, never pane content. Content and input remain denied unless a session's name matches a whitelist pattern, or it has been explicitly granted read/input from the dashboard (see "Dashboard session grants") -- discovery never grants either.
- Sensitive names containing `root`, `ssh`, `password`, `secret`, or `database` require an exact literal whitelist entry, and can never be dashboard-granted either.
- Output is sanitized for common API keys, bearer/authorization values, passwords, and tokens.
- There is no shell execution, arbitrary filesystem access, environment disclosure, process killing, scheduler, or autonomous agent.
- HTTP mode listens only on loopback and cannot change permissions or whitelist through requests.
- `terminal_send_text` uses tmux literal mode. `terminal_send_keys` accepts only a fixed V1 key allowlist.
- Errors for a denied/ungranted session's content or input reveal nothing beyond that denial (no pane content, no reason tied to its content).

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
- `supervisor_watch`, `supervisor_set_verifier_policy`, `supervisor_unwatch`,
  `supervisor_list_watches`, `supervisor_status`, `supervisor_list_events`,
  `supervisor_ack_event`, `supervisor_run_once` — see "Supervisor Loop v1"
  below, and "Independent completion verification" for
  `supervisor_set_verifier_policy`

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

## Supervisor Loop v2

**What v2 solves:** when v1 emits an actionable, unacknowledged event
(`WAITING_INPUT`/`ERROR`), v2 provides a safe, auditable, restart-safe
claim → decide → approve → send pipeline that can continue a watched session
without a human re-typing "check"/"continue" every time — while still going
through the exact same guarded send path as manual input.

**What v2 does not build:** no ChatGPT/webhook callback exists to invoke, and
none is faked here. v2 is the local queue/claim/decide/send contract an
external caller (ChatGPT, a script, a human) drives via the `supervisor2_*`
MCP tools — it never invents a way to wake an external agent on its own. It
also never adds a second send path: `execute_send` calls the exact same
`terminal_send_text`/`terminal_send_bound` methods `terminal_send_text`/
`terminal_input` already use, so `terminal_input`, whitelist, binding
`input_enabled`, `input_policy`, confirmation, sensitive-target, redaction,
audit, and length limits all still apply unchanged.

**Policy modes** (per watch, via `supervisor2_set_policy`; default for every
watch is `observe_only` — nothing is ever auto-sent unless a watch is
explicitly opted in):

- `observe_only` (default) — v2 never offers, claims, or sends anything for
  this watch; `supervisor2_list_actionable_events` never returns its events.
- `suggest_only` — a decision/prompt can be claimed and submitted, but always
  needs an explicit `supervisor2_review_action(decision="approve")` before
  `supervisor2_execute_send` will do anything.
- `approved_auto_continue` — requires an `approved_template` string set on the
  policy. A submitted prompt auto-approves **only** if it is byte-for-byte
  equal (after redaction) to that template — no free-form filling, no partial
  match. This is the whole mechanism that keeps auto-continue inside the
  scope the watch owner pre-approved; anything else falls back to needing
  `supervisor2_review_action`.

**Hard stop conditions** — any of these halts the action (state `blocked` or
`failed`) and surfaces the reason rather than guessing:
`max_auto_actions`, `wall_clock_timeout_seconds` (since the watch's first v2
action), `same_prompt_repeat_limit` (identical prompt sent too many times in a
row), `no_progress_limit` (output hash unchanged across repeated post-send
checks), a stale/expired claim lease, and content screening against
`ATTENTION_STOP_PATTERNS` — password/API-key/credential/token requests,
confirmation prompts ("are you sure", "irreversible", "cannot be undone"),
and destructive-looking commands (`rm -rf`, `force-push`, `drop table`,
`sudo`, ...) — checked at both claim time (against the triggering output) and
decision time (against the proposed prompt and the freshly re-fetched current
output). A match blocks the action **and** the watch's policy
(`blocked_reason` set) so no repeated attempt can slip through.

**Idempotency & concurrency.** A small SQLite compare-and-swap
(`UPDATE supervisor_actions SET state=? WHERE id=? AND state=<expected>`) is
the only concurrency primitive — no external queue/broker. `execute_send`
CASes `approved → sent` *before* calling the guarded send, so a retry,
duplicate call, or a restart mid-send always finds `state != approved` and
is a safe no-op — it can never send twice. Only one open (non-terminal)
action is allowed per watch at a time, so two workers can't double-claim or
double-decide the same watch concurrently.

**Recovery.** Nothing is replayed on restart: an action already in `sent` or
beyond is never re-sent (the CAS guard above), and a `claimed`/`decided`
action past its 5-minute lease is treated as expired and can be reclaimed
rather than resumed blindly.

**Reconciliation → DONE.** After every `supervisor_run_once`/poll cycle, v2
checks every `observing` (post-send) action: if the watch's output hash
changed, the action completes and links `resulting_event_id`; if the watch
also reached `DONE`, the watch's v2 counters (`auto_action_count`, repeat/
no-progress counters) reset — "the loop stops cleanly at DONE". If output
never changes within `no_progress_limit` checks, the action is blocked
instead.

**MCP tools:** `supervisor2_set_policy`, `supervisor2_get_policy`,
`supervisor2_list_actionable_events`, `supervisor2_claim_event`,
`supervisor2_submit_decision`, `supervisor2_review_action`,
`supervisor2_execute_send`, `supervisor2_list_actions`. Persisted in the same
`supervisor.db` as v1, in two new tables: `supervisor_policies` (one row per
watch opted into v2) and `supervisor_actions` (the full claim → decision →
approval → send → outcome record per action, linking back to the triggering
`supervisor_events.id`). Never stores secrets or raw unredacted output —
prompts are redacted before storage and before send, and `send_result` only
ever holds `terminal_send_text`/`terminal_send_bound`'s own return value
(a character count, never the text itself).

**Dashboard.** The existing 🛰 Supervisor overlay gained a compact per-watch
v2 section (policy badge, auto-action count, latest action's state/blocked
reason/send result, and a one-click "Pause (observe only)" button) — it does
not touch or resize the main terminal viewer.

Still fully manual/opt-in end to end: a fresh install defaults every watch to
`observe_only`, and even `approved_auto_continue` only ever sends the one
exact template a human configured for that watch.

## Independent completion verification

For a watch under `approved_auto_continue` policy (with v2's global
`supervisor.v2_enabled` also on — both gates, same as `execute_send`
requires), prose/marker "done" evidence alone is **not** sufficient to reach
`VERIFIED_DONE` and reset the auto-continue chain: quiet-window/nonce
evidence that would promote any other watch instead moves this one through a
new `VERIFYING` state while a real, independent verifier runs *outside* the
target pane. Every other watch (the default) is completely unaffected —
unchanged, direct promotion, exactly as described above.

Configure the verifier once per watch with `supervisor_set_verifier_policy`:

```text
supervisor_set_verifier_policy(
  session="claude-mesflow",
  worktree="/home/you/project",      # real subprocess cwd, `git -C` target
  require_git_clean=true,            # fail if `git status --porcelain` is non-empty
  require_commit_matches="<sha>",    # optional: pin to a specific commit
  test_command=["pytest", "-q"],     # a literal argv list -- never a shell string
  timeout_seconds=300,
)
```

Only `git rev-parse`/`git status`/`git diff --stat` (read-only) and, if
configured, that one fixed `test_command` ever run — always
`subprocess.run(..., shell=False)`, always a fixed argument list, never
anything parsed out of what the watched pane printed. An autonomous watch
with **no** verifier policy configured goes to `BLOCKED` rather than ever
reaching `VERIFIED_DONE` on prose alone — this is the actual enforcement of
"independent verification required", not an oversight to work around.

**New states:** `VERIFYING` (a real verifier run in progress — durable,
survives a process restart mid-run, the next poll safely re-verifies),
`FAILED` (the verifier ran and rejected the claim -- e.g. a failing test, a
dirty worktree, a commit mismatch), `BLOCKED` (autonomous, but no verifier
configured, or one that couldn't even run). Both `FAILED` and `BLOCKED`
disable the watch (no repeated re-verification against unchanged pane
output) and set the v2 policy's `blocked_reason`, so no further autonomous
send happens until an operator fixes the underlying issue and explicitly
`supervisor_watch`s the target again.
# terminal-mcp
