# Onboarding a Windows node

Turn a Windows machine into a Terminal MCP node: download one file, run it
as Administrator, done. The machine appears on the Nodes page, is reachable
over SSH, keeps an independent rescue path, and comes back by itself after
a reboot.

---

## Quick start (3 steps)

1. **Dashboard → Nodes → `+ Add Node` → Windows.** Give it a name (e.g.
   `win-work`), pick a profile, press **Generate setup**.
2. **Download `windows-setup.ps1`** and copy it to the Windows machine.
   Right-click → **Run with PowerShell**. It asks for Administrator itself.
3. **Wait.** The script prints a checklist; the machine shows up on the
   Nodes page within about a minute.

If the operator prefers a command line, the same wizard prints:

```powershell
irm http://<controller>:8766/enroll/windows-setup.ps1 -OutFile setup.ps1
.\setup.ps1 -EnrollmentCode TMCP-XXXXX-XXXXX-XXXXX
```

The enrollment code is **single-use** and expires in ~15 minutes. If it
expires, generate a new one — that is cheaper and safer than extending it.

### Profiles

| Profile | What it installs |
|---|---|
| **Minimal** | OpenSSH Server + Client, connectivity, node registration. Nothing else. |
| **Developer** | Minimal, plus Git, PowerShell 7, Python 3.12, Node.js LTS, GitHub CLI (winget). |
| **AI Coding** | Developer, plus the Claude Code, Codex and OpenCode CLIs (npm). |

AI CLIs need an interactive sign-in on the machine itself. The installer
puts the binaries there; it reports **Needs sign-in** rather than claiming
the node is ready to run agents. Terminal MCP never stores an AI token.

---

## Architecture

```
                      PRIMARY  (Tailscale, or LAN)
   controller  ─────────────────────────────────────────►  Windows node
                                                              sshd :22
                                                          (key-only auth)
                      RESCUE  (reverse SSH, node-initiated)
   controller ──► gateway VPS ◄──────── -R 127.0.0.1:P:127.0.0.1:22 ───┘
                  127.0.0.1:P             held open by a Scheduled Task
```

**Primary.** If this controller is itself on a tailnet, the node joins the
same tailnet and that address becomes its primary transport. If it is not,
the node's LAN address is used instead. No router or NAT change is ever
required, and inbound TCP 22 is firewalled to the source ranges in
`nodes.onboarding.ssh_firewall_cidrs` (default: Tailscale's `100.64.0.0/10`
— *not* "any").

**Rescue.** The node dials **out** to a gateway host and holds a reverse
tunnel open. This is the path that still works when Tailscale is broken,
logged out, or not used at all. Properties that are enforced in code, not
just documented:

- the reverse listener binds `127.0.0.1` **on the gateway** — never the
  wildcard address, and the key's `permitlisten="127.0.0.1:<port>"` makes
  sshd refuse anything else even if the node asked for it;
- each node gets its own port from a persistent, collision-free allocator
  (`rescue.db`, `UNIQUE` port, `BEGIN IMMEDIATE`), and re-running the
  installer returns the same port rather than leaking a new one;
- `ExitOnForwardFailure=yes` + `ServerAliveInterval`/`ServerAliveCountMax`
  mean a tunnel that cannot forward **exits** instead of looking connected,
  and the supervising loop rebuilds it with bounded backoff;
- host keys are pinned. `StrictHostKeyChecking=yes` against a known_hosts
  file that holds only the gateway key the controller shipped. Never `no`.

**Persistence.** Both the rescue tunnel and the heartbeat run from Scheduled
Tasks with an **AtStartup** trigger and a **SYSTEM** principal, so they run
before any user logs in and return after every reboot.

**Cloudflare.** Not part of V1 and not required. The existing
`cloudflared`-based access in this repo remains available for emergency
human access; there is no toggle for it in this wizard on purpose — adding
a third transport would make onboarding harder to support, not easier.

---

## Server configuration

All of it lives under `nodes.onboarding` in `config.yaml`. **Everything is
optional**: with none of it set, onboarding still works over the LAN with a
manual Tailscale login and no rescue tunnel, and says so.

