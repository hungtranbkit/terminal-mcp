#!/usr/bin/env bash
# Installs terminal-node-agent on a WORKER node (e.g. the Lenovo M910) --
# run this ON THE WORKER NODE ITSELF, not on the controller (Dell). It
# never touches the controller's own systemd units, config, or tmux
# sessions -- see this script's own printed final step for the one manual
# action still required on the controller (registering this node in its
# config.yaml).
#
# Usage:
#   ./install-node-agent.sh --controller <http://controller-host:8766> --node-id <id> [options]
#
# Required:
#   --controller URL     Controller's terminal-mcp-http base URL (e.g. http://192.168.1.10:8766)
#   --node-id ID          This node's own id (e.g. "m910") -- must match what you'll register
#                          on the controller's config.yaml nodes.remote[].node_id
#
# Optional:
#   --repo-dir DIR         Where to clone/use the terminal-mcp repo (default: ~/terminal-mcp)
#   --repo-url URL         Git remote to clone from (default: unset -- if --repo-dir doesn't
#                           already contain a checkout, you'll be told to clone it yourself)
#   --port PORT             Node agent's own listen port (default: 8790)
#   --host HOST             Node agent's own bind address (default: 127.0.0.1 -- see the
#                           systemd unit's own comment before changing this)
#   --heartbeat-interval N  Seconds between heartbeat pushes to the controller (default: 20)
#   --no-systemd            Print the commands instead of running systemctl (for a non-systemd host)
set -euo pipefail

CONTROLLER_URL=""
NODE_ID=""
REPO_DIR="$HOME/terminal-mcp"
REPO_URL=""
AGENT_PORT=8790
AGENT_HOST=127.0.0.1
HEARTBEAT_INTERVAL=20
USE_SYSTEMD=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --controller) CONTROLLER_URL="$2"; shift 2 ;;
    --node-id) NODE_ID="$2"; shift 2 ;;
    --repo-dir) REPO_DIR="$2"; shift 2 ;;
    --repo-url) REPO_URL="$2"; shift 2 ;;
    --port) AGENT_PORT="$2"; shift 2 ;;
    --host) AGENT_HOST="$2"; shift 2 ;;
    --heartbeat-interval) HEARTBEAT_INTERVAL="$2"; shift 2 ;;
    --no-systemd) USE_SYSTEMD=0; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$CONTROLLER_URL" || -z "$NODE_ID" ]]; then
  echo "Error: --controller and --node-id are both required. See --help." >&2
  exit 1
fi
if [[ ! "$NODE_ID" =~ ^[a-zA-Z0-9_-]+$ ]]; then
  echo "Error: --node-id must be alphanumeric/-/_ only (got: $NODE_ID)" >&2
  exit 1
fi

echo "== Terminal MCP node agent installer =="
echo "  node-id:    $NODE_ID"
echo "  controller: $CONTROLLER_URL"
echo "  repo-dir:   $REPO_DIR"
echo

# -- 1. Repo checkout --------------------------------------------------------
if [[ -d "$REPO_DIR/.git" ]]; then
  echo "-> Found existing repo checkout at $REPO_DIR, leaving it as-is (pull/checkout yourself if you need a specific version)."
elif [[ -n "$REPO_URL" ]]; then
  echo "-> Cloning $REPO_URL into $REPO_DIR"
  git clone "$REPO_URL" "$REPO_DIR"
else
  echo "Error: $REPO_DIR does not exist and --repo-url was not given -- either clone the repo" >&2
  echo "there yourself first, or re-run with --repo-url <git-url>." >&2
  exit 1
fi

# -- 2. Python venv + install -------------------------------------------------
cd "$REPO_DIR"
if [[ ! -d .venv ]]; then
  echo "-> Creating .venv"
  python3 -m venv .venv
fi
echo "-> Installing terminal-mcp (pip install -e .)"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -e .

# -- 3. This node's own token -------------------------------------------------
ENV_FILE="$REPO_DIR/node-agent.env"  # legacy migration source only
TOKEN_FILE="$REPO_DIR/node-agent.token"
if [[ -f "$TOKEN_FILE" ]]; then
  chmod 600 "$TOKEN_FILE"
  echo "-> Reusing protected token file $TOKEN_FILE"
