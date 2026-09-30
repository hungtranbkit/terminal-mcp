#!/usr/bin/env bash
set -euo pipefail

HOST="${PAPERCLIP_HOST:-127.0.0.1}"
PORT="${PAPERCLIP_PORT:-3100}"
URL="http://${HOST}:${PORT}"

case "$HOST" in
  127.0.0.1|localhost|::1) ;;
  *) echo "FAIL non-loopback Paperclip host: $HOST" >&2; exit 2 ;;
esac

if ! command -v paperclipai >/dev/null 2>&1; then
  echo "FAIL paperclipai is not installed" >&2
  exit 1
fi

echo "paperclip=$(paperclipai --version 2>/dev/null || true)"
echo "node=$(node -v)"
echo "codex=$(command -v codex || true)"
echo "claude=$(command -v claude || true)"

paperclipai service status || true

if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "(^|:)${PORT}$"; then
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "(0\.0\.0\.0|\[::\]|:::).*:${PORT}$"; then
    echo "FAIL Paperclip port ${PORT} is exposed on a wildcard address" >&2
    exit 1
  fi
  curl -fsS --max-time 5 "${URL}/" >/dev/null
  echo "PASS ${URL} reachable on loopback"
else
  echo "INFO Paperclip is not listening on ${PORT}"
fi
