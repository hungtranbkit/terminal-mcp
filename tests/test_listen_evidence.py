"""What the process is listening on, and how confidently we know it.

The regression this file exists for: `doctor connection` printed
`lan: not configured (loopback-only)` on a host whose controller was listening
on 192.168.1.109:8766 and 100.117.214.87:8766 at that moment. The cause was
not parsing -- it read TERMINAL_MCP_LAN_BIND out of the DOCTOR's environment,
which is a different process from the systemd-started server, and reported it
as the server's binding.

So the assertions here are about provenance as much as values: a stated fact
has to carry how it was obtained, and "I could not look" must never be
flattened into "there is nothing there".
"""
from __future__ import annotations

import pytest

from terminal_mcp import listen_evidence as le

PORT = 8766
PORT_HEX = f"{PORT:04X}"

_PROC_HEADER = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when "
                "retrnsmt   uid  timeout inode\n")


def _proc_line(addr_hex: str, *, port_hex: str = PORT_HEX, state: str = "0A") -> str:
    return (f"   0: {addr_hex}:{port_hex} 00000000:0000 {state} "
            f"00000000:00000000 00:00000000 00000000  1000        0 1 1 0 100 0 0 10 0\n")


@pytest.fixture
def procfs(tmp_path, monkeypatch):
    """A fake /proc/net/tcp{,6} pair, so every bind shape can be exercised
    without needing the host to actually be in that state."""
    v4 = tmp_path / "tcp"
    v6 = tmp_path / "tcp6"
    v4.write_text(_PROC_HEADER)
    v6.write_text(_PROC_HEADER)

    real_open = open

    def _fake_open(path, *args, **kwargs):
        if path == "/proc/net/tcp":
            return real_open(v4, *args, **kwargs)
        if path == "/proc/net/tcp6":
            return real_open(v6, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fake_open)
    monkeypatch.setattr(le.os.path, "exists", lambda p: True if p == "/proc/net/tcp" else False)
    return {"v4": v4, "v6": v6}


# -- address decoding ---------------------------------------------------------------

@pytest.mark.parametrize("hex_value,expected", [
    ("0100007F", "127.0.0.1"),
    ("6D01A8C0", "192.168.1.109"),      # the real address from the incident
    ("57D67564", "100.117.214.87"),
    ("00000000", "0.0.0.0"),
])
def test_ipv4_addresses_decode_little_endian(hex_value, expected):
    assert le._hex_to_ipv4(hex_value) == expected


@pytest.mark.parametrize("hex_value,expected", [
    ("00000000000000000000000000000000", "::"),
    ("00000000000000000000000001000000", "::1"),
])
def test_ipv6_addresses_decode(hex_value, expected):
    """Four 32-bit words, each little-endian -- the format procfs uses and
    the one most hand-rolled parsers get backwards."""
    assert le._hex_to_ipv6(hex_value) == expected


# -- the bind shapes ----------------------------------------------------------------

def test_a_lan_bind_is_reported_as_lan_bound(procfs):
    procfs["v4"].write_text(_PROC_HEADER + _proc_line("6D01A8C0") + _proc_line("0100007F"))

    state = le.lan_state(PORT, readers=[le.SOURCE_PROC])

    assert state["state"] == le.LAN_BOUND
    assert state["source"] == le.SOURCE_PROC
    assert state["lan_addresses"] == ["192.168.1.109"]
    assert state["confident"] is True


def test_loopback_only_is_not_the_same_as_not_configured(procfs):
    """These used to print as one sentence. They call for opposite actions:
    one means "enable a LAN bind", the other means "start the service"."""
    procfs["v4"].write_text(_PROC_HEADER + _proc_line("0100007F"))

    state = le.lan_state(PORT, readers=[le.SOURCE_PROC])

    assert state["state"] == le.LOOPBACK_ONLY
    assert state["lan_addresses"] == []


def test_a_wildcard_bind_counts_as_lan_reachable(procfs):
    """0.0.0.0 names no LAN address but is reachable on every interface --
    reporting it as loopback-only would be the same defect in reverse."""
    procfs["v4"].write_text(_PROC_HEADER + _proc_line("00000000"))

    state = le.lan_state(PORT, readers=[le.SOURCE_PROC])

    assert state["state"] == le.LAN_BOUND
    assert "0.0.0.0" in state["lan_addresses"]
    assert "wildcard" in state["detail"]


