"""Terminal MCP Bootstrap: the terminalmcp:// grammar, the one-time
handle, and the routes behind them.

The helper's input is a URL that ANY web page open on the machine can
trigger, and the helper runs things as Administrator. So most of this file
is a list of URLs that must be refused -- the passing cases are the small
part.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from terminal_mcp import bootstrap_protocol as proto
from terminal_mcp.enrollment import EnrollmentStore

ALLOWED = ("https://terminal-dashboard.example.net", "http://192.168.1.109:8766")


def _url(handle="a" * 32, controller="https://terminal-dashboard.example.net", action="enroll"):
    from urllib.parse import quote
    return f"terminalmcp://{action}?handle={handle}&controller={quote(controller, safe='')}"


# ---------------------------------------------------------------------------
# the grammar
# ---------------------------------------------------------------------------

def test_a_well_formed_enroll_url_parses():
    request = proto.parse(_url(), allowed_controllers=ALLOWED)
    assert request.action == "enroll"
    assert request.handle == "a" * 32
    assert request.controller == "https://terminal-dashboard.example.net"
    # The dict used for logging/status must not carry the handle.
    assert "handle" not in request.to_dict()
    assert request.to_dict()["has_handle"] is True


def test_no_field_can_carry_a_command():
    """The property that matters most: there is no parameter through which
    a page can hand the helper something to execute."""
    from urllib.parse import quote
    ctl = quote(ALLOWED[0], safe="")
    for hostile in (
        f"terminalmcp://enroll?handle={'a'*32}&controller={ctl}&cmd=calc.exe",
        f"terminalmcp://enroll?handle={'a'*32}&controller={ctl}&script=evil.ps1",
        f"terminalmcp://enroll?handle={'a'*32}&controller={ctl}&exec=whoami",
        f"terminalmcp://enroll?handle={'a'*32}&controller={ctl}&args=-Command+rm",
        f"terminalmcp://enroll?handle={'a'*32}&controller={ctl}&package=evil",
        f"terminalmcp://enroll?handle={'a'*32}&controller={ctl}&url=http://evil/x.ps1",
    ):
        with pytest.raises(proto.ProtocolError) as excinfo:
            proto.parse(hostile, allowed_controllers=ALLOWED)
        assert excinfo.value.reason == proto.ERR_EXTRA


def test_unknown_actions_are_refused():
    from urllib.parse import quote
    ctl = quote(ALLOWED[0], safe="")
    for action in ("install", "run", "exec", "..", "enroll2", ""):
        with pytest.raises(proto.ProtocolError) as excinfo:
            proto.parse(f"terminalmcp://{action}?handle={'a'*32}&controller={ctl}",
                        allowed_controllers=ALLOWED)
        assert excinfo.value.reason in (proto.ERR_ACTION, proto.ERR_MALFORMED)


def test_only_this_scheme_is_accepted():
    for url in ("https://enroll?handle=x", "file://enroll", "terminalmcpx://enroll", "javascript:alert(1)"):
        with pytest.raises(proto.ProtocolError):
            proto.parse(url, allowed_controllers=ALLOWED)


# Case and surrounding whitespace are NORMALISED before matching, not
# rejected -- see test_handle_case_is_normalised_then_matched_strictly.
# What must be refused is anything whose hex content is wrong.
@pytest.mark.parametrize("handle", [
    "", "short", "g" * 32, "a" * 31, "a" * 33,
    "../../etc/passwd", "a" * 16 + "/" + "a" * 15, "%2e%2e", "a" * 30 + "!!",
])
def test_malformed_handles_are_refused(handle):
    from urllib.parse import quote
    url = f"terminalmcp://enroll?handle={quote(handle, safe='')}&controller={quote(ALLOWED[0], safe='')}"
    with pytest.raises(proto.ProtocolError) as excinfo:
        proto.parse(url, allowed_controllers=ALLOWED)
    assert excinfo.value.reason in (proto.ERR_HANDLE, proto.ERR_EXTRA)


def test_handle_case_is_normalised_then_matched_strictly():
    """Windows can hand the handler back a case-mangled URL, so the check
    runs on the normalised value. Normalising is safe precisely because
    the match after it is exact-width strict hex."""
    assert proto.parse(_url(handle="A" * 32), allowed_controllers=ALLOWED).handle == "a" * 32
    assert proto.parse(_url(handle="  " + "b" * 32 + "  "), allowed_controllers=ALLOWED).handle == "b" * 32


# ---------------------------------------------------------------------------
# the controller allowlist -- "install my software instead" defence
# ---------------------------------------------------------------------------

def test_a_controller_outside_the_allowlist_is_refused():
    for hostile in ("https://evil.example", "https://terminal-dashboard.example.net.evil.com",
                    "http://terminal-dashboard.example.net", "https://terminal-dashboard.example.net:8443"):
        with pytest.raises(proto.ProtocolError) as excinfo:
            proto.parse(_url(controller=hostile), allowed_controllers=ALLOWED)
        assert excinfo.value.reason == proto.ERR_CONTROLLER


def test_an_empty_allowlist_refuses_everything():
    """A helper that does not know which controller owns it must do
    nothing, rather than trust whoever asked first."""
    with pytest.raises(proto.ProtocolError) as excinfo:
        proto.parse(_url(), allowed_controllers=())
    assert excinfo.value.reason == proto.ERR_CONTROLLER


def test_origins_compare_as_origins_not_strings():
    for equivalent in ("https://terminal-dashboard.example.net/",
                       "https://TERMINAL-DASHBOARD.example.net",
                       "https://terminal-dashboard.example.net/some/path"):
        assert proto.parse(_url(controller=equivalent), allowed_controllers=ALLOWED).controller == \
               "https://terminal-dashboard.example.net"


def test_repeated_parameters_are_refused():
    """Ambiguity is how one layer reads handle #1 while another reads #2."""
    from urllib.parse import quote
    ctl = quote(ALLOWED[0], safe="")
    url = f"terminalmcp://enroll?handle={'a'*32}&handle={'b'*32}&controller={ctl}"
    with pytest.raises(proto.ProtocolError) as excinfo:
        proto.parse(url, allowed_controllers=ALLOWED)
    assert excinfo.value.reason == proto.ERR_EXTRA


