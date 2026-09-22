"""Runbook Registry -- the READ-ONLY lookup a worker consults before it
rediscovers a procedure someone already wrote down.

WHAT THE AUDIT FOUND (task "Use runbook registry from worker flow").
There was no runbook registry in this project at all. `grep -ri runbook`
over `terminal_mcp/` returns nothing; the only runbook that exists is one
hand-written document, `docs/CONTROLLER_RUNBOOK.md`, which nothing reads
programmatically. So there was nothing to "use from the worker flow" --
the retrieval side had to be built before it could be wired. This module
is that retrieval side, and deliberately nothing more: it does not author
runbooks, does not execute them, and owns no procedure text of its own.

SOURCE OF TRUTH IS A FILE IN THE PROJECT REPO -- `.terminal-mcp/
runbooks.json`, exactly the precedent `backlog_store.py` already set for
`.terminal-mcp/backlog.json` and for the same reasons (portable with a
clone, diffable/reviewable/revertible in git, readable by a human without
this server running, never stranded on one machine's SQLite file). This
module reuses that module's own `.terminal-mcp` directory constant rather
than declaring a second one.

WHAT A WORKER GETS BACK IS A REFERENCE, NOT A PROCEDURE. A hit returns
`RunbookRef` -- id, title, version, source pointer, one-line summary --
and never the runbook's body. That is the secret-handling boundary: the
registry file is a repo file that a human edits, so it can and eventually
will contain something it shouldn't (a host, a token pasted into a step).
Surfacing only a bounded set of short, redacted fields means a leak needs
BOTH a secret in the registry AND that secret to be in the title/summary,
instead of every dispatched prompt carrying whatever the file holds. Every
surfaced string goes through `redaction.redact_text`, and any key whose
name looks like a credential is dropped before a ref is ever built (see
_SECRET_KEY_RE).

RETRIEVAL IS ADVISORY. A `RunbookRef` is a pointer a worker may read; this
module has no execute path, and `destructive` is carried through so the
dispatch wrapper can say so out loud. Nothing here runs a step, and a
`destructive` runbook is returned exactly like any other -- as a reference
with a warning attached, never as an action.

NEVER RAISES, ALWAYS FALLS BACK. `lookup()` returns a `RunbookLookup` in
every case -- registry missing, unreadable, malformed, schema too new, no
match. A worker that cannot reach a registry must behave exactly like a
worker from before this module existed, so every failure is a MISS-shaped
result with a real reason string, never an exception into a dispatch path.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backlog_store import BACKLOG_DIRNAME
from .redaction import redact_text

SCHEMA_VERSION = 1
"""The document/entry schema THIS build understands. An entry declaring a
HIGHER version is refused as stale rather than parsed optimistically --
see STATUS_STALE's own reasoning below."""

RUNBOOK_DIRNAME = BACKLOG_DIRNAME
RUNBOOK_FILENAME = "runbooks.json"

# Lookup outcomes. MISS and STALE and UNAVAILABLE are three genuinely
# different facts and are never collapsed into one "no runbook" boolean:
# MISS means the registry was read fine and nothing matched (the normal,
# uninteresting case); STALE means something DID match but this build
# cannot honestly use it (a registry written by a newer build -- using it
# anyway is how a worker follows a procedure whose meaning has changed);
# UNAVAILABLE means the registry could not be read at all (missing file,
# bad JSON, unreadable path). An operator debugging "why did no runbook
# attach" needs to tell these apart -- the first is fine, the second is a
# version-skew problem, the third is a deployment problem.
STATUS_HIT = "HIT"
STATUS_MISS = "MISS"
STATUS_STALE = "STALE"
STATUS_UNAVAILABLE = "UNAVAILABLE"

# Match axes, most specific first. Order IS the precedence rule -- a
# failure fingerprint is an exact statement about one observed failure, a
# task class is a statement about a kind of work, and a context tag is the
# weakest ("anything on this node"). Ties inside one axis are broken
# further in _best_candidate.
MATCH_FINGERPRINT = "failure_fingerprint"
MATCH_TASK_CLASS = "task_class"
MATCH_CONTEXT_TAG = "context_tag"
_AXIS_SPECIFICITY: dict[str, int] = {MATCH_FINGERPRINT: 3, MATCH_TASK_CLASS: 2, MATCH_CONTEXT_TAG: 1}

