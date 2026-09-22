"""Identify -- and, only under an explicit apply flag, purge -- session_
registry rows that are provably artifacts of this project's own test
suite.

Background (backlog blg_80635ab65719): this host's production
session_registry.db accumulated hundreds of test-named rows. Two separate
mechanisms produced them, and only one of them was ever a bug in the
tests themselves:

  1. A test process whose stores defaulted to the real ~/.local/state
     path. Fixed earlier, in tests/conftest.py's pytest_configure
     (XDG_STATE_HOME redirect).
  2. A test process creating real tmux sessions on the SHARED default
     tmux server, which this host's own separately-running service then
     observed during its normal reconcile pass and recorded in the
     PRODUCTION registry. A different process entirely -- nothing inside
     the test process could prevent it. Fixed alongside this module, by
     giving the suite its own tmux server socket (tmux.py's
     TERMINAL_MCP_TMUX_SOCKET / conftest's pytest_configure).

With (2) fixed the pollution stops accruing, but the rows already
written stay. This module is the one-off remediation for those, and it
is deliberately built to be boring and refusable rather than clever:

**Dry run is the default.** Nothing is ever written unless `--apply` is
passed AND `--confirm-count` matches the number of rows the plan
actually selected. The second flag exists because a dry run and an apply
are two separate invocations against a LIVE database: rows can appear or
age in between, and "delete whatever matches right now" would happily
delete a different, larger set than the human reviewed. Requiring the
reviewed count to still be exact turns any drift into a refusal.

**Selection is by proof, not by resemblance.** A row is selected only if
its session name is an exact literal that appears in this project's own
test sources -- mined from the AST of tests/*.py, not guessed from a
prefix pattern. "test-*"-style prefix matching was measured against the
real database and rejected: it both misses genuinely test-created rows
(`rn-*`, `mn-*`, `reg-*`, `ctrl-*` ...) and, far worse, sweeps in real
session names. Even exact-literal matching is not trusted on its own --
`terminal-mcp` and `mesflow` are BOTH real sessions on this host AND
literals inside the test suite -- so every candidate then has to survive
every guard in `GUARDS` before it can be selected.

Anything that looks test-shaped but fails a guard is not silently
dropped: it is reported in its own "needs review" bucket, with the guard
that rejected it, so a human decides. This module never decides.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .session_registry import SessionRegistryStore, default_session_registry_path

# Statuses a row must be in to be even considered. ACTIVE means the
# session was alive as of the last reconcile pass; OFFLINE means its node
# is unreachable and its fate is therefore UNKNOWN (session_registry.py's
# own wording) -- neither is ever a safe thing to purge, and DELETED is
# already a tombstone.
TERMINAL_STATUSES = frozenset({"MISSING", "KILLED"})

DEFAULT_MIN_AGE_DAYS = 7

_NAME_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9._-]{1,60}\Z")


@dataclass(frozen=True)
class Candidate:
    node_id: str
    session_name: str
    status: str
    created_at: str | None
    last_seen_at: str | None
    stable_session_id: str | None
    conversation_id: str | None
    auto_recovery_enabled: Any
    cwd: str | None
    age_days: float | None
    sources: tuple[str, ...]
    """Test files whose source contains this exact session name -- the
    evidence for calling the row a test artifact, reported so a reviewer
    can check the claim instead of taking it on faith."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "session_name": self.session_name,
            "status": self.status,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "stable_session_id": self.stable_session_id,
            "age_days": None if self.age_days is None else round(self.age_days, 1),
            "cwd": self.cwd,
            "sources": list(self.sources),
        }


@dataclass
class Plan:
    selected: list[Candidate] = field(default_factory=list)
    needs_review: list[tuple[Candidate, str]] = field(default_factory=list)
    total_rows: int = 0
    inventory_size: int = 0
    min_age_days: float = DEFAULT_MIN_AGE_DAYS

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "inventory_size": self.inventory_size,
            "min_age_days": self.min_age_days,
            "selected_count": len(self.selected),
            "needs_review_count": len(self.needs_review),
            "selected": [c.as_dict() for c in self.selected],
            "needs_review": [{**c.as_dict(), "held_back_by": why}
                             for c, why in self.needs_review],
        }


