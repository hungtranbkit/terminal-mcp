"""The Bootstrap helper binary, and the manifest that vouches for it.

WHY A MANIFEST AND NOT JUST A FILE PATH

The Dashboard is about to hand an operator an executable and ask them to run
it elevated on a machine they own. The weakest version of that is a route that
streams whatever happens to be at a path. Then a half-finished copy, a
truncated upload, or a stale build from three commits ago is served with the
same confidence as the real thing, and the person running it has no way to
tell.

So an artifact is only servable when a manifest says what it should be --
version, build SHA, size, SHA256, signed -- and the bytes on disk still match.
A mismatch is refused, not served with a warning: the whole reason to check is
that the operator cannot check for themselves.

WHERE ARTIFACTS LIVE

Under the state directory, versioned:

    <root>/<version>/terminal-mcp-bootstrap-<arch>.exe
    <root>/<version>/manifest.json

Never a temp directory. /tmp on this host is a tmpfs with a per-user quota
that has already been filled once by an unrelated test run, taking every
shell on the box down with it; an artifact an operator is about to install
must not live somewhere that can evaporate between the page loading and the
click.

Versioned because two builds will exist at once the moment a signed one
appears beside the dev one, and "the file" stops being a single thing.

SIGNED IS REPORTED, NEVER ASSUMED

`signed` comes from the manifest, which is written by the build. An unsigned
dev build says so, and the UI says so in plain words rather than coaching
anyone past a SmartScreen warning. Nothing here infers a signature from the
absence of evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Error codes, stable: they reach the route, the page and the tests.
NO_MANIFEST = "HELPER_ARTIFACT_NOT_PUBLISHED"
UNKNOWN_TARGET = "HELPER_ARTIFACT_UNKNOWN_TARGET"
MISSING_FILE = "HELPER_ARTIFACT_MISSING"
HASH_MISMATCH = "HELPER_ARTIFACT_HASH_MISMATCH"
SIZE_MISMATCH = "HELPER_ARTIFACT_SIZE_MISMATCH"
BAD_MANIFEST = "HELPER_ARTIFACT_MANIFEST_INVALID"

# The only targets this route serves. An allowlist rather than a path
# parameter: a target name reaches the filesystem, so it may never be
# caller-shaped.
TARGETS: dict[str, str] = {
    "windows-x64": "terminal-mcp-bootstrap.exe",
    "windows-arm64": "terminal-mcp-bootstrap-arm64.exe",
}

MANIFEST_NAME = "manifest.json"

# Read in bounded chunks: the artifact is ~7 MiB today, but a hash check that
# loads the whole file is a habit that breaks on the first big one.
_CHUNK = 1024 * 1024


class ArtifactError(RuntimeError):
    """Refused, with a reason the caller can turn into a status code."""

    def __init__(self, code: str, detail: str, **extra: Any) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.extra = extra

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "detail": self.detail, **self.extra}


@dataclass(frozen=True)
class Artifact:
    """One servable binary, already verified against its manifest."""

    path: Path
    target: str
    filename: str
    version: str
    build_sha: str
    sha256: str
    size: int
    signed: bool

    def as_dict(self) -> dict[str, Any]:
        return {"target": self.target, "filename": self.filename,
                "version": self.version, "build_sha": self.build_sha,
                "sha256": self.sha256, "size": self.size, "signed": self.signed}


def artifact_root() -> Path:
    """Where published helper builds live. Overridable for tests and for a
    host that keeps state elsewhere; never a temp directory by default."""
    override = os.environ.get("TERMINAL_MCP_HELPER_ARTIFACT_DIR")
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return base / "terminal-mcp" / "helper"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _published_versions(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted((child for child in root.iterdir()
                   if child.is_dir() and (child / MANIFEST_NAME).is_file()),
                  key=lambda p: p.name)


def latest_manifest(root: Path | None = None) -> tuple[Path, dict[str, Any]]:
    """The newest published manifest, or a refusal naming what is absent."""
    root = root or artifact_root()
    versions = _published_versions(root)
    if not versions:
        raise ArtifactError(NO_MANIFEST,
                            "no helper build has been published on this controller",
                            root=str(root))
    directory = versions[-1]
    try:
        manifest = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ArtifactError(BAD_MANIFEST, f"{directory / MANIFEST_NAME}: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("artifacts"), dict):
        raise ArtifactError(BAD_MANIFEST,
                            "manifest has no 'artifacts' map", path=str(directory))
    return directory, manifest


def resolve(target: str, *, root: Path | None = None, verify: bool = True) -> Artifact:
    """The artifact for `target`, verified against the manifest.

    `verify=False` exists only for the listing endpoint, which reports what
    is published without paying for a hash of every file on every page load.
    The DOWNLOAD path always verifies -- that is the point of it.
    """
    if target not in TARGETS:
        raise ArtifactError(UNKNOWN_TARGET, f"{target!r} is not a published target",
                            known=sorted(TARGETS))
    directory, manifest = latest_manifest(root)
    entry = manifest["artifacts"].get(target)
    if not isinstance(entry, dict):
        raise ArtifactError(UNKNOWN_TARGET,
                            f"the published build has no {target!r} artifact",
                            version=manifest.get("version"),
                            known=sorted(manifest["artifacts"]))

    filename = TARGETS[target]
    path = directory / filename
    if not path.is_file():
        raise ArtifactError(MISSING_FILE,
                            f"manifest lists {target} but {filename} is not on disk",
                            version=manifest.get("version"), path=str(path))

    declared_size = int(entry.get("size") or 0)
    actual_size = path.stat().st_size
    if verify and declared_size and actual_size != declared_size:
        # Checked before the hash: a truncated file is the common case and
        # this names it in one stat instead of a full read.
        raise ArtifactError(SIZE_MISMATCH,
                            f"{filename} is {actual_size} bytes, manifest says {declared_size}",
                            version=manifest.get("version"))

    declared_hash = str(entry.get("sha256") or "").lower()
    if verify:
        if not declared_hash:
            raise ArtifactError(BAD_MANIFEST, f"{target} entry has no sha256")
        actual_hash = sha256_of(path)
        if actual_hash != declared_hash:
            # Refused, never served with a warning: the reason to check is
            # that the operator downloading it cannot.
            raise ArtifactError(HASH_MISMATCH,
                                f"{filename} does not match the manifest",
                                version=manifest.get("version"))

    return Artifact(
        path=path, target=target, filename=filename,
        version=str(manifest.get("version") or entry.get("version") or "unknown"),
        build_sha=str(manifest.get("build_sha") or entry.get("build_sha") or "unknown"),
        sha256=declared_hash, size=actual_size,
        # Absent means unsigned. A missing field can never read as signed.
        signed=bool(entry.get("signed", manifest.get("signed", False))),
    )


def available(root: Path | None = None) -> dict[str, Any]:
    """What this controller can hand out, for the Dashboard to decide with.

    Never raises for "nothing published" -- an empty list is the honest
    answer, and the CTA uses it to keep pointing at the manual fallback.
    """
    try:
        _directory, manifest = latest_manifest(root)
    except ArtifactError as exc:
        return {"published": False, "reason": exc.code, "detail": exc.detail,
                "targets": []}
    targets = []
    for target in sorted(manifest.get("artifacts", {})):
        try:
            targets.append(resolve(target, root=root, verify=False).as_dict())
        except ArtifactError:
            continue
    return {"published": bool(targets), "version": manifest.get("version"),
            "build_sha": manifest.get("build_sha"), "targets": targets,
            "signed": bool(manifest.get("signed", False))}


def publish(source: Path, *, target: str, version: str, build_sha: str,
            signed: bool = False, root: Path | None = None) -> Artifact:
    """Copy a built binary into the versioned store and write its manifest.

    The hash is computed from the bytes that land in the store, not from the
    build's own report -- a manifest that quotes the builder rather than the
    file would vouch for something nobody re-read.
    """
    import shutil

    if target not in TARGETS:
        raise ArtifactError(UNKNOWN_TARGET, f"{target!r} is not a publishable target",
                            known=sorted(TARGETS))
    source = Path(source)
    if not source.is_file():
        raise ArtifactError(MISSING_FILE, f"{source} does not exist")

    root = root or artifact_root()
    directory = root / version
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / TARGETS[target]
    shutil.copy2(source, destination)

    manifest_path = directory / MANIFEST_NAME
    manifest: dict[str, Any] = {"version": version, "build_sha": build_sha,
                                "signed": bool(signed), "artifacts": {}}
    if manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("artifacts"), dict):
                manifest["artifacts"] = existing["artifacts"]
        except (OSError, ValueError):
            manifest["artifacts"] = {}

    manifest["artifacts"][target] = {
        "sha256": sha256_of(destination), "size": destination.stat().st_size,
        "signed": bool(signed), "version": version, "build_sha": build_sha,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    return resolve(target, root=root)
