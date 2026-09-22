"""What the process is ACTUALLY listening on, and how we know.

THE DEFECT THIS CLOSES

`doctor connection` reported `lan: not configured (loopback-only)` on a host
whose controller was, at that moment, listening on 192.168.1.109:8766 and
100.117.214.87:8766. It was not a parsing bug. `describe_endpoints` reads
`os.environ["TERMINAL_MCP_LAN_BIND"]` -- out of the DOCTOR's environment. The
doctor is a separate CLI process; the server is started by systemd with that
variable set in its unit or a drop-in. So the doctor was reading a different
process's environment and reporting it as the server's binding, under a
comment claiming it "always reflects the RUNNING process's real binding, never
a guess". It was exactly a guess, and a confident one.

A diagnostic that says a thing is off while it is on is worse than one that
says nothing: it is the kind of answer people act on.

SOURCE-OF-TRUTH PRECEDENCE

  1. RUNTIME  the kernel's own list of listening sockets. This is ground
              truth: the socket either exists or it does not.
  2. CONFIG   the resolved env/unit value, used ONLY when runtime cannot be
              inspected, and always labelled as configuration rather than
              observation.
  3. UNKNOWN  neither available. Said plainly; never downgraded to
              "not configured", because "I could not look" and "I looked and
              there is nothing" are different facts that call for different
              actions.

When runtime and config disagree, both are reported along with the drift. A
stale config that no longer matches the running process is a finding in its
own right -- it is what an operator is about to edit.

WHY NOT A LOCALHOST PROBE

Connecting to 127.0.0.1:port proves only that something answers on loopback.
It cannot distinguish loopback-only from wildcard from a specific LAN bind,
which is the entire question. Every reader here enumerates the bind ADDRESSES
rather than testing reachability.

READERS, IN ORDER OF PREFERENCE

`/proc/net/tcp{,6}` first: no external binary, no root, present on every Linux
including containers. `ss` and `netstat` are fallbacks for hosts without procfs
(and for future non-Linux support). Each reader reports which one answered, so
a surprising result can be traced to how it was obtained.
"""
from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# Sources, as stable strings: they reach the doctor output, the dashboard and
# the JSON payload, and a caller may branch on them.
SOURCE_PROC = "proc_net_tcp"
SOURCE_SS = "ss"
SOURCE_NETSTAT = "netstat"
SOURCE_CONFIG = "config"
SOURCE_UNAVAILABLE = "unavailable"

# States the LAN question can be in.
LAN_BOUND = "lan_bound"
LOOPBACK_ONLY = "loopback_only"
NOT_CONFIGURED = "not_configured"
UNKNOWN = "unknown"

# TCP_LISTEN, as procfs spells it.
_TCP_LISTEN_HEX = "0A"


@dataclass
class Listener:
    """One listening socket, as observed."""

    address: str
    port: int
    family: str          # "ipv4" | "ipv6"
    source: str

    @property
    def is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.address).is_loopback
        except ValueError:
            return False

    @property
    def is_wildcard(self) -> bool:
        """0.0.0.0 / :: -- bound to every interface, so LAN-reachable even
        though no specific LAN address appears anywhere."""
        try:
            return ipaddress.ip_address(self.address).is_unspecified
        except ValueError:
            return False

    @property
    def is_routable(self) -> bool:
        """Reachable from off-box: a wildcard bind, or any non-loopback,
        non-link-local address."""
        if self.is_wildcard:
            return True
        try:
            parsed = ipaddress.ip_address(self.address)
        except ValueError:
            return False
        return not (parsed.is_loopback or parsed.is_link_local)

    def as_dict(self) -> dict[str, Any]:
        return {"address": self.address, "port": self.port, "family": self.family,
                "source": self.source, "loopback": self.is_loopback,
                "wildcard": self.is_wildcard, "routable": self.is_routable}


def _hex_to_ipv4(value: str) -> str:
    """procfs writes the address little-endian: `6D01A8C0` is 192.168.1.109."""
    raw = int(value, 16)
    octets = [(raw >> shift) & 0xFF for shift in (0, 8, 16, 24)]
    return ".".join(str(o) for o in octets)


def _hex_to_ipv6(value: str) -> str:
    """Four 32-bit words, each little-endian, in network order."""
    words = [value[i:i + 8] for i in range(0, 32, 8)]
    packed = b"".join(int(w, 16).to_bytes(4, "little") for w in words)
    return str(ipaddress.ip_address(packed))


def _read_proc(path: str, port: int, family: str) -> list[Listener] | None:
    try:
        with open(path, "r", encoding="ascii", errors="replace") as handle:
            lines = handle.read().splitlines()
    except (OSError, PermissionError):
        return None
    out: list[Listener] = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 4 or parts[3].upper() != _TCP_LISTEN_HEX:
            continue
        local = parts[1]
        if ":" not in local:
            continue
        addr_hex, port_hex = local.rsplit(":", 1)
        try:
            if int(port_hex, 16) != port:
                continue
            address = _hex_to_ipv4(addr_hex) if family == "ipv4" else _hex_to_ipv6(addr_hex)
        except (ValueError, OverflowError):
            continue
        out.append(Listener(address=address, port=port, family=family, source=SOURCE_PROC))
    return out


class _ListenerRead(list):
    """A partial read proves presence, but cannot prove absence."""
    def __init__(self, listeners, *, complete: bool):
        super().__init__(listeners)
        self.complete = complete


def _from_proc(port: int) -> list[Listener] | None:
    v4 = _read_proc("/proc/net/tcp", port, "ipv4")
    v6 = _read_proc("/proc/net/tcp6", port, "ipv6")
    if v4 is None and v6 is None:
        return None
    return _ListenerRead((v4 or []) + (v6 or []),
                         complete=v4 is not None and v6 is not None)


