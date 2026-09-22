from __future__ import annotations

import hashlib

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from terminal_mcp.release_manifest import ManifestError, ReleaseManifest, normalize_arch, sign_manifest, verify_artifact, verify_manifest


def _artifact(tmp_path, *, platform="linux", arch="x86_64"):
    payload = tmp_path / f"terminal-mcp-{platform}-{arch}.whl"
    payload.write_bytes(b"terminal-mcp-production-artifact")
    return payload, {
        "platform": platform,
        "arch": arch,
        "url": f"file://{payload}",
        "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
        "size": payload.stat().st_size,
        "filename": payload.name,
    }


def _manifest(tmp_path):
    payload, artifact = _artifact(tmp_path)
    manifest = ReleaseManifest.from_mapping({
        "schema_version": 1,
        "version": "0.13.1",
        "channel": "beta",
        "protocol_min": 1,
        "protocol_max": 1,
        "created_at": "2026-09-21T00:00:00Z",
        "previous_version": "0.13.0",
        "rollback_compatible": True,
        "artifacts": [artifact],
    })
    return payload, manifest


def _keys():
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = public.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def test_signed_manifest_round_trip_and_artifact_verification(tmp_path):
    payload, manifest = _manifest(tmp_path)
    private_pem, public_pem = _keys()
    signed = sign_manifest(manifest, private_pem)
    verify_manifest(signed, public_pem)
    artifact = signed.choose_artifact(platform="Linux", arch="amd64")
    verify_artifact(payload, artifact)
    assert artifact.platform == "linux"
    assert normalize_arch("AMD64") == "x86_64"


def test_signature_covers_compatibility_and_checksum(tmp_path):
    _payload, manifest = _manifest(tmp_path)
    private_pem, public_pem = _keys()
    signed = sign_manifest(manifest, private_pem)
    tampered = signed.to_dict()
    tampered["protocol_max"] = 2
    with pytest.raises(ManifestError, match="signature verification failed"):
        verify_manifest(ReleaseManifest.from_mapping(tampered), public_pem)


def test_unsigned_release_is_rejected_by_default(tmp_path):
    _payload, manifest = _manifest(tmp_path)
    _, public_pem = _keys()
    with pytest.raises(ManifestError, match="unsigned"):
        verify_manifest(manifest, public_pem)
    verify_manifest(manifest, public_pem, require_signature=False)


def test_artifact_checksum_and_size_are_load_bearing(tmp_path):
    payload, manifest = _manifest(tmp_path)
    artifact = manifest.artifacts[0]
    payload.write_bytes(b"tampered")
    with pytest.raises(ManifestError, match="size mismatch|sha256 mismatch"):
        verify_artifact(payload, artifact)


def test_manifest_rejects_duplicate_platform_arch(tmp_path):
    _payload, artifact = _artifact(tmp_path)
    value = {
        "schema_version": 1,
        "version": "0.13.1",
        "channel": "stable",
        "protocol_min": 1,
        "protocol_max": 1,
        "created_at": "2026-09-21T00:00:00Z",
        "artifacts": [artifact, dict(artifact)],
    }
    with pytest.raises(ManifestError, match="duplicate"):
        ReleaseManifest.from_mapping(value)


def test_manifest_protocol_range_and_platform_selection(tmp_path):
    _payload, manifest = _manifest(tmp_path)
    assert manifest.supports_protocol(1)
    assert not manifest.supports_protocol(2)
    with pytest.raises(ManifestError, match="no unique artifact"):
        manifest.choose_artifact(platform="windows", arch="amd64")
