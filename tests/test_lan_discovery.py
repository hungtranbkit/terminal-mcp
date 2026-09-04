"""lan_discovery.py -- subnet derivation, private-range filtering, scan
engine safety limits (concurrency/timeout/host cap), classification.
Every subprocess call (`ip addr`/`ip neigh`) is monkeypatched to a fixed
fixture string so these tests are fully deterministic regardless of this
host's own real NICs -- the one place this project deliberately tests
against the REAL local network (a live smoke check, not a unit test) is
the manual verification in the task's own final report, not here."""
from __future__ import annotations

import asyncio
import ipaddress

import pytest

from terminal_mcp import lan_discovery
from terminal_mcp.lan_discovery import (
    STATUS_ALREADY_CONNECTED,
    STATUS_CONNECTABLE,
    STATUS_NEEDS_SETUP,
    STATUS_UNKNOWN,
    DiscoveredDevice,
    DiscoveryService,
    is_lan_scannable,
    local_ipv4_subnets,
    read_arp_table,
)

# ---------------------------------------------------------------------------
# is_lan_scannable -- the one gate every subnet AND every manual-add target
# (dashboard.py's SSRF check, via remote_connect.py) is checked against.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ip", ["10.0.0.1", "10.255.255.254", "172.16.0.1", "172.31.255.254",
                                "192.168.0.1", "192.168.255.254", "169.254.1.1"])
def test_private_and_link_local_are_scannable(ip):
    assert is_lan_scannable(ipaddress.IPv4Address(ip)) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "127.0.0.1", "127.5.5.5",
                                "224.0.0.1", "255.255.255.255", "0.0.0.0"])
def test_public_loopback_multicast_reserved_are_not_scannable(ip):
    # NOTE: real, globally-routable addresses (8.8.8.8, 1.1.1.1,
    # 93.184.216.34) plus loopback/multicast/broadcast/unspecified --
    # deliberately NOT an IANA documentation range (192.0.2.0/24,
    # 198.51.100.0/24, 203.0.113.0/24): Python's own ipaddress.is_private
    # already classifies those as "private" too (RFC 5737's own special-
    # purpose registry membership, the same authoritative source
    # is_lan_scannable defers to) -- correctly so, since they are non-
    # globally-routable bogons, not a real SSRF-relevant target either way.
    assert is_lan_scannable(ipaddress.IPv4Address(ip)) is False


# ---------------------------------------------------------------------------
# local_ipv4_subnets -- parses `ip -o -4 addr show` / `ip -o link show up`,
# filters to UP+non-loopback+private+small-enough.
# ---------------------------------------------------------------------------

IP_LINK_UP_FIXTURE = (
    "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN mode DEFAULT group default qlen 1000\\    link/loopback\n"
    "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP mode DEFAULT group default qlen 1000\\    link/ether\n"
    "3: eth1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP mode DEFAULT group default qlen 1000\\    link/ether\n"
)

IP_ADDR_FIXTURE = (
    "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n"
    "2: eth0    inet 192.168.1.100/24 brd 192.168.1.255 scope global dynamic noprefixroute eth0\\       valid_lft 86390sec preferred_lft 86390sec\n"
    "3: eth1    inet 93.184.216.34/24 brd 93.184.216.255 scope global eth1\\       valid_lft forever preferred_lft forever\n"
)


def test_local_ipv4_subnets_skips_loopback_and_public_and_down_interfaces(monkeypatch):
    def fake_run(cmd, timeout=5.0):
        if cmd[:3] == ["ip", "-o", "link"]:
            return IP_LINK_UP_FIXTURE
        if cmd[:4] == ["ip", "-o", "-4", "addr"]:
            return IP_ADDR_FIXTURE
        return ""
    monkeypatch.setattr(lan_discovery, "_run", fake_run)
    subnets = local_ipv4_subnets(max_hosts=512)
    # eth0's private /24 is included; lo (loopback) and eth1 (a real,
    # globally-routable public /24) are not.
    assert [str(s.network) for s in subnets] == ["192.168.1.0/24"]
    assert subnets[0].interface == "eth0"
    assert subnets[0].local_ip == ipaddress.IPv4Address("192.168.1.100")


