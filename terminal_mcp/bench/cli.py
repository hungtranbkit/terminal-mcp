"""`terminal-mcp-bench` -- run the comparison and print the report.

Read-only by construction: the only thing this command writes is the
report file you ask for with `--out`. It never touches the databases it
reads, never starts a service, and never deploys anything.

Two defaults are deliberate and both are conservative:

* `--assignment observational`. Saying "randomised" is a claim about
  how tasks were put into cohorts, and it is not the harness's to make
  -- an operator has to assert it. Until they do, every figure is
  labelled descriptive.
* `--cache-ttl-policy split_required`. If telemetry records only a
  collapsed cache-write total, cost cannot be priced exactly, and the
  default is to report it as missing rather than guess a TTL. The
  `assume_5m` / `assume_1h` policies exist so an operator can bound the
  answer from both sides, and whichever was used is printed in the
  report header.

Both default `--state-dir` locations are scanned, because this host is
a NODE as well as running a local federation controller: the node's own
`~/.local/state/terminal-mcp` and the controller's
`~/.local/state/tmcp-fed/terminal-mcp`. Instrumenting only the node's
queue would instrument a permanently empty database if the real queue
lives on the controller.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from . import report as report_module
from . import sources as sources_module
from .work_telemetry import WorkTelemetryDbSource, default_path as work_telemetry_default_path
from .matching import ALL_CONTROLS, DEFAULT_CONTROLS
from .model import TTL_ASSUME_1H, TTL_ASSUME_5M, TTL_SPLIT_REQUIRED
from .report import ASSIGNMENTS, ASSIGNMENT_OBSERVATIONAL, MIN_MATCHED_PER_GROUP

DEFAULT_STATE_DIRS = (
    Path.home() / ".local" / "state" / "terminal-mcp",
    Path.home() / ".local" / "state" / "tmcp-fed" / "terminal-mcp",
)
TTL_POLICIES = (TTL_SPLIT_REQUIRED, TTL_ASSUME_5M, TTL_ASSUME_1H)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="terminal-mcp-bench",
        description="Read-only before/after efficiency comparison of matched task cohorts.",
    )
    parser.add_argument(
        "--state-dir",
        action="append",
        default=None,
        help="Directory holding queue.db / ai_usage.db / work.db. Repeatable. "
        "Defaults to the node state dir and the federation controller state dir.",
    )
    parser.add_argument("--queue-db", action="append", default=None, help="Explicit queue.db path.")
    parser.add_argument("--ai-usage-db", action="append", default=None, help="Explicit ai_usage.db path.")
    parser.add_argument("--work-db", action="append", default=None, help="Explicit work.db path.")
    parser.add_argument(
        "--work-telemetry-db",
        action="append",
        default=None,
        help="Explicit work_telemetry.db path (Task A's per-task token/turn/re-entry store). "
        "Defaults to $TERMINAL_MCP_WORK_TELEMETRY_DB, else the state dirs.",
    )
    parser.add_argument(
        "--price-model",
        default=None,
        help="Model id to price cost_units against for telemetry that records no model. "
        "Every cost figure then carries that stated assumption in the report.",
    )
    parser.add_argument(
        "--jsonl",
        action="append",
        default=None,
        help="Normalised task records, one JSON object per line. Repeatable.",
    )
    parser.add_argument(
        "--cohort-map",
        default=None,
        help="JSON file of {task_id: legacy|new_pipeline} for telemetry that does not label itself.",
    )
    parser.add_argument(
        "--controls",
        default=",".join(DEFAULT_CONTROLS),
        help=f"Comma-separated matching controls from {', '.join(ALL_CONTROLS)}.",
    )
    parser.add_argument(
        "--assignment",
        choices=ASSIGNMENTS,
        default=ASSIGNMENT_OBSERVATIONAL,
        help="How tasks were assigned to cohorts. Only 'randomised' permits a directional claim.",
    )
    parser.add_argument("--cache-ttl-policy", choices=TTL_POLICIES, default=TTL_SPLIT_REQUIRED)
    parser.add_argument(
        "--min-matched",
        type=int,
        default=MIN_MATCHED_PER_GROUP,
        help=f"Reporting floor, matched tasks per arm per risk class (default {MIN_MATCHED_PER_GROUP}).",
    )
    parser.add_argument("--format", choices=("md", "json"), default="md")
    parser.add_argument("--out", default=None, help="Write the report here instead of stdout.")
    parser.add_argument(
        "--fail-on-insufficient",
        action="store_true",
        help="Exit 2 when the verdict is INSUFFICIENT_DATA (for CI; off by default because "
        "INSUFFICIENT_DATA is the correct, expected answer before enough tasks exist).",
    )
    return parser


def _state_dirs(args: argparse.Namespace) -> list[Path]:
    if args.state_dir:
        return [Path(value).expanduser() for value in args.state_dir]
    return [path for path in DEFAULT_STATE_DIRS]


def _collect_sources(args: argparse.Namespace, cohort_map: dict[str, str]) -> list[sources_module.BenchSource]:
    collected: list[sources_module.BenchSource] = []
    seen: set[Path] = set()

    def add_queue(path: Path) -> None:
        if path in seen:
            return
        seen.add(path)
        collected.append(sources_module.QueueDbSource(path, cohort_map=cohort_map))

    def add_usage(path: Path, label: str) -> None:
        if path in seen:
            return
        seen.add(path)
        collected.append(sources_module.UsageDbSource(path, name=label, cohort_map=cohort_map))

    for directory in _state_dirs(args):
        add_queue(directory / "queue.db")
    for value in args.queue_db or ():
        add_queue(Path(value).expanduser())
    # Token sources come after the task spine so the spine's fields win.
    for directory in _state_dirs(args):
        label = f"{directory.parent.name}/{directory.name}"
        add_usage(directory / "work.db", f"work.db ({label})")
        add_usage(directory / "ai_usage.db", f"ai_usage.db ({label})")
    for value in args.work_db or ():
        add_usage(Path(value).expanduser(), "work.db")
    for value in args.ai_usage_db or ():
        add_usage(Path(value).expanduser(), "ai_usage.db")
    telemetry_paths = [Path(value).expanduser() for value in args.work_telemetry_db or ()]
    if not telemetry_paths:
        telemetry_paths = [work_telemetry_default_path()]
        telemetry_paths += [directory / "work_telemetry.db" for directory in _state_dirs(args)]
    for path in telemetry_paths:
        if path in seen:
            continue
        seen.add(path)
        collected.append(
            WorkTelemetryDbSource(path, cohort_map=cohort_map, price_model=args.price_model)
        )
    for value in args.jsonl or ():
        collected.append(sources_module.JsonlFixtureSource(Path(value).expanduser()))
    return collected


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cohort_map = sources_module.load_cohort_map(args.cohort_map)
    except (OSError, ValueError, sources_module.SourceError) as exc:
        print(f"error: could not read --cohort-map: {exc}", file=sys.stderr)
        return 1

    controls = tuple(value.strip() for value in args.controls.split(",") if value.strip())
    groups: list[tuple] = []
    statuses = []
    warnings: list[str] = []
    vocabularies: list[frozenset[str]] = []
    for source in _collect_sources(args, cohort_map):
        result = source.load()
        statuses.append(result.status)
        warnings.extend(result.warnings)
        if result.reason_vocabulary is not None and result.status.available:
            vocabularies.append(result.reason_vocabulary)
        if result.records:
            groups.append(result.records)
    records = sources_module.merge_records(groups)
    # A reason is recordable if ANY contributing source admits it; it is
    # unavailable only when every source that declares a vocabulary
    # rejects it.
    reason_vocabulary = frozenset().union(*vocabularies) if vocabularies else None

    notes = [
        "Sources listed as unavailable are reported, not silently skipped — an empty comparison "
        "always says which telemetry was missing.",
    ]
    try:
        built = report_module.build_report(
            records,
            controls=controls,
            sources=statuses,
            warnings=warnings,
            assignment=args.assignment,
            ttl_policy=args.cache_ttl_policy,
            min_matched_per_group=args.min_matched,
            reason_vocabulary=reason_vocabulary,
            notes=notes,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    text = (
        json.dumps(built.as_dict(), indent=2, sort_keys=False)
        if args.format == "json"
        else report_module.render_markdown(built)
    )
    if args.out:
        Path(args.out).expanduser().write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out} ({built.verdict})")
    else:
        print(text)
    if args.fail_on_insufficient and built.verdict == report_module.INSUFFICIENT:
        return 2
    return 0


def main() -> None:  # pragma: no cover - console script entry
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
