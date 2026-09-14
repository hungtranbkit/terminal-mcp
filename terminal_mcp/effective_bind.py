"""Effective (RUNTIME) bind-state detection -- what this host is ACTUALLY
listening on right now, as opposed to what some config file says it
should be listening on.

Why this exists (backlog blg_316183197b4c): `terminal-mcp-doctor
connection` resolved the controller's LAN endpoint from
`os.environ["TERMINAL_MCP_LAN_BIND"]` read in the DOCTOR's own process.
network_bind.describe_endpoints' docstring claims that "always reflects
what the RUNNING process actually resolved" -- true for the dashboard
route, which runs INSIDE the server process, and false for the CLI,
which is a separate process started from an interactive shell that never
inherited the service's environment. Result on dell-linux: the doctor
printed "lan: not configured (loopback-only)" while the service was in
fact bound to 100.81.85.120:8766. A loopback-only claim about a host
that is actually reachable over an overlay network is the dangerous
direction for this error to point.

WHY NOT JUST READ THE SYSTEMD UNIT. Because static config is not the
truth either, in both directions, and this host demonstrates both:

  * The live unit (terminal-mcp-fed-controller.service) sets no LAN bind
    of its own. It runs an ExecStartPre that GENERATES bind.env at every
    start, writing TERMINAL_MCP_LAN_BIND only when the tailnet address
    genuinely exists on a NIC. No static file can tell you whether that
    happened on this particular boot.
  * The retired unit (terminal-mcp-http.service, inactive) still pins
    TERMINAL_MCP_LAN_BIND=192.168.1.132,100.81.85.120 in its
    override.conf. Verified live: 192.168.1.132 DOES currently exist on
    this host (wlp60s0, an intermittent WiFi address), and NOTHING is
    listening on it, because that unit is not running. A doctor that
    trusted the drop-in would report LAN exposure that does not exist --
    and note that "the address is real" is not the deciding fact, "a
    socket is bound to it" is. (The unit's own comment asserts the host
    "no longer has" that address; that comment is itself stale, which is
    the general hazard of reasoning about runtime from checked-in text.)

So: EXPOSURE IS ONLY EVER REPORTED FROM OBSERVED LISTENING SOCKETS.
Config is carried alongside as DECLARED INTENT, clearly labelled, and
never promoted into the effective state -- `describe_effective_lan`
returns "unknown", never "lan", when socket evidence is missing, even if
config loudly declares a bind. Every returned state carries `source` and
`confidence` so the caller can print where the answer came from.

Evidence comes from /proc/net/tcp and /proc/net/tcp6, which are readable
unprivileged (unlike `ss -p`, which needs root to attribute a socket to
a PID -- and attribution is not needed here: the question is "is this
port exposed off-loopback", not "which process owns it"). Both readers
are injectable so every state below is testable without a real socket.
"""
from __future__ import annotations

import ipaddress
from typing import Callable

# /proc/net/tcp's `st` column. 0A == TCP_LISTEN. Every other state is a
# live or dying CONNECTION, not a listener -- on this host the same port
# also shows 06 (TIME_WAIT) rows, and counting one of those as a bind
# would invent exposure that does not exist.
LISTEN_STATE = "0A"

PROC_NET_TCP = "/proc/net/tcp"
PROC_NET_TCP6 = "/proc/net/tcp6"

# The effective states. Ordered by how much exposure they describe.
STATE_ALL_INTERFACES = "all-interfaces"
STATE_LAN = "lan"
STATE_LOOPBACK_ONLY = "loopback-only"
STATE_NOT_RUNNING = "service-not-running"
STATE_UNKNOWN = "unknown"

Reader = Callable[[], str]


def _decode_hex_address(token: str) -> str | None:
    """/proc/net/tcp* stores the address as 32-bit words in HOST byte
    order (little-endian on every platform this runs on), so each 8-hex
    group is byte-reversed: '0100007F' is 127.0.0.1, not 1.0.0.127.
    Returns None for anything unparseable rather than raising -- one
    malformed row must never take down the whole diagnosis."""
    token = token.strip()
    if len(token) not in (8, 32):
        return None
    try:
        raw = b"".join(
            bytes.fromhex(token[i:i + 8])[::-1] for i in range(0, len(token), 8)
        )
        address = ipaddress.ip_address(raw)
    except ValueError:
        return None
    # ::ffff:127.0.0.1 must classify as the IPv4 loopback it really is,
    # not as some unfamiliar IPv6 address.
    mapped = getattr(address, "ipv4_mapped", None)
    return str(mapped or address)


def parse_listening(text: str, *, port: int) -> tuple[str, ...]:
    """Every address LISTENING on `port` in one /proc/net/tcp* table.
    Deduplicated, order preserved."""
    wanted = f"{port:04X}"
    found: list[str] = []
    for line in text.splitlines()[1:]:  # row 0 is the column header
        fields = line.split()
        if len(fields) < 4 or fields[3].upper() != LISTEN_STATE:
            continue
        local = fields[1]
        if ":" not in local:
            continue
        addr_hex, _, port_hex = local.rpartition(":")
        if port_hex.upper() != wanted:
            continue
        address = _decode_hex_address(addr_hex)
        if address is not None and address not in found:
            found.append(address)
    return tuple(found)


def _read(reader: Reader | None, path: str) -> tuple[str | None, str | None]:
    """(contents, error). Never raises: an unreadable table is missing
    evidence, which this module reports honestly as such."""
    try:
        if reader is not None:
            return reader(), None
        with open(path, encoding="ascii", errors="replace") as handle:
            return handle.read(), None
    except OSError as exc:
        return None, f"{path}: {type(exc).__name__}: {exc}"


