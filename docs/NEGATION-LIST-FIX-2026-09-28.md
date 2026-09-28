# Coordinator negation-list repair — 2026-09-28

## Scope and state
Source repair on fix/negation-list-0928, based on e046981. Not deployed. No shared-branch update, service restart, production database mutation, or task approval override performed. The existing Memory task has not been dispatched by this repair.

## Root causes and changes
The old helper split at commas and had no Vietnamese prohibition support. It also let some new imperative clauses inherit an earlier English negator. The replacement recognizes immediate Vietnamese prohibitions and bounded comma-separated action lists. List inheritance requires a prohibition at the beginning of the first entry (optional bullet), known action heads, and connector-only text before the current action. Sentence punctuation and new-instruction/contrast markers stop inheritance. Unclear cases still require human review. This heuristic does not authorize any operation.

## Verification
Baseline: tests/test_coordinator.py -> 52 passed.
First regression run: 11 failed and 14 passed, demonstrating the original bugs before implementation.
Independent read-only Claude Opus review found one Important issue: an unanchored list head could use a reported prohibition to hide a later real command. Two reproducer tests failed before the fix; anchoring the list head fixed them.
Final command: PYTHONPATH=. /home/dell/workspace/terminal-mcp/.venv/bin/python -m pytest tests/test_coordinator*.py tests/test_stale_admission_recovery.py -q --tb=short
Final result: 190 passed in 3.73s, exit 0. There are 27 new regression cases. git diff --check passed.
Whole-repository pytest was attempted on the first revision with a 180-second execution limit; it stopped with exit 124 before completion. It is not evidence of a fully passing suite, and the final revision still needs whole-suite validation.

## Recovery and release boundary
The existing automatic recovery handles certain resolved preflight conditions, not arbitrary sensitive-action pauses. This patch does not broaden that recovery rule or clear approvals in the queue database. A supported, auditable task review/resume is still needed after the repaired coordinator is integrated and loaded. Preserve the Memory scope: isolated development/testing/local commits, with main and production unchanged unless separately authorized.

## Evidence files on Dell
/tmp/tmcp-negation-baseline-0928.log
/tmp/tmcp-negation-red-0928.log
/tmp/tmcp-negation-review-0928.log
/tmp/tmcp-negation-review-red-0928.log
/tmp/tmcp-negation-final-0928.log
/tmp/tmcp-negation-full-0928.log
