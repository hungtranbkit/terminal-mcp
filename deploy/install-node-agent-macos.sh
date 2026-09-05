#!/usr/bin/env bash
# Installs terminal-node-agent on a macOS WORKER node -- run this ON THE
# WORKER NODE ITSELF, not on the controller (Dell). Mirrors install-
# node-agent.sh (the Linux installer) closely -- SessionBackend/tmux.py
# have no OS-specific branching at all, so a plain tmux server on macOS
# is exactly as good a backend as on Linux (host_metrics.py's own
# `collect()` already has a graceful macOS fallback: CPU/RAM/swap read
# as None/unknown, disk usage still populated via shutil, cross-platform
# -- a known, accepted gap, not a bug). The two real platform
# differences this script exists for: (1) no systemd on macOS -- a
# LaunchAgent (~/Library/LaunchAgents) is this platform's equivalent of
# `systemctl --user`, loaded into the user's own GUI launchd session so
# it starts at login and restarts on crash, same spirit as
# `Restart=always` + `loginctl enable-linger`; (2) `hostname -I` doesn't
# exist on macOS -- LAN IP is read via `ipconfig getifaddr` per
# interface instead.
#
# Usage:
#   ./install-node-agent-macos.sh --controller <http://controller-host:8766> --node-id <id> [options]
#
# Required:
#   --controller URL     Controller's terminal-mcp-http base URL (e.g. http://192.168.1.10:8766)
#   --node-id ID          This node's own id (e.g. "macbook") -- must match what you'll register
#                          on the controller's config.yaml nodes.remote[].node_id
#
# Optional:
#   --repo-dir DIR         Where to clone/use the terminal-mcp repo (default: ~/terminal-mcp)
#   --repo-url URL         Git remote to clone from (default: unset -- if --repo-dir doesn't
#                           already contain a checkout, you'll be told to clone it yourself)
#   --python BIN            Python 3.11+ interpreter to build the venv with (default: python3.11,
#                            falls back to python3 -- this project needs >=3.11; Apple's own
#                            bundled /usr/bin/python3 is typically older, see this script's own
#                            preflight check)
#   --port PORT             Node agent's own listen port (default: 8790)
#   --host HOST             Node agent's own bind address (default: 127.0.0.1 -- see the
#                            LaunchAgent's own comment before changing this)
#   --heartbeat-interval N  Seconds between heartbeat pushes to the controller (default: 20)
#   --no-launchd            Print the commands instead of running launchctl
set -euo pipefail

CONTROLLER_URL=""
NODE_ID=""
REPO_DIR="$HOME/terminal-mcp"
REPO_URL=""
PYTHON_BIN=""
AGENT_PORT=8790
AGENT_HOST=127.0.0.1
HEARTBEAT_INTERVAL=20
USE_LAUNCHD=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --controller) CONTROLLER_URL="$2"; shift 2 ;;
    --node-id) NODE_ID="$2"; shift 2 ;;
    --repo-dir) REPO_DIR="$2"; shift 2 ;;
    --repo-url) REPO_URL="$2"; shift 2 ;;
    --python) PYTHON_BIN="$2"; shift 2 ;;
    --port) AGENT_PORT="$2"; shift 2 ;;
    --host) AGENT_HOST="$2"; shift 2 ;;
    --heartbeat-interval) HEARTBEAT_INTERVAL="$2"; shift 2 ;;
    --no-launchd) USE_LAUNCHD=0; shift ;;
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

echo "== Terminal MCP node agent installer (macOS) =="
echo "  node-id:    $NODE_ID"
echo "  controller: $CONTROLLER_URL"
echo "  repo-dir:   $REPO_DIR"
echo

# -- 0. Preflight: tmux + a real 3.11+ Python --------------------------------
if ! command -v tmux >/dev/null 2>&1; then
  echo "Error: tmux not found. Install it first, e.g.: brew install tmux" >&2
  exit 1
