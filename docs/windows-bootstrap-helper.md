# Terminal MCP Bootstrap — one-file Windows onboarding

## Current one-file contract

The official `TerminalMCP-Setup.exe` contains one public, trusted HTTPS
controller origin and no pairing handle, node token, SSH private key, or
Tailscale auth key. Renaming the file does not alter setup. A double-click:

1. shows **Install prerequisites** and requests native UAC once;
2. installs/repairs the bootstrap service, then opens the authenticated owner
   page for **Approve secure connection**;
3. accepts an origin-bound loopback nonce plus a single-use pairing handle,
   installs the `ai_coding` Node Agent, and reports **Verify and finish** only
   after the local Agent health endpoint is ready.

Windows pairing requires an explicit checkbox approving controller-scoped SSH
administrator recovery. The elevated principal is sent during the one-time
redeem and persisted as connection metadata; the controller public key remains
in Windows' required `administrators_authorized_keys` file with inheritance
disabled and only SYSTEM/Administrators ACLs. The controller private key is
never included in the installer.

## What a browser actually cannot do

Stating this first, because every design below is shaped by it and because
a plan that pretends otherwise wastes a week.

A web page on an **unmanaged** Windows machine cannot install OpenSSH,
create a service, write a firewall rule, or write under `ProgramData`. No
browser API exposes that, deliberately. Nothing about this architecture
changes that. So:

| | clicks | why |
|---|---|---|
| **First** machine, no helper yet | 1 download + 1 **UAC Yes** | something native has to install the helper |
| **Every later** enrollment on that machine | **1 click, web-only** | the helper is already installed and elevated-capable |
| Enterprise-managed (Intune/GPO) | **0** — helper pre-deployed | see *Zero-touch* below; not a V1 dependency |

The honest target is therefore **one button + one UAC on a new machine,
and one button after that** — not "zero touch on an unmanaged box", which
is not achievable from a browser.

## Flow

```
  Dashboard                          Browser                      Windows machine
  ─────────                          ───────                      ───────────────
  + Add Node → Windows
  [ Kết nối máy này ]  ──click──►  probe 127.0.0.1:<port>/detect ──► helper?
                                        │
                    ┌───────────────────┴────────────────────┐
                    │ helper present                          │ helper absent
                    ▼                                         ▼
        POST .../enrollments/{id}/handle           show ONE step:
        → short-lived one-time handle              [ Cài Terminal MCP Bootstrap ]
                    │                                         │
        terminalmcp://enroll?handle=…&controller=…             download + run once (UAC)
                    │                                         │
                    ▼                                         └──► helper installs itself,
        helper redeems handle over HTTPS                            registers terminalmcp://,
        → enrollment code + controller candidates                   then the page RESUMES
                    │                                              (it re-probes and continues)
                    ▼
        helper elevates (UAC) and runs the same
        setup logic as windows-setup.ps1
                    │
                    ▼
        POST /dashboard/api/enroll/progress  ──► Dashboard shows live stage
```

## The handle, and why the code is not in the URL

A custom-protocol URL is not a private channel. It can land in the
registry's MRU, in browser history, in a crash report. So the URL carries
a **handle**, not the enrollment code:

- 128 bits, **sha256-hashed at rest**, single-use, **120-second TTL**
  (the gap between a click and a helper launching, not longer)
- minted only by an **authenticated operator action** — the same
  `_mutation_guard` as every other dashboard mutation
- redeemed **once**, over HTTPS, for the enrollment code; the second
  redemption of the same handle is refused

So a handle leaking after the fact buys nothing: it is spent, or expired,
usually both. The enrollment code — itself single-use and 15-minute —
never appears in a URL at all.

## Protocol security: what the helper accepts

The helper's attack surface is a URL any web page on the machine can
trigger. The rules are therefore narrow and absolute:

1. **No commands, ever.** The URL grammar is
   `terminalmcp://enroll?handle=<32 hex>&controller=<origin>` and nothing
   else. There is no field that carries a script, a path, an argument
   list, or a package name. A helper that accepted one would be a remote
   code execution primitive dressed as a convenience.
