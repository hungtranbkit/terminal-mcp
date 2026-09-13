"""Procedure registry -- the runbook layer, so a settled workflow is CALLED
rather than re-derived.

WHY THIS EXISTS, WITH THE EVIDENCE IN THIS REPOSITORY

Across one long session an agent ran the same full regression thirty-seven
times, and each time re-typed the same invocation, re-read the same tail of
the same log, and re-derived the same restart-and-verify sequence for a
deploy. None of that thinking was new after the second occurrence. It was
just expensive.

A procedure is that settled sequence, named once, stored as a script, and
afterwards invoked. The registry holds only METADATA and a link -- never a
copy of the script, which would immediately begin to diverge from the file
that actually runs.

THE LOOKUP RULE

Before doing a repeated operation, look here first. A procedure that is
VERIFIED and FRESH is called directly: no reading the script, no reading the
docs, no re-deriving the flow. The implementation is inspected only when it
FAILS, when its dependencies changed, when the command vanished, or when a
human asks for the process itself to change.

OUTPUT IS A CONTRACT, NOT A TRANSCRIPT

A successful run returns one line and a log path. A long successful log has
no information in it and putting it in an agent's context is pure cost. A
failure returns the error summary and where to read more -- and the reader
tails that region rather than ingesting the whole file.

RESULTS ARE CACHED BY WHAT THEY DEPEND ON

A gate that passed at this HEAD, whose dependency paths have not changed, did
not become false because someone asked again. It is reused. It is never
reused across a change that could affect it -- the cache key is the commit
plus the hash of the dependency paths' current state, so an edit invalidates
it by construction rather than by someone remembering to.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .project_knowledge import (KnowledgeError, ProjectKnowledge, _run_git,
                                scrub_knowledge)

PROCEDURE_STATE_FILE = "PROCEDURE_STATE.json"
# Run EVIDENCE is per machine and never travels with the repository. A cached
# PASS names a host-specific dependency fingerprint and a local log path, so
# committing it would let one machine's green result look like evidence on
# another -- which is precisely the mistake the cache exists to avoid.
LOCAL_RUNS_FILE = "procedure-runs.json"
_RUN_FIELDS = ("last_verified_commit", "last_success_at", "last_result")
RUNBOOKS_DOC = "RUNBOOKS.md"
SCHEMA_VERSION = 1

# Where generated procedures live when the repo has none of its own. Checked
# for existing scripts FIRST -- a repo that already has `make test` or
# `scripts/ci.sh` must not grow a second way to do the same thing.
AGENT_SCRIPT_DIR = "scripts/agent"

# Status of one procedure, derived not stored.
VERIFIED = "VERIFIED"   # ran green at a commit whose dependencies are unchanged
STALE = "STALE"         # its dependencies moved since it last passed
BROKEN = "BROKEN"       # the script it names is gone or not executable
UNVERIFIED = "UNVERIFIED"  # registered, never run green

# Risk decides what may be called automatically. A procedure that changes
# production is never auto-invoked by a classifier, however fresh it is.
RISK_READ_ONLY = "read_only"
RISK_LOCAL = "local"
RISK_PREVIEW = "preview"
RISK_STAGING = "staging"
RISK_PRODUCTION = "production"
AUTO_INVOKABLE_RISK = (RISK_READ_ONLY, RISK_LOCAL, RISK_PREVIEW)


class ProcedureError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Procedure:
    """One settled operation. Metadata only -- the script itself is a file."""

    id: str
    name: str
    command: list[str]                 # argv, never a shell string
    working_dir: str = "."
    args_hint: str = ""
    prerequisites: tuple[str, ...] = ()
    environment: str = "local"         # which environment it touches
    risk: str = RISK_LOCAL
    timeout_seconds: float = 1800.0
    success_criteria: str = "exit code 0"
    # The paths whose change should invalidate a green result. This is what
    # makes caching safe: an edit under one of these makes the cache key
    # differ, so nothing has to remember to expire anything.
    depends_on: tuple[str, ...] = ()
    output_contract: str = "PASS/FAIL + stage + error summary + log path"
    last_verified_commit: str | None = None
    last_success_at: str | None = None
    last_result: str | None = None
    source: str = "declared"           # declared | discovered | generated

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "command": list(self.command),
                "working_dir": self.working_dir, "args_hint": self.args_hint,
                "prerequisites": list(self.prerequisites), "environment": self.environment,
                "risk": self.risk, "timeout_seconds": self.timeout_seconds,
                "success_criteria": self.success_criteria,
                "depends_on": list(self.depends_on), "output_contract": self.output_contract,
                "last_verified_commit": self.last_verified_commit,
                "last_success_at": self.last_success_at, "last_result": self.last_result,
                "source": self.source}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Procedure":
        return cls(
            id=raw["id"], name=raw.get("name") or raw["id"],
            command=list(raw.get("command") or []),
            working_dir=raw.get("working_dir") or ".",
            args_hint=raw.get("args_hint") or "",
            prerequisites=tuple(raw.get("prerequisites") or ()),
            environment=raw.get("environment") or "local",
            risk=raw.get("risk") or RISK_LOCAL,
            timeout_seconds=float(raw.get("timeout_seconds") or 1800.0),
            success_criteria=raw.get("success_criteria") or "exit code 0",
            depends_on=tuple(raw.get("depends_on") or ()),
            output_contract=raw.get("output_contract") or "",
            last_verified_commit=raw.get("last_verified_commit"),
            last_success_at=raw.get("last_success_at"),
            last_result=raw.get("last_result"),
            source=raw.get("source") or "declared")


@dataclass
class ProcedureResult:
    """What a caller gets back. Deliberately small."""

    procedure_id: str
    ok: bool
    stage: str
    summary: str
    log_path: str | None = None
    exit_code: int | None = None
    duration_seconds: float | None = None
    from_cache: bool = False
    error_excerpt: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"procedure_id": self.procedure_id, "ok": self.ok, "stage": self.stage,
                "summary": self.summary, "log_path": self.log_path,
                "exit_code": self.exit_code, "duration_seconds": self.duration_seconds,
                "from_cache": self.from_cache, "error_excerpt": self.error_excerpt}

    def one_line(self) -> str:
        """What goes into an agent's context on success: this, and nothing else."""
        mark = "PASS" if self.ok else "FAIL"
        cached = " (cached)" if self.from_cache else ""
        return f"{mark} {self.procedure_id} :: {self.stage}{cached} :: {self.summary}"


