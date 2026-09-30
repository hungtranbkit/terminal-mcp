#!/usr/bin/env bash
set -euo pipefail

VERSION="${PAPERCLIP_VERSION:-2026.916.1}"
HOST="${PAPERCLIP_HOST:-127.0.0.1}"
PORT="${PAPERCLIP_PORT:-3100}"

case "$HOST" in
  127.0.0.1|localhost|::1) ;;
  *) echo "Refusing non-loopback PAPERCLIP_HOST=$HOST for TMPC-002" >&2; exit 2 ;;
esac

if ! command -v node >/dev/null || ! command -v npm >/dev/null || ! command -v npx >/dev/null; then
  echo "Node/npm/npx are required" >&2
  exit 2
fi

node -e 'const [maj,min]=process.versions.node.split(".").map(Number); if (maj < 24 || (maj === 24 && min < 11)) process.exit(2)' \
  || { echo "Paperclip requires Node >=24.11" >&2; exit 2; }

export PAPERCLIP_HOST="$HOST"
export PAPERCLIP_PORT="$PORT"
export PAPERCLIP_OPEN_ON_LISTEN=false

if ! command -v paperclipai >/dev/null 2>&1; then
  echo "Installing managed Paperclip $VERSION ..."
  npx --yes "paperclipai@$VERSION" install --version "$VERSION" --yes
  export PATH="$HOME/.local/bin:$PATH"
fi

paperclipai onboard --yes --install-service

echo "Paperclip configured locally. Install/start service explicitly with:"
echo "  PAPERCLIP_HOST=$HOST PAPERCLIP_PORT=$PORT paperclipai service install"
echo "  paperclipai service start"
