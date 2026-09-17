#!/usr/bin/env bash
# Route smoke against the running controller. 403 on loopback is CORRECT --
# it means the route exists and the Cloudflare Access gate is enforcing.
# A 404 means the route is missing; a 000 means nothing is listening.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
STAGE="routes"
BASE="${SMOKE_BASE:-http://127.0.0.1:8766}"
ROUTES="${SMOKE_ROUTES:-/dashboard /dashboard/work /dashboard/api/work /dashboard/api/work/workers /dashboard/terminal-wall /dashboard/fleet /dashboard/audit}"
BAD=""
for route in $ROUTES; do
  CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$BASE$route" || echo 000)"
  case "$CODE" in
    200|302|403) ;;
    *) BAD="$BAD $route=$CODE" ;;
  esac
done
[ -z "$BAD" ] || fail "summary=\"unexpected status:$BAD\""
pass "summary=\"$(echo $ROUTES | wc -w) route(s) reachable and gated\""