# -- discovery ---------------------------------------------------------------

# Known ways a repository already expresses these operations. Checked before
# anything is generated: a second way to run the tests is a way for the two to
# disagree.
DISCOVERY_RULES: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("test_gate", "Makefile", ("make", "test"), "grep:^test:"),
    ("test_gate", "Justfile", ("just", "test"), "grep:^test:"),
    ("test_gate", "scripts/test.sh", ("bash", "scripts/test.sh"), "exists"),
    ("test_gate", "scripts/ci.sh", ("bash", "scripts/ci.sh"), "exists"),
    ("build", "Makefile", ("make", "build"), "grep:^build:"),
    ("build", "scripts/build.sh", ("bash", "scripts/build.sh"), "exists"),
    ("deploy_preview", "scripts/deploy-preview.sh", ("bash", "scripts/deploy-preview.sh"), "exists"),
    ("deploy_staging", "scripts/deploy-staging.sh", ("bash", "scripts/deploy-staging.sh"), "exists"),
    ("smoke", "scripts/smoke.sh", ("bash", "scripts/smoke.sh"), "exists"),
    ("healthcheck", "scripts/healthcheck.sh", ("bash", "scripts/healthcheck.sh"), "exists"),
)


def discover_existing(root: Path) -> list[dict[str, Any]]:
    """What this repository ALREADY has for these operations.

    Run before generating anything. Reusing what a project already does is
    both cheaper and safer than adding a parallel path that will drift from
    the one its humans actually use.
    """
    found: list[dict[str, Any]] = []
    for procedure_id, relative, command, how in DISCOVERY_RULES:
        path = root / relative
        if not path.exists():
            continue
        if how.startswith("grep:"):
            needle = how.split(":", 1)[1]
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            import re as _re

            if not _re.search(needle, text, _re.MULTILINE):
                continue
        found.append({"id": procedure_id, "via": relative, "command": list(command)})
    return found


