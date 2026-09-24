"""Allowed-root project discovery for the shared Archify capability."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


PROJECT_MARKERS = (
    ".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod",
    "pom.xml", "build.gradle", "build.gradle.kts",
)


class ArchifyPolicyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProjectInfo:
    name: str
    path: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


class ArchifyProjectPolicy:
    """Resolve and enumerate projects without widening configured roots."""

    def __init__(self, allowed_roots: Iterable[str | Path], *, max_projects: int = 200,
                 max_discovery_depth: int = 2) -> None:
        roots: list[Path] = []
        for value in allowed_roots:
            root = Path(value).expanduser()
            if not root.is_absolute():
                raise ValueError("archify.allowed_roots entries must be absolute")
            resolved = root.resolve()
            if resolved == Path(resolved.anchor):
                raise ValueError("archify.allowed_roots may not contain filesystem root")
            if resolved.is_dir():
                roots.append(resolved)
        self.allowed_roots = tuple(dict.fromkeys(roots))
        self.max_projects = max(1, int(max_projects))
        self.max_discovery_depth = max(0, int(max_discovery_depth))

    @staticmethod
    def _is_project(path: Path) -> bool:
        return any((path / marker).exists() for marker in PROJECT_MARKERS)

    def _inside_root(self, path: Path) -> bool:
        return any(path == root or path.is_relative_to(root) for root in self.allowed_roots)

    def resolve_project(self, value: str | Path) -> ProjectInfo:
        raw = Path(value).expanduser()
        if not raw.is_absolute():
            raise ArchifyPolicyError("PROJECT_NOT_ALLOWED", "project path must be absolute")
        candidate = raw.resolve(strict=False)
        if not self._inside_root(candidate):
            raise ArchifyPolicyError("PROJECT_NOT_ALLOWED", "project is outside allowed roots")
        try:
            resolved = raw.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ArchifyPolicyError("PROJECT_NOT_FOUND", "project directory does not exist") from None
        if not resolved.is_dir():
            raise ArchifyPolicyError("PROJECT_NOT_FOUND", "project path is not a directory")
        if not self._inside_root(resolved):
            raise ArchifyPolicyError("PROJECT_NOT_ALLOWED", "project is outside allowed roots")
        if not self._is_project(resolved):
            raise ArchifyPolicyError("PROJECT_NOT_FOUND", "directory has no recognized project marker")
        return ProjectInfo(name=resolved.name, path=str(resolved))

    def discover_projects(self) -> list[ProjectInfo]:
        found: dict[str, ProjectInfo] = {}
        queue: list[tuple[Path, int]] = [(root, 0) for root in self.allowed_roots]
        while queue and len(found) < self.max_projects:
            current, depth = queue.pop(0)
            if self._is_project(current):
                found[str(current)] = ProjectInfo(name=current.name, path=str(current))
            if depth >= self.max_discovery_depth:
                continue
            try:
                children = sorted(current.iterdir(), key=lambda item: item.name.lower())
            except OSError:
                continue
            for child in children:
                if child.name.startswith(".") or child.is_symlink():
                    continue
                try:
                    if child.is_dir():
                        queue.append((child.resolve(), depth + 1))
                except OSError:
                    continue
        return sorted(found.values(), key=lambda project: (project.name.lower(), project.path))[:self.max_projects]
