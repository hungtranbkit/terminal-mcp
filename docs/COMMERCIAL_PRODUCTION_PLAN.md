# Terminal MCP — Commercial Production Plan

**Planning date:** 2026-09-20  
**Target:** Commercial Production V1  
**Status:** Execution plan; no feature status in this document overrides `docs/REQUIREMENTS.md`.  
**Canonical technical truth:** `docs/REQUIREMENTS.md`  
**Canonical live task state:** Terminal MCP durable backlog/controller DB. The repo backlog file is a portable projection only.

---

## 1. Objective

Turn the current Terminal MCP system from a powerful operator/development tool into a product that can be safely installed, operated, upgraded, billed, supported, and trusted by external paying customers.

Commercial Production V1 must prove one end-to-end outcome:

> A customer can create an account/workspace, install a Terminal MCP node on a supported machine, enroll it without manually copying long-lived secrets, connect ChatGPT/another MCP client, dispatch long-running work, survive client/node/controller interruptions without duplicating work, review an auditable history, upgrade/rollback safely, and operate under explicit permissions and commercial entitlements.

This plan deliberately treats the existing orchestration/recovery/session/fleet work as the product core. It does **not** reposition Terminal MCP as a generic MCP gateway.

---

## 2. Product scope for V1

### Included

- Terminal MCP Core/Node Agent on customer-controlled machines.
- Multi-node session discovery and control.
- Durable task queue, checkpoint/recovery, Project Coordinator and integration/release gates.
- Browser dashboard for project/session/task/health/audit.
- Organization/workspace/project model.
- Role-based access control.
- Secure node enrollment and credential rotation/revocation.
- Internet-capable outbound node connectivity.
- Installer + updater for Linux, Windows and macOS.
- Release channels: stable / beta.
- Centralized audit/telemetry necessary for support and commercial operations.
- Licensing/entitlement and usage metering.
- Self-hosted/Enterprise deployment path.
- Commercial documentation, support runbooks, privacy/retention controls.
- Production SLOs, backup/restore, rollback and disaster-recovery procedures.

### Explicitly out of V1

- General-purpose marketplace of arbitrary third-party MCP servers.
- Full multi-region active/active control plane.
- Formal SOC 2 / ISO certification completion.
- Arbitrary business-process automation unrelated to terminal/agent operations.
- Unlimited autonomous destructive operations without an approval/policy gate.
- Replacing existing MCP clients or coding agents.

These may follow after GA, but they are not allowed to expand the V1 critical path.

---

## 3. Current-state snapshot used for this plan

Reviewed against GitHub `main` on 2026-09-20.

The existing system already has a substantial production core:

- Session operations/lifecycle, local tmux, remote Linux, macOS worker posture and Windows/ConPTY are marked VERIFIED.
- Dashboard auth, permissions, session/task/global inbox, Supervisor/Coordinator, integration-lane view and web terminal are marked VERIFIED.
- Persistent Task Queue v2, persist-before-dispatch, Coordinator gates, three-role pipeline and migration/load balancing are present.
- Queue background loop exists and is live at global level.
- Reliable prompt submission, idempotency and durable bounded waits exist.
- Recovery/checkpoint primitives exist for chat/project/session continuity.
- Multi-node scheduler/discovery/watchdog exist.

The current snapshot still exposes important production gaps:

- Worktree Janitor is contract-only; no executor yet.
- Auto-dispatch has not been enabled on a real production lane in the captured requirements state.
- Real remote-node auto-dispatch smoke remains unrun in the captured requirements state.
- Direct-send reliability fix still required Windows deployment/live re-verification in the captured backlog.
- Work-efficiency telemetry is implemented but not live-wired.
- Token-efficiency benchmark failed its own acceptance bar.
- Event-driven integration wake is still partial.
- Internet/VPS node transport is planned, not complete.
- Current backlog projection still contains security/Windows/reliability blockers including node-token maintenance, bearer-auth throttling, replay protection, central audit aggregation and unsafe Codex default review.
- Commercial account/tenant/entitlement/billing/install/update/support concerns are not yet the product model described by `docs/REQUIREMENTS.md`.

**Important execution rule:** before changing code, reconcile this plan against the live controller/backlog. Do not duplicate an item already completed after this snapshot.

---

## 4. Target product architecture

