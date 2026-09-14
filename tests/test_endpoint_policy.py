"""blg_0393ee9bd3d2 -- node endpoint scheme/host policy.

The bug: `nodes.remote[].endpoint` accepted any non-empty string, so
`http://a-public-host:8790` was a valid node endpoint and every request to
it carried `Authorization: Bearer <node token>` in the clear.

These tests pin three things: the rule itself (per address family), that
the gate cannot be bypassed through config / API / the controller, and
that every flow which was valid before this change still is.
"""
from __future__ import annotations

import ipaddress
import socket

import pytest
import yaml

from terminal_mcp import endpoint_policy as policy
from terminal_mcp.endpoint_policy import EndpointPolicyError, validate_node_endpoint


@pytest.fixture
def no_dns(monkeypatch):
    """Hostname tests must not depend on this machine's resolver. Each test
    declares what its names resolve to."""
    table: dict[str, list[str]] = {}

    def fake_getaddrinfo(host, *args, **kwargs):
        if host not in table:
            raise OSError(-2, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in table[host]]

    monkeypatch.setattr(policy.socket, "getaddrinfo", fake_getaddrinfo)
    return table


# ---------------------------------------------------------------------------
# the rule, per address family
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", [
    "local",                                  # the in-process sentinel
    "http://127.0.0.1:8790",                  # loopback v4
    "http://127.10.20.30:8790",               # all of 127/8
    "http://[::1]:8790",                      # loopback v6
    "http://10.1.2.3:8790",                   # RFC1918
    "http://172.16.0.5:8790",
    "http://172.31.255.254:8790",
    "http://192.168.1.250:8790",
    "http://100.64.0.1:8790",                 # CGNAT lower bound
    "http://100.67.53.117:8790",              # a real tailnet address
    "http://100.127.255.254:8790",            # CGNAT upper bound
    "http://169.254.1.1:8790",                # link-local v4 (existing policy)
    "http://[fc00::1]:8790",                  # IPv6 ULA
    "http://[fd12:3456::1]:8790",
    "http://[fe80::1]:8790",                  # link-local v6
])
def test_plaintext_to_a_private_address_is_allowed(endpoint):
    assert validate_node_endpoint(endpoint, context="test") == endpoint


@pytest.mark.parametrize("endpoint", [
    "https://node.example.com:8790",
    "https://8.8.8.8:8790",
    "https://[2001:4860:4860::8888]:8790",
    "https://127.0.0.1:8790",
])
def test_https_is_always_allowed(endpoint):
    """TLS is what protects the token, and it does so wherever the host
    is. Refusing public https would break the very fix we recommend."""
    assert validate_node_endpoint(endpoint, context="test") == endpoint


@pytest.mark.parametrize("endpoint,expected", [
    ("http://8.8.8.8:8790", policy.REASON_PUBLIC_PLAINTEXT),
    ("http://1.1.1.1", policy.REASON_PUBLIC_PLAINTEXT),
    ("http://99.99.99.99:8790", policy.REASON_PUBLIC_PLAINTEXT),
    ("http://100.128.0.1:8790", policy.REASON_PUBLIC_PLAINTEXT),   # just past CGNAT
    ("http://100.63.255.255:8790", policy.REASON_PUBLIC_PLAINTEXT),  # just before CGNAT
    ("http://[2001:4860:4860::8888]:8790", policy.REASON_PUBLIC_PLAINTEXT),
])
def test_plaintext_to_a_public_address_is_refused(endpoint, expected):
    with pytest.raises(EndpointPolicyError) as excinfo:
        validate_node_endpoint(endpoint, context="test")
    assert excinfo.value.reason == expected


def test_cgnat_boundaries_are_exact():
    """100.64.0.0/10 is 100.64.0.0 - 100.127.255.255. Off-by-one here
    would either leak (too wide) or break every tailnet node (too narrow)."""
    for inside in ("100.64.0.0", "100.127.255.255"):
        assert policy.is_private_address(ipaddress.ip_address(inside))
    for outside in ("100.63.255.255", "100.128.0.0"):
        assert not policy.is_private_address(ipaddress.ip_address(outside))


@pytest.mark.parametrize("endpoint,expected", [
    ("", policy.REASON_EMPTY),
    ("   ", policy.REASON_EMPTY),
    ("ftp://10.0.0.1", policy.REASON_SCHEME),
    ("ssh://10.0.0.1", policy.REASON_SCHEME),
    ("file:///etc/passwd", policy.REASON_SCHEME),
    ("javascript:alert(1)", policy.REASON_SCHEME),
    ("not-a-url", policy.REASON_SCHEME),
    ("10.0.0.1:8790", policy.REASON_SCHEME),        # missing scheme entirely
    ("http://", policy.REASON_NO_HOST),
    ("http:///path", policy.REASON_NO_HOST),
])
def test_malformed_endpoints_are_refused(endpoint, expected):
    with pytest.raises(EndpointPolicyError) as excinfo:
        validate_node_endpoint(endpoint, context="test")
    assert excinfo.value.reason == expected


