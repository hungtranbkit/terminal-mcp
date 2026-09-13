#!/usr/bin/env bash
# Shared contract for every agent procedure script.
#
# The output contract exists because an agent's context is the scarce
# resource: a successful run says PASS and where the log is, and nothing
# else. A long green log carries no information and crowds out what does.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOG_DIR="${TERMINAL_MCP_PROC_LOGS:-${XDG_STATE_HOME:-$HOME/.local/state}/terminal-mcp/procedure-logs}"
mkdir -p "$LOG_DIR"
STAGE="start"

log_file() { echo "$LOG_DIR/$1-$(date -u +%Y%m%dT%H%M%SZ).log"; }

pass() { echo "PASS stage=$STAGE ${1:-}"; exit 0; }
fail() { echo "FAIL stage=$STAGE ${1:-}"; exit 1; }

# Fail fast with the stage that broke, so a caller knows where to look
# without reading anything.
trap 'echo "FAIL stage=$STAGE error=unexpected line=$LINENO"; exit 1' ERR
