# The ChatGPT connector endpoint

**The canonical ChatGPT connector endpoint is the compact surface on
`127.0.0.1:8768/mcp` (server identity `terminal-mcp-chatgpt-v1`, surface
version 2.1.0, which publishes exactly ONE tool: `terminal_turn`). The full
293-tool `127.0.0.1:8766/mcp` is NOT the ChatGPT connector endpoint** — it
remains the endpoint for Claude Code, the dashboard and every admin path.

> The catalog was 11 tools when this surface first shipped. It was narrowed to
> the single `terminal_turn` so that one logical orchestration step is one
> "Called tool" line. Narrowing the CATALOG did not narrow the CAPABILITY:
> every one of the other ten is still callable here, because a cached client
> that calls it by its old name is TRANSLATED into the equivalent
> `terminal_turn` action. See "Cached-call translation" below.

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
| Catalog (`tools/list`) | exactly 1 tool: `terminal_turn` |
| Accepted at `tools/call` | `terminal_turn` plus the 11 legacy names it replaced, translated |
| Owns | nothing — no database, no tmux, no queue/supervisor loop |

It is a **catalog filter and a proxy**. Authorization, routing, node
selection, idempotency and durable state all stay in the controller, so there
is no second copy of anything that could drift. `tools/list` serves the
*backend's own* schemas filtered to the catalog, so an upstream argument change
propagates automatically.

### The catalog

`tools/list` serves exactly one row — `CATALOG` in `chatgpt_sidecar.py`:

```
 1  terminal_turn   action = inspect | send | send_wait | wait | resume |
                             list_sessions | list_nodes | create_session |
                             delete_session | enqueue_task | task_status |
                             task_batch_status
```

`inspect` takes either one `target` or many `targets` — **`terminal_turn`
with `targets` IS the canonical batch-inspect path**; there is no separate
batch tool to publish.

### Cached-call translation (the compatibility contract)

A ChatGPT conversation can only emit the schema it cached, and nothing the
backend does invalidates that cache (`tools.listChanged: false` — the same
root cause as the original incident). So every legacy name this surface has
EVER advertised is still accepted at **call** time and rewritten into the
equivalent `terminal_turn` action — `_LEGACY_TRANSLATIONS` / `CALL_COMPAT`:

```
terminal_batch_inspect     -> inspect (targets kept)
terminal_status            -> inspect (session -> target, tail_lines=1, compact)
terminal_list_sessions     -> list_sessions
terminal_list_nodes        -> list_nodes
terminal_create_session    -> create_session (name -> target)
terminal_delete_session    -> delete_session (name -> target)
terminal_send_text         -> send (session -> target)
terminal_wait_for_state    -> wait
terminal_resume_wait       -> resume
terminal_enqueue_task      -> enqueue_task (session -> target, prompt -> text)
terminal_task_status       -> task_status
terminal_task_batch_status -> task_batch_status
```

**Translated, not forwarded** — and that distinction is the point. These names
are all still real tools on the full controller, so forwarding them verbatim
would "work" while silently buying the older, thinner semantics: the
unguarded send path, and the client-side polling that the server-side
wait/task-following moved off the client. Translating means a cached
conversation executes the same canonical operation a new one does, and every
later improvement to `terminal_turn` reaches both without the client ever
updating.

This is a **call-time allowance and never a listing**: nothing here can put a
row back into `tools/list`, and every translation lands on an action
`terminal_turn` already validates, so the controller remains the only
authority on authorization and idempotency.

Still refused, deliberately: `terminal_send_keys` (raw keystroke injection has
no guarded equivalent) and **every admin tool on the 293-tool surface**, which
answer `TOOL_NOT_ON_THIS_SURFACE`. A cached call naming a translatable tool but
missing a required argument answers `INVALID_ARGUMENT` rather than guessing a
target or a prompt.

`terminal_send_text` and `terminal_send_keys` are **deliberately absent**, not
merely deprioritised: raw keystroke injection is what `terminal_turn`'s guarded
send path replaces, and publishing them would re-offer the exact legacy shape
this surface exists to retire. `terminal_status` and `terminal_tail` are absent
for the same reason — they are what the spam was made of.

A call to anything that is neither the catalog nor a translatable legacy name
is refused **at the sidecar** and never forwarded, so the compact endpoint
cannot be used to reach the admin surface.

### How to tell, from inside a chat, which surface you are on

The sidecar's MCP `instructions` name the surface and its version
(`SURFACE_VERSION`, bumped whenever `CATALOG` changes) and state that a list
lacking `terminal_turn` is the known regression. That is the diagnostic gap that let
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

### Tunnel — what is actually deployed

**As deployed on the canonical HP controller there is ONE tunnel profile, and
it was repointed rather than replaced.** `~/.config/tunnel-client/
terminal-mcp.yaml` keeps the existing `tunnel_6a952da18e308191bfeb3c138409704b`
and points its `main` channel at the compact surface:

```yaml
health:
  listen_addr: "127.0.0.1:8767"
mcp:
  server_urls:
    - channel: main
      url: "http://127.0.0.1:8768/mcp"     # the compact sidecar, not 8766
```

Run by `terminal-mcp-tunnel.service` (`tunnel-client run --profile
terminal-mcp`). There is no second profile and no 8769 listener.

**Why the "new tunnel_id" plan below was not needed.** Reusing the existing
tunnel does carry the stale catalog over — that part of the original finding
holds, and it is why a cached conversation still calls
`terminal_batch_inspect`. But cached-call translation makes a stale catalog
HARMLESS instead of fatal: the old name arrives and executes the current
canonical operation anyway. Translating was preferred over a new connector URL
because it fixes every already-cached conversation, including ones nobody will
reconnect, whereas a new URL only helps conversations created after it.

Service ordering matters here: the tunnel's upstream is 8768, so
`terminal-mcp-tunnel.service` must order itself after
`terminal-mcp-chatgpt-v1.service`, not only after `terminal-mcp-http.service`.
It originally did the latter, and a boot where the tunnel won the race left
`/readyz` serving a stale `connection refused` against 8768 indefinitely while
`/metrics` showed healthy 200s — a health check that lies. Corrected by the
drop-in `terminal-mcp-tunnel.service.d/10-after-chatgpt-sidecar.conf`.

### If you DO want a separate tunnel identity

Still supported, and still the only way to make the connector re-read the
CATALOG itself rather than rely on translation:

```bash
cp deploy/tunnel/tunnel-client-chatgpt-v1.yaml.example \
   ~/.config/tunnel-client/terminal-mcp-chatgpt-v1.yaml
# edit: put a NEW tunnel_id in it
tunnel-client run --profile terminal-mcp-chatgpt-v1
```

`tunnel-client` maps one MCP url per channel and does not transparently
forward subpaths, which is why that would be a second profile on its own
health port (8769) rather than a path on the existing one. The versioned
server identity means such a catalog change can ship as `-v2` without
repeating the original incident.

## Ports

| Port | Process | Surface |
|---|---|---|
| 8766 | `terminal-mcp-http` | full 293 tools — Claude Code, dashboard, admin |
| 8767 | `tunnel-client` (the one deployed profile) | health/admin; its `main` channel forwards to 8768 |
| 8768 | `terminal-mcp-chatgpt-v1` | **compact surface, 1 published tool — the ChatGPT connector** |
| 8769 | `tunnel-client` (only if a second profile is added) | health/admin for a separate compact tunnel; **not deployed** |

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