# ---------------------------------------------------------------------------
# hostnames -- resolution, and failing closed
# ---------------------------------------------------------------------------

def test_hostname_resolving_private_is_allowed(no_dns):
    no_dns["node.internal"] = ["192.168.1.50"]
    assert validate_node_endpoint("http://node.internal:8790", context="test")


def test_hostname_resolving_public_is_refused(no_dns):
    no_dns["node.example.com"] = ["93.184.216.34"]
    with pytest.raises(EndpointPolicyError) as excinfo:
        validate_node_endpoint("http://node.example.com:8790", context="test")
    assert excinfo.value.reason == policy.REASON_PUBLIC_PLAINTEXT


def test_unresolvable_hostname_fails_closed(no_dns):
    """Unknown is not 'probably fine'. A name we cannot resolve could point
    anywhere, including somewhere public a moment later."""
    with pytest.raises(EndpointPolicyError) as excinfo:
        validate_node_endpoint("http://nowhere.invalid:8790", context="test")
    assert excinfo.value.reason == policy.REASON_UNRESOLVABLE


def test_mixed_resolution_is_refused(no_dns):
    """The DNS-rebinding shape: trusting whichever address is tried first
    is exactly what remote_connect already refuses to do."""
    no_dns["dual.example.com"] = ["192.168.1.9", "93.184.216.34"]
    with pytest.raises(EndpointPolicyError) as excinfo:
        validate_node_endpoint("http://dual.example.com:8790", context="test")
    assert excinfo.value.reason == policy.REASON_MIXED


def test_localhost_keeps_working_even_with_a_stubbed_resolver(no_dns):
    no_dns["localhost"] = ["127.0.0.1"]
    assert validate_node_endpoint("http://localhost:8790", context="test")


def test_https_never_resolves_anything(no_dns):
    """An https endpoint must not be refused just because DNS is down --
    and must not cost a lookup at all."""
    assert validate_node_endpoint("https://unresolvable.invalid:8790", context="test")


# ---------------------------------------------------------------------------
# operator-declared ranges
# ---------------------------------------------------------------------------

def test_operator_declared_cidr_is_honoured(monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_TRUSTED_VPN_CIDRS", "203.0.113.0/24")
    # 203.0.113.0/24 is TEST-NET-3 -- not globally routable, so the
    # existing trusted-CIDR parser accepts it.
    assert validate_node_endpoint("http://203.0.113.5:8790", context="test")


def test_a_malformed_operator_allowlist_does_not_widen_the_gate(monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_TRUSTED_VPN_CIDRS", "not-a-cidr")
    with pytest.raises(EndpointPolicyError):
        validate_node_endpoint("http://8.8.8.8:8790", context="test")
    # ...and does not narrow it either.
    assert validate_node_endpoint("http://10.0.0.1:8790", context="test")


def test_cgnat_allowed_without_the_env_var(monkeypatch):
    """Deliberate divergence from is_lan_scannable: 100.64/10 is RFC 6598
    shared space and is never public, so it must not depend on an env var
    that a CLI shell may not export -- every tailnet deployment's config
    would otherwise stop loading."""
    monkeypatch.delenv("TERMINAL_MCP_TRUSTED_VPN_CIDRS", raising=False)
    assert validate_node_endpoint("http://100.67.53.117:8790", context="test")


# ---------------------------------------------------------------------------
# error quality
# ---------------------------------------------------------------------------

def test_error_is_actionable_and_leaks_nothing():
    with pytest.raises(EndpointPolicyError) as excinfo:
        validate_node_endpoint("http://8.8.8.8:8790", context="nodes.remote[2].endpoint")
    message = str(excinfo.value)
    assert "nodes.remote[2].endpoint" in message, "must say WHICH endpoint"
    assert "https://" in message, "must say what to do instead"
    assert "100.64.0.0/10" in message or "RFC1918" in message, "must name the private option"
    # The validator is never handed a token, so it cannot print one -- this
    # guards against someone later passing the whole node config in.
    assert "Bearer" not in message
    assert "token" in message.lower()  # explains WHY, without containing one


# ---------------------------------------------------------------------------
# the chokepoints -- config / controller / API
# ---------------------------------------------------------------------------

def _config_yaml(tmp_path, endpoint):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "permissions": {"terminal_read": True, "terminal_input": False},
        "allowed_session_patterns": ["test-*"],
        "nodes": {"remote": [{"node_id": "n1", "endpoint": endpoint,
                              "token_env": "TERMINAL_MCP_NODE_TOKEN_N1"}]},
    }))
    return path


