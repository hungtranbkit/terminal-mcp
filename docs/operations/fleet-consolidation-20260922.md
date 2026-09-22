# Fleet consolidation — 2026-09-22

This release consolidates the unique local Dell, HP, and origin branch histories on top of HP main `2d8082f`. Patch-equivalent branch tips were recorded only after `git cherry` showed no unique patch. Existing branch refs remain available.

## Reconciled behavior

- Active queue reservations are recovered across all lanes, including disabled auto-dispatch lanes. Missing sessions release stale capacity after the configured timeout; repeatedly idle sessions require a stable observation window. Running or uncertain sessions, valid completion evidence, and live verifier claims remain protected. Recovery never kills an active terminal or silently enables dispatch.
- The compact terminal interface, orchestration bootstrap, analysis gate, harness queue projection, project workflows, callback recovery, fleet identity, and node-aware supervisor share their canonical implementations.
- Backlog project selection uses `/dashboard/api/backlog/projects`; `/dashboard/api/projects` retains the runtime project contract.
- Observer OAuth and shared repository tooling, the opt-in streaming controller proxy, release manifests, registry cleanup, and protected installer token files include recovered uncommitted work.
- Integration tests use a private tmux socket per pytest process, isolated from attended sessions and other test workers.
- Generic unknown programs cannot confirm a submission solely from echoed input. Known shells retain foreground-identity-guarded Enter confirmation.

## Preserved material

The old SQLite-only project knowledge prototype is superseded by the canonical Markdown knowledge map and was archived rather than replacing it. Temporary pane-content debug prints from the registry hygiene worktree were archived and excluded from deployment.

Pre-consolidation patches, untracked source, and worktree archives are in `/home/dell/workspace/tmcp-consolidation-backup-20260922`. Live SQLite databases were backed up with SQLite's backup API on each host. HP backups are in `/home/kimex/.local/share/terminal-mcp-backups/20260922-unified`.

76 merged worktrees were removed across Dell, HP, and M910 after checking active process directories. Worktrees containing uncommitted material were archived and their file contents verified before removal. Each Linux host now retains only its active main checkout; branch refs and archives remain available.

## M910 recovery and additional source

M910 became reachable during final validation. Its enabled user node-agent automatically connected after boot. Its separate WorkStore opt-in analysis contract and watch persistence tests were recovered under `work_analysis_gate.py`; canonical queue analysis profiles remain unchanged. The explicitly parked composer experiment remains archival; its old code is not deployed over the newer composer implementation.

## Validation and deployment

- Full default suite: 8,935 passed, 114 skipped, two outdated doctor fixtures failed. After reconciling those fixtures with observed socket evidence, all 109 doctor/network tests passed. The skipped cases require optional live platforms, toolchains, or explicitly enabled live tests.
- Subsequent M910 source recovery: 237 work runtime, checkpoint, analysis gate, and watch persistence tests passed. Knowledge index checks: 41 passed.
- Release code `ece6654` deployed on HP, Dell, and M910. All three nodes subsequently reported fresh heartbeats and `EXECUTION_OK`, with no pending retry or probe error.
- HP and Dell controller readiness returned HTTP 200. Dell and M910 node health returned HTTP 200. Dell observer OAuth discovery returned HTTP 200.
- Existing tmux session identities were preserved: HP 1/1, Dell 3/3, M910 0/0. Services are enabled with user lingering; process-only restart policy preserves terminal sessions.
- Four old HP RUNNING/VERIFYING tasks emitted `STALE_ACTIVE_RECOVERED` and released admission. Subsequent repository prechecks correctly paused or queued work with an invalid working directory or uncommitted changes; no bypass of those gates was enabled.
- M910's preserved runtime config had a legacy 1.5-second watchdog sweep interval. It was migrated to the required 3-second minimum and validated before restarting. Dell and M910 runtime configs now live outside the source checkout under `~/.config/terminal-mcp/runtime-config.yaml`; HP retains its existing external config.

SQLite migration was tested on copies before deployment, preserving task counts and passing integrity checks. Fresh predeployment databases, old commit IDs, checkout/install logs, and tmux identities remain in each host's protected backup directory. Predeployment uncommitted source is also retained in named Git stashes.
