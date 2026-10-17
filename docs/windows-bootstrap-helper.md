# Terminal MCP current-machine join

## User-visible flow

The Dashboard `Join this machine` action creates an idempotent enrollment for
the browser's current platform. The enrollment code is never returned to the
browser. The Dashboard receives a short-lived, one-use pairing handle bound to
its HTTPS origin.

If the Windows Bootstrap helper is already installed, the page fetches a
fresh loopback challenge and sends `{nonce, handle}` to the helper. The helper
redeems the handle over HTTPS, installs or updates its Windows service using
the normal Windows permission prompt, and reports bounded installation stages.
The page shows success only after the helper reports ready.

If no helper answers, the Dashboard downloads the verified Windows helper
artifact paired to that pending enrollment. The user must open the download
and approve the Windows permission prompt. Downloading is not installation.
The artifact manifest's signature state is shown as recorded; an unsigned
development build may be blocked by Windows.

Linux and macOS use a paired shell installer instead of the Windows helper.
The user opens the downloaded script in Terminal and explicitly confirms its
prompt. It redeems the one-time handle over HTTPS and installs the existing
user-level systemd service or LaunchAgent. It asks for no sudo permission.
The Dashboard reports success after the controller receives that node's
authenticated first heartbeat.

The manual Add Node workflow remains available. Repeating Join with the same
browser idempotency key reuses the enrollment and never reissues a consumed
handle. Retry can mint a fresh handle while the enrollment is pending. Revoke
removes the node credential and invalidates its pending enrollment state.

## Windows helper loopback protocol

The Windows service listens only on `127.0.0.1:47562`. Its routes are:

| Method and path | Purpose |
|---|---|
| `GET /v1/challenge` | Issue a random 32-byte, 90-second, one-use nonce and report product/version/platform metadata. |
| `POST /v1/pair` | Accept `{ "nonce": "…", "handle": "…" }` once and start pairing. |
| `OPTIONS` | Allow only the matching challenge/pair CORS preflight with Private Network Access enabled. |

Requests require the exact loopback `Host`, a secure Dashboard `Origin`, and
an origin-bound nonce. The origin is added to the helper's configured
Dashboard origins after successful redemption. Responses use `no-store`; the
challenge contains no machine identifier or credential. Pairing handles are
not logged. The body is bounded and rejects extra JSON fields, replayed or
expired nonces, and concurrent pairing.

The browser never sends an enrollment code to loopback. The handle is
redeemed by the helper over the configured HTTPS bootstrap controller. On a
machine without the helper, a shell installer redeems the same one-time handle
directly over HTTPS after the user confirms the install.

## Cloudflare boundary

Dashboard APIs remain protected by Cloudflare Access and independently
validate the signed Access assertion in the application. With
`dashboard.access_email_roles` configured, role assignment comes only from
the verified JWT email: viewers may read, operators may join and revoke nodes,
and owners retain the existing owner-only mutation permissions. Configure the
Google identity provider and Access application in the Cloudflare Zero Trust
dashboard; see `deploy/cloudflare/TERMINAL_DASHBOARD_ACCESS.md`.

Machine-facing enrollment and heartbeat requests cannot use the operator's
Access session. Only the explicit routes in
`deploy/cloudflare/bootstrap-ingress.template.yml` belong on the bootstrap
hostname; the Dashboard and helper download remain Access protected.

## Verification limits

Go protocol, enrollment/API, script generation, shell syntax, and dashboard
contract tests run in CI/local verification. This Linux environment cannot
exercise Windows UAC, Windows service control, macOS LaunchAgent registration,
or first-run package-manager prompts. Production Google IdP, Access policies,
and tunnel activation must be verified in Cloudflare before claiming
production readiness.
