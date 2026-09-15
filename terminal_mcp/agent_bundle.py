"""The Windows node-agent bundle: what a freshly onboarded node installs to
become able to host sessions.

WHY THIS EXISTS. deploy/install-node-agent.ps1 has always been able to
install the agent, but it requires a terminal-mcp CHECKOUT on the node
(`pip install -e "$RepoDir[windows]"`). A machine that arrived through Add
Node has no checkout and no way to get one: the repository is private, the
Dashboard hostname is Access-gated and a node mid-onboarding cannot obtain
an Access session. So onboarding could bring a node to "registered and
heartbeating" and no further -- port 8790 closed, no transport, Create
Session offering a node it could never reach.

This module closes that gap WITHOUT inventing a second delivery protocol:
the controller publishes the same source tree install-node-agent.ps1
already consumes, as one pinned zip, and serves it over an exact
machine-facing route authenticated by the node's own bearer token. The
script then runs unchanged against the extracted directory.

WHAT IS IN THE BUNDLE, and why each piece:

    pyproject.toml              install-node-agent.ps1 refuses to run
                                without it, and pip needs it to resolve
                                dependencies
    terminal_mcp/               the package itself, including
                                windows_agent.py -- the entry point
    config.example.yaml         the script copies it to config.yaml when
                                the node has none
    deploy/install-node-agent.ps1   so the node installs with the SAME
                                script this release was tested against,
                                not whatever an older setup left behind

WHAT IS DELIBERATELY NOT IN IT: tests, .git, helper/ (the Go source),
docs, and every dotfile. A node needs to run the agent, not rebuild the
project, and a smaller bundle is a smaller thing to verify.
"""
from __future__ import annotations

import hashlib
import json
import os
import zipfile
from pathlib import Path
from typing import Any

_CHUNK = 1024 * 1024

MANIFEST_NAME = "manifest.json"
BUNDLE_NAME = "terminal-mcp-node-agent.zip"

# Everything the install script touches, and nothing else. Paths are
# relative to the repository root and are copied verbatim.
BUNDLE_FILES = ("pyproject.toml", "config.example.yaml", "deploy/install-node-agent.ps1",
                 "deploy/node-agent-config.yaml")
BUNDLE_PACKAGE = "terminal_mcp"

# Excluded from the package copy: bytecode and caches are rebuilt on the
# node, and shipping them makes the hash depend on whoever ran the build.
_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_SKIP_SUFFIXES = (".pyc", ".pyo", ".pyd", ".so")


class BundleError(RuntimeError):
    """Raised for a missing, unreadable or unpublished bundle."""

    def __init__(self, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.extra = extra


def bundle_root() -> Path:
    """Where published node-agent bundles live. Same discipline as the
    helper artifact store: overridable for tests, never a temp directory by
    default."""
    override = os.environ.get("TERMINAL_MCP_AGENT_BUNDLE_DIR")
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return base / "terminal-mcp" / "node-agent"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _members(repo_root: Path) -> list[tuple[Path, str]]:
    """(source path, archive name) for everything that goes in, sorted.

    Sorted and with fixed metadata so two builds of the same tree produce
    the same bytes -- a bundle whose hash moved for no reason is a bundle
    nobody can verify.
    """
    members: list[tuple[Path, str]] = []
    for relative in BUNDLE_FILES:
        source = repo_root / relative
        if not source.is_file():
            raise BundleError("MISSING_SOURCE", f"{relative} is missing from {repo_root}")
        members.append((source, relative))

    package = repo_root / BUNDLE_PACKAGE
    if not package.is_dir():
        raise BundleError("MISSING_SOURCE", f"{BUNDLE_PACKAGE}/ is missing from {repo_root}")
    for path in package.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in _SKIP_SUFFIXES:
            continue
        members.append((path, path.relative_to(repo_root).as_posix()))
    members.sort(key=lambda item: item[1])
    return members


def build(repo_root: Path, destination: Path) -> Path:
    """Write the bundle zip. Deterministic: sorted members, a fixed
    timestamp and fixed permissions, so the same tree always hashes the
    same."""
    repo_root = Path(repo_root)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    members = _members(repo_root)
    # A fixed DOS timestamp (1980-01-01), because zip stores mtimes and the
    # checkout's mtimes are not a property of the release.
    fixed = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source, name in members:
            info = zipfile.ZipInfo(filename=name, date_time=fixed)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, source.read_bytes())
    return destination


def publish(source: Path, *, version: str, build_sha: str,
            root: Path | None = None) -> dict[str, Any]:
    """Copy a built bundle into the versioned store and write its manifest.

    The hash is computed from the bytes that land in the store, never from
    the builder's own report -- the node verifies against this manifest, so
    it has to describe the file the node will actually receive.
    """
    import shutil

    source = Path(source)
    if not source.is_file():
        raise BundleError("MISSING_FILE", f"{source} does not exist")
    root = root or bundle_root()
    directory = root / version
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / BUNDLE_NAME
    shutil.copy2(source, destination)

    manifest = {
        "version": version,
        "build_sha": build_sha,
        "bundle": {
            "name": BUNDLE_NAME,
            "sha256": sha256_of(destination),
            "size": destination.stat().st_size,
            "version": version,
            "build_sha": build_sha,
        },
    }
    (directory / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _published_versions(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted((child for child in root.iterdir()
                   if child.is_dir() and (child / MANIFEST_NAME).is_file()),
                  key=lambda p: p.name)


def resolve(*, root: Path | None = None, verify: bool = True) -> dict[str, Any]:
    """The newest published bundle, with the path to its bytes.

    `verify` re-hashes the file against the manifest. A bundle whose bytes
    have drifted from what the manifest promises is refused here rather
    than handed to a node that would install it.
    """
    root = root or bundle_root()
    versions = _published_versions(root)
    if not versions:
        raise BundleError("NOT_PUBLISHED", "no node-agent bundle has been published on this controller")
    directory = versions[-1]
    try:
        manifest = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError("BAD_MANIFEST", f"unreadable manifest in {directory.name}") from exc
    entry = manifest.get("bundle")
    if not isinstance(entry, dict):
        raise BundleError("BAD_MANIFEST", f"manifest in {directory.name} has no bundle entry")
    path = directory / str(entry.get("name") or BUNDLE_NAME)
    if not path.is_file():
        raise BundleError("MISSING_FILE", f"{path.name} is missing from {directory.name}")
    if verify:
        actual = sha256_of(path)
        if actual != entry.get("sha256"):
            raise BundleError("HASH_MISMATCH",
                              "the published bundle does not match its manifest")
    return {"path": path, "version": str(entry.get("version") or manifest.get("version") or ""),
            "build_sha": str(entry.get("build_sha") or manifest.get("build_sha") or ""),
            "sha256": str(entry.get("sha256") or ""), "size": int(entry.get("size") or 0),
            "name": path.name}


def available(root: Path | None = None) -> dict[str, Any]:
    """What this controller can hand a node, for a caller that must not
    raise. "Nothing published" is an honest answer, not an error."""
    try:
        found = resolve(root=root, verify=False)
    except BundleError:
        return {"published": False, "bundle": None}
    return {"published": True,
            "bundle": {k: v for k, v in found.items() if k != "path"}}
