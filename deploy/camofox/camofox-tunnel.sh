#!/usr/bin/env bash
# Operator-run bootstrap for the optional Camofox SSH forward (default off).
#
#   camofox-tunnel.sh keygen              # dedicated key + restricted authorized_keys line
#   camofox-tunnel.sh install --host H    # write unit + tunnel.env; does NOT start
#   camofox-tunnel.sh start|stop|status   # explicit lifecycle
#   camofox-tunnel.sh smoke               # /health + unauthenticated 401 through the forward
#   camofox-tunnel.sh uninstall
#
# Options: --host USER@HOST (or ssh alias, must already be in known_hosts),
#          --local-port N (default 19377), --identity FILE
#          (default ~/.ssh/camofox_tunnel_ed25519), --enable (install only:
#          also enable at boot; leave off unless the owner approved it).
#
# Never prints the Camofox access key. The forward listens on 127.0.0.1 only.
set -euo pipefail

UNIT=camofox-tunnel.service
CONF_DIR="$HOME/.config/camofox"
ENV_FILE="$CONF_DIR/tunnel.env"
UNIT_DIR="$HOME/.config/systemd/user"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST=""
PORT=19377
IDENTITY="$HOME/.ssh/camofox_tunnel_ed25519"
ENABLE=0

cmd="${1:-}"; shift || true
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --local-port) PORT="$2"; shift 2 ;;
    --identity) IDENTITY="$2"; shift 2 ;;
    --enable) ENABLE=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

die() { echo "camofox-tunnel: $*" >&2; exit 1; }
load_env() { [ -f "$ENV_FILE" ] || die "not installed ($ENV_FILE missing)"; # shellcheck disable=SC1090
  . "$ENV_FILE"; PORT="${CAMOFOX_TUNNEL_LOCAL_PORT:-$PORT}"; }

case "$cmd" in
  keygen)
    umask 077
    [ -f "$IDENTITY" ] || ssh-keygen -q -t ed25519 -N "" -C "camofox-tunnel@$(hostname)" -f "$IDENTITY"
    echo "# Add this ONE line to ~/.ssh/authorized_keys of the worker account on m910."
    echo "# It allows only a forward to 127.0.0.1:9377: no shell, no other ports, no agent/X11."
    printf 'restrict,port-forwarding,permitopen="127.0.0.1:9377",command="/bin/false" %s\n' "$(cat "$IDENTITY.pub")"
    ;;
  install)
    [ -n "$HOST" ] || die "--host is required"
    [[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ] || die "bad --local-port"
    [ -f "$IDENTITY" ] || die "identity $IDENTITY missing (run keygen, then authorize it on m910)"
    resolved=$(ssh -G "$HOST" 2>/dev/null | awk '/^hostname /{print $2; exit}')
    ssh-keygen -F "${resolved:-${HOST#*@}}" >/dev/null 2>&1 \
      || die "host key for ${resolved:-$HOST} not in known_hosts (StrictHostKeyChecking=yes); connect once interactively and verify it"
    umask 077; mkdir -p "$CONF_DIR" "$UNIT_DIR"
    printf 'CAMOFOX_TUNNEL_HOST=%s\nCAMOFOX_TUNNEL_LOCAL_PORT=%s\nCAMOFOX_TUNNEL_IDENTITY=%s\n' \
      "$HOST" "$PORT" "$IDENTITY" > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    install -m 0644 "$SRC_DIR/$UNIT" "$UNIT_DIR/$UNIT"
    systemctl --user daemon-reload
    if [ "$ENABLE" = 1 ]; then systemctl --user enable "$UNIT"; fi
    echo "installed (not started). Start: $0 start ; provider env:"
    echo "  TERMINAL_MCP_CAMOFOX_BASE_URL=http://127.0.0.1:$PORT"
    echo "  TERMINAL_MCP_CAMOFOX_ACCESS_KEY_FILE=$CONF_DIR/access.key  (copy out of band, chmod 600)"
    ;;
  start) load_env; systemctl --user start "$UNIT"; sleep 2; systemctl --user --no-pager status "$UNIT" | head -5 ;;
  stop) systemctl --user stop "$UNIT" ;;
  status)
    systemctl --user --no-pager status "$UNIT" 2>/dev/null | head -5 || true
    load_env; ss -ltn | grep -E "127\.0\.0\.1:$PORT\b" || echo "no listener on 127.0.0.1:$PORT"
    ;;
  smoke)
    load_env
    curl -fsS -m 5 "http://127.0.0.1:$PORT/health" >/dev/null && echo "health: ok" || die "health failed"
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://127.0.0.1:$PORT/tabs?userId=tunnel-smoke")
    [ "$code" = 401 ] || [ "$code" = 403 ] || die "unauthenticated request got $code (expected 401/403)"
    echo "auth: unauthenticated request rejected ($code)"
    ;;
  uninstall)
    systemctl --user disable --now "$UNIT" 2>/dev/null || true
    rm -f "$UNIT_DIR/$UNIT" "$ENV_FILE"; systemctl --user daemon-reload
    echo "removed unit and tunnel.env (dedicated key and access key left in place)"
    ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