_SECRET_KEY_RE = re.compile(r"secret|token|password|passwd|credential|api[_-]?key|private[_-]?key|env", re.I)
"""Entry keys dropped before a ref is built. This is a blunt name-based
screen on purpose: it runs on a file humans hand-edit, where the realistic
mistake is a well-meaning `"token": "..."` field, not a cleverly disguised
one. It is the second of two layers -- redact_text already catches known
secret SHAPES in the strings that do get surfaced."""

_MAX_TITLE_CHARS = 200
_MAX_SUMMARY_CHARS = 500
"""Surfaced strings are truncated, not just redacted. A runbook summary
ends up inside a dispatched prompt; an unbounded field in a repo file
should not be able to push a task's own prompt out of a worker's context."""


@dataclass(frozen=True)
class RunbookRef:
    """What a worker is told. Deliberately small: everything here is safe
    to paste into a dispatched prompt, and the `source` pointer is how a
    worker reads the actual procedure (a repo path, a doc anchor, a URL)
    -- the body itself never travels through this object."""
    id: str
    title: str
    version: int
    source: str
    summary: str = ""
    destructive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "version": self.version, "source": self.source,
                "summary": self.summary, "destructive": self.destructive}


@dataclass(frozen=True)
class RunbookLookup:
    """One lookup's full, honest result -- including the misses.

    `matched_on`/`candidates` exist so hit/miss telemetry can answer "why
    this runbook and not the other one" later, without re-running the
    lookup against a registry file that has since changed."""
    status: str
    runbook: RunbookRef | None = None
    reason: str = ""
    matched_on: str | None = None
    candidates: tuple[str, ...] = ()
    stale_ids: tuple[str, ...] = ()
    registry_source: str = ""
    registry_revision: int | None = None
    registry_schema_version: int | None = None

    @property
    def hit(self) -> bool:
        return self.status == STATUS_HIT

    def to_dict(self) -> dict[str, Any]:
        """The shape written into queue_events metadata (hit/miss/source/
        version tracking). Flat and JSON-safe -- no nested dataclass."""
        return {
            "status": self.status, "reason": self.reason, "matched_on": self.matched_on,
            "runbook": self.runbook.to_dict() if self.runbook is not None else None,
            "candidates": list(self.candidates), "stale_ids": list(self.stale_ids),
            "registry_source": self.registry_source, "registry_revision": self.registry_revision,
            "registry_schema_version": self.registry_schema_version,
        }


@dataclass(frozen=True)
class LookupKey:
    """What a worker knows about the task in front of it. Every field is
    optional: a task carrying none of them is an ordinary MISS, not an
    error, which is what keeps a legacy task (no runbook metadata at all)
    behaving exactly as it did before this module existed."""
    failure_fingerprint: str | None = None
    task_class: str | None = None
    context_tags: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.failure_fingerprint or self.task_class or self.context_tags)


def runbook_path(repo_root: str | Path) -> Path:
    return Path(repo_root) / RUNBOOK_DIRNAME / RUNBOOK_FILENAME


def lookup_key_for_task(task: Any) -> LookupKey:
    """Read the lookup axes off a queue task's OWN metadata -- explicit
    keys only.

    This never derives a fingerprint from `last_error` or guesses a class
    from a title. Inventing a fingerprint out of free-text error output is
    exactly the business logic this task's own brief says not to duplicate
    here: whoever classifies a failure (a coordinator, a verifier, an
    operator) writes `failure_fingerprint` into the task's metadata, and
    this module only reads it back. A task nobody classified gets a clean
    MISS, which is the correct and safe outcome."""
    metadata = getattr(task, "metadata", None)
    if not isinstance(metadata, dict):
        return LookupKey()
    raw_tags = metadata.get("context_tags")
    tags: tuple[str, ...] = ()
    if isinstance(raw_tags, (list, tuple)):
        tags = tuple(str(tag) for tag in raw_tags if isinstance(tag, (str, int)) and str(tag))
    return LookupKey(
        failure_fingerprint=_clean_str(metadata.get("failure_fingerprint")),
        task_class=_clean_str(metadata.get("task_class")),
        context_tags=tags,
    )


def _clean_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _safe_surface(value: Any, *, limit: int) -> str:
    """Redact, collapse, truncate -- in that order. Redaction runs on the
    raw value so a secret split across lines is still caught before the
    whitespace collapse could hide it."""
    if not isinstance(value, str):
        return ""
    text = redact_text(value)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _entry_without_secret_keys(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if not _SECRET_KEY_RE.search(key)}