fi
if [[ -z "$PYTHON_BIN" ]]; then
  if command -v python3.11 >/dev/null 2>&1; then
    PYTHON_BIN="python3.11"
  elif command -v python3.12 >/dev/null 2>&1; then
    PYTHON_BIN="python3.12"
  else
    PYTHON_BIN="python3"
  fi
fi
PY_VERSION=$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_OK=$("$PYTHON_BIN" -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)')
if [[ "$PY_OK" != "1" ]]; then
  echo "Error: $PYTHON_BIN is $PY_VERSION, need >=3.11. Apple's bundled /usr/bin/python3 is" >&2
  echo "commonly older than this -- install a newer one first, e.g.: brew install python@3.11" >&2
  echo "then re-run with --python \$(brew --prefix python@3.11)/bin/python3.11" >&2
  exit 1
fi
echo "-> Using $PYTHON_BIN ($PY_VERSION)"

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
  echo "-> Creating .venv with $PYTHON_BIN"
  "$PYTHON_BIN" -m venv .venv
fi
echo "-> Installing terminal-mcp (pip install -e .)"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -e .

# -- 3. This node's own token -------------------------------------------------
ENV_FILE="$REPO_DIR/node-agent.env"
if [[ -f "$ENV_FILE" ]]; then
  echo "-> $ENV_FILE already exists, leaving its token as-is."
else
  TOKEN=$("$PYTHON_BIN" -c "import secrets; print(secrets.token_hex(32))")
  umask 077
  cat > "$ENV_FILE" <<EOF
# Shared secret for THIS node agent -- generated by install-node-agent-macos.sh.
# Register the SAME value on the controller as an environment variable
# named TERMINAL_MCP_NODE_TOKEN_$(echo "$NODE_ID" | tr '[:lower:]' '[:upper:]' | tr '-' '_')
# (see this script's own final instructions). Never commit this file,
# never log this value, never send it anywhere except that one variable.
TERMINAL_MCP_NODE_TOKEN=$TOKEN
EOF
  chmod 600 "$ENV_FILE"
  echo "-> Generated a new token at $ENV_FILE (mode 600)"
fi
TOKEN_VALUE=$(grep '^TERMINAL_MCP_NODE_TOKEN=' "$ENV_FILE" | cut -d= -f2-)

# -- 4. config.yaml -----------------------------------------------------------
if [[ ! -f "$REPO_DIR/config.yaml" ]]; then
  echo "-> No config.yaml found -- copying config.example.yaml as a starting point."
  echo "   Review it (allowed_session_patterns, session_lifecycle.allowed_cwd_roots) before starting the service."
  cp "$REPO_DIR/config.example.yaml" "$REPO_DIR/config.yaml"
fi

# macOS has no `hostname -I` -- ipconfig getifaddr per interface instead.
# Same RFC1918-preference logic as the Linux installer (a machine with
# more than one active interface, e.g. Wi-Fi + a USB-Ethernet dongle,
# could otherwise pick the wrong one).
DETECTED_IP=""
for iface in en0 en1 en2 en3 en4; do
  ip=$(ipconfig getifaddr "$iface" 2>/dev/null || true)
  if [[ -n "$ip" ]]; then
    case "$ip" in
      10.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*|192.168.*) DETECTED_IP="$ip"; break ;;
    esac
    [[ -z "$DETECTED_IP" ]] && DETECTED_IP="$ip"
  fi
done

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
  echo "!! network before using it, e.g. with 'ipconfig getifaddr en0')."
  echo
fi

# -- 5. LaunchAgent ------------------------------------------------------------
# macOS's closest equivalent of `systemctl --user` + `loginctl enable-
# linger`: a per-user LaunchAgent plist in ~/Library/LaunchAgents,
# bootstrapped into the user's own GUI launchd domain (`gui/<uid>`) so it
# starts at login/boot (once the user is logged in -- exactly the
# clamshell-mode condition this node's own bring-up already relies on:
# charger + external display keeps the GUI session alive with the lid
# closed) and is restarted automatically on crash (KeepAlive), mirroring
# systemd's Restart=always.
UID_NUM=$(id -u)
LAUNCH_DIR="$HOME/Library/LaunchAgents"
PLIST_LABEL="com.terminal-mcp.node-agent.$NODE_ID"
PLIST_PATH="$LAUNCH_DIR/$PLIST_LABEL.plist"
LOG_DIR="$HOME/Library/Logs/terminal-mcp"
mkdir -p "$LOG_DIR"

