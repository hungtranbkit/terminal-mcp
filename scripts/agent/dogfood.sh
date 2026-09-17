#!/usr/bin/env bash
# The dogfood gate: does this system's own contract constrain a real run?
#
# Two files, one question each. `test_dogfood_work_v1.py` plans a real bug and
# a real feature of this repository through the shipped pipeline;
# `test_dogfood_budget_gate.py` then drives a worker through the budget and
# the gate with real file reads and real `git grep`, and asserts it is stopped
# at the declared limits. Neither uses a fixture repository -- a dogfood over
# a convenient fake proves the fake.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
cd "$ROOT"

STAGE="interpreter"
# A git worktree has no .venv of its own, and a worktree is exactly where you
# want to run this. Fall back to the main checkout's venv for the SAME repo --
# `--git-common-dir` is what every worktree shares -- before giving up on a
# bare python3, which will not have pytest.
PY="${TERMINAL_MCP_PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  COMMON="$(git -C "$ROOT" rev-parse --git-common-dir 2>/dev/null || true)"
  [ -n "$COMMON" ] && PY="$(cd "$(dirname "$COMMON")" && pwd)/.venv/bin/python"
fi
[ -x "$PY" ] || PY="$(command -v python3 || true)"
[ -x "$PY" ] || fail "error=no python interpreter with pytest found"
# PYTHONPATH pins the package to THIS checkout, so the dogfood measures the
# code in front of you rather than whichever one owns the interpreter.
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

STAGE="dogfood"
LOG="$(log_file dogfood)"
if "$PY" -m pytest -q \
      tests/test_dogfood_work_v1.py \
      tests/test_dogfood_budget_gate.py \
      >"$LOG" 2>&1; then
  pass "summary=\"$(grep -E 'passed|failed' "$LOG" | tail -1 || true)\" log=$LOG"
fi
# Only the failing lines reach the caller; the rest stays in the log.
SUMMARY="$(grep -E '^FAILED|^ERROR' "$LOG" | head -5 | tr '\n' ';' || true)"
fail "summary=\"${SUMMARY:-see log}\" log=$LOG"
