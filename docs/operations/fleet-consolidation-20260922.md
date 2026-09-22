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

64 merged worktrees were removed across Dell, HP, and M910 after checking active process directories. Worktrees containing uncommitted material were archived and their file contents verified before removal. Active runtime checkouts and any worktree still under review are retained.

## M910 recovery and additional source

M910 became reachable during final validation. Its enabled user node-agent automatically connected after boot. Its separate WorkStore opt-in analysis contract and watch persistence tests were recovered under `work_analysis_gate.py`; canonical queue analysis profiles remain unchanged. The explicitly parked composer experiment remains archival; its old code is not deployed over the newer composer implementation.

## Deployment verification

Release validation and service switch results are recorded after the final integration checks. Database migration was tested on copies: HP task counts were preserved and SQLite integrity checks passed. Service restarts must preserve existing tmux session identities and use a process-only kill policy.
