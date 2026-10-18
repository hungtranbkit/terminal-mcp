"""Shared skill packs: one pinned, read-only copy per node, used by every session.

WHAT THIS IS FOR
----------------
A skill pack is a third-party kit of agent skills plus the tools that check
their output -- today exactly one, ux-ui-agent-skills (MIT, plugin87), declared
in `deploy/skill-packs/<id>.json`. The alternative this replaces is copying the
kit into every project (`npx ux-ui-agent-skills init`), which forks it per repo,
drifts per repo and rewrites a project's AGENTS.md/CLAUDE.md. Here the kit lives
ONCE per node:

    $TERMINAL_MCP_SKILL_PACK_HOME            (~/.local/share/terminal-mcp/skill-packs)
      bin/tmcp_skill_packs.py                 a copy of this file -- stdlib only
      bin/ux-ui-gates                         launcher: `ux-ui-gates review ...`
      <pack>/releases/<version>-<commit12>/
          kit/                                the pinned upstream tree, read-only
          node_modules/playwright             browser runtime for the render gates
          PROVENANCE.json                     repo, tag, commit, tree hash, licence
      <pack>/current  -> releases/...         what sessions use
      <pack>/previous -> releases/...         what `rollback` switches back to
      <pack>/state.json                       enabled, history, last gate result

HOW A SESSION FINDS IT (LAZILY)
-------------------------------
`sync` writes one small router skill, `ux-ui-kit`, into the user-level skill
directory of each host (Claude: ~/.claude/skills, Codex: $CODEX_HOME/skills).
Both hosts list a skill by its description and read the body only when a task
matches, so an unrelated session pays one line of description and nothing
else. The router points at the pinned kit by its stable `current` path; it never
copies a file into a project and never touches AGENTS.md or CLAUDE.md. A
directory of the same name that Terminal MCP did not write is left alone and
reported as a conflict.

The upstream plugin's hooks (SessionStart / PostToolUse / Stop) are NOT
installed: hooks fire on every session whether or not it is UI work, which is
the opposite of lazy, and this repo does not register third-party hooks (see
docs/GSTACK_INTEGRATION.md).

GATES REPORT WHAT THEY MEASURED, NOTHING ELSE
---------------------------------------------
The kit's own MCP `review_ui` can report "N/N passed" while gates were left
out: browser gates under `skip_browser`, token gates when no token file was
given, and `validate_component_spec` / `validate_contrast`, which check the
KIT's own files when no project path is passed. `verify_focustrap` with no
`--open` prints its usage and exits 0. So the runner here builds every gate's
argv itself and classifies each run:

    PASS      exit 0 and the gate actually looked
    FAIL      exit 1 -- the gate found a defect
    BLOCKED   exit 2 (could not look), or a missing browser/runtime
    ERROR     crashed or timed out
    NOT_RUN   the inputs it needs were not supplied, or it was skipped

and a review is PASS only when every catalogued gate ran and passed. FAIL wins
over everything; anything else is INCOMPLETE. The CLI exits 0 / 1 / 2 for
PASS / FAIL / INCOMPLETE, so a shell caller cannot mistake a partial review for
a clean one.

STANDALONE
----------
This file imports only the standard library, so `sync` can copy it next to the
packs and the launcher keeps working when the Terminal MCP checkout it came
from is replaced by a new release.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PACK_HOME_ENV = "TERMINAL_MCP_SKILL_PACK_HOME"
MANIFEST_DIR_ENV = "TERMINAL_MCP_SKILL_PACK_MANIFESTS"
OWNER_MARKER = ".terminal-mcp-skill-pack.json"
LAUNCHER_NAME = "ux-ui-gates"
STANDALONE_NAME = "tmcp_skill_packs.py"
KEEP_RELEASES = 3

# Pack states, worst first where it matters.
READY = "READY"            # verified tree, runtime present
DEGRADED = "DEGRADED"      # verified tree, but node/browser missing: gates will BLOCK
DRIFT = "DRIFT"            # installed release is not the manifest's pin
TAMPERED = "TAMPERED"      # tree hash no longer matches the pin
MISSING = "MISSING"
DISABLED = "DISABLED"

# Gate run statuses.
PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
ERROR = "ERROR"
NOT_RUN = "NOT_RUN"
INCOMPLETE = "INCOMPLETE"
_SEVERITY = {FAIL: 4, ERROR: 3, BLOCKED: 2, NOT_RUN: 1, PASS: 0}
EXIT_FOR = {PASS: 0, FAIL: 1, INCOMPLETE: 2}

MAX_OUTPUT_CHARS = 6000
MAX_HTML_TARGETS = 10
GATE_TIMEOUT_SECONDS = 300


class SkillPackError(RuntimeError):
    """A refusal with a reason the operator can act on."""


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Manifest:
    id: str
    display_name: str
    version: str
    repo: str
    tag: str
    commit: str
    tree_sha256: str
    tree_files: int
    license_spdx: str
    license_file: str
    license_sha256: str
    copyright: str = ""
    node_min: str = "20"
    playwright: str = ""
    router_skill: str = ""
    hosts: tuple[str, ...] = ("claude", "codex")
    register_mcp: dict[str, bool] = field(default_factory=dict)
    mcp_server_name: str = ""
    upstream_counts: dict[str, int] = field(default_factory=dict)
    path: str = ""

    @property
    def release_name(self) -> str:
        return f"{self.version}-{self.commit[:12]}"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "display_name": self.display_name, "version": self.version,
                "repo": self.repo, "tag": self.tag, "commit": self.commit,
                "tree_sha256": self.tree_sha256, "license": self.license_spdx,
                "router_skill": self.router_skill, "hosts": list(self.hosts),
                "register_mcp": dict(self.register_mcp), "upstream_counts": dict(self.upstream_counts),
                "manifest": self.path}


_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def manifest_dirs() -> list[Path]:
    """Where manifests are read from: the env override, then the repository's
    `deploy/skill-packs`, then a copy `sync` leaves next to the packs."""
    dirs: list[Path] = []
    if os.environ.get(MANIFEST_DIR_ENV):
        dirs.append(Path(os.environ[MANIFEST_DIR_ENV]).expanduser())
    dirs.append(Path(__file__).resolve().parent.parent / "deploy" / "skill-packs")
    dirs.append(pack_home() / "manifests")
    return dirs


def parse_manifest(data: dict[str, Any], path: str = "") -> Manifest:
    try:
        source, licence = data["source"], data["license"]
        manifest = Manifest(
            id=str(data["id"]), display_name=str(data.get("display_name") or data["id"]),
            version=str(data["version"]), repo=str(source["repo"]), tag=str(source["tag"]),
            commit=str(source["commit"]).lower(), tree_sha256=str(source["tree_sha256"]).lower(),
            tree_files=int(source.get("tree_files") or 0),
            license_spdx=str(licence["spdx"]), license_file=str(licence.get("file") or "LICENSE"),
            license_sha256=str(licence["sha256"]).lower(), copyright=str(licence.get("copyright") or ""),
            node_min=str((data.get("runtime") or {}).get("node_min") or "20"),
            playwright=str((data.get("runtime") or {}).get("playwright") or ""),
            router_skill=str(data.get("router_skill") or ""),
            hosts=tuple(data.get("hosts") or ("claude", "codex")),
            register_mcp={str(k): bool(v) for k, v in (data.get("register_mcp") or {}).items()},
            mcp_server_name=str(data.get("mcp_server_name") or ""),
            upstream_counts={str(k): int(v) for k, v in (data.get("upstream_counts") or {}).items()},
            path=path)
    except (KeyError, TypeError, ValueError) as exc:
        raise SkillPackError(f"invalid skill pack manifest {path or '<inline>'}: {exc}") from exc
    # A manifest names directories and becomes argv; refuse anything that is
    # not exactly the shape a pin has.
    if not _SLUG.match(manifest.id) or (manifest.router_skill and not _SLUG.match(manifest.router_skill)):
        raise SkillPackError(f"manifest {path}: id/router_skill must be slugs")
    if not _HEX40.match(manifest.commit) or not _HEX64.match(manifest.tree_sha256) \
            or not _HEX64.match(manifest.license_sha256):
        raise SkillPackError(f"manifest {path}: commit must be a full SHA-1 and hashes full SHA-256")
    if not re.match(r"^[0-9A-Za-z._+-]{1,64}$", manifest.version) or not manifest.repo.startswith(("https://", "file://", "/")):
        raise SkillPackError(f"manifest {path}: bad version or repo")
    if set(manifest.hosts) - {"claude", "codex"}:
        raise SkillPackError(f"manifest {path}: unknown host in {manifest.hosts}")
    return manifest


def list_manifests() -> list[Manifest]:
    seen: dict[str, Manifest] = {}
    for directory in manifest_dirs():
        try:
            files = sorted(directory.glob("*.json"))
        except OSError:
            continue
        for file in files:
            try:
                manifest = parse_manifest(json.loads(file.read_text(encoding="utf-8")), str(file))
            except (OSError, ValueError, SkillPackError):
                continue
            seen.setdefault(manifest.id, manifest)
    return list(seen.values())


def load_manifest(pack_id: str) -> Manifest:
    for manifest in list_manifests():
        if manifest.id == pack_id:
            return manifest
    raise SkillPackError(f"no manifest for skill pack {pack_id!r} in {[str(d) for d in manifest_dirs()]}")


# ---------------------------------------------------------------------------
# Paths and small helpers
# ---------------------------------------------------------------------------
def pack_home() -> Path:
    raw = os.environ.get(PACK_HOME_ENV)
    if raw:
        return Path(raw).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "terminal-mcp" / "skill-packs"


def pack_dir(pack_id: str) -> Path:
    return pack_home() / pack_id


def claude_skills_dir() -> Path:
    return Path(os.environ.get("CLAUDE_SKILLS_HOME") or (Path.home() / ".claude" / "skills")).expanduser()


def codex_skills_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser() / "skills"


def host_skills_dir(host: str) -> Path:
    return claude_skills_dir() if host == "claude" else codex_skills_dir()


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_state(pack_id: str) -> dict[str, Any]:
    state = _read_json(pack_dir(pack_id) / "state.json", {})
    return state if isinstance(state, dict) else {}


def save_state(pack_id: str, state: dict[str, Any]) -> None:
    _write_json_atomic(pack_dir(pack_id) / "state.json", state)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(root: Path) -> tuple[int, str]:
    """(file count, sha256) over every file under `root` except `.git`.

    Each entry is `relpath NUL sha256(content)`, in a deterministic walk, so the
    hash pins content AND layout. A symlink contributes its target string, never
    the file it points at -- following it could hash something outside the tree.
    """
    digest = hashlib.sha256()
    count = 0
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files):
            full = os.path.join(directory, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if os.path.islink(full):
                entry = hashlib.sha256(("L:" + os.readlink(full)).encode()).hexdigest()
            else:
                entry = _sha256_file(Path(full))
            digest.update(rel.encode() + b"\0" + entry.encode() + b"\n")
            count += 1
    return count, digest.hexdigest()


def _set_writable(root: Path, writable: bool) -> None:
    """Make a tree read-only (or writable again so it can be removed)."""
    if not root.exists():
        return
    for directory, dirs, files in os.walk(root, topdown=not writable):
        for name in files:
            path = os.path.join(directory, name)
            if os.path.islink(path):
                continue
            mode = os.stat(path).st_mode
            os.chmod(path, (mode | stat.S_IWUSR) if writable else (mode & ~0o222))
        for name in dirs:
            path = os.path.join(directory, name)
            if os.path.islink(path):
                continue
            mode = os.stat(path).st_mode
            os.chmod(path, (mode | stat.S_IWUSR) if writable else (mode & ~0o222))
    mode = os.stat(root).st_mode
    os.chmod(root, (mode | stat.S_IWUSR) if writable else (mode & ~0o222))


def _remove_tree(root: Path) -> None:
    if root.is_symlink():
        root.unlink()
        return
    if root.exists():
        _set_writable(root, True)
        shutil.rmtree(root)


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value)[:3]) or (0,)


def _extra_bin_dirs() -> list[str]:
    home = Path.home()
    dirs = [str(home / ".local" / "bin"), str(home / ".npm-global" / "bin"), "/usr/local/bin",
            "/opt/homebrew/bin", "/snap/bin", str(home / ".bun" / "bin")]
    nvm = sorted((home / ".nvm" / "versions" / "node").glob("v*/bin"),
                 key=lambda p: _version_tuple(p.parent.name), reverse=True)
    return [*map(str, nvm), *dirs]


def find_executable(name: str) -> str | None:
    """`shutil.which`, then the places a login shell would have added to PATH.

    Node-agents run under systemd/launchd with a minimal PATH; a node that has
    `node` via nvm must not be reported as lacking it."""
    found = shutil.which(name)
    if found:
        return found
    return shutil.which(name, path=os.pathsep.join(_extra_bin_dirs()))


def node_runtime(node_min: str = "20") -> dict[str, Any]:
    node = find_executable("node")
    if not node:
        return {"ok": False, "node": None, "detail": "node not found on PATH or the usual install locations"}
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "node": node, "detail": f"node --version failed: {exc}"}
    ok = _version_tuple(out) >= _version_tuple(node_min)
    return {"ok": ok, "node": node, "version": out,
            "detail": "ok" if ok else f"node {out} is older than the required {node_min}"}


def browser_runtime(release: Path | None) -> dict[str, Any]:
    """What a render gate will find. A probe, not proof: only a gate run that
    exits 0 proves a browser launched."""
    playwright = None
    if release is not None:
        package = release / "node_modules" / "playwright" / "package.json"
        data = _read_json(package, None)
        if isinstance(data, dict):
            playwright = data.get("version")
    chrome = None
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"):
        chrome = find_executable(name)
        if chrome:
            break
    mac_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome and mac_chrome.exists():
        chrome = str(mac_chrome)
    caches = [Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])] if os.environ.get("PLAYWRIGHT_BROWSERS_PATH") else []
    caches += [Path.home() / ".cache" / "ms-playwright", Path.home() / "Library" / "Caches" / "ms-playwright"]
    bundled = sorted(str(p) for cache in caches for p in cache.glob("chromium*") if p.is_dir()) if caches else []
    ok = bool(playwright) and bool(chrome or bundled)
    if not playwright:
        detail = "playwright is not installed next to the kit; browser gates will BLOCK"
    elif not (chrome or bundled):
        detail = "no Chrome and no Playwright Chromium found; browser gates will BLOCK"
    else:
        detail = "ok"
    return {"ok": ok, "playwright": playwright, "chrome": chrome, "playwright_chromium": bundled[-1:] or [],
            "detail": detail}


@contextlib.contextmanager
def _lock(pack_id: str) -> Iterator[None]:
    """One installer per pack per node. POSIX only; elsewhere a no-op."""
    path = pack_dir(pack_id) / ".lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows
        yield
        return
    with path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SkillPackError(f"another sync of {pack_id} is running (lock {path})") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _run(argv: Sequence[str], *, cwd: Path | None = None, timeout: int = 600,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                          timeout=timeout, env=env)


# ---------------------------------------------------------------------------
# Install / verify / rollback
# ---------------------------------------------------------------------------
def _current_release(pack_id: str) -> Path | None:
    link = pack_dir(pack_id) / "current"
    try:
        return link.resolve(strict=True) if link.is_symlink() else None
    except OSError:
        return None


def _previous_release(pack_id: str) -> Path | None:
    link = pack_dir(pack_id) / "previous"
    try:
        return link.resolve(strict=True) if link.is_symlink() else None
    except OSError:
        return None


def _point(link: Path, target: Path) -> None:
    """Atomically repoint a symlink (relative, so the home can be moved)."""
    tmp = link.with_name(f".{link.name}.tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    os.symlink(os.path.relpath(target, link.parent), tmp)
    os.replace(tmp, link)


def verify_release(manifest: Manifest, release: Path) -> dict[str, Any]:
    """Re-hash the installed tree and licence against the manifest's pin."""
    kit = release / "kit"
    problems: list[str] = []
    provenance = _read_json(release / "PROVENANCE.json", {})
    if not kit.is_dir():
        return {"ok": False, "problems": [f"{kit} is missing"], "provenance": provenance}
    count, digest = tree_digest(kit)
    if digest != manifest.tree_sha256:
        problems.append(f"tree sha256 {digest} != pinned {manifest.tree_sha256} ({count} files)")
    licence = kit / manifest.license_file
    try:
        licence_sha = _sha256_file(licence)
    except OSError:
        licence_sha = None
    if licence_sha != manifest.license_sha256:
        problems.append(f"{manifest.license_file} sha256 {licence_sha} != pinned {manifest.license_sha256}")
    package = _read_json(kit / "package.json", {})
    if package and str(package.get("version")) != manifest.version:
        problems.append(f"package.json version {package.get('version')} != {manifest.version}")
    if provenance.get("commit") and provenance.get("commit") != manifest.commit:
        problems.append(f"provenance commit {provenance.get('commit')} != pinned {manifest.commit}")
    return {"ok": not problems, "problems": problems, "files": count, "tree_sha256": digest,
            "provenance": provenance}


