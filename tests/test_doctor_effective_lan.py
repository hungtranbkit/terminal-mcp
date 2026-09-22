"""terminal-mcp-doctor must report the EFFECTIVE runtime LAN state, not
a static config guess (backlog blg_316183197b4c).

The bug it fixes, reproduced live on dell-linux before this change:

    lan:      not configured (loopback-only -- set TERMINAL_MCP_LAN_BIND to enable)

while `ss -ltn` showed `100.81.85.120:8766` LISTENing. The doctor is a
separate process from the service and never inherited the service's
systemd EnvironmentFile, so its own os.environ was empty.

The security-relevant half of these tests is that the fix does NOT
overcorrect into trusting config: when socket evidence is unreadable the
state is "unknown", never "lan", no matter how loudly config declares a
bind -- see test_unknown_* below. This host has a real stale-config trap
to justify that: a retired, INACTIVE unit whose override.conf still pins
192.168.1.132 -- an address that does currently exist on wlp60s0, with
nothing listening on it.
"""
from __future__ import annotations

import ipaddress

import pytest

from terminal_mcp import doctor, effective_bind
from terminal_mcp.effective_bind import describe_effective_lan, parse_listening

PORT = 8766
OTHER_PORT = 9999


# --- /proc/net/tcp* fixture builders ---------------------------------

def _hex_addr(address: str) -> str:
    """Encode like the kernel does: 32-bit words in host (little-endian)
    byte order, i.e. each 4-byte group reversed."""
    packed = ipaddress.ip_address(address).packed
    return "".join(packed[i:i + 4][::-1].hex().upper() for i in range(0, len(packed), 4))


def _table(rows: list[tuple[str, int, str]]) -> str:
    """rows: (local address, local port, st). st '0A' is LISTEN."""
    lines = ["  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode"]
    for index, (address, port, state) in enumerate(rows):
        remote = "0" * len(_hex_addr(address))
        lines.append(
            f"  {index}: {_hex_addr(address)}:{port:04X} {remote}:0000 {state} "
            "00000000:00000000 00:00000000 00000000  1000 0 134974 1 0000000000000000 100 0 0 10 0"
        )
    return "\n".join(lines) + "\n"


def _reader(text: str):
    return lambda: text


def _boom(message: str = "Permission denied"):
    def _raise():
        raise OSError(13, message)
    return _raise


EMPTY4 = _table([])
EMPTY6 = _table([])


def _describe(*, tcp4: str | None = EMPTY4, tcp6: str | None = EMPTY6, declared: str | None = None):
    return describe_effective_lan(
        port=PORT, declared_lan_bind=declared,
        proc_net_tcp=_reader(tcp4) if tcp4 is not None else _boom("/proc/net/tcp unreadable"),
        proc_net_tcp6=_reader(tcp6) if tcp6 is not None else _boom("/proc/net/tcp6 unreadable"),
    )


# --- the encoder matches the real kernel table -----------------------

def test_encoder_matches_a_real_proc_row_observed_on_this_host():
    """Grounding test: this exact hex was read out of /proc/net/tcp on
    dell-linux while the controller was bound to the tailnet address. If
    the byte-order handling ever regresses, every other test in this file
    would still pass against its own encoder -- this one would not."""
    assert _hex_addr("100.81.85.120") == "78555164"
    assert _hex_addr("127.0.0.1") == "0100007F"
    real_row = ("  42: 78555164:223E 00000000:0000 0A 00000000:00000000 "
                "00:00000000 00000000  1000 0 134975 1 0000000000000000 100 0 0 10 0")
    header = "  sl  local_address rem_address st tx_queue"
    assert parse_listening(f"{header}\n{real_row}\n", port=PORT) == ("100.81.85.120",)


# --- loopback-only ----------------------------------------------------

def test_loopback_only_ipv4():
    result = _describe(tcp4=_table([("127.0.0.1", PORT, "0A")]))
    assert result["state"] == "loopback-only"
    assert result["confidence"] == "effective"
    assert result["lan_addresses"] == ()
    assert result["loopback_addresses"] == ("127.0.0.1",)
    assert result["mismatch"] is None


def test_loopback_only_ipv6():
    result = _describe(tcp6=_table([("::1", PORT, "0A")]))
    assert result["state"] == "loopback-only"
    assert result["loopback_addresses"] == ("::1",)


def test_loopback_only_ipv4_mapped_in_the_ipv6_table():
    """::ffff:127.0.0.1 is the IPv4 loopback and must classify as such,
    not as some unrecognised IPv6 address that looks off-loopback."""
    result = _describe(tcp6=_table([("::ffff:127.0.0.1", PORT, "0A")]))
    assert result["state"] == "loopback-only"
    assert result["loopback_addresses"] == ("127.0.0.1",)


# --- LAN bind (the reported bug) -------------------------------------

