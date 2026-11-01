# ProjectFlow Autopilot — implementation plan (issue #106)

Spec: `docs/superpowers/specs/2026-10-31-projectflow-autopilot-design.md`.
Branch `feat/projectflow-autopilot-106`, worktree
`/home/kimex/workspace/.worktrees/terminal-mcp-autopilot-106`, base `origin/main` `702550f`.
Tests: `.venv/bin/python -m pytest -q <files>`, run from the worktree.

## Slice 1 — P0 core (`terminal_mcp/autopilot.py`, `tests/test_autopilot.py`)

1. RED: test the policy gates. Disabled is a no-op that reads nothing. The kill switch halts. A project that hasn't opted in is refused. Observe-only mode never dispatches. A lane with `auto_dispatch_enabled` or one that is paused is refused. A cross-project lane is skipped.
2. RED: test dispatch of a ready task. It goes QUEUED→PRECHECK→READY→DISPATCHING with a persisted key, then RUNNING only on RUNNING evidence.
3. RED: test that a false ACK (PENDING) stays DISPATCHING, and that IDLE (PENDING) while RUNNING stays RUNNING.
4. RED: test that DONE leads to a verify job (VERIFYING), and that a verifier PASS makes the dependent task dispatchable on the next tick (A→B).
5. RED: test that QA FAIL leads to bounded repair, that the repair prompt holds a reference only, and that once the budget is exhausted the task stays FAILED and the owner is alerted.
6. RED: test that BLOCKED goes to owner attention and that an independent lane continues. WAITING_INPUT is reported and causes no mutation.
7. RED: test that UNREACHABLE and exceptions cause no mutation and report UNKNOWN.
8. RED: test the two-coordinator case (one lease), and that a lost lease before send means nothing is sent.
9. RED: test restart cases. Persisted but not sent is requeued, then sent once. Sent but not transitioned is reconciled, not resent.
10. RED: test that a destructive prompt goes to BLOCKED, and that agent-output injection is inert.
11. GREEN: implement `AutopilotPolicy`, `ProjectAutopilot.tick`, `Observation`, and `DirectTaskDispatcher`.

## Slice 2 — config (`terminal_mcp/config.py`, `tests/test_autopilot_config.py`)

- Add an `autopilot:` block whose defaults are all off. Parsing must reject `dispatch: true` without `approved_by`, `approval_ref`, and `lanes`. Inline secrets are not accepted for the gateway, only `token_file`.

## Slice 3 — staging E2E (`tests/test_autopilot_e2e.py`)

- Synthetic project `synthetic/brandagent-staging` with A → test(QA) → B, run on real `QueueStore`, `VerifyQueue`, `OutcomeStore`, `EventBus` and `ResourceLockStore` in a tmp dir. A fake dispatcher persists before it sends. A fake independent verifier is used. There are no human calls between ticks.
- The scenarios are: two-coordinator race, IDLE not DONE, false ACK, node offline, crash after dispatch+event, cross-project isolation, a blocked owner switching to an independent task, prompt injection staying inert, and a dirty worktree with no destructive commands.

## Slice 4 — P1 planner (`terminal_mcp/autopilot_planner.py`, `tests/test_autopilot_planner.py`)

- Schema validation, the cycle/cross-project/oversize/sensitive/high-risk rejections, context redaction, idempotent apply, the deterministic fallback, `BLOCKED_PROVIDER`, and budgets.

## Slice 5 — P2 API (`terminal_mcp/autopilot_view.py`, dashboard routes, `tests/test_autopilot_view.py`)

- `autopilot_status/v1` snapshot, which must not fabricate a %. Readiness must be separate per gate, with stale shown as UNKNOWN.
- Pause and resume are idempotent and audited. A resume does not override a foreign pause.
- Delta notifications are cursor-based.
- Route guards are covered by tests that go through the dashboard app.

## Slice 6 — automatic QA dispatch (`autopilot.py`, `verify_queue.py`, `tests/test_autopilot_qa_dispatch.py`)

