"""Linux one-command pairing for the existing enrollment protocol."""
from __future__ import annotations

import re
import shlex

from .enrollment import normalize_code


def build_linux_pair_command(*, controller_url: str, node_id: str, enrollment_code: str) -> str:
    """Build the one command shown after a controller operator approves pairing."""
    origin = str(controller_url or "").rstrip("/")
    if not origin.startswith("https://"):
        raise ValueError("Linux pairing requires a trusted HTTPS controller URL")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", str(node_id or "")):
        raise ValueError("invalid node_id")
    code = normalize_code(enrollment_code)
    if not code:
        raise ValueError("invalid enrollment code")
    script_url = origin + "/enroll/linux-pair.sh"
    return (f"curl --fail --silent --show-error --proto '=https' --tlsv1.2 {shlex.quote(script_url)} | bash -s -- "
            f"{shlex.quote(origin)} {shlex.quote(node_id)} {shlex.quote(code)}")


def render_linux_pair_script() -> str:
    """A generic secret-free script; the code is supplied only at execution."""
    return r'''#!/usr/bin/env bash
set -euo pipefail
CONTROLLER_URL="${1:-}"
NODE_ID="${2:-}"
PAIRING_CODE="${3:-}"
if [[ "$CONTROLLER_URL" != https://* ]]; then
  echo "Pairing requires the controller's trusted HTTPS address." >&2; exit 2
fi
if [[ ! "$NODE_ID" =~ ^[a-z0-9][a-z0-9_-]{0,63}$ || ! "$PAIRING_CODE" =~ ^TMCP-[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}$ ]]; then
  echo "Invalid pairing request. Generate a fresh code in Nodes → Add computer." >&2; exit 2
fi
if [[ "$(uname -s)" != Linux ]] || ! command -v systemctl >/dev/null; then
  echo "This installer requires Linux with systemd." >&2; exit 2
fi
for tool in curl git python3 tmux; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Missing $tool. Install it, then run the pairing command again." >&2; exit 2; }
done
if ! command -v tailscale >/dev/null || ! tailscale ip -4 >/dev/null 2>&1; then
  echo "Tailscale is not connected. Join this machine to the controller's tailnet, then run the same pairing command again." >&2
  exit 3
fi
TS_IP="$(tailscale ip -4 | head -n 1)"
if [[ ! "$TS_IP" =~ ^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\. ]]; then
  echo "Tailscale did not provide a valid tailnet IPv4 address; pairing was not attempted." >&2; exit 3
fi
if [[ -e "$HOME/terminal-mcp" && ! -d "$HOME/terminal-mcp/.git" ]]; then
  echo "~/terminal-mcp exists but is not a git checkout; no pairing changes were made." >&2; exit 5
fi
if [[ ! -d "$HOME/terminal-mcp/.git" ]]; then
  git clone --quiet https://github.com/hungtranbkit/terminal-mcp.git "$HOME/terminal-mcp"
fi
ORIGIN="$(git -C "$HOME/terminal-mcp" remote get-url origin 2>/dev/null || true)"
if [[ "$ORIGIN" != "https://github.com/hungtranbkit/terminal-mcp.git" && "$ORIGIN" != "https://github.com/hungtranbkit/terminal-mcp" ]]; then
  echo "~/terminal-mcp is not the trusted HTTPS Terminal MCP checkout; pairing was not attempted." >&2; exit 5
fi
HOSTNAME_VALUE="$(hostname -s)"
TMP_RESPONSE="$(mktemp)"
chmod 600 "$TMP_RESPONSE"
cleanup() { [[ -z "${TMP_RESPONSE:-}" ]] || rm -f "$TMP_RESPONSE"; unset PAIRING_CODE PAYLOAD; }
trap cleanup EXIT
PAYLOAD="$(TMCP_PAIRING_CODE="$PAIRING_CODE" TMCP_HOSTNAME="$HOSTNAME_VALUE" TMCP_TS_IP="$TS_IP" \
  python3 -c 'import json,os; print(json.dumps({"code":os.environ["TMCP_PAIRING_CODE"],"hostname":os.environ["TMCP_HOSTNAME"],"platform":"linux","addresses":{"tailscale_ip":os.environ["TMCP_TS_IP"]}}))')"
if ! \
  curl --fail --silent --show-error --proto '=https' --tlsv1.2 \
    -H 'Content-Type: application/json' --data "$PAYLOAD" \
    "$CONTROLLER_URL/dashboard/api/enroll/consume" -o "$TMP_RESPONSE"; then
  DIAGNOSTIC="$(python3 - "$TMP_RESPONSE" <<'PY'
import json,pathlib,sys
allowed={"ENROLLMENT_NOT_FOUND","ENROLLMENT_EXPIRED","ENROLLMENT_ALREADY_USED","ENROLLMENT_REVOKED","RATE_LIMITED","REPAIR_HOST_MISMATCH","HTTPS_REQUIRED","NODE_ALREADY_EXISTS"}
try:
 code=json.loads(pathlib.Path(sys.argv[1]).read_text()).get("error","")
 print(code if code in allowed else "PAIRING_REJECTED")
except Exception: print("CONTROLLER_UNREACHABLE")
PY
)"
  echo "Pairing failed ($DIAGNOSTIC). Check the displayed code/expiry, hostname, Tailscale and controller availability." >&2
  exit 4
fi
unset PAIRING_CODE PAYLOAD
python3 - "$TMP_RESPONSE" "$HOME/terminal-mcp/node-agent.token" "$NODE_ID" <<'PY'
import json, os, pathlib, sys
payload=json.loads(pathlib.Path(sys.argv[1]).read_text())
if payload.get("node_id") != sys.argv[3] or not isinstance(payload.get("node_token"),str) or len(payload["node_token"]) != 64:
    raise SystemExit("Controller returned an invalid pairing response")
target=sys.argv[2]
temporary=target+".new"
fd=os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_TRUNC|getattr(os,"O_NOFOLLOW",0), 0o600)
os.fchmod(fd,0o600)
with os.fdopen(fd,"w") as stream: stream.write(payload["node_token"])
os.replace(temporary,target)
PY
rm -f "$TMP_RESPONSE"
unset TMP_RESPONSE
bash "$HOME/terminal-mcp/deploy/install-node-agent.sh" \
  --controller "$CONTROLLER_URL" --node-id "$NODE_ID" --host "$TS_IP" --repo-dir "$HOME/terminal-mcp" --paired
echo "Pairing setup finished. The controller will report Online only after heartbeat and execution checks pass."
'''
