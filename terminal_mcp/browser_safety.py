"""URL policy and output hygiene for the browser gateway.

WHY THIS IS A SEPARATE MODULE
-----------------------------
The gateway drives a REAL browser on this host on behalf of a chat. That
makes it the one tool on this surface whose argument is an instruction to
open a network connection from inside the trust boundary -- so the check
that decides which connections are allowed must be readable on its own,
testable without a browser, and impossible to skip by calling a different
entry point. `validate_url` is that single gate: `browser_gateway` never
hands a URL to the worker that has not come back out of this function.

THREE LAYERS, IN THIS ORDER
---------------------------
1. SCHEME. Only http/https ever reach a browser. `file://` would turn a
   page-verification tool into an arbitrary file reader, and
   `javascript:`/`data:`/`blob:`/`view-source:` are script-injection
   shapes that do not need a network at all. Refused by name, never by
   an allowlist a config edit could widen.
2. HARD BLOCKS. Cloud instance-metadata endpoints are refused even when
   an operator has explicitly allowlisted them -- this is the one rule
   config cannot turn off. 169.254.169.254 and friends hand out IAM
   credentials to anything that can issue a GET, and a chat-driven
   browser is exactly that. See `_METADATA_*`.
3. POLICY. Loopback (the actual Phase-1 use case -- verifying a web UI
   this box is serving), private/LAN ranges, and operator allow/deny
   patterns. Deny always beats allow.

DNS IS RESOLVED BEFORE THE BROWSER SEES THE NAME
------------------------------------------------
A hostname is not a destination. `evil.example.com` resolving to
169.254.169.254 defeats a name-only check completely, so the policy
resolves and judges the ADDRESSES. Resolution failures are refused. The worker proxy connects to the validated
numeric address, preventing a second DNS lookup from changing the destination.

OUTPUT HYGIENE
--------------
Everything a page yields -- console lines, network URLs, visible text,
the final URL -- is attacker-influenced text that lands in a chat
transcript. It goes through `redaction.redact_output` (the project's
existing rules, not a second weaker copy) and then through hard length and
count caps, so one page cannot flood a connector's context or smuggle a
token out in a query string.
"""
from __future__ import annotations

import ipaddress
import socket
import re
from dataclasses import replace
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .redaction import redact_output

#: Schemes a browser may ever be pointed at. Not configurable: every other
#: scheme is either a local-file read or a script-execution primitive.
ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

#: Refused by name so the error says WHICH dangerous shape was attempted,
#: rather than a generic "not http". Anything absent from ALLOWED_SCHEMES is
#: refused regardless; this list only improves the message.
BLOCKED_SCHEMES: frozenset[str] = frozenset({
    "file", "data", "javascript", "blob", "about", "view-source",
    "chrome", "chrome-extension", "devtools", "ftp", "ws", "wss",
})

#: Instance-metadata hostnames. Hard block -- see the module docstring.
_METADATA_HOSTS: frozenset[str] = frozenset({
    "metadata.google.internal", "metadata.goog", "metadata",
    "instance-data", "instance-data.ec2.internal",
})

#: Instance-metadata addresses (AWS/GCP/Azure IMDS, ECS task role, Alibaba,
#: and the IPv6 IMDS endpoint). Hard block.
_METADATA_IPS: frozenset[str] = frozenset({
    "169.254.169.254", "169.254.170.2", "169.254.169.123",
    "100.100.100.200", "fd00:ec2::254",
})

#: Link-local as a whole. 169.254.0.0/16 exists to be unrouted and
#: host-local; on a cloud instance it is where the metadata service lives,
#: and there is no legitimate web UI to verify inside it.
_LINK_LOCAL_NETS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
)

#: How many addresses a single hostname may contribute before the policy
#: stops looking. A name with hundreds of A records is not a web UI, and an
#: unbounded loop here would be a cheap way to stall the gateway.
MAX_RESOLVED_ADDRESSES = 16