Commercial V1 should establish clear trust boundaries without forcing an immediate repository split.

```text
                    TERMINAL MCP CONTROL PLANE
  ┌────────────────────────────────────────────────────────────┐
  │ Accounts / Organizations / Workspaces / Projects          │
  │ Entitlements / Billing / Usage                            │
  │ Durable project/task metadata                             │
  │ Policy / Approval / RBAC                                  │
  │ Fleet inventory / enrollment / health                     │
  │ Central audit + operational telemetry                     │
  │ Release/update metadata                                   │
  └─────────────────────────┬──────────────────────────────────┘
                            │ outbound authenticated channel
                            │ no inbound customer firewall hole required
  ┌─────────────────────────▼──────────────────────────────────┐
  │ TERMINAL MCP NODE / EDGE                                 │
  │ customer-controlled machine                              │
  │                                                         │
  │ Session manager  Queue executor  Browser/terminal bridge │
  │ Local secrets     Repo/worktree     Agent adapters        │
  │ Policy enforcement + audit spool                         │
  └──────────────┬──────────────────────┬─────────────────────┘
                 │                      │
            Claude/Codex/etc.        shell/browser/git
```

### Architectural rules

1. Customer credentials and local terminal access stay on the edge wherever possible.
2. The cloud control plane stores only metadata required to coordinate, authorize, bill and support the product.
3. Every mutating action has an idempotency key and auditable actor/project/node context.
4. Node-to-cloud connectivity is outbound-first; no customer should need to expose a raw node-agent port to the public internet.
5. Control-plane unavailability must not corrupt local state or cause tasks to replay.
6. An Enterprise/self-hosted deployment must be achievable from the same product boundary, not a fork.
7. The existing `docs/REQUIREMENTS.md` remains the feature truth. This document is the release program and gate definition.

---

## 5. Release gates

No Commercial Production release is allowed by percentage-complete alone. Every gate below must pass.

### Gate 0 — Baseline reconciliation

Goal: prove we know exactly what code/state is being commercialized.

Pass conditions:

- Git topology reconciled; no unknown unique commits or unreviewed production branch divergence.
- Live backlog reconciled with repo projection.
- Current production commit is identifiable and reproducible.
- All active P0 blockers have owner/status/evidence.
- A clean install from source/package can reproduce the current service.

### Gate 1 — Existing-core reliability

Goal: finish the unresolved reliability work before adding commercial surface area.

Pass conditions:

- Windows node maintenance/security recovery complete.
- Prompt-delivery/acceptance gate enforced on all intended send paths.
- Remote-node auto-dispatch passes a real end-to-end smoke.
- Direct-send passes live Windows re-verification.
- Integration/merge queue is enabled for one real dogfood project and exact-SHA deploy is proven.
- Worktree cleanup cannot delete dirty/unmerged/active worktrees.
- Restart/reconnect tests prove no duplicate dispatch/deploy.

### Gate 2 — Security baseline

Goal: make the system safe for an external tenant.

Pass conditions:

- Threat model and trust boundaries reviewed.
- No default `--dangerously-bypass-approvals-and-sandbox` commercial posture.
- Node bearer authentication rate-limited.
- Heartbeat/replay protection implemented.
- Enrollment credential is one-time/short-lived; long-lived node credential is rotatable/revocable.
- Central audit covers control-plane and node actions.
- Organization/workspace/project RBAC enforced server-side.
- Secrets are never returned through ordinary API/tool/UI surfaces.
- Dependency/SBOM/signing and vulnerability scan run in CI.
- Security tests cover cross-tenant and privilege-escalation cases.

### Gate 3 — Install, enrollment and upgrade

Goal: a new customer can get from zero to first healthy node without operator handholding.

Pass conditions:

- Supported Linux installer.
- Supported Windows installer/service.
- Supported macOS installer/service.
- One-command or guided enrollment.
- Outbound secure transport to the control plane.
- Node profile/health visible after enrollment.
- Signed/versioned update path.
- Upgrade and rollback proven with preserved task/session state.
- Uninstall/re-enroll documented and tested.

### Gate 4 — Commercial control plane

Goal: add the minimum cloud product model.

Pass conditions:

