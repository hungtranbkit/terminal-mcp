"""LAN device discovery -- Nodes page "Discover devices" feature.

Pure stdlib + asyncio (this project's own standing "no new dependency
unless genuinely needed" discipline, same posture as host_metrics.py):
subnets come from parsing `ip` (iproute2, already a hard dependency of
this whole project -- tmux itself needs a Linux host with it), online-
host evidence comes from the kernel's own ARP/neighbor table (`ip neigh
show`, read-only, no packets sent by US to build it) plus a small,
FIXED set of TCP connect probes (never a port sweep) on the node-agent
port and the well-known SSH/WinRM ports. No raw ICMP ping (would need a
raw socket / setuid helper this project's own unprivileged service user
does not have -- and is not needed: a TCP connect attempt against a
closed port on a live host still completes fast with a real RST, and
either way resolves that host's MAC into the kernel's ARP table as a
side effect, which this module re-reads after probing).

Safety, all non-negotiable per this feature's own task spec:
  - ONLY scans subnets derived from this host's own UP, non-loopback
    NICs (see local_ipv4_subnets) -- never an arbitrary/operator-typed
    range, never anything routed to the public internet.
  - Every candidate subnet/address is re-checked against
    is_lan_scannable() (RFC1918 + link-local only, loopback/multicast/
    reserved excluded) before it is ever touched -- belt and suspenders
    on top of "we only look at our own NICs' subnets" above.
  - A subnet larger than `max_hosts_per_scan` is skipped outright, never
    silently truncated to a different (wrong) range -- see
    local_ipv4_subnets's own docstring.
  - Fixed, small probe port list (agent port + 22 + 5985 + 5986) --
    never a range scan of any kind.
  - Bounded concurrency (semaphore) + a short per-host timeout + a hard
    overall wall-clock budget for the whole scan (asyncio.wait_for) --
    a scan can never run away or starve the event loop the rest of the
    dashboard's own routes share.
  - Exactly one scan may run at a time, with a cooldown between scans
    (rate limit) -- see DiscoveryService.start_scan.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

DEFAULT_AGENT_PORT = 8790
SSH_PORT = 22
WINRM_HTTP_PORT = 5985
WINRM_HTTPS_PORT = 5986

STATUS_ALREADY_CONNECTED = "already_connected"
STATUS_CONNECTABLE = "connectable"
STATUS_NEEDS_SETUP = "needs_setup"
STATUS_UNKNOWN = "unknown"

SCAN_RUNNING = "running"
SCAN_DONE = "done"
SCAN_CANCELLED = "cancelled"
SCAN_ERROR = "error"


def is_lan_scannable(ip: ipaddress.IPv4Address) -> bool:
    """RFC1918 private ranges + link-local (169.254.0.0/16) only --
    excludes loopback/multicast/reserved/unspecified even though Python's
    own `IPv4Address.is_private` would otherwise also call 127.0.0.0/8
    "private". This is the ONE gate every subnet AND every manual-add
    target (dashboard.py's SSRF check) is checked against."""
    return (
        (ip.is_private or ip.is_link_local)
        and not ip.is_loopback
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def _run(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return result.stdout
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ""


def _up_interfaces() -> set[str]:
    output = _run(["ip", "-o", "link", "show", "up"])
    names = set()
    for line in output.splitlines():
        match = re.match(r"^\d+:\s+([^:@\s]+)", line)
        if match:
            names.add(match.group(1))
    return names


@dataclass(frozen=True)
class LocalSubnet:
    interface: str
    network: ipaddress.IPv4Network
    local_ip: ipaddress.IPv4Address


def local_ipv4_subnets(max_hosts: int = 512) -> list[LocalSubnet]:
    """This host's own scannable subnets -- one per UP, non-loopback IPv4
    interface whose network is itself private/link-local (never a public
    IP the NIC happens to carry, e.g. a cloud instance's public
    interface) AND small enough to scan (`network.num_addresses <=
    max_hosts`). A subnet that fails either check is skipped entirely,
    never widened/narrowed/guessed at -- an operator with a
    legitimately-huge internal range gets an honest empty result for
    that NIC, not a silently-wrong partial scan."""
    up = _up_interfaces()
    output = _run(["ip", "-o", "-4", "addr", "show"])
    subnets: list[LocalSubnet] = []
    seen: set[str] = set()
    for line in output.splitlines():
        match = re.match(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
        if not match:
            continue
        iface, addr, prefix = match.group(1), match.group(2), int(match.group(3))
        if iface == "lo" or iface not in up:
            continue
        try:
            interface_obj = ipaddress.ip_interface(f"{addr}/{prefix}")
        except ValueError:
            continue
        network = interface_obj.network
        if not is_lan_scannable(network.network_address):
            continue
        if network.num_addresses > max_hosts:
            continue
        key = str(network)
        if key in seen:
            continue
        seen.add(key)
        subnets.append(LocalSubnet(interface=iface, network=network, local_ip=interface_obj.ip))
    return subnets


def read_arp_table() -> dict[str, dict[str, str]]:
    """Read-only: the kernel's OWN neighbor/ARP cache (`ip neigh show`) --
    this sends nothing itself. {ip: {"mac": ..., "state": ...}}."""
    output = _run(["ip", "-4", "neigh", "show"])
    table: dict[str, dict[str, str]] = {}
    for line in output.splitlines():
        parts = line.split()
        if not parts:
            continue
        ip = parts[0]
        mac = None
        state = parts[-1] if parts else "UNKNOWN"
        if "lladdr" in parts:
            mac = parts[parts.index("lladdr") + 1]
        table[ip] = {"mac": mac or "", "state": state}
    return table


async def _tcp_probe(ip: str, port: int, timeout: float) -> bool:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def _reverse_dns(ip: str, timeout: float) -> str | None:
    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: socket.getnameinfo((ip, 0), 0)), timeout=timeout,
        )
        return result[0] if result and result[0] != ip else None
    except (OSError, asyncio.TimeoutError, socket.gaierror):
        return None


async def _agent_health(ip: str, port: int, timeout: float) -> dict[str, Any] | None:
    """GET /v1/health -- the ONE unauthenticated node-agent route (see
    node_agent.py's own module docstring), used here purely to detect
    "is a terminal-node-agent already running here", never to read
    anything privileged."""
    import json
    import urllib.error
    import urllib.request

    def _fetch() -> dict[str, Any] | None:
        try:
            with urllib.request.urlopen(f"http://{ip}:{port}/v1/health", timeout=timeout) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return None

    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(None, _fetch), timeout=timeout + 1.0)


@dataclass
class DiscoveredDevice:
    ip: str
    hostname: str | None = None
    mac: str | None = None
    arp_state: str | None = None
    open_ports: list[int] = field(default_factory=list)
    agent_reachable: bool = False
    agent_info: dict[str, Any] | None = None
    os_guess: str | None = None  # "windows" | "linux" -- hedged, never asserted as fact
    status: str = STATUS_UNKNOWN
    already_connected_node_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ip": self.ip, "hostname": self.hostname, "mac": self.mac, "arp_state": self.arp_state,
            "open_ports": list(self.open_ports), "agent_reachable": self.agent_reachable,
            "agent_info": self.agent_info, "os_guess": self.os_guess, "status": self.status,
            "already_connected_node_id": self.already_connected_node_id,
        }