def test_an_ipv6_wildcard_bind_counts_too(procfs):
    procfs["v6"].write_text(_PROC_HEADER
                            + _proc_line("00000000000000000000000000000000"))

    state = le.lan_state(PORT, readers=[le.SOURCE_PROC])

    assert state["state"] == le.LAN_BOUND
    assert "::" in state["lan_addresses"]


def test_an_ipv6_loopback_only_bind_is_loopback_only(procfs):
    procfs["v6"].write_text(_PROC_HEADER
                            + _proc_line("00000000000000000000000001000000"))

    state = le.lan_state(PORT, readers=[le.SOURCE_PROC])
    assert state["state"] == le.LOOPBACK_ONLY


def test_a_link_local_address_is_not_treated_as_lan(procfs):
    """169.254/16 is not routable off the segment; calling it a LAN bind
    would promise reachability that does not exist."""
    procfs["v4"].write_text(_PROC_HEADER + _proc_line("0101FEA9"))  # 169.254.1.1

    state = le.lan_state(PORT, readers=[le.SOURCE_PROC])
    assert state["state"] == le.LOOPBACK_ONLY


def test_only_listening_sockets_count(procfs):
    """An established connection to the port is not a listener."""
    procfs["v4"].write_text(_PROC_HEADER + _proc_line("6D01A8C0", state="06"))

    state = le.lan_state(PORT, readers=[le.SOURCE_PROC])
    assert state["lan_addresses"] == []


def test_another_ports_listener_is_ignored(procfs):
    procfs["v4"].write_text(_PROC_HEADER + _proc_line("6D01A8C0", port_hex="1F90"))

    state = le.lan_state(PORT, readers=[le.SOURCE_PROC])
    assert state["all_addresses"] == []


# -- "could not look" is its own answer -----------------------------------------------

def test_no_reader_available_is_unknown_not_not_configured(monkeypatch):
    """The heart of the bug. Blindness reported as absence is what made the
    doctor confidently wrong."""
    # Isolate from the host: this machine really does listen on 8766.
    monkeypatch.setattr(le, "_from_proc", lambda port: None)
    monkeypatch.setattr(le.os.path, "exists", lambda p: False)
    monkeypatch.setattr(le.shutil, "which", lambda name: None)

    state = le.lan_state(PORT, readers=[le.SOURCE_PROC, le.SOURCE_SS])

    assert state["state"] == le.UNKNOWN
    assert state["confident"] is False
    assert "not 'not configured'" in state["detail"]


def test_no_reader_but_a_configured_bind_falls_back_and_says_so(monkeypatch):
    monkeypatch.setattr(le, "_from_proc", lambda port: None)
    monkeypatch.setattr(le.os.path, "exists", lambda p: False)
    monkeypatch.setattr(le.shutil, "which", lambda name: None)

    state = le.lan_state(PORT, configured_binds=["192.168.1.9"],
                         readers=[le.SOURCE_PROC])

    assert state["state"] == le.LAN_BOUND
    assert state["source"] == le.SOURCE_CONFIG
    assert state["confident"] is False, "configuration is not an observation"
    assert "could not be inspected" in state["detail"]


def test_nothing_listening_while_a_bind_is_configured_reads_as_service_down(procfs):
    """Observed-and-empty with config present is a different finding again."""
    state = le.lan_state(PORT, configured_binds=["192.168.1.9"],
                         readers=[le.SOURCE_PROC])

    assert state["state"] == le.UNKNOWN
    assert "probably down" in state["detail"]


def test_nothing_listening_and_nothing_configured_is_not_configured(procfs):
    state = le.lan_state(PORT, readers=[le.SOURCE_PROC])
    assert state["state"] == le.NOT_CONFIGURED
    assert state["confident"] is True


# -- reader fallbacks -------------------------------------------------------------------

def test_ss_is_used_when_procfs_has_nothing(monkeypatch):
    monkeypatch.setattr(le, "_from_proc", lambda port: None)
    monkeypatch.setattr(le.shutil, "which", lambda name: f"/usr/bin/{name}")

    class _Done:
        returncode = 0
        stdout = ("State  Recv-Q Send-Q Local Address:Port Peer Address:Port\n"
                  f"LISTEN 0      4096   192.168.1.50:{PORT}      0.0.0.0:*\n")

    monkeypatch.setattr(le.subprocess, "run", lambda *a, **k: _Done())

    state = le.lan_state(PORT, readers=[le.SOURCE_PROC, le.SOURCE_SS])

    assert state["source"] == le.SOURCE_SS
    assert state["lan_addresses"] == ["192.168.1.50"]