- Account -> Organization -> Workspace -> Project hierarchy.
- RBAC and membership lifecycle.
- Entitlement service and edition flags.
- Usage metering with deterministic source-of-truth events.
- Billing integration and invoice/subscription state sync.
- License grace behavior for temporary cloud outages.
- Data retention/export/delete controls.
- Admin/support tooling that cannot silently bypass tenant boundaries.

### Gate 5 — Observability and operations

Goal: an operator can detect, explain and recover incidents.

Pass conditions:

- Central health view: controller, nodes, tunnel, queue, DB, task followers.
- Structured logs with correlation/task/request IDs.
- Metrics and alert thresholds.
- Backups + restore drill.
- Migration rollback plan.
- Production runbooks for outage, stuck queue, credential compromise, bad release and node loss.
- Defined SLO/SLI targets measured in production-like testing.

### Gate 6 — Product UX and documentation

Goal: external users can succeed without internal project knowledge.

Pass conditions:

- Onboarding wizard/checklist.
- Clear connection instructions for supported MCP clients.
- First project / first node / first task path.
- Explain permissions, approvals and recovery.
- User-facing error codes with remediation.
- Admin, security and self-hosted docs.
- Support diagnostics bundle with secret redaction.
- Pricing/edition page and entitlement behavior agree.

### Gate 7 — Pilot/beta

Goal: prove the product outside the development environment.

Pass conditions:

- MESFlow and NovaRetail used as internal dogfood projects.
- 3–5 external design-partner environments or equivalent independent pilot environments.
- At least one Windows, one Linux and one macOS node in pilot coverage.
- No open Sev-1/Sev-2 production defect.
- Recovery drill succeeds after forced controller restart and forced node disconnect.
- Upgrade/rollback succeeds on pilot nodes.
- Billing/entitlement behavior verified in sandbox and live low-risk flow.
- Security review findings at Critical/High closed or explicitly release-blocked.

### Gate 8 — GA

Pass conditions:

- Stable release signed/tagged.
- Changelog and migration notes published.
- Rollback artifact retained.
- Support/on-call ownership defined.
- Error budget/SLO dashboards active.
- Privacy/Terms/Security documentation published.
- Commercial backup/restore/recovery evidence archived.
- GA acceptance report links every gate to real evidence.

---

## 6. Workstreams and detailed execution

### A. Existing core / reliability

#### TMCP-PROD-001 — Reconcile repository/release topology — P0
Deliverables:
- identify canonical production HEAD and remote HEAD;
- inventory unique commits/dirty worktrees;
- resolve existing push/divergence blocker without force-push/reset/clean;
- document merge/release authority and exact-SHA release flow.

Acceptance:
- `main`, release candidate and deployed SHA are traceable;
- no unique commit is lost;
- CI can build the selected production SHA.

#### TMCP-PROD-002 — Close Windows node maintenance bundle — P0
Combines the existing maintenance window dependencies, but does not collapse their acceptance evidence.

Acceptance:
- rotate leaked/old node credential;
- Scheduled Task/service ownership is correct and restartable;
- old token rejected, new credential works;
- required sessions are recovered/recreated according to documented behavior;
- direct-send re-verification and session-loss recovery evidence attached.

#### TMCP-PROD-003 — Enforce prompt-delivery acceptance gate — P0
Acceptance:
- every commercial send/dispatch path uses one acceptance vocabulary;
- `DELIVERY_UNKNOWN` never causes blind replay;
- duplicate submit test proves idempotency;
- queue/supervisor/direct-send behavior is consistent.

#### TMCP-PROD-004 — Real remote auto-dispatch smoke — P0
Acceptance:
- task persisted before dispatch;
- real remote node receives task;
- task continues across bounded client disconnect/reconnect;
- resulting task has task_id -> session -> branch/commit/test evidence;
- no duplicate dispatch after retry.

#### TMCP-PROD-005 — Enable one real integration/release lane — P0
Acceptance:
- one dogfood project produces a handoff;
- Integration lane wakes/reviews/merges/tests;
- deploy is tied to exact SHA;
- failed verification prevents promotion;
- release evidence appears in audit/task history.

#### TMCP-PROD-006 — Worktree Janitor safe executor MVP — P1
Acceptance:
- active process/worktree lease prevents deletion;
- dirty -> BLOCKED;
- unmerged -> REVIEW/BLOCKED;
- only clean+merged+inactive becomes AUTO_SAFE;
- dry-run and audit are default before auto-delete rollout.