elif [[ -f "$ENV_FILE" ]]; then
  TOKEN_VALUE=$(grep "^TERMINAL_MCP_NODE_TOKEN=" "$ENV_FILE" | cut -d= -f2- || true)
  if [[ -z "$TOKEN_VALUE" ]]; then echo "Error: legacy $ENV_FILE contains no node token" >&2; exit 1; fi
  umask 077
  printf "%s" "$TOKEN_VALUE" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  rm -f "$ENV_FILE"
  unset TOKEN_VALUE
  echo "-> Migrated legacy token into $TOKEN_FILE and removed $ENV_FILE"
else
  TOKEN_VALUE=$("$REPO_DIR/.venv/bin/python" -c "import secrets; print(secrets.token_hex(32))")
  umask 077
  printf "%s" "$TOKEN_VALUE" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  unset TOKEN_VALUE
  echo "-> Generated a protected token file at $TOKEN_FILE"
fi

# -- 4. config.yaml -----------------------------------------------------------
if [[ ! -f "$REPO_DIR/config.yaml" ]]; then
  echo "-> No config.yaml found -- copying config.example.yaml as a starting point."
  echo "   Review it (allowed_session_patterns, session_lifecycle.allowed_cwd_roots) before starting the service."
  cp "$REPO_DIR/config.example.yaml" "$REPO_DIR/config.yaml"
fi

# `hostname -I` lists every address on every interface in no particular
# order -- on a machine with more than one NIC this can put a
# link-local 169.254.x.x autoconf address first, ahead of the real
# routable LAN address on another interface (seen live on the M910: a
# disconnected wired port's APIPA address sorted before the real Wi-Fi
# LAN address). Prefer a private RFC1918 address; fall back to the
# first address only if none is found.
DETECTED_IP=""
for ip in $(hostname -I 2>/dev/null); do
  case "$ip" in
    10.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*|192.168.*) DETECTED_IP="$ip"; break ;;
  esac
done
if [[ -z "$DETECTED_IP" ]]; then
  DETECTED_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
fi

if [[ "$AGENT_HOST" == "127.0.0.1" ]]; then
  echo
  echo "!! --host was not given, so this agent is bound to 127.0.0.1 (loopback) --"
  echo "!! that is the SAFE DEFAULT (never exposed without an explicit choice), but"
  echo "!! it also means the controller can push heartbeats FROM this node (this node"
  echo "!! calls out to the controller) but can NEVER reach back IN to create/attach/"
  echo "!! send input to a session here -- the node will show status=online (from the"
  echo "!! heartbeat) yet every session operation on it will fail with a connection"
  echo "!! error. For a node that must actually run sessions, re-run with:"
  echo "!!   --host $DETECTED_IP"
  echo "!! (this node's own detected LAN address -- verify it's correct for your"
  echo "!! network before using it, e.g. with 'ip -o -4 addr show')."
  echo
fi

# -- 5. systemd unit -----------------------------------------------------------
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/terminal-node-agent.service"
UNIT_CONTENT=$(cat <<EOF
[Unit]
Description=Terminal MCP node agent ($NODE_ID)
After=default.target
# StartLimit* belong to [Unit], NOT [Service]. systemd parses them only
# here; left in [Service] it logs "Unknown key ... ignoring" and the
# rate limit silently does not exist (seen live installing dell-linux
# on 2026-09-10).
StartLimitIntervalSec=300
StartLimitBurst=8

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
Environment=TERMINAL_MCP_CONFIG=$REPO_DIR/config.yaml
ExecStart=$REPO_DIR/.venv/bin/terminal-node-agent --node-id $NODE_ID --controller-url $CONTROLLER_URL --token-file $TOKEN_FILE --host $AGENT_HOST --port $AGENT_PORT --heartbeat-interval-seconds $HEARTBEAT_INTERVAL
Restart=always
RestartSec=3
RestartSteps=6
RestartMaxDelaySec=60
# KillMode=process, NOT the control-group default. The default SIGTERMs
# the ENTIRE cgroup on stop/restart -- including any tmux server started
# from within it. That is exactly how session "m1" was destroyed on m910
# by a routine node-agent restart on 2026-09-09. tmux is meant to outlive
# whatever started it; the controller unit sets this for the same reason
# (see docs/CONTROLLER_RUNBOOK.md).
KillMode=process
# Sized by SESSION COUNT, not by feel. Each tmux session this node
# supervises costs 2 tasks -- an `sh` + `cat` pair piping the pane into
# ~/.local/state/terminal-mcp/raw/<session>.log -- plus the tmux server
# and the agent itself. Budget ~2*sessions + 20.
#
# The previous values (512M / 256) were sized for a small fleet and
# silently became the failure mode on dell-linux at 108 sessions: the
# cgroup sat at 222/256 tasks, every request that needed a new thread
# died with `RuntimeError: can't start new thread`, and the node answered
# `GET /v1/sessions/<name>/status` with HTTP 500. The controller reports
# that as flaky connectivity, which sends you looking at the network
# instead of at `pids.events`. 2048 tasks covers ~1000 sessions.
#
# Check headroom with scripts/node-agent-health.sh; a non-zero `max`
# counter in the cgroup's pids.events/memory.events means you already
# hit the ceiling.
MemoryMax=3G
TasksMax=2048
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=%h/.local/state/terminal-mcp
NoNewPrivileges=yes
SystemCallFilter=@system-service

[Install]
WantedBy=default.target
EOF
)