def test_lan_bind_ipv4_is_reported_even_with_no_config_in_this_process():
    """THE REGRESSION TEST. Empty environment, live off-loopback socket:
    the answer must be "lan", sourced from the sockets."""
    result = _describe(tcp4=_table([("127.0.0.1", PORT, "0A"), ("100.81.85.120", PORT, "0A")]),
                       declared=None)
    assert result["state"] == "lan"
    assert result["lan_addresses"] == ("100.81.85.120",)
    assert result["confidence"] == "effective"
    assert "/proc/net/tcp" in result["source"]
    assert "no TERMINAL_MCP_LAN_BIND" in result["mismatch"]


def test_lan_bind_ipv6_global_address():
    result = _describe(tcp6=_table([("fd00::1", PORT, "0A")]))
    assert result["state"] == "lan"
    assert result["lan_addresses"] == ("fd00::1",)


def test_lan_bind_across_both_families_is_merged():
    result = _describe(tcp4=_table([("127.0.0.1", PORT, "0A"), ("192.168.1.50", PORT, "0A")]),
                       tcp6=_table([("::1", PORT, "0A"), ("fd00::1", PORT, "0A")]))
    assert result["state"] == "lan"
    assert set(result["lan_addresses"]) == {"192.168.1.50", "fd00::1"}
    assert set(result["loopback_addresses"]) == {"127.0.0.1", "::1"}


@pytest.mark.parametrize("wildcard,table", [("0.0.0.0", "tcp4"), ("::", "tcp6")])
def test_wildcard_bind_reports_all_interfaces_not_merely_lan(wildcard, table):
    """A 0.0.0.0/:: bind is broader than any named LAN address;
    collapsing it into "lan" would understate the exposure."""
    rows = _table([(wildcard, PORT, "0A")])
    result = _describe(**{table: rows})
    assert result["state"] == "all-interfaces"
    assert "ALL interfaces" in result["mismatch"]


# --- service not running ---------------------------------------------

def test_service_not_running_is_distinct_from_loopback_only():
    result = _describe()
    assert result["state"] == "service-not-running"
    assert result["confidence"] == "effective"
    assert result["listening"] == ()


def test_service_not_running_with_declared_config_never_claims_exposure():
    result = _describe(declared="192.168.1.132")
    assert result["state"] == "service-not-running"
    assert result["lan_addresses"] == ()
    assert "intent only, not exposure" in result["mismatch"]


# --- config override / drop-in (stale config must not win) -----------

def test_stale_drop_in_config_does_not_create_a_phantom_lan_bind():
    """The retired terminal-mcp-http.service on this host still pins
    TERMINAL_MCP_LAN_BIND=192.168.1.132 in its override.conf while the
    unit is inactive. Verified live: that address DOES exist (wlp60s0)
    and yet nothing is listening on it -- so an existing address is not
    evidence of a bind, and config declaring one must never promote the
    state to "lan"."""
    result = _describe(tcp4=_table([("127.0.0.1", PORT, "0A")]), declared="192.168.1.132")
    assert result["state"] == "loopback-only"
    assert result["lan_addresses"] == ()
    assert result["declared_lan_bind"] == ("192.168.1.132",)
    assert "stale config" in result["mismatch"]


def test_multi_address_override_reports_which_declared_address_is_absent():
    """override.conf declares 192.168.1.132,100.81.85.120; only the
    tailnet address is actually up."""
    result = _describe(tcp4=_table([("127.0.0.1", PORT, "0A"), ("100.81.85.120", PORT, "0A")]),
                       declared="192.168.1.132,100.81.85.120")
    assert result["state"] == "lan"
    assert "declared but NOT listening: 192.168.1.132" in result["mismatch"]


def test_declared_auto_is_not_reported_as_a_missing_address():
    result = _describe(tcp4=_table([("100.81.85.120", PORT, "0A")]), declared="auto")
    assert result["state"] == "lan"
    assert result["mismatch"] is None


def test_config_matching_reality_produces_no_mismatch():
    result = _describe(tcp4=_table([("127.0.0.1", PORT, "0A"), ("100.81.85.120", PORT, "0A")]),
                       declared="100.81.85.120")
    assert result["state"] == "lan"
    assert result["mismatch"] is None


# --- ambiguous / unknown (the security-preserving direction) ---------

def test_unknown_when_no_socket_evidence_is_readable():
    result = _describe(tcp4=None, tcp6=None)
    assert result["state"] == "unknown"
    assert result["confidence"] == "unknown"
    assert result["source"] == "unavailable"
    assert len(result["evidence_errors"]) == 2


def test_unknown_never_promoted_to_lan_by_config_alone():
    """The whole point of the fix: config is intent, not evidence."""
    result = _describe(tcp4=None, tcp6=None, declared="100.81.85.120")
    assert result["state"] == "unknown"
    assert result["lan_addresses"] == ()
    assert "NOT verified" in result["mismatch"]
    assert "not assumed from config" in result["mismatch"]


def test_partial_read_showing_exposure_still_reports_lan():
    """Positive evidence survives a partial read: an unreadable IPv6
    table cannot un-see an IPv4 LAN listener."""
    result = _describe(tcp4=_table([("100.81.85.120", PORT, "0A")]), tcp6=None)
    assert result["state"] == "lan"
    assert result["lan_addresses"] == ("100.81.85.120",)
    assert result["evidence_errors"]


