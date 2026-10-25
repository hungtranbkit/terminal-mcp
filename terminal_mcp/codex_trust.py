"""Prelaunch Codex folder trust for operator-approved managed workspaces only.

THE HANG THIS REMOVES

Codex asks "Trust this folder?" the first time it opens a git repository, and
keys the answer by the REPOSITORY ROOT: measured on codex-cli 0.162, a fresh
repo under an already-trusted ~/workspace still prompts, and a linked worktree
prompts with "Trusting will apply to the repository root: <main checkout>".
Every fresh managed repo/worktree therefore opened on a modal that no
dispatcher may answer, and the session sat there.

WHAT THIS IS NOT

It is not a prompt answerer: it never reads a pane or sends a key. It is not
sandbox/command approval either -- `approval_policy`/`sandbox_mode` are never
read or written, so a session keeps asking for command approval exactly as its
own config says. It writes one `trust_level = "trusted"` entry, before launch,
and only when ALL of these hold:

  1. `session_lifecycle.codex_trust_roots` lists the root (default: none, so
     the feature is off until an operator opts a managed workspace in);
  2. the root is a real directory owned by this user, not a symlink, not
     world writable, group writable only by the user's own private group, and
     is neither "/" nor the user's home;
  3. the launch cwd and the repository root Codex will key on both resolve,
     symlinks followed, STRICTLY under that root, with no symlink anywhere in
     the path (a link pointing out of the root cannot smuggle a path in);
  4. the cwd is inside a git checkout owned by this user (non-git paths are
     covered by Codex's own ancestor trust and get no entry).

An existing entry is never changed: "trusted" is a no-op, anything else (an
owner's explicit "untrusted") is refused. The config file is updated under an
advisory lock, re-read immediately before an atomic replace, and refused if it
is a symlink or does not parse -- unrelated settings are preserved byte for
byte because the new table is appended to the existing text.
"""
from __future__ import annotations

import grp
import json
import os
import pwd
import stat
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Iterable

from .backlog_store import file_lock

REGISTERED = "REGISTERED"
ALREADY_TRUSTED = "ALREADY_TRUSTED"
REFUSED = "REFUSED"
NOT_CONFIGURED = "NOT_CONFIGURED"
NOT_A_REPO = "NOT_A_REPO"


def service_home() -> Path:
    """The node service user's real home, not whatever $HOME says."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def codex_config_path(env: dict[str, str] | None = None) -> Path:
    """The config.toml a Codex launched by this process reads."""
    value = (os.environ if env is None else env).get("CODEX_HOME")
    return (Path(value).expanduser() if value else service_home() / ".codex") / "config.toml"


def _strictly_under(path: Path, root: Path) -> bool:
    return path != root and root in path.parents


def _no_symlink_below(root: Path, path: Path) -> bool:
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def _safe_root(raw: str) -> Path | None:
    root = Path(raw)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        return None
    resolved = root.resolve()
    if resolved != Path(os.path.normpath(root)) or resolved in (Path("/"), service_home().resolve()):
        return None
    info = resolved.stat()
    if info.st_uid != os.getuid() or info.st_mode & stat.S_IWOTH:
        return None
    if info.st_mode & stat.S_IWGRP and not _private_group(info.st_gid):
        return None
    return resolved


def _private_group(gid: int) -> bool:
    """Ubuntu's umask 002 makes ~/workspace group-writable by the user's own
    private group; that grants nobody else anything. Any shared group does."""
    user = pwd.getpwuid(os.getuid())
    try:
        group = grp.getgrgid(gid)
    except KeyError:
        return False
    return gid == user.pw_gid and group.gr_name == user.pw_name and set(group.gr_mem) <= {user.pw_name}


def _inside(root: Path, raw_cwd: Path, real_cwd: Path, key: Path) -> bool:
    """Both the cwd as requested and the trust key sit strictly under `root`,
    and neither reaches it through a symlink."""
    requested = Path(os.path.normpath(raw_cwd))
    return (key.resolve() == key
            and _strictly_under(key, root) and _strictly_under(real_cwd, root)
            and requested.is_relative_to(root) and _no_symlink_below(root, requested)
            and _no_symlink_below(root, key))


def trust_key(cwd: str | os.PathLike[str]) -> Path | None:
    """The repository root Codex keys folder trust on, or None outside git."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--path-format=absolute",
             "--show-toplevel", "--git-common-dir"],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 2:
        return None
    common = Path(lines[1])
    # A linked worktree shares <main>/.git; Codex trusts <main>. Anything
    # else (bare repo, separate git dir) has no well-defined root: no entry.
    return common.parent if common.name == ".git" else None


