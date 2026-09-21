#!/usr/bin/env bash
# Health + capacity check for a terminal-mcp node agent.
#
# Answers "is this node healthy, or about to start answering HTTP 500 and
# look like a network problem to the controller?" -- the failure mode hit
# on dell-linux 2026-09-21: 108 tmux sessions against TasksMax=256, so
# every request needing a thread died with `can't start new thread` and
# `GET /v1/sessions/<name>/status` returned 500. It read as controller
# flakiness; the network was fine.
#
#   node-agent-health.sh              health snapshot (read-only)
#   node-agent-health.sh --recommend  caps sized from THIS machine
#   node-agent-health.sh --stale      sessions the registry says are dead
#   node-agent-health.sh --quiet      only WARN/CRIT lines
#
# Read-only in every mode. It never starts, kills or reconfigures
# anything -- --recommend prints a command for a human to run.
#
# Exit: 0 OK, 1 WARN (>=80% of a cap, or a cap was hit before), 2 CRIT
# (>=95%, unit not running, or caps too small for the current session
# count). Composes with a systemd timer or a CI step.
set -uo pipefail

UNIT="${NODE_AGENT_UNIT:-terminal-node-agent.service}"
STATE="${TERMINAL_MCP_STATE:-$HOME/.local/state/terminal-mcp}"
WARN_PCT="${WARN_PCT:-80}"
CRIT_PCT="${CRIT_PCT:-95}"
SINCE="${SINCE:-30 min ago}"

# Per-session cost, measured on dell-linux at 110 sessions (490 MB / 218
# tasks in the agent's own cgroup). A session costs 2 tasks -- an `sh` +
# `cat` pair piping the pane into $STATE/raw/<session>.log -- and a few MB
# of pipe buffers. These describe the AGENT's cgroup only; the agent/shell
# processes running inside each tmux pane live outside it and are bounded
# by machine RAM, not by this unit.
TASKS_PER_SESSION=2
MB_PER_SESSION=5
TASKS_BASE=64
MB_BASE=256

MODE=health; QUIET=0
for a in "$@"; do case "$a" in
  --recommend) MODE=recommend;; --stale) MODE=stale;;
  --quiet) QUIET=1;; -h|--help) sed -n '2,25p' "$0"; exit 0;;
esac; done

rc=0
note() { local lvl="$1"; shift
  case "$lvl" in CRIT) rc=2;; WARN) [[ $rc -lt 1 ]] && rc=1;; esac
  [[ "$lvl" != OK || $QUIET -eq 0 ]] && printf '%-4s %s\n' "$lvl" "$*"; return 0; }
say() { [[ $QUIET -eq 0 ]] && printf '%s\n' "$*"; return 0; }
pct() { local u=$1 l=$2; [[ "$l" =~ ^[0-9]+$ ]] || { echo -1; return; }
        [[ "$l" -eq 0 ]] && { echo -1; return; }; echo $(( u * 100 / l )); }
level_for() { local p=$1; [[ $p -lt 0 ]] && { echo OK; return; }
  if [[ $p -ge $CRIT_PCT ]]; then echo CRIT; elif [[ $p -ge $WARN_PCT ]]; then echo WARN; else echo OK; fi; }
sessions_now() { tmux ls 2>/dev/null | wc -l; }

# ---------------------------------------------------------------- recommend
# Caps derived from what this machine can actually hold, not a guess.
# The ceiling on concurrent sessions is machine RAM (each pane may run a
# real agent), so budget a conservative slice of total RAM per session and
# size the agent's own cgroup to comfortably supervise that many.
if [[ $MODE == recommend ]]; then
  total_mb=$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo)
  cpus=$(nproc)
  ram_per_session_mb="${RAM_PER_SESSION_MB:-96}"   # conservative per-pane slice
  by_ram=$(( total_mb / ram_per_session_mb ))
  host_thread_budget=$(( $(cat /proc/sys/kernel/threads-max) / 4 ))
  by_tasks=$(( host_thread_budget / TASKS_PER_SESSION ))
  max_sessions=$(( by_ram < by_tasks ? by_ram : by_tasks ))
  tasks=$(( max_sessions * TASKS_PER_SESSION + TASKS_BASE ))
  mem_mb=$(( max_sessions * MB_PER_SESSION + MB_BASE ))
  # round up to something human
  tasks=$(( ((tasks + 255) / 256) * 256 ))
  mem_gb=$(( (mem_mb + 1023) / 1024 )); [[ $mem_gb -lt 1 ]] && mem_gb=1
  cat <<EOF
machine        ${total_mb} MB RAM, ${cpus} CPU
headroom       ~${max_sessions} concurrent sessions (RAM-bound at ${ram_per_session_mb} MB/session)
in use now     $(sessions_now) sessions

recommended caps for ${UNIT}:
  TasksMax  = ${tasks}      (${TASKS_PER_SESSION}/session + ${TASKS_BASE} base)
  MemoryMax = ${mem_gb}G       (${MB_PER_SESSION} MB/session + ${MB_BASE} MB base)

apply live, WITHOUT a restart (a restart is survivable only because the
unit sets KillMode=process -- do not rely on it):

  systemctl --user set-property ${UNIT} TasksMax=${tasks} MemoryMax=${mem_gb}G

revert with:  systemctl --user revert ${UNIT}
EOF
  exit 0
fi

