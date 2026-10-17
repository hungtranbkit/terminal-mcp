# Unified Memory for Terminal MCP: architecture audit and proposed design

Status: **MVP scope approved and implemented**. See `docs/unified-memory.md` for the delivered contract and staging commands; production activation remains separately gated.
Audited base: `79d93e8430703f0fdb4b9d92c6caece91cdb1227`.

## Outcome and constraints

Provide a durable, searchable view of Git repositories, projects, installed skill metadata, tasks, nodes, and explicitly shared memory across enrolled Terminal MCP nodes. Continue serving permitted cached results offline, with honest freshness and provenance. Discover joined nodes without treating discovery as authorization. Changes are additive: no deletion of source data, no automatic retention/compaction of the new memory journal, no migration that overwrites existing stores. Stage on isolated databases, sockets, and processes before any production activation. Existing production-approval restrictions remain in force.

User-approved scope: share metadata and selected redacted notes/checkpoints, not all source files, terminal transcripts, credentials, or executable skill bodies. No embeddings service, new cloud account, credential redistribution, or automatic execution of retrieved content is required.

## Audit: reusable sources of truth

| Area | Existing implementation | Implication |
| --- | --- | --- |
| Node identity/enrollment | `node_registry.py`, node-agent authentication, connection store | Reuse enrollment and node IDs; no parallel membership authority. |
| Fleet cache | `fleet_registry.py`, `fleet_service.py`, `fleet_registry_read.py` | SQLite/WAL, revisions, tombstones, offline view already exist. |
| Exchange/scheduling | `fleet_sync.py`, `fleet_loop.py` | Authenticated HTTP and controller-driven polling/backoff already exist. |
| Git/project identity | `project_identity.py`, `fleet_projection.py` | Remote-derived project identity; per-node checkout facts must remain distinct. |
| Skills/projects | `agent_registry.py`, `skill_packages.py`, `skill_provider.py` | Reuse registered records and approved-root, symlink-safe discovery. Discovery does not install or execute skills. |
| Tasks | Backlog, queue, work stores | Project read-only task summaries; retain scheduling and status transitions in their authoritative stores. |
| Session memory | `session_knowledge.py`, `core.terminal_knowledge_search` | Persistent redacted output with FTS and permission-filtered reads; not a unified replicated memory store. Existing compaction can delete old chunks, so do not reuse that policy for this no-deletion task. |
| Project knowledge | `project_knowledge.py` | Git-aware Markdown maps with commit provenance; ingest only approved documents without changing originals. |
| Secret handling | `redaction.py`, `scrub_knowledge`, `scrub_payload` | Reuse defenses, but metadata field-name checks alone are insufficient for free text. |
| Discovery | `lan_discovery.py`, worker discovery | Bounded candidate discovery already exists; do not start network scans implicitly on search or join. |

## Verified gaps

A synthetic, temporary-database probe against the audited base produced:

1. Publish 2,001 objects, exchange twice with the same peer: destination remains at 2,000; second push applies zero. `MAX_BATCH` slicing and timestamp filtering do not supply durable pagination/cursor advancement.
2. An existing `node:b` owned by b accepts a higher revision claiming owner a through `merge`. Local `publish` ownership checks do not establish equivalent inbound enforcement. This is a store-level trust-boundary finding, not a claim that an unauthenticated remote exploit was demonstrated.
3. `scrub_payload({summary: 'Authorization: Bearer <synthetic value>'})` accepts the text. No actual credential was used or printed. Free-text memory requires value redaction and validation before persistence, indexing, export, and error logging.
4. Current fleet kinds are node/session/project/ssh_target, without task/skill/memory projections. Search and freshness are fragmented across stores.
5. A node-agent exchange reads `from` from the body. That field cannot become an authenticated principal merely by naming a node.

Baseline: **303 passed in 8.14s** across fleet registry/API/loop/read, project knowledge, session knowledge, and redaction tests. Baseline tests do not cover the three reproduced gaps. Probe used temporary SQLite files only. No production mutation or new network scan occurred.

## Options and recommendation

**Recommended: extend the existing fleet projection/cache boundary, with a dedicated additive memory index and versioned incremental exchange.** Preserve source stores; controller mediates authenticated replication; each node keeps a durable local cache and outbox. This fits deployed authentication and offline requirements with the smallest migration surface.

Alternative: federate searches on demand against each node. Smaller initial index, but cannot satisfy offline search or dependable discovery of historical memory.

Alternative: introduce a separate full-mesh memory service with peer credentials/signatures. Removes controller dependence for live replication, but adds identity, credential distribution, conflict, and deployment obligations outside this request's necessary scope. Offline cached reads do not require full-mesh live replication.

## Proposed records and indexing

Use versioned envelopes containing `kind`, `object_id`, `owner_node_id`, `project_id`, `revision`, schema version, sanitized payload/hash, source reference, verified Git commit where applicable, visibility, and observation time. Observation time is display/freshness evidence, never a replication cursor.

- Project: canonical remote-derived identity. Repository/checkout: node-local identity referencing that project, normalized remote without credentials/query/fragment, path, branch, HEAD, dirty/observed status. Local-only repos remain explicitly node-scoped; do not merge by display name.
- Node: enrolled identity, protocol capabilities, last contact, membership/cache status. Discovered endpoints remain candidates until authenticated through existing enrollment.
- Skill: registered skill ID/version/hash and provenance; no automatic copying/execution of prompt bodies or hooks.
- Task: owner-node + authoritative task ID, project, status, dependency references and bounded redacted summary. Memory cannot dispatch tasks or mark them complete.
- Memory: immutable entry/revision identity, project/node scope, bounded title/body/tags, provenance and visibility. Import approved notes/checkpoints explicitly; preserve source documents and revision history.

