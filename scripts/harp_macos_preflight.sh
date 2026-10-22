#!/usr/bin/env bash
# Read-only prerequisite check for Harp on an operator's Mac.
# Usage: bash scripts/harp_macos_preflight.sh [configured-ssh-alias ...]
# No install, no credential reads, no config writes, no tmux modifications.
set -u

failures=0
warn() { printf 'WARN: %s\n' "$*"; }
ok() { printf 'OK: %s\n' "$*"; }
bad() { printf 'FAIL: %s\n' "$*" >&2; failures=$((failures + 1)); }

os="$(uname -s 2>/dev/null || printf unknown)"
arch="$(uname -m 2>/dev/null || printf unknown)"
if [[ "$os" == "Darwin" && "$arch" == "arm64" ]]; then
  ok "macOS Apple Silicon ($arch)"
else
  bad "Harp release 0.1.3 targets macOS Apple Silicon; detected $os/$arch"
fi

if command -v tmux >/dev/null 2>&1; then
  ok "local tmux executable: $(command -v tmux)"
else
  warn "local tmux not installed; Harp local tmux integration cannot be verified"
fi

if command -v ssh >/dev/null 2>&1; then
  ok "ssh client exists"
else
  bad "ssh client missing"
fi

if [[ -d "/Applications/Harp.app" ]]; then
  ok "Harp.app found in /Applications"
else
  warn "Harp.app not yet installed (no install attempted)"
fi

settings="$HOME/.harp/settings.toml"
if [[ -f "$settings" ]]; then
  if awk '
    /^[[:space:]]*\[integrations\.tmux\][[:space:]]*(#.*)?$/ { within=1; next }
    /^[[:space:]]*\[/ { within=0 }
    within && /^[[:space:]]*enabled[[:space:]]*=[[:space:]]*true([[:space:]]*(#.*)?)?$/ { found=1 }
    END { exit !found }
  ' "$settings"; then
    ok "Harp tmux integration enabled in settings.toml"
  else
    warn "Harp tmux integration not enabled in settings.toml"
  fi
else
  warn "Harp settings.toml not created; enable [integrations.tmux] after install"
fi

for alias in "$@"; do
  # Avoid passing SSH options from untrusted positional arguments.
  if [[ "$alias" == -* || -z "$alias" ]]; then
    bad "invalid ssh alias argument"
    continue
  fi
  if ! command -v ssh >/dev/null 2>&1; then
    continue
  fi
  printf 'SSH %s: ' "$alias"
  # Bounded, non-interactive read; does not start or attach a session.
  if result="$(ssh -n -o BatchMode=yes -o ConnectTimeout=5 -- "$alias"       'command -v tmux >/dev/null && tmux list-sessions -F "#{session_name}"' 2>&1)"; then
    ok "remote tmux reachable"
    printf '%s\n' "$result"
  else
    warn "unreachable / auth missing / no tmux: $alias (no changes made)"
  fi
done

printf '\nFleet source of truth: Terminal MCP; Harp remote SSH panes are not an MCP bridge.\n'
if (( failures > 0 )); then
  exit 1
fi
