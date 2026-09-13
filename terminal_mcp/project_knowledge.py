"""Project Knowledge Map -- what a worker should read BEFORE it reads the repo.

THE PROBLEM

Every bug in a project currently starts the same way: an agent opens the
repository and re-derives its shape from scratch. That is the single largest
avoidable cost in this system, and it is paid again on every task, by every
worker, for knowledge that did not change.

THE RULE THAT KEEPS IT HONEST

The map is a MAP. The current code and git history are the territory. Nothing
here is ever treated as authoritative about what the code does today -- it is
treated as a fast, fallible index that says WHERE to look, and every entry
carries the commit it was last verified against so a reader can tell whether
to trust it.

That inversion is deliberate. A knowledge store that claims to be truth
becomes wrong silently and then confidently. One that claims only to be a
starting point degrades into "a bit out of date", which is survivable.

WHY FILES AND NOT A DATABASE

Canonical form is Markdown in `.projectflow/knowledge/` inside the repo, with
one machine-readable `KNOWLEDGE_STATE.json` beside it. That means a human can
read it, review it in a diff, and correct it; it travels with the code it
describes; and it cannot drift between machines the way a per-node database
would. The JSON carries only what a machine needs: schema version, indexed
commit, and per-module freshness.

WORKTREES

The canonical map lives in the MAIN worktree. A temporary worktree reads and
writes through `git rev-parse --git-common-dir`, so two agents working in two
worktrees of one repo share one map instead of forking it -- which is the
whole point of having it.

SECRETS NEVER ENTER

Every write goes through `scrub_knowledge`, which refuses rather than strips.
A knowledge base is exactly the kind of long-lived, widely-read, rarely-audited
store a leaked credential would sit in for a year.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

SCHEMA_VERSION = 1
KNOWLEDGE_DIRNAME = ".projectflow/knowledge"
STATE_FILE = "KNOWLEDGE_STATE.json"

# The canonical documents. Order is the order a worker should read them in:
# what the project is, how it is shaped, where features live, where bugs live.
DOCUMENTS: tuple[tuple[str, str], ...] = (
    ("PROJECT.md", "What this project is, who uses it, and what it must not break"),
    ("ARCHITECTURE.md", "Shape of the system: processes, boundaries, and why"),
    ("MODULE_MAP.md", "Business feature -> paths, symbols, APIs, tables, tests"),
    ("DEBUG_MAP.md", "Bug area -> entry points, handlers, state, search keywords, past fixes"),
    ("DATA_FLOW.md", "The dependency chains that matter, named not pasted"),
    ("TEST_MAP.md", "Commands that have actually been run and passed"),
    ("DEPLOY_MAP.md", "How this ships, verified"),
    ("KNOWN_ISSUES.md", "Live problems and their workarounds"),
    ("DECISIONS.md", "Architectural decisions, so a later agent does not undo them"),
)

# Confidence in a module's entry. Deliberately three coarse words rather than a
# percentage: a number would imply a measurement nobody took.
HIGH = "HIGH"       # verified against the current HEAD
MEDIUM = "MEDIUM"   # verified against an ancestor, no changes seen in its paths
LOW = "LOW"         # its paths changed since it was verified, or never verified

# Distinguishes "caller passed no dirty list" from "the working tree could
# not be read", which mean different things for confidence.
_UNSET: Any = object()

# `git status --porcelain` lines: a 1-2 character XY status field, then
# the path. Written to tolerate a stripped leading space.
_STATUS_LINE = re.compile(r"^(?P<xy>[ MADRCU?!]{1,2})\s+(?P<path>.+)$")


class KnowledgeError(RuntimeError):
    """A refused knowledge operation, with a reason a caller can show."""


class SecretInKnowledge(KnowledgeError):
    """Refused: knowledge must never carry a credential.

    Fatal rather than a silent strip. A knowledge base is long-lived, widely
    read and rarely audited -- exactly where a leaked secret survives longest.
    """


# Matched on the VALUE here, unlike the fleet registry's field-name check:
# knowledge is free prose, so there are no field names to reason about. Only
# assignment-shaped and unmistakable-prefix forms, to avoid refusing a
# document that merely discusses authentication.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("assigned_secret", re.compile(
        r"(?im)^\s*(?:export\s+)?[A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|APIKEY|API_KEY"
        r"|PRIVATE_KEY|CREDENTIAL)[A-Z0-9_]*\s*[=:]\s*(?!\s*$)(?![<$\"']?(?:your|example|"
        r"redacted|xxx|\.\.\.|placeholder|\*+)\b)[^\s#]{6,}")),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}")),
    ("cookie", re.compile(r"(?im)^\s*(?:Set-)?Cookie\s*:\s*\S{8,}")),
)


def scrub_knowledge(text: str, *, where: str = "document") -> str:
    """Refuse text carrying a credential. Returns it unchanged if clean.

    An env var NAME is explicitly fine and is the supported way to record
    that a credential is needed -- `DATABASE_URL is read from PGURL` says
    everything useful without saying anything dangerous.
    """
    for name, pattern in _SECRET_PATTERNS:
        match = pattern.search(text or "")
        if match:
            line = text[:match.start()].count("\n") + 1
            raise SecretInKnowledge(
                f"{where} line {line}: looks like a {name}. Knowledge may name an "
                f"environment VARIABLE but never its value.")
    return text


def _run_git(args: Sequence[str], *, cwd: str,
             runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
             timeout: float = 20.0) -> tuple[int, str]:
    try:
        result = runner(["git", *args], cwd=cwd, capture_output=True, text=True,
                        timeout=timeout)
    except Exception as exc:  # noqa: BLE001 -- a git failure is data, not a crash
        return 1, f"{type(exc).__name__}: {exc}"
    return result.returncode, (result.stdout or result.stderr or "").strip()


def canonical_root(cwd: str, *,
                   runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                   ) -> Path | None:
    """The MAIN worktree's root, even when called from a temporary one.

    `--git-common-dir` points at the shared `.git` for every worktree of a
    repo, so two agents in two worktrees resolve to one knowledge map instead
    of silently forking it -- which would defeat the entire purpose.
    """
    code, common = _run_git(["rev-parse", "--git-common-dir"], cwd=cwd, runner=runner)
    if code != 0 or not common:
        return None
    path = Path(common)
    if not path.is_absolute():
        path = (Path(cwd) / path).resolve()
    # `.../repo/.git` -> `.../repo`; a bare repo has no working tree to host a map.
    return path.parent if path.name == ".git" else None


@dataclass
class ModuleState:
    """One module's freshness. `paths` is what makes staleness computable."""

    name: str
    paths: tuple[str, ...] = ()
    last_verified_commit: str | None = None
    last_verified_at: str | None = None
    summary: str = ""
    confidence: str = LOW
    # Why the confidence is what it is. A bare label tells a worker what to
    # feel; the reason tells it what to do next.
    confidence_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "paths": list(self.paths),
                "last_verified_commit": self.last_verified_commit,
                "last_verified_at": self.last_verified_at,
                "summary": self.summary, "confidence": self.confidence,
                "confidence_reason": self.confidence_reason}


