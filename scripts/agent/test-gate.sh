#!/usr/bin/env bash
# Full regression. The gate a NORMAL or SAFE change passes before release.
# Verified procedure: this is the invocation used throughout this project.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
cd "$ROOT"
STAGE="pytest"
LOG="$(log_file test-gate)"
if .venv/bin/python -m pytest -q >"$LOG" 2>&1; then
  pass "summary=\"$(tail -n 3 "$LOG" | grep -E 'passed|failed' | tail -1)\" log=$LOG"
fi
# Only the failing lines reach the caller; the rest stays in the log.
SUMMARY="$(grep -E '^FAILED|^ERROR' "$LOG" | head -5 | tr '\n' ';')"
fail "summary=\"${SUMMARY:-see log}\" log=$LOG"