@dataclass
class ScanResult:
    scan_id: str
    state: str
    started_at: float
    finished_at: float | None = None
    subnets: list[str] = field(default_factory=list)
    devices: list[DiscoveredDevice] = field(default_factory=list)
    truncated: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scan_id": self.scan_id, "state": self.state, "started_at": self.started_at,
            "finished_at": self.finished_at, "subnets": list(self.subnets),
            "devices": [d.to_dict() for d in self.devices], "truncated": self.truncated, "error": self.error,
        }


class DiscoveryService:
    """One instance lives for the life of the dashboard process (held by
    register_dashboard's closure, same lifetime as ControllerService).
    Exactly one scan runs at a time; `cooldown_seconds` rate-limits how
    often a NEW scan may be started (task's own "rate limit" requirement)
    -- Rescan before the cooldown elapses returns the still-running/last
    result instead of starting a second overlapping scan."""

    def __init__(self, *, agent_port: int = DEFAULT_AGENT_PORT, concurrency: int = 32,
                host_timeout: float = 0.35, max_hosts_per_scan: int = 512,
                overall_timeout_seconds: float = 45.0, cooldown_seconds: float = 5.0,
                cache_ttl_seconds: float = 300.0) -> None:
        self.agent_port = agent_port
        self.concurrency = concurrency
        self.host_timeout = host_timeout
        self.max_hosts_per_scan = max_hosts_per_scan
        self.overall_timeout_seconds = overall_timeout_seconds
        self.cooldown_seconds = cooldown_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self._result: ScanResult | None = None
        self._task: asyncio.Task | None = None
        self._last_started_at: float = 0.0

    def status(self) -> ScanResult | None:
        return self._result

    def start_scan(self, *, known_endpoints: dict[str, str] | None = None) -> tuple[ScanResult, bool]:
        """Returns (result, started) -- started=False when a scan is
        already running or the cooldown hasn't elapsed yet (the caller
        gets the current/last result either way, never an error, since
        "I asked for a rescan a bit too soon" is not a failure)."""
        now = time.monotonic()
        if self._task is not None and not self._task.done():
            return self._result, False
        if now - self._last_started_at < self.cooldown_seconds and self._result is not None:
            return self._result, False
        self._last_started_at = now
        scan_id = uuid.uuid4().hex[:12]
        result = ScanResult(scan_id=scan_id, state=SCAN_RUNNING, started_at=time.time())
        self._result = result
        self._task = asyncio.ensure_future(self._run_scan(result, known_endpoints or {}))
        return result, True

    async def cancel(self) -> bool:
        if self._task is None or self._task.done():
            return False
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 -- cancellation path, never propagate
            pass
        if self._result is not None and self._result.state == SCAN_RUNNING:
            self._result.state = SCAN_CANCELLED
            self._result.finished_at = time.time()
        return True

    async def _run_scan(self, result: ScanResult, known_endpoints: dict[str, str]) -> None:
        try:
            await asyncio.wait_for(self._scan_body(result, known_endpoints), timeout=self.overall_timeout_seconds)
            result.state = SCAN_DONE
        except asyncio.CancelledError:
            result.state = SCAN_CANCELLED
            raise
        except asyncio.TimeoutError:
            result.state = SCAN_DONE
            result.truncated = True
            result.error = "scan_timed_out_after_budget"
        except Exception as exc:  # noqa: BLE001 -- a scan failure must never crash the dashboard process
            result.state = SCAN_ERROR
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            result.finished_at = time.time()

    async def _scan_body(self, result: ScanResult, known_endpoints: dict[str, str]) -> None:
        subnets = local_ipv4_subnets(max_hosts=self.max_hosts_per_scan)
        result.subnets = [str(s.network) for s in subnets]
        if not subnets:
            return

        candidates: list[str] = []
        budget = self.max_hosts_per_scan
        for index, subnet in enumerate(subnets):
            hosts = [str(h) for h in subnet.network.hosts() if h != subnet.local_ip]
            if len(hosts) > budget:
                hosts = hosts[:budget]
                result.truncated = True
            candidates.extend(hosts)
            budget -= len(hosts)
            if budget <= 0:
                if index < len(subnets) - 1:
                    result.truncated = True  # one or more later subnets never got scanned at all
                break

        semaphore = asyncio.Semaphore(self.concurrency)
        ports = (self.agent_port, SSH_PORT, WINRM_HTTP_PORT, WINRM_HTTPS_PORT)
        # A live (non-FAILED/INCOMPLETE) kernel ARP entry is evidence on its
        # own -- a device with every one of our 4 probed ports closed (a
        # real, common case: a plain workstation/printer/IoT device with no
        # SSH/WinRM/agent listening at all) still gets reported as
        # STATUS_UNKNOWN rather than silently dropped, exactly like the
        # task's own "Unknown" status category implies. Read the ARP table
        # ONCE, up front (not just after probing) so it can also seed the
        # candidate-evidence decision below, not only enrich an already-
        # decided-live device after the fact.
        _DEAD_ARP_STATES = {"FAILED", "INCOMPLETE"}
        arp = read_arp_table()

        async def probe_one(ip: str) -> DiscoveredDevice | None:
            async with semaphore:
                probes = await asyncio.gather(*(_tcp_probe(ip, port, self.host_timeout) for port in ports))
            open_ports = [port for port, ok in zip(ports, probes) if ok]
            arp_entry = arp.get(ip)
            arp_alive = bool(arp_entry) and arp_entry.get("state") not in _DEAD_ARP_STATES
            if not open_ports and not arp_alive:
                return None  # no evidence this address is even online -- never reported as a "device"
            device = DiscoveredDevice(ip=ip, open_ports=open_ports)
            device.agent_reachable = self.agent_port in open_ports
            if arp_entry:
                device.mac = arp_entry.get("mac") or None
                device.arp_state = arp_entry.get("state")
            return device

        probed = await asyncio.gather(*(probe_one(ip) for ip in candidates))
        devices = [d for d in probed if d is not None]

        # Best-effort reverse DNS + node-agent identity, bounded concurrency.
        async def enrich(device: DiscoveredDevice) -> None:
            async with semaphore:
                device.hostname = await _reverse_dns(device.ip, self.host_timeout)
                if device.agent_reachable:
                    device.agent_info = await _agent_health(device.ip, self.agent_port, self.host_timeout)
                    device.agent_reachable = device.agent_info is not None

        await asyncio.gather(*(enrich(d) for d in devices))

        endpoints_by_host = {}
        for node_id, endpoint in known_endpoints.items():
            host = _endpoint_host(endpoint)
            if host:
                endpoints_by_host[host] = node_id

        for device in devices:
            matched_node = endpoints_by_host.get(device.ip)
            has_ssh = SSH_PORT in device.open_ports
            has_winrm = WINRM_HTTP_PORT in device.open_ports or WINRM_HTTPS_PORT in device.open_ports
            if has_winrm and not has_ssh:
                device.os_guess = "windows"
            elif has_ssh and not has_winrm:
                device.os_guess = "linux"
            if matched_node:
                device.status = STATUS_ALREADY_CONNECTED
                device.already_connected_node_id = matched_node
            elif device.agent_reachable:
                device.status = STATUS_CONNECTABLE
            elif has_ssh or has_winrm:
                device.status = STATUS_NEEDS_SETUP
            else:
                device.status = STATUS_UNKNOWN

        # Sort SSH-reachable hosts first (task's own explicit "ưu tiên máy
        # có SSH" -- prioritize machines with SSH, since those are the
        # ones actionable through the Connect Node SSH flow right now;
        # WinRM-only hosts next, then everything else), IP address as the
        # tiebreaker within each tier so the ordering still reads as
        # sensible/stable rather than shuffled.
        def _sort_key(d: DiscoveredDevice) -> tuple[int, tuple[int, ...]]:
            has_ssh = SSH_PORT in d.open_ports
            has_winrm = WINRM_HTTP_PORT in d.open_ports or WINRM_HTTPS_PORT in d.open_ports
            tier = 0 if has_ssh else (1 if has_winrm else 2)
            return (tier, tuple(int(p) for p in d.ip.split(".")))

        devices.sort(key=_sort_key)
        result.devices = devices


def _endpoint_host(endpoint: str) -> str | None:
    match = re.match(r"^https?://([^:/]+)", endpoint)
    return match.group(1) if match else None