def test_partial_read_without_exposure_is_unknown_not_loopback_only():
    """The unsafe direction. IPv4 shows loopback only, but IPv6 was
    unreadable -- exposure cannot be ruled out, so it must not be
    reported as loopback-only."""
    result = _describe(tcp4=_table([("127.0.0.1", PORT, "0A")]), tcp6=None)
    assert result["state"] == "unknown"
    assert result["confidence"] == "unknown"


# --- parser hygiene ---------------------------------------------------

def test_non_listening_states_are_ignored():
    """This host really does show 06 (TIME_WAIT) rows on port 8766;
    counting one as a bind would invent exposure."""
    rows = _table([("100.81.85.120", PORT, "06"), ("127.0.0.1", PORT, "0A")])
    result = _describe(tcp4=rows)
    assert result["state"] == "loopback-only"


def test_other_ports_are_ignored():
    result = _describe(tcp4=_table([("100.81.85.120", OTHER_PORT, "0A")]))
    assert result["state"] == "service-not-running"


def test_malformed_rows_do_not_raise():
    text = "header\ngarbage\n  1: NOTHEX:223E 0 0A\n\n"
    assert parse_listening(text, port=PORT) == ()


def test_duplicate_listeners_are_deduplicated():
    rows = _table([("100.81.85.120", PORT, "0A"), ("100.81.85.120", PORT, "0A")])
    assert parse_listening(rows, port=PORT) == ("100.81.85.120",)


# --- doctor output must name its source ------------------------------

def _render(addresses, capsys, monkeypatch, *, declared=None, observed=True) -> str:
    import argparse
    from terminal_mcp import listen_evidence

    monkeypatch.setattr(listen_evidence, "observe_listeners", lambda *a, **kw: {
        "observed": observed,
        "source": listen_evidence.SOURCE_PROC if observed else "unavailable",
        "listeners": [listen_evidence.Listener(ip, PORT, "ipv4", "proc_net_tcp").as_dict()
                      for ip in addresses],
    })
    monkeypatch.setattr(doctor, "default_state_path", lambda: None)
    monkeypatch.setattr(doctor.WatchdogState, "load", lambda path: None)
    monkeypatch.delenv("TERMINAL_MCP_LAN_BIND", raising=False)
    monkeypatch.setenv("TERMINAL_MCP_ALLOWED_NODE_CIDRS", "192.168.0.0/16,100.64.0.0/10")
    monkeypatch.setenv("TERMINAL_MCP_TRUSTED_VPN_CIDRS", "100.64.0.0/10")
    if declared:
        monkeypatch.setenv("TERMINAL_MCP_LAN_BIND", declared)
    monkeypatch.setattr(doctor, "diagnose", lambda **kwargs: {
        "mcp_local": "healthy", "mcp_local_detail": "ok", "tunnel_process": "active",
        "tunnel_process_sub_state": "running", "tunnel_ready": "ready",
        "last_heartbeat_age_sec": 1, "network_dns_tls": "pass", "network_dns_tls_detail": "ok",
        "chatgpt_side": "ok", "last_recovery_action": "none", "last_recovery_action_reason": "",
        "last_recovery_action_at": "", "recommended_action": "none",
    })
    assert doctor.cmd_connection(argparse.Namespace(json=False, stale_threshold=60)) == 0
    return capsys.readouterr().out


def test_output_states_the_source_for_a_live_lan_bind(capsys, monkeypatch):
    out = _render(["127.0.0.1", "100.81.85.120"], capsys, monkeypatch)
    assert f"lan:      http://100.81.85.120:{PORT}" in out
    assert "source=proc_net_tcp" in out
    assert "not configured" not in out


def test_output_states_the_source_for_loopback_only(capsys, monkeypatch):
    out = _render(["127.0.0.1"], capsys, monkeypatch)
    assert "loopback-only" in out
    assert "observed via proc_net_tcp" in out


def test_output_refuses_to_assume_exposure_when_evidence_is_missing(capsys, monkeypatch):
    out = _render([], capsys, monkeypatch, observed=False, declared="100.81.85.120")
    assert "UNKNOWN" in out
    assert "exposure NOT verified" in out
    assert "declared intent" in out


def test_output_flags_a_config_runtime_mismatch(capsys, monkeypatch):
    out = _render(["127.0.0.1"], capsys, monkeypatch, declared="192.168.1.132")
    assert "loopback-only" in out
    assert "config drift" in out
    assert "192.168.1.132 configured but not listening" in out
    assert f"lan:      http://192.168.1.132:{PORT}" not in out


def test_real_proc_tables_are_parseable_on_this_host():
    """Integration smoke: the real files exist, are readable, and the
    parser survives them. Asserts nothing about THIS host's exposure --
    that is deployment state, not a property of the code."""
    result = describe_effective_lan(port=PORT)
    assert result["state"] in (
        effective_bind.STATE_LAN, effective_bind.STATE_ALL_INTERFACES,
        effective_bind.STATE_LOOPBACK_ONLY, effective_bind.STATE_NOT_RUNNING,
        effective_bind.STATE_UNKNOWN,
    )
    assert result["confidence"] in ("effective", "unknown")
