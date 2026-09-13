#!/usr/bin/env bash
# Restart the controller to activate new code, then verify.
#
# RISK: this box has ONE controller. There is no separate preview
# environment, so this restart touches what everyone is using. It is
# registered at staging risk and is never invoked automatically by a
# classifier -- calling it is a decision, and this script only makes that
# decision repeatable rather than automatic.
#
# It does NOT touch node agents, does not reboot, and cannot reach a tmux
# session: the unit is KillMode=process with only the controller in its
# cgroup.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
cd "$ROOT"
STAGE="baseline"
BEFORE="$(tmux list-panes -a -F '#{session_name}:#{pane_pid}' 2>/dev/null | sort || true)"
STAGE="restart"
systemctl --user restart terminal-mcp-http.service || fail "summary=\"restart failed\""
sleep 8
STAGE="verify"
bash "$ROOT/scripts/agent/healthcheck.sh" >/dev/null || fail "summary=\"unhealthy after restart\""
AFTER="$(tmux list-panes -a -F '#{session_name}:#{pane_pid}' 2>/dev/null | sort || true)"
[ "$BEFORE" = "$AFTER" ] || fail "summary=\"session set changed across restart\""
bash "$ROOT/scripts/agent/smoke.sh" >/dev/null || fail "summary=\"routes not healthy after restart\""
pass "summary=\"restarted, sessions preserved, routes gated\" commit=$(git rev-parse --short HEAD)"
