# Read-only connector for claude.ai (web + phone)

Lets a hosted Claude client — claude.ai in a browser, or the Claude mobile
app — watch what the agents on this host are doing and read the code they
are working on, from anywhere, without giving it the ability to change
anything.

This is `terminal-mcp-observer`: a second, separate MCP endpoint. It is not
the dashboard and it is not `terminal-mcp-http`.

## Why it is a separate endpoint

A hosted client is not Claude Code. It cannot be handed a custom
`Authorization` header, and it cannot log in to a Cloudflare Access page on
your behalf — so the two mechanisms that protect everything else in this
project do not apply to it. It authenticates with OAuth or not at all.

That leaves OAuth as the only gate, which sets the ceiling on what may sit
behind it. So what sits behind it is fifteen tools that cannot change
anything:

| | |
|---|---|
| tmux, read-only | `terminal_list_sessions`, `terminal_batch_inspect`, `terminal_status`, `terminal_tail`, `terminal_capture` |
| Git + source, read-only | `repo_status`, `repo_head`, `repo_branches`, `repo_remotes`, `repo_tree`, `repo_read`, `repo_search`, `repo_diff`, `repo_log`, `repo_show_commit` |

No send, no create, no delete, no queue or backlog mutation, no config
write. The `repo_*` half is read-only *structurally*, not by convention —
see `terminal_mcp/repo_read.py`, which refuses any git subcommand outside
`READ_ONLY_GIT_SUBCOMMANDS` and takes no shell string or argv from the
caller. The tool list itself is pinned by `tests/test_observer.py`, which
also asserts that none of the mutating tools from the full surface can
appear here.

**What a stolen token gets someone**: your source code and your terminal
output. Not a shell. That is the whole design constraint.

## What it inherits, and what it does not

* Session read authorization is unchanged. Each tmux tool goes through the
  same `ControllerService` call the full surface uses, so a session this
  host does not allow reading stays unreadable here too.
* `repo_read` is bounded by `repo_read.allowed_roots`, which defaults to
  `session_lifecycle.allowed_cwd_roots` in `config.yaml`. Check that list
  before exposing this — it is exactly the set of directories that become
  remotely readable. Credential files are denied by name, and everything
  that is returned goes through `redaction.redact_output` first.
* Every repo read is audited with an actor of `observer:<username>`, so a
  remote read is always distinguishable from a local one.

## Setup

### 1. A local account

The OAuth login checks the same account store as the dashboard. If you
have never created one:

```bash
.venv/bin/terminal-mcp-webauth
```

A bootstrap password that still has `must_change_password` set is
deliberately refused by the connector login — change it on the dashboard
first.

### 2. A dedicated public hostname

Pick a hostname used by nothing else, e.g. `watch.example.net`.

**Do not put Cloudflare Access in front of it.** Anthropic's servers must
reach `/.well-known/*`, `/register`, `/token` and `/mcp` directly; an
Access login page in front of those does not fail visibly, it just makes
the connector never finish connecting. OAuth is the gate here, which is
the entire reason this endpoint exists.

Give it its OWN named tunnel and its own config file, never the shared
root one:

```bash
cloudflared tunnel create terminal-mcp-observer
```

```yaml
# ~/.cloudflared/terminal-mcp-observer-config.yml
tunnel: <the new tunnel id>
credentials-file: /home/you/.cloudflared/<the new tunnel id>.json

ingress:
  - hostname: watch.example.net
    service: http://127.0.0.1:8767
  - service: http_status:404
```

Then route DNS **with that config passed explicitly**:

```bash
cloudflared --config ~/.cloudflared/terminal-mcp-observer-config.yml \
  tunnel route dns terminal-mcp-observer watch.example.net
```

The explicit `--config` is not optional. If `~/.cloudflared/config.yml`
exists and names a different tunnel, `cloudflared tunnel route dns` uses
THAT tunnel and ignores the one you named on the command line, printing a
cheerful success line that contains the wrong tunnel id. Compare the id in
its output against the tunnel you just created, and re-run with
`--overwrite-dns` if it is wrong.