def test_local_ipv4_subnets_skips_down_interface(monkeypatch):
    def fake_run(cmd, timeout=5.0):
        if cmd[:3] == ["ip", "-o", "link"]:
            return "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\\    link/loopback\n"  # eth0 NOT up
        if cmd[:4] == ["ip", "-o", "-4", "addr"]:
            return IP_ADDR_FIXTURE
        return ""
    monkeypatch.setattr(lan_discovery, "_run", fake_run)
    assert local_ipv4_subnets(max_hosts=512) == []


def test_local_ipv4_subnets_skips_subnet_larger_than_cap(monkeypatch):
    def fake_run(cmd, timeout=5.0):
        if cmd[:3] == ["ip", "-o", "link"]:
            return "2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\\    link/ether\n"
        if cmd[:4] == ["ip", "-o", "-4", "addr"]:
            return "2: eth0    inet 10.0.0.5/8 brd 10.255.255.255 scope global eth0\\       valid_lft forever\n"
        return ""
    monkeypatch.setattr(lan_discovery, "_run", fake_run)
    # 10.0.0.0/8 is far larger than any sane max_hosts cap -- must be
    # skipped entirely, never silently narrowed to a different range.
    assert local_ipv4_subnets(max_hosts=512) == []


def test_local_ipv4_subnets_deduplicates_same_network_on_multiple_nics(monkeypatch):
    def fake_run(cmd, timeout=5.0):
        if cmd[:3] == ["ip", "-o", "link"]:
            return ("2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\\    link/ether\n"
                   "3: eth1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\\    link/ether\n")
        if cmd[:4] == ["ip", "-o", "-4", "addr"]:
            return ("2: eth0    inet 192.168.1.5/24 brd 192.168.1.255 scope global eth0\\       valid_lft forever\n"
                   "3: eth1    inet 192.168.1.9/24 brd 192.168.1.255 scope global eth1\\       valid_lft forever\n")
        return ""
    monkeypatch.setattr(lan_discovery, "_run", fake_run)
    subnets = local_ipv4_subnets(max_hosts=512)
    assert len(subnets) == 1


# ---------------------------------------------------------------------------
# read_arp_table -- parses `ip -4 neigh show` (read-only, never sends
# anything itself).
# ---------------------------------------------------------------------------

IP_NEIGH_FIXTURE = (
    "192.168.1.1 dev eth0 lladdr aa:bb:cc:dd:ee:01 REACHABLE\n"
    "192.168.1.50 dev eth0 lladdr aa:bb:cc:dd:ee:02 STALE\n"
    "192.168.1.99 dev eth0  FAILED\n"
)


def test_read_arp_table_parses_mac_and_state(monkeypatch):
    monkeypatch.setattr(lan_discovery, "_run", lambda cmd, timeout=5.0: IP_NEIGH_FIXTURE)
    table = read_arp_table()
    assert table["192.168.1.1"] == {"mac": "aa:bb:cc:dd:ee:01", "state": "REACHABLE"}
    assert table["192.168.1.50"]["state"] == "STALE"
    assert table["192.168.1.99"]["mac"] == ""  # no lladdr -- an incomplete/failed entry, not a crash
    assert table["192.168.1.99"]["state"] == "FAILED"


