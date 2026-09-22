# Terminal MCP Commercial Production V1 — Execution Contract

**Status:** binding implementation contract. `docs/REQUIREMENTS.md` remains canonical for what exists today.

## 1. Authority
Precedence: (1) live code/runtime/test/deployed-SHA evidence; (2) `docs/REQUIREMENTS.md` for feature status; (3) this file for Commercial V1 contracts; (4) `docs/COMMERCIAL_PRODUCTION_TASKS.yaml` for DAG/acceptance; (5) live durable backlog for runtime task state; (6) `.terminal-mcp/backlog.json` as projection; (7) old chats/agent prose as context only. Never mark VERIFIED/DONE from prose.

## 2. Product boundaries
V1 is an Agent Operations Platform, not a generic MCP marketplace. Preserve proven core. No second queue/session registry/audit/permission implementation; no rewrite, speculative microservices or UI-framework replacement without an accepted ADR. Secrets stay edge-local where possible. Nodes connect outbound-first. Enterprise self-hosted shares contracts rather than forking.

## 3. Ownership map
| Domain | Existing authority | Allowed extension | Forbidden |
|---|---|---|---|
| sessions | `core.py:TerminalService` + backends | commercial authorization/correlation | second session manager |
| fleet | `controller.py`, `node_registry.py`, node agents | enrollment/outbound transport | UI routing directly to nodes |
| tasks | `queue_store.py/queue_engine.py/queue_loop.py` | tenant/project metadata + policy hooks | divergent cloud queue |
| coordinator | `coordinator.py` | commercial policy context | bypass deterministic gates |
| integration | integration store/engine/reviewer/loop | Release/exact-SHA promotion | deploy from worker prose |
| grants | `grants.py` + core checks | RBAC/policy mapping | UI-only security |
| dashboard | `dashboard.py` + webauth | canonical-service views | duplicate frontend state |
| audit | existing local audit | central idempotent aggregation | delete local evidence early |
| recovery | knowledge/checkpoint stores | tenant/retention metadata | chat history as truth |
| repo | read-only `repo_*` | tenant authorization | arbitrary mutation |

Before editing `core.py/controller.py/queue_store.py/config.py/dashboard.py`, inspect active branches. One owner per shared chokepoint.

## 4. Domain model
Commercial IDs are opaque server-generated IDs; authorization never derives tenant from caller IDs. `Organization -> Workspace -> Project`; User joins Organization through Membership. Project maps to existing project_identity. Commercial Node maps to runtime node_id. Session name is routing/display, never a principal. Existing queue task_id/request_key remain execution identities.

Entities: User, Organization, Membership, Workspace, Project, Node, NodeCredential, EnrollmentToken, Policy, Approval, Entitlement, Subscription, UsageEvent, AuditEvent, Release, ReleaseChannel. Every lookup binds Organization ownership before return/mutation; foreign IDs must not disclose existence.

## 5. Authn/authz
Human browser, MCP client, node machine and support/admin are separate principals. Deny by default; enforce server-side. Roles: Owner/Admin/Operator/Developer/Viewer. Owner/Admin manage security; Operator runs policy-allowed operations; Developer creates normal project tasks but has no destructive production rights by default; Viewer never mutates. Existing dashboard grant semantics remain authoritative until a reviewed migration maps commercial policy into the same effective checks. Support access is explicit, scoped and audited.

## 6. Node enrollment/credentials
State: `UNENROLLED -> ENROLLING -> ACTIVE <-> DEGRADED`; `ACTIVE -> ROTATING -> ACTIVE`; `ACTIVE/DEGRADED -> REVOKED -> RETIRED`. RETIRED never silently reactivates.
Enrollment token is random, scoped, one-use, short-lived, hashed/secret-referenced and atomically consumed. Node credential is never logged/recoverable after issuance; rotation has bounded overlap; revocation is immediate. Heartbeats/actions include replay defense, protocol version and node identity. Reconnect uses exponential backoff+jitter. Offline state never replays completed effects.

## 7. Outbound transport
Envelope: `protocol_version,message_id,message_type,created_at,expires_at,node_id,organization_id,workspace_id,project_id,task_id,correlation_id,idempotency_key,payload_version,payload`. Transport may be at-least-once; effects are effectively-once via idempotency. ACK states: ACCEPTED, COMPLETED, REJECTED, UNKNOWN. UNKNOWN means query evidence before retry, never blind resend. Handshake negotiates protocol range; incompatible nodes return VERSION_INCOMPATIBLE + remediation.

