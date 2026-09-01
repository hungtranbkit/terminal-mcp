"""Cloudflare Access identity verification -- P1 hardening item #2.

The tunnel/DNS setup only makes Cloudflare Access *redirect* an
unauthenticated browser to a login page at the edge; that says nothing to
THIS application about a request that does arrive here, because that trust
is purely topological ("this origin is only reachable through the tunnel
today" is a deployment fact, not something any code here checks). Real
edge-identity verification means cryptographically checking the
Cf-Access-Jwt-Assertion header Access attaches to every request once a
browser has completed its login -- signature verified against the Access
team's own published JWKS, audience pinned to this specific Access
Application, expiry/not-before enforced. See dashboard.py for where this
is wired into the mutation-route gate; see config.py's DashboardConfig for
the (opt-in, no-op-unless-configured) team_domain/audience settings.

Fails closed, always: any missing header, network error fetching the
JWKS, signature mismatch, wrong audience, or expired/premature token all
come back as "not verified" (None) -- never raises out to the caller,
never partially trusts a claim it could not fully verify.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKClient

# One JWKS client per team_domain, refetched (PyJWKClient's own internal
# cache handles that) at most once per this TTL -- Access rotates signing
# keys infrequently, and a short TTL just means an extra cheap GET on the
# rare cache-miss path, never a security weakening.
_JWKS_CACHE_TTL_SECONDS = 3600
_jwks_clients: dict[str, tuple[float, PyJWKClient]] = {}
_jwks_lock = threading.Lock()


def _jwks_client(team_domain: str) -> PyJWKClient:
    now = time.monotonic()
    with _jwks_lock:
        cached = _jwks_clients.get(team_domain)
        if cached is not None and now - cached[0] < _JWKS_CACHE_TTL_SECONDS:
            return cached[1]
    # Constructed outside the lock (a network-fetching constructor should
    # never hold it) -- a benign race just means two threads each build one
    # on a simultaneous cache miss, and the last one to store wins; nothing
    # unsafe about that here.
    client = PyJWKClient(f"https://{team_domain}/cdn-cgi/access/certs", cache_keys=True)
    with _jwks_lock:
        _jwks_clients[team_domain] = (now, client)
    return client


@dataclass(frozen=True)
class AccessIdentity:
    """The subset of a verified Access assertion's claims this project
    actually uses. `email` is the identity recorded in dashboard-mutation
    audit metadata (see dashboard.py) -- the smallest-equivalent identity
    model item #1 asks for: not a full viewer/operator/approver role
    system (this app has no user/session store to build one on), but every
    dashboard-driven mutation is now attributable to a specific verified
    identity rather than an anonymous "whoever reached the tunnel"."""
    email: str | None
    subject: str | None
    raw_claims: dict[str, Any]


def verify_access_assertion(token: str | None, *, team_domain: str, audience: str) -> AccessIdentity | None:
    if not token or not isinstance(token, str):
        return None
    try:
        client = _jwks_client(team_domain)
        signing_key = client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=f"https://{team_domain}",
            options={"require": ["exp", "iat", "aud"]},
        )
    except Exception:
        # Deliberately broad: PyJWT raises a family of distinct exceptions
        # (InvalidSignatureError, ExpiredSignatureError, InvalidAudienceError,
        # DecodeError, ...) plus PyJWKClientError/network errors from the
        # JWKS fetch itself -- all of them mean the same thing to a caller
        # here: this assertion did not verify, full stop.
        return None
    return AccessIdentity(
        email=claims.get("email") if isinstance(claims.get("email"), str) else None,
        subject=claims.get("sub") if isinstance(claims.get("sub"), str) else None,
        raw_claims=claims,
    )