2. **`action` is a closed set** — `enroll`, `repair`, `status`. Unknown
   actions are refused, not ignored.
3. **`controller` must be in the helper's own allowlist**, written at
   install time from the controller that installed it. A page cannot
   point the helper at an attacker's controller, which is the obvious way
   to turn this into "install my software instead".
4. **`handle` must match `^[0-9a-f]{32}$`** before it is used for
   anything — no length-flexible, no unicode, no path separators.
5. The helper does the redemption itself. The browser never sees the
   bootstrap payload, and the helper never accepts one from the page.

`terminal_mcp/bootstrap_protocol.py` implements exactly this and nothing
else; `tests/test_bootstrap_helper.py` is largely a list of URLs that must
be refused.

## Detection: loopback, not protocol-sniffing

There is no reliable way to feature-detect a custom protocol handler from
a page. So the helper runs a **loopback-only** listener and the page
probes it:

- binds `127.0.0.1` **only** — never a LAN address
- answers `GET /detect` with its version and nothing else
- **requires an `Origin` header in the allowlist** on every request, which
  is what stops a hostile page (or DNS rebinding) from driving it
- exposes **no** enroll endpoint over loopback: enrollment arrives only
  via the protocol handler, so a page cannot start an install by fetch

## Getting the binary to the machine

The Dashboard serves the built helper itself. Two routes, both behind
`_read_guard` — the same Cloudflare Access check every other fleet read
route uses, on the Dashboard's own hostname:

    GET /dashboard/api/nodes/onboard/helper            what is published
    GET /dashboard/api/nodes/onboard/helper/{target}   the bytes

`target` is an allowlist (`windows-x64`, `windows-arm64`), never a path the
caller shapes. **No credential travels with the download** — no enrollment
code, no handle, no node token, not in the URL and not in a header. The
helper earns its credential afterwards by redeeming a `terminalmcp://`
handle itself.

Artifacts live under the state directory, versioned, never a temp path:

    <XDG_STATE_HOME>/terminal-mcp/helper/<version>/terminal-mcp-bootstrap.exe
    <XDG_STATE_HOME>/terminal-mcp/helper/<version>/manifest.json

The manifest records version, build SHA, size, SHA256 and `signed`. The hash
is re-checked against the bytes on disk on **every** request rather than
trusted from publish time, and a mismatch is refused rather than served with
a warning: the reason to check at all is that the operator about to run it
elevated cannot check for themselves.

The CTA offers the download only when this controller has actually published
a build. Otherwise it stays hidden and the copy/paste path remains primary —
a button that 404s in front of an operator is worse than no button.

The official build is pinned to `https://terminal-bootstrap.mesflow.net`.
Authentication and one-time approval still occur in the browser; knowing this
public origin grants no enrollment authority.

## Signing

**Unsigned artifacts are marked dev-only, in the UI, in plain words.**
There is no code-signing certificate in this project today. SmartScreen
will warn, and the download panel says so rather than coaching the user to
click past it. When a certificate exists, sign the artifact and the same
panel drops the warning — no other change.

MSIX was considered and rejected for V1: it implies Store distribution or
sideloading policy changes, and an unsigned MSIX cannot install at all. A
plain executable (or, today, the PowerShell installer in helper mode) has
none of those constraints.

## Zero-touch (Intune / GPO) — documented, not depended on

For managed fleets the helper is pre-deployed and the first machine costs
zero clicks. Deploy the helper package with the controller origin
pre-seeded; the Dashboard CTA then finds it on first probe. **V1 does not
depend on this** and works without any management stack.

## Repair / uninstall

Same discipline as the installer: machine-wide `ProgramData`, token 0600
equivalent (SYSTEM + Administrators only), atomic writes, no secret in any
log line. `repair` re-verifies and fixes; `uninstall` removes the helper,
its protocol registration and its loopback task, and nothing the machine's
owner installed.

## What is NOT verified

The new generic-file flow is cross-compiled and exercised in Windows CI. The
existing Windows node has previously passed Node Agent and real Codex session
smoke tests, but this exact build has not yet passed a fresh double-click,
sign-out and reboot acceptance run. The release remains an explicitly unsigned
development build until that run and Authenticode signing are complete.
