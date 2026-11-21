# Regression classification — 2026-11-08

The feature worktree completed the full suite on HP with `TMUX_ONLY` left as
the default: **9,675 passed, 44 failed, 122 skipped, 103 deselected** in
1,285.22 seconds. The 44 failures are not introduced by this branch:

- the nine `test_backlog_brief.py` and eleven
  `test_fleet_registry_controller.py` failures reproduce on detached clean
  `main` (`20 failed, 56 passed` for the identical three-module comparison);
- all remaining 24 failures reproduce on detached clean `main` in the same
  venv and host (`24 failed, 142 passed, 5 skipped`) across recovery, rename,
  session knowledge/registry, and Windows onboarding modules.

The branch originally added three knowledge-index failures. Adding the two
new modules to the index and regenerating its state removed those regressions;
the three targeted efficiency tests then passed. This evidence does not turn
the pre-existing failures into passes and does not satisfy the promotion gate.
