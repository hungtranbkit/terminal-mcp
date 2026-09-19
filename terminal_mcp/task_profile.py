"""What a task NEEDS, derived before anything decides where to run it.

WHY THIS IS METADATA-FIRST AND DETERMINISTIC

The production failure this whole feature exists to remove
(task f807b3fb…, CANCELLED after sitting QUEUED while a compatible session
sat IDLE) was not a bad decision. It was the absence of one: nothing in the
system ever asked "what does this task need, and which runtime satisfies
that". Adding an LLM call to answer that question would have made the answer
slower, non-reproducible, and unavailable exactly when the model endpoint is
the thing that is down -- which is a common reason for a queue to back up in
the first place.

So the profile is computed from data the caller already supplied, in a fixed
order, with every field recording HOW it was derived:

  1. explicit metadata      -- the caller said so
  2. the task's project_id  -- the durable column, already populated
  3. prompt heuristics      -- narrow, conservative, and always last

A heuristic never overrides an explicit value, and a field nothing could
establish stays None. None means "unconstrained" everywhere downstream: an
unknown repo must not be scored as a MISMATCHED repo, because "we don't know"
and "it's the wrong one" lead to opposite decisions.

RISK FLAGS ARE NOT RE-DERIVED HERE. task_classifier.py already owns the
question "what makes a change dangerous" and has the exclusion list to prove
it, in two languages. This module calls it rather than growing a second,
inevitably-diverging copy.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import task_classifier
from .project_identity import normalise_git_remote

#: Runtimes a task can ask for. These are `agent_type` values the rest of the
#: system already uses (controller.terminal_create_session, session records) --
#: never a parallel vocabulary.
KNOWN_RUNTIMES = ("claude", "codex", "shell", "gemini", "cursor")

#: A task that names no runtime runs on anything. Deliberately NOT defaulted to
#: "shell": defaulting would turn "no preference" into a hard constraint and
#: reject every agent session in the fleet.
ANY_RUNTIME = None

TASK_TYPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Ordered: the first match wins, so the more specific intents come first.
    # Vietnamese terms are included because this fleet is driven in both
    # languages -- same posture as task_classifier's own exclusion list.
    ("deploy", re.compile(r"(?i)\b(deploy|rollout|release|ship to prod|triển khai|phát hành)\b")),
    ("review", re.compile(r"(?i)\b(review|audit|code review|rà soát|kiểm tra lại)\b")),
    ("test", re.compile(r"(?i)\b(test|pytest|unit test|regression|kiểm thử)\b")),
    ("bugfix", re.compile(r"(?i)\b(fix|bug|broken|regression|crash|lỗi|sửa|hỏng)\b")),
    ("refactor", re.compile(r"(?i)\b(refactor|clean ?up|tidy|simplify|dọn dẹp|tái cấu trúc)\b")),
    ("docs", re.compile(r"(?i)\b(doc|docs|documentation|readme|tài liệu)\b")),
    ("feature", re.compile(r"(?i)\b(add|implement|build|create|feature|xây dựng|thêm|triển khai tính năng)\b")),
)

#: Prompt signals for a runtime preference. Narrow on purpose -- a prompt that
#: merely MENTIONS claude is not a prompt that requires it, so this only fires
#: on phrasing that names the runtime as the thing to run in.
RUNTIME_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("claude", re.compile(r"(?i)\b(?:in|using|with|via|run)\s+claude(?:\s+code)?\b")),
    ("codex", re.compile(r"(?i)\b(?:in|using|with|via|run)\s+codex\b")),
    ("shell", re.compile(r"(?i)\b(?:in|using|with|via|run)\s+(?:a\s+)?(?:plain\s+)?shell\b")),
)

_PATH_LIKE = re.compile(r"(?<![\w/])(/[\w.@+-]+(?:/[\w.@+-]+)+)")
_BRANCH_LIKE = re.compile(
    r"(?i)\b(?:branch|nhánh)\s+[\"'`]?((?:feat|fix|hotfix|chore|feature|release|docs|test|p0|refactor)"
    r"/[\w./-]+)[\"'`]?")

#: Skill vocabulary keys a caller may use for the same idea. Normalised so
#: "required_skills", "skills" and "capabilities" all land in one place instead
#: of each caller's spelling becoming its own silent no-match.
_REQUIRED_SKILL_KEYS = ("required_skills", "requires_skills", "skills_required")
_OPTIONAL_SKILL_KEYS = ("optional_skills", "preferred_skills", "skills_optional")
_SKILL_KEYS = ("skill_ids", "skills")


def _as_str(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _as_tuple(value: Any) -> tuple[str, ...]:
    """A list-ish metadata field, normalised. A bare string is one item, not a
    character sequence -- the alternative silently turns "lint" into five
    single-letter skills."""
    if value is None:
        return ()
    if isinstance(value, str):
        items: Sequence[Any] = [part for part in re.split(r"[,\s]+", value) if part]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        return ()
    seen: list[str] = []
    for item in items:
        text = _as_str(item)
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


def _first(metadata: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if metadata.get(key) not in (None, "", [], ()):
            return metadata[key]
    return None


@dataclass(frozen=True)
class TaskProfile:
    """What the task needs. Every field may be None/empty -- see module doc:
    unknown is a real answer and must not be confused with a constraint."""

    task_id: str | None = None
    project: str | None = None
    project_id: str | None = None
    repo: str | None = None
    """Normalised git remote when one is known, else the repo root path. The
    normalised remote is preferred because it is the only repo identity that
    survives the same repository being checked out at different paths on
    different nodes -- which is the normal case across this fleet."""
    workspace: str | None = None
    """The worktree/cwd the work belongs in, when the caller named one."""
    branch: str | None = None
    task_type: str | None = None
    capabilities: tuple[str, ...] = ()
    required_skills: tuple[str, ...] = ()
    optional_skills: tuple[str, ...] = ()
    preferred_runtime: str | None = ANY_RUNTIME
    runtime_required: bool = False
    """True only when the caller made the runtime a CONSTRAINT (explicit
    metadata), not merely a prompt hint. A hint influences the score; a
    constraint rejects. Conflating them is how a heuristic silently becomes a
    hard gate."""
    agent_id: str | None = None
    skill_ids: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    requires_approval: bool = False
    evidence: dict[str, str] = field(default_factory=dict)
    """field name -> how it was derived ("metadata", "column", "prompt"). The
    dashboard shows this so a surprising routing decision can be traced back
    to the input that caused it rather than to the router's reputation."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "project": self.project, "project_id": self.project_id,
            "repo": self.repo, "workspace": self.workspace, "branch": self.branch,
            "task_type": self.task_type, "capabilities": list(self.capabilities),
            "required_skills": list(self.required_skills),
            "optional_skills": list(self.optional_skills),
            "preferred_runtime": self.preferred_runtime,
            "runtime_required": self.runtime_required,
            "agent_id": self.agent_id, "skill_ids": list(self.skill_ids),
            "risk_flags": list(self.risk_flags), "requires_approval": self.requires_approval,
            "evidence": dict(self.evidence),
        }