def _decision(status: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, **{k: v for k, v in extra.items() if v is not None}}


def ensure_trust(cwd: str | os.PathLike[str], roots: Iterable[str], *,
                 config_path: Path | None = None) -> dict[str, Any]:
    """Register Codex trust for `cwd`'s repository if it is a managed workspace."""
    roots = tuple(roots)
    if not roots:
        return _decision(NOT_CONFIGURED, "session_lifecycle.codex_trust_roots is empty")
    raw = Path(cwd)
    if not raw.is_absolute() or not raw.is_dir():
        return _decision(REFUSED, "cwd is not an existing absolute directory", cwd=str(cwd))
    real_cwd = raw.resolve()
    key = trust_key(real_cwd)
    if key is None:
        return _decision(NOT_A_REPO, "cwd is not inside a git checkout; no trust entry is needed",
                         cwd=str(real_cwd))
    for root in filter(None, (_safe_root(r) for r in roots)):
        if _inside(root, raw, real_cwd, key):
            if key.stat().st_uid != os.getuid():
                return _decision(REFUSED, "repository root is not owned by the node service user",
                                 path=str(key))
            return _write(key, root, config_path or codex_config_path())
    return _decision(REFUSED, "repository root is not strictly inside an approved managed "
                     "workspace root (or a symlink escapes it)", path=str(key),
                     cwd=str(real_cwd))


def _write(key: Path, root: Path, config: Path) -> dict[str, Any]:
    if config.is_symlink():
        return _decision(REFUSED, "Codex config is a symlink; refusing to replace it",
                         path=str(key), config=str(config))
    with file_lock(config):
        for _attempt in range(3):
            before = config.read_text(encoding="utf-8") if config.is_file() else ""
            try:
                parsed = tomllib.loads(before) if before else {}
            except tomllib.TOMLDecodeError as exc:
                return _decision(REFUSED, f"Codex config does not parse ({exc}); not modified",
                                 path=str(key), config=str(config))
            projects = parsed.get("projects", {})
            existing = projects.get(str(key)) if isinstance(projects, dict) else None
            if existing is not None:
                if isinstance(existing, dict) and existing.get("trust_level") == "trusted":
                    return _decision(ALREADY_TRUSTED, "repository already trusted",
                                     path=str(key), root=str(root), config=str(config))
                return _decision(REFUSED, "an existing Codex project entry is not 'trusted'; "
                                 "owner decision preserved", path=str(key), config=str(config))
            revised = (before.rstrip("\n") + "\n\n" if before.strip() else "") + (
                f"[projects.{json.dumps(str(key))}]\ntrust_level = \"trusted\"\n")
            check = tomllib.loads(revised)
            check["projects"].pop(str(key))
            if not check["projects"] and "projects" not in parsed:
                check.pop("projects")
            if check != parsed:
                return _decision(REFUSED, "appending trust would change other settings; not modified",
                                 path=str(key), config=str(config))
            config.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            mode = stat.S_IMODE(config.stat().st_mode) if config.exists() else 0o600
            fd, tmp_name = tempfile.mkstemp(prefix=".config.toml.tmcp-", dir=config.parent)
            temporary = Path(tmp_name)
            try:
                os.fchmod(fd, mode)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(revised)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Codex itself does not take our lock: re-read just before the
                # swap and start over if it rewrote the file meanwhile.
                current = config.read_text(encoding="utf-8") if config.is_file() else ""
                if current != before:
                    continue
                os.replace(temporary, config)
            finally:
                temporary.unlink(missing_ok=True)
            return _decision(REGISTERED, "managed workspace repository trusted before launch",
                             path=str(key), root=str(root), config=str(config))
    return _decision(REFUSED, "Codex config kept changing during the update; not modified",
                     path=str(key), config=str(config))