# -------------------------------------------------------------------- stale
# Sessions alive in tmux that the node's OWN registry no longer considers
# active. This is the only defensible "unused" signal: a pane sitting at a
# bash prompt is a READY session the controller can reuse, not garbage --
# on dell-linux 99 of 110 looked idle that way while the registry called
# every one of them ACTIVE.
if [[ $MODE == stale ]]; then
  db="$STATE/session_registry.db"
  [[ -f "$db" ]] || { note WARN "no session registry at $db"; exit "$rc"; }
  tmux ls -F '#{session_name}' 2>/dev/null > /tmp/.nah-live.$$ || : >/tmp/.nah-live.$$
  python3 - "$db" /tmp/.nah-live.$$ <<'PY'
import sqlite3, sys, pathlib
db, livef = sys.argv[1], sys.argv[2]
live = [s for s in pathlib.Path(livef).read_text().split() if s]
con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
rec = {r[0]: r for r in con.execute(
    "select session_name,status,killed_at,deleted_at,offline_at,last_activity_at from session_records")}
stale, unknown = [], []
for s in live:
    r = rec.get(s)
    if r is None:
        unknown.append(s)
    elif r[3]:  stale.append((s, 'deleted'))
    elif r[2]:  stale.append((s, 'killed'))
    elif (r[1] or '').upper() not in ('ACTIVE', 'RUNNING', ''):
        stale.append((s, (r[1] or '?').lower()))
print(f"live tmux sessions: {len(live)}")
print(f"registry says dead but still running: {len(stale)}")
for s, why in stale: print(f"   REAP  {s}   (registry: {why})")
print(f"running with no registry record at all: {len(unknown)}")
for s in unknown: print(f"   ORPHAN {s}")
if not stale and not unknown:
    print("\nNothing to reclaim: every live session is ACTIVE in the registry.")
    print("Do NOT kill sessions on an idle-looking pane -- terminal-mcp reuses")
    print("them, and a killed session cannot be recreated by hand (it needs")
    print("POST /v1/sessions with grants, or it comes back allowed:false).")
PY
  rm -f /tmp/.nah-live.$$
  exit 0
fi

# ------------------------------------------------------------------- health
state=$(systemctl --user is-active "$UNIT" 2>/dev/null || true)
[[ "$state" == active ]] || { note CRIT "$UNIT is '$state' (expected active)"; exit "$rc"; }
say "unit      $UNIT  active  pid=$(systemctl --user show -p MainPID --value "$UNIT")  restarts=$(systemctl --user show -p NRestarts --value "$UNIT")"

base="/sys/fs/cgroup$(systemctl --user show -p ControlGroup --value "$UNIT")"
pm=max
if [[ ! -d "$base" ]]; then
  note WARN "cgroup not readable: $base (skipping resource checks)"
else
  pc=$(cat "$base/pids.current" 2>/dev/null || echo 0); pm=$(cat "$base/pids.max" 2>/dev/null || echo max)
  pp=$(pct "$pc" "$pm"); note "$(level_for "$pp")" "tasks     ${pc}/${pm}$([[ $pp -ge 0 ]] && echo "  (${pp}%)")"
  mc=$(cat "$base/memory.current" 2>/dev/null || echo 0); mm=$(cat "$base/memory.max" 2>/dev/null || echo max)
  mp=$(pct "$mc" "$mm")
  h() { [[ "$1" =~ ^[0-9]+$ ]] && echo "$(( $1/1024/1024 ))M" || echo "$1"; }
  note "$(level_for "$mp")" "memory    $(h "$mc")/$(h "$mm")$([[ $mp -ge 0 ]] && echo "  (${mp}%)")"
  ph=$(awk '/^max /{print $2}' "$base/pids.events" 2>/dev/null || echo 0)
  mh=$(awk '/^max /{print $2}' "$base/memory.events" 2>/dev/null || echo 0)
  [[ "${ph:-0}" -gt 0 ]] && note WARN "task ceiling hit ${ph}x (pids.events) -- run --recommend" || say "OK   task ceiling never hit"
  [[ "${mh:-0}" -gt 0 ]] && note WARN "memory ceiling hit ${mh}x (memory.events) -- run --recommend" || say "OK   memory ceiling never hit"
fi

if command -v tmux >/dev/null 2>&1; then
  n=$(sessions_now); need=$(( n * TASKS_PER_SESSION + TASKS_BASE ))
  say "sessions  ${n} tmux sessions  (needs ~${need} tasks)"
  [[ "$pm" =~ ^[0-9]+$ && $need -gt $pm ]] && note CRIT "TasksMax=${pm} cannot hold ${n} sessions (needs ~${need}) -- run --recommend"
fi

errs=$(journalctl --user -u "$UNIT" --since "$SINCE" --no-pager 2>/dev/null | grep -c "can't start new thread" || true)
http5=$(journalctl --user -u "$UNIT" --since "$SINCE" --no-pager 2>/dev/null | grep -cE 'HTTP/1\.1" 5[0-9][0-9] ' || true)
[[ "${errs:-0}"  -gt 0 ]] && note CRIT "${errs}x \"can't start new thread\" since ${SINCE} -- node is refusing work" || say "OK   no thread exhaustion since ${SINCE}"
[[ "${http5:-0}" -gt 0 ]] && note WARN "${http5}x HTTP 5xx since ${SINCE}" || say "OK   no HTTP 5xx since ${SINCE}"

exit "$rc"
