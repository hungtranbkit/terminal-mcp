"""Handing an operator an executable is a promise, so it gets checked.

The Dashboard is about to ask someone to run this binary elevated on a machine
they own. The weak version of that route streams whatever is at a path -- and
then a truncated upload, a half-written replacement, or a stale build from
three commits ago is served with exactly the same confidence as the real
thing, to a person who has no way to tell the difference.

So the assertions here are mostly about REFUSING: mismatched hash, wrong size,
missing file, nothing published, a target name that is not on the allowlist.
Serving the right bytes is the easy half.
"""
from __future__ import annotations

import json

import pytest

from terminal_mcp import helper_artifact as ha

BODY = b"MZ" + b"\x00" * 4094          # PE magic, so it looks like what it is
OTHER = b"MZ" + b"\xff" * 4094


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_HELPER_ARTIFACT_DIR", str(tmp_path / "helper"))
    return tmp_path / "helper"


def _publish(store, *, version="0.1.0-dev", body=BODY, target="windows-x64",
             signed=False, build_sha="abc1234"):
    source = store.parent / f"src-{version}-{target}.exe"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(body)
    return ha.publish(source, target=target, version=version,
                      build_sha=build_sha, signed=signed, root=store)


# -- publishing and resolving ---------------------------------------------------------

def test_a_published_artifact_resolves_with_its_metadata(store):
    published = _publish(store)

    resolved = ha.resolve("windows-x64", root=store)

    assert resolved.version == "0.1.0-dev"
    assert resolved.build_sha == "abc1234"
    assert resolved.size == len(BODY)
    assert resolved.sha256 == published.sha256
    assert resolved.signed is False
    assert resolved.filename == "terminal-mcp-bootstrap.exe"


def test_the_manifest_hash_is_computed_from_the_stored_bytes(store):
    """Not quoted from the builder: a manifest that repeats what the build
    said would vouch for something nobody re-read."""
    published = _publish(store)
    assert published.sha256 == ha.sha256_of(published.path)


def test_nothing_published_is_refused_with_a_reason(store):
    with pytest.raises(ha.ArtifactError) as caught:
        ha.resolve("windows-x64", root=store)
    assert caught.value.code == ha.NO_MANIFEST


def test_an_unknown_target_is_refused_before_touching_the_filesystem(store):
    """A target name reaches a path, so it may never be caller-shaped."""
    with pytest.raises(ha.ArtifactError) as caught:
        ha.resolve("../../etc/passwd", root=store)
    assert caught.value.code == ha.UNKNOWN_TARGET


def test_an_arch_the_build_did_not_produce_is_refused(store):
    _publish(store, target="windows-x64")
    with pytest.raises(ha.ArtifactError) as caught:
        ha.resolve("windows-arm64", root=store)
    assert caught.value.code == ha.UNKNOWN_TARGET


# -- the refusals that matter -----------------------------------------------------------

def test_a_tampered_artifact_is_refused_not_served_with_a_warning(store):
    published = _publish(store)
    published.path.write_bytes(OTHER)      # same length, different bytes

    with pytest.raises(ha.ArtifactError) as caught:
        ha.resolve("windows-x64", root=store)

    assert caught.value.code == ha.HASH_MISMATCH


def test_a_truncated_artifact_is_refused_on_size_before_hashing(store):
    published = _publish(store)
    published.path.write_bytes(BODY[:100])

    with pytest.raises(ha.ArtifactError) as caught:
        ha.resolve("windows-x64", root=store)

    assert caught.value.code == ha.SIZE_MISMATCH


def test_a_manifest_entry_with_no_hash_is_refused(store):
    published = _publish(store)
    manifest_path = published.path.parent / ha.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    del manifest["artifacts"]["windows-x64"]["sha256"]
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ha.ArtifactError) as caught:
        ha.resolve("windows-x64", root=store)
    assert caught.value.code == ha.BAD_MANIFEST


def test_a_manifest_listing_a_file_that_is_gone_is_refused(store):
    published = _publish(store)
    published.path.unlink()

    with pytest.raises(ha.ArtifactError) as caught:
        ha.resolve("windows-x64", root=store)
    assert caught.value.code == ha.MISSING_FILE


def test_a_corrupt_manifest_is_refused(store):
    published = _publish(store)
    (published.path.parent / ha.MANIFEST_NAME).write_text("{not json")

    with pytest.raises(ha.ArtifactError) as caught:
        ha.resolve("windows-x64", root=store)
    assert caught.value.code == ha.BAD_MANIFEST


