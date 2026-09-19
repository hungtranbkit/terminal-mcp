#!/usr/bin/env bash
# Installs the Terminal MCP verified prompt-start watcher timer into THIS
# user's systemd (`systemctl --user`) -- run it ON THE CONTROLLER, as the
# user that owns the tmux sessions and the controller's own
# terminal-mcp-http.service.
#
# Why this script exists: deploy/systemd/terminal-mcp-prompt-start-watcher.
# {service,timer} shipped in the repo for a long time while
# `systemctl --user is-enabled terminal-mcp-prompt-start-watcher.timer`
# answered `not-found` on the live controller -- the units hardcoded an
# absolute /home/<someone>/... ExecStart from a retired host, so there was
# never a copy step that could have worked. "In the repo" is not "installed",
# and nothing closed that gap. This script is that step, and
# tests/test_prompt_start_watcher_units.py is the guard that keeps the two
# from drifting apart again.
#
# Everything here is idempotent: re-running it re-renders the same unit text,
# reloads, and re-enables. It never edits any other unit, never restarts the
# controller, and never touches the durable prompt_submissions.db.
#
# Usage:
#   ./deploy/install-prompt-start-watcher.sh [options]
#
# Options:
#   --repo-dir DIR     Checkout to run the watcher from (default: this script's
#                       own repo, resolved via git rev-parse --show-toplevel)
#   --venv DIR         Virtualenv holding the installed package
#                       (default: <repo-dir>/.venv)
#   --config FILE      TERMINAL_MCP_CONFIG for the cycle -- the SAME controller
#                       config terminal-mcp-http.service uses (default: read
#                       from that unit, else the repo's config.yaml)
#   --watcher-config F Watcher's own JSON knobs
#                       (default: ~/.config/terminal-mcp/prompt-start-watcher.json)
#   --no-enable        Install and daemon-reload, but do not enable/start the
#                       timer (for staging a host you are not cutting over yet)
#   --dry-run          Print what would be installed; write and change nothing
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR=""
VENV_DIR=""
CONTROLLER_CONFIG=""
WATCHER_CONFIG="$HOME/.config/terminal-mcp/prompt-start-watcher.json"
ENABLE=1
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-dir) REPO_DIR="$2"; shift 2 ;;
    --venv) VENV_DIR="$2"; shift 2 ;;
    --config) CONTROLLER_CONFIG="$2"; shift 2 ;;
    --watcher-config) WATCHER_CONFIG="$2"; shift 2 ;;
    --no-enable) ENABLE=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

UNIT_DIR="$HOME/.config/systemd/user"
SERVICE_UNIT="terminal-mcp-prompt-start-watcher.service"
TIMER_UNIT="terminal-mcp-prompt-start-watcher.timer"
ENV_FILE="$HOME/.config/terminal-mcp/prompt-start-watcher.env"

# -- 1. Resolve this host's real paths ---------------------------------------
if [[ -z "$REPO_DIR" ]]; then
  REPO_DIR="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || echo "$(cd "$SCRIPT_DIR/.." && pwd)")"
fi
REPO_DIR="$(cd "$REPO_DIR" && pwd)"
[[ -z "$VENV_DIR" ]] && VENV_DIR="$REPO_DIR/.venv"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Error: no interpreter at $VENV_DIR/bin/python." >&2
  echo "       Create it first: python3 -m venv '$VENV_DIR' && '$VENV_DIR/bin/pip' install -e '$REPO_DIR'" >&2
  exit 1
fi

# The controller config is the one piece of genuinely host-specific state the
# unit needs and cannot guess. Prefer the value the live controller unit is
# already using, so the watcher can never reconcile a DIFFERENT store than the
# controller writes to -- that would be worse than not running at all.
if [[ -z "$CONTROLLER_CONFIG" ]]; then
  CONTROLLER_CONFIG="$(sed -n 's/^Environment=TERMINAL_MCP_CONFIG=//p' \
    "$UNIT_DIR/terminal-mcp-http.service" 2>/dev/null | tail -1 || true)"
fi
[[ -z "$CONTROLLER_CONFIG" ]] && CONTROLLER_CONFIG="${TERMINAL_MCP_CONFIG:-$REPO_DIR/config.yaml}"

if [[ ! -f "$CONTROLLER_CONFIG" ]]; then
  echo "Error: controller config not found: $CONTROLLER_CONFIG" >&2
  echo "       Pass the right one with --config -- it must be the same file" >&2
  echo "       terminal-mcp-http.service reads, or the watcher would reconcile" >&2
  echo "       a different submission store than the controller writes to." >&2
  exit 1
