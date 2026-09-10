# Restoring sessions after the Dell host goes off

## dell-5530 (Windows) — automatic, no action needed

The Windows agent has `--controller-url http://192.168.1.132:8766` baked in as
a command-line argument and cannot be repointed without restarting it. Its
ConPTY sessions are **direct children of the agent process** (verified in the
Windows process tree: the `conhost.exe` PIDs have the agent as parent), so a
restart destroys `win1`, `win2` and `wtest` — all of which had live prompts in
flight.

So instead, **m910 adopts the old controller's address** once the Dell host is
gone. dell-5530 then reconnects with *no change on the Windows side at all*
and keeps its sessions.

- `terminal-mcp-ip-takeover.timer` on m910 polls every 60s.
- It **refuses to act** while `192.168.1.132` still answers — ping three times
  over ~15s *and* a TCP probe on port 8766. Two hosts holding one address is an
  ARP conflict that breaks the fleet for everyone, which is worse than
  dell-5530 staying offline a bit longer.
- On adoption it adds `192.168.1.132/24` to `wlp2s0` and appends it to
  `TERMINAL_MCP_LAN_BIND` (a comma-separated list), so `192.168.1.109` keeps
  working throughout.
- Log: `/home/mesflow/.local/state/terminal-mcp/ip-takeover.log`

**Proven, not assumed:** run against an unused address (192.168.1.241), the
controller answered on both that address and its own simultaneously, then the
test address was removed cleanly.

Expect dell-5530 to go `online` within ~2 minutes of the Dell host powering off
(60s timer + 20s heartbeat).

### If it does not adopt

Most likely cause is DHCP handing `192.168.1.132` to another device. Check:
```bash
ssh root-thinkcentre 'tail /home/mesflow/.local/state/terminal-mcp/ip-takeover.log'
```
Fallback is the permanent repoint, which **costs win1/win2/wtest**:
```powershell
# on dell-5530, via: ssh root-thinkcentre  ->  ssh dell-5530
# edit C:\Users\tranv\terminal-mcp\run-node-agent.ps1
#   --controller-url http://192.168.1.132:8766  ->  http://192.168.1.109:8766
Stop-ScheduledTask -TaskName 'TerminalMcpNodeAgent-dell-5530'
Start-ScheduledTask -TaskName 'TerminalMcpNodeAgent-dell-5530'
```
Then recreate the three sessions; each has a `conversation_id` recorded in
`dell5530-session-tails.json`, so `claude --resume <id>` restores the
conversation.

**Also on dell-5530:** there are TWO `windows_agent` processes (PID 12248 spawns
12284) — the known orphaned-Scheduled-Task issue. Worth cleaning during any
maintenance window; the sessions belong to 12284.

## Dell Linux sessions — preserved, not migrated

The 15 tmux sessions on the Dell host cannot be meaningfully recreated on m910:
their working directories are `/home/dell/workspace/…`, and m910 has only 2 of
those repos (`terminal-mcp`, `ai-design-council`) out of ~40GB.

What **is** preserved on m910, under
`/home/mesflow/terminal-mcp-migration-backup/`:

| Artefact | Contents |
| --- | --- |
| `dell-session-scrollback.json` | last 3000 lines of all 15 sessions |
| `claude-transcripts/` | 37 recent Claude conversation transcripts (327MB) |
| `terminal-mcp-migration-backup-*/dell-sessions-manifest.json` | name, cwd, agent, repo, branch per session |
| `terminal-mcp-migration-backup-*/state/` | all 19 controller DBs |

To bring one back later: clone the repo onto m910, copy the matching transcript
into `~/.claude/projects/<encoded-cwd>/`, and start the session with
`claude --resume <uuid>`. The encoded directory name is the absolute cwd with
`/` replaced by `-`, so a different path on m910 needs the directory renamed
to match.

## Uncommitted work still only on the Dell disk

These repos had uncommitted changes when the controller moved. Powering off does
not delete them — but they are unreachable until that machine is on again:

```
xe-ghep-manager 16   projectflow-workspace-manager 6   docmcp-lab 4
ai-qc-agent 2        mesflow 1   mini-factory-tycoon 1
offline-pos 1        serverhub 1
```
Several repos also have **no upstream** at all (`docmcp-lab`,
`mini-factory-tycoon`, `nail-app`, `projectflow`, `promptflow`,
`promptflow-s7-worker`, `xe-ghep-manager`), so nothing of theirs is on a remote.
If any of that matters, commit and push **before** powering the machine off.