if [[ "$USE_SYSTEMD" -eq 1 ]]; then
  mkdir -p "$UNIT_DIR"
  echo "$UNIT_CONTENT" > "$UNIT_PATH"
  echo "-> Wrote $UNIT_PATH"
  systemctl --user daemon-reload
  systemctl --user enable terminal-node-agent.service
  # `enable --now` only STARTS the unit if it wasn't already running -- a
  # re-run of this script (e.g. to change --host) that rewrites an
  # already-active unit's ExecStart would then silently keep the OLD
  # process running with the OLD command line forever. `restart` always
  # picks up the new unit file, whether the service was running or not.
  systemctl --user restart terminal-node-agent.service
  echo "-> Started/restarted terminal-node-agent.service (systemctl --user status terminal-node-agent to check)"
  loginctl enable-linger "$USER" 2>/dev/null || echo "   (could not enable-linger automatically -- run 'loginctl enable-linger $USER' as root/sudo so this survives logout)"
else
  echo "-> --no-systemd given: unit content that WOULD have been written to $UNIT_PATH:"
  echo "$UNIT_CONTENT"
  echo "-> Start it manually with:"
  echo "   $REPO_DIR/.venv/bin/terminal-node-agent --node-id $NODE_ID --controller-url $CONTROLLER_URL --host $AGENT_HOST --port $AGENT_PORT"
fi

echo
echo "== Node agent installed. ONE MORE STEP -- on the CONTROLLER (Dell) =="
echo
echo "1) Add this node to the controller's config.yaml (nodes.remote list):"
echo
echo "     nodes:"
echo "       remote:"
echo "         - node_id: $NODE_ID"
echo "           display_name: \"$NODE_ID\""
echo "           hostname: \"$(hostname)\""
echo "           endpoint: \"http://$DETECTED_IP:$AGENT_PORT\"  # verify this is really the LAN address reachable from the controller -- this host has more than one candidate; double-check with 'ip -o -4 addr show' if unsure"
echo "           token_env: TERMINAL_MCP_NODE_TOKEN_$(echo "$NODE_ID" | tr '[:lower:]' '[:upper:]' | tr '-' '_')"
echo
echo "2) Export the SAME token this node generated as that environment variable"
echo "   wherever the controller's terminal-mcp-http.service reads its own"
echo "   environment from (its systemd unit's EnvironmentFile, or an /etc/"
echo "   systemd/system/terminal-mcp-http.service.d/ override -- never inline"
echo "   in the unit file itself):"
echo
echo "     credential: stored only in $TOKEN_FILE (value intentionally not printed)"
echo
echo "3) Restart the controller's terminal-mcp-http.service (safe restart --"
echo "   verify existing tmux sessions/session_created timestamps are"
echo "   unchanged before/after, same as any other restart of this service)."
echo
echo "4) Verify with: terminal-mcp-doctor nodes   (on the controller)"
echo "   -- $NODE_ID should show status=online within one heartbeat interval (~${HEARTBEAT_INTERVAL}s)."