## 8. Task/orchestration
Existing Queue v2 is canonical; use actual queue constants. Persist-before-dispatch. request_key deduplicates creation; dispatch has generation/idempotency identity. Dependencies/checkpoints are durable. BLOCKED/REWORK is not DONE. Retry resumes same task unless semantics require a new one. Checkpoint before context rollover/migration/destructive stage. Worker selection uses declared capabilities/health. Conflicting code uses isolated worktree/branch. Structured handoff required. DONE requires objective evidence.

Coordinator may decompose accepted scope, select eligible worker/node, retry safe idempotent work, checkpoint, test and request review. Human/policy approval is required for irreversible production data changes, privilege widening, secret disclosure, force/reset/clean, irreversible migration or configured release gates.

## 9. Git/worktree/release
Never force-push/reset --hard/clean/delete unknown work without destructive approval. Preserve unique commits. Janitor auto-delete requires clean + merged + inactive + no lease/process + retention satisfied; dirty => BLOCKED; unmerged => REVIEW/BLOCKED.
Handoff: task_id, base/head SHA, branch, changed paths, tests, docs/migration/rollback impact. Integration tests candidate SHA. `deploy_sha == tested_sha`; if SHA changes, retest. Never deploy untested SHA.

## 10. Idempotency
| Operation | Identity | Duplicate behavior |
|---|---|---|
| enrollment | enrollment_token_id | prior result/TOKEN_USED; never second credential |
| rotation | node_id+rotation_request_id | same generation |
| task create | project+request_key | existing task |
| dispatch | task_id+dispatch_generation | one submission |
| terminal send | session identity+submission_id | query evidence |
| migration | task_id+migration_generation | one destination |
| merge | target+handoff/head SHA | already-integrated result |
| deploy | environment+release_id+tested_sha | one effect |
| billing webhook | provider+event_id | process once |
| usage/audit | producer+event_id | ingest once |

## 11. Persistence/migration
Do not migrate proven edge stores for appearance. Edge-local: runtime/session recovery, execution queue truth, checkpoints, secret material, audit spool. Central: tenant/membership/project, node credential metadata, policy/entitlement/subscription, usage, central audit index, releases. Health/audit/usage/task projections replicate idempotently.
Every store has schema_version, owner, backup, migration and compatibility policy. Backup before destructive migration. A central PostgreSQL ADR does not imply replacing edge SQLite.

## 12. Audit/observability
Audit != telemetry. Audit envelope includes event_id/version, occurred/ingested time, actor, tenant/workspace/project, node/session/task, action/outcome, correlation/request IDs, resource and redacted metadata. Audit auth, credentials, permission, task, terminal mutation, git/release/deploy, billing/entitlement and support actions. Security audit is not sampled.
SLIs: API availability, node connection, persist/dispatch success, duplicate-effect rate, recovery/update success, p95 control latency, queue age, audit lag.

## 13. Entitlement/billing
Core contains no prices. One entitlement service returns capabilities/limits. Community/Pro/Team/Enterprise are bundles; pricing is business config. Meter product-owned resources, not provider-model tokens by default. Billing webhook is signed/idempotent. Outage uses cached grace. Downgrade may block new premium work but cannot corrupt or interrupt an in-flight destructive operation. Security revocation overrides grace.

## 14. Install/update/rollback
Linux: packaged systemd. Windows: installer must account for current ConPTY child-survival behavior. macOS: LaunchAgent. Artifacts/update metadata are signed/checksum-attested. Stable/beta channels have compatibility matrix. Preflight checks protocol/disk/checkpoint/critical operations. Staged rollout can halt. Rollback restores known-good binary/config under schema compatibility. Uninstall explicitly preserves/deletes state and revokes credential.

## 15. Threat baseline
Cover malicious terminal/repo output, compromised node, stolen enrollment token, replay, brute force, tenant enumeration, CSRF/WebSocket origin, privilege escalation, secret exfiltration, unsafe agent flags, dependency compromise and support abuse. Preserve current untrusted-output/redaction/secret-path/origin/grant controls. Missing controls map to SEC tasks; do not invent a parallel security layer.

## 16. Failure invariants
Chat reset => recover durable task/checkpoint, never restart from prose. MCP disconnect/DELIVERY_UNKNOWN => query evidence, never blind resend. Controller restart => accepted task stays durable. Node restart => report real platform survival. Partition/reconnect => backoff+jitter, no replay. DB corruption => readiness failure/restore, never silent reset. Migration fail => stop/rollback. stale identity pin => deny/regrant. context exhausted => checkpoint/rollover. test fail => REWORK. deploy failure => recorded rollback/forward-fix, never silently change SHA. duplicate billing => once. entitlement outage => grace without corrupting active state. clock skew never disables replay checks.