```yaml
nodes:
  onboarding:
    enabled: true
    enrollment_ttl_seconds: 900        # 60..86400
    # What the installer calls back to. Leave empty to derive it from the
    # controller's own tailnet address, else from the request's Host.
    controller_url: "http://100.117.214.87:8766"
    # PUBLIC key installed into the node's administrators_authorized_keys
    # so this controller can SSH in. Defaults to ~/.ssh/id_ed25519.pub.
    controller_ssh_public_key_file: "/home/mesflow/.ssh/id_ed25519.pub"
    agent_port: 8790
    heartbeat_interval_seconds: 30
    ssh_firewall_cidrs: ["100.64.0.0/10"]

    tailscale:
      enabled: true
      # NAME of the env var holding an auth key -- never the key itself.
      # Unset => the installer prints an interactive `tailscale up` step.
      auth_key_env: TERMINAL_MCP_TAILSCALE_AUTH_KEY
      tags: []
      unattended: true
      login_server: ""                 # Headscale, if you run one

    rescue:
      enabled: false                   # <- off until a gateway exists
      gateway_host: "gw.example.net"
      gateway_port: 22
      gateway_user: "tunnel"
      # ONE known_hosts line. Public. Get it with:
      #   ssh-keyscan -t ed25519 gw.example.net
      gateway_host_key: "gw.example.net ssh-ed25519 AAAA..."
      port_range_start: 22000
      port_range_end: 22999
      keepalive_interval_seconds: 30
      keepalive_count_max: 3
      retry_seconds: 15
```

The loader **refuses** `tailscale.auth_key` and any `rescue.*private_key*`
/ `*password*` field outright, rather than accepting and redacting them.

### Standing up a rescue gateway

Any host with a public address and sshd. One unprivileged account, used for
nothing else:

```bash
# on the gateway
sudo adduser --disabled-password --gecos "" tunnel
sudo -u tunnel mkdir -p /home/tunnel/.ssh && sudo -u tunnel chmod 700 /home/tunnel/.ssh
# sshd: keep the reverse listeners on loopback (this is the default, make it explicit)
echo 'GatewayPorts no' | sudo tee -a /etc/ssh/sshd_config
sudo systemctl reload ssh
# the host key line to paste into gateway_host_key:
ssh-keyscan -t ed25519 gw.example.net
```

Then, on the controller, for each enrolled node the dashboard stages one
`authorized_keys` line (public key, `restrict,port-forwarding,permitlisten`).
**Dashboard → Nodes → node → Kết nối** shows them, and
`GET /dashboard/api/nodes/onboard/gateway` returns the combined file plus
the one-line sync command:

```bash
scp ~/.local/state/terminal-mcp/rescue-authorized-keys/authorized_keys \
    tunnel@gw.example.net:~/.ssh/authorized_keys
```

Re-run that after every enrollment and every Remove Node. The controller
does **not** push this by itself: it holds no gateway credential, by
design. Until the line is installed the installer reports the tunnel as
*"task installed, but the gateway has not accepted this key yet"* rather
than claiming success.

**Status of this piece in this deployment: code complete, not
production-verified.** There is no gateway host configured here, so
`rescue.enabled` is `false` and the rescue path has been exercised against
generated argv, the allocator, the probe classifier and the disabled state
— not against a live VPS.

---

## Security model

| | |
|---|---|
| **Enrollment code** | 75 bits, single-use (atomic `UPDATE … WHERE status='pending'`), ~15 min TTL, revocable, **sha256-hashed at rest**. The downloadable script carries this and nothing else. |
| **Node bearer token** | Minted server-side at consume time, returned exactly once over the enrollment response, stored 0600 (controller: `connections.db` token file; node: `node.token`, ACL SYSTEM+Administrators). |
| **Tailscale auth key** | Read from an env var at consume time, delivered once, never written to the script, to disk on the node, or to a log. |
| **Rescue keypair** | Generated **on the node**. The private half never leaves it. The controller only ever sees the public half. |
| **Controller SSH key** | Public key only. |
| **SSH on the node** | Key-only (`PasswordAuthentication no`, via a drop-in we own so the operator's own sshd_config survives). Inbound 22 firewalled to configured CIDRs. |
| **Host keys** | Pinned both directions. No `StrictHostKeyChecking=no` anywhere. |
| **Audit** | `node_enrollment_created` / `_rejected` / `_revoked`, `node_enrolled`, `node_removed` — every payload through `redaction.redact_text` first. |
| **Route auth** | Operator routes: Cloudflare Access + CSRF/Origin, same as every other dashboard mutation. Machine routes: the enrollment code (consume) or the node's own bearer token (heartbeat, deregister). The script download carries no secret and needs no auth. |
| **Rate limit** | 12 consume attempts per source address per minute. |

A node's claimed addresses are validated, not trusted: an address outside
`100.64.0.0/10` is recorded as LAN even if the node called it a Tailscale
address.

---

## Repair and removal

**Repair** — re-check and fix everything, using the credentials already on
disk, without consuming a code:

```powershell
C:\ProgramData\TerminalMCP\... # wherever you saved it
.\windows-setup.ps1 -Repair
```

It re-verifies sshd (service + Automatic + key-only), the controller's
authorized_key, the firewall rule, Tailscale's unattended login, the rescue
tunnel task, the heartbeat task, and ends with a live heartbeat to the
controller. Anything already correct is left alone.

**Uninstall** — remove what Terminal MCP installed, and only that:

```powershell
.\windows-setup.ps1 -Uninstall
```

Removes both Scheduled Tasks, the controller's authorized_key line (leaving
any other key in the file), the firewall rule, `C:\ProgramData\TerminalMCP`,
and asks the controller to deregister the node. It does **not** uninstall
OpenSSH, Tailscale, Git, Python, Node, or any AI CLI — that is the
machine's own software.

