"""Linux one-command pairing for the existing enrollment protocol."""
from __future__ import annotations

import re
import shlex

from .enrollment import normalize_code

AGENT_PORT = 8790


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
    # The code travels in the environment of the receiving bash only, never
    # in its argv: /proc/<pid>/cmdline is readable by every local user for
    # the minutes the install runs, /proc/<pid>/environ only by the owner.
    return (f"set -o pipefail; curl --fail --silent --show-error --proto '=https' --tlsv1.2 {shlex.quote(script_url)} | "
            f"TMCP_PAIRING_CODE={shlex.quote(code)} bash -s -- {shlex.quote(origin)} {shlex.quote(node_id)}")


# Secret-free checks an operator runs on the node when pairing stalls. The
# dashboard shows the same list, so the browser and the terminal agree.
def linux_diagnostic_commands(*, agent_host: str = "<tailscale-ip>", agent_port: int = AGENT_PORT) -> list[str]:
    return [
        "systemctl --user status terminal-node-agent --no-pager",
        "journalctl --user -u terminal-node-agent -n 50 --no-pager",
        f"curl -fsS http://{agent_host}:{agent_port}/v1/health",
        "tailscale status",
        "loginctl show-user \"$USER\" --property=Linger",
    ]


def render_linux_pair_script() -> str:
    """A generic secret-free script; the code is supplied only at execution."""
    return r'''#!/usr/bin/env bash
set -euo pipefail
CONTROLLER_URL="${1:-}"
NODE_ID="${2:-}"
# Current commands hand the code over in TMCP_PAIRING_CODE; the positional
# third argument is still accepted for commands copied before that change.
PAIRING_CODE="${TMCP_PAIRING_CODE:-${3:-}}"
unset TMCP_PAIRING_CODE
AGENT_PORT=8790
STARTED_AT="$(date +%s)"
STAGE_REPORTING=0
TS_IP=""
FAILURE_HINT=""

# Best-effort telemetry for the dashboard's status panel. It never decides
# the outcome: a progress POST that fails is ignored, and the code goes to
# curl on stdin, never in its argv.
report_stage() {
  [[ "$STAGE_REPORTING" -eq 1 && -n "${PAIRING_CODE:-}" ]] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  local elapsed=$(( $(date +%s) - STARTED_AT ))
  TMCP_PAIRING_CODE="$PAIRING_CODE" TMCP_STAGE="$1" TMCP_ELAPSED="$elapsed" \
    python3 -c 'import json,os; print(json.dumps({"code":os.environ["TMCP_PAIRING_CODE"],"stage":os.environ["TMCP_STAGE"],"elapsed_seconds":int(os.environ["TMCP_ELAPSED"])}))' \
    | curl --silent --proto '=https' --tlsv1.2 --max-time 10 -o /dev/null \
        -H 'Content-Type: application/json' --data-binary @- \
        "$CONTROLLER_URL/dashboard/api/enroll/progress" >/dev/null 2>&1 || true
}
print_diagnostics() {
  echo "Diagnose on this machine:" >&2
  echo "  systemctl --user status terminal-node-agent --no-pager" >&2
  echo "  journalctl --user -u terminal-node-agent -n 50 --no-pager" >&2
  echo "  curl -fsS http://${TS_IP:-<tailscale-ip>}:$AGENT_PORT/v1/health" >&2
  echo "  tailscale status" >&2
}
cleanup() {
  local status=$?
  if [[ "$status" -ne 0 && "$STAGE_REPORTING" -eq 1 ]]; then
    report_stage failed
    [[ -z "$FAILURE_HINT" ]] || echo "$FAILURE_HINT" >&2
    print_diagnostics
  fi
  [[ -z "${TMP_RESPONSE:-}" ]] || rm -f "$TMP_RESPONSE"
  if [[ -n "${SOURCE_DIR:-}" && -d "$SOURCE_DIR" ]]; then
    git -C "$HOME/terminal-mcp" worktree remove --force "$SOURCE_DIR" >/dev/null 2>&1 || true
  fi
  [[ -z "${WORKTREE_ROOT:-}" ]] || rm -rf "$WORKTREE_ROOT"
  unset PAIRING_CODE PAYLOAD
  return "$status"
}
trap cleanup EXIT

if [[ "$CONTROLLER_URL" != https://* ]]; then
  echo "Pairing requires the controller's trusted HTTPS address." >&2; exit 2
fi
if [[ ! "$NODE_ID" =~ ^[a-z0-9][a-z0-9_-]{0,63}$ || ! "$PAIRING_CODE" =~ ^TMCP-[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}$ ]]; then
  echo "Invalid pairing request. Generate a fresh code in Nodes → Add computer." >&2; exit 2
fi
# Bash and curl are prerequisites for the dashboard command itself.
command -v curl >/dev/null 2>&1 || { echo "Missing curl. Install it, then run the pairing command again." >&2; exit 2; }
# From here on the dashboard hears about every stage, including a failure
# (once python3 exists to encode the report; bootstrap below may add it).
STAGE_REPORTING=1
report_stage starting
if [[ "$(uname -s)" != Linux ]] || ! command -v systemctl >/dev/null; then
  echo "This installer requires Linux with systemd." >&2; exit 2
fi
# -- 0. Bootstrap packages BEFORE redeeming the single-use code.
# Bash and curl are prerequisites for the initial dashboard command.
missing_packages=()
for tool in git python3 tmux; do
  command -v "$tool" >/dev/null 2>&1 || missing_packages+=("$tool")
done
# Debian/Ubuntu may provide python3 without python3-venv.
if command -v python3 >/dev/null 2>&1; then
  venv_probe="$(mktemp -d)"
  if ! python3 -m venv "$venv_probe/check" >/dev/null 2>&1; then
    missing_packages+=("python3-venv")
  fi
  rm -rf -- "$venv_probe"
else
  missing_packages+=("python3-venv")
fi
if (( ${#missing_packages[@]} > 0 )); then
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "Missing packages: ${missing_packages[*]}. Automatic install supports Debian/Ubuntu (apt-get) only; install them manually and rerun setup." >&2
    exit 2
  fi
  echo "Installing missing Terminal MCP dependencies: ${missing_packages[*]}"
  apt_prefix=(env DEBIAN_FRONTEND=noninteractive)
  if (( EUID != 0 )); then
    if ! command -v sudo >/dev/null 2>&1; then
      echo "Missing sudo; install ${missing_packages[*]} with an administrator account and rerun setup." >&2
      exit 2
    fi
    # curl | bash uses stdin for the script. Never let sudo consume it:
    # use cached sudo authentication or the controlling terminal.
    if ! sudo -n -v 2>/dev/null; then
      if [[ ! -r /dev/tty ]] || ! sudo -v </dev/tty; then
        echo "Administrator authorization unavailable. Install ${missing_packages[*]} manually, then rerun setup." >&2
        exit 2
      fi
    fi
    apt_prefix=(sudo env DEBIAN_FRONTEND=noninteractive)
  fi
  if ! "${apt_prefix[@]}" apt-get update -qq || ! "${apt_prefix[@]}" apt-get install -y --no-install-recommends "${missing_packages[@]}"; then
    echo "Package installation failed. Resolve apt/dpkg and rerun setup; pairing code was not consumed." >&2
    exit 2
  fi
fi
for tool in curl git python3 tmux; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Missing $tool after package installation; pairing code was not consumed." >&2; exit 2; }
done
venv_probe="$(mktemp -d)"
if ! python3 -m venv "$venv_probe/check" >/dev/null 2>&1; then
  rm -rf -- "$venv_probe"
  echo "Python venv is unavailable; install the matching python3-venv package and rerun setup. Pairing code was not consumed." >&2
  exit 2
fi
rm -rf -- "$venv_probe"
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
report_stage downloading
if [[ ! -d "$HOME/terminal-mcp/.git" ]]; then
  git clone --quiet https://github.com/hungtranbkit/terminal-mcp.git "$HOME/terminal-mcp"
fi
ORIGIN="$(git -C "$HOME/terminal-mcp" remote get-url origin 2>/dev/null || true)"
if [[ "$ORIGIN" != "https://github.com/hungtranbkit/terminal-mcp.git" && "$ORIGIN" != "https://github.com/hungtranbkit/terminal-mcp" ]]; then
  echo "~/terminal-mcp is not the trusted HTTPS Terminal MCP checkout; pairing was not attempted." >&2; exit 5
fi
# Fetch trusted upstream source into a detached temporary worktree. This keeps
# tracked config, credentials, and every local edit in the user's checkout
# untouched while ensuring the installer and Python package are current.
WORKTREE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/terminal-mcp-pair.XXXXXX")"
SOURCE_DIR="$WORKTREE_ROOT/source"
FRESH_INSTALLER="$WORKTREE_ROOT/install-node-agent.sh"
if ! git -C "$HOME/terminal-mcp" fetch --quiet --no-tags origin refs/heads/main; then
  echo "Could not fetch trusted Terminal MCP source; pairing was not attempted." >&2; exit 5
fi
UPSTREAM_COMMIT="$(git -C "$HOME/terminal-mcp" rev-parse --verify 'FETCH_HEAD^{commit}')"
git -C "$HOME/terminal-mcp" worktree add --quiet --detach "$SOURCE_DIR" "$UPSTREAM_COMMIT"
# Extract the installer from that exact commit and verify the temporary file
# against the commit's Git blob id before executing it.
git -C "$HOME/terminal-mcp" show "$UPSTREAM_COMMIT:deploy/install-node-agent.sh" > "$FRESH_INSTALLER"
chmod 700 "$FRESH_INSTALLER"
EXPECTED_INSTALLER_BLOB="$(git -C "$HOME/terminal-mcp" rev-parse "$UPSTREAM_COMMIT:deploy/install-node-agent.sh")"
ACTUAL_INSTALLER_BLOB="$(git -C "$HOME/terminal-mcp" hash-object "$FRESH_INSTALLER")"
if [[ "$EXPECTED_INSTALLER_BLOB" != "$ACTUAL_INSTALLER_BLOB" ]]; then
  echo "Trusted installer integrity check failed; pairing was not attempted." >&2; exit 5
fi
if ! grep -q -- '--source-dir' "$FRESH_INSTALLER" || ! grep -q -- '--paired' "$FRESH_INSTALLER"; then
  echo "Trusted upstream installer does not support this pairing flow yet; pairing was not attempted." >&2; exit 5
fi
report_stage registering
HOSTNAME_VALUE="$(hostname -s)"
TMP_RESPONSE="$(mktemp)"
chmod 600 "$TMP_RESPONSE"
PAYLOAD="$(TMCP_PAIRING_CODE="$PAIRING_CODE" TMCP_HOSTNAME="$HOSTNAME_VALUE" TMCP_TS_IP="$TS_IP" \
  python3 -c 'import json,os; print(json.dumps({"code":os.environ["TMCP_PAIRING_CODE"],"hostname":os.environ["TMCP_HOSTNAME"],"platform":"linux","addresses":{"tailscale_ip":os.environ["TMCP_TS_IP"]}}))')"
if ! printf '%s' "$PAYLOAD" | \
  curl --fail --silent --show-error --proto '=https' --tlsv1.2 \
    -H 'Content-Type: application/json' --data-binary @- \
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
  case "$DIAGNOSTIC" in
    ENROLLMENT_NOT_FOUND|ENROLLMENT_EXPIRED|ENROLLMENT_ALREADY_USED|ENROLLMENT_REVOKED)
      FAILURE_HINT="This command can no longer be used. Generate a fresh one in Nodes → + Add Node." ;;
    NODE_ALREADY_EXISTS)
      FAILURE_HINT="Node '$NODE_ID' already exists. Choose another name, or use Repair connection on that node." ;;
    REPAIR_HOST_MISMATCH)
      FAILURE_HINT="This repair code is bound to the node's registered hostname. Run it on that machine." ;;
    RATE_LIMITED)
      FAILURE_HINT="Too many attempts. Wait a minute, then run the same command again." ;;
    *)
      FAILURE_HINT="Check that this machine reaches the controller: curl -fsSI $CONTROLLER_URL/enroll/linux-pair.sh" ;;
  esac
  exit 4
fi
unset PAYLOAD
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
# The code is spent now. It stays in memory only to label the remaining
# progress reports, which cannot do anything else with it.
FAILURE_HINT="Node '$NODE_ID' is registered but its agent is not healthy yet. Fix the error above, then use Nodes → $NODE_ID → Repair connection for a fresh code."
report_stage installing_service
bash "$FRESH_INSTALLER" \
  --controller "$CONTROLLER_URL" --node-id "$NODE_ID" --host "$TS_IP" \
  --port "$AGENT_PORT" --repo-dir "$HOME/terminal-mcp" --source-dir "$SOURCE_DIR" --paired
# The installer only proves systemd accepted the unit. Wait for the agent
# itself to answer before telling the dashboard this machine is ready.
HEALTHY=0
for _attempt in $(seq 1 30); do
  if systemctl --user is-active --quiet terminal-node-agent.service \
    && curl --fail --silent --max-time 3 -o /dev/null "http://$TS_IP:$AGENT_PORT/v1/health"; then
    HEALTHY=1; break
  fi
  sleep 2
done
if [[ "$HEALTHY" -ne 1 ]]; then
  echo "The node agent did not become healthy at http://$TS_IP:$AGENT_PORT/v1/health within 60 seconds." >&2
  exit 6
fi
report_stage ready
echo "Pairing setup finished; the local agent is healthy. The dashboard shows Online after the first heartbeat (about 20 seconds)."
'''
