# ProjectFlow Autopilot — design (issue #106)

Status: P0 implemented on `feat/projectflow-autopilot-106`; P1 implemented with a
deterministic provider and a Gateway provider that reports `BLOCKED_PROVIDER` until
authorized; P2 adds a read/control API on the existing dashboard project surface.
Nothing here is enabled in any deployment. See §9 for what is NOT done.

## 1. Baseline (audited 2026-10-31, read-only)

| Fact | Source | Value |
| --- | --- | --- |
| Live controller | `GET 127.0.0.1:8766/version` | `0.13.0`, commit `73c90f7` (deploy branch `fix/codex-direct-owner-probe-20261031`), not origin/main `702550f` |
| `queue.enabled` | `~/hp-rollout/controller-config.yaml` | `true` |
| `queue.drain_enabled`, `project_feeds`, `project_dispatch_rules` | same | absent → defaults: off / empty |
| Lanes with `auto_dispatch_enabled` | `queue.db` (read-only) | 12 of 283 |
| Active queue tasks | `queue.db` | 0 (BLOCKED 3, CANCELLED 246, COMPLETED 41, SKIPPED 1) |
| `events.db` | read-only | 1616 events, 0 consumer cursors → the bus is write-only in production |
| Policy | `docs/CHATGPT_ORCHESTRATION_POLICY.md` v1.5.1 | queue-producing tools retired for normal ops; tracked work = `terminal_turn action=supervise` (`direct_task.py`) |

The autopilot therefore must not rely on `QueueLoop`, must not touch the 12
auto-dispatch lanes, and must ship disabled.

## 2. What already exists and is reused verbatim