def _classify(addresses: tuple[str, ...]) -> str:
    for address in addresses:
        if ipaddress.ip_address(address).is_unspecified:
            return STATE_ALL_INTERFACES
    for address in addresses:
        if not ipaddress.ip_address(address).is_loopback:
            return STATE_LAN
    return STATE_LOOPBACK_ONLY if addresses else STATE_NOT_RUNNING


def _declared(raw: str | None) -> tuple[str, ...]:
    """The DECLARED bind, split but deliberately NOT validated through
    network_bind.resolve_lan_binds: 'auto' and stale/no-longer-present
    addresses must survive into the report as the literal text an
    operator configured, so a mismatch against reality can be shown.
    This value never influences `state`."""
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def describe_effective_lan(*, port: int, declared_lan_bind: str | None = None,
                           proc_net_tcp: Reader | None = None,
                           proc_net_tcp6: Reader | None = None) -> dict:
    """The effective LAN exposure of `port`, from observed sockets.

    `declared_lan_bind` is reported as intent only. It is NEVER able to
    turn an unverified state into "lan" -- see this module's docstring.
    """
    ipv4_text, ipv4_error = _read(proc_net_tcp, PROC_NET_TCP)
    ipv6_text, ipv6_error = _read(proc_net_tcp6, PROC_NET_TCP6)

    addresses: list[str] = []
    for text in (ipv4_text, ipv6_text):
        if text is None:
            continue
        for address in parse_listening(text, port=port):
            if address not in addresses:
                addresses.append(address)

    errors = [error for error in (ipv4_error, ipv6_error) if error]
    state = _classify(tuple(addresses))
    exposed = state in (STATE_LAN, STATE_ALL_INTERFACES)

    if errors and not exposed:
        # Some table was unreadable and nothing we COULD read showed
        # exposure. "loopback-only" would be an under-report of a risk we
        # cannot actually rule out, so this stays explicitly unknown.
        # (Observed exposure is kept even with a partial read: seeing a
        # LAN listener is positive evidence that no missing table undoes.)
        state = STATE_UNKNOWN

    if state == STATE_UNKNOWN:
        source = "unavailable"
        confidence = "unknown"
        detail = ("could not read listening-socket evidence (" + "; ".join(errors) + ")"
                  if errors else "no listening-socket evidence available")
    else:
        read_paths = [path for path, text in ((PROC_NET_TCP, ipv4_text), (PROC_NET_TCP6, ipv6_text))
                      if text is not None]
        source = "listening-sockets(" + ",".join(read_paths) + ")"
        confidence = "effective"
        detail = {
            STATE_ALL_INTERFACES: f"listening on all interfaces on port {port}",
            STATE_LAN: f"listening off-loopback on port {port}",
            STATE_LOOPBACK_ONLY: f"listening on loopback only on port {port}",
            STATE_NOT_RUNNING: f"nothing is listening on port {port}",
        }[state]

    declared = _declared(declared_lan_bind)
    lan_addresses = tuple(a for a in addresses if not ipaddress.ip_address(a).is_loopback)
    loopback_addresses = tuple(a for a in addresses if ipaddress.ip_address(a).is_loopback)

    result = {
        "state": state,
        "source": source,
        "confidence": confidence,
        "detail": detail,
        "port": port,
        "listening": tuple(addresses),
        "lan_addresses": lan_addresses,
        "loopback_addresses": loopback_addresses,
        "declared_lan_bind": declared,
        "declared_source": "TERMINAL_MCP_LAN_BIND in this CLI process's own environment "
                           "(declared intent -- NOT evidence of what the service bound)",
        "evidence_errors": tuple(errors),
    }
    result["mismatch"] = _mismatch(state, lan_addresses, declared)
    return result


def _mismatch(state: str, lan_addresses: tuple[str, ...], declared: tuple[str, ...]) -> str | None:
    """A human-readable disagreement between declared config and live
    sockets, or None. Note `declared` here is the DOCTOR process's own
    environment: the common, benign case on this host is declared=()
    while the service (a different process, with bind.env loaded) is
    genuinely LAN-bound -- which is precisely the bug that motivated
    this module, so it is called out rather than passed over."""
    if state == STATE_UNKNOWN:
        if declared:
            return (f"config declares {', '.join(declared)} but no socket evidence was readable -- "
                    "exposure NOT verified, and deliberately not assumed from config")
        return None
    if state == STATE_NOT_RUNNING:
        if declared:
            return (f"config declares {', '.join(declared)} but nothing is listening -- "
                    "the service is down, so this declares intent only, not exposure")
        return None
    if state == STATE_ALL_INTERFACES:
        return ("listening on ALL interfaces -- this is broader than any declared LAN bind"
                if declared else "listening on ALL interfaces with no declared LAN bind")
    if state == STATE_LAN and not declared:
        return (f"live LAN listener(s) {', '.join(lan_addresses)} with no TERMINAL_MCP_LAN_BIND in "
                "this CLI's environment -- the service's own environment differs from this shell's "
                "(e.g. a systemd EnvironmentFile); trust the sockets, not this shell")
    if state == STATE_LOOPBACK_ONLY and declared:
        return (f"config declares {', '.join(declared)} but only loopback is listening -- "
                "stale config, or the service has not picked it up")
    if state == STATE_LAN and declared:
        # "auto" declares "whichever private address this host has", so
        # it cannot be contradicted by a specific live address -- it
        # neither names a missing one nor makes a live one undeclared.
        auto = "auto" in declared
        extra = [] if auto else [a for a in lan_addresses if a not in declared]
        missing = [d for d in declared if d not in lan_addresses and d != "auto"]
        parts = []
        if missing:
            parts.append(f"declared but NOT listening: {', '.join(missing)}")
        if extra:
            parts.append(f"listening but NOT declared: {', '.join(extra)}")
        return "; ".join(parts) if parts else None
    return None