# -- signed is reported, never assumed ---------------------------------------------------

def test_an_unsigned_build_reports_unsigned(store):
    assert _publish(store, signed=False).signed is False


def test_a_missing_signed_field_reads_as_unsigned(store):
    """Absent evidence must never read as a signature."""
    published = _publish(store)
    manifest_path = published.path.parent / ha.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    del manifest["artifacts"]["windows-x64"]["signed"]
    del manifest["signed"]
    manifest_path.write_text(json.dumps(manifest))

    assert ha.resolve("windows-x64", root=store).signed is False


# -- versioning ---------------------------------------------------------------------------

def test_the_newest_published_version_is_the_one_served(store):
    _publish(store, version="0.1.0-dev", body=BODY)
    _publish(store, version="0.2.0-dev", body=OTHER)

    resolved = ha.resolve("windows-x64", root=store)
    assert resolved.version == "0.2.0-dev"


def test_artifacts_live_in_the_controlled_state_directory(tmp_path, monkeypatch):
    """Never a scratch path of its own choosing.

    /tmp on this host is a quota'd tmpfs that has already been filled once,
    taking every shell on the box with it. The artifact store therefore
    follows the SAME state directory as every other durable thing here, so
    wherever the operator has pointed that, the binary lives beside it --
    rather than somewhere that can evaporate mid-click.
    """
    monkeypatch.delenv("TERMINAL_MCP_HELPER_ARTIFACT_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    root = ha.artifact_root()

    assert root == tmp_path / "state" / "terminal-mcp" / "helper"


def test_the_artifact_directory_can_be_pointed_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_HELPER_ARTIFACT_DIR", str(tmp_path / "custom"))
    assert ha.artifact_root() == tmp_path / "custom"


# -- what the CTA reads ---------------------------------------------------------------------

def test_available_reports_nothing_published_without_raising(store):
    result = ha.available(root=store)
    assert result["published"] is False
    assert result["targets"] == []
    assert result["reason"] == ha.NO_MANIFEST


def test_available_lists_what_can_be_handed_out(store):
    _publish(store, target="windows-x64")
    _publish(store, target="windows-arm64", body=OTHER)

    result = ha.available(root=store)

    assert result["published"] is True
    assert {t["target"] for t in result["targets"]} == {"windows-x64", "windows-arm64"}
    assert result["signed"] is False


# -- the route ---------------------------------------------------------------------------

from starlette.testclient import TestClient                       # noqa: E402

from terminal_mcp.config import (AppConfig, DashboardConfig, InputPolicyConfig,  # noqa: E402
                                 PermissionsConfig, SessionAccessConfig)
from terminal_mcp.core import TerminalService                     # noqa: E402
from terminal_mcp.dashboard import register_dashboard             # noqa: E402
from terminal_mcp.mcp_app import build_mcp                        # noqa: E402

ROUTE = "/dashboard/api/nodes/onboard/helper"
DOWNLOAD = ROUTE + "/windows-x64"


def _plain_config():
    return AppConfig(PermissionsConfig(True, True), ("test-*",), 50, 20,
                     InputPolicyConfig(allowed_session_patterns=("test-*",)))


def _guarded_config():
    return AppConfig(
        PermissionsConfig(True, True), ("test-*",), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
        dashboard=DashboardConfig(
            cloudflare_access_team_domain="test-team.cloudflareaccess.com",
            cloudflare_access_audience="test-aud"),
        session_access=SessionAccessConfig(default_read=True, default_input=True))


def _client(config):
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    return TestClient(server.streamable_http_app(),
                      headers={"Origin": "http://testserver"})


@pytest.fixture
def client():
    return _client(_plain_config())


def test_the_download_sits_behind_the_same_access_guard_as_every_other_read():
    """Not a public path. When Cloudflare Access is configured, an
    unauthenticated request for the binary is refused like any other read."""
    guarded = _client(_guarded_config())

    response = guarded.get(DOWNLOAD)

    assert response.status_code == 403
    assert response.json()["error"] == "CLOUDFLARE_ACCESS_VERIFICATION_FAILED"


def test_the_manifest_endpoint_is_guarded_too():
    guarded = _client(_guarded_config())
    assert guarded.get(ROUTE).status_code == 403


def test_nothing_published_is_a_404_not_a_stack_trace(client, store):
    response = client.get(DOWNLOAD)
    assert response.status_code == 404
    assert response.json()["error"] == ha.NO_MANIFEST


def test_a_published_artifact_downloads_with_the_right_headers(client, store):
    published = _publish(store)

    response = client.get(DOWNLOAD)

    assert response.status_code == 200
    assert response.content == BODY
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == \
        'attachment; filename="terminal-mcp-bootstrap.exe"'
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-artifact-sha256"] == published.sha256
    assert response.headers["x-artifact-version"] == "0.1.0-dev"
    assert response.headers["x-artifact-signed"] == "false"


def test_a_tampered_artifact_is_never_streamed(client, store):
    """The refusal that matters: the operator cannot check the bytes, so the
    controller does it on every request rather than trusting publish time."""
    published = _publish(store)
    published.path.write_bytes(OTHER)

    response = client.get(DOWNLOAD)

    assert response.status_code == 500
    assert response.json()["error"] == ha.HASH_MISMATCH
    assert OTHER not in response.content


def test_a_missing_file_is_refused(client, store):
    _publish(store).path.unlink()
    response = client.get(DOWNLOAD)
    assert response.status_code == 404
    assert response.json()["error"] == ha.MISSING_FILE


def test_an_unknown_target_is_refused(client, store):
    _publish(store)
    response = client.get(ROUTE + "/linux-x64")
    assert response.status_code == 404
    assert response.json()["error"] == ha.UNKNOWN_TARGET


def test_no_credential_ever_rides_along_with_the_download(client, store):
    """No enrollment code, no handle, no node token -- not in the URL, not in
    the body, not in a header. The helper earns its credential later, by
    redeeming a handle itself."""
    published = _publish(store)

    response = client.get(DOWNLOAD)

    assert "?" not in DOWNLOAD
    joined = " ".join(f"{k}: {v}" for k, v in response.headers.items()).lower()
    for forbidden in ("token", "handle", "enroll", "secret", "authorization"):
        assert forbidden not in joined, forbidden
    assert response.content == published.path.read_bytes()


def test_the_manifest_endpoint_reports_what_is_published(client, store):
    _publish(store)

    payload = client.get(ROUTE).json()

    assert payload["published"] is True
    assert payload["version"] == "0.1.0-dev"
    assert payload["signed"] is False
    assert [t["target"] for t in payload["targets"]] == ["windows-x64"]


def test_the_manifest_endpoint_says_so_when_nothing_is_published(client, store):
    payload = client.get(ROUTE).json()
    assert payload["published"] is False
    assert payload["targets"] == []


# -- the CTA, and the fallback that must survive it -----------------------------------

from terminal_mcp.dashboard import NODES_ADMIN_HTML               # noqa: E402


def test_the_cta_points_at_this_route_when_the_helper_is_absent():
    assert 'id="anHelperDlBtn"' in NODES_ADMIN_HTML
    assert '/dashboard/api/nodes/onboard/helper/windows-x64' in NODES_ADMIN_HTML
    assert 'download="terminal-mcp-bootstrap.exe"' in NODES_ADMIN_HTML


def test_the_cta_only_offers_a_download_this_controller_actually_published():
    """Otherwise the button 404s in front of the operator, and the honest
    state -- copy/paste is the only path here -- is hidden behind a dead
    button."""
    assert "published.data.published" in NODES_ADMIN_HTML
    assert "if (!build) { helperBox.hidden = true; return; }" in NODES_ADMIN_HTML


def test_an_unsigned_build_is_stated_plainly_not_coached_past():
    assert "chưa ký" in NODES_ADMIN_HTML
    assert "SmartScreen" in NODES_ADMIN_HTML
    assert "build.signed" in NODES_ADMIN_HTML


def test_the_manual_powershell_fallback_is_untouched():
    """The one path that works today. A download button must never replace
    it -- it is what an operator falls back to when the helper is unsigned,
    blocked, or simply refused by their machine."""
    for marker in ("anCopyCmdBtn", "quick_install_command",
                   "Run with PowerShell", "anCopyCodeBtn"):
        assert marker in NODES_ADMIN_HTML, marker


def test_the_one_click_connect_button_still_exists_for_an_installed_helper():
    assert 'id="anHelperBtn"' in NODES_ADMIN_HTML
    assert "terminalmcp" in NODES_ADMIN_HTML or "issued.data.url" in NODES_ADMIN_HTML