#### TMCP-PROD-007 — Recovery/chaos suite — P0
Scenarios:
- Chat/UI reset;
- MCP reconnect;
- controller restart;
- node restart/disconnect;
- network partition;
- duplicated client retry;
- agent context rollover;
- partially completed deploy.

Acceptance:
- task is resumed or reaches a deterministic recovery state;
- no destructive stage runs twice;
- recovery evidence is machine-queryable.

---

### B. Security and tenant isolation

#### TMCP-SEC-001 — Threat model — P0
Model:
- MCP client;
- browser dashboard;
- cloud control plane;
- local node agent;
- remote node transport;
- coding-agent subprocess;
- Git credentials;
- billing/admin/support access;
- tenant boundary.

Output:
- assets, attackers, trust boundaries, abuse cases, controls, residual risks.

#### TMCP-SEC-002 — Execution-policy hardening — P0
Acceptance:
- commercial defaults are least privilege;
- unsafe/yolo flags are opt-in and clearly surfaced;
- destructive classes require explicit policy/approval;
- policy is enforced server-side, not only UI-side.

#### TMCP-SEC-003 — Node auth abuse protection — P0
Acceptance:
- per-source/risk-aware throttle for repeated auth failure;
- normal heartbeat not throttled;
- lockout cannot permanently wedge a legitimate node;
- metrics/audit expose abuse without leaking tokens.

#### TMCP-SEC-004 — Replay protection — P0
Acceptance:
- timestamp/nonce or equivalent signed request protection;
- clock-skew behavior defined;
- captured heartbeat/action cannot be replayed successfully.

#### TMCP-SEC-005 — Enrollment + credential lifecycle — P0
Acceptance:
- short-lived enrollment token;
- node exchanges it for node identity/credential;
- rotate and revoke without reinstall;
- compromised node can be disabled immediately;
- credential never appears in logs/support bundle.

#### TMCP-SEC-006 — Organization/workspace/project RBAC — P0
Initial roles:
- Owner;
- Admin;
- Operator;
- Developer;
- Viewer.

Acceptance:
- role decisions enforced at API/service layer;
- project isolation covered by tests;
- membership removal revokes effective access;
- support/admin impersonation, if later allowed, is explicit and audited.

#### TMCP-SEC-007 — Central audit — P0
Events include:
- auth/login;
- node enrollment/rotation/revocation;
- permission changes;
- task create/dispatch/retry/cancel;
- terminal send/destructive action;
- git merge/release/deploy;
- billing/entitlement change;
- admin/support actions.

Acceptance:
- immutable-enough append semantics;
- actor, tenant, project, node, task, request/correlation IDs;
- retention and export rules.

#### TMCP-SEC-008 — Supply-chain baseline — P1
Acceptance:
- locked/reproducible dependencies;
- SBOM artifact;
- dependency scan;
- signed release artifact/checksum;
- protected release workflow;
- secret scan.

---

### C. Control plane and commercial data model

#### TMCP-CP-001 — Product data model — P0
Entities:
- User;
- Organization;
- Membership;
- Workspace;
- Project;
- Node;
- NodeCredential;
- Policy;
- Entitlement;
- Subscription;
- UsageEvent;
- AuditEvent;
- ReleaseChannel.

Rules:
- every resource carries tenant ownership;
- cross-tenant queries are structurally difficult, not filtered only in UI;
- destructive deletes use explicit lifecycle state/retention.

#### TMCP-CP-002 — API/auth contract — P0
Acceptance:
- versioned API contract;
- browser session/auth and machine/node auth separated;
- idempotency header/key on mutations;
- stable error codes;
- request correlation.

#### TMCP-CP-003 — Multi-tenant storage isolation — P0
Acceptance:
- tenant ownership on every commercial control-plane table;
- test suite attempts cross-tenant ID enumeration;
- no data returned for foreign tenant;
- migrations include rollback/backup rules.

#### TMCP-CP-004 — Outbound node transport — P0
Build on the existing Internet/VPS roadmap.

Acceptance:
- node initiates secure connection outbound;
- reconnect with jitter/backoff;
- queued control messages have bounded retention and idempotency;
- loss of control plane does not replay completed local work;
- old node versions are rejected or compatibility-gated intentionally.

