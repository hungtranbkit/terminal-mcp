"""Skill providers: where a workflow stage's prompt-level skill comes from.

WHAT THIS IS FOR
----------------
gstack (github.com/garrytan/gstack, MIT) is a set of Claude Code / Codex
*skills* -- SKILL.md files a coding agent loads to run a review, a QA pass,
a ship checklist. It is prompt-level work done INSIDE a worker session.

It is not an orchestrator, and this module is careful not to let it become
one. ProjectFlow already owns the decisions:

    backlog_service    what the project intends to do
    queue_engine       what executes, and when
    coordinator        whether a dispatch is safe            (gate)
    integration_reviewer  whether a merge is safe            (gate)
    verify_queue       whether the work is actually done     (gate)

Those gates are deliberately deterministic -- "no ML/LLM call to invent
scope that doesn't already exist", as planner_service puts it. A skill can
produce findings, a report, a diff, a screenshot. It cannot mark a task
DONE. That stays with verification, which is the whole reason this is a
provider abstraction and not a pile of slash commands sprinkled through
the coordinator.

WHAT A PROVIDER RESOLVES
------------------------
A (stage, host) pair -> a SkillRef, or nothing. "Nothing" is a first-class
answer: gstack may not be installed, may not support that host, or the
project may have that stage switched off. Every caller must work when the
answer is None, which is why resolution returns a Resolution carrying the
REASON rather than raising.

SECURITY POSTURE
----------------
This module only ever READS the filesystem to see what is installed. It
never installs, never upgrades, never registers a shell hook, and never
executes a skill -- gstack ships an opt-in Stop hook (`gstack-verify-gate`)
which this deliberately does not touch. Installation stays a documented,
operator-run step (docs/GSTACK_INTEGRATION.md).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Protocol

# The pipeline ProjectFlow already runs, named once so a policy can switch
# individual stages on and off without inventing new vocabulary.
STAGES: tuple[str, ...] = (
    "discovery",            # what are we even building
    "plan_review",          # product/CEO-mode read of the plan
    "architecture_review",  # eng-manager-mode read of the plan
    "implementation",       # the worker doing the work
    "pre_merge_review",     # code review before it lands
    "verification",         # QA / acceptance
    "release",              # ship / deploy gate
    "retro",                # what did we learn
)

# Agent hosts a skill can be installed for. gstack supports more (opencode,
# cursor, factory, kiro, slate); these are the two this fleet actually runs.
HOSTS: tuple[str, ...] = ("claude", "codex")

STATUS_READY = "READY"
STATUS_MISSING = "MISSING"
STATUS_DRIFT = "DRIFT"          # installed, but older than the project pins
STATUS_UNSUPPORTED = "UNSUPPORTED"  # installed, but not for this host


@dataclass(frozen=True)
class SkillRef:
    """One concrete skill a worker session can be told to run."""

    provider: str
    skill: str
    host: str
    invocation: str          # exactly what the worker types, e.g. "/review"
    version: str | None = None


@dataclass(frozen=True)
class ProviderStatus:
    status: str
    detail: str
    version: str | None = None
    root: str | None = None
    skills: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.status == STATUS_READY


@dataclass(frozen=True)
class Resolution:
    """What a stage resolved to, and -- always -- why.

    `skill is None` is a normal outcome, not an error: it means ProjectFlow
    runs that stage with its own gate alone. The reason is carried so a
    dashboard or a task's evidence can say which it was.
    """

    stage: str
    skill: SkillRef | None
    reason: str
    fallback: bool = False


class SkillProvider(Protocol):
    name: str

    def status(self, host: str) -> ProviderStatus: ...

    def resolve(self, stage: str, host: str) -> SkillRef | None: ...


# -- gstack -------------------------------------------------------------------

# Verified against garrytan/gstack @ v1.84.1.0 (commit 71f6048, 2026-09-09)
# by listing the repo tree: 53 directories carry a SKILL.md. These are the
# ones that correspond to a stage ProjectFlow already runs. Every name here
# was read from the repo, never from its README prose -- the README still
# advertises "23 opinionated tools".
GSTACK_STAGE_SKILLS: Mapping[str, tuple[str, ...]] = {
    "discovery": ("office-hours",),
    "plan_review": ("plan-ceo-review",),
    "architecture_review": ("plan-eng-review",),
    "pre_merge_review": ("review",),
    "verification": ("qa",),
    "release": ("ship",),
    "retro": ("retro",),
    # "implementation" is deliberately absent: the worker agent does the
    # work, and wrapping that in someone else's skill is exactly the
    # "replace the orchestration" move this integration refuses.
}

_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")


def _claude_root() -> Path:
    return Path(os.environ.get("CLAUDE_SKILLS_HOME") or (Path.home() / ".claude" / "skills")) / "gstack"


def _codex_root() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "skills"


class GstackProvider:
    """Reads an existing gstack install. Never writes one.

    `minimum_version` is the project's pin. An older install reports DRIFT
    rather than READY: a skill whose behaviour the project has not seen is
    not the same skill, and silently running it would put unattributable
    findings into a task's evidence.
    """

    name = "gstack"

    def __init__(self, *, minimum_version: str | None = None,
                 claude_root: Path | None = None, codex_root: Path | None = None) -> None:
        self.minimum_version = minimum_version
        self._claude_root = claude_root
        self._codex_root = codex_root

    def _root_for(self, host: str) -> Path | None:
        if host == "claude":
            return self._claude_root if self._claude_root is not None else _claude_root()
        if host == "codex":
            return self._codex_root if self._codex_root is not None else _codex_root()
        return None

    def _installed_skills(self, root: Path, host: str) -> tuple[str, ...]:
        if host == "codex":
            # gstack installs one directory per skill, prefixed: gstack-<name>/
            return tuple(sorted(
                child.name[len("gstack-"):]
                for child in _iterdir(root)
                if child.name.startswith("gstack-") and (child / "SKILL.md").is_file()))
        return tuple(sorted(
            child.name for child in _iterdir(root) if (child / "SKILL.md").is_file()))

    def _version(self, root: Path) -> str | None:
        version_file = root / "VERSION"
        try:
            text = version_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text if _VERSION_RE.match(text) else None

    def status(self, host: str) -> ProviderStatus:
        root = self._root_for(host)
        if root is None:
            return ProviderStatus(STATUS_UNSUPPORTED, f"gstack has no known install path for host {host!r}")
        if not root.is_dir():
            return ProviderStatus(STATUS_MISSING, f"no gstack install at {root}", root=str(root))
        skills = self._installed_skills(root, host)
        if not skills:
            return ProviderStatus(STATUS_MISSING, f"{root} exists but contains no SKILL.md", root=str(root))
        version = self._version(root if host == "claude" else root.parent)
        if self.minimum_version and version and _older(version, self.minimum_version):
            return ProviderStatus(STATUS_DRIFT,
                                  f"installed {version} is older than the pinned {self.minimum_version}",
                                  version=version, root=str(root), skills=skills)
        return ProviderStatus(STATUS_READY, f"{len(skills)} skills at {root}",
                              version=version, root=str(root), skills=skills)

    def resolve(self, stage: str, host: str) -> SkillRef | None:
        candidates = GSTACK_STAGE_SKILLS.get(stage, ())
        if not candidates:
            return None
        current = self.status(host)
        if not current.usable:
            return None
        for candidate in candidates:
            if candidate in current.skills:
                return SkillRef(provider=self.name, skill=candidate, host=host,
                                invocation=f"/{candidate}", version=current.version)
        return None


def _iterdir(root: Path) -> Iterable[Path]:
    try:
        return sorted(root.iterdir())
    except OSError:
        return ()


def _older(found: str, minimum: str) -> bool:
    def parts(value: str) -> list[int]:
        return [int(chunk) for chunk in value.split(".") if chunk.isdigit()]

    a, b = parts(found), parts(minimum)
    length = max(len(a), len(b))
    a += [0] * (length - len(a))
    b += [0] * (length - len(b))
    return a < b


# -- policy -------------------------------------------------------------------

@dataclass(frozen=True)
class WorkflowPolicy:
    """Which stages a project runs through a skill provider, and which not.

    Nothing is on by default. A pipeline every project must run is how an
    integration becomes a tax; this starts empty and a pilot project opts
    individual stages in (docs/GSTACK_INTEGRATION.md, "Rollout").
    """

    project: str
    enabled_stages: frozenset[str] = field(default_factory=frozenset)
    provider: str = "gstack"

    def __post_init__(self) -> None:
        unknown = set(self.enabled_stages) - set(STAGES)
        if unknown:
            raise ValueError(f"unknown workflow stages: {sorted(unknown)}")

    def enabled(self, stage: str) -> bool:
        return stage in self.enabled_stages


def resolve_stage(policy: WorkflowPolicy, stage: str, host: str,
                  providers: Mapping[str, SkillProvider]) -> Resolution:
    """The one place a stage turns into a skill, or into an honest no.

    Callers never branch on whether gstack is installed; they read
    `resolution.skill` and, when it is None, run the stage exactly as they
    did before this module existed.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown workflow stage: {stage!r}")
    if not policy.enabled(stage):
        return Resolution(stage, None, f"stage {stage!r} is not enabled for project {policy.project!r}")
    provider = providers.get(policy.provider)
    if provider is None:
        return Resolution(stage, None, f"no provider named {policy.provider!r} is registered", fallback=True)
    current = provider.status(host)
    if not current.usable:
        return Resolution(stage, None,
                          f"{policy.provider} unusable on host {host!r}: {current.status} -- {current.detail}",
                          fallback=True)
    skill = provider.resolve(stage, host)
    if skill is None:
        return Resolution(stage, None,
                          f"{policy.provider} has no skill for stage {stage!r} on host {host!r}",
                          fallback=True)
    return Resolution(stage, skill, f"{policy.provider} {skill.invocation} on {host}")


# -- evidence -----------------------------------------------------------------

def evidence_record(resolution: Resolution, *, task_id: str, project: str,
                    branch: str | None = None, commit: str | None = None,
                    artifact: str | None = None, outcome: str | None = None) -> dict[str, object]:
    """What a stage run contributes to a ProjectFlow task's evidence trail.

    Deliberately NOT a verdict. `outcome` records what the skill REPORTED;
    whether the task is done remains verify_queue's call. Keeping those
    separable is the point -- a skill that says "looks good" is a data
    point, not a gate.
    """
    return {
        "task_id": task_id,
        "project": project,
        "stage": resolution.stage,
        "provider": resolution.skill.provider if resolution.skill else None,
        "skill": resolution.skill.skill if resolution.skill else None,
        "skill_version": resolution.skill.version if resolution.skill else None,
        "host": resolution.skill.host if resolution.skill else None,
        "invocation": resolution.skill.invocation if resolution.skill else None,
        "fallback": resolution.fallback,
        "reason": resolution.reason,
        "branch": branch,
        "commit": commit,
        "artifact": artifact,
        "reported_outcome": outcome,
        "decides_done": False,
    }
