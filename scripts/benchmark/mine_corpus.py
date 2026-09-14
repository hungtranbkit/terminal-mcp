#!/usr/bin/env python3
"""Build the token-efficiency benchmark corpus out of this repository's own history.

WHY MINED, NOT WRITTEN. A benchmark whose cases were invented by the person
being measured proves nothing: the cases drift, without anyone intending it,
towards the shape the system happens to handle well. Every REAL case here is
a commit that actually fixed something in this repository, and the two facts
that decide whether the system helped -- what the bug was called and which
files the fix touched -- are read out of git rather than supplied.

WHAT IS RECORDED PER CASE, and where each field comes from:

  commit/parent/date/subject   git, verbatim
  fix_paths                    `git show --name-only`, filtered to SOURCE
                               files (see _is_source): tests and docs are
                               dropped because a benchmark about locating a
                               defect should not be scored on finding the
                               test that was updated alongside it
  category                     assigned HERE, by hand, in CATEGORIES below --
                               the one judged field, kept in one visible
                               place rather than spread through the corpus
  origin                       REAL for a mined commit; a SYNTHETIC case is
                               written into the corpus file by hand and says
                               so in its own `origin` and `note`

The subject line is used as the bug's SYMPTOM, which is deliberately
generous to the unassisted baseline: a commit subject is written after the
fix, with hindsight, and frequently names the module or the symbol. A real
bug report rarely does. So a baseline measured from it is an EASIER baseline
than reality, and any advantage the assisted path still shows is understated
rather than flattered.

Run: python3 scripts/benchmark/mine_corpus.py > benchmarks/tokeff_corpus.json
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# (commit, category) -- the hand-assigned half, all of it, in one place.
# Categories are the six the benchmark task names. `unknown` means exactly
# that: a bug whose shape did not announce itself, including one whose fix
# turned out not to live in the Python package at all.
CATEGORIES: list[tuple[str, str]] = [
    # -- UI: what a person sees in the dashboard or the web terminal --------
    ("a25834e", "ui"),
    ("9970f97", "ui"),
    ("600c9d7", "ui"),
    ("dba2d05", "ui"),
    # -- session lifecycle --------------------------------------------------
    ("805d644", "session"),
    ("8f9d03a", "session"),
    ("4d51782", "session"),
    # -- auth / permissions / identity --------------------------------------
    ("c6e2e63", "auth"),
    ("5588feb", "auth"),
    ("8fca313", "auth"),
    # -- backend internals ---------------------------------------------------
    ("998ab63", "backend"),
    ("32c77ac", "backend"),
    ("d160eb0", "backend"),
    # -- simple logic: one function got one condition wrong ------------------
    ("b107c94", "simple_logic"),
    ("fe53f97", "simple_logic"),
    ("12d41d4", "simple_logic"),
    # -- unknown: cross-cutting, or not where anyone would have looked -------
    ("184b7e0", "unknown"),
    ("4a3ca43", "unknown"),
]

# The two cases history could not supply cleanly. Both are SYNTHETIC, say so
# in their own `origin`, and are reported SEPARATELY from the real aggregate
# -- they bracket the range rather than contributing to the headline.
SYNTHETIC_CASES: list[dict[str, object]] = [
    {
        "case_id": "synthetic-repeat-glyphs",
        "origin": "SYNTHETIC",
        "category": "ui",
        "commit": "",
        "parent": "",
        "date": "",
        # The claim under test is "the SECOND bug in a module costs less than
        # the first". Real history rarely reports the same defect twice in
        # comparable words, so this restates a real one (9970f97) the way a
        # user would report it, with no commit behind it.
        "symptom": ("box drawing characters show as empty tofu boxes in the web "
                    "terminal when the session is on a Windows node"),
        "fix_paths": ["terminal_mcp/dashboard.py"],
        "touched_total": 0,
        "mirrors": "real-9970f97",
        "note": ("SYNTHETIC. Restates real-9970f97 as a user would report it, to "
                 "measure the repeat-bug case directly. Its fix paths are that "
                 "commit's own, not a guess."),
    },
    {
        "case_id": "synthetic-unmapped-bridge",
        "origin": "SYNTHETIC",
        "category": "unknown",
        "commit": "",
        "parent": "",
        "date": "",
        # The other end of the range: a real module (bridge.py) that the
        # knowledge map does not cover at all, so retrieval has nothing to
        # offer and the measurement should show exactly that.
        "symptom": ("the ask-ChatGPT bridge stops answering after a while and no "
                    "receipt is ever written for the turn"),
        "fix_paths": ["terminal_mcp/bridge.py"],
        "touched_total": 0,
        "note": ("SYNTHETIC. No commit behind it. Written to bracket the case "
                 "where the knowledge map covers nothing relevant -- the module "
                 "is real and genuinely unmapped."),
    },
]

_TEST_OR_DOC = ("tests/", "docs/", ".projectflow/")


def _is_source(path: str) -> bool:
    """A file a worker would have to FIND to fix the bug.

    Tests and documentation are excluded: they are usually changed in the
    same commit, and counting them would let a benchmark claim the system
    located the defect when all it located was the test that noticed it.
    """
    if path.startswith(_TEST_OR_DOC) or path.endswith(".md"):
        return False
    return bool(path.strip())


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def mine(commit: str, category: str) -> dict[str, object]:
    full = _git("rev-parse", commit)
    parent = _git("rev-parse", f"{commit}^")
    subject = _git("log", "-1", "--format=%s", full)
    date = _git("log", "-1", "--format=%cI", full)
    touched = [line for line in
               _git("show", "--name-only", "--format=", full).splitlines() if line.strip()]
    return {
        "case_id": f"real-{full[:7]}",
        "origin": "REAL",
        "category": category,
        "commit": full[:12],
        "parent": parent[:12],
        "date": date,
        # The bug as it was named. Not rewritten, not summarised.
        "symptom": subject,
        "fix_paths": [p for p in touched if _is_source(p)],
        "touched_total": len(touched),
        "note": "",
    }


def main() -> int:
    cases = [mine(commit, category) for commit, category in CATEGORIES]
    cases.extend(SYNTHETIC_CASES)
    missing = [c["case_id"] for c in cases if not c["fix_paths"]]
    if missing:
        print(f"cases with no source file touched: {missing}", file=sys.stderr)
    payload = {
        "corpus_version": 1,
        "repository": "terminal-mcp",
        "generated_by": "scripts/benchmark/mine_corpus.py",
        "cases": cases,
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
