# Codex startup readiness fix

## Goal

Create Codex sessions that are task-ready only after a stable composer is visible. Keep tool permissions separate and never change shared daemon settings or trust a folder automatically.

## Steps

1. Reproduce the incompatible daemon menu and exercise Codex CLI feature detection for versions with and without `--no-daemon`.
2. Pass the supported per-process flag before any resume arguments; retain a bounded exact-menu fallback that selects only “Run without daemon this time.”
3. Verify trust prompts remain blocked, first-turn dispatch remains submit/ack gated, and generic `FINISHED` text cannot complete a supervised task.
4. Run focused and full applicable tests, then a controlled live create/first-task smoke where the runtime is isolated and authorized.
5. Update project context and publish a focused PR. Deploy only if runtime topology and safe rollout are verified.