@dataclass
class _Candidate:
    entry_id: str
    axis: str
    version: int
    ref: RunbookRef


class RunbookRegistry:
    """Read-only view over one `.terminal-mcp/runbooks.json`.

    Caches the parsed document keyed by the file's (mtime_ns, size) so a
    dispatch loop calling `lookup()` on every task does not re-read and
    re-parse the file every time, while an operator editing the registry
    still takes effect on the next lookup without restarting the server.
    The cache is deliberately NOT time-based: a stale runbook attached to
    a real task is worse than a stat() call per dispatch."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._cache_stamp: tuple[int, int] | None = None
        self._cache_document: dict[str, Any] | None = None

    # -- loading ----------------------------------------------------------

    def _load(self) -> tuple[dict[str, Any] | None, str]:
        """Returns (document, error_reason). Never raises: every failure
        mode a repo file can present -- absent, a directory, unreadable,
        truncated mid-write, hand-edited into invalid JSON, valid JSON but
        not an object -- comes back as a reason string a caller turns into
        UNAVAILABLE."""
        try:
            stat = self.path.stat()
        except OSError as exc:
            self._cache_stamp = None
            self._cache_document = None
            return None, f"registry unreadable: {type(exc).__name__}"
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._cache_stamp == stamp and self._cache_document is not None:
            return self._cache_document, ""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            return None, f"registry unreadable: {type(exc).__name__}"
        try:
            document = json.loads(raw)
        except ValueError as exc:
            return None, f"registry is not valid JSON: {exc}"
        if not isinstance(document, dict):
            return None, "registry root is not a JSON object"
        self._cache_stamp = stamp
        self._cache_document = document
        return document, ""

    # -- lookup -----------------------------------------------------------

    def lookup(self, key: LookupKey) -> RunbookLookup:
        """The single public entry point. Always returns a result."""
        try:
            return self._lookup(key)
        except Exception as exc:  # noqa: BLE001 -- a registry problem must never break a dispatch
            return RunbookLookup(status=STATUS_UNAVAILABLE, reason=f"registry lookup failed: {type(exc).__name__}",
                                 registry_source=str(self.path))

    def _lookup(self, key: LookupKey) -> RunbookLookup:
        source = str(self.path)
        document, error = self._load()
        if document is None:
            return RunbookLookup(status=STATUS_UNAVAILABLE, reason=error, registry_source=source)

        doc_schema = document.get("schema_version")
        doc_schema = doc_schema if isinstance(doc_schema, int) else None
        revision = document.get("revision")
        revision = revision if isinstance(revision, int) else None
        base = {"registry_source": source, "registry_revision": revision,
                "registry_schema_version": doc_schema}

        # A document written by a NEWER build is refused wholesale. Reading
        # a subset of fields we happen to recognise would silently hand a
        # worker a procedure whose semantics may have been redefined -- the
        # exact "stale/version mismatch" failure this must surface, not
        # paper over.
        if doc_schema is not None and doc_schema > SCHEMA_VERSION:
            return RunbookLookup(status=STATUS_STALE,
                                 reason=f"registry schema_version {doc_schema} is newer than supported "
                                        f"{SCHEMA_VERSION}", **base)

        entries = document.get("runbooks")
        if not isinstance(entries, list):
            return RunbookLookup(status=STATUS_UNAVAILABLE, reason="registry has no 'runbooks' list", **base)
        if key.empty:
            return RunbookLookup(status=STATUS_MISS, reason="task carries no runbook lookup keys", **base)

        candidates: list[_Candidate] = []
        stale_ids: list[str] = []
        for raw_entry in entries:
            if not isinstance(raw_entry, dict):
                continue
            entry = _entry_without_secret_keys(raw_entry)
            entry_id = _clean_str(entry.get("id"))
            if entry_id is None:
                continue
            entry_schema = entry.get("schema_version", doc_schema if doc_schema is not None else SCHEMA_VERSION)
            if isinstance(entry_schema, int) and entry_schema > SCHEMA_VERSION:
                stale_ids.append(entry_id)
                continue
            axis = _matching_axis(entry, key)
            if axis is None:
                continue
            candidates.append(_Candidate(entry_id=entry_id, axis=axis, version=_entry_version(entry),
                                         ref=_ref_from_entry(entry, entry_id)))

        if not candidates:
            if stale_ids:
                # Something matched the shape of this task but every match
                # is unusable. Reporting this as a plain MISS would hide a
                # version-skew problem behind "no runbook exists".
                return RunbookLookup(status=STATUS_STALE,
                                     reason="every candidate runbook declares a newer schema_version",
                                     stale_ids=tuple(sorted(stale_ids)), **base)
            return RunbookLookup(status=STATUS_MISS, reason="no runbook matched", **base)

        best = _best_candidate(candidates)
        return RunbookLookup(status=STATUS_HIT, runbook=best.ref, matched_on=best.axis,
                             reason=f"matched on {best.axis}",
                             candidates=tuple(sorted(c.entry_id for c in candidates)),
                             stale_ids=tuple(sorted(stale_ids)), **base)


def _entry_version(entry: dict[str, Any]) -> int:
    value = entry.get("version")
    return value if isinstance(value, int) and value >= 0 else 0


def _string_set(entry: dict[str, Any], field_name: str) -> set[str]:
    values = entry.get(field_name)
    if not isinstance(values, (list, tuple)):
        return set()
    return {value.strip() for value in values if isinstance(value, str) and value.strip()}


def _matching_axis(entry: dict[str, Any], key: LookupKey) -> str | None:
    """The most specific axis on which this entry matches, or None.

    Checked in specificity order and returns the FIRST hit, so an entry
    listing both a fingerprint and a broad context tag is credited with
    the fingerprint -- otherwise a catch-all entry could outrank a precise
    one purely by listing more axes."""
    match = entry.get("match")
    if not isinstance(match, dict):
        return None
    if key.failure_fingerprint and key.failure_fingerprint in _string_set(match, "failure_fingerprints"):
        return MATCH_FINGERPRINT
    if key.task_class and key.task_class in _string_set(match, "task_classes"):
        return MATCH_TASK_CLASS
    if key.context_tags:
        tags = _string_set(match, "context_tags")
        if tags and any(tag in tags for tag in key.context_tags):
            return MATCH_CONTEXT_TAG
    return None


def _best_candidate(candidates: list[_Candidate]) -> _Candidate:
    """Deterministic choice among several matches -- a total order, so the
    same registry and the same task always produce the same runbook, on
    any machine, in any file order.

    1. Most specific axis wins (fingerprint > task class > context tag).
    2. Then the HIGHEST version -- two entries for the same failure means
       someone revised the procedure; the newer revision is the canonical
       one.
    3. Then the lowest id, lexicographically. This last tiebreak is
       arbitrary by nature but must exist and must not depend on the order
       entries happen to appear in the file, or two workers reading the
       same registry could follow different procedures for the same
       failure."""
    return sorted(candidates, key=lambda c: (-_AXIS_SPECIFICITY[c.axis], -c.version, c.entry_id))[0]


def _ref_from_entry(entry: dict[str, Any], entry_id: str) -> RunbookRef:
    return RunbookRef(
        id=entry_id,
        title=_safe_surface(entry.get("title"), limit=_MAX_TITLE_CHARS) or entry_id,
        version=_entry_version(entry),
        source=_safe_surface(entry.get("source"), limit=_MAX_TITLE_CHARS),
        summary=_safe_surface(entry.get("summary"), limit=_MAX_SUMMARY_CHARS),
        destructive=bool(entry.get("destructive")),
    )


def registry_from_config(config: Any) -> RunbookRegistry | None:
    """Build the registry a QueueEngine should use, or None for "behave
    exactly as before".

    Takes the config object duck-typed (`enabled`/`path`) rather than
    importing `RunbookConfig`, so this module stays importable without
    pulling config.py's much wider import graph into a dispatch path --
    the same decoupling queue_engine.py already keeps from
    integration_store.py.

    An empty `path` resolves to the conventional in-repo location under
    the process's own working directory. Resolution happens ONCE here, at
    construction; the file itself is read lazily, per lookup, so a
    controller that starts before an operator writes the registry picks it
    up without a restart."""
    if config is None or not getattr(config, "enabled", False):
        return None
    configured = str(getattr(config, "path", "") or "")
    return RunbookRegistry(Path(configured) if configured else runbook_path(Path.cwd()))