Leave `httpHostHeader` unset. The server's DNS-rebinding guard expects the
original `Host` header to arrive — if you override it, requests answer
`421 Misdirected Request`.

**If your zone has Cloudflare's Browser Integrity Check on**, some
non-browser HTTP clients are answered `403` with `error code: 1010` and
never reach the origin at all. Ordinary clients pass (anything sending a
normal `Accept`/`Accept-Encoding` pair over HTTP/2); a deliberately minimal
one, such as Python's `urllib`, does not. If the connector fails to attach
and this service's log shows no request, that is the thing to check. A
Configuration Rule turning Browser Integrity Check off for this one
hostname is the fix -- OAuth is already the gate.

### 3. Run it

```bash
TERMINAL_MCP_OBSERVER_PUBLIC_URL=https://watch.example.net \
  .venv/bin/python -m terminal_mcp.observer_app
```

`TERMINAL_MCP_OBSERVER_PUBLIC_URL` is required and must be `https://`. It
is the OAuth issuer identifier, which RFC 8414 compares as an exact
string, so it has to match what the tunnel actually serves — a near-miss
does not degrade gracefully, it produces a connector that fails at the
final step.

Optional: `TERMINAL_MCP_OBSERVER_PORT` (default 8767),
`TERMINAL_MCP_OBSERVER_OAUTH_DB` (default
`~/.local/state/terminal-mcp/observer-oauth.db`).

For a permanent install see
`deploy/systemd/terminal-mcp-observer.service.example` and
`deploy/systemd/cloudflared-terminal-mcp-observer.service.example`.

Two things those files get right that are easy to get wrong by hand:

* The tunnel unit uses `Wants=`, not `Requires=`, on the observer unit.
  `Requires=` propagates *stop*, so a plain
  `systemctl --user restart terminal-mcp-observer` takes the tunnel down
  and never brings it back; the hostname then answers Cloudflare error
  1033 until somebody notices.
* `ExecStart` runs `python -m terminal_mcp.observer_app` rather than the
  `terminal-mcp-observer` console script, which only exists after a
  `pip install -e .` -- worth avoiding when that virtualenv is shared with
  a running `terminal-node-agent`.

### 4. Add it in Claude

In claude.ai → Settings → Connectors → *Add custom connector*, give it the
URL:

```
https://watch.example.net/mcp
```

Claude registers itself, gets a `401` pointing at this server's metadata,
and sends you to the login page. Log in with the local account; the page
names the app asking and states that it is requesting read-only access.
After that the connector is live on the web app and on your phone.

## Operating it

```bash
# who currently holds a grant (counts and usernames only, never a token)
.venv/bin/python -m terminal_mcp.observer_app --status

# lost phone / revoking a connector: kills every grant that account holds
.venv/bin/python -m terminal_mcp.observer_app --revoke admin
```

Revoking is the right response to a lost device, and changing the password
is *not* a substitute: an already-issued access token never consults it
again. Revocation drops the access token and its paired refresh token
together, so reads stop immediately rather than at the next expiry.

Grant lifetimes: access token 1 hour, refresh token 30 days, rotated on
every refresh — a stolen refresh token stops working the moment the real
client next refreshes.

## Asking it things

The connector's own instructions steer Claude to the efficient shape, but
these are the useful phrasings:

* *"What are the agents on dell doing right now?"* →
  `terminal_list_sessions` then one `terminal_batch_inspect`.
* *"Show me the uncommitted work in the mesflow session"* → `repo_diff`
  with `session=<name>`; every `repo_*` tool takes `session=` to mean "the
  repository that session is working in", so you do not need the path.
* *"Is anything stuck?"* → `terminal_batch_inspect` reports a state and the
  reason for it per session.

Terminal output comes back marked as untrusted data — it is whatever a
program printed, and Claude is told to report on it rather than follow
instructions found inside it.