def install_runtime(manifest: Manifest, release: Path, *, install_browser: bool = True) -> dict[str, Any]:
    """Playwright next to the kit (so the kit's `import('playwright')` resolves
    it by walking up from kit/scripts) and, when no Chrome is present, the
    Playwright Chromium build. `--ignore-scripts`: no package install script
    runs; the browser download is the one explicit step below."""
    result: dict[str, Any] = {"node": node_runtime(manifest.node_min)}
    if not manifest.playwright:
        result["playwright"] = "not required by manifest"
        return result
    if not result["node"]["ok"]:
        result["playwright"] = "skipped: " + result["node"]["detail"]
        return result
    npm = find_executable("npm")
    if not npm:
        result["playwright"] = "skipped: npm not found"
        return result
    existing = _read_json(release / "node_modules" / "playwright" / "package.json", {})
    if existing.get("version") != manifest.playwright:
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([str(Path(npm).parent), env.get("PATH", "")])
        proc = _run([npm, "install", "--prefix", str(release), "--no-save", "--no-package-lock",
                     "--ignore-scripts", "--no-audit", "--no-fund", f"playwright@{manifest.playwright}"],
                    cwd=release, timeout=900, env=env)
        if proc.returncode != 0:
            result["playwright"] = f"npm install failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-800:]}"
            return result
    result["playwright"] = manifest.playwright
    browser = browser_runtime(release)
    if browser["ok"] or not install_browser:
        result["browser"] = browser
        return result
    cli = release / "node_modules" / "playwright" / "cli.js"
    proc = _run([result["node"]["node"], str(cli), "install", "chromium"], cwd=release, timeout=1800)
    result["browser_install"] = {"exit": proc.returncode, "tail": (proc.stdout + proc.stderr)[-600:]}
    result["browser"] = browser_runtime(release)
    return result