def test_config_refuses_plaintext_public_at_startup(tmp_path):
    from terminal_mcp.config import load_config
    with pytest.raises(ValueError) as excinfo:
        load_config(_config_yaml(tmp_path, "http://8.8.8.8:8790"))
    assert "nodes.remote[0].endpoint" in str(excinfo.value)


@pytest.mark.parametrize("endpoint", [
    "http://192.168.1.250:8790", "http://100.67.53.117:8790",
    "http://127.0.0.1:8790", "https://node.example.com:8790",
])
def test_config_still_accepts_every_previously_valid_endpoint(tmp_path, endpoint):
    """Backward compatibility: this is the shape of every endpoint in the
    real deployment's config today."""
    from terminal_mcp.config import load_config
    config = load_config(_config_yaml(tmp_path, endpoint))
    assert config.nodes.remote_nodes[0].endpoint == endpoint


def _controller(tmp_path):
    from terminal_mcp.controller import ControllerService
    from terminal_mcp.node_registry import NodeRegistry
    return ControllerService(NodeRegistry(tmp_path / "nodes.db"), local_client=None,
                             local_workspace_root=str(tmp_path))


def test_controller_is_the_chokepoint(tmp_path):
    """Even a caller that skipped every other check cannot get a
    RemoteNodeClient -- the thing that actually puts a bearer token on a
    wire -- built for a refused endpoint."""
    controller = _controller(tmp_path)
    with pytest.raises(EndpointPolicyError):
        controller.register_remote_node("n1", display_name="n", hostname="h",
                                        endpoint="http://8.8.8.8:8790", token="t")
    assert controller.node_status("n1") is None, "a refused node must not be registered at all"
    assert controller.client_for("n1") is None


def test_controller_still_registers_valid_endpoints(tmp_path):
    controller = _controller(tmp_path)
    for index, endpoint in enumerate(("http://192.168.1.9:8790", "https://n.example.com")):
        node_id = f"n{index}"
        controller.register_remote_node(node_id, display_name=node_id, hostname="h",
                                        endpoint=endpoint, token="t")
        assert controller.node_status(node_id) is not None


def test_api_refuses_plaintext_public_with_an_actionable_400(tmp_path, monkeypatch):
    """The route has TWO gates: the pre-existing host check (is this host
    public?) and the new scheme check (would the token travel in
    plaintext?). The host check runs first and owns the INVALID_REQUEST
    contract, so this asserts the OUTCOME -- a 400 the operator can act on,
    and no 500 from deeper in -- rather than which gate happened to fire."""
    from tests.test_windows_onboarding import _client
    client, _controller, _onboarding = _client(tmp_path, monkeypatch)
    response = client.post("/dashboard/api/nodes/connect/agent-token",
                           json={"node_id": "n1", "endpoint": "http://8.8.8.8:8790", "token": "x" * 64})
    assert response.status_code == 400
    body = response.json()
    assert body["error"] in ("INVALID_REQUEST", policy.REASON_PUBLIC_PLAINTEXT)
    assert body["detail"], "a refusal must say why"
    assert "x" * 64 not in str(body), "the token must never be echoed back"


def test_api_scheme_gate_is_reachable_when_the_host_gate_opts_in(tmp_path, monkeypatch):
    """With allow_public_manual_add on, the host gate passes -- and the
    scheme gate is passed the SAME opt-in, so the documented escape hatch
    still works end to end rather than being half-blocked by this change."""
    from terminal_mcp.config import NodesConfig, RemoteConnectConfig
    from tests.test_windows_onboarding import _client, _config
    config = _config(tmp_path)
    object.__setattr__(config, "nodes", NodesConfig(
        onboarding=config.nodes.onboarding,
        remote_connect=RemoteConnectConfig(allow_public_manual_add=True)))
    client, _controller, _onboarding = _client(tmp_path, monkeypatch, config=config)
    response = client.post("/dashboard/api/nodes/connect/agent-token",
                           json={"node_id": "n1", "endpoint": "http://8.8.8.8:8790", "token": "x" * 64})
    # Gets past BOTH gates and fails on reachability, which is the right
    # reason -- nothing listens at 8.8.8.8:8790.
    assert response.status_code == 502
    assert response.json()["error"] == "AGENT_NOT_REACHABLE"


def test_describe_policy_lists_the_rule():
    described = policy.describe_policy()
    assert described["https"] == "always allowed"
    for network in ("100.64.0.0/10", "fc00::/7", "127.0.0.0/8"):
        assert network in described["private_ranges"]