#### TMCP-CP-005 — Fleet presence/health — P1
Acceptance:
- node online/offline/degraded;
- agent version;
- last heartbeat;
- active sessions/tasks;
- upgrade eligibility;
- clear stale-state semantics.

---

### D. Packaging, installation and upgrades

#### TMCP-PKG-001 — Linux package/installer — P0
Acceptance:
- fresh supported host -> installed service -> enrolled node;
- systemd setup;
- least-privilege filesystem permissions;
- doctor command passes.

#### TMCP-PKG-002 — Windows package/installer — P0
Acceptance:
- service/Scheduled Task behavior explicitly chosen;
- ConPTY requirements installed;
- restart behavior understood;
- no manual Python/venv setup required for ordinary customer.

#### TMCP-PKG-003 — macOS package/installer — P1
Acceptance:
- LaunchAgent/service lifecycle;
- tmux/dependency checks;
- enrollment and health parity.

#### TMCP-PKG-004 — Versioning and release channels — P0
Acceptance:
- semver policy;
- stable/beta channel;
- server/node protocol compatibility matrix;
- upgrade refusal when incompatible.

#### TMCP-PKG-005 — Auto-update + rollback — P0
Acceptance:
- signed update metadata;
- staged rollout;
- preflight backup/checkpoint;
- rollback to previous known-good;
- task state not silently discarded.

#### TMCP-PKG-006 — Uninstall/re-enroll/recovery — P1
Acceptance:
- uninstall can preserve or intentionally delete data;
- revokes node credential;
- re-enroll path documented;
- lost-node replacement flow tested.

---

### E. Reliability, persistence and operations

#### TMCP-REL-001 — Persistence inventory/migrations — P0
The current product uses several SQLite stores. Commercial V1 must document ownership, backup, schema version and migration behavior for each.

Acceptance:
- startup migration is deterministic;
- backup before destructive migration;
- rollback rule defined;
- migration tested from at least two prior supported versions.

#### TMCP-REL-002 — Backup/restore — P0
Acceptance:
- automated backup of required control-plane state;
- node-local state backup policy;
- encrypted storage;
- restore drill with RPO/RTO evidence.

#### TMCP-REL-003 — Global idempotency contract — P0
Acceptance:
- task dispatch, terminal send, merge, deploy, enrollment and billing-event ingestion each document idempotency identity;
- duplicate request tests;
- partial failure tests.

#### TMCP-REL-004 — Live work telemetry wiring — P1
Acceptance:
- work-efficiency telemetry attached to real coordinator/runtime path;
- per-task and aggregate numbers visible;
- no double-count after retry/recovery.

#### TMCP-REL-005 — Performance/token budget remediation — P1
Use the existing failed benchmark as baseline rather than hiding it.

Acceptance:
- define release threshold;
- reduce redundant tool/agent calls;
- batch project/session status reads;
- benchmark rerun with evidence;
- commercial default stays within accepted latency/token budget.

#### TMCP-REL-006 — SLO/SLI definition — P0
Initial measures:
- control-plane API availability;
- node connection success;
- task-persist success;
- dispatch success;
- duplicate-dispatch rate;
- recovery success;
- upgrade success;
- p95 command/control latency.

Exact numeric targets must be chosen from measured pilot data and then frozen before GA.

#### TMCP-REL-007 — Alerting/runbooks — P0
Runbooks:
- controller unavailable;
- tunnel/transport failure;
- node offline;
- queue stuck;
- DB corruption/migration failure;
- credential compromise;
- failed/bad release;
- billing webhook lag;
- cross-tenant security incident.

---

### F. Commercialization

#### TMCP-COM-001 — Edition model — P0
Proposed editions:
- Community: local/self-managed, limited nodes/features.
- Pro: individual/small developer use.
- Team: organization/workspace collaboration.
- Enterprise: self-hosted/advanced policy/support.

Do not hard-code pricing into core execution logic. Core consumes entitlements.

#### TMCP-COM-002 — Entitlement service — P0
Acceptance:
- product features check entitlement through one service;
- temporary cloud outage has explicit grace behavior;
- downgrade never corrupts existing task data;
- audit entitlement changes.

#### TMCP-COM-003 — Usage metering — P0
Meter product-owned units, not model-provider tokens unless explicitly needed.