def install(manifest: Manifest, *, install_browser: bool = True, force: bool = False) -> dict[str, Any]:
    """Fetch the pinned tag, prove it is the pinned commit and tree, make it
    read-only and switch `current` to it. The previous `current` becomes
    `previous`. Re-running with the same pin is a no-op unless `force`."""
    git = find_executable("git")
    if not git:
        raise SkillPackError("git is required to install a skill pack")
    home = pack_dir(manifest.id)
    releases = home / "releases"
    target = releases / manifest.release_name
    releases.mkdir(parents=True, exist_ok=True)
    with _lock(manifest.id):
        reused = False
        if target.exists() and not force and verify_release(manifest, target)["ok"]:
            reused = True
        else:
            staging = Path(tempfile.mkdtemp(prefix=f".staging-{manifest.release_name}-", dir=str(releases)))
            try:
                kit = staging / "kit"
                proc = _run([git, "-c", "advice.detachedHead=false", "clone", "--quiet", "--depth", "1",
                             "--branch", manifest.tag, manifest.repo, str(kit)], timeout=600)
                if proc.returncode != 0:
                    raise SkillPackError(f"git clone of {manifest.repo}@{manifest.tag} failed: {proc.stderr.strip()[-400:]}")
                head = _run([git, "-C", str(kit), "rev-parse", "HEAD"]).stdout.strip()
                if head != manifest.commit:
                    raise SkillPackError(f"tag {manifest.tag} resolves to {head}, not the pinned {manifest.commit}; "
                                         "refusing a moved tag")
                shutil.rmtree(kit / ".git")
                check = verify_release(manifest, staging)
                if not check["ok"]:
                    raise SkillPackError("pinned content check failed: " + "; ".join(check["problems"]))
                _write_json_atomic(staging / "PROVENANCE.json", {
                    "pack": manifest.id, "version": manifest.version, "repo": manifest.repo,
                    "tag": manifest.tag, "commit": manifest.commit, "tree_sha256": check["tree_sha256"],
                    "tree_files": check["files"], "license": manifest.license_spdx,
                    "license_sha256": manifest.license_sha256, "copyright": manifest.copyright,
                    "installed_at": _now(), "installed_by": "terminal-mcp skill_packs",
                    "manifest": manifest.path})
                _set_writable(kit, False)
                if target.exists():
                    _remove_tree(target)
                os.replace(staging, target)
            except BaseException:
                _remove_tree(staging)
                raise
        runtime = install_runtime(manifest, target, install_browser=install_browser)
        before = _current_release(manifest.id)
        if before != target.resolve():
            if before is not None:
                _point(home / "previous", before)
            _point(home / "current", target)
        state = load_state(manifest.id)
        history = list(state.get("history") or [])
        if before != target.resolve():
            history.append({"at": _now(), "action": "install", "release": manifest.release_name,
                            "from": before.name if before else None})
        state.update({"history": history[-20:], "runtime": runtime, "enabled": state.get("enabled", True)})
        state.pop("pinned_release", None)
        save_state(manifest.id, state)
        _prune(manifest.id)
    return {"pack": manifest.id, "release": manifest.release_name, "reused": reused,
            "switched": before != target.resolve(), "previous": before.name if before else None,
            "runtime": runtime}


