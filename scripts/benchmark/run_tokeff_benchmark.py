#!/usr/bin/env python3
"""Run the token-efficiency benchmark and write its report.

Reads the corpus, measures every case against THIS repository (real git
greps at each fix's parent commit, the real context pack against the real
knowledge map), and writes both the machine-readable result and the markdown
report a person reads.

    python3 scripts/benchmark/run_tokeff_benchmark.py \
        --out docs/TOKEFF_BENCHMARK.md --json benchmarks/tokeff_result.json

Nothing here is deployed and nothing is written outside the paths given. The
telemetry store is opened READ-ONLY-in-effect: the benchmark only ever asks
it whether a provider reported counters for these cases.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from terminal_mcp import efficiency_benchmark as eb          # noqa: E402
from terminal_mcp.project_knowledge import ProjectKnowledge  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--corpus", default="benchmarks/tokeff_corpus.json")
    parser.add_argument("--out", default="docs/TOKEFF_BENCHMARK.md")
    parser.add_argument("--json", dest="json_out", default="benchmarks/tokeff_result.json")
    parser.add_argument("--telemetry-db", default="",
                        help="optional telemetry database to look for real "
                             "provider counters in")
    args = parser.parse_args()

    repo = Path(args.repo)
    cases = eb.load_corpus(Path(args.corpus))
    validation = eb.validate_corpus(cases, repo_root=repo)
    if not validation["ok"]:
        print("corpus validation failed:", file=sys.stderr)
        for problem in validation["problems"]:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    # Really ask a real telemetry database whether any provider ever reported
    # counters for these cases. One is NEVER created here: creating an empty
    # store and reading zero out of it would turn "nothing was recorded" into
    # a measurement, which is the one thing this benchmark must not do.
    from terminal_mcp.work_telemetry import TelemetryStore, default_telemetry_db_path

    telemetry = None
    telemetry_path = Path(args.telemetry_db) if args.telemetry_db \
        else default_telemetry_db_path()
    if telemetry_path.exists():
        telemetry = TelemetryStore(telemetry_path)
        print(f"reading provider counters from {telemetry_path}")
    else:
        print(f"no telemetry database at {telemetry_path} -- provider usage will "
              f"report UNAVAILABLE, which is the honest answer for tasks that "
              f"predate it")

    with tempfile.TemporaryDirectory(prefix="tokeff-bench-") as tmp:
        report = eb.run_benchmark(
            cases, repo_root=repo, knowledge=ProjectKnowledge(repo),
            store_path=Path(tmp) / "specs.db", telemetry_store=telemetry)
    report["corpus_validation"] = validation

    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(eb.render_markdown(report), encoding="utf-8")
    print(f"verdict: {report['acceptance']['verdict']} "
          f"(token claim {report['acceptance']['token_claim']['status']})")
    print(f"wrote {args.out} and {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
