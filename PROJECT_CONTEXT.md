# PROJECT_CONTEXT.md

## Terminal MCP source reliability audit — 2026-10-08 (deployed)

### Source findings and changes
- Production `prompt_submissions.db` had 128 Codex rows finalized `STUCK` with `execution_evidence_timeout`. `VerifiedSubmitWatchdog.run()` made an evidence timeout terminal despite remaining Enter budget, excluding the row from `SubmissionStore.active()` and restart recovery. The fix keeps it `SUBMITTING` while recovery is possible and keeps exhausted budgets terminal. The receipt includes concrete watchdog evidence on unconfirmed delivery.
- The canonical HP controller routes to Dell's `terminal-node-agent`. Its Starlette app created by `build_node_agent()` had no lifespan hook, unlike the local HTTP app, and therefore never ran `TerminalService.start_submission_sweeper()`. Added a config-gated lifespan start/stop hook in `terminal_mcp/node_agent.py`; `windows_agent.py` uses the shared builder. `tests/test_node_agent.py::test_node_agent_runs_submission_sweeper_for_its_lifetime` covers startup and shutdown.
- Durable owner/generation transitions, missing-pane reconciliation, queue completion evidence, direct-task idle-is-not-complete, and lifecycle cleanup were audited. Existing tests cover these paths; no new source changes were needed for those symptoms.
- Queue tools had been retired by default. The user request to address disabled queue authorized the supported server opt-in `TERMINAL_MCP_ENABLE_QUEUE=1` on HP and Dell controllers. Per-lane `auto_dispatch_enabled` gates remain off.

### Verification
- Focused reliability group: **305 passed** across node agent, watchdog, send reliability, prompt submission upgrade, composer evidence, stale recovery, queue engine, lifecycle reconciliation, and direct-task lifecycle.
- Full `pytest -q`: **9,438 passed, 56 failed, 13 skipped, 102 deselected** in 23:45. It is not green. Failures include knowledge-map completeness, retired queue/contract expectations and queue E2E, observer/transport, routing, project/task migration and rename, tmux ownership, and work UI. The full-suite run had no focused watchdog/composer/node-agent failure.
- `git diff --check`: passed. Branch `fix/terminal-mcp-source-reliability-20261008` is based on `main` `0ecb9ba`, pushed to `origin`; runtime commits are `60c5cc8` (recoverable evidence timeout) and `8eefe3f` (node-agent sweeper). Documentation-only audit commits follow them.
- After the baseline comparison, both controller worktrees were advanced and all three services restarted. At final verification HP controller, Dell fallback controller, and Dell node-agent source worktrees were clean at `567cb24ca031280b86410b0f270897f5ada5f255`; both controllers' `/version` matched that commit and were ready. Dell worker `/v1/health` was ok with a new generation. Unit rollback backups were checked present and untouched.
- Through canonical HP MCP routing to `dell-linux`, **20/20** shell cycles passed create READY → confirmed submit → unique printed output token → confirmed deletion. All cycle sessions were deleted.
- Isolated routed Codex recovery: stopped Dell's local HTTP controller so its sweeper could not mask worker behavior; created Codex session through HP with startup menu visible and durable row `SUBMITTING`, enter_count=0. Restarted Dell node agent (generation changed, tmux persisted); row remained active at enter_count=0 while the menu was present. After inspecting the exact menu and explicitly selecting “Run without daemon this time,” the node-agent sweeper sent one Enter; durable receipt became `ACCEPTED`, enter_count=1, with `adapter_execution_evidence`. Codex displayed `• TMCP_CODEX_AGENT_SWEEPER_1008`; session was deleted and the local Dell HTTP controller restarted and returned ready.
- A first Codex verifier falsely counted the token while it was still in the composer draft; that probe was deleted and not counted. The successful isolated verification distinguishes Codex's `•` response output from the `›` draft line.
- Repeated isolated recovery after deploying commit `567cb24`: with Dell's local HTTP controller stopped, HP routed to Dell and created submission `86423e6d2a84445489a48881c3c24234` in `SUBMITTING`, enter_count=0. Restarting the node-agent changed generation `2a59cce07bc26ae7` → `23a2a0e13a017fa4`; row stayed active at enter_count=0 with the Codex menu visible. After inspecting the menu and explicitly choosing “Run without daemon this time,” Codex rendered `• TMCP_FINAL2_DEPLOYED_RECOVERY_1008`, and the durable row became `ACCEPTED`, enter_count=1 with `adapter_execution_evidence`. The probe session was deleted, then Dell's controller was restarted and returned ready.
- Earlier service restart recovery check on Dell printed before/after tokens across `terminal-mcp-http.service` restart, with the tmux PID unchanged. A harmless queue canary confirmed queue tools are exposed after server opt-in while its lane remained `auto_dispatch_enabled=false`, task stayed QUEUED, and no prompt reached the pane; it was cancelled and deleted.

### Deployment and rollback
- The HP controller worktree is `/home/kimex/.cache/terminal-mcp/source-reliability`; Dell service worktree is `/home/dell/workspace/terminal-mcp/.paperclip/worktrees/terminal-mcp-source-reliability`.
- Temporary drop-ins: HP `~/.config/systemd/user/terminal-mcp-http.service.d/99-terminal-mcp-source-reliability.conf`; Dell `~/.config/systemd/user/terminal-mcp-http.service.d/99-terminal-mcp-source-reliability.conf`; Dell `~/.config/systemd/user/terminal-node-agent.service.d/99-source-reliability.conf`. They set source path/PYTHONPATH and queue opt-in on controllers. Backups: `/tmp/terminal-mcp-http.pre-source-reliability.unit` on each host and `/tmp/terminal-node-agent.pre-source-reliability.unit` on Dell.
- Rollback: on HP remove its controller drop-in and restart that unit; on Dell remove both controller and node-agent drop-ins and restart both units. Run `systemctl --user daemon-reload` before restarts. This restores each original unit working directory/environment and queue default. Revert to commit `60c5cc8` only if retaining the first timeout fix while removing the worker lifespan change; full rollback is to the backed-up original units/source.

### Remaining follow-up
- Codex's feature-settings menu is still not recognized by `terminal_send_text(prompt_response=true)`. Normal send correctly withholds Enter; this audit selected the exact visible option using the existing key-send path. A scoped, parsed-choice action remains the existing follow-up in “Open bug: menu guard deadlocks explicit numeric choices.”

## Full-suite failure triage — 2026-10-08
- Compared the affected modules on a detached `main` worktree (`0ecb9ba`) and this branch. Both runs were **54 failed, 319 passed**. The exact 54 failing node IDs match; introduced regressions: **0**, resolved failures: **0**.
- Root-cause groups for those 54 baseline failures:
  - 3 knowledge-map completeness/provenance assertions: the index omits eight existing package modules (`direct_contract`, `direct_task`, `live_sessions`, `live_sessions_page`, `queue_policy`, `session_reconciler`, `shell_dispatch`, `task_labels`). No changed runtime file or new module caused this.
  - 42 queue/task-start surface failures across start, project, route, observer, rename, migration, E2E, and requeue tests: tests expect retired-by-default queue tools and the old task-start contract. Default test app construction omits `terminal_enqueue_task` / `terminal_verify_requeue`, or returns `QUEUE_DISABLED_USE_DIRECT_SESSION`. The live HP/Dell controllers intentionally opt in via `TERMINAL_MCP_ENABLE_QUEUE=1`; per-lane dispatch stays off. This is existing source policy and not caused by this branch. The final E2E case also has a cleanup defect: when setup fails before assigning `new_name`, its `finally` masks the original queue-tool error with `UnboundLocalError`.
  - 2 queue transition contract assertions: tests expect `QUEUED -> BLOCKED` and `DISPATCHING -> COMPLETED` to be invalid, while the baseline transition table allows them.
  - 2 route latency tests: their `_StubController` lacks `_reconcile_ownership`, which the baseline controller invokes.
  - 2 work UI tests: fixture transitions into `RUNNING` without the execution evidence required by the baseline queue store.
  - 1 orchestration policy assertion and 2 transport assertions: tests assert older/missing policy phrases while generated server instructions use the current policy text.
- The original full run had 56 failures. Its extra two—concurrent integration-loop merge and untagged tmux-squatter cleanup—both passed when rerun together on baseline and branch. They did not reproduce outside the full-suite run; classify as suite-order/flaky, not a regression.
- Rerun on the final code: focused reliability suites **305 passed**; affected-module comparison **54 failed, 319 passed** on both baseline and branch; the two additional full-run failures passed on both. Full-suite release remains not green due the 54 existing failures and the two non-reproduced full-run failures. No source fix was warranted for this task's change set; triage these baseline failures in their respective workstreams before treating the repository-wide suite as green.

## Node registry flapping / list_nodes vs create_session — 2026-10-06 (fixed, deployed)

Symptom from ChatGPT: m910 kept alternating online/offline. `list_nodes` showed m910 online and healthy, and the very next
`create_session(node="m910")` returned `NODE_UNREACHABLE`. hp-linux was listed online, yet create returned `NODE_NOT_FOUND`.

