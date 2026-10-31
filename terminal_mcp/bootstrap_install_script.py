"""Paired, user-run Linux/macOS installer for a machine without the helper."""
from __future__ import annotations

import re

from .bootstrap_protocol import normalize_origin


def render_install_script(*, platform: str, handle: str, controller: str,
                          dashboard_origin: str) -> str:
    if platform not in ("linux", "macos"):
        raise ValueError("paired shell installers support linux and macos")
    if not re.fullmatch(r"[0-9a-f]{32}", handle):
        raise ValueError("pairing handle is invalid")
    controller_origin = normalize_origin(controller)
    if not controller_origin.startswith("https://"):
        raise ValueError("machine-facing controller must use HTTPS")
    dashboard = normalize_origin(dashboard_origin)
    installer = "install-node-agent-macos.sh" if platform == "macos" else "install-node-agent.sh"
    return f'''#!/usr/bin/env bash
set -euo pipefail

echo "Terminal MCP {platform} node installer"
echo "This will redeem a short-lived one-time pairing and install a user service."
read -r -p "Continue on this machine? [y/N] " answer
case "$answer" in y|Y|yes|YES) ;; *) echo "Cancelled; no changes made."; exit 0 ;; esac

if ! command -v python3 >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1; then
  echo "Python 3 and git are required. Install them with your OS package manager, then run this installer again." >&2
  exit 1
fi

REPO_DIR="${{HOME}}/terminal-mcp"
if [[ ! -f "$REPO_DIR/pyproject.toml" ]]; then
  if [[ -e "$REPO_DIR" ]]; then
    echo "Refusing to use $REPO_DIR because it is not a Terminal MCP checkout." >&2
    exit 1
  fi
  git clone --depth 1 https://github.com/hungtranbkit/terminal-mcp.git "$REPO_DIR"
fi

export TMCP_PAIR_HANDLE='{handle}'
export TMCP_PAIR_CONTROLLER='{controller_origin}'
export TMCP_PAIR_DASHBOARD_ORIGIN='{dashboard}'
export TMCP_PAIR_PLATFORM='{platform}'
IFS=$'\\t' read -r NODE_ID CONTROLLER_URL NODE_TOKEN < <(python3 - <<'PY'
import json, os, re, secrets, socket, stat, sys, urllib.request
from pathlib import Path

state = Path.home() / ".local" / "state" / "terminal-mcp"
state.mkdir(mode=0o700, parents=True, exist_ok=True)
state.chmod(0o700)
identity = state / "machine-id"
if identity.is_symlink():
    raise SystemExit("Refusing a symlinked helper identity file")
if identity.exists():
    machine_id = identity.read_text(encoding="ascii").strip()
else:
    machine_id = secrets.token_urlsafe(32)
    fd = os.open(identity, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as stream:
        stream.write(machine_id + "\\n")
body = json.dumps({{
    "handle": os.environ["TMCP_PAIR_HANDLE"],
    "dashboard_origin": os.environ["TMCP_PAIR_DASHBOARD_ORIGIN"],
    "machine_id": machine_id,
    "hostname": socket.gethostname(),
    "platform": os.environ["TMCP_PAIR_PLATFORM"],
}}).encode()
request = urllib.request.Request(
    os.environ["TMCP_PAIR_CONTROLLER"].rstrip("/") + "/dashboard/api/enroll/redeem",
    data=body, headers={{"Content-Type": "application/json"}}, method="POST")
with urllib.request.urlopen(request, timeout=30) as response:
    result = json.loads(response.read().decode("utf-8"))
token = str(result.get("node_token") or "")
node_id = str(result.get("node_id") or "")
controller = str(result.get("controller_url") or "")
if not re.fullmatch(r"[A-Za-z0-9_-]{{32,256}}", token) or not re.fullmatch(r"[A-Za-z0-9_-]{{1,64}}", node_id):
    raise SystemExit("Controller returned an incomplete enrollment response")
if not controller.startswith("https://"):
    raise SystemExit("Controller did not provide an HTTPS heartbeat endpoint")
sys.stdout.write("\\t".join((node_id, controller, token)) + "\\n")
PY
)
unset TMCP_PAIR_HANDLE TMCP_PAIR_CONTROLLER TMCP_PAIR_DASHBOARD_ORIGIN TMCP_PAIR_PLATFORM
if [[ -z "${{NODE_ID:-}}" || -z "${{NODE_TOKEN:-}}" ]]; then
  echo "Pairing failed or expired. Return to the Dashboard and retry Join this machine." >&2
  exit 1
fi
TERMINAL_MCP_ONBOARDING_NODE_TOKEN="$NODE_TOKEN" \\
  "$REPO_DIR/deploy/{installer}" --controller "$CONTROLLER_URL" --node-id "$NODE_ID" \\
    --repo-dir "$REPO_DIR" --onboarding
unset NODE_TOKEN
'''
