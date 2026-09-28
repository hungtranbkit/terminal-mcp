# Compact queue resume — 2026-09-28

Adds an explicit operator-requested queue_resume action to terminal_turn. It routes to the existing terminal_queue_resume handler and requires the normal session input authorization before restoring a lane. It does not approve risk, change task prompts, bypass the coordinator, or alter dashboard/Cloudflare authentication.

Regression evidence: 5 new routing/input-validation tests failed before implementation; 2 additional native authorization cases failed before the session-input guard. All 326 selected compact/coordinator/recovery tests passed after both changes. Logs: /tmp/memory-compact-resume-red-0928.log, /tmp/memory-compact-resume-auth-red-0928.log, /tmp/memory-resume-final-0928.log.

Review: direct source review of the wrapper, handler registration, input guard and unchanged QueueService.resume. The attempted independent-review/full-suite command was blocked before execution; neither is claimed complete.

Operational use after loading this commit: terminal_turn(action=queue_resume, target=the authorized session). This restores prior status; normal dispatch checks still decide whether the task may run. It does not mark business work complete.