def test_a_bracketed_ipv6_listener_is_parsed_from_ss(monkeypatch):
    monkeypatch.setattr(le, "_from_proc", lambda port: None)
    monkeypatch.setattr(le.shutil, "which", lambda name: f"/usr/bin/{name}")

    class _Done:
        returncode = 0
        stdout = f"LISTEN 0 4096 [2001:db8::1]:{PORT} [::]:*\n"

    monkeypatch.setattr(le.subprocess, "run", lambda *a, **k: _Done())

    state = le.lan_state(PORT, readers=[le.SOURCE_PROC, le.SOURCE_SS])
    assert state["lan_addresses"] == ["2001:db8::1"]


def test_a_star_wildcard_from_netstat_is_understood(monkeypatch):
    monkeypatch.setattr(le, "_from_proc", lambda port: None)
    monkeypatch.setattr(le.shutil, "which",
                        lambda name: "/usr/bin/netstat" if name == "netstat" else None)

    class _Done:
        returncode = 0
        stdout = f"tcp 0 0 *:{PORT} *:* LISTEN\n"

    monkeypatch.setattr(le.subprocess, "run", lambda *a, **k: _Done())

    state = le.lan_state(PORT, readers=[le.SOURCE_SS, le.SOURCE_NETSTAT])
    assert state["state"] == le.LAN_BOUND
    assert state["source"] == le.SOURCE_NETSTAT


def test_readers_are_not_merged_so_provenance_stays_answerable(procfs, monkeypatch):
    """First reader that answers wins. Merging would double-count one socket
    seen two ways and make "how do we know" unanswerable."""
    procfs["v4"].write_text(_PROC_HEADER + _proc_line("6D01A8C0"))
    called = []
    monkeypatch.setattr(le, "_from_command",
                        lambda argv, port, source: called.append(source) or [])

    evidence = le.observe_listeners(PORT)

    assert evidence["source"] == le.SOURCE_PROC
    assert called == [], "ss/netstat must not run once procfs answered"


def test_a_permission_error_on_procfs_degrades_to_the_next_reader(monkeypatch):
    def _boom(*a, **k):
        raise PermissionError("nope")

    monkeypatch.setattr("builtins.open", _boom)
    monkeypatch.setattr(le.os.path, "exists", lambda p: True)
    monkeypatch.setattr(le.shutil, "which", lambda name: None)

    evidence = le.observe_listeners(PORT)
    assert evidence["listeners"] == []
    assert evidence["observed"] is False, "existence is not proof that a read succeeded"


# -- stale configuration ------------------------------------------------------------------

def test_configuration_that_no_longer_matches_the_process_is_reported_as_drift(procfs):
    """The file an operator is about to edit."""
    procfs["v4"].write_text(_PROC_HEADER + _proc_line("6D01A8C0"))

    state = le.lan_state(PORT, configured_binds=["10.0.0.5"], readers=[le.SOURCE_PROC])

    assert state["state"] == le.LAN_BOUND
    assert state["lan_addresses"] == ["192.168.1.109"], "runtime wins over config"
    assert state["config_drift"]["configured_not_listening"] == ["10.0.0.5"]


def test_a_wildcard_bind_is_not_reported_as_drift(procfs):
    """0.0.0.0 covers every configured address, so nothing is missing."""
    procfs["v4"].write_text(_PROC_HEADER + _proc_line("00000000"))

    state = le.lan_state(PORT, configured_binds=["10.0.0.5"], readers=[le.SOURCE_PROC])
    assert "config_drift" not in state


def test_matching_configuration_produces_no_drift(procfs):
    procfs["v4"].write_text(_PROC_HEADER + _proc_line("6D01A8C0"))

    state = le.lan_state(PORT, configured_binds=["192.168.1.109"],
                         readers=[le.SOURCE_PROC])
    assert "config_drift" not in state


# -- the doctor's own output: the false "not configured" ------------------------------

def _endpoints(**kwargs):
    from terminal_mcp import network_bind

    return network_bind.describe_endpoints(
        port=PORT, lan_bind_env=kwargs.pop("lan_bind_env", None),
        cidrs_env=kwargs.pop("cidrs_env", None), **kwargs)