Candidate units:
- active workspace;
- enrolled/active node;
- active agent/session;
- premium retention/enterprise features.

Acceptance:
- deterministic event source;
- retry-safe aggregation;
- customer-visible usage summary.

#### TMCP-COM-004 — Billing integration — P1
Acceptance:
- checkout/subscription;
- webhook verification;
- duplicate webhook idempotency;
- payment state -> entitlement transition;
- cancellation/grace/recovery;
- invoices/receipts linked from account surface.

#### TMCP-COM-005 — Legal/privacy/security docs — P0 for GA
Deliver:
- Terms;
- Privacy notice;
- data-processing/retention description;
- security overview;
- subprocessor list when relevant;
- responsible disclosure contact/process.

Legal wording requires appropriate legal review before public launch.

#### TMCP-COM-006 — Data lifecycle controls — P0
Acceptance:
- tenant data export;
- account/workspace deletion workflow;
- retention policy;
- deletion tombstone/audit behavior;
- backups expire deleted data according to policy.

---

### G. User experience and supportability

#### TMCP-UX-001 — First-run onboarding — P0
Path:
1. create account/org;
2. create workspace/project;
3. copy/run installer;
4. enrollment completes;
5. node shows healthy;
6. connect MCP client;
7. dispatch first safe task;
8. see task progress/recovery/audit.

Acceptance: a test user completes without shell-level product knowledge beyond running the installer.

#### TMCP-UX-002 — Production dashboard information architecture — P1
Required top-level views:
- Projects;
- Tasks;
- Nodes;
- Sessions;
- Releases;
- Audit;
- Usage/Billing;
- Settings/Security.

Do not duplicate existing task/session state stores merely for UI.

#### TMCP-UX-003 — Error/remediation catalog — P0
For every common user-visible error:
- stable code;
- plain-language meaning;
- safe next action;
- support correlation ID.

#### TMCP-UX-004 — Diagnostics bundle — P0
Acceptance:
- one command/UI action creates a support bundle;
- contains versions/health/config shape/log excerpts;
- recursively redacts secrets;
- user sees what will be included.

#### TMCP-UX-005 — Documentation set — P0
Docs:
- quick start;
- supported platforms;
- MCP-client connection;
- node enrollment;
- project/task model;
- permissions/policies;
- recovery;
- update/rollback;
- self-hosted;
- security;
- troubleshooting.

---

### H. CI/CD, testing and release

#### TMCP-QA-001 — CI matrix — P0
At minimum:
- Linux;
- Windows;
- macOS;
- supported Python/runtime versions;
- migration tests;
- unit/integration;
- browser smoke;
- packaging install smoke.

#### TMCP-QA-002 — End-to-end matrix — P0
Golden flows:
- new tenant/new node;
- task dispatch/complete;
- client disconnect/recover;
- node disconnect/recover;
- duplicate send/retry;
- blocked approval;
- git worktree/merge/deploy;
- upgrade/rollback;
- revoke node/user access.

#### TMCP-QA-003 — Security test suite — P0
Cases:
- foreign-tenant object access;
- permission escalation;
- stale/revoked credential;
- replay;
- brute-force throttle;
- CSRF/origin;
- secret leakage;
- unsafe action without approval.

#### TMCP-QA-004 — Load/soak — P1
Measure:
- nodes;
- concurrent sessions;
- queued tasks;
- audit events;
- reconnect storms;
- long-lived WSS connections;
- DB growth.

Freeze supported commercial limits from evidence, not guesses.

#### TMCP-QA-005 — Release rehearsal — P0
Acceptance:
- build;
- sign;
- migrate;
- deploy;
- live smoke;
- forced rollback;
- restore;
- repeat on clean environment.

---

## 7. Dogfood and rollout strategy

### Stage A — Internal dogfood
Use Terminal MCP itself plus MESFlow and NovaRetail.

Required evidence:
- a real long-running task survives a ChatGPT UI reset;
- one real multi-node job uses remote dispatch;
- one real merge/deploy uses exact-SHA release gate;
- one node is upgraded and rolled back;
- audit reconstructs the timeline.

### Stage B — Closed alpha
2–3 controlled environments. No broad self-service signup.

Focus:
- installer;
- enrollment;
- recovery;
- diagnostics;
- support burden.

