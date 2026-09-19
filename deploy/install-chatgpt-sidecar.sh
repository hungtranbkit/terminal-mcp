#!/usr/bin/env bash
# Install the compact ChatGPT connector surface (terminal-mcp-chatgpt-v1) as a
# systemd --user service on THIS host.
#
# Why a script and not "copy the unit": the unit ships %h-relative defaults for
# the canonical controller layout (~/workspace/terminal-mcp). A host whose
# checkout or venv lives anywhere else needs those two lines rewritten, and
# doing that by hand is how deploy/systemd ended up shipping a unit pinned to a
# retired /home/dell layout that could not start at all. This resolves the
# repo it is run from instead of assuming one, and refuses to install a unit it
# knows cannot work.
#
# Idempotent: safe to re-run after a pull. It does NOT touch
# terminal-mcp-http.service.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${TERMINAL_MCP_VENV:-$REPO/.venv}"
BIN="$VENV/bin/terminal-mcp-chatgpt-v1"
UNIT_SRC="$REPO/deploy/systemd/terminal-mcp-chatgpt-v1.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_DST="$UNIT_DIR/terminal-mcp-chatgpt-v1.service"
PORT="${TERMINAL_MCP_CHATGPT_PORT:-8768}"
BACKEND="${TERMINAL_MCP_CHATGPT_BACKEND:-http://127.0.0.1:8766/mcp}"

echo "repo:    $REPO"
echo "venv:    $VENV"
echo "port:    127.0.0.1:$PORT"
echo "backend: $BACKEND"

[ -f "$UNIT_SRC" ] || { echo "FATAL: $UNIT_SRC missing" >&2; exit 1; }

# Fail BEFORE installing rather than leaving a unit that crash-loops. The
# console script only exists after `pip install -e .`, which is exactly the
# step people skip after adding a new entry point.
if [ ! -x "$BIN" ]; then
  echo "FATAL: $BIN not found or not executable." >&2
  echo "  The console script is new, so an existing venv does not have it yet." >&2
  echo "  Run:  $VENV/bin/pip install -e $REPO" >&2
  exit 1
fi

# Refuse to fight the controller for a port.
if [ "$PORT" = "8766" ]; then
  echo "FATAL: port 8766 is the full controller. Pick another (default 8768)." >&2
  exit 1
fi

mkdir -p "$UNIT_DIR"
# Rewrite the %h-relative paths only when this checkout is NOT the canonical
# location; otherwise ship the unit byte-for-byte so `systemctl cat` matches
# what is in git.
if [ "$REPO" = "$HOME/workspace/terminal-mcp" ] && [ "$VENV" = "$REPO/.venv" ]; then
  install -m 0644 "$UNIT_SRC" "$UNIT_DST"
  echo "installed unit unchanged (canonical layout)"
else
  sed -e "s|^WorkingDirectory=.*|WorkingDirectory=$REPO|" \
      -e "s|^ExecStart=.*|ExecStart=$BIN|" \
      "$UNIT_SRC" > "$UNIT_DST.tmp"
  install -m 0644 "$UNIT_DST.tmp" "$UNIT_DST"
  rm -f "$UNIT_DST.tmp"
  echo "installed unit with rewritten paths (non-canonical layout)"
fi

ENV_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/terminal-mcp/chatgpt-v1.env"
if [ ! -f "$ENV_FILE" ]; then
  mkdir -p "$(dirname "$ENV_FILE")"
  umask 077
  cat > "$ENV_FILE" <<ENVEOF
# Overrides for terminal-mcp-chatgpt-v1. Carries no credential -- the sidecar
# reaches the controller over loopback and holds no secret of its own.
TERMINAL_MCP_CHATGPT_BACKEND=$BACKEND
TERMINAL_MCP_CHATGPT_PORT=$PORT
ENVEOF
  echo "wrote $ENV_FILE"
fi

systemctl --user daemon-reload
systemctl --user enable --now terminal-mcp-chatgpt-v1.service
sleep 2

echo
echo "--- status ---"
systemctl --user --no-pager --lines=5 status terminal-mcp-chatgpt-v1.service || true

echo
echo "--- catalog served on 127.0.0.1:$PORT ---"
# Prove the surface is actually answering, and with the compact catalog. A
# unit that started is not the same as a connector that works.
SID=$(curl -s -m 10 -D - -o /dev/null -X POST "http://127.0.0.1:$PORT/mcp" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"install","version":"0"}}}' \
  2>/dev/null | tr -d '\r' | awk '/[Mm]cp-session-id/{print $2}')
if [ -z "${SID:-}" ]; then
  echo "WARNING: no MCP session id -- the sidecar did not answer initialize." >&2
  echo "  Check: journalctl --user -u terminal-mcp-chatgpt-v1 -n 40" >&2
  exit 1
fi
curl -s -m 10 -X POST "http://127.0.0.1:$PORT/mcp" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H "mcp-session-id: $SID" -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null
curl -s -m 20 -X POST "http://127.0.0.1:$PORT/mcp" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H "mcp-session-id: $SID" -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | "$VENV/bin/python" -c 'import json,sys
d=json.load(sys.stdin); t=[x["name"] for x in d.get("result",{}).get("tools",[])]
print(f"count={len(t)}")
for n in t: print("  -",n)
legacy={"terminal_list_sessions","terminal_tail","terminal_capture","terminal_status","terminal_send_text","terminal_send_keys"}
assert t, "empty catalog -- is the controller on 8766 up?"
assert not legacy.issubset(set(t)), "LEGACY SIX regression on the compact surface"
assert "terminal_turn" in t and "terminal_batch_inspect" in t, "compact tools missing"
print("OK: compact catalog served, not the legacy six")'

echo
echo "Next: point the ChatGPT connector at the tunnel that fronts"
echo "      http://127.0.0.1:$PORT/mcp  (see docs/CHATGPT_CONNECTOR.md)."
