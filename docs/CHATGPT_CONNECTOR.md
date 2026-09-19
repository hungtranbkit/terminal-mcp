# The ChatGPT connector endpoint

**The canonical ChatGPT connector endpoint is the compact surface on
`127.0.0.1:8768/mcp` (server identity `terminal-mcp-chatgpt-v1`, 11 tools).
The full 293-tool `127.0.0.1:8766/mcp` is NOT the ChatGPT connector
endpoint** — it remains the endpoint for Claude Code, the dashboard and every
admin path.

## The incident this exists for

Audited 2026-09-19. The live controller answered `tools/list` with 293 tools
including `terminal_turn`, `terminal_batch_inspect` and
`terminal_enqueue_task`. The ChatGPT connector in the conversation kept
offering only six:

    terminal_list_sessions, terminal_tail, terminal_capture,
    terminal_status, terminal_send_text, terminal_send_keys

Those six are this project's **original** tool surface. They are not a
truncation of the current one — in the live `tools/list` they sit at positions
0, 1, 2, 3, 9 and 10, so nothing positional produces exactly that set. It is a
**stale catalog**, cached against the connector/tunnel identity rather than
re-read from the endpoint.

Two pieces of evidence for that reading:

* The tunnel serving it reports its name as `terminal-mcp-dell` — it was
  registered during the retired Dell topology, when the surface really was
  those six tools.
* `tunnel-client`'s own metrics show `tools/call` traffic but **no
  `tools/list`** forwarded to the backend. The catalog is answered upstream
  from cache; the backend is never asked.

The server also advertises `tools.listChanged: false`, so there is no
protocol-level signal that would invalidate a cached catalog. Nothing the
backend can do makes a cached connector re-read it.

**Consequence:** `terminal_turn` and `terminal_batch_inspect` are unavailable
at the connector layer, so every logical operation degrades into several
low-level `terminal_status`/`terminal_tail` calls — the "Called tool" spam.

### Re-pointing the same connector URL does not fix it

That was tried and it recurred after reconnect and after restart. The cache is
keyed to the connector identity, so the only durable escape is an identity
that has never been registered: a new server name, a new tunnel id, a new URL.

## What was built

A **sidecar process**, `terminal_mcp/chatgpt_sidecar.py`:

| | |
|---|---|
| Server identity | `terminal-mcp-chatgpt-v1` |
| Listens on | `127.0.0.1:8768/mcp` (loopback only, not configurable to a LAN address) |
| Backend | proxies every call to `127.0.0.1:8766/mcp` |
| Catalog | exactly 11 tools, fixed order |
| Owns | nothing — no database, no tmux, no queue/supervisor loop |

It is a **catalog filter and a proxy**. Authorization, routing, node
selection, idempotency and durable state all stay in the controller, so there
is no second copy of anything that could drift. `tools/list` serves the
*backend's own* schemas filtered to the catalog, so an upstream argument change
propagates automatically.

### The catalog

```
 1  terminal_turn               preferred: inspect / send / send_wait / wait / resume
 2  terminal_batch_inspect      many targets in one call
 3  terminal_enqueue_task       durable submission
 4  terminal_task_status        one task
 5  terminal_task_batch_status  up to 100 task states in one call
 6  terminal_wait_for_state     durable wait
 7  terminal_resume_wait        resume a wait that returned PENDING
 8  terminal_list_sessions      discovery
 9  terminal_create_session     lifecycle
10  terminal_delete_session     lifecycle
11  terminal_list_nodes         which node a session is on
```

`terminal_send_text` and `terminal_send_keys` are **deliberately absent**, not
merely deprioritised: raw keystroke injection is what `terminal_turn`'s guarded
send path replaces, and publishing them would re-offer the exact legacy shape
this surface exists to retire. `terminal_status` and `terminal_tail` are absent
for the same reason — they are what the spam was made of.

A call to anything outside the catalog is refused **at the sidecar** and never
forwarded, so the compact endpoint cannot be used to reach the other 282 tools.

### How to tell, from inside a chat, which surface you are on

The sidecar's MCP `instructions` name the surface, its version and all 11
tools, and state that a list containing `terminal_send_text` or lacking
`terminal_turn` is the known regression. That is the diagnostic gap that let
this recur: a stale connector and a healthy one used to look identical.

## Deploy

```bash
# 1. the console script is new, so an existing venv does not have it yet
.venv/bin/pip install -e .

# 2. install + enable + prove the catalog is really served
deploy/install-chatgpt-sidecar.sh
```

The installer refuses to proceed if the console script is missing or if the
port collides with the controller, and it finishes by running a real
`initialize` + `tools/list` against the sidecar and asserting the result is
neither empty nor the legacy six.

### Tunnel

```bash
cp deploy/tunnel/tunnel-client-chatgpt-v1.yaml.example \
   ~/.config/tunnel-client/terminal-mcp-chatgpt-v1.yaml
# edit: put a NEW tunnel_id in it
tunnel-client run --profile terminal-mcp-chatgpt-v1
```

A **new `tunnel_id` is required.** Reusing the existing
`tunnel_6a952da18e308191bfeb3c138409704b` would carry the stale catalog
straight over. `tunnel-client` maps one MCP url per channel and does not
transparently forward subpaths, which is why this is a second profile on its
own health port (8769) rather than a path on the existing one.

### Changing the connector URL once is unavoidable

The stale catalog is bound to the existing connector identity. No backend
change can invalidate it — that is the finding, not a limitation of this
implementation. The URL changes **once**, to the new tunnel, and the versioned
identity means a future catalog change can be shipped as `-v2` without
repeating this.

## Ports

| Port | Process | Surface |
|---|---|---|
| 8766 | `terminal-mcp-http` | full 293 tools — Claude Code, dashboard, admin |
| 8767 | `tunnel-client` (existing) | health/admin for the full-surface tunnel |
| 8768 | `terminal-mcp-chatgpt-v1` | **compact 11 tools — the ChatGPT connector** |
| 8769 | `tunnel-client` (new) | health/admin for the compact tunnel |

## Security

The sidecar binds `127.0.0.1` only, and that is not an overridable default —
there is no LAN/overlay socket here to reach. `tests/test_chatgpt_sidecar.py`
asserts that `lan_route_policy.is_allowed_on_lan` refuses `/mcp` **and**
`/mcp/chatgpt-v1` for every method, so the P0 that
`lan_route_policy.py` closed (the full `/mcp` reachable unauthenticated on the
tailnet socket) cannot reappear under a new path.

The sidecar holds no credential of its own: it reaches the controller over
loopback, and the controller's own authorization gates apply to every
forwarded call exactly as they do for a local client.