## 17. API/error contract
Errors: `code,user_message,correlation_id,retryable,safe_to_retry,retry_after_seconds?`; privileged detail is redacted. Stable codes include UNAUTHORIZED, FORBIDDEN, RESOURCE_NOT_FOUND, TENANT_SCOPE_DENIED, IDEMPOTENCY_CONFLICT, DELIVERY_UNKNOWN, NODE_OFFLINE, NODE_REVOKED, ENROLLMENT_TOKEN_EXPIRED/USED, CREDENTIAL_REVOKED, REPLAY_REJECTED, RATE_LIMITED, VERSION_INCOMPATIBLE, APPROVAL_REQUIRED, POLICY_DENIED, MIGRATION_FAILED.

## 18. UX IA
Top-level: Projects, Tasks, Nodes, Sessions, Releases, Audit, Usage/Billing, Settings/Security. Every view reads canonical services; security/task truth is never UI-only. Mobile responsive required.
Onboarding success: account/org -> workspace/project -> installer -> one-use enrollment -> healthy node -> MCP connection -> first safe durable task -> progress/recovery/audit visible.

## 19. Test/release evidence
Every behavior task declares unit/integration/live evidence. Mock pass never substitutes for required live node/browser/restart evidence. CI covers Linux/Windows/macOS, migrations, browser smoke, security isolation and install smoke. GA manifest: source SHA, artifact checksum/signature, SBOM, schemas, tests, security scan, E2E, chaos/load, backup/restore, upgrade/rollback, pilot results, known issues, rollback target.

## 20. Work-package template
Before coding any task: objective/user outcome; verified gap; in/out scope; affected modules/files; dependencies/shared chokepoints; implementation steps; state/API/data changes; edge cases; tests/live evidence; rollback; 15–30m slice count; parallel/conflict notes; stop/escalation conditions. Vague tasks are not executable.

## 21. ADR gates
Freeze before dependent code:
- ADR-CP-001 control-plane packaging boundary (hard to reverse).
- ADR-CP-002 central DB (default recommendation: PostgreSQL only for central multi-tenant data; keep edge SQLite).
- ADR-AUTH-001 identity provider/auth.
- ADR-TRANS-001 outbound secure transport (default WSS + versioned envelope unless evidence rejects).
- ADR-BILL-001 provider-neutral billing adapter.
- ADR-PKG-001 installer formats/platform service model.
- ADR-SIGN-001 artifact signing/provenance.
- ADR-OBS-001 telemetry backend.
After acceptance agents do not invent alternatives.

## 22. Agent operating rules
Execute, do not brainstorm inside accepted slices. Read REQUIREMENTS + this spec + task contract + live state. Do not duplicate features/stores. Keep calls bounded; use durable PENDING/checkpoint rather than restart. Ask only for credentials, legal/business decision, destructive production approval, or unresolved ADR. Behavior changes update REQUIREMENTS. Final report includes commit, tests, live evidence, migration/rollback and exact SHA. Claude/Codex are not required; ChatGPT may execute directly through approved tools.

## 23. Execution DAG
Wave 0: PROD-001 + ADR inventory.
Wave 1 parallel: PROD-002/003; SEC-001; CP-001; PKG-004.
Wave 2: PROD-004/005/007 + SEC-002/003/004 + CP-002.
Wave 3: SEC-005 + CP-003/004 + REL-001/003.
Wave 4: SEC-006/007 + PKG-001/002/003 + REL-002/004.
Wave 5: PKG-005/006 + COM-001/002/003 + REL-005/006/007.
Wave 6: COM-004/006 + UX-001/003/004 + QA-001/003.
Wave 7: UX-002/005 + QA-002/004/005 + COM-005.
Wave 8: PILOT-001 -> PILOT-002 -> GA-001.
Parallel work may not edit the same shared chokepoint without an integration owner.

First executable tasks: PROD-001, SEC-001, CP-001, PKG-004, PROD-003, SEC-003, SEC-004, REL-001; PROD-002 at maintenance gate; PROD-004 after PROD-003.

## 24. Anti-drift handoff
- [ ] REQUIREMENTS + execution contract + live state read.
- [ ] No duplicate queue/store/security model.
- [ ] Tenant/project scope explicit.
- [ ] Idempotency identity defined.
- [ ] Rollback defined.
- [ ] Tests passed.
- [ ] Required live evidence attached.
- [ ] Behavior docs updated.
- [ ] Tested SHA recorded; deploy SHA invariant preserved.
- [ ] No hidden destructive step.
- [ ] Unresolved ADR/blocker stated instead of guessed.

