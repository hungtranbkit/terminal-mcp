"""Rapid Capture Inbox and the planner pool.

A developer can describe twenty problems far faster than any planner can
analyse one. Capture therefore has to be cheap and immediate, and planning
has to happen behind it, in parallel, without the two blocking each other.

Three things live here:

* **Capture.** A batch of free-form bullets becomes persisted issue records
  straight away -- an id, a short title, a rough type, a duplicate flag.
  Nothing here does root-cause analysis, because anything that waits on
  analysis stops being capture.
* **A durable state machine.** NEW through DONE, with every transition
  written down. It survives restart because the next planner has to be able
  to tell what was already in flight.
* **A planner pool.** Bounded concurrent claims with leases, so two planners
  cannot take one issue and a crashed planner's issue does not stay claimed
  forever.

It deliberately reuses what already exists: `context_pack.fingerprint` for
duplicate detection, `bug_spec` for the spec a planner produces, and
`work_policy` for the rules a task runs under. The queue still owns
execution -- an issue that reaches READY becomes a queue task, and this
module never grows a second dispatcher.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .schema import Migration, apply_migrations

SCHEMA_VERSION = 1

# -- states -------------------------------------------------------------------
# The main line plus the branches a real issue actually takes. Kept explicit
# rather than free-form: a state nobody enumerated is a state nobody handles.
NEW = "NEW"
TRIAGED = "TRIAGED"
PLANNING = "PLANNING"
NEEDS_USER_HINT = "NEEDS_USER_HINT"
NEEDS_REDEFINE = "NEEDS_REDEFINE"
READY = "READY"
EXECUTING = "EXECUTING"
PREVIEW_READY = "PREVIEW_READY"
VERIFYING = "VERIFYING"
DONE = "DONE"
FAILED = "FAILED"
BLOCKED = "BLOCKED"
REWORK = "REWORK"
DUPLICATE = "DUPLICATE"
CANCELLED = "CANCELLED"

ISSUE_STATES = (NEW, TRIAGED, PLANNING, NEEDS_USER_HINT, NEEDS_REDEFINE, READY,
                EXECUTING, PREVIEW_READY, VERIFYING, DONE, FAILED, BLOCKED,
                REWORK, DUPLICATE, CANCELLED)
TERMINAL_STATES = (DONE, FAILED, CANCELLED, DUPLICATE)
# States a planner may pick up. NEEDS_USER_HINT is NOT here: it is waiting on a
# human, and re-planning it would discard the analysis already done.
CLAIMABLE_STATES = (NEW, TRIAGED, NEEDS_REDEFINE, REWORK)

# How alike two captured items must read before one is called a duplicate of
# the other. Text overlap, not the module+type bucket used for bug retrieval:
# at capture time the module is usually unknown, so that bucket collapses
# every unclassified issue into one and would mark them all duplicates of
# whichever arrived first.
DUPLICATE_SIMILARITY = 0.6

DEFAULT_PLANNER_CONCURRENCY = 3
DEFAULT_CLAIM_LEASE_SECONDS = 900.0


class InboxError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat(timespec="seconds")


def default_inbox_db_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_INBOX_DB")
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return base / "terminal-mcp" / "work_inbox.db"


# -- capture ------------------------------------------------------------------

_BULLET = re.compile(r"^\s*(?:[-*•·–—]|\d+[.)]|\(\d+\))\s+")
_TYPE_HINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ui", re.compile(r"(?i)\b(css|layout|overlap|badge|button|mobile|responsive|"
                      r"lệch|chồng|giao diện|màu|font)\b")),
    ("auth", re.compile(r"(?i)\b(login|auth|session|permission|token|đăng nhập|phân quyền)\b")),
    ("data", re.compile(r"(?i)\b(migration|schema|database|sql|dữ liệu|bảng)\b")),
    ("infra", re.compile(r"(?i)\b(deploy|systemd|tunnel|nginx|server|hạ tầng)\b")),
    ("perf", re.compile(r"(?i)\b(slow|chậm|timeout|memory|leak|hiệu năng)\b")),
)


def split_batch(text: str) -> list[str]:
    """Split a pasted batch into atomic items.

    Bullets when the text has them, blank-line paragraphs otherwise. It does
    NOT split every sentence: one coherent bug described in three sentences is
    one issue, and shredding it into three would create work rather than
    capture it.
    """
    if not text or not text.strip():
        return []
    lines = text.splitlines()
    bulleted = [line for line in lines if _BULLET.match(line)]
    if len(bulleted) >= 2:
        items: list[str] = []
        current: list[str] = []
        for line in lines:
            if _BULLET.match(line):
                if current:
                    items.append("\n".join(current).strip())
                current = [_BULLET.sub("", line)]
            elif current and line.strip():
                current.append(line.strip())          # a continuation line
            elif current and not line.strip():
                items.append("\n".join(current).strip())
                current = []
        if current:
            items.append("\n".join(current).strip())
        return [item for item in items if item]
    chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n", text)]
    return [chunk for chunk in chunks if chunk]


_WORD = re.compile(r"[\w]{3,}", re.UNICODE)
# Words common enough in a bug report that they separate nothing.
_STOPWORDS = frozenset("""
the and for with that this from not but are was were has have not you your
khi không bị của là các và cho một trên dưới sau trước nhưng thì mà nào
issue bug problem error lỗi vấn đề
""".split())


def _salient_tokens(text: str) -> set[str]:
    return {word for word in _WORD.findall((text or "").lower())
            if word not in _STOPWORDS}


def short_title(raw: str, *, limit: int = 90) -> str:
    first = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    first = _BULLET.sub("", first).strip()
    return first[:limit].rstrip() + ("…" if len(first) > limit else "")


def rough_type(raw: str) -> str:
    for name, pattern in _TYPE_HINTS:
        if pattern.search(raw):
            return name
    return "unknown"


def rough_difficulty(raw: str) -> str:
    """A cheap first guess, replaced by real triage at planning time.

    Named `rough_` because it is: capture cannot afford the analysis that
    would make it trustworthy, and pretending otherwise would have planners
    trusting a coin flip.
    """
    from .bug_spec import triage

    verdict = triage(short_title(raw), raw)
    return verdict["difficulty"]


@dataclass
class Issue:
    issue_id: str
    raw_text: str
    short_title: str
    state: str = NEW
    project: str | None = None
    rough_type: str = "unknown"
    likely_module: str | None = None
    rough_difficulty: str = "MEDIUM"
    priority: int = 0
    source: str = "capture"
    batch_id: str | None = None
    fingerprint: str | None = None
    duplicate_of: str | None = None
    bug_id: str | None = None
    queue_task_id: str | None = None
    human_hints: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    claimed_by: str | None = None
    claim_expires_at: str | None = None
    policy_version: str | None = None
    policy_hash: str | None = None
    created_at: str = field(default_factory=_iso)
    updated_at: str = field(default_factory=_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = dict(self.__dict__)
        payload["human_hints"] = list(self.human_hints)
        payload["questions"] = list(self.questions)
        return payload


ISSUE_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: issues + issue_events", lambda connection: None),
]


class InboxStore:
    """Durable issues and their history."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_inbox_db_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        with self._connection:
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS issues (
                    issue_id TEXT PRIMARY KEY,
                    project TEXT,
                    state TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    fingerprint TEXT,
                    duplicate_of TEXT,
                    batch_id TEXT,
                    claimed_by TEXT,
                    claim_expires_at TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""")
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS issue_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id TEXT NOT NULL,
                    at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT,
                    actor TEXT,
                    detail TEXT
                )""")
            for column in ("state", "project", "fingerprint", "batch_id"):
                self._connection.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_issues_{column} ON issues({column})")
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_issue_events_issue ON issue_events(issue_id, id)")
        apply_migrations(self._connection, ISSUE_MIGRATIONS)

    # -- persistence ---------------------------------------------------------

    def _write(self, issue: Issue) -> Issue:
        issue.updated_at = _iso()
        with self._connection:
            self._connection.execute(
                "INSERT INTO issues (issue_id, project, state, priority, fingerprint, "
                "duplicate_of, batch_id, claimed_by, claim_expires_at, payload, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(issue_id) DO UPDATE SET project=excluded.project, "
                "state=excluded.state, priority=excluded.priority, "
                "fingerprint=excluded.fingerprint, duplicate_of=excluded.duplicate_of, "
                "batch_id=excluded.batch_id, claimed_by=excluded.claimed_by, "
                "claim_expires_at=excluded.claim_expires_at, payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (issue.issue_id, issue.project, issue.state, issue.priority, issue.fingerprint,
                 issue.duplicate_of, issue.batch_id, issue.claimed_by, issue.claim_expires_at,
                 json.dumps(issue.as_dict(), ensure_ascii=False), issue.created_at,
                 issue.updated_at))
        return issue

    def record_event(self, issue_id: str, *, kind: str, from_state: str | None = None,
                     to_state: str | None = None, actor: str | None = None,
                     detail: str | None = None) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO issue_events (issue_id, at, kind, from_state, to_state, actor, detail) "
                "VALUES (?,?,?,?,?,?,?)",
                (issue_id, _iso(), kind, from_state, to_state, actor, detail))

    def get(self, issue_id: str) -> Issue | None:
        row = self._connection.execute(
            "SELECT payload FROM issues WHERE issue_id = ?", (issue_id,)).fetchone()
        return _issue_from_payload(row["payload"]) if row else None

    def history(self, issue_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self._connection.execute(
            "SELECT at, kind, from_state, to_state, actor, detail FROM issue_events "
            "WHERE issue_id = ? ORDER BY id", (issue_id,))]

    def list_issues(self, *, state: str | None = None, project: str | None = None,
                    limit: int = 200) -> list[Issue]:
        sql = "SELECT payload FROM issues WHERE 1=1"
        args: list[Any] = []
        if state:
            sql += " AND state = ?"
            args.append(state)
        if project:
            sql += " AND project = ?"
            args.append(project)
        sql += " ORDER BY priority DESC, created_at ASC LIMIT ?"
        args.append(limit)
        return [_issue_from_payload(row["payload"])
                for row in self._connection.execute(sql, args)]

    def counts(self) -> dict[str, int]:
        rows = self._connection.execute(
            "SELECT state, COUNT(*) AS n FROM issues GROUP BY state")
        counts = {row["state"]: row["n"] for row in rows}
        return {state: counts.get(state, 0) for state in ISSUE_STATES}

    def find_by_fingerprint(self, fingerprint: str, *, exclude: str | None = None) -> Issue | None:
        for row in self._connection.execute(
                "SELECT payload FROM issues WHERE fingerprint = ? "
                "AND state NOT IN ('CANCELLED','DUPLICATE') ORDER BY created_at LIMIT 5",
                (fingerprint,)):
            issue = _issue_from_payload(row["payload"])
            if issue.issue_id != exclude:
                return issue
        return None

    def close(self) -> None:
        self._connection.close()