### Stage C — Design-partner beta
3–5 independent customer environments.

Entry:
- Gate 0–6 passed.

Exit:
- pilot stability target met;
- no Critical/High security issue;
- billing/entitlement tested;
- upgrade/rollback and restore drill passed.

### Stage D — GA
Only after Gate 7 acceptance report.

---

## 8. Execution order / critical path

### Lane 1 — Reliability + release foundation
`PROD-001 -> PROD-002/003 -> PROD-004/005 -> PROD-006/007`

### Lane 2 — Security
`SEC-001 -> SEC-002/003/004 -> SEC-005 -> SEC-006/007 -> SEC-008`

### Lane 3 — Control plane + product model
`CP-001/002 -> CP-003 -> CP-004/005 -> COM-001/002/003 -> COM-004/006`

### Lane 4 — Packaging
`PKG-004 -> PKG-001/002/003 -> PKG-005/006`

### Lane 5 — Reliability/Ops/QA
`REL-001/003 -> REL-002/004/006 -> REL-007 -> QA-001/002/003 -> QA-004/005`

### Lane 6 — UX/docs
Starts once API/enrollment contracts stabilize:
`UX-001/003 -> UX-002/004/005`

**Commercial critical path:** Gate 0 -> Gate 1 -> Security/auth/enrollment -> outbound transport -> packaging/update -> tenant/entitlement -> QA/recovery -> pilot -> GA.

---

## 9. Task-slicing rules

Top-level tasks in this document are epics/work packages. When dispatched into Terminal MCP:

- split implementation into 15–30 minute bounded subtasks where practical;
- every subtask receives a stable task ID and idempotency/request key;
- persist before dispatch;
- one worktree/branch per conflicting code slice;
- checkpoint before expensive/long operations;
- `PENDING` is a valid continuation state, not a reason to restart from zero;
- completion requires evidence: commit + tests + live verification where relevant;
- destructive/production operations require the relevant approval gate;
- never mark DONE from agent prose alone.

Traceability target:

`plan task -> backlog_id -> queue_task_id -> session -> branch -> commit -> tests -> live verify -> merge -> deploy SHA`

---

## 10. Release evidence checklist

Every GA candidate must include:

- source commit SHA;
- build artifact checksum/signature;
- dependency/SBOM artifact;
- database migration version;
- compatibility matrix;
- automated test report;
- security scan report;
- E2E report;
- load/soak report;
- backup/restore drill;
- upgrade/rollback drill;
- dogfood/pilot results;
- open known issues;
- runbook links;
- release notes;
- rollback command/procedure.

---

## 11. Commercial Production Definition of Done

Terminal MCP is ready to sell as Commercial Production V1 only when:

1. A clean customer environment can install and enroll without internal operator intervention.
2. Task state survives ChatGPT/MCP/controller/node interruptions according to documented semantics.
3. Duplicate requests cannot duplicate destructive execution.
4. Tenant access is server-side isolated and security-tested.
5. Node credentials can enroll, rotate and revoke safely.
6. Internet operation does not require exposing a raw unauthenticated node endpoint.
7. Upgrade and rollback are proven on all supported platforms.
8. Audit can reconstruct who/what/where/when for a task/release.
9. Backups and restore have been tested, not merely configured.
10. Entitlement/billing cannot silently disable or corrupt active work.
11. User-facing setup, errors and diagnostics are documented.
12. Dogfood + external pilot evidence passes the release gates.
13. No open Critical/High security finding and no open Sev-1/Sev-2 release blocker.
14. GA exact SHA, artifact and rollback path are recorded.

---

## 12. Immediate next actions

Start in this order:

1. Reconcile live backlog/production SHA with this snapshot.
2. Close existing P0 reliability/security blockers before adding account/billing UI.
3. Produce the threat model and product data model in parallel.
4. Lock outbound node transport/enrollment contract.
5. Build installers + upgrade/rollback against that contract.
6. Add tenant/RBAC/entitlement/usage.
7. Wire central audit/telemetry/SLOs.
8. Run full cross-platform E2E and recovery drills.
9. Dogfood on MESFlow/NovaRetail.
10. Closed alpha -> design-partner beta -> GA.

The machine-readable work packages corresponding to this plan live in:

`docs/COMMERCIAL_PRODUCTION_TASKS.yaml`
