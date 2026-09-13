# Decisions

Recorded so a later agent does not quietly undo them.

- **Work mode is opt-in by session name.** `-work` suffix, decided in ONE
  place. An ordinary session must keep its exact previous behaviour.
- **Evidence over assertion.** The Terminal Wall shows RUNNING only on a
  witnessed output change. Progress comes from task weights and real queue
  state, never from a percentage a model wrote about itself.
- **Confidence is derived, never stored.** Three coarse words, not a
  percentage: a number would imply a measurement nobody took.
- **The working tree outranks the commit graph.** A module with uncommitted
  edits cannot claim HIGH confidence, because the file on disk is what runs.
- **Secrets are REFUSED, not stripped.** `scrub_knowledge` raises. A knowledge
  base is long-lived and rarely audited -- the worst place for a quiet strip
  to fail open.
- **Fail closed on a non-git directory.** The coordinator refuses rather than
  loosening the check.
- **The registry owns commands; specs reference runbooks by ID.** Copying a
  command into a spec lets the two diverge silently.
- **Budgets never hard-stop mid-task.** An abandoned task produces a guess,
  which costs more than the reading it avoided.
- **Never fabricate precision.** An unavailable token count is reported as
  unavailable; a partial total is labelled PARTIAL, not EXACT.