| Need (issue #106) | Existing primitive | Used for |
| --- | --- | --- |
| task identity, deps, priority | `queue_store.queue_tasks` (`project_id`, `outcome_id`, `depends_on`, `request_key`, `dispatch_idempotency_key`) | the only task registry |
| atomic claim + fencing | `QueueStore.claim_next_task` (`BEGIN IMMEDIATE`, `claim_token`) + `VALID_TRANSITIONS` | one claimant per task |
| dependency safety | `validate_dependencies` (cycle/unknown refused at creation), `_dependencies_satisfied_locked` (fail-closed) | ready-set computation |
| RUNNING requires evidence | `mark_running_with_evidence`, `_valid_execution_evidence` | false ACK never RUNNING |
| independent QA | `VerifyQueue.ensure_verify_job(require_independent=True)` / `complete` / `fail` | QA gate; PASS completes the task atomically |
| outcome rule | `OutcomeStore.refresh_status` (cannot reach DONE), `complete` (evidence per AC) | rollup only |
| event log + cursor | `EventBus.read_since` / `commit_cursor` (monotonic) / `publish(idempotency_key)` | wake-ups + audit trail |
| single coordinator per project | `lease.ResourceLockStore.acquire` (atomic, TTL) | coordinator lease |
| dispatch with persist-before-send | `DirectTaskSupervisor.start(idempotency_key=…)` / `get_by_key` | the real dispatcher adapter |
| prompt safety | `coordinator.SENSITIVE_PROMPT_PATTERNS` + `_negated` | same screen as the queue gate |
| pause | queue lane pause (`ProjectService.pause`) | honoured; autopilot never resumes a lane it did not pause |

New code is limited to the glue: `terminal_mcp/autopilot.py` (P0),
`terminal_mcp/autopilot_planner.py` (P1), one config block, and a dashboard API (P2).

## 3. P0 — durable auto-continue

### 3.1 Authority split

| Decided by deterministic code | Never decided by AI or by pane text |
| --- | --- |
| claim, lease, transition, dependency readiness, dispatch, QA routing, completion, repair budget | any of those |

Agent/pane output reaches the autopilot only as a dispatcher **state enum** plus a
clipped, redacted reason string stored as data. It is never interpolated into a
prompt, a command, or a transition decision.

### 3.2 Opt-in (all must hold to dispatch)

1. `autopilot.enabled: true` (global, default false)
2. `autopilot.kill_switch: false`
3. project listed in `autopilot.projects`
4. that entry has `dispatch: true` **and** non-empty `approved_by` + `approval_ref` **and** a non-empty `lanes` allowlist
5. the lane is not paused and **not** `auto_dispatch_enabled` (a QueueLoop-owned lane is refused: `LANE_OWNED_BY_QUEUE_LOOP`), so two dispatchers can never share a lane
6. the lane's next dispatchable task belongs to this project (`CROSS_PROJECT_LANE` otherwise)
7. optional workspace probe reports the lane's worktree clean (`WORKTREE_DIRTY` → skip; never cleaned)

With 1–3 but not 4 the autopilot is **observe-only**: it reconciles and reports, but never dispatches.

### 3.3 Tick (one call = one bounded pass, no internal loop)

```
gates → acquire coordinator lease (project, "autopilot:coordinator")
      → read events since cursor            (wake-up + audit; the store is truth)
      → reconcile in-flight autopilot tasks via dispatcher.observe(key)
      → react to QA verdicts (bounded repair) and refresh outcome rollups
      → dispatch ready tasks up to max_in_flight
      → publish AUTOPILOT_* decisions (idempotent keys), commit cursor
```

### 3.4 Dispatcher contract

```python
dispatch(target, text, idempotency_key) -> Observation
observe(idempotency_key)                -> Observation
Observation.state ∈ {NOT_FOUND, PENDING, RUNNING, WAITING_INPUT, UNCERTAIN,
                     DONE, BLOCKED, FAILED, UNREACHABLE}
```

The dispatcher must persist its record under `idempotency_key` **before** it sends
(the `DirectTaskSupervisor` contract). That is what makes `NOT_FOUND` a proof of
"never sent".

### 3.5 Reconcile table

| Queue status | Observation | Action |
| --- | --- | --- |
| DISPATCHING | NOT_FOUND | → QUEUED (key kept sticky) |
| DISPATCHING | PENDING (`SUBMIT_CONFIRMED` without execution evidence) | none: **false ACK is not RUNNING** |
| DISPATCHING / DISPATCH_UNCERTAIN | RUNNING | `mark_running_with_evidence` |
| DISPATCHING / DISPATCH_UNCERTAIN / RUNNING | DONE (explicit `TMCP-DONE` / `supervise_complete`) | → RUNNING (if needed) → `ensure_verify_job(require_independent)` = VERIFYING |
| DISPATCHING | UNCERTAIN | → DISPATCH_UNCERTAIN; **never resent** |
| any in-flight | WAITING_INPUT | none; reported `WAITING_INPUT`, lane stays occupied, other lanes continue |
| DISPATCHING / RUNNING | BLOCKED | → BLOCKED + `AUTOPILOT_OWNER_ATTENTION` |
| DISPATCHING / RUNNING | FAILED | → FAILED |
| any | UNREACHABLE / exception | **no mutation**; reported `UNKNOWN` (stale) |
| RUNNING | PENDING (turn IDLE) | none: **IDLE is not DONE** |

### 3.6 QA and bounded repair

- PASS: the verifier's `VerifyQueue.complete` moves the task VERIFYING → COMPLETED. Dependents become ready through the existing fail-closed dependency check.
- FAIL / NEEDS_REWORK: the task lands in FAILED. If `metadata.autopilot.repairs < max_repair_attempts`, the autopilot calls `retry_task`, then records `repairs += 1` and `repair_of_job`. The next dispatch appends a fixed repair section that **references the verify job id only** and never inlines the verifier's text. Past the budget, the task stays FAILED and `AUTOPILOT_REPAIR_EXHAUSTED` is published.
- VERIFY_BLOCKED: the task is BLOCKED, with owner attention.

### 3.6a Automatic QA dispatch to a verifier session

On only when the project has `dispatch: true` **and** a `verifiers` allowlist
(`[{session, capabilities}]`). A verifier session may not be an implementer
lane of any project; config refuses it.

- **Commit correlation.** On an explicit worker DONE the verify job is opened with
  the lane HEAD as the controller observes it (`workspace_probe`, read where the
  session lives through `node_aware_repo_evidence`), not as the agent reports it.
  With `qa_require_commit` (default) the job records `require_commit_evidence`,
  and `VerifyQueue.complete` then refuses a PASS whose evidence does not name
  that commit (`COMMIT_EVIDENCE_REQUIRED`). Evidence naming a different commit
  is refused for every job that has a commit (`STALE_COMMIT`). No observed
  commit: QA is held (`QA_NO_COMMIT_EVIDENCE`). Lane HEAD moved since: held
  (`QA_STALE_COMMIT`).
- **Selection.** A capable verifier has every required capability, is not the
  implementer, and is not busy with another of our QA sends. It must also pass
  `verifier_probe`, a controller status read; an error there means unavailable,
  and the reason is shown.
- **Persist, fence, send.** `metadata.autopilot.qa = {job_id, attempt, commit_sha,
  verifier, key, dispatches}` is written (queue-owned key) before the send, the
  coordinator lease is renewed as a fence, then a fixed prompt is sent. The
  prompt has ids, the commit and allowlisted names only, and **no claim token**.
  The verifier claims that exact job with `terminal_verify_claim(job_id=…)`
  (it gets that job or nothing; independence and capability rules still apply)
  and receives its own token.
- **Key.** `autopilot-qa:{project}:{job}:{n}`. A key whose send is provably
  absent (`NOT_FOUND` after a crash, `REFUSED` by a busy verifier) is reused for
  the same verifier and costs no budget. A verifier run that ended `FAILED`
  without a verdict moves to `n+1`, up to `max_qa_dispatches`, after which
  `QA_DISPATCH_BUDGET_EXHAUSTED` goes to the owner.
- **No verdict by inference.** A verifier ACK, IDLE or TMCP-DONE is not a PASS:
  DONE without a verdict is `QA_DONE_WITHOUT_VERDICT` (owner), and nothing is
  resent. `WAITING_INPUT`, `UNCERTAIN` and `UNREACHABLE` are shown as reasons
  and never resent. A job claimed by someone else is left alone
  (`QA_EXTERNAL_VERIFIER`). Dead verifier leases in this project only are
  returned by `VerifyQueue.reconcile(project_id=…)`.
- PASS still only comes from `VerifyQueue.complete` with evidence, and only PASS
  completes the task and unlocks dependents. FAIL feeds the existing bounded
  repair (§3.6); the repaired attempt gets a new job, a new commit and a fresh
  QA budget.

### 3.7 Exactly-once argument

- **Two coordinators.** The project lease admits one. If a lease lapses mid-tick, `claim_next_task` still admits one claimant per task, transitions are validated (a second DISPATCHING→RUNNING raises), and the dispatcher dedups by key. Just before sending, the holder renews the lease and re-checks it; if it lost the lease, it leaves the row DISPATCHING with the key persisted.
- **Crash after persist, before send.** The row is DISPATCHING with a key, and `observe` returns NOT_FOUND, so the row goes back to QUEUED and is sent once on the next tick.
- **Crash after send, before transition.** `observe(key)` finds the record and reconciles it. Nothing is resent.
- **Crash after an event, before cursor commit.** Events are re-read; decisions derive from the store and publishes are idempotent.

### 3.8 Events published

`AUTOPILOT_DISPATCHED`, `AUTOPILOT_RECONCILED`, `AUTOPILOT_REPAIR_SCHEDULED`,
`AUTOPILOT_REPAIR_EXHAUSTED`, `AUTOPILOT_OWNER_ATTENTION`, `AUTOPILOT_IDLE`. The
actor is always `projectflow-autopilot`. The key is
`autopilot:{project}:{task}:{attempt}:{kind}`, or `…:{source}` for state-keyed
events. Every event carries `causation_id`. The consumer skips its own events.

## 4. P1 — bounded AI Project Coordinator (`autopilot_planner.py`)

- **Input.** The context is built from project-scoped rows only: outcome titles and acceptance criteria, task id/title/status/deps, and the latest verified SHA. Every string goes through `redaction.redact_text`. Prompt bodies are excluded and the context is hard-capped in characters.
- **Output.** Strict JSON with `schema: "projectflow.plan/v1"`, `project_id`, and `tasks[≤N]` of `{key, title, prompt, depends_on[keys|existing ids], priority 0..100, risk low|medium|high, capabilities[], rationale}`.
- **The validator rejects:**
  - a wrong schema or version, unknown fields, or a different `project_id`
  - more than N tasks, or duplicate keys
  - dependency cycles, or a dependency on an unknown id or one from another project
  - a prompt that matches a sensitive or destructive pattern
  - `risk: high` unless an operator approves it
  - secret-looking content
- **Apply.** Applying is deterministic. Tasks are appended through `QueueStore.append_tasks` with `request_key = plan:{project}:{plan_id}:{key}`, which makes re-applying idempotent. Priority is clamped. The AI never sets a status, lease, or completion.
- **Providers.** `DeterministicPlanner` is the always-available fallback; it orders by existing dependencies and priority. `GatewayPlanner` is constructed only from protected server config (endpoint plus a token file path, never inline). Without it the planner returns `BLOCKED_PROVIDER`. Budgets apply: `max_calls_per_hour` and a single in-flight call. A provider failure falls back to deterministic planning. A loop of repeated calls is impossible because each call is keyed by context hash.

## 5. P2 — Control Tower API (extends `/dashboard/api/projects/*`)

- `GET /dashboard/api/projects/autopilot?project_id=` (read guard) returns:
  - goal, outcomes and AC, and tasks with status, timestamps, lane, node, QA verdict and evidence refs
  - `readiness` with **separate** `CODE_READY`, `DEVICE_QA` and `RELEASE_READY`, each `UNKNOWN` unless evidence exists
  - `stale` flags and autopilot state
- `POST /dashboard/api/projects/autopilot/pause|resume` uses `_mutation_guard`: mutations enabled, same-origin CSRF, CF Access identity. Each call is idempotent and audited, and the actor is recorded. Resume never overrides a non-autopilot pause.
- Notifications are deltas only: a new verified milestone, a new blocker, or a needed approval. They are computed from the event stream with a per-consumer cursor, so a repeat read produces no repeat.
- Gateway Task Center #115 consumes this JSON contract through a separate, scoped PR in that repo. Nothing in the gateway repo is changed from here.

## 6. Data/event contract (versioned)

The `autopilot_status/v1` JSON shape is documented in the plan, §Contracts. It is
the same object served to the dashboard and to a read-only ChatGPT connector.

## 7. Rollout gates

| Gate | Requires |
| --- | --- |
| G0 merged-disabled | tests green, defaults off, no config change |
| G1 staging | synthetic project, fake dispatcher, isolated state dir (done in this branch: `tests/test_autopilot_e2e.py`) |
| G2 HP pilot observe-only | owner approval; `enabled: true`, project with `dispatch: false` |
| G3 HP pilot dispatch | owner approval naming project, lanes, `approval_ref`; one synthetic repo; kill switch verified |
| G4 production project | separate approval per project |

## 8. Safety invariants

- Defaults are off. The autopilot never enables `queue.enabled`, `drain_enabled`, or lane auto-dispatch.
- It never runs git or shell. It never resets, stashes, cleans, or force-pushes, and it refuses destructive prompts.
- It never resends a DISPATCH_UNCERTAIN task. It never marks DONE from IDLE or a submit receipt.
- No cross-project mutation is possible: every write is filtered on `project_id`, and lanes are allowlisted per project.

## 9. Not done in this branch (honest)

- The production wiring of `DirectTaskDispatcher` into the HTTP server loop is behind config and has never been run against a live session.
- QA dispatch (§3.6a) is built and tested against synthetic executors only. No real verifier session has received a QA prompt, and the production probes (`build_workspace_probe`, `build_verifier_probe`) have not run against a live controller.
- `GatewayPlanner` stays `BLOCKED_PROVIDER` until the owner authorizes an endpoint and token path.
- There is no Control Tower UI widget, only the API. The Gateway Task Center PR is not opened.
- Acceptance criteria (A) "real enabled pilot" and (C) "two real projects on dashboard" require G2/G3 approval.