# --------------------------------------------------------------- inventory
SESSION_NAME_CALLS = frozenset({
    # the canonical disposable-session fixture (and the local name tests
    # bind it to), plus the real client/service methods that take a
    # session name as their first positional argument.
    "tmux_session_factory", "create", "make_session", "session_factory",
    "new_session", "kill_session", "get_session", "rename_session",
})
"""Calls whose first positional string argument IS a tmux session name.

Mining ANY string literal in the test sources was tried first and is
wrong in the one direction that matters. `quan_ly_ban_hang` is a real
agent session on this host, carefully backfilled by a human audit with
full provenance in its `notes`; it also appears in
test_session_knowledge.py -- as a *search query string* and a *temp
directory name*, never as a session name. A whole-file literal scan
therefore SELECTED a real row for purge. Restricting the mine to
argument positions that genuinely name a session is what makes "provable
test artifact" a true statement rather than a plausible one."""


def _session_name_literals(tree: ast.AST) -> set[str]:
    names: set[str] = set()

    def _take(node: ast.AST | None) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if _NAME_RE.match(value):
                names.add(value)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if called in SESSION_NAME_CALLS:
            if node.args:
                _take(node.args[0])
            for kw in node.keywords:
                if kw.arg in ("name", "session", "session_name", "old", "new"):
                    _take(kw.value)
        elif called == "tmux":
            # conftest's raw helper: tmux("new-session", "-d", "-s", NAME)
            # / tmux("kill-session", "-t", NAME) -- the literal that
            # follows a -s/-t flag is the session name.
            for index, arg in enumerate(node.args[:-1]):
                if isinstance(arg, ast.Constant) and arg.value in ("-s", "-t"):
                    _take(node.args[index + 1])
    return names


def mine_test_session_names(tests_dir: pathlib.Path) -> dict[str, tuple[str, ...]]:
    """String literals the test sources actually pass as a tmux SESSION
    NAME, mapped to the files they appear in.

    Parsed with `ast`, not grepped: a regex over source text also matches
    names inside comments and docstrings (this project's are long and
    quote real session names while describing real incidents) -- exactly
    the rows that must NOT be selected.

    f-string session names (`f"lane-{uuid4().hex}"`) are deliberately NOT
    reduced to their static prefix: a prefix is a pattern, and patterns
    are what this module exists to avoid. Rows from those tests stay
    unselected and get reported for human review instead.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(tests_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for name in _session_name_literals(tree):
            found.setdefault(name, set()).add(str(path))
    return {name: tuple(sorted(files)) for name, files in found.items()}


# ------------------------------------------------------------------ guards
def _age_days(row: dict[str, Any], now: datetime) -> float | None:
    stamp = row.get("last_seen_at") or row.get("created_at")
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds() / 86400.0


def _guard_status(row, ctx):
    if row["status"] not in TERMINAL_STATUSES:
        return f"status={row['status']} (only {'/'.join(sorted(TERMINAL_STATUSES))} are eligible)"
    return None


def _guard_not_live(row, ctx):
    if row["session_name"] in ctx["live_sessions"]:
        return "a tmux session by this name is alive right now"
    return None


def _guard_protected(row, ctx):
    if row["session_name"] in ctx["protected"]:
        return "listed in session_lifecycle.protected_sessions"
    return None


def _guard_no_conversation(row, ctx):
    if row.get("conversation_id"):
        return "has a conversation_id (a real agent session with resumable history)"
    return None


def _guard_no_auto_recovery(row, ctx):
    if row.get("auto_recovery_enabled"):
        return "auto_recovery_enabled (a session something is meant to bring back)"
    return None


def _guard_not_backfilled(row, ctx):
    """A non-empty `notes` means a human (or an audit acting for one)
    deliberately wrote provenance onto this row -- upsert_manual's
    backfill path, session_registry.py items 11/13. Those rows exist
    precisely BECAUSE the session itself vanished and someone
    reconstructed it from audit logs; purging one destroys the only
    surviving record of a real session. Caught live: `quan_ly_ban_hang`,
    a real OfflinePOS agent session, whose notes cite the audit.db entry
    and git remote it was reconstructed from."""
    if str(row.get("notes") or "").strip():
        return "has curated notes (a deliberately backfilled/annotated record)"
    return None


def _guard_age(row, ctx):
    age = row["_age_days"]
    if age is None:
        return "no usable last_seen_at/created_at to age-check"
    if age < ctx["min_age_days"]:
        return f"only {age:.1f}d old (min {ctx['min_age_days']}d)"
    return None


GUARDS: tuple[tuple[str, Callable[[dict, dict], str | None]], ...] = (
    ("status", _guard_status),
    ("live", _guard_not_live),
    ("protected", _guard_protected),
    ("conversation", _guard_no_conversation),
    ("auto_recovery", _guard_no_auto_recovery),
    ("backfilled", _guard_not_backfilled),
    ("age", _guard_age),
)
"""Every guard runs against every name-matched row, and ALL must pass.