fi

echo "== Terminal MCP prompt-start watcher installer =="
echo "  repo:              $REPO_DIR"
echo "  venv:              $VENV_DIR"
echo "  controller config: $CONTROLLER_CONFIG"
echo "  watcher config:    $WATCHER_CONFIG"
echo "  unit dir:          $UNIT_DIR"
echo

if [[ ! -x "$VENV_DIR/bin/terminal-mcp-prompt-watcher" ]]; then
  echo "-> Note: $VENV_DIR/bin/terminal-mcp-prompt-watcher is not present (this venv"
  echo "   predates the console-script entry point). The rendered unit will fall back"
  echo "   to '$VENV_DIR/bin/python -m terminal_mcp.prompt_start_watcher', which works"
  echo "   identically. Re-run '$VENV_DIR/bin/pip install -e $REPO_DIR' to get the script."
fi

if [[ $DRY_RUN -eq 1 ]]; then
  echo "-- DRY RUN: rendered units (nothing written) --"
  TMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TMP_DIR"' EXIT
  "$VENV_DIR/bin/python" -m terminal_mcp.prompt_start_watcher \
    --render-units "$TMP_DIR" --repo-dir "$REPO_DIR" --venv-dir "$VENV_DIR" \
    --config "$WATCHER_CONFIG" >/dev/null
  for unit in "$SERVICE_UNIT" "$TIMER_UNIT"; do
    echo "----- $UNIT_DIR/$unit -----"
    cat "$TMP_DIR/$unit"
  done
  echo "----- $ENV_FILE -----"
  echo "TERMINAL_MCP_CONFIG=$CONTROLLER_CONFIG"
  exit 0
fi

# -- 2. Watcher config + env file (both idempotent) ---------------------------
mkdir -p "$(dirname "$WATCHER_CONFIG")" "$UNIT_DIR"
if [[ -f "$WATCHER_CONFIG" ]]; then
  echo "-> $WATCHER_CONFIG already exists, leaving its values as-is."
else
  # Defaults only. max_enters matches orchestration_policy.PROMPT_RETRY_CAP;
  # the durable per-submission cap in the store is what actually enforces it.
  umask 077
  cat > "$WATCHER_CONFIG" <<'JSON'
{
  "enabled": true,
  "max_enters": 6,
  "interval_seconds": 10,
  "include_sessions": [],
  "exclude_sessions": []
}
JSON
  echo "-> Wrote default watcher config to $WATCHER_CONFIG"
fi

umask 077
cat > "$ENV_FILE" <<EOF
# Written by deploy/install-prompt-start-watcher.sh -- the controller config
# the watcher cycle must read. Keep this identical to the value in
# terminal-mcp-http.service, or the watcher reconciles a different store.
TERMINAL_MCP_CONFIG=$CONTROLLER_CONFIG
EOF
echo "-> Wrote $ENV_FILE"

# -- 3. Render + install the units --------------------------------------------
"$VENV_DIR/bin/python" -m terminal_mcp.prompt_start_watcher \
  --render-units "$UNIT_DIR" --repo-dir "$REPO_DIR" --venv-dir "$VENV_DIR" \
  --config "$WATCHER_CONFIG"

# -- 4. Reload + enable --------------------------------------------------------
if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo
  echo "Units are installed, but this shell cannot reach the user systemd bus"
  echo "(no XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS -- typical over plain ssh)."
  echo "Finish from a real login session, or re-run with the bus exported:"
  echo "  export XDG_RUNTIME_DIR=/run/user/\$(id -u)"
  echo "  systemctl --user daemon-reload"
  echo "  systemctl --user enable --now $TIMER_UNIT"
  exit 0
fi

systemctl --user daemon-reload
echo "-> daemon-reload done"

if [[ $ENABLE -eq 0 ]]; then
  echo "-> --no-enable given; not enabling the timer. Enable it with:"
  echo "   systemctl --user enable --now $TIMER_UNIT"
  exit 0
fi

systemctl --user enable --now "$TIMER_UNIT"
echo "-> Timer enabled and started"
echo
systemctl --user list-timers --all "$TIMER_UNIT" || true
echo
echo "Verify a real activation (give it one interval first):"
echo "  systemctl --user status $SERVICE_UNIT"
echo "  journalctl --user -u $SERVICE_UNIT -n 50 --no-pager"
echo "  cat ~/.local/state/terminal-mcp/prompt-start-watcher.json"
