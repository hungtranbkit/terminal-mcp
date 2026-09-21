"""Optional Graphify adapter for bounded repository-graph retrieval.

Graphify is deliberately an augmentation layer, not a replacement for the
Project Knowledge Map, Module Context Pack, Similar Bug Retrieval, runbooks,
or token/file/search budgets.

The adapter has three rules:

1. Never auto-install Graphify or make network calls.
2. Never auto-build a graph on the task-start path. Building belongs to setup,
   git hooks, or an explicit sync operation.
3. Query results are bounded before they enter a worker briefing, so a graph
   cannot turn a small context pack back into a repository-sized prompt.

The official Python distribution is graphifyy; the executable remains
graphify. This module only depends on the executable and the generated
graphify-out/graph.json so Terminal MCP keeps working normally when the
optional dependency is absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

DEFAULT_GRAPH_RELATIVE = Path("graphify-out/graph.json")
DEFAULT_QUERY_BUDGET = 900
DEFAULT_QUERY_TIMEOUT = 6.0
DEFAULT_QUERY_CHARS = 1400

_DISABLED = {"0", "false", "no", "off", "disabled"}


def _enabled_from_env() -> bool:
    return os.environ.get("TERMINAL_MCP_GRAPHIFY", "auto").strip().lower() not in _DISABLED


def _clean_message(value: str, *, limit: int = 500) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 14)].rstrip() + " [truncated]"


@dataclass(frozen=True)
class GraphifyQuery:
    """One bounded graph lookup, including why it could not run."""

    available: bool
    text: str = ""
    graph_path: str | None = None
    reason: str | None = None

    @property
    def useful(self) -> bool:
        return bool(self.available and self.text.strip())

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "useful": self.useful,
            "text": self.text,
            "graph_path": self.graph_path,
            "reason": self.reason,
        }


class GraphifyBridge:
    """Thin, safe wrapper around the local graphify CLI."""

    def __init__(
        self,
        root: str | Path,
        *,
        executable: str | None = None,
        graph_path: str | Path | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        configured_graph = graph_path or os.environ.get("TERMINAL_MCP_GRAPHIFY_GRAPH")
        resolved = Path(configured_graph) if configured_graph else DEFAULT_GRAPH_RELATIVE
        if not resolved.is_absolute():
            resolved = self.root / resolved
        self.graph_path = resolved

        configured_bin = executable or os.environ.get("TERMINAL_MCP_GRAPHIFY_BIN")
        self.executable = configured_bin or shutil.which("graphify")

    def status(self) -> dict[str, Any]:
        enabled = _enabled_from_env()
        graph_exists = self.graph_path.is_file()
        installed = bool(self.executable)
        return {
            "enabled": enabled,
            "installed": installed,
            "graph_exists": graph_exists,
            "ready": bool(enabled and installed and graph_exists),
            "executable": self.executable,
            "graph_path": str(self.graph_path),
        }

    def query(
        self,
        question: str,
        *,
        budget: int = DEFAULT_QUERY_BUDGET,
        timeout: float = DEFAULT_QUERY_TIMEOUT,
        max_chars: int = DEFAULT_QUERY_CHARS,
    ) -> GraphifyQuery:
        """Query the existing graph without building, updating, or widening it."""

        question = (question or "").strip()
        if not question:
            return GraphifyQuery(False, graph_path=str(self.graph_path),
                                 reason="empty graph question")
        if not _enabled_from_env():
            return GraphifyQuery(False, graph_path=str(self.graph_path),
                                 reason="Graphify integration disabled")
        if not self.executable:
            return GraphifyQuery(False, graph_path=str(self.graph_path),
                                 reason="graphify executable not installed")
        if not self.graph_path.is_file():
            return GraphifyQuery(False, graph_path=str(self.graph_path),
                                 reason="graphify-out/graph.json not built")

        safe_budget = max(100, min(int(budget), 4000))
        safe_chars = max(200, min(int(max_chars), 6000))
        args = [
            self.executable,
            "query",
            question,
            "--graph",
            str(self.graph_path),
            "--budget",
            str(safe_budget),
        ]
        try:
            result = subprocess.run(
                args,
                cwd=str(self.root),
                text=True,
                capture_output=True,
                timeout=max(0.5, float(timeout)),
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return GraphifyQuery(False, graph_path=str(self.graph_path),
                                 reason=f"graphify query failed: {type(exc).__name__}")

        if result.returncode != 0:
            detail = _clean_message(result.stderr or result.stdout or "unknown error")
            return GraphifyQuery(False, graph_path=str(self.graph_path),
                                 reason=f"graphify exited {result.returncode}: {detail}")

        text = (result.stdout or "").strip()
        if not text:
            return GraphifyQuery(True, graph_path=str(self.graph_path),
                                 reason="graphify returned no context")
        if len(text) > safe_chars:
            text = text[: safe_chars - 18].rstrip() + "\n[graph truncated]"
        return GraphifyQuery(True, text=text, graph_path=str(self.graph_path))

    def sync_command(self, *, update: bool = True, code_only: bool = True) -> list[str]:
        """Return the explicit command setup/runbooks may execute.

        The command is returned, not executed, so merely constructing a context
        pack can never trigger an expensive repository scan.
        """

        executable = self.executable or "graphify"
        if update:
            return [executable, "update", "."]
        command = [executable, "extract", "."]
        if code_only:
            command.append("--code-only")
        return command

    def run_sync(
        self,
        *,
        update: bool = True,
        code_only: bool = True,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        """Explicitly build/update the graph; never called by task-start code."""

        if not _enabled_from_env():
            return {"ok": False, "reason": "Graphify integration disabled"}
        if not self.executable:
            return {"ok": False, "reason": "graphify executable not installed"}

        command = self.sync_command(update=update, code_only=code_only)
        try:
            result = subprocess.run(
                command,
                cwd=str(self.root),
                text=True,
                capture_output=True,
                timeout=max(1.0, float(timeout)),
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "reason": f"graphify sync failed: {type(exc).__name__}",
                    "command": command}

        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "command": command,
            "stdout": _clean_message(result.stdout, limit=1600),
            "stderr": _clean_message(result.stderr, limit=800),
            "graph_path": str(self.graph_path),
            "graph_exists": self.graph_path.is_file(),
        }


def module_question(module: str, *, paths: Sequence[str] = ()) -> str:
    """Small deterministic question used by Module Context Pack retrieval."""

    name = (module or "").strip()
    path_hint = ", ".join(str(path) for path in paths[:4] if str(path).strip())
    question = (
        f"Explain the code structure and important dependencies for module {name}. "
        "Focus on entry points, callers, callees, and files likely to matter for a change. "
        "Be concise and do not restate source code."
    )
    if path_hint:
        question += f" Known module paths: {path_hint}."
    return question
