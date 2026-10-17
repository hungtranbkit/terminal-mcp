# Terminal Dashboard Node Join — P0 Design

## Goal and verified starting point

Give an authenticated operator a responsive Dashboard flow to join the machine currently running the browser. The flow must use Cloudflare Access Google/Gmail SSO at the edge and independently verify the Access JWT in the app, apply an explicit email-to-role allowlist, and never claim an install succeeded without an OS/service confirmation. Existing one-time enrollment, token, heartbeat, and revocation stores are reused where their contracts fit.

Audit of `origin/main` `79d93e8` found: `terminal_mcp/cf_access.py` verifies RS256, issuer, audience and token times; `dashboard.py` already applies that verifier to dashboard reads/mutations. `terminal_mcp/enrollment.py` has hashed, one-time, expiring enrollment codes and pairing handles; a Windows-only Go bootstrap helper installs a Windows service with OS permission; Linux/macOS agent installers and heartbeat transport already exist. Current RBAC is only `operator`, with no email-role allowlist. The dashboard hostname redirects to the configured Cloudflare Access team login; the bootstrap health endpoint responds. These observations do not prove the Access application's IdP/policy or production allowlist. No Cloudflare API credentials were available in the audited workspace.

## Design

### Identity and roles

Cloudflare Access remains responsible for Google identity-provider login. The application trusts only a JWT verified with its configured team JWKS, issuer, audience, expiry and not-before checks. When Access verification is configured, a verified email-role allowlist is mandatory; missing, empty, unknown, malformed, or unverified identities fail closed. Roles are `viewer` (read), `operator` (read and join/revoke nodes), and `owner` (all current dashboard mutations plus join/revoke). Installations without Cloudflare Access configuration retain the existing self-hosted behavior. Never infer a Google identity from an unverified header or browser-supplied email.

### Join this machine

The responsive Dashboard presents a `Join this machine` action and truthful states: helper detected, first-time install required, pairing, installing, ready, retryable failure, revoked. It probes only loopback addresses and does not treat download completion as installation. If a compatible helper is absent, it offers the signed/versioned OS installer and instructions that require the user to launch it and accept any OS permission prompt. The UI does not simulate an install or bypass UAC, sudo, Gatekeeper, or service-manager consent.

An installed helper exposes a loopback-only HTTP interface on an ephemeral/local port. It accepts requests only for an exact configured Dashboard origin, an expected loopback Host, and the correct CORS preflight; challenge responses contain a cryptographically random nonce, short expiry, helper version, and platform. Pairing requires that nonce and origin, is one-use, and is bounded by the existing pairing-handle TTL. A browser cannot supply a controller URL: the helper is pinned to the controller origin. Requests are rate-limited, responses are `no-store`, and URLs/query strings/logs never carry enrollment secrets. Existing enrollment handles and redemption remain the source of one-time provisioning credentials.

First install remains an explicit OS action. Windows uses the existing bootstrap helper. Linux and macOS use their existing supported agent installers/service managers, surfaced as explicit user-run installers; service installation or update reports success only after the native service manager confirms it. Re-running join for the same helper machine identity is an idempotent repair/update, not a duplicate node.

### Node data and lifecycle

Node agents initiate outbound HTTPS heartbeats and supported WSS control connections to the pinned controller. Inventory contains only an explicit allowlist of platform/version, capability names, project identifiers, skill names, and health timestamps/status. It must never include project file contents, prompts, environment values, tokens, credentials, or arbitrary filesystem paths. The controller binds enrollment to a stable random helper machine identifier, rejects conflicting duplicate node claims, supports request idempotency and bounded retry, and revokes enrollment/node credentials through existing revocation mechanisms. Revocation is visible in the Dashboard and prevents future authenticated heartbeats/pairing.

### Cloudflare and delivery

Repository changes can implement JWT policy and provide a reviewed Cloudflare configuration/runbook, but changing a production Access application, Google IdP, DNS, or tunnel requires production Cloudflare credentials and a verified rollback path. No production change is claimed without those credentials. Merge/deploy is allowed only after the applicable tests pass and production configuration/credentials are actually available; otherwise commit the isolated branch and report the blocker precisely.

## Boundaries and decisions

- Preserve the current browser's Dashboard origin as the sole helper origin; never accept a caller-provided origin or controller URL.
- Keep node agent operation endpoints narrow; do not add arbitrary shell or file-read APIs.
- Keep installer privilege elevation interactive and native to the OS.
- Keep one-time handles hashed at rest and do not place them in logs, URL query strings, or persistent browser storage.
- Keep inventory schema allowlisted and metadata-only.

## Acceptance criteria

1. Responsive Dashboard layout exposes the current-machine join action and truthful progress/error/retry states at phone and desktop widths.
2. When configured, unverified and non-allowlisted identities cannot read protected dashboard data or mutate/join; viewer cannot join/revoke; operator/owner can; roles come only from verified Access email.
3. Loopback helper accepts only the pinned origin/loopback Host, consumes short-lived nonce/pairing once, rejects replay, expiry, wrong origin, malformed requests, and remote binds.
4. An absent helper leads to an explicit OS installer/run/permission flow; no silent install is represented or attempted.
5. Successful join is idempotent for the same machine identity; duplicates/conflicts are explicit; retry does not create another node; revoke prevents further use.
6. Heartbeats/inventory are outbound TLS and contain only documented metadata; no prompts, contents, secrets, or arbitrary paths.
7. Unit/API/UI/helper tests cover acceptance criteria; project handoff is updated. Production Cloudflare/edge setup is reported separately from local code evidence.
