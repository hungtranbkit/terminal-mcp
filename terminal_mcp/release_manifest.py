"""Signed release manifests for Terminal MCP production installers."""
from __future__ import annotations

import base64
import hashlib
import json
import platform as _platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

SCHEMA_VERSION = 1
CHANNELS = frozenset({"stable", "beta"})
PLATFORMS = frozenset({"linux", "windows", "macos"})


class ManifestError(ValueError):
    """A release manifest is malformed, incompatible, or untrusted."""


@dataclass(frozen=True)
class Artifact:
    platform: str
    arch: str
    url: str
    sha256: str
    size: int
    filename: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Artifact":
        platform = str(value.get("platform", "")).strip().lower()
        arch = str(value.get("arch", "")).strip().lower()
        url = str(value.get("url", "")).strip()
        sha256 = str(value.get("sha256", "")).strip().lower()
        filename = str(value.get("filename", "")).strip()
        try:
            size = int(value.get("size", -1))
        except (TypeError, ValueError) as exc:
            raise ManifestError("artifact size must be an integer") from exc
        if platform not in PLATFORMS:
            raise ManifestError(f"unsupported artifact platform {platform!r}")
        if not arch or any(ch.isspace() for ch in arch):
            raise ManifestError("artifact arch is required and must not contain whitespace")
        if not url.startswith(("https://", "file://")):
            raise ManifestError("artifact URL must use https:// or file://")
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise ManifestError("artifact sha256 must be 64 lowercase hex characters")
        if size < 0:
            raise ManifestError("artifact size must be >= 0")
        if not filename or "/" in filename or "\\" in filename:
            raise ManifestError("artifact filename must be a basename")
        return cls(platform=platform, arch=arch, url=url, sha256=sha256,
                   size=size, filename=filename)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform, "arch": self.arch, "url": self.url,
            "sha256": self.sha256, "size": self.size, "filename": self.filename,
        }


@dataclass(frozen=True)
class ReleaseManifest:
    version: str
    channel: str
    protocol_min: int
    protocol_max: int
    created_at: str
    artifacts: tuple[Artifact, ...]
    previous_version: str | None = None
    rollback_compatible: bool = True
    schema_version: int = SCHEMA_VERSION
    signature: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReleaseManifest":
        try:
            schema_version = int(value.get("schema_version", 0))
            protocol_min = int(value["protocol_min"])
            protocol_max = int(value["protocol_max"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError("manifest protocol/schema fields are invalid") from exc
        if schema_version != SCHEMA_VERSION:
            raise ManifestError(f"unsupported manifest schema {schema_version}; expected {SCHEMA_VERSION}")
        version = str(value.get("version", "")).strip()
        channel = str(value.get("channel", "")).strip().lower()
        created_at = str(value.get("created_at", "")).strip()
        if not version or any(ch.isspace() for ch in version):
            raise ManifestError("version is required and must not contain whitespace")
        if channel not in CHANNELS:
            raise ManifestError(f"channel must be one of {sorted(CHANNELS)}")
        if protocol_min < 1 or protocol_max < protocol_min:
            raise ManifestError("protocol range is invalid")
        if not created_at:
            raise ManifestError("created_at is required")
        raw_artifacts = value.get("artifacts")
        if not isinstance(raw_artifacts, Sequence) or isinstance(raw_artifacts, (str, bytes)):
            raise ManifestError("artifacts must be a list")
        artifacts = tuple(Artifact.from_mapping(item) for item in raw_artifacts)
        if not artifacts:
            raise ManifestError("at least one artifact is required")
        keys = {(item.platform, normalize_arch(item.arch)) for item in artifacts}
        if len(keys) != len(artifacts):
            raise ManifestError("duplicate platform/arch artifact")
        previous = value.get("previous_version")
        previous_version = None if previous in (None, "") else str(previous).strip()
        signature = value.get("signature")
        signature = None if signature in (None, "") else str(signature).strip()
        return cls(
            version=version, channel=channel, protocol_min=protocol_min,
            protocol_max=protocol_max, created_at=created_at, artifacts=artifacts,
            previous_version=previous_version,
            rollback_compatible=bool(value.get("rollback_compatible", True)),
            schema_version=schema_version, signature=signature,
        )

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "version": self.version,
            "channel": self.channel, "protocol_min": self.protocol_min,
            "protocol_max": self.protocol_max, "created_at": self.created_at,
            "previous_version": self.previous_version,
            "rollback_compatible": self.rollback_compatible,
            "artifacts": [item.to_dict() for item in self.artifacts],
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.unsigned_dict()
        if self.signature:
            value["signature"] = self.signature
        return value

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.unsigned_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    def choose_artifact(self, *, platform: str | None = None, arch: str | None = None) -> Artifact:
        platform = normalize_platform(platform or _platform.system())
        arch = normalize_arch(arch or _platform.machine())
        matches = [item for item in self.artifacts
                   if item.platform == platform and normalize_arch(item.arch) == arch]
        if len(matches) != 1:
            raise ManifestError(f"manifest has no unique artifact for platform={platform!r} arch={arch!r}")
        return matches[0]

    def supports_protocol(self, protocol_version: int) -> bool:
        return self.protocol_min <= int(protocol_version) <= self.protocol_max


def normalize_platform(value: str) -> str:
    lowered = str(value or "").strip().lower()
    aliases = {"linux": "linux", "windows": "windows", "win32": "windows",
               "darwin": "macos", "macos": "macos"}
    try:
        return aliases[lowered]
    except KeyError as exc:
        raise ManifestError(f"unsupported platform {value!r}") from exc


def normalize_arch(value: str) -> str:
    lowered = str(value or "").strip().lower().replace("-", "_")
    aliases = {"x86_64": "x86_64", "amd64": "x86_64",
               "aarch64": "arm64", "arm64": "arm64"}
    return aliases.get(lowered, lowered)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(path: str | Path, artifact: Artifact) -> None:
    path = Path(path)
    if not path.is_file():
        raise ManifestError(f"artifact not found: {path}")
    stat = path.stat()
    if stat.st_size != artifact.size:
        raise ManifestError(f"artifact size mismatch: expected {artifact.size}, got {stat.st_size}")
    actual = sha256_file(path)
    if actual != artifact.sha256:
        raise ManifestError("artifact sha256 mismatch")


def sign_manifest(manifest: ReleaseManifest, private_key_pem: bytes) -> ReleaseManifest:
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ManifestError("release signing key must be Ed25519")
    signature = base64.b64encode(key.sign(manifest.canonical_bytes())).decode("ascii")
    value = manifest.to_dict()
    value["signature"] = signature
    return ReleaseManifest.from_mapping(value)


def verify_manifest(manifest: ReleaseManifest, public_key_pem: bytes, *, require_signature: bool = True) -> None:
    if not manifest.signature:
        if require_signature:
            raise ManifestError("release manifest is unsigned")
        return
    key = serialization.load_pem_public_key(public_key_pem)
    if not isinstance(key, Ed25519PublicKey):
        raise ManifestError("release verification key must be Ed25519")
    try:
        signature = base64.b64decode(manifest.signature, validate=True)
    except Exception as exc:
        raise ManifestError("release manifest signature is not valid base64") from exc
    try:
        key.verify(signature, manifest.canonical_bytes())
    except InvalidSignature as exc:
        raise ManifestError("release manifest signature verification failed") from exc


def load_manifest(path: str | Path) -> ReleaseManifest:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read release manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise ManifestError("release manifest root must be an object")
    return ReleaseManifest.from_mapping(value)
