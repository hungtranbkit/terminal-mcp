#!/usr/bin/env bash
# Is the controller up, and did it log anything it should not have?
# Read-only: it inspects, it never restarts or changes anything.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
STAGE="service"
ACTIVE="$(systemctl --user show terminal-mcp-http.service -p ActiveState --value 2>/dev/null || echo unknown)"
PID="$(systemctl --user show terminal-mcp-http.service -p MainPID --value 2>/dev/null || echo 0)"
[ "$ACTIVE" = "active" ] || fail "summary=\"controller is $ACTIVE\""
STAGE="exceptions"
SINCE="$(date -u -d '5 minutes ago' '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo '')"
COUNT="$(journalctl --user -u terminal-mcp-http.service --since "$SINCE" 2>/dev/null \
         | grep -ciE 'traceback|"level": "CRITICAL"' || true)"
[ "${COUNT:-0}" -eq 0 ] || fail "summary=\"$COUNT exception(s) in the last 5 minutes\""
pass "summary=\"active pid=$PID, no exceptions in 5m\""
