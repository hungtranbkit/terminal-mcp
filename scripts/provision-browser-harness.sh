#!/usr/bin/env bash
# TMCP-BROWSER-GATEWAY-001 -- reproducible Browser Use / Browser Harness provisioning.
#
# Browser Use CLI 3.x is Browser Harness-backed; this installs the official
# PyPI `browser-use` distribution into an ISOLATED uv venv on Python 3.12 and
# records the resolved version where the gateway's capability probe reads it.
#
# WHY A DEDICATED VENV: the terminal-mcp service venv must not gain a heavy
# browser stack (and browser-use pins its own deps). The gateway shells out to
# this interpreter, so the runtime dependency is a PATH, never an import.
#
# WHY NOT THE WEBUI: browser-use-webui is a separate, unrelated server surface.
# Terminal MCP stays the only control plane; the WebUI is deliberately NOT
# installed.
#
# RECORDING IS OFF BY DEFAULT: no video/trace/har directory is configured here.
# Recording can only be turned on per call, explicitly, and the gateway itself
# refuses to enable it unless TERMINAL_MCP_BROWSER_ALLOW_RECORDING=1.
#
# Usage
#   scripts/provision-browser-harness.sh                  # install / upgrade
#   scripts/provision-browser-harness.sh --check          # report only, no changes
#   scripts/provision-browser-harness.sh --register-skill # + agent skill files
set -uo pipefail

PREFIX="${TERMINAL_MCP_BROWSER_HOME:-$HOME/.local/share/terminal-mcp/browser-harness}"
VENV="$PREFIX/venv"
PY_VERSION="${TERMINAL_MCP_BROWSER_PYTHON:-3.12}"
MANIFEST="$PREFIX/manifest.json"
CHECK_ONLY=0
REGISTER_SKILL=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --register-skill) REGISTER_SKILL=1 ;;
  esac
done

log() { printf '[browser-harness] %s\n' "$*"; }

report() {
  if [ -x "$VENV/bin/python" ]; then
    local ver
    ver="$("$VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("browser-use"))' 2>/dev/null || echo "")"
    if [ -n "$ver" ]; then
      log "installed: browser-use $ver"
      log "python:    $("$VENV/bin/python" -V 2>&1)"
      log "venv:      $VENV"
      [ -f "$MANIFEST" ] && log "manifest:  $MANIFEST"
      return 0
    fi
  fi
  log "not installed at $VENV"
  return 1
}

if [ "$CHECK_ONLY" = "1" ]; then
  report
  exit $?
fi

command -v uv >/dev/null 2>&1 || { log "FATAL: uv not on PATH (https://docs.astral.sh/uv/)"; exit 2; }

mkdir -p "$PREFIX" || exit 2

log "creating venv on python $PY_VERSION at $VENV"
uv venv --python "$PY_VERSION" "$VENV" || { log "FATAL: uv venv failed"; exit 2; }

log "installing latest stable browser-use (Browser Harness-backed CLI)"
VIRTUAL_ENV="$VENV" uv pip install --python "$VENV/bin/python" --upgrade browser-use || {
  log "FATAL: browser-use install failed"; exit 2; }

RESOLVED="$("$VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("browser-use"))' 2>/dev/null || echo "")"
[ -n "$RESOLVED" ] || { log "FATAL: browser-use not importable after install"; exit 2; }

# The deterministic executor drives Chrome over CDP through Playwright's
# chromium, which browser-use itself depends on. Install the browser binary
# only if the cache has none -- a shared cache is reused, never re-downloaded.
if ! "$VENV/bin/python" -c 'from playwright.sync_api import sync_playwright' 2>/dev/null; then
  log "playwright not present in venv (browser-use variant without it) -- skipping browser install"
else
  log "ensuring chromium binary is present"
  "$VENV/bin/python" -m playwright install chromium >/dev/null 2>&1 || \
    log "WARN: playwright install chromium failed; an existing cache/system Chrome may still work"
fi

CHROME="$(command -v google-chrome || command -v chromium || command -v chromium-browser || true)"

cat > "$MANIFEST" <<JSON
{
  "package": "browser-use",
  "version": "$RESOLVED",
  "python": "$("$VENV/bin/python" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')",
  "venv": "$VENV",
  "interpreter": "$VENV/bin/python",
  "cli": "$VENV/bin/browser-use",
  "system_chrome": "$CHROME",
  "recording_default": "off",
  "webui_installed": false,
  "provisioned_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON

log "wrote $MANIFEST"

# The browser skill teaches Claude/Codex the harness helper vocabulary. It is
# OPT-IN because it is a direct browser path that bypasses Terminal MCP: for
# ChatGPT-driven work the gateway's terminal_browser_* tools are the control
# plane, and this is for an agent working locally in a repo.
if [ "$REGISTER_SKILL" = "1" ]; then
  SKILL_TEXT="$("$VENV/bin/browser-harness" skill 2>/dev/null)"
  if [ -z "$SKILL_TEXT" ]; then
    log "WARN: browser-harness produced no skill text; skipping registration"
  else
    for target in "$HOME/.claude/skills/browser-harness" "$HOME/.codex/skills/browser-harness"; do
      parent="$(dirname "$target")"
      # Only register where the agent is actually installed -- never create
      # a config tree for a tool this box does not have.
      [ -d "$parent" ] || { log "skip $target (no $parent)"; continue; }
      mkdir -p "$target" || continue
      printf '%s\n' "$SKILL_TEXT" > "$target/SKILL.md"
      log "registered skill: $target/SKILL.md"
    done
  fi
fi

report
