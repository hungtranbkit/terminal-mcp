# Validator-only reuse from #112 commit 3db10723891e3b76ced306a0dd5d05053a0047a5.
# Trust registration and unrestricted permission helpers are deliberately excluded.
"""Scoped owner-authorized CLI launch policy and project trust helpers.

The policy changes only agent CLI flags inside the already authorized user
process. It never changes OS privileges, grants MCP input rights, or trusts an
entire workspace directory. Every launch is checked against an exact
configured project repository and the requested working directory.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path



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

