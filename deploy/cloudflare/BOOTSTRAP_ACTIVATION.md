# Activating the public bootstrap hostname

**Nothing in this repository performs any step below.** Every one of them
needs a human who has decided to do it. The file exists so that decision is
made against a written plan and a written way back, rather than in a shell at
the moment it seems necessary.

## What this buys, and what it does not

A machine being onboarded for the first time has no Cloudflare Access session
and cannot obtain one — there is no human at a browser on it, and the point of
the feature is that there does not need to be. The three enrollment routes are
built for that: machine-facing, no Access guard, each authenticating its caller
by the one-time code or handle it presents, rate-limited in the application.

This hostname exposes those three and liveness. It does **not** expose the
helper download: that stays behind Access on the Dashboard hostname. So the
flow it enables is *operator downloads the helper from the Dashboard and
carries it to the new machine, and that machine can then complete its
enrollment over the public hostname* — not *a bare machine fetches a binary
from the open Internet*. Those are different decisions with different blast
radii, and only the first one is on the table here.

## Preconditions

- [ ] `deploy/cloudflare/bootstrap-ingress.template.yml` reviewed in a diff by
      someone other than whoever wrote it
- [ ] `pytest tests/test_bootstrap_ingress_template.py` green on the commit
      being activated (28 assertions: the allowlist, the anchoring, the
      catch-all, and that no admin route matches)
- [ ] the controller reachable from the tunnel on its loopback port
- [ ] a decision on rate limits at the edge, because the application's own
      limiter is now the only thing between the open Internet and the
      enrollment routes

## Apply

1. **DNS.** Create the record for `${BOOTSTRAP_HOSTNAME}` pointed at the
   tunnel. Nothing else changes yet: without ingress rules the tunnel answers
   404 for it, which is a safe intermediate state and a good place to stop and
   look.

2. **Ingress.** Merge the template's three rules into the tunnel config
   **above** its existing catch-all. Order matters: a catch-all above a real
   rule swallows it.

3. **Validate before reloading.**

       cloudflared tunnel ingress validate
       cloudflared tunnel ingress url https://${BOOTSTRAP_HOSTNAME}/mcp

   The second must resolve to `http_status:404`. If it resolves to the
   controller, stop — the rules landed in the wrong order and `/mcp` is about
   to be public.

4. **Reload**, then verify from outside the network, not from the controller:

       curl -sS https://${BOOTSTRAP_HOSTNAME}/health/live          # 200
       curl -sS -o /dev/null -w '%{http_code}\n' \
            https://${BOOTSTRAP_HOSTNAME}/mcp                      # 404
       curl -sS -o /dev/null -w '%{http_code}\n' \
            https://${BOOTSTRAP_HOSTNAME}/dashboard                # 404
       curl -sS -o /dev/null -w '%{http_code}\n' \
            https://${BOOTSTRAP_HOSTNAME}/dashboard/api/nodes/onboard/helper/windows-x64
                                                                   # 404

   A 200 on any of the last three means the hostname is exposing more than
   this plan authorised. Roll back immediately; do not "fix it forward".

## Rollback

Reversible at every step, and the earlier steps are the cheap ones.

| Went wrong | Undo |
|---|---|
| Ingress rules wrong, tunnel reloaded | Remove the three rules, `ingress validate`, reload. The hostname returns 404 for everything. Fastest and sufficient for almost every mistake. |
| Tunnel will not start after the edit | Restore the previous config file, reload. Keep a copy before editing — `cp config.yml config.yml.pre-bootstrap` — so this is one command and not a reconstruction. |
| A private route was exposed, even briefly | Remove the rules first, then treat it as an exposure: nothing here authenticates by network position, so the blast radius is the routes' own guards — but check the access log for what actually arrived. |
| Enrollment routes abused from the open Internet | Remove the rules. The application's rate limiter and the one-time/expiring nature of codes and handles are the standing defences; an edge rate limit is the durable fix. |
| Need to stop entirely | Delete the DNS record. The tunnel keeps running for every other hostname. |

**The rollback does not require a controller restart.** Nothing about this
changes how the controller runs; it only changes what the edge forwards. That
is deliberate — a change that needs a production restart to undo is a change
people hesitate to undo.

## After

- [ ] a first real enrollment completed over the public hostname, end to end
- [ ] the access log read once, deliberately, to see what the open Internet
      sends at a new hostname in its first hours
- [ ] this checklist updated with anything that surprised you
