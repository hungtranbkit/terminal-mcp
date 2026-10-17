# Unified Memory MVP

Opt-in metadata and curated-note synchronization through the existing controller.
The feature is disabled unless `TERMINAL_MCP_MEMORY_CONFIG` names a local JSON file.
It does not execute retrieved instructions, copy source trees/transcripts/skill bodies,
install skills, enroll unknown nodes, or change terminal permissions.

## Configuration

Use a separate database and explicit approved roots on each node. For example:

```json
{
  "node": "worker-a",
  "controller": "controller",
  "database": "/absolute/private/state/memory.sqlite3",
  "projects": {
    "example": {
      "roots": ["/absolute/approved/repo"],
      "skills": ["/absolute/approved/repo/skills/example"],
      "notes": [{"path": "/absolute/approved/repo/checkpoint.md", "kind": "checkpoint"}],
      "services": [{"title": "api", "status": "configured", "endpoint": "https://example.invalid"}]
    }
  },
  "share_projects": [],
  "interval_seconds": 60
}
```

`share_projects` defaults empty: all projections are private. Set it to
`["example"]` only for projects explicitly authorized for this controller.
Project keys are operator-assigned cross-node identities; they must agree on the
controller and workers. Each checkout retains its node/path identity.
The controller's config uses its own node ID and `controller` equal to that ID,
and adds `"peers": {"worker-a": ["example"], "worker-b": ["example"]}`.
Peers must already be enrolled in the controller's existing node client registry.
No tokens go in the memory config. An unenrolled peer reports `NEEDS_AUTH`.

Node-agent startup and HTTP-controller startup load the config and start the bounded
background worker. Restart only an isolated staging instance during evaluation;
production activation requires separate approval. Config changes take effect on restart.
The optional `session_database` is opened read-only; only local-node sessions with
`read_granted=1` and approved repository roots contribute status metadata.

The projector reads Git repository/branch/worktree facts, explicit project/service
entries, selected SKILL.md frontmatter plus digest, `.terminal-mcp/backlog.json`
status fields, and explicit bounded note/checkpoint files. It does not crawl all
registered projects or ingest task descriptions. Selected notes are operator-curated:
redaction removes recognized secrets, not arbitrary confidential prose. Do not select
unreviewed files. Symlink escapes and credential filenames are refused.

## Permissions and protocol

The existing node-agent bearer authenticates the configured controller. It grants
only configured project scopes; request bodies cannot grant roles. Join checks
version and node identity. Pull can narrow the server-side scope, never expand it.
Only the controller relays other nodes' records. This is node/project RBAC, not a
new per-human account or per-document ACL system. Anyone with local filesystem
access to the database is within the trusted operator boundary.

`POST /v1/memory/{join,pull,push,search}` is absent when disabled. It uses bounded
JSON payloads and existing bearer authentication. Search filters project scopes
before limit/FTS matching and exposes provenance, observation time, expiry and
`untrusted_output`. HTTP MCP adds `terminal_memory_search` only when enabled,
with the existing read-permission check. The compact sidecar catalog is unchanged.
The trusted local CLI supports:

```sh
terminal-mcp-memory --config /absolute/memory.json --node worker-a refresh
terminal-mcp-memory --config /absolute/memory.json --node worker-a search checkpoint --project example
terminal-mcp-memory --config /absolute/memory.json --node worker-a status
```

The standalone CLI can project and search offline; it does not load controller
credentials or enroll peers. `status` describes that process, not a remote daemon.

## Durability, revocation and offline operation

Sanitization happens before the SQLite WAL journal, current view and FTS index.
Journal writes and received cursors commit together; projector ownership commits
with publication. Bounded snapshot progress persists across cycles and restarts.
Normal cycles send deltas; periodic snapshots renew absolute 15-minute cache leases.
Downstream relays inherit the origin deadline and cannot extend it. Expired cached
rows remain stored but disappear from search/export. Clock rollback fails closed.

Removing a configured source/project or revoking session read permission emits a
bodyless private revision. Errors reading a formerly projected source also withdraw
it until a later successful refresh. History and source files remain intact. An
unreachable node can retain previously authorized search results until its existing
lease expires (at most 15 minutes); this is the explicit offline revocation bound.
Reducing project scope likewise does not imply instantaneous remote deletion.

Record IDs include the originating database incarnation. Rebuilding a node database
therefore cannot collide with old revisions. Direct epoch recovery expires the
controller's old owner cache; downstream copies expire by their inherited deadline.
No source, historical journal, credential, or existing fleet database is deleted or
migrated. The versioned memory API is independent of legacy fleet exchange.

## Verification and staging

```sh
python -m pytest -q tests/test_unified_memory.py tests/test_unified_memory_http.py tests/test_memory_projection.py tests/test_memory_runtime.py
python scripts/stage_unified_memory.py
```

The staging script creates a controller plus two loopback HTTP node agents with
disposable databases, homes and tokens. It exercises pagination, authentication,
private defaults, redaction, relay, restart, offline cache and withdrawals; it stops
only its own child processes. CI runs both commands on Ubuntu and macOS. This is
multi-process local staging, not a deployment to existing production nodes.
