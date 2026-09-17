"""One gate for "may this controller send a bearer token to this URL?".

The bug this closes (blg_0393ee9bd3d2): `nodes.remote[].endpoint` accepted
any non-empty string. `http://a-public-host:8790` was therefore a valid
node endpoint, and every request to it -- heartbeat verification, session
listing, create/kill -- carries `Authorization: Bearer <node token>` in
the clear. RemoteNodeClient already verifies TLS correctly for `https://`;
only the scheme check was missing.

The rule
--------
`https://`  always allowed. TLS is what protects the token, and it does
            so regardless of where the host is.
`http://`   allowed ONLY when every address the host resolves to is
            non-public. Plaintext is fine on a wire that cannot leave the
            operator's own network.
`local`     the in-process node sentinel, which is not a URL at all.
anything else is refused.

What counts as non-public
-------------------------
Deliberately spelled out rather than delegated to `ipaddress.is_private`,
which disagrees with this project in both directions (it calls 127/8
private, and it does not cover CGNAT):

  loopback        127.0.0.0/8, ::1            never leaves the machine
  RFC1918         10/8, 172.16/12, 192.168/16
  CGNAT           100.64.0.0/10 (RFC 6598)    Tailscale's range
  IPv6 ULA        fc00::/7
  link-local      169.254.0.0/16, fe80::/10
  operator ranges TERMINAL_MCP_TRUSTED_VPN_CIDRS

Two of those need justifying, because they differ from the nearest
existing helper (`lan_discovery.is_lan_scannable`):

* **loopback is allowed here, and is not LAN-scannable.** The two answer
  different questions. Scanning 127.0.0.1 is meaningless; sending a token
  to it is completely safe. `http://127.0.0.1:8790` is how the local node
  agent is reached in development and must keep working.

* **CGNAT is allowed here without TERMINAL_MCP_TRUSTED_VPN_CIDRS being
  set.** That env var governs which overlay ranges may be *bound* and
  *scanned* -- a trust decision about inbound exposure. This gate answers
  a narrower question: could this token reach the public internet?
  100.64.0.0/10 is RFC 6598 shared address space and is not globally
  routable, so the answer is no regardless of that variable. Requiring it
  here would also break every existing deployment whose tailnet endpoints
  are configured but whose CLI shell does not export the variable.

Fail-closed cases
-----------------
A hostname that does not resolve, resolves to nothing, or resolves to a
MIX of public and private addresses is refused for `http://`. The mixed
case matters: trusting whichever address was tried first is exactly the
DNS-rebinding shape `remote_connect.validate_hostname_or_ip` already
guards against, and the same reasoning applies to an endpoint URL.

Errors name the node, the endpoint and the remedy. They never include a
token -- this module is never given one.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from .lan_discovery import TrustedCidrError, trusted_vpn_cidrs

# The in-process node. Not a URL, and deliberately still accepted.
LOCAL_ENDPOINT = "local"

SCHEME_HTTP = "http"
SCHEME_HTTPS = "https"

# RFC 6598 shared address space -- Tailscale's default range. Python's
# `is_private` does NOT cover it, which is why it is named explicitly.
CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")


class EndpointPolicyError(ValueError):
    """Carries a machine-readable `reason` so a caller can branch without
    matching on message text."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


REASON_EMPTY = "endpoint_empty"
REASON_SCHEME = "endpoint_scheme_not_supported"
REASON_NO_HOST = "endpoint_has_no_host"
REASON_UNRESOLVABLE = "endpoint_host_unresolvable"
REASON_PUBLIC_PLAINTEXT = "endpoint_plaintext_to_public_host"
REASON_MIXED = "endpoint_host_resolves_public_and_private"


def is_private_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address,
                       extra_cidrs: tuple = ()) -> bool:
    """True when sending plaintext to this address cannot reach the public
    internet. See the module docstring for why this is spelled out rather
    than delegated to `ipaddress.is_private`."""
    if address.is_loopback or address.is_link_local or address.is_unspecified:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        if address in CGNAT_V4:
            return True
        # is_private covers 10/8, 172.16/12, 192.168/16 (and more).
        if address.is_private:
            return True
        for network in extra_cidrs:
            try:
                if address in network:
                    return True
            except TypeError:
                continue  # a v6 network cannot contain a v4 address
        return False
    # IPv6: ULA fc00::/7 is what `is_private` means here, plus mapped v4.
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        return is_private_address(mapped, extra_cidrs)
    return bool(address.is_private)


def resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address a host resolves to, v4 and v6. An IP literal resolves
    to itself without a lookup. Raises EndpointPolicyError when the name
    cannot be resolved at all -- fail closed, never 'assume private'."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise EndpointPolicyError(
            REASON_UNRESOLVABLE,
            f"host {host!r} could not be resolved ({exc.strerror or exc}); "
            "refusing plaintext http:// to a host whose address is unknown") from None
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addresses:
        raise EndpointPolicyError(REASON_UNRESOLVABLE,
                                  f"host {host!r} did not resolve to any usable address")
    return addresses


def validate_node_endpoint(endpoint: str, *, context: str = "endpoint",
                           allow_public_http: bool = False) -> str:
    """THE chokepoint. Returns the endpoint unchanged, or raises
    EndpointPolicyError with an actionable message.

    `context` is what the caller calls this endpoint -- a node id, a
    config path like `nodes.remote[2].endpoint` -- and appears in the
    error so an operator knows which one to fix.

    `allow_public_http` exists for one caller only: a test, or a future
    explicitly-documented override. It is NOT wired to any config key
    today; see the report accompanying this change for the open question
    about whether one is wanted.
    """
    raw = (endpoint or "").strip()
    if not raw:
        raise EndpointPolicyError(REASON_EMPTY, f"{context} is required and must be a non-empty string")
    if raw == LOCAL_ENDPOINT:
        return raw

    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    if scheme not in (SCHEME_HTTP, SCHEME_HTTPS):
        raise EndpointPolicyError(
            REASON_SCHEME,
            f"{context} must be an http:// or https:// URL (got {scheme or 'no'} scheme in {raw!r})")
    host = parts.hostname
    if not host:
        raise EndpointPolicyError(REASON_NO_HOST, f"{context} has no host: {raw!r}")

    if scheme == SCHEME_HTTPS or allow_public_http:
        return raw

    try:
        extra = trusted_vpn_cidrs()
    except TrustedCidrError:
        # A malformed operator allowlist must not silently widen or narrow
        # this gate; fall back to the built-in private ranges only.
        extra = ()
    addresses = resolve_host(host)
    private = [a for a in addresses if is_private_address(a, extra)]
    public = [a for a in addresses if a not in private]
    if not public:
        return raw

    listed = ", ".join(str(a) for a in public[:4])
    if private:
        raise EndpointPolicyError(
            REASON_MIXED,
            f"{context}: {raw!r} resolves to both private and public addresses ({listed}). "
            "Refusing: which one a request lands on is not something this controller can pin down, "
            "so the bearer token could travel in the clear. Use https://, or an address that is "
            "only reachable on your own network.")
    raise EndpointPolicyError(
        REASON_PUBLIC_PLAINTEXT,
        f"{context}: {raw!r} is plaintext http:// to a public address ({listed}). "
        "Every request to a node carries its bearer token, so this would send that token "
        "unencrypted over the internet. Use https:// for a public host, or point this at the "
        "node's private/VPN address (RFC1918, 100.64.0.0/10 Tailscale, or loopback).")


def describe_policy() -> dict:
    """What this gate allows, for docs and for the doctor output -- so the
    rule is readable from one place instead of inferred from behaviour."""
    return {
        "https": "always allowed",
        "http": "allowed only when every resolved address is non-public",
        "private_ranges": ["127.0.0.0/8", "::1", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                           "100.64.0.0/10", "fc00::/7", "169.254.0.0/16", "fe80::/10"],
        "operator_ranges_env": "TERMINAL_MCP_TRUSTED_VPN_CIDRS",
        "fail_closed": ["unresolvable host", "host resolving to both public and private addresses"],
        "local_sentinel": LOCAL_ENDPOINT,
    }
