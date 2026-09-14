# Node token rotation and revocation

`blg_a3cc401d8275`. A node's bearer token used to be a string captured at
process start on the node and typed into a file on the controller.
Rotating one meant editing both and restarting the agent — which is why
`macbook` was rotated on 2026-09-09 and `dell-5530` was not: its restart
would have cost six live sessions. A credential you cannot afford to
rotate is one you cannot revoke either.

## The pieces

| file | what it owns |
|---|---|
| `terminal_mcp/node_credentials.py` | the durable record: one row per token, sha256 only, `ACTIVE` / `GRACE` / `REVOKED`, and `verify()`'s fail-closed verdict |
| `terminal_mcp/token_rotation.py` | the *operation*: staging, delivery, confirmation, and the controller's own outbound copy |
| `terminal_mcp/node_agent.py` (`AgentCredential`) | the node's half: swap the live token in place, keep accepting the previous one during the handoff |
| `dashboard.py` routes under `/dashboard/api/nodes/{node_id}/token*` | the operator and machine surfaces |

There is deliberately **no dashboard UI**. This feature is API-only.

## How a rotation actually runs

```
operator                controller                         node
   |  POST .../token/rotate  |                               |
   |------------------------>| mint NEW (ACTIVE)             |
   |                         | old token -> GRACE            |
   |                         | stage NEW plaintext, 0600     |
   |<-- token_id, state ---- |                               |
   |   (never the token)     |<---- heartbeat (old token) ---|
   |                         |----- 200 + token_refresh ---->|
   |                         |<---- POST .../token/refresh --|
   |                         |----- the new token ---------->| adopt in memory,
   |                         |                               | write token file
   |                         |<---- heartbeat (NEW token) ---|
   |                         | == proof ==                   |
   |                         | move outbound copy            |
   |                         | REVOKE the old token          |
```

Three properties worth stating explicitly:

* **The operator never sees a token.** `rotate` returns fingerprints and
  state. The secret goes to the node, over the channel the node
  authenticates itself on, and nowhere else.
* **The grace window is open-ended by default.** It closes on
  *confirmation*, not on a clock. A fixed window locks out any node that
  happened to be offline for it — precisely when you are most likely to
  be rotating. Pass `grace_seconds: 0` for an immediate cutover when a
  token is believed compromised, or an integer for a hard deadline.
* **Rotation is idempotent while in flight.** Calling `rotate` again
  returns the same staged rotation instead of minting a second one —
  which would push the token the node is actually holding out of grace.
  `force: true` is the deliberate override.

## API

| route | guard | body | returns |
|---|---|---|---|
| `GET  /dashboard/api/nodes/{id}/token` | dashboard read | — | statuses + fingerprints |
| `POST /dashboard/api/nodes/{id}/token/adopt` | dashboard mutation | — | brings a pre-existing token under management, idempotent |
| `POST /dashboard/api/nodes/{id}/token/rotate` | dashboard mutation | `{grace_seconds?, force?}` | staged rotation, no secret |
| `POST /dashboard/api/nodes/{id}/token/revoke` | dashboard mutation | `{token_id?, reason?}` | revoked records; no `token_id` means all |
| `POST /dashboard/api/nodes/{id}/token/refresh` | **the node's own token** | — | the staged token — the one response in the system that carries a secret |

Nodes enrolled from now on are adopted automatically (`token_env_setter`
in `server_http.py`), so they are rotatable from their first heartbeat.
Nodes that predate this feature keep authenticating exactly as before
until someone calls `/token/adopt` — there is no flag day.

## Migration

`node_credentials.db` (`~/.local/state/terminal-mcp/`, 0600, WAL) is
created on first use with `PRAGMA user_version = 1`. Nothing else
migrates: no existing table changes, and a node with no row in it falls
through to the legacy env-var check.

## Revocation, and what it costs per platform

Revocation is enforced **on the controller**, at the one shared
`_verify_node_token` site. It therefore takes effect on the node's very
next request, on every platform, with no node-side action at all:

| platform | inbound (node → controller) | outbound (controller → node) | restart needed |
|---|---|---|---|
| Linux node agent | refused immediately | controller stops presenting the token immediately | **none** |
| Windows node agent | refused immediately | same | **none** |
| macOS node agent | refused immediately | same | **none** |

A revoked node keeps running its own sessions — revocation removes its
place in the fleet, it does not kill work in progress. Re-admitting it
is a normal enrollment.

## Rotation, and what it costs per platform

Rotation needs the node to *collect* its replacement, so unlike
revocation it depends on which agent build the node is running:

| node agent build | rotation cost |
|---|---|
| includes `AgentCredential` (this change) | **no restart** — picked up on the next heartbeat, ≤ one heartbeat interval (20s default) |
| older build | the rotation stays staged and harmless; the old token keeps working (open-ended grace). The node adopts the new token when its agent is upgraded and restarted — one restart, which takes its tmux sessions' *attachments* but not the sessions themselves (`KillMode=process`) |

So on a node still running an older agent, rotation is *safe* but not
*complete*: nothing breaks, and nothing changes, until that agent is
upgraded. That upgrade is the rollout step this change does not perform.

## What still needs rollout approval

Everything above is implemented and tested but **not deployed**:

1. Deploying the controller half (new routes + credential store) to
   production 8766.
2. Upgrading each node agent (`dell-5530`, `hp`, the Windows nodes) to a
   build containing `AgentCredential`, which is what makes rotation
   restart-free there.
3. Adopting the existing nodes (`POST .../token/adopt` once each), after
   which their tokens become rotatable.

Until (2), treat rotation on an existing node as "stage it and wait for
the agent upgrade"; revocation needs only (1).
