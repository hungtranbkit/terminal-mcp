"""Scoped owner-authorized CLI launch policy and project trust helpers.

The policy changes only agent CLI flags inside the already authorized user
process. It never changes OS privileges, grants MCP input rights, or trusts an
entire workspace directory. Every launch is checked against an exact
configured project repository and the requested working directory.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .backlog_store import file_lock


class LaunchPolicyError(ValueError):
    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f"{code}: {detail}")


@dataclass(frozen=True)
class ProjectDirectory:
    path: Path
    repo_root: Path
    common_dir: Path
    canonical_repo_root: Path


def _inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def _git(cwd: str, flag: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", cwd, "rev-parse", flag],
                                capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _owned_by_current_user(path: Path) -> bool:
    """Fail closed unless the project and its common Git dir belong to us."""
    try:
        st = path.stat()
    except OSError:
        return False
    uid = getattr(os, "getuid", None)
    if uid is not None:
        return st.st_uid == uid()
    # Windows ownership is verified through the current user's PowerShell ACL
    # provider. The path is passed as an argv value, never shell-interpolated.
    expected = "\\".join(part for part in (os.environ.get("USERDOMAIN"),
                                           os.environ.get("USERNAME")) if part)
    if not expected:
        return False
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-Acl -LiteralPath $args[0]).Owner", str(path)],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip().casefold() == expected.casefold()


def validate_project_directory(requested: str | os.PathLike[str], *,
                               allowed_roots: tuple[str, ...] | list[str],
                               trusted_project_roots: tuple[str, ...] | list[str]) -> ProjectDirectory:
    """Validate one exact repo/worktree cwd and its canonical Git mapping."""
    raw = Path(requested).expanduser()
    if not raw.is_absolute():
        raise LaunchPolicyError("CWD_MUST_BE_ABSOLUTE")
    try:
        path = raw.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise LaunchPolicyError("CWD_NOT_FOUND") from exc
    if not path.is_dir():
        raise LaunchPolicyError("CWD_NOT_FOUND")
    if not allowed_roots:
        raise LaunchPolicyError("ALLOWED_PROJECT_ROOTS_NOT_CONFIGURED")
    allowed = tuple(Path(root).expanduser().resolve() for root in allowed_roots)
    if not any(_inside(path, root) for root in allowed):
        raise LaunchPolicyError("CWD_NOT_ALLOWED")
    if not trusted_project_roots:
        raise LaunchPolicyError("TRUSTED_PROJECT_ROOTS_NOT_CONFIGURED")
    if not _owned_by_current_user(path):
        raise LaunchPolicyError("PROJECT_NOT_OWNED_BY_OPERATOR")

    top_text = _git(str(path), "--show-toplevel")
    common_text = _git(str(path), "--git-common-dir")
    if not top_text or not common_text:
        raise LaunchPolicyError("CWD_NOT_A_GIT_PROJECT")
    repo_root = Path(top_text).resolve()
    common_dir = Path(common_text)
    if not common_dir.is_absolute():
        common_dir = (repo_root / common_dir).resolve()
    else:
        common_dir = common_dir.resolve()
    # A normal non-bare repository stores its shared common dir in `<root>/.git`.
    # Git intentionally refuses `rev-parse --show-toplevel` when cwd is the
    # `.git` directory itself, so map this verified Git output directly.
    if common_dir.name != ".git":
        raise LaunchPolicyError("GIT_COMMON_DIR_UNVERIFIED")
    canonical_root = common_dir.parent.resolve()
    trusted = tuple(Path(root).expanduser().resolve() for root in trusted_project_roots)
    if canonical_root not in trusted:
        raise LaunchPolicyError("PROJECT_ROOT_NOT_TRUSTED")
    if not any(_inside(repo_root, root) for root in allowed):
        raise LaunchPolicyError("WORKTREE_ROOT_NOT_ALLOWED")
    if not _owned_by_current_user(common_dir):
        raise LaunchPolicyError("PROJECT_NOT_OWNED_BY_OPERATOR")
    return ProjectDirectory(path, repo_root, common_dir, canonical_root)


def build_permission_args(agent_type: str, permission_mode: str, launcher: str, *,
                          probe: Callable[..., Any] = subprocess.run) -> tuple[str, ...]:
    """Return the requested CLI mode only after the installed binary accepts it.

    ``native`` is the explicit per-session opt-out. The probe uses only
    ``--version`` and never starts an agent turn or prints environment data.
    """
    if permission_mode not in {"owner", "native"}:
        raise LaunchPolicyError("INVALID_PERMISSION_MODE")
    if permission_mode == "native" or agent_type == "shell":
        return ()
    flag = {"claude": "--dangerously-skip-permissions", "codex": "--yolo"}.get(agent_type)
    if not flag:
        return ()
    try:
        result = probe([launcher, flag, "--version"], capture_output=True, text=True,
                       timeout=8, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LaunchPolicyError("PERMISSION_MODE_PREFLIGHT_FAILED", type(exc).__name__) from exc
    if result.returncode != 0:
        # Do not include arbitrary CLI output in the API receipt.
        raise LaunchPolicyError("PERMISSION_MODE_UNSUPPORTED", agent_type)
    return (flag,)


def register_claude_project_trust(path: str | os.PathLike[str], *,
                                  config_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Persist Claude's supported exact-project trust bit, preserving config."""
    project = Path(path).expanduser().resolve(strict=True)
    if not project.is_dir() or not (project / ".git").exists():
        raise LaunchPolicyError("CLAUDE_TRUST_REQUIRES_GIT_PROJECT")
    target = (Path(config_path) if config_path is not None else
              Path(os.environ.get("CLAUDE_CONFIG_PATH", Path.home() / ".claude.json")).expanduser())
    target.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(target):
        if target.exists():
            try:
                current = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                raise LaunchPolicyError("CLAUDE_CONFIG_UNREADABLE") from exc
            if not isinstance(current, dict):
                raise LaunchPolicyError("CLAUDE_CONFIG_UNREADABLE")
        else:
            current = {}
        projects = current.get("projects", {})
        if not isinstance(projects, dict):
            raise LaunchPolicyError("CLAUDE_CONFIG_UNREADABLE")
        entry = projects.get(str(project), {})
        if not isinstance(entry, dict):
            raise LaunchPolicyError("CLAUDE_CONFIG_UNREADABLE")
        if entry.get("hasTrustDialogAccepted") is True:
            return {"registered": True, "already": True, "path": str(project)}
        entry["hasTrustDialogAccepted"] = True
        projects[str(project)] = entry
        current["projects"] = projects
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                         dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(current, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
    return {"registered": True, "already": False, "path": str(project)}


def register_codex_project_trust(path: str | os.PathLike[str], *,
                                config_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Append Codex's supported per-project trust setting without rewriting config."""
    project = Path(path).expanduser().resolve(strict=True)
    if not project.is_dir() or not (project / ".git").exists():
        raise LaunchPolicyError("CODEX_TRUST_REQUIRES_GIT_PROJECT")
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    target = Path(config_path) if config_path is not None else codex_home / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    section = f"projects.{json.dumps(str(project))}"
    with file_lock(target):
        try:
            original = target.read_text(encoding="utf-8") if target.exists() else ""
            parsed = tomllib.loads(original) if original.strip() else {}
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise LaunchPolicyError("CODEX_CONFIG_UNREADABLE") from exc
        projects = parsed.get("projects", {})
        if not isinstance(projects, dict):
            raise LaunchPolicyError("CODEX_CONFIG_UNREADABLE")
        existing = projects.get(str(project))
        if isinstance(existing, dict):
            if existing.get("trust_level") == "trusted":
                return {"registered": True, "already": True, "path": str(project)}
            # An explicit prior choice remains authoritative; do not silently
            # turn an untrusted project into a trusted one.
            raise LaunchPolicyError("CODEX_PROJECT_EXPLICITLY_UNTRUSTED")
        if not isinstance(existing, (dict, type(None))):
            raise LaunchPolicyError("CODEX_CONFIG_UNREADABLE")
        line = f"[{section}]\ntrust_level = \"trusted\"\n"
        updated = original.rstrip() + ("\n\n" if original.strip() else "") + line
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                         dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(updated)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise
    return {"registered": True, "already": False, "path": str(project)}
