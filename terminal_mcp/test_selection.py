"""Which tests actually have to run for this change, and when the full suite does.

THE COST THIS REMOVES

The regression suite here is ~3757 tests. Running all of it to prove a
three-line change is the same waste as re-reading the repository to make one:
the evidence nobody asked for, paid for in wall-clock time before a human can
look at the result.

So a change selects its tests from what it touched, runs that fast lane first,
and runs FULL_VERIFY once -- before the change is called done, not before the
author can see it working.

FAIL CLOSED, ALWAYS

The dangerous version of this module is the one that quietly selects nothing
and reports green. Every unknown answers FULL_VERIFY rather than an empty set:

  * a changed path no test imports          -> FULL_VERIFY
  * a path outside the package              -> FULL_VERIFY
  * a shared/high-fan-in module             -> FULL_VERIFY
  * an empty or unreadable change list      -> FULL_VERIFY

"Selected nothing" is never an answer. A caller gets either a non-empty fast
lane with the reason each file was picked, or an honest instruction to run
everything with the reason the selection was not trustworthy.

HOW THE MAP IS BUILT

From the test files' own imports, not from a hand-maintained table. A table
drifts the moment a test is renamed, and a drifted table is worse than none --
it reports a confident selection that misses the one test that mattered. The
index is derived on read and cached by mtime, so it cannot disagree with the
tests on disk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

FAST_LANE = "FAST_LANE"
FULL_VERIFY = "FULL_VERIFY"

# Modules so widely imported that "tests that import it" is most of the suite.
# Selecting for these pretends to narrow while narrowing nothing, so they go
# straight to FULL_VERIFY and say so.
HIGH_FAN_IN = frozenset({
    "config", "models", "schema", "core", "controller", "mcp_app",
    "server", "server_http", "permissions", "redaction",
})

# A change to any of these is not a code change with a blast radius a test
# index can describe.
ALWAYS_FULL = (
    "pyproject.toml", "requirements", "conftest.py", "config.example.yaml",
    ".github/", "deploy/",
)

_IMPORT_PATTERNS = (
    # from terminal_mcp.work_spec import ...
    re.compile(r"^\s*from\s+terminal_mcp\.(\w+)\s+import", re.MULTILINE),
    # from terminal_mcp import work_spec as ws, bug_spec
    re.compile(r"^\s*from\s+terminal_mcp\s+import\s+([\w,\s]+)", re.MULTILINE),
    # import terminal_mcp.work_spec
    re.compile(r"^\s*import\s+terminal_mcp\.(\w+)", re.MULTILINE),
    # patch("terminal_mcp.work_spec.thing")
    re.compile(r"[\"']terminal_mcp\.(\w+)", re.MULTILINE),
)


@dataclass
class Selection:
    """What to run, and why -- the reasons are the point.

    A selection a reviewer cannot audit is a selection they have to re-derive,
    which costs more than running the suite.
    """

    lane: str
    tests: list[str] = field(default_factory=list)
    reasons: dict[str, list[str]] = field(default_factory=dict)
    full_verify_because: list[str] = field(default_factory=list)
    unmapped_paths: list[str] = field(default_factory=list)

    @property
    def is_full(self) -> bool:
        return self.lane == FULL_VERIFY

    def as_dict(self) -> dict[str, Any]:
        return {"lane": self.lane, "tests": list(self.tests),
                "reasons": {k: list(v) for k, v in self.reasons.items()},
                "full_verify_because": list(self.full_verify_because),
                "unmapped_paths": list(self.unmapped_paths)}

    def command(self) -> list[str]:
        """The argv a runbook would call. FULL_VERIFY names no paths, so it
        cannot accidentally inherit a narrowed selection."""
        if self.is_full:
            return ["pytest", "-q"]
        return ["pytest", "-q", *self.tests]


def _module_of(path: str) -> str | None:
    """`terminal_mcp/work_spec.py` -> `work_spec`; anything else -> None."""
    parts = Path(str(path)).parts
    if "terminal_mcp" not in parts:
        return None
    index = parts.index("terminal_mcp")
    rest = parts[index + 1:]
    if len(rest) != 1 or not rest[0].endswith(".py"):
        # A package subdirectory (vendor/, etc.) is not a module this index
        # describes; the caller gets FULL_VERIFY rather than a wrong guess.
        return None
    return rest[0][:-3]


def build_index(tests_dir: str | Path) -> dict[str, set[str]]:
    """module name -> the test files that import it.

    Derived from the files on disk every call. The suite is a few hundred
    files of a few hundred lines; reading them costs milliseconds and removes
    a whole class of drift.
    """
    root = Path(tests_dir)
    index: dict[str, set[str]] = {}
    if not root.is_dir():
        return index

    for test_file in sorted(root.rglob("test_*.py")):
        try:
            text = test_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # Unreadable is not empty: a caller that cannot see a test file
            # must not be told nothing imports the module.
            continue
        rel = str(test_file.relative_to(root.parent)) if root.parent in test_file.parents \
            else str(test_file)
        for pattern in _IMPORT_PATTERNS:
            for match in pattern.finditer(text):
                for name in match.group(1).split(","):
                    name = name.strip().split(" as ")[0].strip()
                    if name and name.isidentifier():
                        index.setdefault(name, set()).add(rel)
    return index


def select(changed_paths: Sequence[str], *, tests_dir: str | Path = "tests",
           index: dict[str, set[str]] | None = None) -> Selection:
    """Pick the fast lane for this change, or say why it has to be everything.

    Every branch that cannot describe the blast radius returns FULL_VERIFY with
    a reason. There is deliberately no branch that returns an empty fast lane.
    """
    paths = [str(p) for p in changed_paths if str(p).strip()]
    if not paths:
        return Selection(lane=FULL_VERIFY,
                         full_verify_because=["no changed paths were supplied"])

    forced: list[str] = []
    for path in paths:
        for marker in ALWAYS_FULL:
            if marker in path:
                forced.append(f"{path} ({marker} affects how everything runs)")
    if forced:
        return Selection(lane=FULL_VERIFY, full_verify_because=forced)

    index = build_index(tests_dir) if index is None else index

    tests: dict[str, list[str]] = {}
    unmapped: list[str] = []
    reasons_full: list[str] = []

    for path in paths:
        # A changed test file always runs itself. The directory is what makes
        # it a test, not the basename: `terminal_mcp/test_selection.py` is a
        # source module whose name happens to start with `test_`, and treating
        # it as a test both runs a non-test file and skips the tests that
        # actually cover it.
        parts = Path(path).parts
        if "tests" in parts and Path(path).name.startswith("test_") and path.endswith(".py"):
            tests.setdefault(path, []).append("the changed test itself")
            continue

        module = _module_of(path)
        if module is None:
            unmapped.append(path)
            reasons_full.append(f"{path} is outside terminal_mcp/*.py; blast radius unknown")
            continue
        if module in HIGH_FAN_IN:
            reasons_full.append(
                f"{path} is high fan-in ({module}); narrowing here narrows nothing")
            continue

        importers = index.get(module) or set()
        if not importers:
            unmapped.append(path)
            reasons_full.append(f"no test imports {module}; cannot prove this change narrowly")
            continue
        for test_file in sorted(importers):
            tests.setdefault(test_file, []).append(f"imports {module}")

    if reasons_full:
        return Selection(lane=FULL_VERIFY, full_verify_because=reasons_full,
                         unmapped_paths=unmapped,
                         tests=sorted(tests), reasons=tests)

    if not tests:
        # Unreachable by construction today -- every path above either adds a
        # test or a FULL_VERIFY reason. Kept because "selected nothing and
        # reported green" is the one outcome this module exists to prevent.
        return Selection(lane=FULL_VERIFY,
                         full_verify_because=["selection produced no tests"])

    return Selection(lane=FAST_LANE, tests=sorted(tests), reasons=tests)


def plan(changed_paths: Sequence[str], *, tests_dir: str | Path = "tests",
         index: dict[str, set[str]] | None = None) -> dict[str, Any]:
    """The two-stage instruction a worker follows.

    The fast lane is for the author's loop. FULL_VERIFY is not optional and not
    replaced by it -- it is what runs before the change is called done, so the
    narrowing never becomes the only evidence.
    """
    selection = select(changed_paths, tests_dir=tests_dir, index=index)
    return {
        "selection": selection.as_dict(),
        "stages": [
            {"stage": "fast", "lane": selection.lane,
             "command": selection.command(),
             "why": ("the tests that import what changed -- run this while working"
                     if not selection.is_full else
                     "the change could not be narrowed; see full_verify_because")},
            {"stage": "full", "lane": FULL_VERIFY, "command": ["pytest", "-q"],
             "why": "runs before the change is called done, never replaced by the fast lane"},
        ],
    }