def _parse_addr_port(token: str) -> tuple[str, int] | None:
    """`192.168.1.9:8766`, `[::]:8766`, `*:8766`, `0.0.0.0:8766`."""
    token = token.strip()
    if token.startswith("["):
        host, _, tail = token.partition("]")
        host = host[1:]
        port_text = tail.lstrip(":")
    else:
        host, _, port_text = token.rpartition(":")
    if not port_text.isdigit():
        return None
    if host in ("*", ""):
        host = "0.0.0.0"
    return host, int(port_text)


def _from_command(argv: Sequence[str], port: int, source: str) -> list[Listener] | None:
    binary = shutil.which(argv[0])
    if binary is None:
        return None
    try:
        done = subprocess.run([binary, *argv[1:]], capture_output=True, text=True,
                              timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    out: list[Listener] = []
    for line in done.stdout.splitlines():
        if "LISTEN" not in line.upper():
            continue
        for token in line.split():
            parsed = _parse_addr_port(token)
            if parsed is None or parsed[1] != port:
                continue
            address = parsed[0]
            try:
                family = "ipv6" if ipaddress.ip_address(address).version == 6 else "ipv4"
            except ValueError:
                continue
            out.append(Listener(address=address, port=port, family=family, source=source))
            break
    return out


def observe_listeners(port: int, *, readers: Iterable[str] | None = None) -> dict[str, Any]:
    """Every socket listening on `port`, with the reader that found them.

    Readers are tried in order and the FIRST that returns anything wins --
    not merged. Merging would double-count the same socket seen two ways and
    make "how do we know" unanswerable.
    """
    order = list(readers) if readers is not None else [SOURCE_PROC, SOURCE_SS, SOURCE_NETSTAT]
    attempted: list[str] = []
    empty_source = None
    for reader in order:
        attempted.append(reader)
        if reader == SOURCE_PROC:
            found = _from_proc(port)
        elif reader == SOURCE_SS:
            found = _from_command(["ss", "-ltn"], port, SOURCE_SS)
        elif reader == SOURCE_NETSTAT:
            found = _from_command(["netstat", "-ltn"], port, SOURCE_NETSTAT)
        else:
            continue
        if found is None:
            continue
        complete = getattr(found, "complete", True)
        if found and (complete or any(listener.is_routable for listener in found)):
            return {"observed": True, "source": reader, "attempted": attempted,
                    "complete": complete,
                    "listeners": [listener.as_dict() for listener in found]}
        if complete:
            empty_source = reader

    if empty_source is not None:
        return {"observed": True, "source": empty_source,
                "attempted": attempted, "listeners": [],
                "detail": f"nothing is listening on port {port}"}
    return {"observed": False, "source": SOURCE_UNAVAILABLE, "attempted": attempted,
            "listeners": [],
            "detail": "could not completely enumerate listening sockets on this host"}


def lan_state(port: int, *, configured_binds: Sequence[str] = (),
              readers: Iterable[str] | None = None) -> dict[str, Any]:
    """Is this controller reachable on the LAN, and on what evidence?

    `configured_binds` is the resolved configuration -- consulted as a
    FALLBACK when runtime cannot be inspected, and compared against runtime
    when it can, so a stale config is reported rather than silently believed.
    """
    evidence = observe_listeners(port, readers=readers)
    configured = [str(b) for b in configured_binds if str(b).strip()]

    if not evidence["observed"]:
        # Blind. Fall back to configuration and SAY it is configuration.
        if configured:
            return {"state": LAN_BOUND, "source": SOURCE_CONFIG, "confident": False,
                    "lan_addresses": configured, "configured_binds": configured,
                    "evidence": evidence,
                    "detail": "reported from configuration; the running process could "
                              "not be inspected on this host"}
        return {"state": UNKNOWN, "source": SOURCE_UNAVAILABLE, "confident": False,
                "lan_addresses": [], "configured_binds": configured, "evidence": evidence,
                "detail": "no runtime evidence and no LAN bind configured -- this is "
                          "'could not determine', not 'not configured'"}

    listeners = [Listener(l["address"], l["port"], l["family"], l["source"])
                 for l in evidence["listeners"]]
    routable = [l for l in listeners if l.is_routable]
    wildcard = [l for l in routable if l.is_wildcard]

    if routable:
        state, detail = LAN_BOUND, None
        if wildcard:
            detail = ("bound to a wildcard address, so it is reachable on every "
                      "interface this host has")
    elif listeners:
        state = LOOPBACK_ONLY
        detail = "listening, but only on loopback"
    else:
        # Observed, and nothing is there. Not the same as unconfigured.
        state = NOT_CONFIGURED if not configured else UNKNOWN
        detail = (evidence.get("detail")
                  or "nothing is listening on this port")
        if configured:
            detail += " -- but a LAN bind IS configured, so the service is probably down"

    result: dict[str, Any] = {
        "state": state, "source": evidence["source"], "confident": True,
        "lan_addresses": [l.address for l in routable],
        "all_addresses": [l.address for l in listeners],
        "configured_binds": configured, "evidence": evidence,
    }
    if detail:
        result["detail"] = detail

    # Drift: configuration that no longer describes the running process. Worth
    # naming -- it is the file an operator is about to edit.
    if configured and evidence.get("complete", True):
        observed = {l.address for l in listeners}
        missing = [b for b in configured if b not in observed and not wildcard]
        if missing:
            result["config_drift"] = {
                "configured_not_listening": missing,
                "detail": ("configuration names an address the running process is not "
                           "listening on; the process predates the config change, or "
                           "the bind failed"),
            }
    return result
