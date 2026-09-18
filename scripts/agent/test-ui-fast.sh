#!/usr/bin/env bash
# Dependency-aware fast gate for a UI-only change: the dashboard template
# guards, the Work UI suite, and the mobile/rendering checks. Deliberately
# NOT the full suite -- a CSS fix does not need the queue engine re-proven.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
cd "$ROOT"
STAGE="ui-tests"
LOG="$(log_file test-ui-fast)"
if .venv/bin/python -m pytest -q \
      tests/test_work_ui.py \
      tests/test_backlog_panel.py \
      tests/test_dashboard_mobile_portrait.py \
      >"$LOG" 2>&1; then
  pass "summary=\"$(tail -n 3 "$LOG" | grep -E 'passed|failed' | tail -1)\" log=$LOG"
fi
SUMMARY="$(grep -E '^FAILED|^ERROR' "$LOG" | head -5 | tr '\n' ';')"
fail "summary=\"${SUMMARY:-see log}\" log=$LOG"