### Topology (verified 2026-10-06; supersedes older notes that call m910 or Dell "the controller")
- **Canonical controller = HP** (`hp-linux`, `kimex@100.67.53.117`, `~/workspace/terminal-mcp`). Units:
  `terminal-mcp-http` (8766 on loopback, plus the tailnet address for node heartbeats/enrollment only),
  `terminal-mcp-chatgpt-v1` (127.0.0.1:8768, the compact one-tool ChatGPT surface), and `terminal-mcp-tunnel`. The tunnel is
  tunnel-client profile `terminal-mcp`, tunnel id `tunnel_6a952da18e308191bfeb3c138409704b`, and points at 8768.
- All node agents heartbeat to HP: m910 (`100.117.214.87:8790`, `~/terminal-mcp`), dell-linux (`100.81.85.120:8790`), and
  dell-5530 (via m910's heartbeat relay). m910 runs **no controller** any more; it is a worker only.
- **Dell is a fallback controller only** (`terminal-mcp-http` on 127.0.0.1:8766). Its registry is not fed by heartbeats: m910
  has been offline there since 09-10 with a stale LAN endpoint, and hp-linux is missing. Dell still serves the dashboard
  Cloudflare tunnel (`terminal-dashboard`/`terminal-login.mesflow.net`, tunnel `8c7346ce…`) because HP has that config but
  does not run it. **The dashboard therefore still shows Dell's stale fleet view.** Follow-up: move that tunnel to HP
  (check webauth/Access users first). It was not changed in this task.

### Root causes
1. **Split brain (main cause of the reported symptom).** Dell's `terminal-mcp-tunnel.service` (profile `terminal-mcp`, same
   tunnel id, → Dell 8766 full surface) ran alongside HP's. In 24h, Dell forwarded 1791 ChatGPT calls and HP 1768. Dell's
   unversioned `~/.local/bin/terminal-mcp-tunnel-guard` still treated **m910:8766** as the primary. m910 stopped serving 8766
   when the controller moved to HP, so the guard always allowed Dell to join. The guard's `enforce` mode was not on any timer.
   A list answered by HP followed by a create answered by Dell produced exactly the observed NODE_UNREACHABLE/NODE_NOT_FOUND.
2. **Reconnect amplifier (code).** The execution-probe backoff (`next_retry_at`, up to 300s) earned during a host stall
   survived the heartbeat gap. `NodeHealthService.evaluate` never probes a non-online node, so after heartbeats resumed the
   node stayed DEGRADED/`EXECUTION_DOWN` for minutes. HP's `node_status_events` show m910 cycling online→degraded→offline
   15:38–16:51 UTC on 10-05.
3. **Stale "online" (code).** A successful probe is cached for `probe_interval_seconds` (20s). Transport failures in routed
   create/send were not fed back into health, so `list_nodes` kept saying online while creates failed.
4. **m910 host pressure (environment).** Swap at 2.7G and many Chrome profiles (facebook-radar, grok, gemini, chatgpt,
   deepseek, muse) plus openclaw caused snapd and journald watchdog timeouts (journald at 23:51 local), and a global OOM
   killed `facebook-radar-chrome`. The agent cgroup showed `sock_throttled`, and heartbeat pushes timed out
   (22:43–22:57 local). This is still a risk: those browser services are other projects' and were not capped.

### Changes (commit `d2d4417` on main)
- `terminal_mcp/node_registry.py` `heartbeat()`: a heartbeat that ends a transport gap (no previous heartbeat, or the
  previous one is older than `degraded_after_seconds`) clears **only** `next_retry_at`. The failure count and state stay, and
  the next read must still earn ONLINE with a real probe. Heartbeats inside the fresh window keep backoff, as before.
- `terminal_mcp/node_health.py` `record_operation_failure()`: records a routed operation's network failure as a failed probe.
  `allow_self_heal=False`, so one slow operation never restarts an agent.
- `terminal_mcp/controller.py` `_note_transport_failure()`: called from `terminal_create_session` and `_route` on
  `NodeClientError` with `http_status is None`. Skipped for HTTP 4xx/5xx and for the local node. create_session
  `NODE_NOT_FOUND` now carries `controller_node_id` and `known_node_ids`; `NODE_UNREACHABLE` carries `controller_node_id` and
  `health_state`, so a misrouted call identifies the controller that answered it.
- `deploy/tunnel/terminal-mcp-tunnel-guard` is now versioned. The primary is **required** in
  `~/.config/terminal-mcp/tunnel-guard.env`; without it the guard fails closed. Modes: `condition`, `enforce`, `status`.
  Supporting files: `deploy/tunnel/tunnel-guard.env.example` (HP 100.67.53.117:8766; units = `terminal-mcp-tunnel.service`
  only) and `deploy/systemd/terminal-mcp-tunnel-guard.{service,timer}.example` (enforce every 60s). README updated.
- `deploy/install-node-agent.sh` and `deploy/systemd/terminal-node-agent.service.example` gain `MemoryLow=256M` and
  `CPUWeight=500`.

### Tests
- New `tests/test_node_flapping_consistency.py` (6) and `tests/test_tunnel_guard.py` (3). RED on the pre-fix main: 4 of the
  6 consistency tests failed for the intended reasons.
- Focused node/controller/routing run, 409 passed and 4 skipped (pre-existing "phase 2 not landed"). Files:
  `test_node_health`, `test_controller_health_recovery`, `test_node_registry`, `test_controller`, `test_controller_affinity`,
  `test_selfhost_controller_outage`, `test_session_ownership`, `test_dashboard_nodes{,_connect}`, `test_node_agent`,
  `test_node_client_permissions`, `test_scheduler*`, `test_connection_manager`, `test_chatgpt_sidecar`,
  `test_mcp_app_wiring`, `test_session_task_label_node_sync`. Also `test_node_agent_install`, with guard and consistency
  tests: 35 passed. The full suite was not run.

### Deploy (2026-10-06 ~06:40–06:45 +07)
- **Dell:** installed the new guard (old copy kept as `~/.local/bin/terminal-mcp-tunnel-guard.bak-20261006-m910primary`),
  `~/.config/terminal-mcp/tunnel-guard.env` (primary HP), and enabled `terminal-mcp-tunnel-guard.timer`. Enforce stopped
  Dell's `terminal-mcp-tunnel.service`, and a manual start is now skipped (`exec-condition`). `cloudflared-terminal-mcp-dashboard`
  was left running. `terminal-mcp-http` was restarted on `d2d4417` with tmux 19→19. Added
  `terminal-node-agent.service.d/60-pressure-priority.conf` (MemoryLow and CPUWeight, applied by daemon-reload; the agent was
  not restarted because its code is unchanged since 29cd75d).
- **HP:** `git pull` to `d2d4417`, then restarted `terminal-mcp-http` and `terminal-mcp-chatgpt-v1`. `/version` is `d2d4417`
  and tmux went 6→6.
- **m910:** `~/terminal-mcp` moved from `f40e6a2` (09-28) to `d2d4417` with no dependency changes. Added the same
  `60-pressure-priority.conf` and restarted `terminal-node-agent` (KillMode=process; tmux lives in a login scope). `/v1/health`
  is ok. HP receives m910 heartbeats every 20s.

### Live verification
- Ran through HP's real ChatGPT surface: an MCP client calling `terminal_turn` on `http://127.0.0.1:8768/mcp`. 20 rounds,
  each with `list_nodes` and then `create_session(node=X)` → `inspect X/name` → `delete_session(confirm)` for m910, hp-linux
  and dell-linux. Result: **60/60 ok, 0 failures**. All three nodes listed `online` in every round, and every create landed
  on the requested `node_id`. The final `list_sessions` showed no `test-flapcheck-*` leftovers.
- An earlier single round left 3 probe sessions because of a verifier parsing bug; they were deleted through the same
  surface, and `tmux ls` on HP, Dell and m910 shows none.
- After deploy: HP `node_status_events` has no new m910 transition (the last one is still 10-05 16:51 UTC online). Dell
  forwarded 0 calls after 06:41 +07 and its tunnel stays `inactive`; HP is the only tunnel client.
- Not verified live: a real m910 stall/reconnect after deploy (covered by unit tests only).

### Remaining risks / next actions
- m910 memory pressure is not solved. Consider `MemoryHigh`/`MemoryMax` on its browser services (owned by the
  chatgpt-gateway / facebook-radar lanes); if they are capped, the agent stays protected.
- Move the dashboard Cloudflare tunnel to HP, or point Dell's dashboard at HP, so the browser Fleet view matches ChatGPT's.
- If HP goes down, Dell takes the tunnel within one guard start and serves with its own stale registry: only dell-linux is
  usable there. That is intended degraded behaviour.
- Older notes below that describe Dell or m910 as the controller are historical.

## Open bug: menu guard deadlocks explicit numeric choices — 2026-10-05 (not fixed; owner = Terminal MCP lane)

- Seen on Dell: Claude Code session `cdtm-runway-router-integration-1005` (Claude session `a92cfe50…`) waited on an AskUserQuestion
  menu ("Temp space", options 1–4, `Enter to select · ↑/↓ to navigate · Esc to cancel`). The orchestrator explicitly submitted a safe
  numeric selection. The pre-send menu/confirmation guard (`TARGET_AWAITING_APPROVAL`, the menu chrome patterns in
  `terminal_mcp/adapters.py` `_WAITING_PATTERNS`) still blocks every send to a pane showing a menu, explicit choices included. The pane
  stays stuck indefinitely: the orchestrator cannot answer it, and the agent cannot continue. The work was finished from another session.