def _repo_identity(metadata: Mapping[str, Any], workspace: str | None) -> tuple[str | None, str | None]:
    """(repo identity, how it was derived). Prefers a normalised remote, which
    is the only form comparable across nodes that check the same repository out
    at different paths."""
    remote = _as_str(_first(metadata, ("git_remote", "repo_remote", "remote")))
    if remote:
        return normalise_git_remote(remote) or remote, "metadata"
    named = _as_str(_first(metadata, ("repo", "repo_root", "repository")))
    if named:
        return (normalise_git_remote(named) or named) if "://" in named or named.startswith("git@") else named, \
               "metadata"
    if workspace:
        # A worktree path is a weaker identity than a remote, but it is a real
        # one, and it is what a caller who passed only a directory meant.
        return workspace, "workspace"
    return None, "unset"


def analyze(task: Any = None, *, prompt: str | None = None, title: str | None = None,
            metadata: Mapping[str, Any] | None = None, task_id: str | None = None,
            project_id: str | None = None, agent_id: str | None = None,
            skill_ids: Sequence[str] | None = None) -> TaskProfile:
    """Build a TaskProfile from a QueueTask (or from loose fields).

    Deterministic and side-effect free: the same inputs always produce the same
    profile, which is what makes a routing decision reproducible when someone
    asks three days later why a task went where it did.
    """
    if task is not None:
        prompt = prompt if prompt is not None else getattr(task, "prompt", None)
        title = title if title is not None else getattr(task, "title", None)
        metadata = metadata if metadata is not None else (getattr(task, "metadata", None) or {})
        task_id = task_id or getattr(task, "id", None)
        project_id = project_id or getattr(task, "project_id", None)
        agent_id = agent_id or getattr(task, "agent_id", None)
        skill_ids = skill_ids if skill_ids is not None else (getattr(task, "skill_ids", None) or ())
    metadata = dict(metadata or {})
    text = " ".join(part for part in (title or "", prompt or "") if part)
    evidence: dict[str, str] = {}

    # -- project ---------------------------------------------------------
    project = _as_str(_first(metadata, ("project", "project_name", "project_code")))
    evidence["project"] = "metadata" if project else "unset"
    resolved_project_id = _as_str(project_id) or _as_str(_first(metadata, ("project_id",)))
    evidence["project_id"] = ("column" if _as_str(project_id) else
                              ("metadata" if resolved_project_id else "unset"))

    # -- workspace / repo / branch ----------------------------------------
    workspace = _as_str(_first(metadata, ("workspace", "worktree", "worktree_path", "cwd", "working_directory")))
    evidence["workspace"] = "metadata" if workspace else "unset"
    if not workspace and text:
        # A prompt that names an absolute path that EXISTS is naming a
        # workspace. Existence is required: an invented path would otherwise
        # become a constraint no session could satisfy, turning a heuristic
        # into a permanent WAITING_RUNTIME.
        for match in _PATH_LIKE.finditer(text):
            candidate = match.group(1)
            if os.path.isdir(candidate):
                workspace = candidate
                evidence["workspace"] = "prompt"
                break

    repo, repo_source = _repo_identity(metadata, workspace)
    evidence["repo"] = repo_source

    branch = _as_str(_first(metadata, ("branch", "git_branch", "target_branch")))
    evidence["branch"] = "metadata" if branch else "unset"
    if not branch and text and (match := _BRANCH_LIKE.search(text)):
        branch, evidence["branch"] = match.group(1), "prompt"

    # -- task type / capabilities -----------------------------------------
    task_type = _as_str(_first(metadata, ("task_type", "type", "kind")))
    evidence["task_type"] = "metadata" if task_type else "unset"
    if not task_type and text:
        for name, pattern in TASK_TYPE_PATTERNS:
            if pattern.search(text):
                task_type, evidence["task_type"] = name, "prompt"
                break

    capabilities = _as_tuple(_first(metadata, ("capabilities", "required_capabilities")))
    evidence["capabilities"] = "metadata" if capabilities else "unset"

    required_skills = _as_tuple(_first(metadata, _REQUIRED_SKILL_KEYS))
    evidence["required_skills"] = "metadata" if required_skills else "unset"
    optional_skills = _as_tuple(_first(metadata, _OPTIONAL_SKILL_KEYS))
    evidence["optional_skills"] = "metadata" if optional_skills else "unset"

    resolved_skill_ids = _as_tuple(skill_ids) or _as_tuple(_first(metadata, _SKILL_KEYS))
    evidence["skill_ids"] = "metadata" if resolved_skill_ids else "unset"

    resolved_agent_id = _as_str(agent_id) or _as_str(_first(metadata, ("agent_id", "agent")))
    evidence["agent_id"] = "metadata" if resolved_agent_id else "unset"

    # -- runtime -----------------------------------------------------------
    runtime = _as_str(_first(metadata, ("runtime", "agent_type", "preferred_runtime")))
    runtime_required = False
    if runtime:
        runtime = runtime.lower()
        # An explicit runtime IS a constraint. `runtime_optional: true` lets a
        # caller express a preference instead, rather than having to omit the
        # field and lose the signal entirely.
        runtime_required = not bool(metadata.get("runtime_optional"))
        evidence["preferred_runtime"] = "metadata"
    elif text:
        for name, pattern in RUNTIME_PATTERNS:
            if pattern.search(text):
                runtime, evidence["preferred_runtime"] = name, "prompt"
                break
        else:
            evidence["preferred_runtime"] = "unset"
    else:
        evidence["preferred_runtime"] = "unset"

    # -- risk (delegated, never re-derived) --------------------------------
    classification = task_classifier.classify(
        text, changed_paths=_as_tuple(_first(metadata, ("changed_paths", "paths"))))
    evidence["risk_flags"] = "task_classifier"

    return TaskProfile(
        task_id=_as_str(task_id), project=project, project_id=resolved_project_id,
        repo=repo, workspace=workspace, branch=branch, task_type=task_type,
        capabilities=capabilities, required_skills=required_skills,
        optional_skills=optional_skills, preferred_runtime=runtime,
        runtime_required=runtime_required, agent_id=resolved_agent_id,
        skill_ids=resolved_skill_ids,
        risk_flags=tuple(classification.exclusions_hit),
        requires_approval=classification.requires_approval,
        evidence=evidence,
    )