def test_runtime_evidence_beats_an_empty_environment():
    """The regression, stated exactly.

    The doctor CLI has no TERMINAL_MCP_LAN_BIND -- the systemd unit does. It
    used to read its OWN environment, find nothing, and print "not configured"
    about a controller listening on two LAN addresses.
    """
    runtime = {"state": le.LAN_BOUND, "source": le.SOURCE_PROC, "confident": True,
               "lan_addresses": ["192.168.1.109", "100.117.214.87"]}

    endpoints = _endpoints(lan_bind_env=None, runtime=runtime)

    assert endpoints["lan"] == f"http://192.168.1.109:{PORT}"
    assert endpoints["lans"] == [f"http://192.168.1.109:{PORT}",
                                 f"http://100.117.214.87:{PORT}"]
    assert endpoints["lan_source"] == le.SOURCE_PROC


def test_runtime_evidence_overrides_a_stale_environment():
    """Config says one address, the kernel says another. The kernel wins, and
    the disagreement is carried rather than hidden."""
    runtime = {"state": le.LAN_BOUND, "source": le.SOURCE_PROC, "confident": True,
               "lan_addresses": ["192.168.1.109"],
               "config_drift": {"configured_not_listening": ["10.0.0.5"],
                                "detail": "stale"}}

    endpoints = _endpoints(lan_bind_env="10.0.0.5", runtime=runtime)

    assert endpoints["lan"] == f"http://192.168.1.109:{PORT}"
    assert endpoints["config_drift"]["configured_not_listening"] == ["10.0.0.5"]


def test_an_observed_loopback_only_host_says_loopback_not_unconfigured():
    runtime = {"state": le.LOOPBACK_ONLY, "source": le.SOURCE_PROC, "confident": True,
               "lan_addresses": [], "detail": "listening, but only on loopback"}

    endpoints = _endpoints(lan_bind_env=None, runtime=runtime)

    assert endpoints["lan"] is None
    assert endpoints["lan_state"] == le.LOOPBACK_ONLY
    assert endpoints["lan_source"] == le.SOURCE_PROC


def test_an_uninspectable_host_says_unknown_not_unconfigured():
    runtime = {"state": le.UNKNOWN, "source": le.SOURCE_UNAVAILABLE, "confident": False,
               "lan_addresses": [], "detail": "could not enumerate sockets"}

    endpoints = _endpoints(lan_bind_env=None, runtime=runtime)

    assert endpoints["lan"] is None
    assert endpoints["lan_state"] == le.UNKNOWN
    assert "could not enumerate" in endpoints["lan_detail"]


def test_without_runtime_the_old_config_only_behaviour_is_unchanged():
    """Backward compatibility: every existing caller that passes no runtime
    still gets exactly what it got before."""
    endpoints = _endpoints(lan_bind_env=None)
    assert endpoints["lan"] is None
    assert "lans" not in endpoints

    configured = _endpoints(lan_bind_env="192.168.1.109")
    assert configured["lan"] == f"http://192.168.1.109:{PORT}"
    assert configured["lan_source"] == "config"


@pytest.mark.parametrize("address,expected", [("0100007F", le.UNKNOWN), ("6D01A8C0", le.LAN_BOUND)])
def test_partial_proc_read_cannot_prove_loopback_only(procfs, monkeypatch, address, expected):
    procfs["v4"].write_text(_PROC_HEADER + _proc_line(address))
    procfs["v6"].unlink()
    monkeypatch.setattr(le.shutil, "which", lambda name: None)
    state = le.lan_state(PORT)
    assert state["state"] == expected
    assert state["confident"] is (expected == le.LAN_BOUND)


def test_existing_but_failed_commands_are_not_observation(monkeypatch):
    monkeypatch.setattr(le.shutil, "which", lambda name: "/usr/bin/" + name)
    class Failed:
        returncode = 1
        stdout = ""
    monkeypatch.setattr(le.subprocess, "run", lambda *a, **kw: Failed())
    evidence = le.observe_listeners(PORT, readers=[le.SOURCE_SS, le.SOURCE_NETSTAT])
    assert evidence["observed"] is False


def test_successful_empty_command_is_observation(monkeypatch):
    monkeypatch.setattr(le.shutil, "which", lambda name: "/usr/bin/" + name)
    class Empty:
        returncode = 0
        stdout = ""
    monkeypatch.setattr(le.subprocess, "run", lambda *a, **kw: Empty())
    evidence = le.observe_listeners(PORT, readers=[le.SOURCE_SS])
    assert evidence["observed"] is True
    assert evidence["listeners"] == []