class ProcedureRegistry:
    """Procedures for one project, stored beside its knowledge map."""

    def __init__(self, knowledge: ProjectKnowledge, *,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 log_dir: str | os.PathLike[str] | None = None) -> None:
        self.knowledge = knowledge
        self.root = knowledge.root
        self._runner = runner
        self.log_dir = Path(log_dir) if log_dir else (
            Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
            / "terminal-mcp" / "procedure-logs")

    @property
    def state_path(self) -> Path:
        """Shared DEFINITIONS: what the procedures are. Belongs in the repo."""
        return self.knowledge.dir / PROCEDURE_STATE_FILE

    @property
    def runs_path(self) -> Path:
        """Local EVIDENCE: what has actually run on THIS machine.

        Keyed by the repository path so several clones on one host do not
        share each other's results.
        """
        digest = hashlib.sha256(str(self.root.resolve()).encode("utf-8")).hexdigest()[:12]
        return self.log_dir.parent / "procedure-runs" / f"{digest}-{LOCAL_RUNS_FILE}"

    def _load_runs(self) -> dict[str, Any]:
        if not self.runs_path.exists():
            return {"cache": {}, "runs": {}}
        try:
            data = json.loads(self.runs_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Local evidence is regenerable: a corrupt file means "nothing has
            # run here yet", never a failure of the whole registry.
            return {"cache": {}, "runs": {}}
        data.setdefault("cache", {})
        data.setdefault("runs", {})
        return data

    def _save_runs(self, runs: dict[str, Any]) -> None:
        self.runs_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.runs_path.with_suffix(".tmp")
        temp.write_text(json.dumps(runs, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        os.replace(temp, self.runs_path)

    def load(self) -> dict[str, Any]:
        """Definitions from the repo, merged with THIS machine's run evidence."""
        if not self.state_path.exists():
            data: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "procedures": {}}
        else:
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ProcedureError(f"{PROCEDURE_STATE_FILE} is unreadable: {exc}") from exc
            data.setdefault("procedures", {})
        local = self._load_runs()
        data["cache"] = local["cache"]
        for procedure_id, evidence in (local.get("runs") or {}).items():
            entry = data["procedures"].get(procedure_id)
            if entry is not None:
                entry.update({k: v for k, v in evidence.items() if k in _RUN_FIELDS})
        return data

    def save(self, state: dict[str, Any]) -> None:
        """Split on the way out: definitions to the repo, evidence to this host."""
        runs = {"cache": state.get("cache") or {}, "runs": {}}
        shared = {"schema_version": SCHEMA_VERSION, "procedures": {}}
        for procedure_id, entry in (state.get("procedures") or {}).items():
            evidence = {k: entry.get(k) for k in _RUN_FIELDS if entry.get(k) is not None}
            if evidence:
                runs["runs"][procedure_id] = evidence
            shared["procedures"][procedure_id] = {
                k: v for k, v in entry.items() if k not in _RUN_FIELDS}
        self.knowledge._write_atomic(
            self.state_path,
            json.dumps(shared, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        self._save_runs(runs)

    # -- registration --------------------------------------------------------

    def register(self, procedure: Procedure, *, owner: str = "worker") -> Procedure:
        """Record a procedure's metadata. Never its script body."""
        scrub_knowledge(" ".join(procedure.command) + " " + procedure.args_hint,
                        where=f"procedure:{procedure.id}")
        with self.knowledge.lock(owner=owner):
            state = self.load()
            existing = state["procedures"].get(procedure.id) or {}
            merged = {**existing, **procedure.as_dict()}
            # Registering again must not erase a green result that is still
            # valid -- that would send the next caller off to re-run a gate
            # for no reason.
            for keep in ("last_verified_commit", "last_success_at", "last_result"):
                if procedure.as_dict().get(keep) is None and existing.get(keep) is not None:
                    merged[keep] = existing[keep]
            state["procedures"][procedure.id] = merged
            self.save(state)
        return Procedure.from_dict(state["procedures"][procedure.id])

    def get(self, procedure_id: str) -> Procedure | None:
        raw = self.load()["procedures"].get(procedure_id)
        return Procedure.from_dict(raw) if raw else None

    def list(self) -> list[dict[str, Any]]:
        """Every procedure with its CURRENT status, derived from git and disk."""
        state = self.load()
        out = []
        for raw in sorted(state["procedures"].values(), key=lambda p: p["id"]):
            procedure = Procedure.from_dict(raw)
            out.append({**procedure.as_dict(), "status": self.status_of(procedure),
                        "script_exists": self._script_exists(procedure)})
        return out

    # -- freshness -----------------------------------------------------------

    def _script_exists(self, procedure: Procedure) -> bool:
        if not procedure.command:
            return False
        head = procedure.command[0]
        # `bash scripts/x.sh` -- the script is what matters, not the shell.
        candidate = procedure.command[1] if head in ("bash", "sh", "python", "python3") \
            and len(procedure.command) > 1 else head
        path = self.root / candidate
        if path.exists():
            return True
        # An absolute path or a command on PATH is legitimate too.
        import shutil as _shutil

        return Path(candidate).is_absolute() and Path(candidate).exists() \
            or _shutil.which(candidate) is not None

    def dependency_fingerprint(self, procedure: Procedure) -> str:
        """What this procedure's result depends on, right now.

        The mtime+size of every dependency path, hashed. An edit changes it,
        so a cached PASS cannot survive a change that could affect it -- the
        invalidation is structural rather than something a caller must
        remember to do.
        """
        digest = hashlib.sha256()
        digest.update((self.knowledge.head() or "no-head").encode())
        for relative in sorted(procedure.depends_on):
            path = self.root / relative
            if path.is_dir():
                for child in sorted(path.rglob("*")):
                    if child.is_file():
                        try:
                            stat = child.stat()
                        except OSError:
                            continue
                        digest.update(f"{child.relative_to(self.root)}:{stat.st_mtime_ns}:"
                                      f"{stat.st_size}".encode())
            elif path.is_file():
                stat = path.stat()
                digest.update(f"{relative}:{stat.st_mtime_ns}:{stat.st_size}".encode())
            else:
                digest.update(f"{relative}:missing".encode())
        return digest.hexdigest()[:20]

    def status_of(self, procedure: Procedure) -> str:
        if not self._script_exists(procedure):
            return BROKEN
        if not procedure.last_verified_commit or procedure.last_result != "PASS":
            return UNVERIFIED
        cached = self.load()["cache"].get(procedure.id) or {}
        if cached.get("fingerprint") == self.dependency_fingerprint(procedure):
            return VERIFIED
        return STALE

    # -- running -------------------------------------------------------------

    def run(self, procedure_id: str, *, extra_args: Sequence[str] = (),
            use_cache: bool = True, owner: str = "worker",
            allow_risky: bool = False) -> ProcedureResult:
        """Call a procedure. Returns a small result, never a transcript.

        A green result at the same dependency fingerprint is REUSED rather
        than re-run: it did not become false because somebody asked again.
        Passing `extra_args` disables that -- a different invocation is a
        different question.
        """
        procedure = self.get(procedure_id)
        if procedure is None:
            return ProcedureResult(procedure_id, False, "lookup",
                                   f"no procedure registered as {procedure_id!r}")
        if not self._script_exists(procedure):
            return ProcedureResult(procedure_id, False, "lookup",
                                   f"the script it names is missing: "
                                   f"{' '.join(procedure.command)}")
        if procedure.risk not in AUTO_INVOKABLE_RISK and not allow_risky:
            # Production release stays a separate, explicit decision. A fresh
            # runbook makes it repeatable, never automatic.
            return ProcedureResult(
                procedure_id, False, "policy",
                f"{procedure_id} is risk={procedure.risk}; it requires an explicit "
                f"approval and is never invoked automatically")

        fingerprint = self.dependency_fingerprint(procedure)
        if use_cache and not extra_args:
            cached = self.load()["cache"].get(procedure_id) or {}
            if cached.get("fingerprint") == fingerprint and cached.get("ok"):
                return ProcedureResult(
                    procedure_id, True, "cached", cached.get("summary") or "passed earlier",
                    log_path=cached.get("log_path"), exit_code=0, from_cache=True)

        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = self.log_dir / f"{procedure_id}-{stamp}.log"
        argv = [*procedure.command, *extra_args]
        started = time.monotonic()
        try:
            completed = self._runner(argv, cwd=str(self.root / procedure.working_dir),
                                     capture_output=True, text=True,
                                     timeout=procedure.timeout_seconds)
            output = (completed.stdout or "") + (completed.stderr or "")
            code = completed.returncode
        except Exception as exc:  # noqa: BLE001 -- a failing procedure is data
            output = f"{type(exc).__name__}: {exc}"
            code = 1
        duration = round(time.monotonic() - started, 1)
        try:
            log_path.write_text(output, encoding="utf-8", errors="replace")
        except OSError:
            log_path = None  # type: ignore[assignment]

        ok = code == 0
        summary, excerpt = _summarise(output, ok=ok)
        result = ProcedureResult(procedure_id, ok, "run", summary,
                                 log_path=str(log_path) if log_path else None,
                                 exit_code=code, duration_seconds=duration,
                                 error_excerpt=excerpt)
        self._record(procedure, result, fingerprint, owner=owner)
        return result

    def _record(self, procedure: Procedure, result: ProcedureResult,
                fingerprint: str, *, owner: str) -> None:
        with self.knowledge.lock(owner=owner):
            state = self.load()
            entry = state["procedures"].setdefault(procedure.id, procedure.as_dict())
            entry["last_result"] = "PASS" if result.ok else "FAIL"
            if result.ok:
                entry["last_verified_commit"] = self.knowledge.head()
                entry["last_success_at"] = _now()
                state["cache"][procedure.id] = {
                    "fingerprint": fingerprint, "ok": True,
                    "summary": result.summary, "log_path": result.log_path,
                    "at": _now()}
            else:
                # A failure clears the cache rather than leaving a stale PASS
                # that would let the next caller skip the gate entirely.
                state["cache"].pop(procedure.id, None)
            self.save(state)


# How much of a failing log is worth carrying. Enough to see the failure and
# what led to it; not the whole run.
ERROR_CONTEXT_LINES = 40
_FAILURE_MARKERS = ("Traceback", "ERROR", "FAILED", "error:", "FAIL ", "Exception",
                    "assert", "fatal:", "not found", "Killed")


def _summarise(output: str, *, ok: bool) -> tuple[str, str]:
    """One line for the caller, plus the failing region only.

    A successful run's log has nothing an agent needs. A failing one has one
    region that matters, and the rest is noise that would crowd out the part
    worth reading.
    """
    lines = (output or "").splitlines()
    if ok:
        tail = [line for line in lines[-6:] if line.strip()]
        return (tail[-1].strip()[:200] if tail else "completed"), ""
    index = None
    for position in range(len(lines) - 1, -1, -1):
        if any(marker in lines[position] for marker in _FAILURE_MARKERS):
            index = position
            break
    if index is None:
        region = lines[-ERROR_CONTEXT_LINES:]
        summary = (region[-1].strip() if region else "failed")
    else:
        start = max(0, index - ERROR_CONTEXT_LINES // 2)
        region = lines[start:index + ERROR_CONTEXT_LINES // 2]
        summary = lines[index].strip()
    return summary[:200] or "failed", "\n".join(region)[:4000]
