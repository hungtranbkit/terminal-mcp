"""Canonical project identity -- "which PROJECT is this?", answered the
same way from any session, any node, any cwd inside the repo.

Why this exists (audit, 2026-09-09): the ONLY notion of "project" this
codebase had was `queue_lanes.project` -- a free-text label an operator
sets per session lane via terminal_task_set_project. Two sessions on the
same repo could label themselves differently (or not at all), so nothing
could reliably answer "show me everything for THIS project". A backlog
that is supposed to be shared by every session of a project cannot be
keyed on that.

The rule: identity comes from the REPOSITORY, never from the cwd string.
`/repo`, `/repo/sub/dir`, and a second checkout of the same remote all
resolve to the SAME project_id, while two unrelated repos never collide.

Precedence, most durable first:
  1. `git remote get-url origin`, normalised -- survives being cloned to
     a different path, on a different machine, by a different user. This
     is what makes the id stable across nodes.
  2. PROJECT.yaml's own `project.code`, when present and there is no
     remote -- this project's own existing convention (see PROJECT.yaml
     in this repo), so a repo that deliberately declares its identity is
     honoured before falling back to a path.
  3. The real (symlink-resolved) repo root path -- correct but LOCAL:
     the same repo cloned elsewhere gets a different id. Documented as
     the weakest tier rather than silently pretended to be portable.

Deliberately reuses session_registry.probe_project_info for the git
introspection instead of shelling out to git a second way -- that helper
is already the one place in this project that reads repo_root/git_remote
/git_branch/last_commit, is already read-only and never-raises, and is
already what session records are populated from.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .session_registry import probe_project_info

# A git remote can be written many ways for the SAME repository:
#   https://github.com/o/r.git   git@github.com:o/r.git
#   ssh://git@github.com/o/r     https://user:tok@github.com/o/r.git
# All must normalise to "github.com/o/r", or two sessions on one repo
# would get two different project ids purely from clone-URL style.
_SCP_LIKE = re.compile(r"^(?P<user>[^@/]+@)?(?P<host>[^:/]+):(?P<path>.+)$")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def normalise_git_remote(remote: str | None) -> str | None:
    """URL -> "host/path/without/dot-git", or None if unusable. Never
    raises on malformed input -- an unparseable remote simply falls
    through to the next identity tier rather than breaking identity
    entirely."""
    if not remote:
        return None
    value = remote.strip()
    if not value:
        return None
    if _SCHEME.match(value):
        # Strip scheme, then any credentials -- a token embedded in a
        # remote URL must NEVER end up inside a project_id (that id is
        # written into a file that gets committed).
        value = _SCHEME.sub("", value, count=1)
        value = value.split("@", 1)[-1]
    else:
        match = _SCP_LIKE.match(value)
        if match:
            value = f"{match.group('host')}/{match.group('path')}"
    value = value.strip("/")
    if value.endswith(".git"):
        value = value[: -len(".git")]
    value = value.rstrip("/")
    if not value:
        return None
    # Host is case-insensitive; the path after it is not (GitHub is
    # case-preserving and two repos differing only in case are distinct).
    parts = value.split("/", 1)
    parts[0] = parts[0].casefold()
    return "/".join(parts)


def _project_yaml_code(repo_root: str) -> str | None:
    """`project.code` from a PROJECT.yaml at the repo root, if any.
    Parsed with a tiny, dependency-free scan rather than yaml.safe_load:
    identity resolution runs on every backlog call and must never fail
    or slow down because a PROJECT.yaml elsewhere in the file has
    unrelated syntax this function does not care about."""
    path = Path(repo_root) / "PROJECT.yaml"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    in_project = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw[:1].isspace():
            in_project = raw.split(":", 1)[0].strip() == "project"
            continue
        if in_project:
            key, _, value = raw.strip().partition(":")
            if key.strip() == "code":
                code = value.strip().strip("\"'")
                return code or None
    return None


@dataclass(frozen=True)
class ProjectIdentity:
    """`project_id` is the join key everything else uses. `source` says
    WHICH tier produced it, so a caller (and a human reading a backlog
    file) can tell a portable remote-derived id from a machine-local
    path-derived one instead of having to guess."""

    project_id: str
    source: str  # git_remote | project_yaml | path
    repo_root: str | None
    name: str
    git_remote: str | None = None
    git_branch: str | None = None
    is_portable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id, "source": self.source, "repo_root": self.repo_root,
            "name": self.name, "git_remote": self.git_remote, "git_branch": self.git_branch,
            "is_portable": self.is_portable,
        }


def resolve_project(cwd: str | None) -> ProjectIdentity | None:
    """cwd (anywhere inside a repo) -> that repo's canonical identity, or
    None when `cwd` is not inside a git repo at all. Returning None
    rather than inventing a path-based id for a non-repo directory is
    deliberate: a backlog belongs to a PROJECT, and "some directory" is
    not one -- the caller gets a clear NOT_A_PROJECT error instead of a
    backlog silently created somewhere meaningless."""
    info = probe_project_info(cwd)
    repo_root = info.get("repo_root")
    if not repo_root:
        return None
    try:
        repo_root = str(Path(repo_root).resolve())
    except (OSError, RuntimeError, ValueError):
        pass
    name = Path(repo_root).name or repo_root
    remote = info.get("git_remote")
    branch = info.get("git_branch")

    normalised = normalise_git_remote(remote)
    if normalised:
        return ProjectIdentity(project_id=f"git:{normalised}", source="git_remote", repo_root=repo_root,
                               name=name, git_remote=remote, git_branch=branch, is_portable=True)
    code = _project_yaml_code(repo_root)
    if code:
        return ProjectIdentity(project_id=f"code:{code}", source="project_yaml", repo_root=repo_root,
                               name=code, git_remote=None, git_branch=branch, is_portable=True)
    return ProjectIdentity(project_id=f"path:{repo_root}", source="path", repo_root=repo_root,
                           name=name, git_remote=None, git_branch=branch, is_portable=False)