def test_path_segments_and_fragments_are_refused():
    from urllib.parse import quote
    ctl = quote(ALLOWED[0], safe="")
    for url in (f"terminalmcp://enroll/../../x?handle={'a'*32}&controller={ctl}",
                f"terminalmcp://enroll?handle={'a'*32}&controller={ctl}#frag"):
        with pytest.raises(proto.ProtocolError) as excinfo:
            proto.parse(url, allowed_controllers=ALLOWED)
        assert excinfo.value.reason == proto.ERR_EXTRA


def test_repair_and_status_take_no_handle():
    from urllib.parse import quote
    ctl = quote(ALLOWED[0], safe="")
    for action in ("repair", "status"):
        assert proto.parse(f"terminalmcp://{action}?controller={ctl}",
                           allowed_controllers=ALLOWED).handle is None
        with pytest.raises(proto.ProtocolError):
            proto.parse(f"terminalmcp://{action}?handle={'a'*32}&controller={ctl}",
                        allowed_controllers=ALLOWED)


def test_build_enroll_url_round_trips_and_rejects_bad_handles():
    url = proto.build_enroll_url(controller=ALLOWED[0], handle="b" * 32)
    assert proto.parse(url, allowed_controllers=ALLOWED).handle == "b" * 32
    for bad in ("", "zz", "A" * 32, "../x"):
        with pytest.raises(ValueError):
            proto.build_enroll_url(controller=ALLOWED[0], handle=bad)


# ---------------------------------------------------------------------------
# the handle itself
# ---------------------------------------------------------------------------