- Policy: `verifiers`, `max_qa_dispatches`, `qa_require_commit`; a verifier can never be an implementer lane.
- `VerifyQueue`: `claim_next(job_id=…)`, `ensure_verify_job(require_commit_evidence=…)`, a commit check in `complete`, and `reconcile(project_id=…)`. MCP `terminal_verify_claim` accepts `job_id`.
- RED→GREEN: A→QA→B; ACK/IDLE/DONE without a verdict is not a PASS; implementer rejection (also by id); claim-by-id never takes another job; no capable verifier; verifier unavailable then back; busy refusal costs no budget; duplicate events; restart before and after submit; lost lease and two coordinators; WAITING_INPUT/UNCERTAIN/node lost; QA dispatch budget; QA FAIL repair budget; missing and stale commit; prompt carries no agent text or token; Control Tower QA view.
- Production probes wired in `build_autopilot_runtime`; the loop still starts only when enabled.

## Slice 7 — trusted QA proof in the snapshot (`autopilot_view.py`, `tests/test_autopilot_proof.py`)

- `task_proof()` provenance per task; readiness and `readiness_detail` from trusted proofs only; MILESTONE_VERIFIED from VERIFY_PASS.
- RED→GREEN negatives: terminal receipt, reconcile-closed job with no verifier, self-verified, edited task evidence, commit mismatch (evidence and observed HEAD), incomplete evidence, no commit, device without a device id or on a receipt, failed device QA, cross-project and token leakage.

## Handoff state (2026-11-01)

- Branch `feat/projectflow-autopilot-106`, draft PR #107. Defaults off everywhere. No live config or deployment touched.
- Built and tested against synthetic executors: P0, config, P1 planner, P2 API, QA dispatch.
- Blocked on owner approval: G2 (observe-only) and G3 (dispatch) pilot on HP, naming the project, lanes, verifier session(s) and `approval_ref`. The first live run of `DirectTaskDispatcher` and of the production probes happens there.
- Not built: Control Tower UI widget; Gateway Task Center #115 consumer (separate repo, separate PR); live `GatewayPlanner` (needs an authorized endpoint and token file).

## Contracts

### `autopilot_status/v1`
```json
{"schema":"autopilot_status/v1","project_id":"…","generated_at":"…",
 "autopilot":{"enabled":false,"mode":"DISABLED|OBSERVE_ONLY|DISPATCH|HALTED","paused":false,
              "approval":{"approved_by":"…","approval_ref":"…"}},
 "outcomes":[{"id":"…","title":"…","status":"…","acceptance_criteria":["…"],
              "progress":{"completed":0,"total":0}}],
 "tasks":[{"id":"…","title":"…","status":"…","lane":"…","node_id":null,
           "depends_on":[],"updated_at":"…","stale":false,
           "qa":{"job_id":null,"status":null,"attempt":1,"commit_sha":null,"verifier":null,
                 "dispatches":0,"waiting_reason":null,"waiting_detail":null},"evidence_ref":null,
           "proof":{"kind":"VERIFIED_PASS|TERMINAL_RECEIPT|NONE","trusted":false,"problems":[],
                    "job_id":null,"attempt":null,"verifier":null,"verifier_node_id":null,
                    "implementer":null,"commit_sha":null,"verified_at":null,"device":null}}],
 "readiness":{"CODE_READY":"UNKNOWN|READY|NOT_READY","DEVICE_QA":"UNKNOWN|…","RELEASE_READY":"UNKNOWN|…"},
 "readiness_detail":{"CODE_READY":{"open":[],"failed":[],"unproven":[]},"DEVICE_QA":{"proofs":0}},
 "needs_owner":[{"task_id":"…","reason":"…"}]}
```

### Bus events
`type` ∈ AUTOPILOT_*. `actor="projectflow-autopilot"`, `entity_type="task"`, the payload contains no prompt text, and `causation_id` is the source event id when there is one.

## Verification before push
`git diff --check`. Run the targeted tests, then the full suite, and compare against the baseline in
`/tmp/claude-1000/-home-kimex-workspace-terminal-mcp/ffa1b57b-3e07-4c6a-a132-4842f69af694/scratchpad/baseline.txt`.