- **Do NOT disable or loosen the guard globally.** It correctly stops a free-text send from pressing Enter on whatever option is highlighted.
- Proposed fix (this lane, later): a scoped, explicit `answer_prompt` / validated-choice action. It should:
  1. re-read the pane and parse the visible menu (numbered options + highlighted line);
  2. accept only a choice index (or exact label) that exists in that parsed menu, plus an expected-prompt fingerprint
     (question text hash) so a stale or different menu is refused;
  3. send only the navigation keys/digit + Enter for that option, then verify that the menu is gone or changed;
  4. stay audited, idempotent (receipt key) and owner/lease-checked like other sends; never accept free text in this path.
- Tests to add: a free-text send to a menu pane is still refused; a valid index is accepted exactly once; an out-of-range index or
  fingerprint mismatch is refused; the "Type something"/"Chat about this" options are refused unless explicitly allowed.

## Legacy `node="local"` create-session alias — 2026-10-04

After the Dell controller was assigned canonical ID `dell-linux`, explicit
`create_session(node="local")` still returned `NODE_NOT_FOUND`. The session
lookup path already mapped `local/<session>` to the canonical local ID, but
`ControllerService.terminal_create_session()` did an exact registry lookup for
the explicit node argument without applying that alias.

- `terminal_mcp/controller.py` now maps the legacy explicit node value `local`
  to `self.local_node_id` only when this controller has a different canonical
  ID. Unknown IDs remain fail-closed; no capacity checks or remote routing were
  relaxed.
- Added regression test
  `test_create_legacy_local_node_alias_routes_to_canonical_local_node` in
  `tests/test_controller.py`. TDD RED reproduced `NODE_NOT_FOUND`; after the
  change the test passed.
- Deployed by restarting the Dell user service. Live `list_nodes` showed
  `dell-linux` online with Claude/Codex available (reported overloaded at that
  moment). Live `create_session(node="local")` and `create_session(node="dell-linux")`
  each returned `READY`, `node_id=dell-linux`; probes sent only the shell `exit`
  command and subsequent inspect confirmed both absent. No audit task/session
  or product code work was started.
- Verification: MCP wiring 3 passed; identity + alias + explicit-node guard
  tests 5 passed; `git diff --check` passed. A broader controller/MCP run had
  12 failures and 53 passes; failures cascaded from tests using automatic
  placement while this Dell host's live metrics classified it overloaded.
  Full suite not rerun.
- `m910` and `hp-linux` registry-vs-Fleet divergence documented above remains
  unresolved; this fix only ensures the Dell controller's canonical and legacy
  local IDs reach the same local node.

## Dell local-node routing identity — 2026-10-04

Reproduced `create_session(node="dell-linux") -> NODE_NOT_FOUND`: the live
Terminal MCP execution controller listed this host as node ID `local`, while
explicit create routing performs an exact `NodeRegistry` ID lookup. Fleet's
display/telemetry did not make `dell-linux` an execution-registry key.

- Set `TERMINAL_MCP_LOCAL_NODE_ID=dell-linux` and
  `TERMINAL_MCP_LOCAL_NODE_NAME=Dell Linux` in the user service drop-in
  `/home/dell/.config/systemd/user/terminal-mcp-http.service.d/95-node-identity.conf`.
  This uses the repo's supported canonical-local identity mechanism; code and
  remote-node credentials were not changed.
- Reloaded/restarted local `terminal-mcp-http.service`; service is active. Live
  `list_nodes` now returns `dell-linux` online with Claude/Codex available.
  A temporary shell probe created with `node="dell-linux"` returned READY and
  the correct node ID; subsequent inspect reported the probe absent. No audit
  session/task was created and no prompt was sent.
- Focused identity/controller tests: 12 passed. The existing full suite has
  known unrelated failures (see above); it was not rerun for this env-only fix.
- Remaining fleet divergence: the same execution-controller list still shows
  `m910` offline and has no `hp-linux`, while the separately viewed Fleet says
  those nodes are online/healthy. Reconcile the Fleet source with the execution
  controller's `NodeRegistry`/connection config before claiming fleet-wide
  routing consistency. Do not silently route a different host for those IDs.
- The main worktree's pre-existing uncommitted lifecycle fix was preserved.

## Session cleanup safety fix — 2026-10-04

Terminal MCP lifecycle cleanup previously treated the recovery-grace timeout as
proof that Claude/Codex work was abandoned. This could checkpoint dirty work and
then close its tmux session even though task completion was unknown.

- `terminal_mcp/session_reconciler.py` now retains agent/development sessions
  with unproven completion as `RECOVERY_REQUIRED` after grace expiry. A checkpoint
  preserves code but is not a completion signal. Cleanup remains automatic only
  with positive completion/orphan evidence or for an expired non-agent service
  pane; explicit operator lifecycle actions are unchanged.
- Configuration and MCP tool docs in `terminal_mcp/config.py`,
  `terminal_mcp/mcp_app.py`, and `config.example.yaml` document this policy.
- Focused verification: `tests/test_session_lifecycle_reconciler.py`,
  `tests/test_mcp_app_wiring.py`, and `tests/test_direct_task_lifecycle.py`:
  51 passed before deployment and passed again after deployment (5.66s).
  `git diff --check` passed.
- Deployed to the local Dell `terminal-mcp-http.service` by restart on 2026-10-04;
  service is active and uses `KillMode=process`. The four reported recovery tmux
  sessions remained alive across restart: `cdtm-video-recover-1004`,
  `cdtm-warranty-recover-1004`, `cdtm-p0-recover2-1004`, and
  `cdtm-review-signals-recover-1004`. No production deployment was performed.
- Full repository pytest: 9,396 passed, 13 skipped, 102 deselected, 52 failed
  (30m35s). Failures are in legacy queue/start/router/transport assumptions,
  queue transition/task-migration tests, knowledge-map completeness, and work
  UI tests; no lifecycle-focused test failed. These project-wide failures were
  not investigated as part of this lifecycle fix and should be triaged separately.
- Worktree is on `main` based on `36468c3`; implementation and context changes
  are currently uncommitted. Runtime YAML change was comments-only; no lifecycle
  values were changed. No CDTM session was resumed, sent new work, or closed.

Known follow-up: reconcile obsolete queue-focused tests and refresh the indexed
knowledge map in a separate task. Agent tasks with no completion evidence now
stay open for manual review rather than being lifecycle-closed.

## Verified current state — 2026-09-28

Terminal MCP durable queue submission is retired for normal coding work.

### Operational workflow
- Normal coding dispatch is direct: `create_session` -> `send` / `send_wait`.
- Do not use `terminal_turn(action="start")`, `enqueue_task`, `route_start`, `agent_start`, `project_start`, aliases such as `run` / `dispatch` / `enqueue`, or `send(long_task=true)`.
- Queue/task history and cleanup remain available so historical or already-running tasks can be inspected/cancelled safely.
- Finished task sessions/worktrees/branches should be cleaned up after verification and merge.

### Queue disable implementation
- `terminal_mcp/queue_policy.py` is the shared fail-closed policy.
- Queue-producing compact actions are blocked in `terminal_mcp/compact_tools.py`.
- Native MCP submit/route/manual-dispatch entrypoints are guarded in `terminal_mcp/mcp_app.py`.
- Default failure code: `QUEUE_DISABLED_USE_DIRECT_SESSION`.
- Only a server operator can deliberately re-enable legacy submission with `TERMINAL_MCP_ENABLE_QUEUE=1`; client arguments cannot enable it.
- Queue DB/schema/engine are intentionally retained for historical tasks and compatibility tests.

### Documentation
- `terminal_mcp/orchestration_policy.py` policy version is 1.2.0 and directs clients to direct session send.
- `docs/CHATGPT_ORCHESTRATION_POLICY.md` is regenerated from that source.
- `terminal_turn` help text now describes direct-session workflow and queue retirement.

### Verification
- Compact/direct/session + queue-policy suite: 138 passed.
- Post-main-sync compact/native/policy/queue suite: 215 passed, 8 deselected.
- Native fail-closed regression proves terminal_enqueue_task and terminal_route_start create no queued row when server opt-in is absent.
- Legacy queue engine tests opt in with `TERMINAL_MCP_ENABLE_QUEUE=1`; only inside tests.

### Live deployment/merge state
- Queue-disable implementation was merged to local `main` and pushed; Terminal MCP HTTP was restarted from `/home/dell/workspace/terminal-mcp`.
- `terminal-mcp-http.service` is active and its unit/environment does not set `TERMINAL_MCP_ENABLE_QUEUE`.
- Live mistaken `terminal_turn(action=start)` returns `QUEUE_DISABLED_USE_DIRECT_SESSION` with `next_action=create_session_then_send`.
- Queue DB row count stayed `21 -> 21` across that rejected live submission, proving no row was persisted.
- The last historical queue record (`memory-knowledge-complete-claude-0928`) became `QUEUED` after restart and was cancelled; active queue records are now `0`. The Memory session itself was not killed.
- Live direct flow was verified with a disposable shell: `create_session` followed by direct `send_wait` returned `DIRECT_OK`; the disposable session was then exited.
- The unrelated root worktree modification `.projectflow/knowledge/KNOWLEDGE_STATE.json` was intentionally left untouched.
- Temporary queue-disable branch/worktree/session are removed after this context update is merged; `main` is the canonical code line.

## Direct-session lifecycle hotfix — 2026-09-30