class ProjectKnowledge:
    """Read, write and age-check one project's map."""

    def __init__(self, root: str | os.PathLike[str], *,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
        self.root = Path(root)
        self._runner = runner
        self.dir = self.root / KNOWLEDGE_DIRNAME

    # -- git ---------------------------------------------------------------

    def head(self) -> str | None:
        code, out = _run_git(["rev-parse", "HEAD"], cwd=str(self.root), runner=self._runner)
        return out if code == 0 and out else None

    def changed_paths(self, since_commit: str | None) -> list[str] | None:
        """Paths touched between `since_commit` and HEAD.

        None means "cannot tell" -- an unknown commit, a shallow clone, a
        rewritten history. That is reported as unknown rather than as "no
        changes", because the difference decides whether a worker trusts the
        map or re-reads the code.
        """
        if not since_commit:
            return None
        code, out = _run_git(["diff", "--name-only", f"{since_commit}..HEAD"],
                             cwd=str(self.root), runner=self._runner)
        if code != 0:
            return None
        return [line for line in out.splitlines() if line.strip()]

    def uncommitted_paths(self) -> list[str] | None:
        """Paths modified in the working tree, staged or not.

        Committed history alone is not enough to age this map. A module
        verified at HEAD whose files have since been edited on disk is NOT
        current -- the code the map describes is not the code that will run.
        Reporting HIGH there would contradict the one rule this map is
        subordinate to: the current code is the source of truth.
        """
        code, out = _run_git(["status", "--porcelain", "--untracked-files=all"],
                             cwd=str(self.root), runner=self._runner)
        if code != 0:
            return None
        paths: list[str] = []
        for line in out.splitlines():
            # The XY status field is two columns, but a leading space may
            # have been stripped upstream -- so match the field rather than
            # slicing a fixed offset, which silently ate the first character
            # of every worktree-modified path.
            match = _STATUS_LINE.match(line)
            if not match:
                continue
            entry = match.group("path").strip()
            # Renames arrive as "old -> new"; the new path is what exists now.
            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1]
            entry = entry.strip().strip('"')
            if entry:
                paths.append(entry)
        return paths

    # -- state -------------------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self.dir / STATE_FILE

    def exists(self) -> bool:
        return self.state_path.exists()

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema_version": SCHEMA_VERSION, "last_indexed_commit": None,
                    "last_indexed_at": None, "modules": {}, "topics": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise KnowledgeError(f"KNOWLEDGE_STATE.json is unreadable: {exc}") from exc
        if not isinstance(data, dict):
            raise KnowledgeError("KNOWLEDGE_STATE.json must contain an object")
        data.setdefault("schema_version", SCHEMA_VERSION)
        data.setdefault("modules", {})
        return data

    def _write_atomic(self, path: Path, text: str) -> None:
        """Write via a temp file in the same directory, then rename.

        Two workers on one project WILL write concurrently. A rename is
        atomic on POSIX, so a reader sees the old file or the new one and
        never a half-written map.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".km-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def save_state(self, state: dict[str, Any]) -> None:
        state["schema_version"] = SCHEMA_VERSION
        self._write_atomic(self.state_path,
                           json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    # -- locking -----------------------------------------------------------

    def lock(self, *, timeout: float = 10.0, owner: str = "worker") -> "_KnowledgeLock":
        return _KnowledgeLock(self.dir / ".knowledge.lock", timeout=timeout, owner=owner)

    # -- documents ---------------------------------------------------------

    def document(self, name: str) -> str | None:
        path = self.dir / name
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def write_document(self, name: str, text: str, *, owner: str = "worker") -> None:
        if name not in {doc for doc, _ in DOCUMENTS} and not name.endswith(".md"):
            raise KnowledgeError(f"{name!r} is not a knowledge document")
        scrub_knowledge(text, where=name)
        with self.lock(owner=owner):
            self._write_atomic(self.dir / name, text if text.endswith("\n") else text + "\n")

    # -- freshness ---------------------------------------------------------

    def module_states(self, *, now: str | None = None) -> list[ModuleState]:
        """Every module with its CURRENT confidence, recomputed from git.

        Confidence is never stored as a fact -- it is derived on every read
        from the indexed commit, the module's own verified commit and what
        has actually changed under its paths since. A stored confidence would
        be a claim about a repository state that has since moved.
        """
        state = self.load_state()
        head = self.head()
        # One `git status` for the whole listing rather than one per module.
        dirty = self.uncommitted_paths()
        modules: list[ModuleState] = []
        for name, raw in sorted((state.get("modules") or {}).items()):
            module = ModuleState(
                name=name, paths=tuple(raw.get("paths") or ()),
                last_verified_commit=raw.get("last_verified_commit"),
                last_verified_at=raw.get("last_verified_at"),
                summary=raw.get("summary") or "")
            module.confidence = self._confidence(module, head, dirty)
            modules.append(module)
        return modules

    def _confidence(self, module: ModuleState, head: str | None,
                    dirty: Sequence[str] | None = _UNSET) -> str:
        level, module.confidence_reason = self._confidence_with_reason(module, head, dirty)
        return level

    def _confidence_with_reason(self, module: ModuleState, head: str | None,
                                dirty: Sequence[str] | None = _UNSET,
                                ) -> tuple[str, str]:
        if not module.last_verified_commit:
            return LOW, "never verified against a commit"
        if dirty is _UNSET:
            dirty = self.uncommitted_paths()

        # The working tree is checked FIRST, because it beats history: a file
        # edited on disk is what will run, whatever the commit graph says.
        if dirty is None:
            working_tree = "unknown"
        else:
            touched = [p for p in dirty if self._touches(p, module.paths)]
            working_tree = touched[0] if touched else ""

        if head and module.last_verified_commit == head:
            if working_tree == "unknown":
                return MEDIUM, "verified at HEAD, but the working tree could not be read"
            if working_tree:
                # Not LOW: the edit may be exactly what was just indexed. Not
                # HIGH either: we cannot tell. That is what MEDIUM is for.
                return MEDIUM, f"verified at HEAD, but {working_tree} has uncommitted changes"
            return HIGH, "verified against the current HEAD, working tree clean"

        changed = self.changed_paths(module.last_verified_commit)
        if changed is None:
            # Cannot tell. Not HIGH, not LOW -- exactly what MEDIUM is for.
            return MEDIUM, "cannot compare against the verified commit"
        committed = [p for p in changed if self._touches(p, module.paths)]
        if committed:
            return LOW, f"{committed[0]} changed since this module was verified"
        if working_tree == "unknown":
            return MEDIUM, "no commits touched it; the working tree could not be read"
        if working_tree:
            return LOW, f"{working_tree} has uncommitted changes"
        return MEDIUM, "verified against an ancestor; none of its paths have changed"

    @staticmethod
    def _touches(changed_path: str, module_paths: Sequence[str]) -> bool:
        """Did this changed file fall inside one of the module's paths?

        Prefix match on path segments, so `terminal_mcp/work` never matches
        `terminal_mcp/workspace_other.py` -- a false positive here marks a
        fresh module stale and sends a worker to re-read code for nothing.
        """
        changed = changed_path.strip("/")
        for prefix in module_paths:
            prefix = str(prefix).strip("/")
            if not prefix:
                continue
            if changed == prefix or changed.startswith(prefix + "/"):
                return True
            # A bare filename entry matches that file anywhere.
            if "/" not in prefix and Path(changed).name == prefix:
                return True
        return False

    def stale_modules(self) -> list[ModuleState]:
        return [m for m in self.module_states() if m.confidence == LOW]

    # -- the operator-facing verbs ------------------------------------------

    def status(self) -> dict[str, Any]:
        """Is this map usable, and which parts of it are worth trusting."""
        head = self.head()
        state = self.load_state()
        indexed = state.get("last_indexed_commit")
        modules = self.module_states()
        present = [name for name, _ in DOCUMENTS if (self.dir / name).exists()]
        changed = self.changed_paths(indexed)
        return {
            "root": str(self.root), "exists": self.exists(),
            "schema_version": state.get("schema_version"),
            "head": head, "last_indexed_commit": indexed,
            "last_indexed_at": state.get("last_indexed_at"),
            "up_to_date": bool(head and indexed and head == indexed),
            "commits_behind_known": changed is not None,
            "changed_paths_since_index": len(changed) if changed is not None else None,
            "documents": {"present": present,
                          "missing": [n for n, _ in DOCUMENTS if n not in present]},
            "modules": [m.as_dict() for m in modules],
            "confidence_counts": {
                level: sum(1 for m in modules if m.confidence == level)
                for level in (HIGH, MEDIUM, LOW)},
            "stale_modules": [m.name for m in modules if m.confidence == LOW],
        }

    def show(self, name: str | None = None) -> dict[str, Any]:
        if name:
            text = self.document(name)
            if text is None:
                return {"error": "DOCUMENT_NOT_FOUND", "document": name,
                        "known": [n for n, _ in DOCUMENTS]}
            return {"document": name, "text": text}
        return {"documents": {n: self.document(n) for n, _ in DOCUMENTS
                              if self.document(n) is not None}}

    def search(self, query: str, *, limit: int = 30) -> dict[str, Any]:
        """Knowledge search, which a worker runs BEFORE searching code.

        Returns the matching line plus its heading, because a hit without its
        section is a fact with no address -- the whole value here is being
        told where to look next.
        """
        if not query or not query.strip():
            return {"error": "QUERY_REQUIRED", "matches": []}
        needle = query.strip().casefold()
        matches: list[dict[str, Any]] = []
        for name, _ in DOCUMENTS:
            text = self.document(name)
            if not text:
                continue
            heading = ""
            for number, line in enumerate(text.splitlines(), start=1):
                if line.startswith("#"):
                    heading = line.lstrip("#").strip()
                if needle in line.casefold():
                    matches.append({"document": name, "line": number,
                                    "heading": heading, "text": line.strip()[:300]})
                    if len(matches) >= limit:
                        return {"query": query, "matches": matches, "truncated": True}
        return {"query": query, "matches": matches, "truncated": False}

    def validate(self) -> dict[str, Any]:
        """Which of this map's claims no longer hold.

        Checks what is mechanically checkable: do the paths a module names
        still exist, and is the indexed commit still reachable. It does not
        try to judge whether the prose is true -- that is what the confidence
        level and the "code is the source of truth" rule are for.
        """
        state = self.load_state()
        problems: list[dict[str, Any]] = []
        for name, raw in sorted((state.get("modules") or {}).items()):
            for path in raw.get("paths") or []:
                if not (self.root / str(path)).exists():
                    problems.append({"kind": "MISSING_PATH", "module": name, "path": path,
                                     "detail": "the module names a path that no longer exists"})
        indexed = state.get("last_indexed_commit")
        if indexed:
            code, _ = _run_git(["cat-file", "-e", f"{indexed}^{{commit}}"],
                               cwd=str(self.root), runner=self._runner)
            if code != 0:
                problems.append({"kind": "UNKNOWN_INDEXED_COMMIT", "commit": indexed,
                                 "detail": "history was rewritten or the clone is shallow; "
                                           "staleness cannot be computed"})
        for name, _ in DOCUMENTS:
            if not (self.dir / name).exists():
                problems.append({"kind": "MISSING_DOCUMENT", "document": name,
                                 "detail": "not written yet"})
        # Commands recorded in TEST_MAP/DEPLOY_MAP are reported, never run:
        # validating by execution would make a read-only check destructive.
        commands = self.recorded_commands()
        return {"ok": not problems, "problems": problems,
                "recorded_commands": commands,
                "note": ("recorded commands are listed, not executed -- validating by "
                         "running them would make a read-only check destructive")}

    def recorded_commands(self) -> list[dict[str, str]]:
        """Verified commands from TEST_MAP/DEPLOY_MAP, so nobody invents one."""
        out: list[dict[str, str]] = []
        for name in ("TEST_MAP.md", "DEPLOY_MAP.md"):
            text = self.document(name) or ""
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("`") and stripped.endswith("`") and len(stripped) > 4:
                    out.append({"document": name, "command": stripped.strip("`")})
        return out

    # -- writing -------------------------------------------------------------

    def record_module(self, name: str, *, paths: Sequence[str], summary: str = "",
                      verified_commit: str | None = None, owner: str = "worker",
                      topics: Sequence[str] = ()) -> ModuleState:
        """Record or refresh one module, and nothing else.

        Deliberately per-module: refreshing a whole map because one file moved
        is the cost this system exists to avoid.
        """
        scrub_knowledge(summary, where=f"module:{name}")
        commit = verified_commit or self.head()
        stamp = datetime.now(timezone.utc).isoformat()
        with self.lock(owner=owner):
            state = self.load_state()
            modules = state.setdefault("modules", {})
            entry = modules.setdefault(name, {})
            entry.update({"paths": list(paths), "summary": summary,
                          "last_verified_commit": commit, "last_verified_at": stamp})
            if topics:
                entry["topics"] = sorted({str(t) for t in topics})
            state["last_indexed_commit"] = state.get("last_indexed_commit") or commit
            state["last_indexed_at"] = state.get("last_indexed_at") or stamp
            self.save_state(state)
        module = ModuleState(name=name, paths=tuple(paths), last_verified_commit=commit,
                             last_verified_at=stamp, summary=summary)
        module.confidence = self._confidence(module, self.head())
        return module

    def mark_indexed(self, *, commit: str | None = None, owner: str = "worker") -> dict[str, Any]:
        commit = commit or self.head()
        stamp = datetime.now(timezone.utc).isoformat()
        with self.lock(owner=owner):
            state = self.load_state()
            state["last_indexed_commit"] = commit
            state["last_indexed_at"] = stamp
            self.save_state(state)
        return {"last_indexed_commit": commit, "last_indexed_at": stamp}


class _KnowledgeLock:
    """A cooperative lock so two workers do not overwrite each other.

    An O_EXCL lock file with the holder recorded inside, and a staleness
    timeout so a crashed worker cannot wedge a project's knowledge forever.
    Cooperative is the right strength here: every writer goes through this
    class, and the atomic rename underneath means the worst case of a broken
    lock is a lost update rather than a corrupted file.
    """

    STALE_AFTER_SECONDS = 120.0

    def __init__(self, path: Path, *, timeout: float, owner: str) -> None:
        self.path = path
        self.timeout = timeout
        self.owner = owner
        self._held = False

    def __enter__(self) -> "_KnowledgeLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                handle = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(handle, "w") as stream:
                    json.dump({"owner": self.owner, "pid": os.getpid(),
                               "at": datetime.now(timezone.utc).isoformat()}, stream)
                self._held = True
                return self
            except FileExistsError:
                if self._break_if_stale():
                    continue
                if time.monotonic() >= deadline:
                    raise KnowledgeError(
                        f"knowledge is locked by another worker ({self._holder()}); "
                        f"gave up after {self.timeout}s") from None
                time.sleep(0.05)

    def _holder(self) -> str:
        try:
            return str(json.loads(self.path.read_text()).get("owner"))
        except Exception:  # noqa: BLE001
            return "unknown"

    def _break_if_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return True
        if age > self.STALE_AFTER_SECONDS:
            # A crashed holder must not wedge a project's knowledge forever.
            try:
                os.unlink(self.path)
            except OSError:
                return False
            return True
        return False

    def __exit__(self, *exc: Any) -> None:
        if self._held:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self._held = False
