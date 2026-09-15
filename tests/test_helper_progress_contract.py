"""The helper's own progress contract: a longer pairing window, and the
stages it reports BEFORE the installer exists to report anything.

What these pin down came out of a live production run. The operator clicked
the CTA, the browser created an enrollment, minted a handle and downloaded
the paired helper -- all three 200 -- and then the Dashboard showed nothing
for the rest of the enrollment's life. Two independent causes:

  * the pairing handle lived 120 seconds, measured from the DOWNLOAD, so it
    was routinely dead before a human had cleared SmartScreen and UAC
  * the helper had no way to say anything before redeeming, because the
    only progress route authenticated by an enrollment code it does not
    hold

So the handle is now a first-class progress authenticator, and its TTL is
configurable with a default that matches the enrollment it belongs to.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from terminal_mcp import enrollment as enroll
from terminal_mcp.config import OnboardingConfig, _load_onboarding_config
from terminal_mcp.enrollment import (
    HANDLE_TTL_SECONDS,
    HELPER_STAGES,
    STAGE_LABELS,
    STAGES,
    EnrollmentStore,
)


@pytest.fixture
def store(tmp_path):
    return EnrollmentStore(tmp_path / "enrollment.db")


def _pending(store, node_id="win-work"):
    """create() returns (record, plaintext_code); these tests never need the
    code -- the handle is the whole point -- so it is dropped here."""
    record, _code = store.create(node_id=node_id, display_name=node_id,
                                 os_name="windows", profile="minimal",
                                 connectivity={}, ttl_seconds=900)
    return record


# ---------------------------------------------------------------------------
# 1. the pairing window
# ---------------------------------------------------------------------------

def test_the_pairing_handle_now_lives_fifteen_minutes():
    """120s was measured from the download and had to cover a human finding
    the file, clearing SmartScreen and accepting UAC. It routinely did
    not."""
    assert HANDLE_TTL_SECONDS == 900


def test_the_ttl_is_configurable_and_defaults_to_the_same_value():
    assert OnboardingConfig().pairing_handle_ttl_seconds == 900
    loaded = _load_onboarding_config({"pairing_handle_ttl_seconds": 600})
    assert loaded.pairing_handle_ttl_seconds == 600


@pytest.mark.parametrize("bad", [29, 3601, 0, -1])
def test_an_unreasonable_ttl_is_refused_at_load(bad):
    with pytest.raises(ValueError, match="pairing_handle_ttl_seconds"):
        _load_onboarding_config({"pairing_handle_ttl_seconds": bad})


def test_the_longer_ttl_is_actually_applied_to_a_minted_handle(store):
    record = _pending(store)
    issued = store.create_handle(record.id, ttl_seconds=900)
    assert issued is not None
    _handle, expires_at = issued
    lifetime = datetime.fromisoformat(expires_at) - enroll._now()
    # Allow a second of slack for the clock between mint and assertion.
    assert timedelta(seconds=880) < lifetime <= timedelta(seconds=900)


def test_the_window_widens_but_single_use_does_not(store):
    """The whole risk of a longer TTL is that it might also mean 'more
    redemptions'. It does not: the second attempt still loses."""
    record = _pending(store)
    handle, _expires = store.create_handle(record.id, ttl_seconds=900)

    first = store.redeem_handle(handle)
    replay = store.redeem_handle(handle)

    assert first is not None and first.id == record.id
    assert replay is None


def test_an_expired_handle_still_fails_closed(store):
    record = _pending(store)
    handle, _expires = store.create_handle(record.id, ttl_seconds=30)
    later = enroll._now() + timedelta(seconds=31)

    assert store.redeem_handle(handle, now=later) is None


# ---------------------------------------------------------------------------
# 2. helper stages
# ---------------------------------------------------------------------------

def test_the_six_helper_stages_exist_and_come_before_the_installer_stages():
    assert HELPER_STAGES == ("helper_started", "redeeming", "redeemed",
                             "installing_service", "launching_setup", "setup_started")
    # Ordered before 'starting': the helper narrates the window the
    # installer does not exist for yet.
    assert STAGES.index("setup_started") < STAGES.index("starting")