def test_handle_is_single_use_short_lived_and_hashed(tmp_path):
    store = EnrollmentStore(tmp_path / "e.db")
    record, _code = store.create(node_id="hnode")
    handle, expires_at = store.create_handle(record.id, created_by="op@example.com")

    assert len(handle) == 32
    assert handle.encode() not in (tmp_path / "e.db").read_bytes(), "handle must be hashed at rest"
    # ~2 minutes, not ~15: it covers a click, not a coffee break.
    delta = datetime.fromisoformat(expires_at) - datetime.now(timezone.utc)
    assert timedelta(seconds=60) < delta <= timedelta(seconds=180)

    assert store.redeem_handle(handle).node_id == "hnode"
    assert store.redeem_handle(handle) is None, "a handle must not be redeemable twice"


def test_expired_handle_is_refused(tmp_path):
    store = EnrollmentStore(tmp_path / "e.db")
    record, _code = store.create(node_id="hnode")
    handle, _ = store.create_handle(record.id)
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    assert store.redeem_handle(handle, now=future) is None


def test_handle_is_only_issued_for_a_pending_enrollment(tmp_path):
    store = EnrollmentStore(tmp_path / "e.db")
    record, code = store.create(node_id="hnode")
    store.consume(code, hostname="X")
    assert store.create_handle(record.id) is None
    assert store.create_handle("does-not-exist") is None


def test_concurrent_redemption_yields_one_winner(tmp_path):
    store = EnrollmentStore(tmp_path / "e.db")
    record, _code = store.create(node_id="race")
    handle, _ = store.create_handle(record.id)
    barrier = threading.Barrier(8)

    def attempt(_index):
        barrier.wait()
        return EnrollmentStore(tmp_path / "e.db").redeem_handle(handle)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert sum(1 for r in results if r is not None) == 1


def test_redeeming_a_handle_never_yields_the_enrollment_code(tmp_path):
    """The store keeps only sha256(code) and genuinely cannot reproduce
    one -- which is what makes the helper path safe: nothing it is left
    holding can be replayed."""
    store = EnrollmentStore(tmp_path / "e.db")
    record, code = store.create(node_id="hnode")
    handle, _ = store.create_handle(record.id)
    redeemed = store.redeem_handle(handle)
    assert code not in str(redeemed.to_dict())
    assert not hasattr(redeemed, "code")


# ---------------------------------------------------------------------------
# the routes
# ---------------------------------------------------------------------------

from tests.test_windows_onboarding import _client  # noqa: E402  (shared fixture)


def test_handle_route_requires_the_operator_guard(tmp_path, monkeypatch):
    """Minting a handle is what causes a helper to install software. It
    must sit behind the same boundary as every other dashboard mutation."""
    client, _controller, _onboarding = _client(tmp_path, monkeypatch)
    created = client.post("/dashboard/api/nodes/onboard/enrollments",
                          json={"node_id": "hbx", "profile": "minimal"}).json()
    blocked = client.post(
        f"/dashboard/api/nodes/onboard/enrollments/{created['enrollment']['id']}/handle",
        json={}, headers={"Origin": "https://evil.example"})
    assert blocked.status_code == 403 and blocked.json()["error"] == "ORIGIN_NOT_ALLOWED"


def test_handle_route_returns_a_parseable_url(tmp_path, monkeypatch):
    client, _controller, _onboarding = _client(tmp_path, monkeypatch)
    created = client.post("/dashboard/api/nodes/onboard/enrollments",
                          json={"node_id": "hbx", "profile": "minimal"}).json()
    issued = client.post(
        f"/dashboard/api/nodes/onboard/enrollments/{created['enrollment']['id']}/handle", json={})
    assert issued.status_code == 200
    body = issued.json()
    assert body["url"].startswith("terminalmcp://enroll?")
    parsed = proto.parse(body["url"], allowed_controllers=[body["controller"]])
    assert parsed.handle == body["handle"]
    # The enrollment CODE must not appear anywhere in the handle response.
    assert created["code"] not in str(body)


def test_handle_is_refused_for_a_spent_enrollment(tmp_path, monkeypatch):
    client, _controller, _onboarding = _client(tmp_path, monkeypatch)
    created = client.post("/dashboard/api/nodes/onboard/enrollments",
                          json={"node_id": "hbx", "profile": "minimal"}).json()
    client.post("/dashboard/api/enroll/consume",
                json={"code": created["code"], "hostname": "X", "addresses": {"lan_ip": "192.168.1.5"}})
    refused = client.post(
        f"/dashboard/api/nodes/onboard/enrollments/{created['enrollment']['id']}/handle", json={})
    assert refused.status_code == 409 and refused.json()["error"] == "ENROLLMENT_NOT_PENDING"


