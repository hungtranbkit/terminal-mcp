# Terminal MCP Dashboard Access and Google SSO checklist

This is an operator runbook. It contains no credentials and does not apply
Cloudflare changes. Production configuration remains unverified until every
verification step below is completed against the actual Zero Trust account.

## Dashboard application

- [ ] In Cloudflare Zero Trust, configure the Google identity provider for the
  intended organization. Require the organization's managed Google accounts;
  do not rely on a user-supplied email query or header.
- [ ] Create or review the Access self-hosted application for
  `terminal-dashboard.mesflow.net`, covering `/dashboard/*` and any dashboard
  API paths served on that hostname. Confirm the login redirect reaches Google
  and that a signed Access JWT is issued for this application.
- [ ] Copy the application's AUD tag and Access team domain into the protected
  controller configuration as `dashboard.cloudflare_access_audience` and
  `dashboard.cloudflare_access_team_domain`. Keep secrets and production
  values out of the repository.
- [ ] Configure `dashboard.access_email_roles` on the controller using
  verified, normalized email addresses and only `viewer`, `operator`, or
  `owner` roles. Add an email to the edge Access allow policy only when it is
  also intended to enter the application allowlist. The application role map
  is the authorization source for Dashboard reads and writes.
- [ ] Check the active configuration has no empty or accidental wildcard
  allowlist. Test an allowed viewer, operator, and owner, plus an unknown
  identity; the unknown identity must be denied by the application.

The application verifies JWT signature, issuer, audience and time claims
itself. Cloudflare's edge login alone is not treated as application
authorization. Operators may join and revoke nodes; viewers cannot. The
existing owner role is required for other privileged Dashboard mutations.

## Bootstrap hostname and tunnel

The dashboard and its binary helper download stay behind Access. A newly
onboarded machine has no Access cookie, so only the routes explicitly listed
in `bootstrap-ingress.template.yml` may be exposed on the machine-facing
bootstrap hostname. Review that template as an allowlist before activation.

- [ ] Verify controller configuration uses the intended HTTPS bootstrap
  origin, while the Dashboard origin remains the Access-protected host.
- [ ] Review the ordered tunnel ingress rules and keep their catch-all 404
  last. Validate with `cloudflared tunnel ingress validate` before reload.
- [ ] Apply the activation procedure in `BOOTSTRAP_ACTIVATION.md`. Test from
  outside the controller network: bootstrap liveness is 200, `/mcp`,
  `/dashboard`, and the helper-download route are 404 on the bootstrap host.
- [ ] Confirm Dashboard requests redirect to Access when unauthenticated and
  that a valid Google-authenticated user reaches the app. Verify an allowed
  identity can join and an unlisted identity cannot read or mutate it.
- [ ] Confirm the machine-facing enrollment and node heartbeat routes work
  without an Access cookie using a disposable test enrollment and node.
- [ ] Check Cloudflare Access logs, tunnel logs, controller audit logs and
  rate-limit behavior. Do not record JWTs, enrollment codes, pairing handles
  or node tokens in tickets or screenshots.

## Rollback

If Dashboard authentication, role resolution, or machine enrollment fails,
restore the last known-good Access application/policy and protected controller
configuration. If bootstrap exposure is broader than its exact route list,
remove the bootstrap hostname's ingress rules first and validate the tunnel
before reloading. Use `BOOTSTRAP_ACTIVATION.md` for hostname rollback and
external 404 checks. Preserve the prior configuration securely for recovery;
never copy its credentials into Git.

## Current audited state

Repository/runtime observations made during the P0 implementation:

- `https://terminal-dashboard.mesflow.net/dashboard` returned HTTP 302,
  consistent with an Access login redirect.
- `https://terminal-bootstrap.mesflow.net/health/live` returned HTTP 200.
- The workspace had no Cloudflare API/Wrangler credential variable names and
  no `wrangler` CLI. A Cloudflare tunnel account certificate and named tunnel
  credentials are present: `cloudflared tunnel list` and `cloudflared tunnel
  info` succeeded. This permits tunnel metadata inspection, but does not
  expose the Cloudflare Access application's Google IdP, audience, or
  allow/deny policies through the available CLI.
- The local account lists an active `terminal-mcp-dashboard` tunnel. Its
  local config routes only `terminal-dashboard.mesflow.net` dashboard paths
  to loopback port 8766; the local current-user systemd unit is inactive, but
  the tunnel reports remote connector connections. Testing that local config
  maps `terminal-bootstrap.mesflow.net` to its catch-all 404 rule. The public
  bootstrap liveness endpoint returns 200, so that hostname is served through
  a different active configuration/tunnel or a separately managed ingress.
- The private `config.yaml` in this local worktree has Access team-domain and
  audience settings, but no `dashboard.access_email_roles` mapping. This is
  a local configuration observation, not evidence of the production host's
  configuration. The public Access login response showed a Cloudflare Access
  sign-in page but did not expose a Google provider in its initial HTML; that
  response alone cannot establish whether Google is enabled behind its
  client-side login flow.
- These public responses do not show the configured Google IdP, Access
  application audience, current allow/deny policy, production email role map,
  or live tunnel configuration. Do not mark production SSO or role policy
  verified based on status codes alone.
