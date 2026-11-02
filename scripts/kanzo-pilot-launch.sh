#!/bin/sh
set -eu

binary=kanzo
home_path=
listen=127.0.0.1
port=14242

while [ "$#" -gt 0 ]; do
    case "$1" in
        --binary) binary=$2; shift 2 ;;
        --home) home_path=$2; shift 2 ;;
        --listen) listen=$2; shift 2 ;;
        --port) port=$2; shift 2 ;;
        *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
    esac
done

case "$listen" in
    127.0.0.1|::1) ;;
    *) printf 'refusing non-loopback Kanzo listener: %s (Kanzo has no authentication)\n' "$listen" >&2; exit 2 ;;
esac
case "$port" in
    ''|*[!0-9]*) printf 'invalid port: %s\n' "$port" >&2; exit 2 ;;
esac
if [ "$port" -lt 1024 ] || [ "$port" -gt 65535 ]; then
    printf 'pilot port must be between 1024 and 65535\n' >&2
    exit 2
fi
if [ -z "$home_path" ] || [ ! -d "$home_path" ] || [ ! -f "$home_path/kanzo.db" ] || [ ! -f "$home_path/tmcp-projection.json" ]; then
    printf 'dedicated --home with an existing Terminal MCP projection is required\n' >&2
    exit 2
fi
home_path=$(cd "$home_path" && pwd -P)
case "$home_path" in
    /|/home|/home/*/.config|/home/*/.local) printf 'refusing broad or shared Kanzo home: %s\n' "$home_path" >&2; exit 2 ;;
esac
if [ ! -x "$binary" ]; then
    binary=$(command -v "$binary" 2>/dev/null || true)
fi
if [ -z "$binary" ] || [ ! -x "$binary" ]; then
    printf 'Kanzo binary is not executable; install and verify an upstream release first\n' >&2
    exit 2
fi
version=$("$binary" --version 2>/dev/null || true)
if [ "$version" != "kanzo 0.3.0" ]; then
    printf 'Kanzo 0.3.0 is required for this projection schema; got: %s\n' "${version:-unknown}" >&2
    exit 2
fi
if ! python3 -c '
import json, sqlite3, sys
from pathlib import Path
home = Path(sys.argv[1])
manifest = json.loads((home / "tmcp-projection.json").read_text(encoding="utf-8"))
if manifest.get("schema") != "tmcp-kanzo-projection/v1" or manifest.get("authority") != "terminal-mcp":
    raise SystemExit(1)
with sqlite3.connect(home / "kanzo.db") as conn:
    digest = conn.execute("select value from meta where key = ?", ("tmcp_projection_digest",)).fetchone()
if not digest or digest[0] != manifest.get("digest"):
    raise SystemExit(1)
' "$home_path" 2>/dev/null; then
    printf 'refusing Kanzo home: projection manifest and database do not match\n' >&2
    exit 2
fi

umask 077
config_tmp="$home_path/.config.toml.tmp.$$"
trap 'rm -f "$config_tmp"' EXIT HUP INT TERM
{
    printf 'port = %s\n' "$port"
    printf 'listen = "%s"\n' "$listen"
    printf 'mcp_http = false\n'
} >"$config_tmp"
mv "$config_tmp" "$home_path/config.toml"
trap - EXIT HUP INT TERM

exec "$binary" --no-open --home "$home_path"
