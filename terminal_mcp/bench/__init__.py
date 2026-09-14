"""Read-only before/after efficiency benchmark for the task pipeline.

Analysis tooling ONLY. Nothing in this package writes to any database,
defines runtime telemetry, validates a gate, or renders production UI --
it reads whatever telemetry already exists, matches comparable tasks
across the legacy and new-pipeline cohorts, and prints a report that
refuses to state a result it cannot defend.

Entry point: `python -m terminal_mcp.bench` (or the `terminal-mcp-bench`
console script). See docs/EFFICIENCY_BENCHMARK.md."""
from __future__ import annotations

from .model import TaskRecord, TaskUsage, Reentry, SourceStatus
from .report import BenchmarkReport, build_report, render_markdown

__all__ = [
    "TaskRecord",
    "TaskUsage",
    "Reentry",
    "SourceStatus",
    "BenchmarkReport",
    "build_report",
    "render_markdown",
]