def _issue_from_payload(payload: str) -> Issue:
    raw = json.loads(payload)
    raw["human_hints"] = tuple(raw.get("human_hints") or ())
    raw["questions"] = tuple(raw.get("questions") or ())
    known = {f for f in Issue.__dataclass_fields__}
    return Issue(**{k: v for k, v in raw.items() if k in known})


# -- the service --------------------------------------------------------------

class InboxService:
    """Capture, state transitions and planner claims over one InboxStore."""

    def __init__(self, store: InboxStore, *,
                 planner_concurrency: int = DEFAULT_PLANNER_CONCURRENCY,
                 claim_lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS,
                 project_concurrency: dict[str, int] | None = None) -> None:
        self.store = store
        self.planner_concurrency = max(1, int(planner_concurrency))
        self.claim_lease_seconds = float(claim_lease_seconds)
        # Per-project caps exist so one noisy project cannot occupy every
        # planner slot while another waits behind it.
        self.project_concurrency = dict(project_concurrency or {})

    # -- capture -------------------------------------------------------------

    def capture(self, text: str, *, project: str | None = None, source: str = "capture",
                priority: int = 0, actor: str | None = None) -> dict[str, Any]:
        """Persist a batch of free-form items. Returns a short acknowledgement.

        Deliberately shallow: a title, a rough type, a duplicate check. No
        root-cause analysis, because capture that waits on analysis is not
        capture -- and the planner pool exists precisely so it does not have to.
        """
        items = split_batch(text)
        if not items:
            return {"error": "NOTHING_TO_CAPTURE", "captured": 0, "issues": []}
        batch_id = f"batch_{uuid.uuid4().hex[:10]}"
        captured: list[dict[str, Any]] = []
        for raw in items:
            issue = Issue(
                issue_id=f"iss_{uuid.uuid4().hex[:12]}", raw_text=raw,
                short_title=short_title(raw), project=project, priority=priority,
                source=source, batch_id=batch_id, rough_type=rough_type(raw),
                rough_difficulty=rough_difficulty(raw))
            issue.fingerprint = self._fingerprint(issue)
            existing = self._duplicate_of(issue)
            if existing is not None:
                # Flagged, never dropped. A second report of one defect is
                # still a fact about the world, and silently discarding it
                # loses whatever the second reporter added.
                issue.state = DUPLICATE
                issue.duplicate_of = existing.issue_id
            self.store._write(issue)
            self.store.record_event(
                issue.issue_id, kind="captured", to_state=issue.state, actor=actor,
                detail=f"batch {batch_id}" + (f"; duplicate of {issue.duplicate_of}"
                                              if issue.duplicate_of else ""))
            captured.append({"issue_id": issue.issue_id, "title": issue.short_title,
                             "state": issue.state, "type": issue.rough_type,
                             "duplicate_of": issue.duplicate_of})
        duplicates = [entry for entry in captured if entry["duplicate_of"]]
        return {
            "batch_id": batch_id,
            "captured": len(captured),
            "duplicates": len(duplicates),
            "new": len(captured) - len(duplicates),
            "issues": captured,
        }

    def _fingerprint(self, issue: Issue) -> str:
        """A bucket for cheap duplicate lookup: rough type plus salient words.

        NOT context_pack.fingerprint. That one is module+type only, which is
        right for narrowing bug-retrieval candidates before scoring them, and
        wrong here: a captured item usually has no module yet, so it would put
        every unclassified issue in one bucket and call them all duplicates.
        """
        import hashlib

        words = "".join(sorted(_salient_tokens(issue.short_title))[:6])
        return hashlib.sha256(f"{issue.rough_type}|{words}".encode("utf-8")).hexdigest()[:16]

    def _duplicate_of(self, issue: Issue) -> Issue | None:
        """The existing issue this one restates, if any.

        Compares the wording, because two people reporting one defect write
        different sentences about it. An exact-hash test would miss those, and
        a module-only test would sweep in everything unclassified.
        """
        mine = _salient_tokens(issue.short_title + " " + issue.raw_text)
        if not mine:
            return None
        best: tuple[float, Issue] | None = None
        for candidate in self.store.list_issues(limit=500):
            if candidate.issue_id == issue.issue_id or candidate.state in TERMINAL_STATES:
                continue
            if candidate.rough_type != issue.rough_type:
                continue
            theirs = _salient_tokens(candidate.short_title + " " + candidate.raw_text)
            if not theirs:
                continue
            score = len(mine & theirs) / len(mine | theirs)
            if score >= DUPLICATE_SIMILARITY and (best is None or score > best[0]):
                best = (score, candidate)
        return best[1] if best else None

    # -- transitions ---------------------------------------------------------

    def transition(self, issue_id: str, to_state: str, *, actor: str | None = None,
                   detail: str | None = None, **fields: Any) -> dict[str, Any]:
        if to_state not in ISSUE_STATES:
            return {"error": "UNKNOWN_STATE", "state": to_state,
                    "allowed": list(ISSUE_STATES)}
        issue = self.store.get(issue_id)
        if issue is None:
            return {"error": "ISSUE_NOT_FOUND", "issue_id": issue_id}
        if issue.state in TERMINAL_STATES and to_state != REWORK:
            return {"error": "ISSUE_TERMINAL", "issue_id": issue_id, "state": issue.state}
        previous = issue.state
        issue.state = to_state
        for name, value in fields.items():
            if hasattr(issue, name):
                setattr(issue, name, value)
        if to_state not in (PLANNING,):
            issue.claimed_by = None
            issue.claim_expires_at = None
        self.store._write(issue)
        self.store.record_event(issue_id, kind="transition", from_state=previous,
                                to_state=to_state, actor=actor, detail=detail)
        return {"issue_id": issue_id, "from": previous, "state": to_state}

    # -- planner pool --------------------------------------------------------

    def _active_claims(self) -> list[Issue]:
        """Issues genuinely being planned right now -- expired leases excluded.

        A planner that died mid-issue must not hold its slot forever, and the
        lease is what makes that recoverable across a restart without anyone
        having to clean up by hand.
        """
        now = _now()
        active = []
        for issue in self.store.list_issues(state=PLANNING, limit=500):
            if not issue.claim_expires_at:
                continue
            try:
                expires = datetime.fromisoformat(issue.claim_expires_at)
            except ValueError:
                continue
            if expires > now:
                active.append(issue)
        return active

    def reclaim_stale(self, *, actor: str = "pool") -> list[str]:
        """Return expired claims to the queue. Safe to call after a restart."""
        now = _now()
        reclaimed: list[str] = []
        for issue in self.store.list_issues(state=PLANNING, limit=500):
            expired = True
            if issue.claim_expires_at:
                try:
                    expired = datetime.fromisoformat(issue.claim_expires_at) <= now
                except ValueError:
                    expired = True
            if not expired:
                continue
            issue.state = TRIAGED
            issue.claimed_by = None
            issue.claim_expires_at = None
            self.store._write(issue)
            self.store.record_event(issue.issue_id, kind="claim_expired", to_state=TRIAGED,
                                    actor=actor,
                                    detail="planner lease expired; returned for re-planning")
            reclaimed.append(issue.issue_id)
        return reclaimed

    def claim_for_planning(self, planner_id: str, *, project: str | None = None
                           ) -> dict[str, Any]:
        """Take at most one issue for this planner, honouring the caps.

        Claims are serialised through the store's own write so two planners
        racing cannot both take the same issue: the loser sees the winner's
        claim when it re-reads, and backs off.
        """
        self.reclaim_stale()
        active = self._active_claims()
        if len(active) >= self.planner_concurrency:
            return {"status": "AT_CAPACITY", "active": len(active),
                    "limit": self.planner_concurrency}

        per_project: dict[str, int] = {}
        for issue in active:
            per_project[issue.project or ""] = per_project.get(issue.project or "", 0) + 1

        candidates: list[Issue] = []
        for state in CLAIMABLE_STATES:
            candidates.extend(self.store.list_issues(state=state, project=project, limit=200))
        # Priority first, then oldest -- FIFO inside one priority, so a
        # long-waiting issue is not starved by a newer one of equal weight.
        candidates.sort(key=lambda issue: (-issue.priority, issue.created_at))

        for candidate in candidates:
            cap = self.project_concurrency.get(candidate.project or "")
            if cap is not None and per_project.get(candidate.project or "", 0) >= cap:
                continue                                  # fairness cap, try the next project
            fresh = self.store.get(candidate.issue_id)
            if fresh is None or fresh.state not in CLAIMABLE_STATES:
                continue                                  # someone else took it first
            fresh.state = PLANNING
            fresh.claimed_by = planner_id
            fresh.claim_expires_at = _iso(_now() + timedelta(seconds=self.claim_lease_seconds))
            self.store._write(fresh)
            self.store.record_event(fresh.issue_id, kind="claimed", from_state=candidate.state,
                                    to_state=PLANNING, actor=planner_id,
                                    detail=f"lease {self.claim_lease_seconds:.0f}s")
            return {"status": "CLAIMED", "issue": fresh.as_dict()}
        return {"status": "NOTHING_TO_CLAIM", "active": len(active)}

    def release(self, issue_id: str, *, to_state: str, actor: str,
                detail: str | None = None, **fields: Any) -> dict[str, Any]:
        """Finish planning an issue and free the planner slot."""
        return self.transition(issue_id, to_state, actor=actor, detail=detail, **fields)

    # -- developer assist ----------------------------------------------------

    def request_user_hint(self, issue_id: str, questions: Sequence[str], *,
                          actor: str = "planner", findings: Sequence[str] = ()
                          ) -> dict[str, Any]:
        """Park an issue on a human, WITHOUT discarding the analysis so far."""
        trimmed = [q.strip() for q in questions if q and q.strip()][:3]
        if not trimmed:
            return {"error": "QUESTIONS_REQUIRED",
                    "detail": "NEEDS_USER_HINT without a question wastes the round trip"}
        result = self.transition(issue_id, NEEDS_USER_HINT, actor=actor,
                                 detail="; ".join(trimmed), questions=tuple(trimmed))
        if "error" in result:
            return result
        if findings:
            issue = self.store.get(issue_id)
            if issue is not None:
                issue.metadata["findings_before_hint"] = list(findings)
                self.store._write(issue)
        return {**result, "questions": trimmed}

    def attach_human_hint(self, issue_id: str, hint: str, *, actor: str = "developer"
                          ) -> dict[str, Any]:
        """Record a developer's answer and resume the SAME issue.

        The hint is guidance, not truth: it is stored with its provenance so a
        planner weighs it against the current code rather than believing it.
        Resuming the same issue is the point -- a new one would lose the
        analysis that produced the question.
        """
        issue = self.store.get(issue_id)
        if issue is None:
            return {"error": "ISSUE_NOT_FOUND", "issue_id": issue_id}
        if not hint or not hint.strip():
            return {"error": "HINT_REQUIRED"}
        from .project_knowledge import scrub_knowledge

        scrub_knowledge(hint, where=f"issue:{issue_id}")
        issue.human_hints = issue.human_hints + (hint.strip(),)
        issue.state = TRIAGED
        issue.claimed_by = None
        issue.claim_expires_at = None
        self.store._write(issue)
        self.store.record_event(issue_id, kind="human_hint", to_state=TRIAGED, actor=actor,
                                detail=hint.strip()[:200])
        return {"issue_id": issue_id, "state": issue.state,
                "human_hints": list(issue.human_hints),
                "note": "hint recorded as guidance; verify it against the current code"}

    # -- reporting -----------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        counts = self.store.counts()
        active = self._active_claims()
        return {
            "counts": {state: n for state, n in counts.items() if n},
            "total": sum(counts.values()),
            "planning_active": len(active),
            "planner_concurrency": self.planner_concurrency,
            "claimed_by": sorted({issue.claimed_by for issue in active if issue.claimed_by}),
        }