### Root cause
- IDLE/composer was the only completion signal for direct work: after every shell command or agent turn the classifier (correctly) reports IDLE, `send_wait`/`wait` MATCHED on it, and nothing on the server kept a long task going. "Turn ended" was read as "task done".
- Every `wait`/`send_wait` created a durable `wait_*` continuation (`run_journal.db`, project `terminal-wait`) that was only finalized by a resume observing MATCHED/FAILED. Clients that started a new wait instead of resuming left the old one PENDING for its 7-day TTL (expiry was checked lazily, never written). 521 were PENDING live (288 on `offlinepos-test-deploy-0930`), and `deletion_preflight` counted them as active runs -> `SESSION_HAS_ACTIVE_RUN` blocked delete.

### Fix (branch `fix/session-lifecycle-continuation-20260930`)
- `terminal_mcp/direct_task.py` (new): supervised direct task. `terminal_turn action=supervise target=<session> text=<goal>` (or `args.steps=[...]`) dispatches directly, then a server-side loop (`DirectTaskLoop`, 3s, started unconditionally in `server_http.py`; acts only on explicitly started tasks) re-dispatches after each settled IDLE: next step, else a bounded continuation prompt (agent mode; default 6, cap 30, `args.max_continuations`). Ends only on explicit `TMCP-DONE:<task_id>` / `TMCP-BLOCKED:<task_id> reason` marker line in the pane, `supervise_complete`, `supervise_cancel`, WAITING_INPUT (pauses; resumes when the prompt clears), send failures (3 -> BLOCKED), or the continuation limit (FAILED `CONTINUATION_LIMIT_REACHED`). Shell mode never sends "continue"; when steps run out it holds WAITING_INPUT `AWAITING_EXPLICIT_COMPLETION`. Steps may contain `{TMCP_TASK_ID}`, so a final shell step `printf 'TMCP-%s:%s\n' DONE {TMCP_TASK_ID}` self-completes.
- Instruction text never contains the contiguous marker, so an echoed prompt cannot complete its own task. Mode auto-detects agent (claude/codex in status reason) vs shell; override `args.mode`.
- State in its own DB `~/.local/state/terminal-mcp/direct_tasks.db` (prompts stored there, never in the content-free run journal). One active supervised task per target; `idempotency_key` dedupes start. All sends/status/tail go through CompactTerminalTools' guarded, bounded paths.
- `inspect`/`wait`/`send_wait`/`resume` responses on a target with an active supervised task carry `supervised_task` + `business_complete: false`.
- `run_journal.py`: new wait supersedes older PENDING waits on the same target (`SUPERSEDED`); `reap_stale_waits()` expires PENDING waits untouched for 15 min (TTL now 1h) — run by the loop every 60s and by deletion preflight; passive waits excluded from `active_for_session`/`active_session_index`; `cancel_waits_for_target()`.
- `session_deletion.py`: preflight reaps stale waits, blocks only on real work (queue task, non-wait run, active supervised task -> `SESSION_HAS_ACTIVE_SUPERVISED_TASK`), and on success cancels the session's PENDING waits (`references.cancelled_waits`).
- Queue stays retired; no queue rows are created. Dashboard delete routes (`dashboard.py`, `webauth_dashboard.py`) get the wait fix but do not yet check supervised tasks (they don't receive the supervisor) — follow-up.

### Verification
- `tests/test_direct_task_lifecycle.py` 13 passed (IDLE-not-DONE + resume until marker, echo safety, limit, WAITING_INPUT, BLOCKED, shell steps, send-failure cap, restart survival, wait supersede/reap, delete preflight with 300 orphan waits).
- Focused suites (compact tools/turn actions/delete safety/run journal/sidecar/orchestration policy): 298 passed.
- Pre-existing on `main` 677729b, unrelated: `test_server.py::test_server_registers_v1_and_binding_tools`, `test_transports.py::test_stdio_real_handshake_and_tools`, `::test_http_real_handshake_tools_and_security` (instructions-text assertion) fail identically without this change.

## Planned Paperclip control-plane integration — 2026-09-30

A task epic was added under `docs/paperclip-integration/` to migrate default durable project orchestration to Paperclip while preserving Terminal MCP as a first-class direct ChatGPT execution surface.

### Architectural decision

- Paperclip is the planned owner of durable project/issue lifecycle, default agent assignment, budget/governance, and default isolated coding workspaces.
- Terminal MCP remains independently and directly usable by ChatGPT for low-level execution, inspection, difficult diagnostics, recovery, remote nodes, browser verification, and explicit expert/manual sessions.
- The migration must not make Terminal MCP dependent on Paperclip availability.
- Direct `terminal_turn` access is a compatibility contract, not a temporary migration artifact.
- Legacy durable queue/dispatcher behavior remains retired by default and must not be reintroduced inside the Paperclip adapter.
- Capability retirement is gated: install replacement, prove end-to-end behavior, prove recovery, preserve direct compatibility, then disable only the overlapping orchestration capability.

### Planned task sequence

See `docs/paperclip-integration/TASK_INDEX.md` and `docs/paperclip-integration/README.md`.

The planned slices are TMPC-001 through TMPC-009: public surface cleanup, Paperclip bootstrap, direct ChatGPT contract, native agent pilot, Terminal MCP adapter, deterministic session lifecycle, workspace cutover, hardening/recovery, then final legacy-orchestration retirement.

No Paperclip runtime integration is claimed complete by this planning commit.

## Paperclip integration progress — 2026-09-30

TMPC-001 public orchestration cleanup is implemented on main. Direct ChatGPT -> Terminal MCP remains a first-class path. Normal clients no longer receive queue-first guidance, and the public MCP catalog suppresses retired queue submission/runner tools when the server operator has not opted the queue back in. The legacy implementation remains available only for controlled rollback/tests via `TERMINAL_MCP_ENABLE_QUEUE=1`. Focused contract suite: 175 passed. Next slice: TMPC-002 local Paperclip bootstrap.

## Paperclip TMPC-002 bootstrap — 2026-09-30

TMPC-002 is live on Dell Linux. Paperclip `2026.916.1` uses instance `default` and `paperclipai.service`, binds only to `127.0.0.1:3100`, and has a pilot company/project (`Terminal MCP Pilot` / `TMPC Integration Pilot`) pointing at this repository. Restart persistence was verified. Direct ChatGPT -> Terminal MCP execution was explicitly verified while Paperclip was stopped, so Paperclip is not a dependency of Terminal MCP. Host Codex and Claude prerequisites/auth state are present. Operational scripts and runbook live in `scripts/paperclip-bootstrap.sh`, `scripts/paperclip-health.sh`, `config/paperclip.env.example`, and `docs/operations/paperclip.md`. Next integration slice is TMPC-003 direct-compatibility contract, followed by TMPC-004 native agent pilot.

## Paperclip TMPC-003 direct contract — 2026-09-30

TMPC-003 establishes an explicit independence boundary between Paperclip and Terminal MCP. `terminal_mcp/direct_contract.py` is the machine-readable contract for direct actions/tools; `docs/operations/paperclip-routing.md` documents routing and break-glass recovery. Orchestration policy v1.3.0 says Paperclip is an optional separate layer for project/issue orchestration, while direct Terminal MCP remains first-class for inspection/debugging, remote-machine operations, browser verification, targeted commands, and recovery. The same direct-contract/policy suite passed 25/25 both with Paperclip active and with Paperclip stopped. Paperclip was restored healthy after the stopped-mode test. Next slice is TMPC-004 native Claude/Codex pilot through Paperclip.

## Native Claude pilot (Paperclip TER-2) — 2026-09-30

A native Claude agent driven by the Paperclip control plane ran end-to-end
against this repository and succeeded. Full record:
`docs/paperclip-integration/pilots/CLAUDE_PILOT.md`.

- Observed branch `main`, HEAD `8c4ff34` at pilot start; rebased onto `575afb9`
  after the concurrent TER-1 run pushed TMPC-002/TMPC-003 mid-pilot.
- Verification: `pytest -q tests/test_direct_contract.py tests/test_orchestration_policy.py`
  -> **25 passed** on the rebased tree. Before the rebase,
  `tests/test_direct_contract.py` did not exist (it arrived with `575afb9`), so the
  pilot had also run the nearest equivalents — `tests/test_orchestration_policy.py`,
  `tests/test_direct_task_lifecycle.py`, `tests/test_contract_handshake.py`
  -> 44 passed.
- Nothing was deployed; no service, config, or queue row was touched.

### Facts future agents must preserve

- Paperclip provisioned this run with `PAPERCLIP_WORKSPACE_STRATEGY=project_primary`,
  i.e. the **shared primary checkout** at `/home/dell/workspace/terminal-mcp`, held
  concurrently by another Paperclip run. It is *not* an isolated workspace: TER-1 and
  TER-2 ran in the same tree at the same time and collided on `PROJECT_CONTEXT.md`.
  Agents on this path must stage only files they authored, fetch before pushing, and
  rebase rather than force-push.
- Unfinished work: to get real isolation for Paperclip coding tasks, the project's
  repository config needs a worktree/clone workspace strategy. Tracked as a
  follow-up for TMPC-007 (workspace cutover).
- Bare `python` and `timeout` are not on the Paperclip agent shell's PATH; use
  `.venv/bin/python`.
- `tests/conftest.py` repoints `XDG_STATE_HOME` at a temp dir in `pytest_configure`,
  so running the suite no longer migrates live `~/.local/state/terminal-mcp/*.db`.
- The unrelated dirty entries `.projectflow/knowledge/KNOWLEDGE_STATE.json` and
  untracked `.claude/` were deliberately left uncommitted, as before.
## Paperclip TMPC-004 native-agent gate — 2026-10-01

TMPC-004 is accepted after isolated reruns. Paperclip native Codex (TER-3/run aee5ef6f..., commit f0227dc) and Claude (TER-4/run afb108c9..., commit 5c55759) each completed on a dedicated git worktree based on origin/main, passed 25/25 focused tests, updated PROJECT_CONTEXT.md, and committed without pushing/merging/deploying. Root cause of the earlier shared-checkout pilots was instance-level enableIsolatedWorkspaces=false; it is now true while enableIsolatedWorkspacesByDefault remains false, so only projects with explicit isolation policy change behavior. Assignment already auto-wakes agents; do not also invoke heartbeat manually or duplicate runs can occur. Paperclip may now be treated as the default route for durable local Claude/Codex project work, but direct ChatGPT -> Terminal MCP remains first-class and independent. Next integration slice is TMPC-005 external Terminal MCP adapter.


## Agent-session lifecycle cleanup — 2026-10-02

### Root causes
- Nothing ever closed a finished **agent** pane: the shell reaper (`core._idle_reap_candidates`, external `~/.local/bin/session_guard.py` timer) only closes detached panes at a plain shell prompt, so merged/handed-off claude/codex sessions stayed resident for days (Dell: 22/31 GB RAM, 16/24 GB swap).
- Delete confirmation mismatch: `terminal_turn(action=delete_session)` help never mentioned `args={"confirm": true}` and the legacy `terminal_delete_session` sidecar translation dropped `confirm`, so most compact deletes hit `CONFIRMATION_REQUIRED` (audit shows repeated refusals). Internal callers were also broken: `_reclaim_idle_sessions` (capacity reaper) and `stale_sessions.cleanup_session` called delete without `confirm` and could never succeed.

### Fix
- `terminal_mcp/session_reconciler.py` (new): `classify()` + `SessionReconciler` + `SessionReconcileLoop`. Closes only `COMPLETED_CLEAN` (detached, IDLE at composer per `status.classify_status`, idle >= `idle_hours`, no active queue task/supervised task/run/lease, clean tree, HEAD contained in origin/HEAD|origin/main|origin/master|main|master, linked worktree — primary checkout only with `close_clean_primary_checkouts`) and `ORPHAN_WORKTREE_MISSING` (cwd proven absent). Preserved with reason: PROTECTED, ATTACHED, SERVICE (any non-agent command: pnpm, cloudflared, ssh…), SHELL, ACTIVE_TASK, AGENT_RECENT, AGENT_NOT_IDLE, UNKNOWN_CWD, NO_REPO, DIRTY_WORKTREE, NO_BASE_REF, UNMERGED_BRANCH, IDLE_PRIMARY_CHECKOUT. Each close re-classifies fresh, runs `deletion_preflight`, saves scrollback to `~/tmux-reaped/`, deletes via `terminal_delete_session(confirm=True, requested_by="session-reconciler")`, unwatches supervisor, audits `reconcile_agent_session`. Idempotent; bounded by `max_closes_per_run`.
- Pane state, not tmux `activity_epoch`, is the real guard: Ink agent CLIs can show a 15h-old activity stamp while actively "Working".
- Config: `session_lifecycle.agent_cleanup` (`AgentCleanupConfig` in `config.py`; default disabled + dry_run). Loop started in `server_http.py` only when enabled.
- Inspection/actions: MCP tools `terminal_session_lifecycle(session=None)` (read-only classification) and `terminal_session_reconcile(dry_run=True, confirm=False)` (real pass needs `dry_run=false` + `confirm=true`); compact `terminal_turn` actions `session_lifecycle` (target -> session) and `session_reconcile` (args `dry_run`, `confirm`).
- Confirmation: help documents `args={"confirm": true}`; `CONFIRMATION_REQUIRED` now returns `next_action`; sidecar maps legacy `confirm` -> `args.confirm`; capacity reaper and `stale_sessions.cleanup_session` pass `confirm=True`. Confirmation never bypasses attached/lease/recovery/protected/active-work blockers.

### Verification
- `tests/test_session_lifecycle_reconciler.py` (18): clean completion closed, dirty/unmerged preserved, active/protected/attached/service/shell/recent/thinking preserved, orphan closed, primary-checkout opt-in, non-repo, dry run, idempotency, preflight refusal, per-run cap, config parsing, confirmation paths (core hint, reaper, sidecar, compact routing).
- Related suites (sidecar, direct contract, policy, direct task, session lifecycle/delete safety, compact, server, transports, observer, capacity, stale cleanup): all pass except 3 pre-existing failures identical on unmodified main 4a998d0 (`test_transports.py` x2 instructions text, `test_observer.py::test_observer_exposes_no_tool_that_can_change_anything` terminal_enqueue_task).

### Deployment + live verification (Dell, 2026-10-02)
- Commit `e3ab229` fast-forwarded to `main` and pushed. `terminal-mcp-http.service` restarted (KillMode=process; tmux server is NOT in its cgroup — it lives in `terminal-node-agent.service`'s cgroup, so the node agent was deliberately NOT restarted). `/version` = `e3ab229…`, `/health/ready` ready, tunnels + node agent active, all 15 sessions kept their created timestamps.
- Effective config is `~/.config/terminal-mcp/runtime-config.yaml` (systemd drop-in overrides repo `config.yaml`); backup `runtime-config.yaml.bak-20261002-lifecycle`. Added `session_lifecycle.agent_cleanup: {enabled: true, dry_run: false, idle_hours: 2, interval_seconds: 900, max_closes_per_run: 10, agent_commands: [claude, codex], close_clean_primary_checkouts: false}`. Journal: `agent session reconciler started (dry_run=False, idle_hours=2.0, interval=900.0s)`.
- Live MCP (`127.0.0.1:8766/mcp`): disposable `tmcp-confirm-verify2-1002` — unconfirmed delete -> `CONFIRMATION_REQUIRED` + `next_action`; attached + `args.confirm=true` -> `SESSION_ATTACHED`; detached + confirm -> `deleted: true`; repeat -> `already_gone`. `session_reconcile` without confirm -> `CONFIRMATION_REQUIRED`.
- Live dry run classified 15 sessions; only `nf68-refresh-0930` and `nf73-refresh-0930` (novafactory linked worktrees, clean, branch contained in origin/main) were `COMPLETED_CLEAN`. Real pass closed exactly those two (audit `reconcile_agent_session` actor `session-reconciler`; scrollback `~/tmux-reaped/2026-10-02T09380{4,5}-nf*-refresh-0930.log`); immediate re-run closed nothing. Their worktrees were left in place (novafactory project, not ours).
- Resources: sessions 15 -> 13; RAM used 22423 -> 21664 MB (available 8757 -> 9516 MB); swap used 15776 -> 15736 MB (the two claude trees were ~233 + ~207 MB RSS).

### Open follow-ups
- HP controller (`hp-net`, `/home/kimex/workspace/terminal-mcp`) still runs `f40e6a2`, 15 commits behind main (also missing queue retirement / direct-task lifecycle / Paperclip contract). Not deployed here: its tmux server is inside `terminal-mcp-http.service`'s cgroup (KillMode=process) with live sessions; needs a deliberate deploy decision. agent_cleanup defaults off there.
- Dell node agent process still runs pre-`e3ab229` code (only affects the `next_action` hint on node-agent-routed deletes); restart only when tmux cgroup placement is handled.
- Preserved-but-idle sessions worth a human look: `nf21-run-0930`, `nf32-run-0930`, `nf52-run-0930`, `offlinepos-disabled-controls-finish-1001` (IDLE_PRIMARY_CHECKOUT, clean/merged), `memory-knowledge-complete-claude-0928` (DIRTY_WORKTREE), `social-commerce-p0-1001` (NO_REPO). Set `close_clean_primary_checkouts: true` only if primary-checkout agents should also auto-close.
- Dashboard delete routes still don't check supervised tasks (from 2026-09-30).

## Hard lifecycle rule: RECOVERY_REQUIRED -> CLEANUP_ELIGIBLE — 2026-10-02 (`d847baa`)

### Rule (code-enforced, not an operational note)
No agent session may be retained indefinitely. `terminal_mcp/session_reconciler.py` layers a persisted lifecycle state machine over the existing `classify()`:
- `CONTROLLED`: PROTECTED, ATTACHED, SHELL (shell reaper governs), ACTIVE_TASK (queue task / supervised task / active run / lease-recovery), AGENT_RECENT (< `idle_hours`), AGENT_RUNNING (agent footer shows a turn in flight), REQUIRED_SERVICE (declared in `required_services` AND verified healthy: pane alive + optional `health_url` < 500), SERVICE_RECENT.
- `RECOVERY_REQUIRED`: uncontrolled/unbound/uninspectable/UNKNOWN — idle agent with no owner and unprovable completion (DIRTY_WORKTREE, UNMERGED_BRANCH, NO_REPO, NO_BASE_REF, UNKNOWN_CWD, AGENT_NOT_IDLE incl. UNKNOWN/WAITING_INPUT, IDLE_PRIMARY_CHECKOUT) or a service pane not verified-healthy-and-required (SERVICE). Each real pass: re-probe the pane (deeper capture when UNKNOWN), re-check ownership, checkpoint unique git work. Regained control (owner appears, attach, activity, pane output hash changes, becomes provably complete) -> CONTROLLED with `recovered_at`, timer reset.
- `CLEANUP_ELIGIBLE`: grace (`recovery_grace_minutes`, default 30, range 5..1440) expired without regained control, or provably complete (COMPLETED_CLEAN / ORPHAN_WORKTREE_MISSING, immediate as before). Closed on the next real pass via the guarded `terminal_delete_session(confirm=True)` after fresh re-classification, checkpoint, preflight and scrollback capture -> `CLOSED`.
- `BLOCKED` (fail closed): checkpoint of dirty/unmerged work failed (retried every pass; closes once it succeeds) or an interactive editor is open (EDITOR_OPEN).
- `GONE`: vanished on its own. CLOSED/GONE rows pruned after 14 days.
- Worst-case lifetime of an abandoned session = `idle_hours` + grace + one interval.

### Checkpoint (preserve unique work)
`checkpoint_git()`: temp `GIT_INDEX_FILE` (read-tree HEAD, `add -u`, add untracked files outside `REGENERABLE_DIRS` like .venv/node_modules/dist/caches) -> `commit-tree` on HEAD -> `refs/terminal-mcp/checkpoints/<session>/<stamp>` + `<scrollback_dir>/checkpoints/<stamp>-<session>.patch`; worktree and real index untouched; verified by rev-parse. Clean-but-unmerged: same ref pinned at HEAD. Only-regenerable dirt -> `kind: none` (no unique work). Untracked > `checkpoint_max_untracked_mb` (200) -> BLOCKED. Reused when content unchanged. Needs git >= 2.26 (`--pathspec-from-file`).

### State/exposure
- Store: `~/.local/state/terminal-mcp/session_lifecycle.db` (`LifecycleStore`, override `TERMINAL_MCP_SESSION_LIFECYCLE_DB`), keyed by session name + tmux identity (`session_id@created_epoch`) so a reused name starts fresh; timers survive restarts. Created lazily (import-time builds don't create it).
- `terminal_session_lifecycle` / compact `session_lifecycle`: per-row `lifecycle_state`, `action` (close/recover/preserve), `lifecycle` {state, reason, classification, first_uncontrolled_at, grace_expires_at, last_observed_at, recovery_attempts, last_recovery_at, recovered_at, closed_at, checkpoint}; top-level `lifecycle_states`, `close_candidates`, `recovery_required`, `blocked`. Read-only (no store writes).
- `terminal_session_reconcile` runs record observations/timers even in dry run (no checkpoints/closes). Real pass still needs `dry_run=false` + `confirm=true`.
- `terminal_list_sessions` (local-node rows) and compact `inspect` rows carry `lifecycle`.
- Config (`AgentCleanupConfig`): new `recovery_grace_minutes`, `checkpoint_dirty_work`, `checkpoint_max_untracked_mb`, `required_services` (pattern string or `{session, health_url}`; `RequiredServiceConfig`). Example in `config.example.yaml`.
- `classify_status` is now called with the reconciler clock (`now=`).

### Tests
`tests/test_session_lifecycle_reconciler.py` 31 passed: uncontrolled->recover->keep, pane-output reset, grace expiry->cleanup (NO_REPO + UNKNOWN pane), dirty->checkpoint->cleanup (worktree/index untouched, untracked captured, patch written, checkpoint reused), unmerged pinned->cleanup, checkpoint failure->BLOCKED->retry closes, regenerable-only dirt, active task exemption, required service exempt only while healthy, recent service/editor, repeated cleanup idempotency + name reuse, restart persistence, read-only inspect/dry run, compact inspect annotation, config validation. Related suites: 421 passed, 3 failed = the same pre-existing failures on main (`test_transports.py` x2, `test_observer.py::test_observer_exposes_no_tool_that_can_change_anything`).

### Deployment (Dell, 2026-10-02 11:01)
- `d847baa` fast-forwarded to `main` and pushed; `terminal-mcp-http` restarted (tmux verified in `terminal-node-agent.service` cgroup, KillMode=process). `/version` = d847baa, `/health/ready` ready, tunnels + node agent active, every session kept its created timestamp. Journal: reconciler started (dry_run=False, idle_hours=2, interval=900s).
- `~/.config/terminal-mcp/runtime-config.yaml` (backup `runtime-config.yaml.bak-20261002-recovery`): `recovery_grace_minutes: 30`, `checkpoint_dirty_work: true`, `checkpoint_max_untracked_mb: 200`, `required_services`: `novafactory-live-0930` (health_url `http://127.0.0.1:8100/`, verified 200) and `novafactory-named-tunnel2-0929` (cloudflared up 2.5 days). Remove them when that preview is no longer needed.
- Pre-deploy read-only preview of live sessions under the rule: RECOVERY_REQUIRED would be `chrome-audit-cleanup-1001` (codex UNKNOWN pane, cwd ~, 26h idle), `facebook-policyfix-check-1632` (ssh stuck at an interactive provider picker, 18h), `memory-knowledge-complete-claude-0928` (DIRTY only by untracked `.venv` -> no unique work), `nf21/nf32/nf52-run-0930`, `offlinepos-disabled-controls-finish-1001` (IDLE_PRIMARY_CHECKOUT), `social-commerce-p0-1001` (NO_REPO, cwd ~/workspace). CONTROLLED: `cdtm-shopee-api-audit-1001` (now RUNNING), the two novafactory services, the shell, the lifecycle-fix session.
- A manual confirmed real pass via MCP was denied by the agent permission classifier and NOT performed. Reconciliation of existing sessions is therefore left to the deployed loop: first pass ~11:17 starts timers (+ checkpoints), closes follow at the first pass after the 30-min grace (~11:47-12:02). NOT yet verified live — next agent: check `terminal_session_lifecycle`, `~/tmux-reaped/`, audit `reconcile_agent_session`, and `git for-each-ref refs/terminal-mcp` in affected repos.

### Open follow-ups
- Pre-existing, unrelated: fleet projector errors `node:node:local is owned by 'dell-linux', not 'local'` (~400/day in the journal since at least 00:02 today).
- HP controller still not deployed (see previous section); remote node agents not redeployed.
- Dashboard delete routes still don't check supervised tasks.


## Live Session Monitor (`/dashboard/live`) — 2026-10-02 (branch `feat/live-session-monitor-1002`, not merged/deployed)

### What / why
Direct dispatch (`create_session` -> `send`/`send_wait`) leaves no queue row, so no task board could show what ChatGPT is running. New top-level page **Đang chạy / Live Sessions** shows every session fleet-wide, active first, auto-refreshing every 2s without reload.

### Files
- `terminal_mcp/live_sessions.py` (new): `LiveSessionMonitor.snapshot(expand=(), include_previews=True)`. Read-only aggregation over existing sources only (no new DB): `controller.terminal_list_sessions` + per-session `controller.terminal_status` (remote rows called as `node/session`), Terminal Wall's `OutputChangeTracker`/`derive_state`, `AuditStore.latest_input_index` (direct sends + creations, local node only), `QueueStore.live_task_rows`, `server.direct_task_supervisor.store.active()` (read lazily), `RunJournalStore.active_session_index`, git via status `resource.git` or `session_resource.probe_git_state(cwd)` (local only, 10s cache). 1.5s TTL cache shared by all tabs; one tracker per monitor (`server.live_session_monitor`, shared by `/dashboard` and `/app`).
- State rules (`live_state`): WAITING/OFFLINE from wall rules; raw RUNNING from a non-shell foreground command/agent footer = RUNNING; a shell prompt/agent composer back and output still >= `SETTLE_SECONDS` (4s) = IDLE (so finished work leaves RUNNING within seconds, not the wall's 90s); witnessed output change <= 90s = RUNNING; delivered input <= `INPUT_ACTIVE_SECONDS` (45s) with no finished evidence = RUNNING (`activity_source=direct_input`). Open supervised/queue task keeps a row business-active. NEW = created <= 10 min ago or seen transitioning into activity (first build adopts existing activity as baseline). `completion` from a finished task, or from an observed active->inactive transition (last non-blank tail line).
- Per-row isolation: status exceptions -> that row OFFLINE with `error`; listing failure / audit / queue / direct-task / run-journal failures -> `source_errors`, never 5xx. Unreachable nodes -> `unreachable_nodes` banner. Unreadable sessions -> RESTRICTED without any pane read.
- Tail: the 20 lines `terminal_status` already carries; `?expand=node/session,...` (max 6) adds a 120-line `terminal_tail` for expanded cards only.
- `terminal_mcp/live_sessions_page.py` (new): `LIVE_SESSIONS_HTML`; filters Đang chạy/Gần đây/Idle/Tất cả + search + node select; repaints a card only when `change_token` changes; textContent only; mobile + 1366px grid.
- Routes: `dashboard.py` `/dashboard/live`, `/dashboard/api/live-sessions` (`_read_guard`; send preview only for operator/owner role — `text_preview` is SENSITIVE_METADATA). `webauth_dashboard.py` `/app/live`, `/app/api/live-sessions` (session cookie; same monitor). Nav: `dashboard_nav.py` primary item `live` and `app-live`.
- Store helpers (read-only): `AuditStore.latest_input_index(since)`, `QueueStore.live_task_rows(since)` (single bounded query; avoids `list_all_lanes` N+1 + `_ensure_lane` writes).

### Known limits
- Direct-send preview/last-input time exists only for sessions on the controller's own node (remote nodes audit their own sends); remote rows still go active from pane evidence.
- Observed while setting up a manual harness (abandoned, not a production path): `build_default_controller` with the host runtime config reported node `local` offline, so wall and live lists were empty there. Production `server_http` builds its own ControllerService; verify during the live canary.

### Verification
- `pytest tests/test_live_sessions.py` -> 17 passed (direct send without task, previews withheld, new session appears + sorts first, RUNNING -> IDLE with completion, queue + supervised task mapping, failing status/listing/sources isolation, restricted, expand, TTL cache, `/dashboard/live` + API, `/app/live` login/rewrite, CF Access 403).
- `pytest tests/test_global_nav.py tests/test_webauth_dashboard.py tests/test_terminal_wall.py` -> 482 passed (includes route coverage for the new page).
- No live/production canary yet: next step is merge + restart `terminal-mcp-http` and run the acceptance (open `/dashboard/live`, create session + direct send a multi-step command, watch it appear/RUN/IDLE without reload, queue disabled).

## Login origin hotfix — 2026-10-02
- Fixed password-login POST 403 behind : Cloudflare Tunnel public Origin must be present in  because the origin service may see a local Host.
- Production runtime config now allows  and ; wrong-credential probe changed from 403 (Origin rejected) to 401 (auth reached), confirming the CSRF gate is no longer blocking login.
-  password was reset via ; existing web sessions were invalidated.
- Targeted  + dashboard allowed-origins config test passed (exit 0).

## Login Fetch Metadata fallback — 2026-10-02
- Browser login attempts at 15:12–15:13 still returned 403 even after public origins were configured, proving Origin/Referer can be absent through this browser+tunnel path.
- Webauth CSRF guard now accepts missing Origin/Referer only when browser Fetch Metadata reports ; missing headers without that signal and cross-site requests remain blocked.
-  covers same-origin fallback and cross-site rejection; full file passes.

## Login CSRF double-submit token — 2026-10-02 (branch `hotfix/login-csrf-token-1002`, not merged/deployed)
- Why: public HTTPS `requests` login worked (POST /login -> 303, GET /app/live -> 200), but a real browser still got 403 on POST /login before credentials were checked. Its browser/tunnel path drops Origin, Referer and Sec-Fetch-Site together, so header-only CSRF cannot be relied on for login.
- `terminal_mcp/webauth_dashboard.py`: every rendered login form (GET /login, plus the 401/403/429 re-renders) mints `secrets.token_urlsafe(32)`. The token goes into hidden field `csrf_token` and into cookie `__Host-terminal_mcp_login_csrf` (Secure, HttpOnly, SameSite=Strict, Path=/, no Domain, 2h max-age). The `__Host-` prefix blocks cookie tossing from a subdomain, and it requires Path=/, which is why the cookie is not scoped to /login.
- POST /login parses the form first. It accepts when cookie == field (`secrets.compare_digest`) **or** the existing `_origin_allowed` rule passes (same-origin Origin/Referer, allowed origins, or `Sec-Fetch-Site: same-origin`). Otherwise it returns 403 with a fresh token. A matching pair is deliberately enough even when cross-site headers are present: a cross-site page can't read the token, and the browser won't attach a Strict cookie to a cross-site request. A successful login deletes the CSRF cookie.
- `_mutation_guard` and `_origin_allowed` are unchanged. /app mutations still need Origin, and the login token is never accepted for them (a test covers this).
- Tests: 8 new tests in `tests/test_webauth_dashboard.py`. Full file: 42 passed. Related suites (webauth, live_sessions, global_nav, notes_auth, output_redaction, observer, bootstrap ingress/setup) ran 273 passed, 1 failed. The failure is `test_observer.py::test_observer_exposes_no_tool_that_can_change_anything` (`terminal_enqueue_task` is missing from the full tool set). It fails the same way on base `f3f82a4`, so it predates this change and is unrelated. Run the tests with `/home/dell/workspace/terminal-mcp/.venv/bin/python -m pytest` because system python lacks starlette.
- Next: merge, deploy, then have the real user reload /login (the old cached form has no token and still depends on the header fallback) and confirm POST /login -> 303.

## Session current-task label ("Task hiện tại") — 2026-10-02 (branch `feat/session-task-label-1002`, not merged/deployed)

### What / why
`/dashboard/live` and `/app/live` showed only the last raw send preview, which after a "y"/"continue" no longer described the work, and a session re-used for a new task kept showing stale info. Every session now carries a persisted **current task label** shown prominently under its name.

### Storage (existing audit DB, no new service/DB)
- `audit.db` migration 5: table `session_task_labels(node_id, session, summary, source, task_id, request_key, updated_at)`, PK `(node_id, session)`, replaced in place. `AuditStore.set_task_label/get_task_label/delete_task_label/task_label_index`.
- Local sessions are keyed by `controller.local_node_id`; `node/session` targets by that node. The monitor also tries `local`/`""` aliases for local rows.

### Label rule (`terminal_mcp/task_labels.py`, `TaskLabeler`) — preserve
- Explicit `title` (or `metadata.task_summary`/`metadata.title`) on send / send_wait / supervise / create_session / enqueue / start always replaces the label (`source=title`).
- `create_session` with `initial_prompt` uses the prompt summary (`initial_prompt`). Without one, it **clears** any label a same-named earlier session left, so the UI shows "Chưa gắn task".
- Durable enqueue/start uses the title, else the prompt summary (`durable_task`, with `task_id`/`request_key`). `supervise` does the same (`supervised`, with `task_id`).
- An untitled send/send_wait **never replaces** an existing label. It may set the first label of an unlabeled session, but only when the text is substantial new work: not a continuation, at least 6 words and at least 40 chars after summarizing (`prompt_fallback`). Continuations include y/yes/continue/tiếp tục/approve/ok go ahead, and anything of 3 words or fewer and 24 chars or fewer.
- Labels are written only after acceptance (SUBMIT_CONFIRMED / task_id / created). Every label write is best-effort and can never fail the real action.
- IDLE never clears a label. `delete_session` (native + compact) deletes the row. The monitor ignores a label older than the session's creation by more than 5s (an orphan from a session deleted outside MCP).
- Summarizer (deterministic, no LLM): redact → strip fences/headings/bullets/emphasis/links → first meaningful line (skip a bare "# Task"/"Goal:" label) → first sentence if ≥12 chars → cap 140 chars on a word boundary with "…".

### Wiring
- `compact_tools.py`: `task_labels` attribute. send/send_wait label after confirmation (send_wait labels **before** the wait so the monitor updates while the turn runs). supervise labels. create_session with a title overrides the prompt summary. Responses gain an additive `current_task`, and `send_task` results gain `node_id`.
- `mcp_app.py`: builds `TaskLabeler(terminal.audit)`. Hooks are in `terminal_create_session`, `terminal_enqueue_task` (which covers compact start/enqueue/long_task, still queue-gated) and `terminal_delete_session`. Exposes `server.task_labels` / `server.compact_tools`. The `terminal_turn` docstring and action schema help tell callers to pass `title="<short summary>"` and omit it for continuations.
- `orchestration_policy.py` → v1.4.0 adds the TASK LABEL rule (and a critical invariant). `docs/CHATGPT_ORCHESTRATION_POLICY.md` was regenerated.
- `live_sessions.py`: each entry has `current_task {summary, summary_withheld, updated_at, source, task_id, request_key, origin: label|task}`. Fallback is the mapped supervised/queue/run task, else `null`. Prompt-derived summaries (and supervised-task titles) are withheld when `include_previews=False` (dashboard viewer role); `title`-sourced ones are always shown. Label read failures go to `source_errors`. `change_token` includes summary + updated_at.
- `live_sessions_page.py`: a "TASK HIỆN TẠI" block under the name row: 2-line clamp, age refreshed every poll, Vietnamese source + state, "Chưa gắn task" when empty. When the label changes between polls the block flashes amber with a "TASK MỚI" badge for about 4-6s. Search also matches the label.

### Verification
- `tests/test_session_task_label.py`: 28 passed (summarizer/continuation units, explicit title persist/replace, continuation does not replace, untitled fallback only on an unlabeled session, failed send, send_wait direct path, metadata alias, remote node key, store failure isolation, compact create title, supervise, native create initial_prompt + empty create clears + failed create, queue-opt-in enqueue, queue-disabled no label, help text, live payload/IDLE keeps label/change_token, durable fallback, label beats task, orphan ignored, preview withholding, remote + OFFLINE rows, source failure, old audit without support, page UI, migration idempotency).
- Related suites (live_sessions, compact tools/turn actions, orchestration policy, direct contract/task, schema, server, transports, observer, global nav, webauth, lifecycle reconciler, audit, sidecar, delete safety): 498 passed, 3 failed. The 3 failures are the known pre-existing ones (`test_transports.py` x2, `test_observer.py::test_observer_exposes_no_tool_that_can_change_anything`), and they fail identically on base `9385a17`.
- Dashboard/sidecar/contract suites: 928 passed. `test_dashboard.py::test_dashboard_mobile_batch_no_unexpected_route_changes` also failed on base because its route allowlist lacked the live-monitor routes from 21a5874. Fixed in the follow-up commit by adding `/dashboard/live` + `/dashboard/api/live-sessions`.
- Final focused run (`test_session_task_label.py`, `test_live_sessions.py`, the route-allowlist test, `test_global_nav.py`, `test_webauth_dashboard.py`, `test_orchestration_policy.py`, `test_compact_tools.py`): **169 passed, 0 failed**. `test_dashboard_nodes.py::test_nodes_list_shows_local_node_online_after_a_get` is a host-load flake (`capacity_status` busy at load ~20). The file passed 30/30 on this branch twice at lower load.
- Not deployed and no live canary. Next: merge, restart `terminal-mcp-http`, then from a fresh ChatGPT chat `create_session` + `send_wait(title=...)` and watch the card relabel within about 2s. A follow-up send of "continue" must leave the label unchanged.

### Known limits / follow-ups
- Remote node-agents don't derive labels themselves. The controller that routes the send writes its central copy (keyed by the node it returned) and, since 2026-10-03, mirrors it to the owning node (see "Node-local task label mirror" below). Sends issued directly on a remote node's own MCP are labeled only by that node's own controller.
- Native `terminal_send_text` / dashboard sends have no `title` parameter. Only `terminal_turn` send/send_wait/supervise/create and native create/enqueue label.

## Session task label compatibility hotfix — 2026-10-03
- Production canary on 7084d54 showed intermittent loss of a new task label when callers supplied only title: task 1 persisted, task 2 could execute without updating session_task_labels. Persistence/upsert itself is correct.
- Sending the same summary in both title and metadata.task_summary updated labels reliably on back-to-back calls; an untitled continue preserved the last label.
- Contract/tool guidance now requires both fields for assignment/change and neither for continuations. metadata.task_summary remains a first-class alias, providing compatibility with cached clients that may omit/drop title.
- Regression coverage verifies metadata-only replacement and continuation preservation. Focused suite: 46 passed (tests/test_session_task_label.py tests/test_live_sessions.py).

## Node-local task label mirror — 2026-10-03 (branch `hotfix/session-task-node-sync-1003`, not merged/deployed)

### Why (production evidence)
- Dell runs its own controller + web UI (`node_id` "local", https://terminal-login.mesflow.net/app/live) AND `terminal-node-agent --node-id dell-linux --controller-url http://100.67.53.117:8766` registered with the HP controller.
- ChatGPT work assigned through the HP controller wrote `session_task_labels` only into HP's AuditStore. Dell's `/app/live` reads Dell's AuditStore, so it never showed the label. A controller-local row is not enough. The label must also live on the node that owns the tmux session.

### Data flow (preserve)
- `TaskLabeler._write` / `clear` (task_labels.py) is the single choke point. It writes the central row as before and then calls `node_sync.terminal_set_task_label(target, summary=, source=, task_id=, request_key=)` / `terminal_clear_task_label(target)`. `node_sync` is the Controller, injected in `mcp_app.py`. Every labeling path inherits the mirror: compact send/send_wait/supervise/create(title), native create_session (initial_prompt or the clear on an empty create), enqueue/start, delete. Continuations never write, so they never sync.
- Target: `node/session` when the node is known (send/create results carry the resolved `node_id`), else the bare name, which the controller resolves fleet-wide via `resolve_session`.
- `Controller.terminal_set_task_label/terminal_clear_task_label` -> `_route` -> `NodeClient.set_task_label/clear_task_label`, with a 3s timeout (`TASK_LABEL_SYNC_TIMEOUT_SECONDS`). It never raises. Offline node, old agent (404), or a client without the method all come back as `{"error": ...}`, and the labeler ignores the result. The central row is written first and is never replaced.
- `RemoteNodeClient` -> node-agent `POST|DELETE /v1/sessions/{name}/task-label` (bearer auth + throttle like every route). Body is `summary/source/task_id/request_key` only. `validate_label_payload` checks the session name, raw summary <= 2000 chars re-normalized to <= 140 and redacted, source must be a known source, and ids <= 200 chars. Extra fields are ignored. Validation errors come back as 200 `{"error": ...}` like other agent app errors. Non-object JSON gets a 400.
- `LocalNodeClient` writes directly to `TerminalService.audit`.
- Node copies are keyed `node_id="local"` (`NODE_LOCAL_LABEL_ID`). Every LiveSessionMonitor already folds "local" into its own local sessions whatever its canonical `local_node_id` is. The configured fleet id (e.g. `dell-linux`) is deliberately NOT written as a second row: it adds nothing a local monitor reads and would be one more row that can go stale.
- `LiveSessionMonitor._current_task`: for a local session it now picks the NEWEST of the exact row and the `local`/canonical aliases (before, the exact row won). So a fresh mirror from another controller is never shadowed by an older own-controller row. On HP (`hp-linux`), a local session routed through HP gets both `(hp-linux, s)` and `(local, s)` rows with the same content, and the UI shows one label.
- Node-agent `DELETE /v1/sessions/{name}` and `POST .../kill` clear the node-local label after a successful result, best-effort. Orphans are already ignored by the monitor's label-older-than-session rule.
- No new service, DB, or migration.

### Deploy notes
- Remote agents running an older build answer 404 on the new route. That is swallowed, so the mirror only starts working on a node once its node-agent is redeployed. Dell needs BOTH `terminal-mcp-http` (controller/UI monitor change) and the `dell-linux` node-agent restarted. HP's controller needs the new `node_client`/`controller` code to start sending.

### Verification (2026-10-03)
- `tests/test_session_task_label_node_sync.py` (new) -> 31 passed. Covers remote titled send (central `dell-linux` row + node `local` row via a real in-process node agent, 3s timeout), qualified target, local send, replacement on the node, continuation with no sync, NodeClientError/RuntimeError/offline/404 swallowed with the send still SUBMIT_CONFIRMED, compact and native create sync, supervise with bare-target resolution, clear mirror, endpoint auth/validation/replace/redaction/extra-field ignore, delete+kill clearing, monitor on the node store with ui id `local` and a canonical id, and newest-row-wins.
- `tests/test_session_task_label.py tests/test_live_sessions.py` -> 46 passed (unchanged).
- `test_node_agent.py test_node_client_permissions.py test_controller.py test_compact_tools.py test_compact_turn_actions.py test_compact_delete_confirmation.py test_mcp_app_wiring.py test_observer.py test_webauth_dashboard.py` -> 299 passed, 1 failed: `test_observer.py::test_observer_exposes_no_tool_that_can_change_anything`, which fails identically on base d0560a0 (pre-existing, unrelated).
- Next: merge, redeploy the HP controller + Dell `terminal-mcp-http` + the `dell-linux` (and other) node-agents, then assign a titled task via HP to a Dell session and confirm Dell `/app/live` shows it.

## 2026-10-05 — Session ownership / resurrection hotfix deployed

### Root cause
- `resolve_session` could lose a session's routing after the short in-memory location cache expired when a node heartbeat/list probe was transiently unavailable. The session runtime could still exist, but later `inspect/send/delete` calls returned `SESSION_LOCATION_UNKNOWN`.
- Session existence/ownership was being inferred from multiple partially independent sources (live node listing, cache, registry/fleet state). An incomplete probe could therefore erase routing knowledge even though there was prior positive ownership evidence.
- Deleted/missing sessions also needed generation/tombstone protection so stale inventories could not make an old runtime look live again.
- User session capacity could saturate normal slots and block repair/maintenance work.

### Deployed fix
- Main commit `29cd75d777e035717a7bed9103b62c6865f81f4f` (`fix(controller): durable session ownership; never lose a known owner`).
- Session create now records authoritative durable ownership before success is returned: owner node, generation and runtime instance id.
- Routing falls back to durable positive ownership. If the known owner cannot currently be probed, the result is `NODE_UNREACHABLE` / known-owner-unreachable, not `SESSION_LOCATION_UNKNOWN`.
- Only an authoritative successful probe of the known owner can transition a runtime to missing.
- Delete/kill records tombstone/generation state; stale observations from an older generation cannot resurrect a deleted runtime.
- Same-name recreation is treated as a new generation / instance.
- Controller/node-agent restart reconstructs routing from durable state.
- Maintenance-prefixed sessions can use reserved control capacity even when normal user-session capacity is full; this is separate from raising the normal global limit.

### Verification
- Focused final-tree regression gate: **333 passed**.
- Production live acceptance log: **21 PASS, 0 FAIL — `SUMMARY ALL PASS (21 checks)`**.
- Verified live cases include:
  - create -> cache TTL expiry (>20 s) -> inspect/send/list;
  - controller restart and node-agent restart while tmux survives;
  - temporary owner-node unavailability returns `NODE_UNREACHABLE`, never location loss;
  - explicit delete/tombstone remains deleted through reconcile/restart;
  - same-name recreation routes as a new generation/instance;
  - maintenance reserve can create control sessions above normal capacity;
  - existing historically resurrecting CDTM sessions remained absent after deploy.
- During acceptance the controller was intentionally restarted and recovered; sessions became routable again from durable ownership.
- `origin/main` was pushed to `29cd75d777e035717a7bed9103b62c6865f81f4f` before live acceptance.

### Operational state / cleanup
- Dell controller/node-agent/http services were restarted as part of live acceptance.
- Canary runtimes were deleted/made missing as expected after the acceptance sequence.
- Remaining task after this note: remove the merged hotfix worktree/branch and temporary maintenance inspection session; do not remove unrelated legacy worktrees/sessions.
