# Terminal MCP Bootstrap — web-only Windows onboarding

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

The helper cannot be executed in this project's environment: there is no
Windows host and no signing certificate. What **is** verified here is the
protocol validation, the handle lifecycle, the backend routes, the
Dashboard states and the generated artifact's syntax. Running the helper
on real Windows is outstanding, and it is marked dev-only until it has
been.
