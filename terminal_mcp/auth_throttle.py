"""Rate limiting and lockout for node-agent bearer authentication.

THREAT MODEL

* **Brute force.** The node token is a shared secret presented on every
  request. Without a throttle an attacker who can reach port 8790 may try
  candidates as fast as the network allows, and the agent will answer each one
  truthfully and immediately.
* **Burst / resource exhaustion.** Even without guessing correctly, an
  unbounded stream of bad credentials costs the agent a comparison and a
  response per attempt, and — if the throttle itself is unbounded — one
  tracking entry per source address. A spoofed source per packet would then
  exhaust memory through the very mechanism meant to protect it.
* **Replay.** Out of scope for this module and stated so deliberately: a
  bearer token is inherently replayable by whoever holds it. This throttles
  guessing, it does not make a stolen token safe. Transport confidentiality
  (LAN/VPN/tunnel, never the public internet) is what limits capture.

WHAT THIS IS NOT

It is not a defence against an attacker who already has the token, and not a
substitute for network placement. It raises the cost of guessing from
"unlimited attempts per second" to a handful, then an exponential wait.

DESIGN NOTES THAT MATTER

* **The policy is webauth's**, imported rather than redefined. Two auth
  surfaces with independently drifting lockout rules is how one of them ends
  up effectively unprotected without anyone noticing.
* **Monotonic clock.** Lockouts are measured with `time.monotonic()`, so
  moving the system clock — NTP correction, a VM resume, an attacker with
  local time access — cannot shorten a lockout.
* **In memory, not SQLite.** A node agent must boot and serve on a box whose
  state directory is read-only or full; that is a documented requirement
  elsewhere in this file's neighbours. The cost is that a restart clears
  lockouts, which is recorded as a known limitation rather than hidden.
* **Nothing derived from the credential is stored or logged.** Not the
  presented token, not its length, not a prefix, not a hash. A throttle that
  logs what was tried hands an attacker with log access exactly what they
  failed to guess.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from .webauth import (
    RATE_LIMIT_BACKOFF_BASE_SECONDS,
    RATE_LIMIT_BACKOFF_CAP_SECONDS,
    RATE_LIMIT_THRESHOLD,
)

# The global ceiling exists so the throttle cannot itself become the
# exhaustion vector: an attacker varying the source address every packet would
# otherwise mint an unbounded number of tracking entries. When the table is
# full, new SOURCES are refused rather than admitted, because admitting them
# is what the attacker wants; existing entries continue to be served normally
# so a legitimate caller already known to the table is unaffected.
DEFAULT_MAX_TRACKED_SOURCES = 4096

# How long an entry with no recent failure is kept before it is reclaimed.
# Long enough that a slow guesser cannot reset by waiting out the table,
# short enough that a transient client does not occupy a slot forever.
ENTRY_TTL_SECONDS = 3600.0

UNKNOWN_SOURCE = "unknown"


@dataclass
class _Entry:
    failures: int = 0
    locked_until: float = 0.0          # monotonic deadline, 0 = not locked
    last_seen: float = 0.0
    lockouts: int = 0                  # how many times this source has tripped


@dataclass(frozen=True)
class Decision:
    """What the caller should do, and what to say about it.

    `retry_after` is deliberately the ONLY quantitative detail exposed. It says
    when to come back; it says nothing about whether the token was close, how
    many attempts remain, or whether this source is otherwise known.
    """

    allowed: bool
    retry_after: float = 0.0
    reason: str = ""

    def headers(self) -> dict[str, str]:
        if self.allowed or self.retry_after <= 0:
            return {}
        return {"Retry-After": str(max(1, int(round(self.retry_after))))}


def client_key(remote_addr: str | None, *,
               forwarded_for: str | None = None,
               trust_forwarded: bool = False) -> str:
    """The throttle key for a request's source.

    `X-Forwarded-For` is IGNORED unless the deployment explicitly says a
    trusted proxy sits in front. Honouring it by default would let any caller
    pick their own throttle bucket — and therefore never be throttled — by
    sending a different value each time. That is not a theoretical bypass; it
    is the first thing an attacker tries against a header-keyed limiter.

    IPv6 is normalised so that the many textual spellings of one address
    (`::1`, `0:0:0:0:0:0:0:1`, `[::1]`, a v4-mapped `::ffff:10.0.0.1`) share a
    single bucket rather than handing out a fresh allowance per spelling.
    """
    candidate = remote_addr
    if trust_forwarded and forwarded_for:
        # Left-most entry is the originating client per RFC 7239 conventions.
        candidate = forwarded_for.split(",")[0].strip() or remote_addr
    if not candidate:
        return UNKNOWN_SOURCE
    return _normalise_address(candidate)


def _normalise_address(raw: str) -> str:
    text = raw.strip()
    if text.startswith("[") and "]" in text:          # [::1]:8790
        text = text[1:text.index("]")]
    elif text.count(":") == 1:                         # 10.0.0.1:8790
        text = text.split(":", 1)[0]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return text or UNKNOWN_SOURCE
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


class AuthThrottle:
    """Per-source failure tracking with exponential backoff.

    Thread-safe: a node agent serves concurrent requests, and two parallel
    wrong guesses must count as two failures rather than racing into one.
    """

    def __init__(self, *, threshold: int = RATE_LIMIT_THRESHOLD,
                 backoff_base_seconds: float = RATE_LIMIT_BACKOFF_BASE_SECONDS,
                 backoff_cap_seconds: float = RATE_LIMIT_BACKOFF_CAP_SECONDS,
                 max_tracked_sources: int = DEFAULT_MAX_TRACKED_SOURCES,
                 entry_ttl_seconds: float = ENTRY_TTL_SECONDS,
                 clock: Any = time.monotonic) -> None:
        self.threshold = max(1, int(threshold))
        self.backoff_base_seconds = float(backoff_base_seconds)
        self.backoff_cap_seconds = float(backoff_cap_seconds)
        self.max_tracked_sources = max(1, int(max_tracked_sources))
        self.entry_ttl_seconds = float(entry_ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    # -- the two calls a caller makes ---------------------------------------

    def check(self, key: str) -> Decision:
        """May this source attempt authentication right now?

        Called BEFORE the token is compared, so a locked-out source never
        reaches the comparison at all.
        """
        now = self._clock()
        with self._lock:
            self._reclaim(now)
            entry = self._entries.get(key)
            if entry is None:
                if len(self._entries) >= self.max_tracked_sources:
                    # Table full. Refuse the unknown source rather than admit
                    # it: admitting is what an address-spoofing attacker wants.
                    return Decision(False, retry_after=self.backoff_base_seconds,
                                    reason="THROTTLE_TABLE_FULL")
                return Decision(True)
            entry.last_seen = now
            remaining = entry.locked_until - now
            if remaining > 0:
                return Decision(False, retry_after=remaining, reason="LOCKED_OUT")
            return Decision(True)

    def record_failure(self, key: str) -> Decision:
        """One failed attempt. Returns the decision for the NEXT attempt."""
        now = self._clock()
        with self._lock:
            self._reclaim(now)
            entry = self._entries.get(key)
            if entry is None:
                if len(self._entries) >= self.max_tracked_sources:
                    return Decision(False, retry_after=self.backoff_base_seconds,
                                    reason="THROTTLE_TABLE_FULL")
                entry = _Entry()
                self._entries[key] = entry
            entry.failures += 1
            entry.last_seen = now
            if entry.failures < self.threshold:
                return Decision(True)
            backoff = min(self.backoff_cap_seconds,
                          self.backoff_base_seconds * (2 ** (entry.failures - self.threshold)))
            entry.locked_until = now + backoff
            entry.lockouts += 1
            return Decision(False, retry_after=backoff, reason="LOCKED_OUT")

    def record_success(self, key: str) -> None:
        """A correct token clears this source's history.

        Recovery is automatic and immediate: an operator who mistyped a token
        five times is not locked out once they get it right, and no manual
        intervention is ever required to clear a lockout.
        """
        with self._lock:
            self._entries.pop(key, None)

    # -- introspection, for operators and tests ------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Counts only. Never a token, never anything derived from one."""
        now = self._clock()
        with self._lock:
            locked = sum(1 for e in self._entries.values() if e.locked_until > now)
            return {"tracked_sources": len(self._entries), "locked_sources": locked,
                    "max_tracked_sources": self.max_tracked_sources,
                    "threshold": self.threshold,
                    "backoff_cap_seconds": self.backoff_cap_seconds}

    def _reclaim(self, now: float) -> None:
        """Drop entries that are idle and not locked. Caller holds the lock."""
        if len(self._entries) < self.max_tracked_sources:
            # Only pay for this when the table is under pressure; an unbounded
            # sweep on every request would make the throttle the bottleneck.
            return
        stale: Iterable[str] = [
            key for key, entry in self._entries.items()
            if entry.locked_until <= now and (now - entry.last_seen) > self.entry_ttl_seconds
        ]
        for key in stale:
            self._entries.pop(key, None)
