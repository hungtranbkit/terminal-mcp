"""Unit coverage for cf_access.py's JWT verification -- the security-
critical piece of P1 hardening item #2. _jwks_client is monkeypatched to
a stub returning a known test RSA keypair's public key, so these tests
exercise the REAL jwt.decode() signature/audience/expiry verification
without any network access."""
from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from terminal_mcp import cf_access

TEAM_DOMAIN = "test-team.cloudflareaccess.com"
AUDIENCE = "test-application-aud-tag"


@pytest.fixture(scope="module")
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(autouse=True)
def _stub_jwks(monkeypatch, keypair):
    _private, public_key = keypair
    stub_client = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=public_key))
    monkeypatch.setattr(cf_access, "_jwks_client", lambda team_domain: stub_client)


def _token(keypair, *, aud=AUDIENCE, iss=f"https://{TEAM_DOMAIN}", exp_delta=3600,
          email="operator@example.com", **extra_claims) -> str:
    private_key, _public = keypair
    now = int(time.time())
    claims = {"aud": aud, "iss": iss, "exp": now + exp_delta, "iat": now, "nbf": now,
             "email": email, "sub": "user-123", **extra_claims}
    return jwt.encode(claims, private_key, algorithm="RS256")


def test_valid_assertion_verifies_and_returns_identity(keypair):
    token = _token(keypair)
    identity = cf_access.verify_access_assertion(token, team_domain=TEAM_DOMAIN, audience=AUDIENCE)
    assert identity is not None
    assert identity.email == "operator@example.com"
    assert identity.subject == "user-123"


def test_missing_token_never_verifies():
    assert cf_access.verify_access_assertion(None, team_domain=TEAM_DOMAIN, audience=AUDIENCE) is None
    assert cf_access.verify_access_assertion("", team_domain=TEAM_DOMAIN, audience=AUDIENCE) is None


def test_wrong_audience_fails(keypair):
    token = _token(keypair, aud="some-other-applications-aud-tag")
    assert cf_access.verify_access_assertion(token, team_domain=TEAM_DOMAIN, audience=AUDIENCE) is None


def test_wrong_issuer_fails(keypair):
    token = _token(keypair, iss="https://a-different-team.cloudflareaccess.com")
    assert cf_access.verify_access_assertion(token, team_domain=TEAM_DOMAIN, audience=AUDIENCE) is None


def test_expired_token_fails(keypair):
    token = _token(keypair, exp_delta=-60)  # expired one minute ago
    assert cf_access.verify_access_assertion(token, team_domain=TEAM_DOMAIN, audience=AUDIENCE) is None


def test_token_signed_by_a_different_key_fails(keypair):
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    token = jwt.encode(
        {"aud": AUDIENCE, "iss": f"https://{TEAM_DOMAIN}", "exp": now + 3600, "iat": now},
        other_key, algorithm="RS256",
    )
    assert cf_access.verify_access_assertion(token, team_domain=TEAM_DOMAIN, audience=AUDIENCE) is None


def test_malformed_token_fails_closed_never_raises():
    assert cf_access.verify_access_assertion("not-a-real-jwt-at-all", team_domain=TEAM_DOMAIN, audience=AUDIENCE) is None


def test_identity_never_leaks_extra_claims_as_trusted_fields(keypair):
    # email/subject are the only claims this project reads as trusted --
    # everything else in the token (raw_claims) is available but never
    # implicitly treated as identity by callers.
    token = _token(keypair, email=None)
    identity = cf_access.verify_access_assertion(token, team_domain=TEAM_DOMAIN, audience=AUDIENCE)
    assert identity is not None
    assert identity.email is None
    assert "aud" in identity.raw_claims