**Remove Node** (from the Dashboard) revokes the node's credentials, its
pending enrollment codes, its rescue port and staged gateway key, and its
transport records. It does not try to reach the machine, because a node
being removed is usually a node that is already gone. Re-sync the gateway
`authorized_keys` afterwards.

**Test Primary / Test Rescue** (Dashboard → node → Kết nối) run the real
resolver and write the result back into the transport rows, so the health
column is a record of what happened rather than a live guess.

---

## Troubleshooting

**"This setup must run as Administrator" / exit code 3.**
The script self-elevates. If UAC was declined, or the file was piped rather
than saved, run it from disk: right-click → Run with PowerShell, or
`Start-Process powershell -Verb RunAs -ArgumentList '-ExecutionPolicy','Bypass','-File','<path>'`.

**Exit code 1 — sshd failed.**
`Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`, then
`Start-Service sshd`, then re-run with `-Repair`. On Server SKUs the
capability may need the Windows Update source to be reachable.

**"Tailscale: no auth key from the controller".**
Expected when `tailscale.auth_key_env` is unset. Run `tailscale up
--unattended` once on the machine; after that it survives reboots and
`-Repair` reports OK. This is the recommended posture when you do not want
a reusable auth key in flight.

**Tailscale unavailable entirely.**
The node still enrols and is still reachable on its LAN address and (if
configured) through the rescue tunnel. The Add Node form says why up front
rather than generating an installer that cannot work.

**Gateway unavailable / "the node's tunnel is down".**
Test Rescue distinguishes three cases by design: *gateway unreachable*
(DNS/route/timeout), *nothing listening on the reverse port* (the node is
not holding the tunnel open), and *rescue not configured*. Look at
`C:\ProgramData\TerminalMCP\logs\rescue-tunnel.log` on the node; the loop
records each exit and its backoff. The most common first-time cause is a
gateway `authorized_keys` that has not been synced yet.

**Enrollment refused.**
`ENROLLMENT_ALREADY_USED` — the code was consumed (possibly by an earlier
attempt on the same machine); generate a new one. `ENROLLMENT_EXPIRED` —
generate a new one. `ENROLLMENT_REVOKED` — someone revoked it from the
Nodes page. `RATE_LIMITED` — wait 60 seconds.

**Node offline after reboot.**
Check the two tasks:

```powershell
Get-ScheduledTask -TaskName 'TerminalMCP-*' |
  Select-Object TaskName, State, @{n='Principal';e={$_.Principal.UserId}}
Get-ScheduledTask -TaskName 'TerminalMCP-*' | Get-ScheduledTaskInfo
```

Both must be `SYSTEM` with an `AtStartup` trigger. `-Repair` re-registers
them. Then `C:\ProgramData\TerminalMCP\logs\heartbeat.log` for why pushes
are failing (most often: the controller URL is not reachable from that
network — check `nodes.onboarding.controller_url`).

**The node shows online but sessions cannot be created on it.**
Expected on the Minimal profile: it has connectivity, SSH and a heartbeat,
but no `terminal-node-agent` process. Install the agent with
`deploy/install-node-agent.ps1` when you want full session management on
that machine; the two are independent.