def test_run_survives_missing_binary_and_timeout(monkeypatch):
    # A host without `ip` (or a slow/hung call) must never crash discovery
    # -- _run swallows both, returning "".
    import subprocess as _subprocess

    def raise_missing(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(_subprocess, "run", raise_missing)
    assert lan_discovery._run(["ip", "addr"]) == ""


# ---------------------------------------------------------------------------
# DiscoveryService -- concurrency/timeout/cap enforcement, classification,
# cache/cancel/rate-limit. Uses a tiny fake subnet (127.0.0.0/30-ish via a
# monkeypatched local_ipv4_subnets) so scans complete in well under a
# second and never touch this host's own real NICs.
# ---------------------------------------------------------------------------


def _fake_subnet(network_str: str, local_ip_str: str, interface: str = "eth0") -> lan_discovery.LocalSubnet:
    network = ipaddress.IPv4Network(network_str)
    return lan_discovery.LocalSubnet(interface=interface, network=network, local_ip=ipaddress.IPv4Address(local_ip_str))


@pytest.mark.anyio
async def test_scan_finds_nothing_on_a_quiet_subnet(monkeypatch):
    # A /29 (6 usable hosts minus the local IP = 5 candidates) where NOTHING
    # answers any of the 4 probed ports and nothing is in ARP -- must
    # report zero devices, never a row for a silent address.
    monkeypatch.setattr(lan_discovery, "local_ipv4_subnets", lambda max_hosts: [_fake_subnet("203.0.113.240/29", "203.0.113.241")])
    monkeypatch.setattr(lan_discovery, "read_arp_table", lambda: {})

    async def never_open(ip, port, timeout):
        return False
    monkeypatch.setattr(lan_discovery, "_tcp_probe", never_open)

    service = DiscoveryService(concurrency=8, host_timeout=0.05, overall_timeout_seconds=5.0, cooldown_seconds=0.0)
    result, started = service.start_scan()
    assert started is True
    await service._task
    assert result.state == lan_discovery.SCAN_DONE
    assert result.devices == []
    assert result.subnets == ["203.0.113.240/29"]


@pytest.mark.anyio
async def test_scan_classifies_agent_reachable_ssh_winrm_and_arp_only_correctly(monkeypatch):
    monkeypatch.setattr(lan_discovery, "local_ipv4_subnets", lambda max_hosts: [_fake_subnet("203.0.113.240/29", "203.0.113.241")])
    monkeypatch.setattr(lan_discovery, "_reverse_dns", lambda ip, timeout: _immediate(None))

    open_ports_by_ip = {
        "203.0.113.242": {lan_discovery.DEFAULT_AGENT_PORT},  # agent-reachable
        "203.0.113.243": {22},  # ssh only -> needs_setup, os_guess linux
        "203.0.113.244": {lan_discovery.WINRM_HTTP_PORT},  # winrm only -> needs_setup, os_guess windows
        # 203.0.113.245 deliberately has NO open port among the 4 probed
        # -- but IS present (REACHABLE) in the ARP table below, e.g. a
        # printer/IoT device with nothing listening on any of those ports.
        # Must still be reported (task's own "Unknown" status), not
        # silently dropped just because no probed port answered.
    }
    monkeypatch.setattr(lan_discovery, "read_arp_table", lambda: {
        "203.0.113.245": {"mac": "aa:bb:cc:dd:ee:ff", "state": "REACHABLE"},
        "203.0.113.246": {"mac": "", "state": "FAILED"},  # a dead/incomplete ARP entry is NOT evidence
    })

    async def fake_probe(ip, port, timeout):
        return port in open_ports_by_ip.get(ip, set())
    monkeypatch.setattr(lan_discovery, "_tcp_probe", fake_probe)
    monkeypatch.setattr(lan_discovery, "_agent_health", lambda ip, port, timeout: _immediate({"node_id": "found", "version": "0.1"}))

    service = DiscoveryService(concurrency=8, host_timeout=0.05, overall_timeout_seconds=5.0, cooldown_seconds=0.0)
    result, started = service.start_scan()
    assert started is True
    await service._task
    by_ip = {d.ip: d for d in result.devices}
    assert by_ip["203.0.113.242"].status == STATUS_CONNECTABLE
    assert by_ip["203.0.113.242"].agent_reachable is True
    assert by_ip["203.0.113.242"].agent_info == {"node_id": "found", "version": "0.1"}
    assert by_ip["203.0.113.243"].status == STATUS_NEEDS_SETUP
    assert by_ip["203.0.113.243"].os_guess == "linux"
    assert by_ip["203.0.113.244"].status == STATUS_NEEDS_SETUP
    assert by_ip["203.0.113.244"].os_guess == "windows"
    assert by_ip["203.0.113.245"].status == STATUS_UNKNOWN
    assert by_ip["203.0.113.245"].os_guess is None
    assert by_ip["203.0.113.245"].mac == "aa:bb:cc:dd:ee:ff"
    assert "203.0.113.246" not in by_ip  # a FAILED ARP entry alone is not evidence of anything online

    # Task's own explicit "ưu tiên máy có SSH" -- the SSH-reachable host
    # (.243) must sort ahead of the WinRM-only (.244) and unknown (.245)
    # hosts, even though its IP is numerically in between, since it's the
    # only one directly actionable through the Connect Node SSH flow.
    ips_in_order = [d.ip for d in result.devices]
    assert ips_in_order.index("203.0.113.243") < ips_in_order.index("203.0.113.244")
    assert ips_in_order.index("203.0.113.244") < ips_in_order.index("203.0.113.245")


@pytest.mark.anyio
async def test_scan_marks_already_connected_by_matching_known_endpoint(monkeypatch):
    monkeypatch.setattr(lan_discovery, "local_ipv4_subnets", lambda max_hosts: [_fake_subnet("203.0.113.240/29", "203.0.113.241")])
    monkeypatch.setattr(lan_discovery, "read_arp_table", lambda: {})
    monkeypatch.setattr(lan_discovery, "_reverse_dns", lambda ip, timeout: _immediate(None))

    async def fake_probe(ip, port, timeout):
        return ip == "203.0.113.242" and port == lan_discovery.DEFAULT_AGENT_PORT
    monkeypatch.setattr(lan_discovery, "_tcp_probe", fake_probe)
    monkeypatch.setattr(lan_discovery, "_agent_health", lambda ip, port, timeout: _immediate({"node_id": "m910"}))

    service = DiscoveryService(concurrency=8, host_timeout=0.05, overall_timeout_seconds=5.0, cooldown_seconds=0.0)
    result, started = service.start_scan(known_endpoints={"m910": "http://203.0.113.242:8790"})
    await service._task
    device = next(d for d in result.devices if d.ip == "203.0.113.242")
    assert device.status == STATUS_ALREADY_CONNECTED
    assert device.already_connected_node_id == "m910"


@pytest.mark.anyio
async def test_only_one_scan_runs_at_a_time_and_cooldown_rate_limits_rescan(monkeypatch):
    monkeypatch.setattr(lan_discovery, "local_ipv4_subnets", lambda max_hosts: [])
    service = DiscoveryService(cooldown_seconds=100.0)  # long cooldown -- easy to assert against
    result1, started1 = service.start_scan()
    assert started1 is True
    result2, started2 = service.start_scan()  # still running (subnets==[] finishes fast, but cooldown blocks a NEW one either way)
    assert started2 is False
    assert result2 is result1
    await service._task


@pytest.mark.anyio
async def test_cancel_marks_scan_cancelled(monkeypatch):
    monkeypatch.setattr(lan_discovery, "local_ipv4_subnets", lambda max_hosts: [_fake_subnet("203.0.113.240/29", "203.0.113.241")])
    monkeypatch.setattr(lan_discovery, "read_arp_table", lambda: {})

    async def slow_probe(ip, port, timeout):
        await asyncio.sleep(5.0)
        return False
    monkeypatch.setattr(lan_discovery, "_tcp_probe", slow_probe)

    service = DiscoveryService(concurrency=8, host_timeout=10.0, overall_timeout_seconds=30.0, cooldown_seconds=0.0)
    result, started = service.start_scan()
    assert started is True
    await asyncio.sleep(0.05)  # let it actually start probing
    cancelled = await service.cancel()
    assert cancelled is True
    assert result.state == lan_discovery.SCAN_CANCELLED


def test_max_hosts_per_scan_caps_and_reports_truncated(monkeypatch):
    # A single /22 (1022 usable hosts) against a max_hosts_per_scan of 100
    # must be capped, never silently scan the whole thing.
    monkeypatch.setattr(lan_discovery, "local_ipv4_subnets", lambda max_hosts: [_fake_subnet("10.1.0.0/22", "10.1.0.1")])
    monkeypatch.setattr(lan_discovery, "read_arp_table", lambda: {})

    async def never_open(ip, port, timeout):
        return False
    monkeypatch.setattr(lan_discovery, "_tcp_probe", never_open)

    async def run_and_check():
        service = DiscoveryService(concurrency=32, host_timeout=0.02, max_hosts_per_scan=100,
                                   overall_timeout_seconds=30.0, cooldown_seconds=0.0)
        result, started = service.start_scan()
        assert started is True
        await service._task
        assert result.truncated is True
    asyncio.run(run_and_check())


async def _immediate(value):
    return value


@pytest.fixture
def anyio_backend():
    return "asyncio"