def _prune(pack_id: str) -> None:
    releases = pack_dir(pack_id) / "releases"
    keep = {p for p in (_current_release(pack_id), _previous_release(pack_id)) if p}
    candidates = sorted((p for p in releases.iterdir() if p.is_dir() and not p.name.startswith(".")),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in candidates[KEEP_RELEASES:]:
        if stale.resolve() not in keep:
            _remove_tree(stale)


def rollback(pack_id: str) -> dict[str, Any]:
    """Swap `current` and `previous`. Sessions pick the change up on their next
    read of the stable `current` path; nothing has to restart."""
    home = pack_dir(pack_id)
    with _lock(pack_id):
        current, previous = _current_release(pack_id), _previous_release(pack_id)
        if previous is None:
            raise SkillPackError(f"{pack_id} has no previous release to roll back to")
        _point(home / "current", previous)
        if current is not None:
            _point(home / "previous", current)
        state = load_state(pack_id)
        history = list(state.get("history") or [])
        history.append({"at": _now(), "action": "rollback", "release": previous.name,
                        "from": current.name if current else None})
        state["history"] = history[-20:]
        state["pinned_release"] = previous.name
        save_state(pack_id, state)
    return {"pack": pack_id, "current": previous.name, "previous": current.name if current else None}


# ---------------------------------------------------------------------------
# Router skills, launcher and MCP registration
# ---------------------------------------------------------------------------
def _front_matter(text: str) -> dict[str, str]:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    fields: dict[str, str] = {}
    for line in (match.group(1).splitlines() if match else ()):
        found = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", line.strip())
        if found:
            fields[found.group(1).lower()] = found.group(2).strip().strip("'\"")
    return fields


def kit_skills(kit: Path) -> list[dict[str, str]]:
    skills = []
    root = kit / ".claude" / "skills"
    for child in sorted(root.iterdir()) if root.is_dir() else ():
        skill = child / "SKILL.md"
        if not skill.is_file():
            continue
        with skill.open("r", encoding="utf-8", errors="replace") as handle:
            fields = _front_matter(handle.read(8192))
        description = re.sub(r"\s+", " ", fields.get("description", "")).strip()
        skills.append({"name": fields.get("name") or child.name, "dir": child.name,
                       "description": description[:160] + ("..." if len(description) > 160 else "")})
    return skills


def launcher_path() -> Path:
    return pack_home() / "bin" / LAUNCHER_NAME


def router_skill_text(manifest: Manifest, *, host: str) -> str:
    kit = pack_dir(manifest.id) / "current" / "kit"
    release = _current_release(manifest.id)
    skills = kit_skills(release / "kit") if release else []
    gates = launcher_path()
    lines = [
        "---",
        f"name: {manifest.router_skill}",
        (f"description: Shared UX/UI design-system kit ({manifest.id} {manifest.version}, "
         f"{manifest.license_spdx}) installed once on this Terminal MCP node. Use ONLY for UI, "
         "frontend or visual-design work - building or reviewing a page, screen, component, design "
         "tokens, theme, accessibility/WCAG, responsive layout or UI QA. Do not use for backend, "
         "infra, data or other non-UI tasks."),
        "---",
        "",
        f"# {manifest.display_name} (shared, read-only)",
        "",
        f"Pinned `{manifest.id}` {manifest.version}, tag `{manifest.tag}`, commit `{manifest.commit[:12]}`, "
        f"{manifest.license_spdx} licence ({manifest.copyright or manifest.repo}). Installed and verified by "
        "Terminal MCP; one copy per node, shared by every project.",
        "",
        f"KIT = `{kit}`",
        "",
        "## Rules",
        "",
        "1. Never copy the kit into the project and never run `npx ux-ui-agent-skills init` here. "
        "Never write inside KIT (it is read-only).",
        "2. Precedence, highest first: the user's instruction; the project's own AGENTS.md / CLAUDE.md and "
        "UI rules; the project's existing design tokens, theme, components and brand; then this kit. "
        "Kit tokens and the 138 library design systems are references, never replacements for the "
        "project's own branding unless the user asks.",
        "3. Do not edit the project's AGENTS.md or CLAUDE.md to add this kit.",
        "4. Never state a contrast ratio, WCAG verdict or pass count a gate did not just print.",
        "",
        "## Kit skills (read only the one the task needs)",
        "",
        "Read `KIT/.claude/skills/<name>/SKILL.md` on demand. When a kit skill says `scripts/...`, "
        "`tokens/...` or `.claude/rules/...`, that path is inside KIT; run it against the PROJECT's files.",
        "",
    ]
    lines += [f"- `{s['dir']}` - {s['description']}" for s in skills] or ["- (kit not installed yet)"]
    lines += [
        "",
        "Doctrine: `KIT/AGENTS.md` (the whole brief), `KIT/.claude/rules/*.md`.",
        "",
        "## Gates - measure, never claim",
        "",
        "```",
        f"{gates} review <files-or-dirs> --tokens <colors.json> --theme <theme.css> \\",
        "    [--focustrap-open '<trigger selector>'] [--component-root <dir with components/>] [--json]",
        f"{gates} gate <gate_name> <path> [extra args]      # one gate",
        f"{gates} list                                       # the catalogue",
        "```",
        "",
        "Exit 0 = PASS: every catalogued gate ran and passed. Exit 1 = FAIL: a gate found a defect. "
        "Exit 2 = INCOMPLETE: something was not measured (missing browser/node, input not supplied, "
        "skipped) - never report that as a pass. Report the printed verdict line and per-gate exit codes.",
        "",
    ]
    if host == "claude":
        lines.append(f"Optional: the same gates as the `{manifest.mcp_server_name}` MCP server (opt-in via "
                     "`terminal-mcp-skill-packs sync --claude-mcp`) "
                     "(review_ui, run_gate, list_gates, get_doctrine, get_design_system, get_tokens, pack_status).")
    elif host == "codex":
        lines.append("Codex: Claude Code hooks and plugin skills are not automatic here; this router skill, "
                     "KIT/AGENTS.md and the gate command above are the Codex path. The MCP server can be "
                     f"enabled with `{gates} mcp-config codex`.")
    lines.append("")
    return "\n".join(lines)


def _install_router(manifest: Manifest, host: str) -> dict[str, Any]:
    directory = host_skills_dir(host) / manifest.router_skill
    marker = directory / OWNER_MARKER
    if directory.exists() and not marker.is_file():
        return {"host": host, "status": "CONFLICT", "path": str(directory),
                "detail": "a skill with this name exists and was not written by Terminal MCP; left untouched"}
    directory.mkdir(parents=True, exist_ok=True)
    text = router_skill_text(manifest, host=host)
    skill = directory / "SKILL.md"
    changed = not skill.is_file() or skill.read_text(encoding="utf-8", errors="replace") != text
    if changed:
        tmp = skill.with_name(".SKILL.md.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, skill)
    _write_json_atomic(marker, {"pack": manifest.id, "version": manifest.version, "commit": manifest.commit,
                                "written_by": "terminal-mcp skill_packs"})
    return {"host": host, "status": "INSTALLED", "path": str(skill), "changed": changed}


def _remove_router(manifest: Manifest, host: str) -> dict[str, Any]:
    directory = host_skills_dir(host) / manifest.router_skill
    if not directory.exists():
        return {"host": host, "status": "ABSENT"}
    if not (directory / OWNER_MARKER).is_file():
        return {"host": host, "status": "CONFLICT", "detail": "not ours; left untouched"}
    shutil.rmtree(directory)
    return {"host": host, "status": "REMOVED"}


def _install_launcher() -> dict[str, Any]:
    """Copy this module next to the packs and write a launcher for it. The copy
    is what keeps `ux-ui-gates` working after the Terminal MCP checkout that
    installed it is replaced."""
    bin_dir = pack_home() / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    module = bin_dir / STANDALONE_NAME
    shutil.copyfile(__file__, module)
    manifests = pack_home() / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    for manifest in list_manifests():
        if manifest.path and Path(manifest.path).parent != manifests:
            shutil.copyfile(manifest.path, manifests / f"{manifest.id}.json")
    python = sys.executable or "python3"
    launcher = launcher_path()
    launcher.write_text(
        "#!/bin/sh\n"
        "# Written by terminal-mcp skill_packs. Shared UX/UI gates; see docs/UX_UI_SKILL_PACK.md.\n"
        f"export {PACK_HOME_ENV}=\"${{{PACK_HOME_ENV}:-{pack_home()}}}\"\n"
        f"PY=\"{python}\"; [ -x \"$PY\" ] || PY=python3\n"
        f"exec \"$PY\" \"{module}\" \"$@\"\n", encoding="utf-8")
    launcher.chmod(0o755)
    return {"launcher": str(launcher), "module": str(module)}


def _claude_mcp(manifest: Manifest, *, register: bool) -> dict[str, Any]:
    claude = find_executable("claude")
    name = manifest.mcp_server_name
    state = load_state(manifest.id)
    if not claude:
        return {"host": "claude", "status": "SKIPPED", "detail": "claude CLI not found"}
    try:
        current = _run([claude, "mcp", "get", name], timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"host": "claude", "status": "ERROR", "detail": str(exc)}
    present = current.returncode == 0
    ours = str(launcher_path()) in (current.stdout or "")
    if register:
        if present:
            return {"host": "claude", "status": "PRESENT" if ours else "CONFLICT",
                    "detail": "already registered" if ours else f"an MCP server named {name} exists; left untouched"}
        proc = _run([claude, "mcp", "add", "--scope", "user", name, "--", str(launcher_path()), "mcp"], timeout=60)
        if proc.returncode != 0:
            return {"host": "claude", "status": "ERROR", "detail": (proc.stderr or proc.stdout)[-400:]}
        state["claude_mcp_registered"] = True
        save_state(manifest.id, state)
        return {"host": "claude", "status": "REGISTERED"}
    if present and ours and state.get("claude_mcp_registered"):
        proc = _run([claude, "mcp", "remove", "--scope", "user", name], timeout=60)
        state["claude_mcp_registered"] = False
        save_state(manifest.id, state)
        return {"host": "claude", "status": "REMOVED" if proc.returncode == 0 else "ERROR",
                "detail": (proc.stderr or "")[-300:]}
    return {"host": "claude", "status": "UNCHANGED"}


_CODEX_BEGIN = "# >>> terminal-mcp skill pack {name} >>>"
_CODEX_END = "# <<< terminal-mcp skill pack {name} <<<"


def codex_mcp_block(manifest: Manifest) -> str:
    name = manifest.mcp_server_name
    return "\n".join([_CODEX_BEGIN.format(name=name), f"[mcp_servers.{name}]",
                      f"command = {json.dumps(str(launcher_path()))}", 'args = ["mcp"]',
                      _CODEX_END.format(name=name), ""])


def _codex_mcp(manifest: Manifest, *, register: bool) -> dict[str, Any]:
    """Codex keeps MCP servers in config.toml. Only a block between our own
    markers is ever added or removed; an existing table of the same name is a
    conflict, not something to overwrite."""
    config = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser() / "config.toml"
    name = manifest.mcp_server_name
    text = config.read_text(encoding="utf-8") if config.is_file() else ""
    begin, end = _CODEX_BEGIN.format(name=name), _CODEX_END.format(name=name)
    ours = begin in text
    if register:
        if ours:
            return {"host": "codex", "status": "PRESENT"}
        if re.search(rf"^\[mcp_servers\.{re.escape(name)}\]", text, re.MULTILINE):
            return {"host": "codex", "status": "CONFLICT", "detail": f"{config} already defines {name}"}
        if config.is_file():
            shutil.copy2(config, config.with_name(f"config.toml.bak-skillpack-{int(time.time())}"))
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(text + ("" if text.endswith("\n") or not text else "\n") + "\n" + codex_mcp_block(manifest),
                          encoding="utf-8")
        return {"host": "codex", "status": "REGISTERED", "path": str(config)}
    if ours:
        pattern = re.compile(rf"\n?{re.escape(begin)}.*?{re.escape(end)}\n?", re.DOTALL)
        config.write_text(pattern.sub("\n", text), encoding="utf-8")
        return {"host": "codex", "status": "REMOVED", "path": str(config)}
    return {"host": "codex", "status": "UNCHANGED"}


def integrate(manifest: Manifest, *, codex_mcp: bool | None = None,
              claude_mcp: bool | None = None) -> dict[str, Any]:
    """Launcher + router skills always; MCP registration only when the
    manifest or the operator asks. Registering writes the host's USER config
    (~/.claude.json, ~/.codex/config.toml) and loads the tools into every
    session, which is the opposite of lazy -- so both default to off."""
    launcher = _install_launcher()
    routers = [_install_router(manifest, host) for host in manifest.hosts]
    mcp = []
    if manifest.mcp_server_name:
        want_claude = manifest.register_mcp.get("claude") if claude_mcp is None else claude_mcp
        if "claude" in manifest.hosts and want_claude:
            mcp.append(_claude_mcp(manifest, register=True))
        want_codex = manifest.register_mcp.get("codex") if codex_mcp is None else codex_mcp
        if "codex" in manifest.hosts and want_codex:
            mcp.append(_codex_mcp(manifest, register=True))
    return {"launcher": launcher, "router_skills": routers, "mcp": mcp}


def set_enabled(pack_id: str, enabled: bool) -> dict[str, Any]:
    """Disable removes everything a session can see (router skills, MCP
    registrations Terminal MCP made) and keeps the releases, so enable is
    instant and needs no network."""
    manifest = load_manifest(pack_id)
    state = load_state(pack_id)
    state["enabled"] = enabled
    save_state(pack_id, state)
    if enabled:
        return {"pack": pack_id, "enabled": True, **integrate(manifest)}
    removed = [_remove_router(manifest, host) for host in manifest.hosts]
    mcp = [_claude_mcp(manifest, register=False), _codex_mcp(manifest, register=False)] \
        if manifest.mcp_server_name else []
    return {"pack": pack_id, "enabled": False, "router_skills": removed, "mcp": mcp}


def sync(pack_ids: Iterable[str] | None = None, *, install_browser: bool = True,
         codex_mcp: bool | None = None, claude_mcp: bool | None = None, force: bool = False) -> dict[str, Any]:
    """Bring this node to the manifests: install a missing/drifted/tampered
    pin, then (re)write the router skills and launcher. Idempotent."""
    manifests = [load_manifest(p) for p in pack_ids] if pack_ids else list_manifests()
    report: dict[str, Any] = {"node": os.uname().nodename if hasattr(os, "uname") else "", "packs": []}
    for manifest in manifests:
        entry: dict[str, Any] = {"pack": manifest.id}
        try:
            state = load_state(manifest.id)
            if state.get("enabled") is False:
                entry["status"] = DISABLED
                report["packs"].append(entry)
                continue
            before = status(manifest.id, verify=True)
            # A rollback pins the release it chose; only --force moves past it.
            pinned_elsewhere = (not force and state.get("pinned_release")
                                and state["pinned_release"] != manifest.release_name)
            if before["state"] in (MISSING, DRIFT, TAMPERED) and not (before["state"] == DRIFT and pinned_elsewhere):
                entry["install"] = install(manifest, install_browser=install_browser,
                                           force=before["state"] == TAMPERED)
            elif before["state"] == DEGRADED and _current_release(manifest.id):
                entry["runtime"] = install_runtime(manifest, _current_release(manifest.id),
                                                   install_browser=install_browser)
            entry["integrate"] = integrate(manifest, codex_mcp=codex_mcp, claude_mcp=claude_mcp)
            entry["status"] = status(manifest.id, verify=False)["state"]
        except (SkillPackError, OSError, subprocess.SubprocessError) as exc:
            entry["status"] = "ERROR"
            entry["error"] = str(exc)
            state = load_state(manifest.id)
            state["last_error"] = {"at": _now(), "error": str(exc)[:800]}
            with contextlib.suppress(OSError):
                save_state(manifest.id, state)
        report["packs"].append(entry)
    report["ok"] = all(p.get("status") in (READY, DEGRADED, DISABLED) for p in report["packs"])
    return report


def status(pack_id: str, *, verify: bool = True) -> dict[str, Any]:
    """Everything an admin asks about one pack on this node, from disk."""
    try:
        manifest = load_manifest(pack_id)
    except SkillPackError as exc:
        return {"pack": pack_id, "state": MISSING, "detail": str(exc)}
    state = load_state(pack_id)
    release = _current_release(pack_id)
    result: dict[str, Any] = {"pack": pack_id, "manifest": manifest.to_dict(), "home": str(pack_dir(pack_id)),
                              "enabled": state.get("enabled", True), "pinned_release": state.get("pinned_release"),
                              "history": (state.get("history") or [])[-5:], "last_gate": state.get("last_gate"),
                              "last_selfcheck": state.get("last_selfcheck"),
                              "last_error": state.get("last_error"), "launcher": str(launcher_path())
                              if launcher_path().exists() else None}
    routers = {}
    for host in manifest.hosts:
        directory = host_skills_dir(host) / manifest.router_skill
        routers[host] = ("INSTALLED" if (directory / OWNER_MARKER).is_file() and (directory / "SKILL.md").is_file()
                         else "CONFLICT" if directory.exists() else "ABSENT")
    result["router_skills"] = routers
    result["agent_compatibility"] = {
        "claude": "router skill + kit skills read on demand" + (
            f"; MCP server {manifest.mcp_server_name}" if state.get("claude_mcp_registered") else ""),
        "codex": "router skill + KIT/AGENTS.md + gate CLI; Claude hooks/plugin skills are not automatic for Codex",
    }
    if release is None:
        result.update(state=DISABLED if state.get("enabled") is False else MISSING,
                      detail="no current release installed")
        return result
    provenance = _read_json(release / "PROVENANCE.json", {})
    result.update(release=release.name, provenance=provenance)
    node = node_runtime(manifest.node_min)
    browser = browser_runtime(release)
    result["runtime"] = {"node": node, "browser": browser}
    if verify:
        check = verify_release(manifest, release)
        result["verification"] = {k: v for k, v in check.items() if k != "provenance"}
        if not check["ok"] and release.name == manifest.release_name:
            result.update(state=TAMPERED, detail="; ".join(check["problems"]))
            return result
    if release.name != manifest.release_name:
        result.update(state=DRIFT, detail=f"current release {release.name} is not the manifest pin "
                                          f"{manifest.release_name}" + (" (rolled back on purpose)"
                                                                        if state.get("pinned_release") else ""))
    elif state.get("enabled") is False:
        result.update(state=DISABLED, detail="disabled by operator; releases kept")
    elif not node["ok"] or not browser["ok"]:
        result.update(state=DEGRADED, detail=f"node: {node['detail']}; browser: {browser['detail']}")
    else:
        result.update(state=READY, detail=f"{manifest.id} {manifest.version} verified")
    return result


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------
_CATALOGUE_JS = ("import(process.argv[1]).then(m => process.stdout.write(JSON.stringify(m.catalogue())))"
                 ".catch(e => { console.error(String(e)); process.exit(2); })")
# A browser that could not start, or died under the gate, measured nothing:
# BLOCKED, never FAIL. Seen live on m910: Chrome launched, its GPU process
# could not start, and every render gate exited 1 with "Target page, context
# or browser has been closed".
_BROWSER_MISSING = re.compile(r"playwright not installed|Executable doesn't exist|browserType\.launch|"
                              r"Failed to launch|Host system is missing dependencies|"
                              r"Target (?:page, context or browser|closed|crashed)|Page crashed|browser has been closed|"
                              r"GPU process isn't usable|Browser closed", re.IGNORECASE)
# An exception nobody caught is a crash, not a finding.
_CRASH = re.compile(r"Traceback \(most recent call last\)|triggerUncaughtException|"
                    r"^\s+at (?:\S+ \()?(?:file://|node:|/)\S*:\d+:\d+\)?(?: \{)?$", re.MULTILINE)

FILE_GATES_ON_PATHS = ("check_no_emoji", "lint_hardcodes", "lint_native_select")
TOKEN_GATES = ("validate_tokens", "validate_contrast")


def current_kit(pack_id: str = "ux-ui-agent-skills") -> Path:
    release = _current_release(pack_id)
    if release is None:
        raise SkillPackError(f"{pack_id} is not installed on this node; run `terminal-mcp-skill-packs sync`")
    return release / "kit"


def catalogue(kit: Path) -> list[dict[str, Any]]:
    """The gate list, read from the kit's own mcp/gates.mjs -- including which
    gates open a browser, which that file derives from each script's source."""
    node = node_runtime()
    if not node["ok"]:
        raise SkillPackError(f"cannot read the gate catalogue: {node['detail']}")
    proc = _run([node["node"], "-e", _CATALOGUE_JS, (kit / "mcp" / "gates.mjs").as_uri()], timeout=60)
    if proc.returncode != 0:
        raise SkillPackError(f"gate catalogue failed: {proc.stderr.strip()[-400:]}")
    return json.loads(proc.stdout)


def classify(exit_code: int | None, output: str) -> str:
    if exit_code is None:
        return ERROR
    text = output or ""
    if exit_code == 0:
        if text.lstrip().lower().startswith("usage:"):
            return NOT_RUN              # printed its usage and measured nothing
        if _BROWSER_MISSING.search(text) or re.search(r"not installed\W+SKIPPED", text):
            return BLOCKED
        return PASS
    if exit_code == 2:
        return BLOCKED
    if _BROWSER_MISSING.search(text):
        return BLOCKED
    if _CRASH.search(text):
        return ERROR
    if exit_code == 1:
        return FAIL
    return ERROR


def _runner(gate: dict[str, Any]) -> str | None:
    if gate["runner"] == "node":
        node = node_runtime()
        return node["node"] if node["ok"] else None
    return sys.executable or find_executable("python3")


def run_one(kit: Path, gate: dict[str, Any], argv: Sequence[str], *, target: str,
            timeout: int = GATE_TIMEOUT_SECONDS) -> dict[str, Any]:
    runner = _runner(gate)
    base = {"gate": gate["name"], "target": target, "argv": [gate["file"], *argv]}
    if runner is None:
        return {**base, "exit_code": None, "status": BLOCKED, "duration_s": 0.0,
                "output": f"{gate['runner']} runtime not available on this node"}
    env = dict(os.environ)
    env["DS_REQUIRE_BROWSER"] = "1"       # a render gate with no browser must fail, never skip
    started = time.monotonic()
    try:
        proc = subprocess.run([runner, str(kit / "scripts" / gate["file"]), *argv], capture_output=True,
                              text=True, timeout=timeout, env=env)
        code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        code, out = None, f"timed out after {timeout}s\n{exc.stdout or ''}"
    out = out.strip()
    return {**base, "exit_code": code, "status": classify(code, out),
            "duration_s": round(time.monotonic() - started, 2),
            "output": out if len(out) <= MAX_OUTPUT_CHARS else "...\n" + out[-MAX_OUTPUT_CHARS:]}


def _html_targets(paths: Sequence[str]) -> list[str]:
    found: list[str] = []
    for raw in paths:
        path = Path(raw)
        if path.is_file() and path.suffix.lower() in (".html", ".htm"):
            found.append(str(path))
        elif path.is_dir():
            for child in sorted(path.rglob("*.htm*")):
                if "node_modules" in child.parts or not child.is_file():
                    continue
                found.append(str(child))
    unique = list(dict.fromkeys(found))
    return unique[:MAX_HTML_TARGETS]


def plan_review(gates: Sequence[dict[str, Any]], paths: Sequence[str], *, tokens: str | None = None,
                theme: str | None = None, focustrap_open: str | None = None,
                component_root: str | None = None, skip_browser: bool = False,
                dark: bool = False) -> list[dict[str, Any]]:
    """Which argv each gate gets, or why it does not run. Pure.

    Every NOT_RUN carries its reason, so a review can say exactly what it left
    out instead of folding it into a pass count."""
    html = _html_targets(paths)
    steps: list[dict[str, Any]] = []
    for gate in gates:
        name = gate["name"]
        if gate.get("browser"):
            if skip_browser:
                steps.append({"gate": gate, "skip": "browser gates skipped by the caller"})
                continue
            if not html:
                steps.append({"gate": gate, "skip": "no .html file among the paths to render"})
                continue
            if name == "verify_focustrap" and not focustrap_open:
                steps.append({"gate": gate, "skip": "needs --focustrap-open <modal trigger selector>"})
                continue
            for target in html:
                extra = [f"--open={focustrap_open}"] if name == "verify_focustrap" else []
                if dark and name in ("axe_audit", "measure_render", "verify_states"):
                    extra.append("--dark")
                steps.append({"gate": gate, "argv": [target, *extra], "target": target})
        elif name in TOKEN_GATES:
            if not tokens:
                steps.append({"gate": gate, "skip": "needs --tokens <DTCG colors JSON>; without it the gate "
                                                    "checks the kit's own tokens, not the project's"})
            else:
                steps.append({"gate": gate, "argv": [tokens], "target": tokens})
        elif name == "validate_theme_refs":
            if not theme:
                steps.append({"gate": gate, "skip": "needs --theme <theme CSS defining the variables>"})
            else:
                steps.append({"gate": gate, "argv": ["--theme", theme, *paths], "target": ",".join(paths)})
        elif name == "validate_component_spec":
            if not component_root:
                steps.append({"gate": gate, "skip": "needs --component-root <dir containing components/>; "
                                                    "without it the gate checks the kit's own specs"})
            else:
                steps.append({"gate": gate, "argv": ["--root", component_root], "target": component_root})
        else:
            steps.append({"gate": gate, "argv": list(paths), "target": ",".join(paths)})
    return steps


def verdict(per_gate: dict[str, dict[str, Any]], expected: int) -> str:
    statuses = [g["status"] for g in per_gate.values()]
    if FAIL in statuses:
        return FAIL
    if len(statuses) == expected and expected > 0 and all(s == PASS for s in statuses):
        return PASS
    return INCOMPLETE


def review(paths: Sequence[str], *, pack_id: str = "ux-ui-agent-skills", tokens: str | None = None,
           theme: str | None = None, focustrap_open: str | None = None, component_root: str | None = None,
           skip_browser: bool = False, dark: bool = False, record: bool = True) -> dict[str, Any]:
    """Run every catalogued gate that can run on these inputs, and say plainly
    which could not. See the module docstring for the verdict rules."""
    started = time.monotonic()
    missing = [p for p in [*paths, tokens, theme, component_root] if p and not Path(p).exists()]
    if not paths or missing:
        result = {"verdict": INCOMPLETE, "exit_code": EXIT_FOR[INCOMPLETE], "gates": {}, "runs": [],
                  "error": "no paths given" if not paths else f"not found: {', '.join(missing)}",
                  "summary": "Nothing was measured. This is not a pass."}
        return result
    release = _current_release(pack_id)
    if release is None:
        raise SkillPackError(f"{pack_id} is not installed on this node")
    kit = release / "kit"
    gates = catalogue(kit)
    runs: list[dict[str, Any]] = []
    per_gate: dict[str, dict[str, Any]] = {}
    for step in plan_review(gates, paths, tokens=tokens, theme=theme, focustrap_open=focustrap_open,
                            component_root=component_root, skip_browser=skip_browser, dark=dark):
        gate = step["gate"]
        if "skip" in step:
            run = {"gate": gate["name"], "target": None, "argv": None, "exit_code": None,
                   "status": NOT_RUN, "duration_s": 0.0, "output": step["skip"]}
        else:
            run = run_one(kit, gate, step["argv"], target=step["target"])
        runs.append(run)
        entry = per_gate.setdefault(gate["name"], {"status": PASS, "browser": bool(gate.get("browser")),
                                                   "exit_codes": [], "runs": 0})
        entry["runs"] += 1
        entry["exit_codes"].append(run["exit_code"])
        if _SEVERITY[run["status"]] > _SEVERITY[entry["status"]]:
            entry["status"] = run["status"]
        if run["status"] != PASS:
            entry.setdefault("reasons", []).append(run["output"].splitlines()[-1][:300] if run["output"] else "")
    outcome = verdict(per_gate, len(gates))
    counts = {s: sum(1 for g in per_gate.values() if g["status"] == s) for s in (PASS, FAIL, BLOCKED, ERROR, NOT_RUN)}
    summary = (f"{outcome}: {counts[PASS]}/{len(gates)} gates passed, {counts[FAIL]} failed, "
               f"{counts[BLOCKED]} blocked, {counts[ERROR]} errored, {counts[NOT_RUN]} not run")
    result = {"verdict": outcome, "exit_code": EXIT_FOR[outcome], "summary": summary, "counts": counts,
              "catalogue_size": len(gates), "release": release.name, "paths": list(paths),
              "gates": per_gate, "runs": runs, "duration_s": round(time.monotonic() - started, 2),
              "scope": "objective correctness only (tokens, accessibility, drift); says nothing about taste"}
    if record:
        _record_gate(pack_id, {"at": _now(), "kind": "review", "verdict": outcome, "summary": summary,
                               "paths": list(paths)[:5], "release": release.name,
                               "not_passed": {k: v["status"] for k, v in per_gate.items() if v["status"] != PASS}})
    return result


def run_gate(gate_name: str, paths: Sequence[str], extra: Sequence[str] = (), *,
             pack_id: str = "ux-ui-agent-skills", record: bool = True) -> dict[str, Any]:
    missing = [p for p in paths if not Path(p).exists()]
    if not paths or missing:
        return {"gate": gate_name, "status": BLOCKED, "exit_code": None,
                "output": (f"Not found: {', '.join(missing)}" if missing else "no paths given")
                + "\nNothing was measured. This is not a pass."}
    kit = current_kit(pack_id)
    gate = next((g for g in catalogue(kit) if g["name"] == gate_name), None)
    if gate is None:
        return {"gate": gate_name, "status": ERROR, "exit_code": None,
                "output": f"unknown gate {gate_name!r}; run `list` for the catalogue"}
    run = run_one(kit, gate, [*paths, *extra], target=",".join(paths))
    if record:
        _record_gate(pack_id, {"at": _now(), "kind": "gate", "gate": gate_name, "verdict": run["status"],
                               "exit_code": run["exit_code"], "paths": list(paths)[:5]})
    return run


_SELFCHECK_LINE = re.compile(r"^\s+(PASS|FAIL)\s+(.+?)\s*$", re.MULTILINE)
SELFCHECK_TIMEOUT_SECONDS = 1800


def selfcheck(pack_id: str = "ux-ui-agent-skills", *, record: bool = True,
              timeout: int = SELFCHECK_TIMEOUT_SECONDS) -> dict[str, Any]:
    """The kit's own `scripts/accuracy_report.mjs` -- the checks behind the
    README's "52 objective gates" -- run inside the pinned, read-only tree.

    It measures the KIT's examples, not a project: it proves this node can run
    the gates as upstream intends (browser, node, deps), nothing about a page.
    Some checks build into the kit (`dist/`) and need its devDependencies, so
    it runs in a writable scratch copy with `npm ci --ignore-scripts` from the
    kit's own lockfile; the pinned release stays read-only. The verdict is the
    script's own exit code; the per-check lines are parsed only to say which
    checks failed."""
    node = node_runtime()
    if not node["ok"]:
        result = {"verdict": BLOCKED, "exit_code": None, "passed": 0, "total": 0, "failed": [],
                  "output": node["detail"]}
    else:
        kit, deps = _selfcheck_workdir(pack_id)
        env = dict(os.environ)
        env["DS_REQUIRE_BROWSER"] = "1"
        env["CLAUDE_PLUGIN_DATA"] = str(pack_dir(pack_id) / "selfcheck")
        started = time.monotonic()
        try:
            proc = subprocess.run([node["node"], "scripts/accuracy_report.mjs"], cwd=str(kit), capture_output=True,
                                  text=True, timeout=timeout, env=env)
            code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        except subprocess.TimeoutExpired as exc:
            code, out = None, f"timed out after {timeout}s\n{exc.stdout or ''}"
        checks = _SELFCHECK_LINE.findall(out)
        passed = sum(1 for status, _ in checks if status == PASS)
        outcome = PASS if code == 0 and checks and passed == len(checks) else (FAIL if code == 1 else ERROR)
        result = {"verdict": outcome, "exit_code": code, "passed": passed, "total": len(checks),
                  "failed": [name for status, name in checks if status == FAIL], "dev_deps": deps,
                  "duration_s": round(time.monotonic() - started, 2),
                  "output": out.strip() if len(out) <= MAX_OUTPUT_CHARS else "...\n" + out.strip()[-MAX_OUTPUT_CHARS:]}
    result["summary"] = f"{result['verdict']}: {result['passed']}/{result['total']} upstream accuracy checks passed"
    if record:
        with contextlib.suppress(OSError):
            state = load_state(pack_id)
            state["last_selfcheck"] = {"at": _now(), "verdict": result["verdict"], "exit_code": result["exit_code"],
                                       "passed": result["passed"], "total": result["total"],
                                       "failed": result["failed"][:20], "dev_deps": result.get("dev_deps"),
                                       "release": _server_version(pack_id)}
            save_state(pack_id, state)
    return result


def _selfcheck_workdir(pack_id: str) -> tuple[Path, str]:
    """A writable copy of the current kit with its locked devDependencies,
    rebuilt only when the release changes. Returns (kit copy, deps note)."""
    release = _current_release(pack_id)
    if release is None:
        raise SkillPackError(f"{pack_id} is not installed on this node; run `terminal-mcp-skill-packs sync`")
    work = pack_dir(pack_id) / "selfcheck" / release.name
    kit, marker = work / "kit", work / "deps.json"
    note = (_read_json(marker, {}) or {}).get("deps")
    if not kit.is_dir() or not note:
        _remove_tree(work)
        work.mkdir(parents=True)
        shutil.copytree(release / "kit", kit, symlinks=True)
        _set_writable(kit, True)
        npm = find_executable("npm")
        if not npm:
            note = "not installed: npm not found (checks that need esbuild/react will fail)"
        elif not (kit / "package-lock.json").is_file():
            note = "not installed: the kit has no package-lock.json"
        else:
            env = dict(os.environ)
            env["PATH"] = os.pathsep.join([str(Path(npm).parent), env.get("PATH", "")])
            proc = _run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=kit, timeout=900, env=env)
            note = ("npm ci --ignore-scripts from the kit's package-lock.json" if proc.returncode == 0 else
                    f"npm ci failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-300:]}")
        _write_json_atomic(marker, {"deps": note, "release": release.name, "at": _now()})
    return kit, note


def _record_gate(pack_id: str, entry: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        state = load_state(pack_id)
        state["last_gate"] = entry
        save_state(pack_id, state)


def format_review(result: dict[str, Any]) -> str:
    lines = [result.get("summary", ""), ""]
    if result.get("error"):
        lines.append(f"error: {result['error']}")
    for name, entry in sorted(result.get("gates", {}).items()):
        codes = ",".join("-" if c is None else str(c) for c in entry["exit_codes"])
        reason = f"  -- {entry['reasons'][0]}" if entry.get("reasons") else ""
        lines.append(f"  {entry['status']:<8} {name:<26} exit {codes}{reason}")
    failed = [r for r in result.get("runs", []) if r["status"] in (FAIL, ERROR, BLOCKED)]
    if failed:
        lines += ["", "Output of every gate that did not pass, in full:"]
        for run in failed:
            lines += ["", f"--- {run['gate']} on {run['target']} ({run['status']}, exit {run['exit_code']})",
                      run["output"]]
    if result.get("scope"):
        lines += ["", f"Scope: {result['scope']}"]
    return "\n".join(lines)


def screenshot(html: str, out: str, *, width: int = 1280, height: int = 900,
               pack_id: str = "ux-ui-agent-skills") -> dict[str, Any]:
    """A PNG of one page, using the same Playwright the render gates use."""
    release = _current_release(pack_id)
    if release is None:
        raise SkillPackError(f"{pack_id} is not installed on this node")
    node = node_runtime()
    if not node["ok"]:
        raise SkillPackError(node["detail"])
    script = ("const {chromium}=require(process.argv[1]);(async()=>{"
              "const b=await chromium.launch({channel:'chrome'}).catch(()=>chromium.launch());"
              "const p=await b.newPage({viewport:{width:+process.argv[4],height:+process.argv[5]}});"
              "await p.goto('file://'+process.argv[2],{waitUntil:'networkidle'});"
              "await p.screenshot({path:process.argv[3],fullPage:true});await b.close();})()"
              ".catch(e=>{console.error(String(e));process.exit(2)})")
    proc = _run([node["node"], "-e", script, str(release / "node_modules" / "playwright"),
                 str(Path(html).resolve()), str(Path(out).resolve()), str(width), str(height)], timeout=180)
    ok = proc.returncode == 0 and Path(out).is_file()
    return {"ok": ok, "exit_code": proc.returncode, "path": str(Path(out).resolve()) if ok else None,
            "detail": (proc.stderr or proc.stdout)[-400:]}


# ---------------------------------------------------------------------------
# MCP server (stdio JSON-RPC, no SDK -- same reason as upstream: stdlib only)
# ---------------------------------------------------------------------------
MCP_PROTOCOL = "2025-06-18"
_DOCTRINE = {"tokens": ".claude/rules/tokens-and-color.md", "typography": ".claude/rules/typography-and-spacing.md",
             "components": ".claude/rules/components.md", "accessibility": ".claude/rules/accessibility.md",
             "frameworks": ".claude/rules/frameworks.md", "review": ".claude/rules/review-and-research.md",
             "brand": ".claude/rules/brand-and-operations.md", "taste": "taste/design-taste.md",
             "agents": "AGENTS.md"}
_PATHS = {"type": "array", "items": {"type": "string"}, "minItems": 1}
MCP_TOOLS: list[dict[str, Any]] = [
    {"name": "list_gates", "description": "Every gate the pinned kit can run, and which need a browser.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "run_gate",
     "description": ("Run one gate. Returns its real exit code and output, classified PASS (exit 0 and it "
                     "looked), FAIL (1, found a defect), BLOCKED (2 or missing browser/runtime), ERROR or "
                     "NOT_RUN. isError is false only for PASS."),
     "inputSchema": {"type": "object", "properties": {"gate": {"type": "string"}, "paths": _PATHS,
                                                      "args": {"type": "array", "items": {"type": "string"}}},
                     "required": ["gate", "paths"], "additionalProperties": False}},
    {"name": "review_ui",
     "description": ("Run every catalogued gate on the paths. Verdict PASS only when EVERY gate ran and passed; "
                     "FAIL when any gate found a defect; otherwise INCOMPLETE with the reason each gate did not "
                     "run. Supply tokens (DTCG colors JSON), theme (CSS), focustrap_open (modal trigger selector) "
                     "and component_root to let the input-dependent gates run."),
     "inputSchema": {"type": "object", "properties": {
         "paths": _PATHS, "tokens": {"type": "string"}, "theme": {"type": "string"},
         "focustrap_open": {"type": "string"}, "component_root": {"type": "string"},
         "skip_browser": {"type": "boolean"}, "dark": {"type": "boolean"}},
         "required": ["paths"], "additionalProperties": False}},
    {"name": "get_doctrine", "description": f"Read a kit rule. Topics: {', '.join(_DOCTRINE)}.",
     "inputSchema": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"],
                     "additionalProperties": False}},
    {"name": "get_design_system", "description": "Read a named library design system; omit name to list them.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "additionalProperties": False}},
    {"name": "get_tokens", "description": "Read a kit DTCG token file; omit file to list them.",
     "inputSchema": {"type": "object", "properties": {"file": {"type": "string"}}, "additionalProperties": False}},
    {"name": "pack_status", "description": "Installed version, provenance, licence, runtime and last gate result.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
]


def _text(body: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": body}], "isError": is_error}


def _read_kit_file(kit: Path, rel: str) -> str | None:
    path = (kit / rel).resolve()
    if kit.resolve() not in path.parents or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def mcp_call(name: str, args: dict[str, Any], *, pack_id: str = "ux-ui-agent-skills") -> dict[str, Any]:
    if name == "pack_status":
        return _text(json.dumps(status(pack_id, verify=True), indent=2, default=str))
    kit = current_kit(pack_id)
    if name == "list_gates":
        gates = catalogue(kit)
        lines = [f"{len(gates)} gates; {sum(1 for g in gates if g['browser'])} open a real browser."]
        lines += [f"{g['name']}{'  [browser]' if g['browser'] else ''}\n    {g['measures']}" for g in gates]
        return _text("\n".join(lines))
    if name == "run_gate":
        run = run_gate(str(args.get("gate", "")), list(args.get("paths") or []), list(args.get("args") or []),
                       pack_id=pack_id)
        return _text(f"{run['status']}: {run['gate']} exited {run['exit_code']}\n\n{run['output']}",
                     run["status"] != PASS)
    if name == "review_ui":
        result = review(list(args.get("paths") or []), pack_id=pack_id, tokens=args.get("tokens"),
                        theme=args.get("theme"), focustrap_open=args.get("focustrap_open"),
                        component_root=args.get("component_root"), skip_browser=bool(args.get("skip_browser")),
                        dark=bool(args.get("dark")))
        return _text(format_review(result), result["verdict"] != PASS)
    if name == "get_doctrine":
        rel = _DOCTRINE.get(str(args.get("topic", "")).lower())
        body = _read_kit_file(kit, rel) if rel else None
        return _text(body, False) if body is not None else _text(
            f"Unknown topic {args.get('topic')!r}. Available: {', '.join(_DOCTRINE)}.", True)
    if name == "get_design_system":
        library = kit / "design-systems" / "library"
        names = sorted(p.name for p in library.iterdir() if p.is_dir()) if library.is_dir() else []
        if not args.get("name"):
            return _text(f"{len(names)} systems:\n\n{', '.join(names)}")
        wanted = str(args["name"])
        body = _read_kit_file(kit, f"design-systems/library/{wanted}/DESIGN.md") if wanted in names else None
        return _text(body, False) if body is not None else _text(f"No system {wanted!r}.", True)
    if name == "get_tokens":
        files = sorted(p.stem for p in (kit / "tokens").glob("*.json"))
        if not args.get("file"):
            return _text(f"{len(files)} token files:\n\n{', '.join(files)}")
        wanted = str(args["file"]).removesuffix(".json")
        body = _read_kit_file(kit, f"tokens/{wanted}.json") if wanted in files else None
        return _text(body, False) if body is not None else _text(f"No token file {wanted!r}.", True)
    raise KeyError(name)


def mcp_dispatch(message: dict[str, Any], *, pack_id: str = "ux-ui-agent-skills") -> dict[str, Any] | None:
    msg_id, method, params = message.get("id"), message.get("method"), message.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": MCP_PROTOCOL, "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "ux-ui-gates", "title": "Terminal MCP shared UX/UI gates",
                           "version": _server_version(pack_id)},
            "instructions": ("Objective design-system gates from the shared, pinned ux-ui-agent-skills kit. "
                             "review_ui says PASS only when every gate ran and passed; INCOMPLETE and BLOCKED "
                             "are never passes. Report verdicts and exit codes exactly as returned. The "
                             "project's own tokens and branding take precedence over kit references.")}}
    if method and method.startswith("notifications/"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": MCP_TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        if name not in {t["name"] for t in MCP_TOOLS}:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
        try:
            result = mcp_call(name, params.get("arguments") or {}, pack_id=pack_id)
        except Exception as exc:  # noqa: BLE001 - a tool failure is a result, not a protocol error
            result = _text(f"{name} could not run: {exc}", True)
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    if msg_id is None:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def _server_version(pack_id: str) -> str:
    release = _current_release(pack_id)
    return release.name if release else "not-installed"


def serve_mcp(stdin: Any = None, stdout: Any = None, *, pack_id: str = "ux-ui-agent-skills") -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply: dict[str, Any] | None = {"jsonrpc": "2.0", "id": None,
                                            "error": {"code": -32700, "message": "Parse error"}}
        else:
            reply = mcp_dispatch(message, pack_id=pack_id) if isinstance(message, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


def mcp_config(host: str, manifest: Manifest) -> str:
    if host == "codex":
        return codex_mcp_block(manifest)
    return f"claude mcp add --scope user {manifest.mcp_server_name} -- {launcher_path()} mcp"


# ---------------------------------------------------------------------------
# Lazy activation: is this task UI work at all?
# ---------------------------------------------------------------------------
_UI_WORDS = re.compile(
    r"\b(ui|ux|frontend|front-end|css|tailwind|html|design[- ]system|design tokens?|theme|"
    r"ui components?|layout|responsive|accessib\w*|a11y|wcag|colou?r contrast|landing page|web ?page|"
    r"figma|mockup|typography|dark mode|button|modal|navbar|stylesheet)\b", re.IGNORECASE)


def activation(task: str, pack_id: str = "ux-ui-agent-skills") -> dict[str, Any]:
    """Should a session for this task be pointed at the pack? Pure apart from
    reading the manifest. Unrelated tasks get `activate: False` and no text."""
    hits = sorted({m.group(0).lower() for m in _UI_WORDS.finditer(task or "")})
    if not hits:
        return {"activate": False, "reason": "no UI/design terms in the task"}
    try:
        manifest = load_manifest(pack_id)
    except SkillPackError as exc:
        return {"activate": False, "reason": str(exc)}
    return {"activate": True, "matched": hits[:8], "pack": manifest.id, "version": manifest.version,
            "router_skill": manifest.router_skill, "gate_command": f"{launcher_path()} review <paths>",
            "note": "the project's tokens, branding and AGENTS.md/CLAUDE.md take precedence over the kit"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="terminal-mcp-skill-packs",
                                     description="Shared, pinned skill packs (docs/UX_UI_SKILL_PACK.md)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_sync = sub.add_parser("sync", help="install/verify every manifest pack and wire it into Claude/Codex")
    p_sync.add_argument("packs", nargs="*")
    p_sync.add_argument("--no-browser", action="store_true", help="do not download Playwright Chromium")
    p_sync.add_argument("--codex-mcp", action="store_true", help="also register the MCP server for Codex")
    p_sync.add_argument("--claude-mcp", action="store_true",
                        help="also register the MCP server for Claude Code (user scope, opt-in)")
    p_sync.add_argument("--best-effort", action="store_true", help="always exit 0 (node join must not fail)")
    p_sync.add_argument("--force", action="store_true", help="move past a rollback pin to the manifest's release")
    p_status = sub.add_parser("status")
    p_status.add_argument("pack", nargs="?", default="ux-ui-agent-skills")
    p_status.add_argument("--no-verify", action="store_true")
    for name in ("rollback", "disable", "enable"):
        sub.add_parser(name).add_argument("pack", nargs="?", default="ux-ui-agent-skills")
    sub.add_parser("list", help="the gate catalogue")
    p_review = sub.add_parser("review", help="run every gate; exit 0 PASS, 1 FAIL, 2 INCOMPLETE")
    p_review.add_argument("paths", nargs="+")
    p_review.add_argument("--tokens")
    p_review.add_argument("--theme")
    p_review.add_argument("--focustrap-open")
    p_review.add_argument("--component-root")
    p_review.add_argument("--skip-browser", action="store_true")
    p_review.add_argument("--dark", action="store_true")
    p_review.add_argument("--json", action="store_true")
    p_gate = sub.add_parser("gate", help="run one gate")
    p_gate.add_argument("gate")
    p_gate.add_argument("paths", nargs="+")
    sub.add_parser("selfcheck", help="upstream accuracy_report (the README's 52 checks) on the pinned kit")
    p_shot = sub.add_parser("screenshot")
    p_shot.add_argument("html")
    p_shot.add_argument("out")
    p_shot.add_argument("--width", type=int, default=1280)
    p_shot.add_argument("--height", type=int, default=900)
    sub.add_parser("mcp", help="serve the gates over MCP stdio")
    p_cfg = sub.add_parser("mcp-config", help="print the MCP registration for a host")
    p_cfg.add_argument("host", choices=("claude", "codex"))
    p_act = sub.add_parser("activation", help="would a task activate the pack?")
    p_act.add_argument("task")
    args, unknown = parser.parse_known_args(argv)
    if unknown and args.cmd != "gate":
        parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    try:
        if args.cmd == "sync":
            try:
                report = sync(args.packs or None, install_browser=not args.no_browser,
                              codex_mcp=True if args.codex_mcp else None,
                              claude_mcp=True if args.claude_mcp else None, force=args.force)
            except Exception as exc:  # noqa: BLE001
                if not args.best_effort:
                    raise
                report = {"ok": False, "error": str(exc)}
            _print(report)
            return 0 if (report.get("ok") or args.best_effort) else 1
        if args.cmd == "status":
            result = status(args.pack, verify=not args.no_verify)
            _print(result)
            return 0 if result["state"] in (READY, DEGRADED, DISABLED) else 1
        if args.cmd == "rollback":
            _print(rollback(args.pack))
            return 0
        if args.cmd in ("disable", "enable"):
            _print(set_enabled(args.pack, args.cmd == "enable"))
            return 0
        if args.cmd == "list":
            gates = catalogue(current_kit())
            for gate in gates:
                print(f"{gate['name']:<26}{'browser' if gate['browser'] else 'file':<9}{gate['measures']}")
            print(f"\n{len(gates)} gates, {sum(1 for g in gates if g['browser'])} need a browser")
            return 0
        if args.cmd == "review":
            result = review(args.paths, tokens=args.tokens, theme=args.theme, focustrap_open=args.focustrap_open,
                            component_root=args.component_root, skip_browser=args.skip_browser, dark=args.dark)
            if args.json:
                _print(result)
            else:
                print(format_review(result))
            return result["exit_code"]
        if args.cmd == "gate":
            run = run_gate(args.gate, args.paths, unknown)
            print(f"{run['status']}: {run['gate']} exited {run['exit_code']}\n\n{run['output']}")
            return {PASS: 0, FAIL: 1}.get(run["status"], 2)
        if args.cmd == "selfcheck":
            result = selfcheck()
            print(result["output"])
            print(f"\n{result['summary']} (exit {result['exit_code']})")
            if result["failed"]:
                print("failed: " + "; ".join(result["failed"]))
            return {PASS: 0, FAIL: 1}.get(result["verdict"], 2)
        if args.cmd == "screenshot":
            result = screenshot(args.html, args.out, width=args.width, height=args.height)
            _print(result)
            return 0 if result["ok"] else 2
        if args.cmd == "mcp":
            serve_mcp()
            return 0
        if args.cmd == "mcp-config":
            print(mcp_config(args.host, load_manifest("ux-ui-agent-skills")))
            return 0
        if args.cmd == "activation":
            _print(activation(args.task))
            return 0
    except SkillPackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