def test_redeem_gives_the_helper_a_full_bootstrap_and_registers_the_node(tmp_path, monkeypatch):
    client, controller, _onboarding = _client(tmp_path, monkeypatch)
    created = client.post("/dashboard/api/nodes/onboard/enrollments",
                          json={"node_id": "hbnode", "profile": "minimal"}).json()
    handle = client.post(
        f"/dashboard/api/nodes/onboard/enrollments/{created['enrollment']['id']}/handle",
        json={}).json()["handle"]

    redeemed = client.post("/dashboard/api/enroll/redeem",
                           json={"handle": handle, "hostname": "HB-PC",
                                 "addresses": {"lan_ip": "192.168.1.77"}})
    assert redeemed.status_code == 200
    payload = redeemed.json()
    assert payload["node_id"] == "hbnode"
    assert len(payload["node_token"]) == 64
    # Same payload shape as the code path -- one bootstrap, two doors.
    assert payload["ssh"]["password_authentication"] is False
    assert controller.node_status("hbnode") is not None
    # The code was never issued to the helper.
    assert created["code"] not in str(payload)

    # Replay of the handle is refused...
    again = client.post("/dashboard/api/enroll/redeem",
                        json={"handle": handle, "hostname": "ATTACKER"})
    assert again.status_code == 401 and again.json()["error"] == "HANDLE_INVALID"
    # ...and so is the original code, which redeem consumed.
    assert client.post("/dashboard/api/enroll/consume",
                       json={"code": created["code"], "hostname": "ATTACKER"}).status_code == 401


def test_redeem_refuses_a_forged_handle(tmp_path, monkeypatch):
    client, _controller, _onboarding = _client(tmp_path, monkeypatch)
    for forged in ("a" * 32, "not-a-handle", ""):
        response = client.post("/dashboard/api/enroll/redeem",
                               json={"handle": forged, "hostname": "X"})
        assert response.status_code in (400, 401)


# ---------------------------------------------------------------------------
# Dashboard: the CTA, and what it degrades to
# ---------------------------------------------------------------------------

def _page():
    import terminal_mcp.dashboard as dashboard_module
    return dashboard_module.NODES_ADMIN_HTML


def test_cta_exists_and_is_gated_on_real_detection():
    page = _page()
    assert 'id="anHelperBtn"' in page and "Kết nối máy này" in page
    assert "anDetectHelper" in page
    # Loopback only -- a helper reachable on a LAN address would be
    # drivable by anything on the network.
    assert "http://127.0.0.1:8791/detect" in page
    assert "product === 'terminal-mcp-bootstrap'" in page
    # Detection failure is the NORMAL case and must be silent.
    assert "return null;   // not installed, or not reachable" in page


def test_cta_mints_a_handle_per_click_and_never_sends_the_code():
    page = _page()
    assert "/handle`" in page and "method: 'POST'" in page
    # The page navigates to the protocol URL the SERVER built; it does not
    # assemble one, and it never puts the enrollment code in a URL.
    assert "issued.data.url" in page
    assert "terminalmcp://" not in page.split("anHelperBtn")[1][:2000], \
        "the page must not hand-build protocol URLs"


def test_copy_paste_flow_is_kept_and_only_demoted():
    """The existing flow is the fallback AND the first-machine path. It
    must still be there, not replaced."""
    page = _page()
    assert 'id="anQuickBox"' in page and 'id="anCopyCmdBtn"' in page
    assert "an-demoted" in page
    assert 'id="anDownloadBtn"' in page and "Cách khác / thủ công" in page


def test_helper_box_is_hidden_until_detection_succeeds():
    page = _page()
    helper_box = page[page.index('id="anHelperBox"'):page.index('id="anQuickBox"')]
    assert "hidden" in helper_box.split(">")[0], "the CTA must not show before the helper is found"