PLIST_CONTENT=$(cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$REPO_DIR/.venv/bin/terminal-node-agent</string>
        <string>--node-id</string>
        <string>$NODE_ID</string>
        <string>--controller-url</string>
        <string>$CONTROLLER_URL</string>
        <string>--host</string>
        <string>$AGENT_HOST</string>
        <string>--port</string>
        <string>$AGENT_PORT</string>
        <string>--heartbeat-interval-seconds</string>
        <string>$HEARTBEAT_INTERVAL</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>TERMINAL_MCP_CONFIG</key>
        <string>$REPO_DIR/config.yaml</string>
        <key>TERMINAL_MCP_NODE_TOKEN</key>
        <string>$TOKEN_VALUE</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/node-agent.out.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/node-agent.err.log</string>
</dict>
</plist>
EOF
)

if [[ "$USE_LAUNCHD" -eq 1 ]]; then
  mkdir -p "$LAUNCH_DIR"
  echo "$PLIST_CONTENT" > "$PLIST_PATH"
  echo "-> Wrote $PLIST_PATH"
  # bootout is a no-op (with a harmless error) if it wasn't loaded yet --
  # always boot it out first so a re-run picks up a changed plist, same
  # reasoning as the Linux installer's own `restart` (not `enable --now`).
  launchctl bootout "gui/$UID_NUM" "$PLIST_PATH" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_NUM" "$PLIST_PATH"
  launchctl enable "gui/$UID_NUM/$PLIST_LABEL"
  echo "-> Loaded $PLIST_LABEL (launchctl print gui/$UID_NUM/$PLIST_LABEL to check;"
  echo "   logs at $LOG_DIR/node-agent.{out,err}.log)"
else
  echo "-> --no-launchd given: plist content that WOULD have been written to $PLIST_PATH:"
  echo "$PLIST_CONTENT"
  echo "-> Start it manually with:"
  echo "   TERMINAL_MCP_CONFIG=$REPO_DIR/config.yaml TERMINAL_MCP_NODE_TOKEN=$TOKEN_VALUE \\"
  echo "     $REPO_DIR/.venv/bin/terminal-node-agent --node-id $NODE_ID --controller-url $CONTROLLER_URL --host $AGENT_HOST --port $AGENT_PORT"
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
echo "           endpoint: \"http://$DETECTED_IP:$AGENT_PORT\"  # verify this is really the LAN address reachable from the controller"
echo "           token_env: TERMINAL_MCP_NODE_TOKEN_$(echo "$NODE_ID" | tr '[:lower:]' '[:upper:]' | tr '-' '_')"
echo
echo "2) Export the SAME token this node generated as that environment variable"
echo "   wherever the controller's terminal-mcp-http.service reads its own"
echo "   environment from (its systemd unit's EnvironmentFile, or an /etc/"
echo "   systemd/system/terminal-mcp-http.service.d/ override -- never inline"
echo "   in the unit file itself):"
echo
echo "     TERMINAL_MCP_NODE_TOKEN_$(echo "$NODE_ID" | tr '[:lower:]' '[:upper:]' | tr '-' '_')=$TOKEN_VALUE"
echo
echo "3) Restart the controller's terminal-mcp-http.service (safe restart --"
echo "   verify existing tmux sessions/session_created timestamps are"
echo "   unchanged before/after, same as any other restart of this service)."
echo
echo "4) Verify with: terminal-mcp-doctor nodes   (on the controller)"
echo "   -- $NODE_ID should show status=online within one heartbeat interval (~${HEARTBEAT_INTERVAL}s)."
