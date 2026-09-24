"""Bounded source inspection and deterministic Archify IR authoring."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .repo_read import SECRET_PATH_GLOBS


DIAGRAM_TYPES = frozenset({"architecture", "workflow", "sequence", "dataflow", "lifecycle"})
SOURCE_SUFFIXES = frozenset({".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".cs"})
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build", "target",
    "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".tox", ".next",
})


class ArchifyEvidenceError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(f"INSUFFICIENT_EVIDENCE: {message}")
        self.code = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True)
class SourceEdge:
    source: str
    target: str
    evidence: str


@dataclass(frozen=True)
class SourceInspection:
    project: str
    modules: tuple[str, ...]
    module_files: dict[str, str]
    edges: tuple[SourceEdge, ...]
    states: tuple[str, ...]
    transitions: tuple[tuple[str, str], ...]
    files: tuple[str, ...]
    bytes_read: int
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["edges"] = [asdict(edge) for edge in self.edges]
        return value


def _secret_name(path: Path) -> bool:
    from fnmatch import fnmatch
    text = path.as_posix()
    return any(fnmatch(text, pattern) or fnmatch(path.name, pattern) for pattern in SECRET_PATH_GLOBS)


def _module_name(relative: Path) -> str:
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _literal_strings(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        values: list[str] = []
        for item in node.elts:
            values.extend(_literal_strings(item))
        return values
    return []


class SourceInspector:
    def __init__(self, *, max_files: int = 500, max_bytes: int = 4 * 1024 * 1024) -> None:
        self.max_files = max(1, int(max_files))
        self.max_bytes = max(1, int(max_bytes))

    def inspect(self, root: str | Path) -> SourceInspection:
        project = Path(root).resolve(strict=True)
        candidates: list[Path] = []
        for path in sorted(project.rglob("*")):
            try:
                relative = path.relative_to(project)
            except ValueError:
                continue
            if path.is_symlink() or any(part in SKIP_DIRS for part in relative.parts):
                continue
            if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES or _secret_name(relative):
                continue
            try:
                resolved = path.resolve(strict=True)
            except OSError:
                continue
            if not resolved.is_relative_to(project):
                continue
            candidates.append(path)

        chosen: list[tuple[Path, str]] = []
        total = 0
        truncated = len(candidates) > self.max_files
        for path in candidates[:self.max_files]:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if total + size > self.max_bytes:
                truncated = True
                break
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            total += len(text.encode("utf-8"))
            chosen.append((path, text))

        module_files = {_module_name(path.relative_to(project)): path.relative_to(project).as_posix()
                        for path, _text in chosen}
        module_names = set(module_files)
        edges: set[SourceEdge] = set()
        states: set[str] = set()
        transitions: set[tuple[str, str]] = set()
        for path, text in chosen:
            relative = path.relative_to(project).as_posix()
            source = _module_name(path.relative_to(project))
            if path.suffix == ".py":
                self._inspect_python(text, source, relative, module_names, edges, states, transitions)
            else:
                self._inspect_generic(text, source, relative, module_names, edges)

        return SourceInspection(
            project=str(project), modules=tuple(sorted(module_names)), module_files=module_files,
            edges=tuple(sorted(edges, key=lambda edge: (edge.source, edge.target, edge.evidence))),
            states=tuple(sorted(states)), transitions=tuple(sorted(transitions)),
            files=tuple(path.relative_to(project).as_posix() for path, _text in chosen),
            bytes_read=total, truncated=truncated,
        )

    @staticmethod
    def _inspect_python(text: str, source: str, evidence: str, modules: set[str],
                        edges: set[SourceEdge], states: set[str],
                        transitions: set[tuple[str, str]]) -> None:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    package = source.split(".")[:-1]
                    keep = max(0, len(package) - (node.level - 1))
                    module = ".".join([*package[:keep], *([module] if module else [])])
                if module:
                    targets.append(module)
                    targets.extend(f"{module}.{alias.name}" for alias in node.names)
            for target in targets:
                match = target if target in modules else next(
                    (name for name in modules if target.startswith(name + ".") or name.startswith(target + ".")), None)
                if match and match != source:
                    edges.add(SourceEdge(source, match, evidence))

            if isinstance(node, ast.ClassDef):
                bases = {getattr(base, "id", "") for base in node.bases}
                bases.update(getattr(base, "attr", "") for base in node.bases)
                if "Enum" in bases:
                    for statement in node.body:
                        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                            value = statement.value
                            states.update(_literal_strings(value) if value is not None else [])
            if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and "TRANSITION" in target.id.upper() for target in node.targets):
                if isinstance(node.value, ast.Dict):
                    for key, value in zip(node.value.keys, node.value.values):
                        origins = _literal_strings(key) if key is not None else []
                        destinations = _literal_strings(value)
                        transitions.update((origin, destination) for origin in origins for destination in destinations)

    @staticmethod
    def _inspect_generic(text: str, source: str, evidence: str, modules: set[str],
                         edges: set[SourceEdge]) -> None:
        patterns = (
            r"(?:import|from)\s+['\"]?([A-Za-z0-9_./-]+)",
            r"require\(['\"]([^'\"]+)",
            r"use\s+([A-Za-z0-9_:]+)",
        )
        for pattern in patterns:
            for raw in re.findall(pattern, text):
                normalized = raw.replace("/", ".").replace("::", ".").lstrip(".")
                match = normalized if normalized in modules else next(
                    (name for name in modules if name.endswith(normalized) or normalized.endswith(name)), None)
                if match and match != source:
                    edges.add(SourceEdge(source, match, evidence))


def _identifier(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-").lower()
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"node-{cleaned}"
    return cleaned[:64]


class ArchifyAuthor:
    MAX_NODES = 8

    def build(self, diagram_type: str, inspection: SourceInspection, prompt: str = "") -> dict[str, Any]:
        if diagram_type not in DIAGRAM_TYPES:
            raise ValueError(f"INVALID_DIAGRAM_TYPE: {diagram_type}")
        if diagram_type == "lifecycle":
            return self._lifecycle(inspection)
        selected, edges = self._select_graph(inspection, prompt)
        if len(selected) < 2 or not edges:
            raise ArchifyEvidenceError(f"{diagram_type} requires at least one verified source relationship")
        title = f"{Path(inspection.project).name} {diagram_type.title()}"
        if diagram_type == "architecture":
            return self._architecture(title, selected, edges, inspection)
        if diagram_type == "workflow":
            return self._workflow(title, selected, edges)
        if diagram_type == "sequence":
            return self._sequence(title, selected, edges)
        return self._dataflow(title, selected, edges)

    def _select_graph(self, inspection: SourceInspection, prompt: str) -> tuple[list[str], list[SourceEdge]]:
        degree = {module: 0 for module in inspection.modules}
        for edge in inspection.edges:
            degree[edge.source] = degree.get(edge.source, 0) + 1
            degree[edge.target] = degree.get(edge.target, 0) + 1
        terms = {term.lower() for term in re.findall(r"[A-Za-z0-9_]+", prompt) if len(term) > 2}
        ranked = sorted(inspection.modules, key=lambda name: (
            -sum(term in name.lower() for term in terms), -degree.get(name, 0), name,
        ))
        start = next((module for module in ranked if degree.get(module, 0)), None)
        if start is None:
            return [], []
        adjacency: dict[str, list[SourceEdge]] = {}
        for edge in inspection.edges:
            adjacency.setdefault(edge.source, []).append(edge)
            adjacency.setdefault(edge.target, []).append(edge)
        selected = [start]
        selected_set = {start}
        tree: list[SourceEdge] = []
        neighbors = sorted(adjacency.get(start, ()), key=lambda edge: (
                -sum(term in (edge.source + edge.target).lower() for term in terms),
                edge.source, edge.target,
            ))
        for edge in neighbors:
            other = edge.target if edge.source == start else edge.source
            if other in selected_set:
                continue
            selected.append(other)
            selected_set.add(other)
            tree.append(edge)
            if len(selected) >= self.MAX_NODES:
                break
        return selected, tree

    @staticmethod
    def _architecture(title: str, modules: list[str], edges: list[SourceEdge],
                      inspection: SourceInspection) -> dict[str, Any]:
        components = []
        for index, module in enumerate(modules):
            if index == 0:
                position = [40, 40 + ((len(modules) - 2) // 2) * 120]
            else:
                position = [400, 40 + (index - 1) * 120]
            components.append({
                "id": _identifier(module), "type": "backend",
                "label": module if len(module) <= 28 else module.split(".")[-1][:28],
                "pos": position, "size": [210, 72],
            })
        return {
            "schema_version": 1, "diagram_type": "architecture", "meta": {"title": title},
            "components": components, "boundaries": [],
            "connections": [{"from": _identifier(edge.source), "to": _identifier(edge.target)}
                            for edge in edges],
        }

    @staticmethod
    def _workflow(title: str, modules: list[str], edges: list[SourceEdge]) -> dict[str, Any]:
        index = {module: position for position, module in enumerate(modules)}
        return {
            "schema_version": 2, "diagram_type": "workflow", "meta": {"title": title},
            "lanes": [{"id": "source", "label": "Verified source modules"}],
            "phases": [], "groups": [],
            "nodes": [{"id": _identifier(module), "lane": "source", "col": position,
                       "type": "backend", "label": module} for module, position in index.items()],
            "edges": [{"from": _identifier(edge.source), "to": _identifier(edge.target),
                       "label": "imports"} for edge in edges],
        }

    @staticmethod
    def _sequence(title: str, modules: list[str], edges: list[SourceEdge]) -> dict[str, Any]:
        return {
            "schema_version": 1, "diagram_type": "sequence", "meta": {"title": title},
            "participants": [{"id": _identifier(module), "type": "backend", "label": module}
                             for module in modules],
            "messages": [{"from": _identifier(edge.source), "to": _identifier(edge.target),
                          "y": 160 + index * 46, "label": "imports"}
                         for index, edge in enumerate(edges)],
            "segments": [], "activations": [],
        }

    @staticmethod
    def _dataflow(title: str, modules: list[str], edges: list[SourceEdge]) -> dict[str, Any]:
        index = {module: position for position, module in enumerate(modules)}
        return {
            "schema_version": 1, "diagram_type": "dataflow", "meta": {"title": title},
            "stages": [{"label": "Source"}, {"label": "Dependency"}],
            "nodes": [{"id": _identifier(module), "type": "backend", "label": module,
                       "stage": position % 2, "row": position // 2}
                      for module, position in index.items()],
            "flows": [{"from": _identifier(edge.source), "to": _identifier(edge.target),
                       "label": "import"} for edge in edges],
        }

    @staticmethod
    def _lifecycle(inspection: SourceInspection) -> dict[str, Any]:
        if len(inspection.states) < 2 or not inspection.transitions:
            raise ArchifyEvidenceError("lifecycle requires explicit states and a transition map")
        connected = {item for transition in inspection.transitions for item in transition}
        states = [state for state in inspection.states if state in connected]
        if len(states) < 2:
            raise ArchifyEvidenceError("lifecycle transition states do not match declared states")
        incoming = {target for _source, target in inspection.transitions}
        outgoing = {source for source, _target in inspection.transitions}
        entries = [state for state in states if state not in incoming]
        ordered: list[str] = []
        queue = sorted(entries)
        while queue:
            state = queue.pop(0)
            if state in ordered:
                continue
            ordered.append(state)
            queue.extend(target for source, target in inspection.transitions if source == state)
        ordered.extend(state for state in states if state not in ordered)
        return {
            "schema_version": 1, "diagram_type": "lifecycle",
            "meta": {"title": f"{Path(inspection.project).name} Lifecycle"},
            "lanes": [{"id": "main", "label": "Verified states"}],
            "states": [{"id": _identifier(state),
                        "type": ("start" if state in entries else "success" if state not in outgoing else "active"),
                        "label": state, "lane": "main", "col": index}
                       for index, state in enumerate(ordered)],
            "transitions": [{"from": _identifier(source), "to": _identifier(target)}
                            for source, target in inspection.transitions
                            if source in connected and target in connected],
        }