def test_every_stage_has_a_vietnamese_label_the_dashboard_can_render():
    """progress_label is what the panel shows; a stage without one renders
    as a raw identifier."""
    for stage in STAGES:
        assert STAGE_LABELS.get(stage), stage
        assert STAGE_LABELS[stage] != stage


# ---------------------------------------------------------------------------
# 3. progress authenticated by the handle
# ---------------------------------------------------------------------------

def test_the_helper_can_report_progress_before_it_redeems(store):
    """The whole point: helper_started and redeeming happen before any
    exchange, so they cannot be authenticated by an enrollment code."""
    record = _pending(store)
    handle, _expires = store.create_handle(record.id, ttl_seconds=900)

    updated = store.record_progress_by_handle(handle, stage="helper_started")

    assert updated is not None
    assert store.get(record.id).progress_stage == "helper_started"


def test_reporting_progress_does_not_spend_the_pairing(store):
    """Narration must not consume the one-shot credential -- otherwise the
    helper's first report would break its own redeem."""
    record = _pending(store)
    handle, _expires = store.create_handle(record.id, ttl_seconds=900)

    store.record_progress_by_handle(handle, stage="helper_started")
    redeemed = store.redeem_handle(handle)

    assert redeemed is not None, "the handle must still be redeemable"


def test_the_helper_keeps_narrating_after_it_has_redeemed(store):
    """installing_service and launching_setup happen after redemption, so a
    redeemed handle must still authenticate progress."""
    record = _pending(store)
    handle, _expires = store.create_handle(record.id, ttl_seconds=900)
    store.redeem_handle(handle)

    updated = store.record_progress_by_handle(handle, stage="launching_setup")

    assert updated is not None
    assert store.get(record.id).progress_stage == "launching_setup"


def test_progress_by_handle_is_idempotent_and_last_write_wins(store):
    record = _pending(store)
    handle, _expires = store.create_handle(record.id, ttl_seconds=900)

    store.record_progress_by_handle(handle, stage="redeeming")
    store.record_progress_by_handle(handle, stage="redeeming")
    store.record_progress_by_handle(handle, stage="redeemed")

    assert store.get(record.id).progress_stage == "redeemed"


@pytest.mark.parametrize("bogus", [
    "", "   ", "not-a-handle", "0123456789abcdef0123456789abcde",      # 31 chars
    "0123456789abcdef0123456789abcdefa",                               # 33 chars
    "0123456789ABCDEF0123456789ABCDEFX", "../../etc/passwd",
])
def test_a_malformed_handle_reports_nothing(store, bogus):
    _pending(store)
    assert store.record_progress_by_handle(bogus, stage="helper_started") is None


def test_an_unknown_handle_reports_nothing(store):
    _pending(store)
    assert store.record_progress_by_handle("f" * 32, stage="helper_started") is None


def test_a_revoked_enrollment_stops_accepting_narration(store):
    """It cannot complete, so progress against it would only ever be a
    misleading spinner."""
    record = _pending(store)
    handle, _expires = store.create_handle(record.id, ttl_seconds=900)
    store.revoke(record.id)

    assert store.record_progress_by_handle(handle, stage="redeeming") is None


def test_an_expired_enrollment_stops_accepting_narration(store):
    record = _pending(store)
    handle, _expires = store.create_handle(record.id, ttl_seconds=900)
    later = enroll._now() + timedelta(seconds=901)

    assert store.record_progress_by_handle(handle, stage="redeeming", now=later) is None


def test_an_unknown_stage_is_refused_rather_than_stored(store):
    """This value is rendered on the dashboard."""
    record = _pending(store)
    handle, _expires = store.create_handle(record.id, ttl_seconds=900)
    with pytest.raises(ValueError):
        store.record_progress_by_handle(handle, stage="<script>alert(1)</script>")


def test_elapsed_seconds_is_clamped_like_the_code_path(store):
    record = _pending(store)
    handle, _expires = store.create_handle(record.id, ttl_seconds=900)

    store.record_progress_by_handle(handle, stage="redeeming", elapsed_seconds=10**9)

    assert store.get(record.id).progress_elapsed_seconds == 86_400


# ---------------------------------------------------------------------------
# 4. the route: one contract, two authenticators, no new public path
# ---------------------------------------------------------------------------

from starlette.testclient import TestClient                          # noqa: E402

