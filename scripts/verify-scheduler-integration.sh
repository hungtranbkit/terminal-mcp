#!/usr/bin/env bash
# Scheduler fix (blg_8d65afc1b38b) -- integration / staging verification.
#
# Run this the moment phase 2 lands. It is safe to run BEFORE phase 2 too:
# the phase-1 half verifies, and the phase-2 checks report as skipped with
# the missing symbol named, which is the integration gap list.
#
# SAFETY
#   - never touches production (8766). It reads supervisor_status there to
#     capture "before" evidence and nothing else.
#   - staging (8777) is loopback-only and has its own state directory.
#   - no deploy, no restart of the production service.
#
# Usage
#   scripts/verify-scheduler-integration.sh              # tests + staging probes
#   scripts/verify-scheduler-integration.sh --tests-only # no staging needed
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PY:-$REPO/.venv/bin/python}"
STAGING="${STAGING:-http://127.0.0.1:8777}"
PROD="${PROD:-http://127.0.0.1:8766}"
TESTS_ONLY="${1:-}"

pass=0; fail=0; skip=0
ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail=$((fail+1)); }
note() { printf '  \033[33mSKIP\033[0m %s\n' "$1"; skip=$((skip+1)); }
head_() { printf '\n\033[36m== %s ==\033[0m\n' "$1"; }

# --------------------------------------------------------------------------
head_ "0. what is under test"
# --------------------------------------------------------------------------
echo "  repo   : $REPO"
echo "  commit : $(git -C "$REPO" rev-parse --short HEAD) ($(git -C "$REPO" rev-parse --abbrev-ref HEAD))"
echo "  phase 2 symbols:"
"$PY" - <<'PY'
import importlib
module = importlib.import_module("terminal_mcp.scheduler_health")
for name in ("discover_workers", "dispatch_refill", "claim_task", "scheduler_status"):
    print(f"    {name:18} {'present' if hasattr(module, name) else 'MISSING (phase 2 not landed)'}")
PY

# --------------------------------------------------------------------------
head_ "1. focused test suites"
# --------------------------------------------------------------------------
run_suite() {
  local label="$1"; shift
  if "$PY" -m pytest "$@" -q -p no:randomly >/tmp/sched-verify-$$.log 2>&1; then
    ok "$label -- $(tail -1 /tmp/sched-verify-$$.log | tr -d '\n')"
  else
    bad "$label"; tail -15 /tmp/sched-verify-$$.log | sed 's/^/       /'
  fi
}
run_suite "scheduler_health (pure model)"      "$REPO/tests/test_scheduler_health.py"
run_suite "supervisor reconcile (real tmux)"   "$REPO/tests/test_supervisor_reconcile.py"
run_suite "integration contract (8 checks)"    "$REPO/tests/test_scheduler_integration.py"
run_suite "supervisor regression"              "$REPO/tests/test_supervisor.py" "$REPO/tests/test_supervisor_v2.py"
run_suite "queue/lease regression"             "$REPO/tests/test_queue_engine.py" "$REPO/tests/test_queue_loop.py" \
                                               "$REPO/tests/test_lease.py" "$REPO/tests/test_task_lease.py"

echo
echo "  integration gaps still open (phase-2 symbols not yet present):"
"$PY" -m pytest "$REPO/tests/test_scheduler_integration.py" -q -p no:randomly -rs 2>/dev/null \
  | grep -o "phase 2 not landed: [^ ]*" | sort -u | sed 's/^/    - /' || echo "    (none -- phase 2 fully landed)"

[ "$TESTS_ONLY" = "--tests-only" ] && { printf '\n%d passed, %d failed, %d skipped\n' "$pass" "$fail" "$skip"; exit $((fail>0)); }

# --------------------------------------------------------------------------
head_ "2. production BEFORE evidence (read-only)"
# --------------------------------------------------------------------------
mcp() {  # mcp <base> <tool> <json-args>
  local base="$1" tool="$2" args="${3:-{\}}" hdr
  hdr=$(mktemp)
  curl -s -m 10 -X POST "$base/mcp" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' -D "$hdr" -o /dev/null \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify","version":"1"}}}'
  local sid; sid=$(grep -i 'mcp-session-id' "$hdr" | tr -d '\r' | awk '{print $2}'); rm -f "$hdr"
  [ -z "$sid" ] && return 1
  curl -s -m 8 -X POST "$base/mcp" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' -H "mcp-session-id: $sid" \
    -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' -o /dev/null
  curl -s -m 20 -X POST "$base/mcp" -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' -H "mcp-session-id: $sid" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"$tool\",\"arguments\":$args}}" \
  | sed 's/^data: //' | "$PY" -c 'import json,sys
for line in sys.stdin:
    line=line.strip()
    if line.startswith("{"):
        print(json.loads(json.loads(line)["result"]["content"][0]["text"]) if line else ""); break' 2>/dev/null
}

if prod_status=$(mcp "$PROD" supervisor_status); then
  echo "  $prod_status" | tr ',' '\n' | grep -E "watch_count|enabled_watch_count|stalled_count" | sed 's/^/    /'
  ok "captured production before-state (not modified)"
else
  note "production MCP not reachable -- before-state not captured"
fi

# --------------------------------------------------------------------------
head_ "3. staging checks (8777)"
# --------------------------------------------------------------------------
if ! curl -s -m 5 -o /dev/null "$STAGING/health/live"; then
  note "staging not listening on 8777 -- start it: ~/terminal-mcp-staging/run-staging.sh"
else
  ok "staging /health/live reachable"
  ready=$(curl -s -m 8 "$STAGING/health/ready" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["status"])' 2>/dev/null)
  [ "$ready" = "ready" ] && ok "staging /health/ready = ready" || bad "staging readiness = ${ready:-unreachable}"

  if stg_status=$(mcp "$STAGING" supervisor_status); then
    ok "staging supervisor_status responds"
    echo "  $stg_status" | tr ',' '\n' \
      | grep -E "watch_count|enabled_watch_count|recoverable_disabled_count|intentionally_excluded_count" \
      | sed 's/^/    /'
    case "$stg_status" in
      *recoverable_disabled_count*) ok "phase-1 status fields present on the deployed build" ;;
      *) bad "phase-1 status fields MISSING -- staging is running an older commit" ;;
    esac
  else
    note "staging MCP not reachable"
  fi
fi

printf '\n\033[36m== summary ==\033[0m\n'
printf '  %d passed, %d failed, %d skipped\n' "$pass" "$fail" "$skip"
[ "$fail" -gt 0 ] && exit 1
exit 0