#: Hard output caps. Deliberately small: a verification RESULT is a verdict
#: plus the evidence for it, not a page dump.
MAX_URL_CHARS = 2_048
MAX_TEXT_CHARS = 4_000
MAX_MESSAGE_CHARS = 500
MAX_MESSAGES = 20
MAX_CHECKS = 50


class UrlRejected(ValueError):
    """A URL the policy refuses. Carries a stable machine-readable `code`
    so a caller can branch on the reason without parsing English."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class UrlPolicy:
    """What this deployment will let a chat point a browser at.

    `allow_loopback` defaults False because it is the Phase-1 use case:
    operators must explicitly permit local dev servers. `allow_private_networks` defaults False for the opposite
    reason -- nothing about "verify a web UI" requires the ability to
    sweep the LAN, and a chat-driven browser that can is a port scanner
    with a nice interface.

    `deny_patterns` beat `allow_patterns`, and neither can reach the
    metadata block above.
    """

    allow_loopback: bool = False
    allow_private_networks: bool = False
    #: Substring patterns (case-insensitive, matched against the full URL).
    #: Empty means "no allowlist" -- the other rules decide.
    allow_patterns: tuple[str, ...] = ()
    deny_patterns: tuple[str, ...] = ()
    resolve_dns: bool = True


def _hard_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if str(ip) in _METADATA_IPS:
        return "instance metadata endpoint"
    for net in _LINK_LOCAL_NETS:
        if ip.version == net.version and ip in net:
            return "link-local address range"
    return None


def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Best-effort address list for `host`. Never raises: see the module
    docstring for why a resolution failure is not a policy decision."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return []
    out: list[Any] = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return out


def validate_url(url: str, policy: UrlPolicy | None = None) -> str:
    """Return the URL to navigate to, or raise `UrlRejected`.

    The returned string is the input with surrounding whitespace removed
    and nothing else changed: a normalizing rewrite here would mean the
    thing checked and the thing navigated to are not the same string,
    which is the classic way a URL filter is bypassed.
    """
    policy = policy or UrlPolicy()
    if not isinstance(url, str) or not url.strip():
        raise UrlRejected("URL_REQUIRED", "a non-empty url is required")
    candidate = url.strip()
    if len(candidate) > MAX_URL_CHARS:
        raise UrlRejected("URL_TOO_LONG",
                          f"url exceeds {MAX_URL_CHARS} characters")
    if any(ch in candidate for ch in ("\n", "\r", "\t", "\x00")):
        raise UrlRejected("URL_INVALID", "url contains control characters")

    if "\\" in candidate or any(ord(ch) < 32 or ord(ch) == 127 for ch in candidate):
        raise UrlRejected("URL_INVALID", "ambiguous URL or control character")
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        raise UrlRejected("URL_INVALID", "invalid host or port") from None
    if parts.username is not None or parts.password is not None:
        raise UrlRejected("URL_INVALID", "URL credentials are not accepted")
    scheme = (parts.scheme or "").lower()
    if not scheme:
        raise UrlRejected("URL_SCHEME_MISSING",
                          "url must start with http:// or https://")
    if scheme in BLOCKED_SCHEMES:
        raise UrlRejected("URL_SCHEME_BLOCKED",
                          f"{scheme}: is never allowed on the browser gateway")
    if scheme not in ALLOWED_SCHEMES:
        raise UrlRejected("URL_SCHEME_BLOCKED",
                          f"only {sorted(ALLOWED_SCHEMES)} are allowed, got {scheme!r}")

    host = (parts.hostname or "").strip().lower().rstrip(".")
    if "%" in host:
        raise UrlRejected("URL_INVALID", "encoded hosts and IPv6 zone IDs are not accepted")
    if not host:
        raise UrlRejected("URL_HOST_MISSING", "url has no host")

    # -- Layer 2: hard blocks, before any allowlist can be consulted. --
    if host in _METADATA_HOSTS:
        raise UrlRejected("URL_METADATA_BLOCKED",
                          f"{host} is an instance metadata endpoint")
    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    addresses = [literal] if literal is not None else (
        _resolve(host) if policy.resolve_dns else [])
    if policy.resolve_dns and not addresses:
        raise UrlRejected("URL_DNS_FAILED", "hostname could not be resolved")
    if host == "localhost" or host.endswith(".localhost"):
        if not policy.allow_loopback:
            raise UrlRejected("URL_LOOPBACK_BLOCKED", "localhost requires allow_loopback")
    addresses = [ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
                 for ip in addresses]
    for ip in addresses:
        reason = _hard_blocked_ip(ip)
        if reason is not None:
            raise UrlRejected("URL_METADATA_BLOCKED",
                              f"{host} resolves to {ip} ({reason})")

    # -- Layer 3: operator policy. Deny wins, then allowlist, then ranges. --
    lowered = candidate.lower()
    for pattern in policy.deny_patterns:
        if pattern and pattern.lower() in lowered:
            raise UrlRejected("URL_DENIED", f"url matches deny pattern {pattern!r}")
    if policy.allow_patterns:
        if not any(p and p.lower() in lowered for p in policy.allow_patterns):
            raise UrlRejected("URL_NOT_ALLOWLISTED",
                              "url does not match any configured allow pattern")
        # Patterns restrict URLs; range permissions remain explicit flags.

    for ip in addresses:
        if ip.is_loopback:
            if not policy.allow_loopback:
                raise UrlRejected("URL_LOOPBACK_BLOCKED",
                                  "loopback targets are disabled by policy")
            continue
        if ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise UrlRejected("URL_ADDRESS_BLOCKED", "non-unicast or reserved address")
        if not ip.is_global:
            if not policy.allow_private_networks:
                raise UrlRejected(
                    "URL_PRIVATE_BLOCKED",
                    f"{host} resolves to private address {ip}; enable "
                    "browser.allow_private_networks or add an allow pattern")
            continue
    return candidate


# ---------------------------------------------------------------------------
# Output hygiene
# ---------------------------------------------------------------------------

def scrub(text: Any, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Redact, then bound. In that order, always.

    Bounding first would let a secret survive by sitting past the cut in a
    string that is then truncated mid-token -- a partial key is still a
    leak, and worse, it no longer matches the redaction rules.
    """
    if text is None:
        return ""
    raw = text if isinstance(text, str) else str(text)
    if not raw:
        return ""
    # Strip URL userinfo even in rejected input and redact every query value,
    # including percent-encoded secret names, before the shared rules.
    raw = re.sub(r"(https?://)[^/\s@]+@", r"\1<REDACTED>@", raw, flags=re.I)
    raw = re.sub(r"([?&][^=&#\s]+)=([^&#\s]*)", r"\1=<REDACTED>", raw)
    cleaned, _report = redact_output(raw)
    if len(cleaned) > limit:
        return cleaned[:limit] + f"...[+{len(cleaned) - limit} chars]"
    return cleaned


def scrub_many(items: Any, *, limit: int = MAX_MESSAGE_CHARS,
               max_items: int = MAX_MESSAGES) -> tuple[list[str], int]:
    """Scrub a list and report how many entries were dropped, rather than
    silently serving a short list -- an operator reading "0 console errors"
    must be able to trust it means zero, not "zero shown"."""
    if not items:
        return [], 0
    rows = list(items)
    kept = [scrub(row, limit) for row in rows[:max_items]]
    return kept, max(0, len(rows) - max_items)


def redaction_applied(*texts: Any) -> bool:
    """True when any of `texts` actually contained something redactable.

    Reported on the result so a reader knows the output was modified; the
    project's own rule that silent redaction is its own bug.
    """
    for text in texts:
        if not text:
            continue
        raw = text if isinstance(text, str) else str(text)
        _cleaned, report = redact_output(raw)
        if report.get("redactions"):
            return True
    return False