Each one is here because it was measured against the real polluted
database, not hypothesised: `terminal-mcp` is a test literal and the
protected control session (protected); `mesflow` and `mcp` are test
literals and real agent sessions carrying a conversation_id
(conversation). Without those two guards an exact-name match alone would
have selected all three."""


def live_session_names(tmux_binary: str = "tmux") -> frozenset[str]:
    """Names alive on the tmux server this process talks to.

    Best-effort by design: if tmux is missing or there is no server, the
    honest answer is "no live sessions", and every other guard still
    applies. It is additive safety, never the only thing standing between
    a real session and a purge."""
    from .tmux import default_socket_name
    socket_name = default_socket_name()
    argv = [tmux_binary, *(("-L", socket_name) if socket_name else ()),
            "list-sessions", "-F", "#{session_name}"]
    try:
        result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    return frozenset(line.strip() for line in result.stdout.splitlines() if line.strip())


# -------------------------------------------------------------------- plan
def build_plan(db_path: pathlib.Path, inventory: dict[str, tuple[str, ...]], *,
               protected: Iterable[str] = (), min_age_days: float = DEFAULT_MIN_AGE_DAYS,
               live: frozenset[str] | None = None,
               now: datetime | None = None) -> Plan:
    """Read-only. Opens the database in SQLite `mode=ro` so that even a
    bug in this function cannot write to it -- a dry run against
    production must be incapable of mutation, not merely careful."""
    now = now or datetime.now(timezone.utc)
    ctx = {
        "live_sessions": frozenset() if live is None else live,
        "protected": frozenset(protected),
        "min_age_days": min_age_days,
    }
    plan = Plan(inventory_size=len(inventory), min_age_days=min_age_days)
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in connection.execute("SELECT * FROM session_records")]
    finally:
        connection.close()
    plan.total_rows = len(rows)

    for row in rows:
        name = row.get("session_name") or ""
        sources = inventory.get(name)
        if sources is None:
            continue  # not provably ours -- not our business
        row["_age_days"] = _age_days(row, now)
        candidate = Candidate(
            node_id=row.get("node_id") or "",
            session_name=name,
            status=row.get("status") or "",
            created_at=row.get("created_at"),
            last_seen_at=row.get("last_seen_at"),
            stable_session_id=row.get("stable_session_id"),
            conversation_id=row.get("conversation_id"),
            auto_recovery_enabled=row.get("auto_recovery_enabled"),
            cwd=row.get("cwd"),
            age_days=row["_age_days"],
            sources=sources,
        )
        rejection = next((why for _, guard in GUARDS if (why := guard(row, ctx))), None)
        if rejection is None:
            plan.selected.append(candidate)
        else:
            plan.needs_review.append((candidate, rejection))

    plan.selected.sort(key=lambda c: (c.node_id, c.session_name))
    plan.needs_review.sort(key=lambda item: (item[0].node_id, item[0].session_name))
    return plan


def apply_plan(db_path: pathlib.Path, plan: Plan, *, purged_by: str) -> int:
    """Purge every selected row, through SessionRegistryStore.purge() --
    the module's own single hard-delete path, which leaves a DELETED
    tombstone plus a note naming who purged it and when. A bare SQL
    DELETE here would be faster and would destroy exactly the audit trail
    that makes a bulk purge reviewable after the fact."""
    store = SessionRegistryStore(db_path)
    purged = 0
    for candidate in plan.selected:
        if store.purge(candidate.node_id, candidate.session_name, purged_by=purged_by):
            purged += 1
    return purged


# --------------------------------------------------------------------- cli
def _render(plan: Plan, *, verbose: bool) -> str:
    out: list[str] = []
    out.append(f"registry rows scanned      : {plan.total_rows}")
    out.append(f"test-name inventory size   : {plan.inventory_size}")
    out.append(f"min age required           : {plan.min_age_days}d")
    out.append(f"SELECTED (provable)        : {len(plan.selected)}")
    out.append(f"needs review (held back)   : {len(plan.needs_review)}")
    if plan.selected:
        out.append("")
        out.append("-- selected --")
        shown = plan.selected if verbose else plan.selected[:20]
        for c in shown:
            age = "?" if c.age_days is None else f"{c.age_days:.0f}d"
            src = pathlib.Path(c.sources[0]).name if c.sources else "?"
            more = f" +{len(c.sources) - 1}" if len(c.sources) > 1 else ""
            out.append(f"   {c.node_id}/{c.session_name}  [{c.status}] age={age} "
                       f"id={c.stable_session_id or '-'} src={src}{more}")
        if len(shown) < len(plan.selected):
            out.append(f"   ... {len(plan.selected) - len(shown)} more (use --verbose)")
    if plan.needs_review:
        out.append("")
        out.append("-- held back for human review (never auto-selected) --")
        shown = plan.needs_review if verbose else plan.needs_review[:20]
        for c, why in shown:
            out.append(f"   {c.node_id}/{c.session_name}  [{c.status}] -> {why}")
        if len(shown) < len(plan.needs_review):
            out.append(f"   ... {len(plan.needs_review) - len(shown)} more (use --verbose)")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="terminal-mcp-registry-cleanup",
        description="Report (and only with --apply, purge) session_registry rows that are "
                    "provably artifacts of this project's own test suite. Dry run by default.")
    parser.add_argument("--db", type=pathlib.Path, default=None,
                        help="session_registry.db (default: this host's real registry path)")
    parser.add_argument("--tests-dir", type=pathlib.Path, default=None,
                        help="test sources to mine session names from (default: <repo>/tests)")
    parser.add_argument("--min-age-days", type=float, default=DEFAULT_MIN_AGE_DAYS,
                        help=f"skip rows seen more recently than this (default {DEFAULT_MIN_AGE_DAYS})")
    parser.add_argument("--protected", action="append", default=[],
                        help="extra session name to never select (repeatable)")
    parser.add_argument("--json", action="store_true", help="machine-readable plan on stdout")
    parser.add_argument("--verbose", action="store_true", help="list every row, not just the first 20")
    parser.add_argument("--apply", action="store_true",
                        help="actually purge the selected rows (requires --confirm-count)")
    parser.add_argument("--confirm-count", type=int, default=None,
                        help="the SELECTED count from the dry run you reviewed; the apply "
                             "refuses if the live plan no longer matches it exactly")
    args = parser.parse_args(argv)

    db_path = args.db or default_session_registry_path()
    if not pathlib.Path(db_path).exists():
        print(f"no registry database at {db_path}", file=sys.stderr)
        return 2

    tests_dir = args.tests_dir or (pathlib.Path(__file__).resolve().parent.parent / "tests")
    if not tests_dir.is_dir():
        print(f"no test sources at {tests_dir} -- pass --tests-dir", file=sys.stderr)
        return 2

    protected = set(args.protected)
    try:  # the real config's protected_sessions, when this runs in a real deployment
        from .config import load_config
        config = load_config(os.environ.get("TERMINAL_MCP_CONFIG", "config.yaml"))
        protected |= set(getattr(config.session_lifecycle, "protected_sessions", ()) or ())
    except Exception:
        protected |= {"terminal-mcp"}  # config.py's own unconditional floor

    inventory = mine_test_session_names(tests_dir)
    plan = build_plan(db_path, inventory, protected=protected,
                      min_age_days=args.min_age_days, live=live_session_names())

    if args.json:
        print(json.dumps(plan.as_dict(), indent=2))
    else:
        print(_render(plan, verbose=args.verbose))

    if not args.apply:
        # stderr, so `--json` stdout stays a single parseable document
        # for anything piping this into jq or a review script.
        print("\nDRY RUN -- nothing was written.", file=sys.stderr)
        print(f"To apply, re-run with:  --apply --confirm-count {len(plan.selected)}",
              file=sys.stderr)
        return 0

    if args.confirm_count is None:
        print("\nrefusing to apply: --apply requires --confirm-count "
              f"{len(plan.selected)} (the count you reviewed in the dry run)", file=sys.stderr)
        return 3
    if args.confirm_count != len(plan.selected):
        print(f"\nrefusing to apply: --confirm-count {args.confirm_count} but the live plan "
              f"now selects {len(plan.selected)}. The database changed since the dry run -- "
              "re-review it.", file=sys.stderr)
        return 3

    purged = apply_plan(db_path, plan, purged_by="registry-cleanup")
    print(f"\nAPPLIED -- purged {purged} row(s) to DELETED tombstones.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