from terminal_mcp.config import (                                    # noqa: E402
    AppConfig,
    DashboardConfig,
    InputPolicyConfig,
    NodesConfig,
    PermissionsConfig,
    SessionLifecycleConfig,
)
from terminal_mcp.core import TerminalService                        # noqa: E402
from terminal_mcp.dashboard import register_dashboard                # noqa: E402
from terminal_mcp.mcp_app import build_mcp                           # noqa: E402

PROGRESS = "/dashboard/api/enroll/progress"


@pytest.fixture
def client(tmp_path):
    config = AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("test-*",),
        input_policy=InputPolicyConfig(allowed_session_patterns=("test-*",)),
        dashboard=DashboardConfig(),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),)),
        nodes=NodesConfig(onboarding=OnboardingConfig()),
    )
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    return TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})


def _make_enrollment(client) -> str:
    response = client.post("/dashboard/api/nodes/onboard/enrollments",
                           json={"node_id": "win-work", "os": "windows",
                                 "profile": "minimal", "connectivity": {}})
    assert response.status_code == 200, response.text
    return response.json()["enrollment"]["id"]


def _mint_handle(client, enrollment_id: str) -> str:
    response = client.post(
        f"/dashboard/api/nodes/onboard/enrollments/{enrollment_id}/handle", json={})
    assert response.status_code == 200, response.text
    return response.json()["handle"]


def test_the_route_accepts_a_handle_and_shows_the_stage(client):
    enrollment_id = _make_enrollment(client)
    handle = _mint_handle(client, enrollment_id)

    posted = client.post(PROGRESS, json={"handle": handle, "stage": "helper_started"})

    assert posted.status_code == 202
    rows = client.get("/dashboard/api/nodes/onboard/enrollments").json()["enrollments"]
    row = next(r for r in rows if r["id"] == enrollment_id)
    assert row["progress_stage"] == "helper_started"
    assert row["progress_label"] == "Helper đã khởi động"


def test_the_route_still_accepts_a_code_exactly_as_before(client):
    """The installer's path is untouched: this is one contract with two
    authenticators, not a replacement."""
    response = client.post("/dashboard/api/nodes/onboard/enrollments",
                           json={"node_id": "win-two", "os": "windows",
                                 "profile": "minimal", "connectivity": {}})
    code = response.json()["code"]

    posted = client.post(PROGRESS, json={"code": code, "stage": "installing_openssh"})

    assert posted.status_code == 202


def test_a_bad_handle_is_answered_identically_to_a_good_one(client):
    """202 either way. A distinct status would turn this into an oracle for
    'is this pairing real', which is the question a guesser wants."""
    _make_enrollment(client)
    good = client.post(PROGRESS, json={"handle": "f" * 32, "stage": "helper_started"})
    bad = client.post(PROGRESS, json={"handle": "nope", "stage": "helper_started"})
    assert good.status_code == bad.status_code == 202


def test_an_unknown_stage_is_still_refused_at_the_route(client):
    enrollment_id = _make_enrollment(client)
    handle = _mint_handle(client, enrollment_id)
    refused = client.post(PROGRESS, json={"handle": handle, "stage": "rm -rf /"})
    assert refused.status_code == 400


def test_no_response_and_no_log_echoes_the_handle(client, caplog):
    """A handle is a credential for its window. It authenticates the call
    and must not appear in the answer or in anything written down."""
    import logging
    enrollment_id = _make_enrollment(client)
    handle = _mint_handle(client, enrollment_id)

    with caplog.at_level(logging.INFO):
        posted = client.post(PROGRESS, json={"handle": handle, "stage": "redeeming"})

    assert handle not in posted.text
    for name, value in posted.headers.items():
        assert handle not in value, name
    assert handle not in caplog.text
    # node_id is the identifier that may be logged, and is.
    assert "win-work" in caplog.text


def test_the_progress_route_is_the_only_path_the_helper_needs(client):
    """Deliberate: reusing this contract means the public bootstrap ingress
    allowlist does not grow a new entry for helper telemetry."""
    from pathlib import Path
    template = (Path(__file__).resolve().parent.parent / "deploy" / "cloudflare"
                / "bootstrap-ingress.template.yml").read_text(encoding="utf-8")
    assert "enroll/(consume|redeem|progress)" in template
    assert "bootstrap-status" not in template
    assert "helper/progress" not in template