Use additive SQLite migrations with a sanitized append-only change journal, current-object projection, FTS5 index, per-peer sync state, and policy records. Persist journal/projection/index atomically. Search returns bounded snippets with source, owner, revision, verified commit, last sync, and stale/offline indicators. Apply ACL/project/node filters before ranking and limiting so inaccessible records cannot leak counts or starve authorized results. Parameterize queries; literalize user FTS input. Provide a bounded SQL fallback if FTS5 is unavailable.

No automatic pruning or deletion is enabled. Quotas stop new ingestion with an explicit capacity error; they do not silently erase old data. Logical withdrawal/revocation hides data from reads and replication while retaining local audit history under its access policy.

## Incremental synchronization

Negotiate a new capability/version without changing legacy response semantics. Use a monotonic local sequence and database epoch, independent push and pull cursors per authenticated peer and policy scope, and bounded pages by object count and encoded bytes. Query pages in SQL, not by exporting the entire database before slicing.

Receiver verifies principal, schema, kind, ownership, policy, limits and redaction before atomically persisting accepted changes and acknowledgement. Sender advances a cursor only after acknowledgement is durable. Retry after timeout/crash may resend a page; `(owner, object, revision, sanitized hash)` makes replay idempotent. Same revision with a different hash is a visible conflict, not a timestamp tie-break. Owner replacement is refused. Transport-supplied identity overrides untrusted body identity.

Direct node submissions may publish only their owned objects. The explicitly trusted existing controller relay may forward validated owner records within configured project/node sharing scopes; arbitrary peers may not relay claims as authority. Local-owned records cannot be overwritten by a remote claim. A single canonical owner writes each object; a different node produces a separate observation or explicit conflict, never a silent takeover.

A changed database epoch, invalid cursor, or policy-generation mismatch triggers a bounded resnapshot from the journal/current state without deleting destination history. Permission checks apply before export, not only to returned search results. A quiet subsequent exchange transfers no object payloads. Backoff and per-cycle limits prevent one unavailable node starving others. Legacy peers retain metadata compatibility and report the new memory capability unavailable; do not ship unrestricted memory via the legacy endpoint.

## Join, offline behavior, and permissions

States are explicit: DISCOVERED, NEEDS_AUTH, ENROLLED, SYNCING, CURRENT, STALE/OFFLINE, and BLOCKED_PERMISSION/UNSUPPORTED. Enrollment triggers an incremental/bootstrap exchange only for authorized scopes. Reconnect resumes acknowledged cursors. Discovery alone grants neither data access nor terminal input.

Default new memory visibility is local/private. Existing authenticated operators explicitly configure project-to-node sharing through the existing authenticated management boundary. There is no public memory endpoint. Never trust request-supplied owner, project, or visibility to expand the caller's grant.

Offline reads use cached policy, never default-open fallback. For remotely owned content, require an unexpired offline-read lease (initial proposed limit: 15 minutes, bounded by the configured grant lifetime); unknown/expired policy or clock rollback fails closed while preserving data. Local-owned reads retain local policy. Revocation cannot retract bytes already delivered to another machine: do not share with untrusted nodes, and state this limitation clearly. On reconnection, process policy changes before serving/exporting newly synchronized data; scope changes reset cursors under the new policy generation.

Secret handling is defense in depth: allowlisted metadata fields, recursive string redaction using existing hardened redaction, reject known credential files/PEM bodies, remove credential-bearing URL components, validate again before FTS/journal/export, and emit only sanitized error classes/counts. Source text is not logged on refusal. Pattern redaction cannot prove arbitrary prose secret-free, which is why source selection and explicit sharing remain mandatory. Retrieved memory is marked untrusted data and cannot alter tool permissions, install skills, or authorize actions.

## Verification and staged rollout

Before product changes, add failing regressions for >2,000 records, duplicate timestamp boundaries, ownership substitution, free-text/URL/nested secrets, crash-before-ack and crash-after-commit, and denied-project search/export. Then implement in slices:

1. Harden inbound identity/ownership and add journal/cursor migrations; preserve legacy compatibility.
2. Add bounded projections for Git/projects/skills/tasks/nodes, selected memory ingestion, search, and policy enforcement.
3. Wire capability-negotiated authenticated exchange and enrollment/reconnect scheduling, offline policy/freshness, and observable sync state.
4. Document operator controls and rollout; validate isolated staging before considering production.

Acceptance tests must include three persistent nodes, restart all three, disconnect controller/one node, create independent authorized changes, reconnect, and verify convergence without duplicate entries or overwriting another owner. Exercise at least 4,501 objects, partial final pages, byte limits, permission revocation, malicious owner/from claims, unknown schema/capability, restricted-source/symlink discovery, and a seeded secret corpus. Verify original/source databases are unchanged by import; new journal retains all accepted revisions. Staging uses synthetic seeded data, separate HOME/XDG/database/tmux sockets and loopback ports; smoke checks assert auth, permission-scoped search, cursors, offline status, and replay recovery.

Run targeted suites, applicable full gates and native Linux/macOS checks on the final HEAD. Review API compatibility and migrations before merge. Production is a separate approved rollout after successful staging; rollback disables the new capability/repoints code while preserving new tables and data. No destructive downgrade migration, force push, credential/quota/DNS change, or automatic deletion.

## Review decision needed

Confirm the recommended metadata + selected redacted notes/checkpoints scope, controller-mediated replication with offline caches, and default-private sharing policy. This document is an implementation proposal, not a claim that Unified Memory has been shipped. After design review, produce the implementation plan and execute the approved scope with test-first changes.
