"""Plan and explicitly apply a project-local ECC skills setup.

The default command path is dry-run. This installer only copies selected
SKILL.md packages into the current repository's Claude/Codex project skill
roots; it never installs hooks, modifies global config, or overwrites files.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

DEFAULT_SKILLS = ("tdd-workflow", "verification-loop", "security-review")
_TARGET_ROOTS = {
    "claude": (Path(".claude") / "skills",),
    "codex": (Path(".agents") / "skills",),
    "both": (Path(".claude") / "skills", Path(".agents") / "skills"),
}


@dataclass(frozen=True)
class InstallPlan:
    source: Path
    project: Path
    target: str
    skills: tuple[str, ...]
    destinations: tuple[Path, ...]
    sources: tuple[Path, ...]
    errors: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.errors


def _safe_skill_dir(root: Path, name: str) -> tuple[Path | None, str | None]:
    if not name or name in {".", ".."} or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in name):
        return None, f"invalid skill id {name!r}"
    candidate = root / name
    if candidate.is_symlink():
        return None, f"skill {name!r} is a symlink; refusing unsafe copy"
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, f"skill {name!r} is missing under {root}"
    if resolved != resolved_root and resolved_root not in resolved.parents:
        return None, f"skill {name!r} resolves outside ECC skills root"
    if not resolved.is_dir() or not (resolved / "SKILL.md").is_file():
        return None, f"skill {name!r} has no SKILL.md"
    for entry in resolved.rglob("*"):
        if entry.is_symlink():
            return None, f"skill {name!r} contains a symlink; refusing unsafe copy"
    return resolved, None


def plan_install(source: str | Path, project: str | Path, *, target: str,
                 skills: Sequence[str] = DEFAULT_SKILLS) -> InstallPlan:
    source_root = Path(source).expanduser().resolve()
    project_root = Path(project).expanduser().resolve()
    errors: list[str] = []
    if target not in _TARGET_ROOTS:
        errors.append(f"unsupported target {target!r}; choose claude, codex, or both")
    if not source_root.is_dir():
        errors.append(f"ECC source directory does not exist: {source_root}")
    if not (source_root / "skills").is_dir():
        errors.append(f"ECC source has no skills/ directory: {source_root}")
    if not project_root.is_dir():
        errors.append(f"project directory does not exist: {project_root}")
    selected = tuple(dict.fromkeys(skills))
    if not selected:
        errors.append("select at least one skill")
    skill_sources: list[Path] = []
    for name in selected:
        resolved, error = _safe_skill_dir(source_root / "skills", name)
        if error:
            errors.append(error)
        elif resolved:
            skill_sources.append(resolved)
    destinations: list[Path] = []
    copy_sources: list[Path] = []
    if target in _TARGET_ROOTS:
        source_by_name = dict(zip(selected, skill_sources))
        for root in _TARGET_ROOTS[target]:
            for name in selected:
                destination = project_root / root / name
                destinations.append(destination)
                try:
                    resolved_destination = destination.resolve(strict=False)
                except (OSError, RuntimeError):
                    resolved_destination = destination
                if resolved_destination != project_root and project_root not in resolved_destination.parents:
                    errors.append(f"destination escapes project root through a symlink: {destination}")
                if name in source_by_name:
                    copy_sources.append(source_by_name[name])
                if destination.exists() or destination.is_symlink():
                    errors.append(f"destination already exists; refusing overwrite: {destination}")
    return InstallPlan(source_root, project_root, target, selected,
                       tuple(destinations), tuple(copy_sources), tuple(errors))


def run_install(plan: InstallPlan, *, apply: bool = False) -> dict[str, object]:
    if not plan.ready:
        return {"status": "BLOCKED", "errors": list(plan.errors),
                "destinations": [str(path) for path in plan.destinations]}
    if not apply:
        return {"status": "DRY_RUN", "errors": [],
                "destinations": [str(path) for path in plan.destinations]}
    staged: list[tuple[Path, Path]] = []
    created: list[Path] = []
    try:
        # Stage every payload first, so source/read errors occur before writes
        # to the project. `os.rename` then refuses a destination that appeared
        # after preflight rather than replacing it.
        for source_dir, destination in zip(plan.sources, plan.destinations):
            fresh_source, source_error = _safe_skill_dir(plan.source / "skills", source_dir.name)
            if source_error or fresh_source != source_dir:
                raise ValueError(source_error or f"ECC source changed after preflight: {source_dir}")
            resolved_destination = destination.resolve(strict=False)
            if resolved_destination != plan.project and plan.project not in resolved_destination.parents:
                raise ValueError(f"destination escaped project root after preflight: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".ecc-stage-", dir=destination.parent) as temp:
                payload = Path(temp) / "skill"
                shutil.copytree(source_dir, payload, symlinks=False)
                if any(path.is_symlink() for path in payload.rglob("*")):
                    raise ValueError(f"staged skill contains a symlink: {source_dir.name}")
                staged_copy = destination.parent / f".ecc-stage-{os.getpid()}-{len(staged)}"
                if staged_copy.exists():
                    raise FileExistsError(staged_copy)
                os.rename(payload, staged_copy)
                staged.append((staged_copy, destination))
        for temp_path, destination in staged:
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(f"destination appeared after preflight: {destination}")
            os.rename(temp_path, destination)
            created.append(destination)
    except Exception as exc:  # cleanup only paths created by this invocation
        for path in reversed(created):
            shutil.rmtree(path, ignore_errors=True)
        for path, _ in staged:
            shutil.rmtree(path, ignore_errors=True)
        return {"status": "FAILED", "errors": [str(exc)],
                "destinations": [str(path) for path in plan.destinations]}
    return {"status": "APPLIED", "errors": [],
            "destinations": [str(path) for path in created]}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="reviewed ECC source checkout")
    parser.add_argument("--project", default=".", help="target project root (default: current directory)")
    parser.add_argument("--target", choices=tuple(_TARGET_ROOTS), default="both")
    parser.add_argument("--skills", default=",".join(DEFAULT_SKILLS), help="comma-separated skill ids")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="copy after preflight")
    mode.add_argument("--dry-run", action="store_true", help="preflight and print destinations without writing (default)")
    args = parser.parse_args(argv)
    plan = plan_install(args.source, args.project, target=args.target,
                        skills=tuple(item.strip() for item in args.skills.split(",") if item.strip()))
    result = run_install(plan, apply=args.apply)
    print(f"ECC project setup: {result['status']}")
    for destination in result["destinations"]:
        print(f"  {destination}")
    for error in result["errors"]:
        print(f"ERROR: {error}", file=sys.stderr)
    if result["status"] == "DRY_RUN":
        print("No files changed. Re-run with --apply to copy project-local skills.")
    return 0 if result["status"] in {"DRY_RUN", "APPLIED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
