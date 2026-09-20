"""Workspace trust for worktrees the Harness itself made, and nothing else.

THE BLOCKER THIS REMOVES

Claude Code asks "Is this a project you trust?" the first time it opens a
directory, and it keys that answer by EXACT path -- trusting
/home/kimex/workspace does not trust /home/kimex/workspace/x. A fresh git
worktree is always a new directory, so every Harness run that spawns a
Builder in one stops on a question no deterministic code may answer, and the
engine correctly routes it to PERMISSION_REQUIRED (see harness_runner).

That is the right answer for a path a human owns. It is the wrong answer for
a directory the Harness created itself, seconds earlier, under a root an
operator listed in config -- there the trust decision was already made, by
the operator, when they approved the root. This module records that
pre-existing decision against the exact path Claude Code will look for.

WHY THIS IS NOT "AUTO-APPROVE TRUST PROMPTS"

It is not a prompt answerer. It never reads a pane, never sends a keystroke,
and cannot be pointed at a directory a person is working in. It writes one
boolean, for one path, and only when EVERY one of these holds:

  1. the path is absolute and is a directory that exists NOW;
  2. its realpath -- symlinks resolved -- is STRICTLY under an approved
     worktree root, itself realpath'd (so a symlink pointing out of the root
     cannot smuggle a path in, and the root itself is never trustable);
  3. it is a real git worktree, not merely a directory in the right place;
  4. the Harness DATABASE already records that path on a run, a dispatch or
     a checkpoint.

(4) is what makes this "Harness-owned" rather than "well-located". A path
nobody ran anything in is refused even if it sits in the right root and is a
perfectly good worktree, because the claim being made is "this process
created this directory", and only the database can attest to that. With no
store at all the answer is always no: failing closed is the only safe
default for a function whose output is a trust boolean.

WHY A CORRUPT CONFIG IS REFUSED RATHER THAN REPLACED

~/.claude.json holds a person's entire Claude Code configuration -- accounts,
MCP servers, per-project tool permissions. If it will not parse, the one
thing this module must NOT do is write a clean file over it: that would
"succeed" while destroying everything the requirement to preserve unrelated
entries exists to protect. So a corrupt file is backed up and the grant is
refused, with the backup path in the reason. A human fixes it; nothing here
guesses what was in it.

A file that is simply ABSENT is different, and is created -- there is nothing
to lose and nothing to guess.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .backlog_store import file_lock

#: The key Claude Code reads. One boolean, per exact absolute path.
TRUST_KEY = "hasTrustDialogAccepted"
PROJECTS_KEY = "projects"

#: The audit event name. Carries run_id, path and source, so "why is this
#: directory trusted" is answerable from the record rather than by inference.
TRUST_REGISTERED = "TRUST_REGISTERED"
TRUST_REFUSED = "TRUST_REFUSED"
TRUST_REVOKED = "TRUST_REVOKED"


def default_config_path() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude.json"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -- refusal reasons, as a closed set ----------------------------------------
NOT_ABSOLUTE = "not_absolute"
NOT_A_DIRECTORY = "not_a_directory"
OUTSIDE_APPROVED_ROOTS = "outside_approved_roots"
IS_A_ROOT_ITSELF = "is_a_root_itself"
NOT_A_GIT_WORKTREE = "not_a_git_worktree"
NOT_HARNESS_OWNED = "not_harness_owned"
NO_STORE = "no_store"
CONFIG_CORRUPT = "config_corrupt"
CONFIG_UNWRITABLE = "config_unwritable"
#: revoke() only: the directory is still there, so this is not a cleanup.
STILL_EXISTS = "still_exists"

REFUSAL_REASONS: tuple[str, ...] = (
    NOT_ABSOLUTE, NOT_A_DIRECTORY, OUTSIDE_APPROVED_ROOTS, IS_A_ROOT_ITSELF,
    NOT_A_GIT_WORKTREE, NOT_HARNESS_OWNED, NO_STORE, CONFIG_CORRUPT,
    CONFIG_UNWRITABLE, STILL_EXISTS,
)


@dataclass(frozen=True)
class TrustDecision:
    """What was decided, and everything needed to audit it afterwards."""

    path: str
    granted: bool
    reason: str
    #: True when the path was ALREADY trusted and nothing was written. The
    #: caller gets granted=True either way -- idempotence means the outcome
    #: is the same, not that the second call pretends to have done work.
    already: bool = False
    run_id: str | None = None
    source: str = "harness"
    backup_path: str | None = None
    checks: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "granted": self.granted, "reason": self.reason,
                "already": self.already, "run_id": self.run_id, "source": self.source,
                "backup_path": self.backup_path, "checks": dict(self.checks)}


def _real(path: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def _is_strictly_under(candidate: str, root: str) -> bool:
    """True when `candidate` is inside `root` and is not `root` itself.

    Both are already realpath'd by the caller. `os.path.commonpath` rather
    than `startswith`, because "/a/bc".startswith("/a/b") is True and is
    exactly the containment bug this would otherwise have.
    """
    if candidate == root:
        return False
    try:
        return os.path.commonpath([candidate, root]) == root
    except ValueError:  # different drives / one is relative
        return False


def is_git_worktree(path: str) -> bool:
    """A real worktree, not just a directory in the right place.

    A linked worktree has a `.git` FILE containing a gitdir pointer; a
    primary checkout has a `.git` directory. Both count -- what is being
    excluded is an ordinary directory somebody created under the root.
    """
    marker = Path(path) / ".git"
    return marker.is_file() or marker.is_dir()


#: The directory name the repository's worktree convention uses. A Harness
#: worktree is always <some approved root>/<this>/harness/<task>.
WORKTREE_DIRNAME = ".terminal-mcp-worktrees"


def default_worktree_roots(allowed_cwd_roots: Iterable[str]) -> tuple[str, ...]:
    """The narrow trust roots implied by an operator's existing declaration.

    A new config key would be a second place to get this wrong, and an
    operator who has already said "sessions may open under /home/x/workspace"
    has already made the judgment this needs. What is derived from it is
    strictly NARROWER: not the approved root, but the one subdirectory the
    worktree convention puts Harness worktrees in.

    So `/home/x/workspace` yields `/home/x/workspace/.terminal-mcp-worktrees`
    -- and a directory a person is working in, which lives directly under the
    approved root rather than inside that one subdirectory, is still refused.

    Roots that do not exist are kept: a root is a policy statement, and the
    worktree directory is created on first use. Containment is checked
    against the realpath at grant time, when it does exist.
    """
    roots: list[str] = []
    for root in allowed_cwd_roots:
        text = str(root or "").strip()
        if not text:
            continue
        candidate = os.path.join(os.path.expanduser(text), WORKTREE_DIRNAME)
        if candidate not in roots:
            roots.append(candidate)
    return tuple(roots)


class WorkspaceTrust:
    """Grants workspace trust for Harness-owned worktrees. Nothing else."""

    def __init__(self, *, config_path: str | os.PathLike[str] | None = None,
                 store: Any = None,
                 worktree_roots: Sequence[str] = (),
                 audit: Any = None) -> None:
        self.config_path = Path(config_path) if config_path else default_config_path()
        self.store = store
        #: Realpath'd once, at construction. A root given as a symlink and a
        #: candidate resolved through that symlink must compare equal, and
        #: resolving only one side is how that check silently stops working.
        self.worktree_roots: tuple[str, ...] = tuple(
            dict.fromkeys(_real(root) for root in worktree_roots if str(root).strip()))
        self.audit = audit

    # =====================================================================
    # the question this module exists to answer
    # =====================================================================
    def is_harness_owned(self, path: str | os.PathLike[str]) -> tuple[bool, str, dict[str, Any]]:
        """(owned, reason, checks). Every gate, evaluated and reported.

        The checks dict is returned whether the answer is yes or no, because
        a refusal nobody can explain is a refusal somebody will work around.
        """
        raw = str(path)
        checks: dict[str, Any] = {"given": raw}
        if not os.path.isabs(os.path.expanduser(raw)):
            return False, NOT_ABSOLUTE, checks

        resolved = _real(raw)
        checks["resolved"] = resolved
        checks["approved_roots"] = list(self.worktree_roots)

        if not os.path.isdir(resolved):
            return False, NOT_A_DIRECTORY, checks

        if not self.worktree_roots:
            # No roots configured means no approved place exists. Refused
            # rather than treated as "anywhere is fine" -- the empty-set
            # reading of an allowlist is the dangerous one.
            checks["root_match"] = None
            return False, OUTSIDE_APPROVED_ROOTS, checks

        if any(resolved == root for root in self.worktree_roots):
            # The root holds many worktrees and is not itself one. Trusting
            # it would not even help -- Claude Code keys by exact path.
            return False, IS_A_ROOT_ITSELF, checks

        matched = next((root for root in self.worktree_roots
                        if _is_strictly_under(resolved, root)), None)
        checks["root_match"] = matched
        if matched is None:
            return False, OUTSIDE_APPROVED_ROOTS, checks

        checks["git_worktree"] = is_git_worktree(resolved)
        if not checks["git_worktree"]:
            return False, NOT_A_GIT_WORKTREE, checks

        if self.store is None:
            checks["store"] = None
            return False, NO_STORE, checks

        owner = self._harness_record_for(resolved, raw)
        checks["harness_record"] = owner
        if owner is None:
            return False, NOT_HARNESS_OWNED, checks
        return True, "harness_owned", checks

    def _harness_record_for(self, resolved: str, raw: str) -> dict[str, Any] | None:
        """The run/dispatch/checkpoint row that says the Harness made this.

        Both the resolved and the literal path are matched, because a run may
        have recorded either -- the store takes whatever the worktree service
        handed it, and normalising on write would be a migration for every
        existing row.
        """
        candidates = {resolved, raw, str(Path(raw))}
        sql = (
            "SELECT 'run' AS kind, id AS ref, task_id, worktree_path "
            "  FROM harness_runs      WHERE worktree_path IS NOT NULL "
            "UNION ALL "
            "SELECT 'dispatch', id, run_id, worktree_path "
            "  FROM harness_dispatches WHERE worktree_path IS NOT NULL "
            "UNION ALL "
            "SELECT 'checkpoint', id, run_id, worktree_path "
            "  FROM harness_checkpoints WHERE worktree_path IS NOT NULL "
        )
        try:
            with self.store._connection() as connection:  # noqa: SLF001 -- same package
                rows = connection.execute(sql).fetchall()
        except Exception:  # noqa: BLE001 -- a store that cannot answer is a no
            return None
        for row in rows:
            recorded = row["worktree_path"]
            if recorded in candidates or _real(recorded) == resolved:
                return {"kind": row["kind"], "ref": row["ref"],
                        "task_or_run": row["task_id"], "recorded_path": recorded}
        return None

    # =====================================================================
    # granting
    # =====================================================================
    def is_trusted(self, path: str | os.PathLike[str]) -> bool:
        """Whether the config ALREADY trusts this exact path."""
        config, error, _ = self._read_config()
        if error is not None:
            return False
        entry = (config.get(PROJECTS_KEY) or {}).get(_real(path))
        return bool(isinstance(entry, dict) and entry.get(TRUST_KEY))

    def register(self, path: str | os.PathLike[str], *, run_id: str | None = None,
                 source: str = "harness") -> TrustDecision:
        """Record trust for one Harness-owned worktree. Idempotent.

        Every refusal is audited too. A trust grant that quietly does nothing
        is indistinguishable, from the caller's side, from one that worked --
        and the caller in this system is an engine that will otherwise spend
        a spawn, a settle timeout and a human decision finding out.
        """
        owned, reason, checks = self.is_harness_owned(path)
        resolved = checks.get("resolved") or str(path)
        if not owned:
            decision = TrustDecision(path=resolved, granted=False, reason=reason,
                                     run_id=run_id, source=source, checks=checks)
            self._audit(decision)
            return decision

        config, error, backup = self._read_config()
        if error is not None:
            decision = TrustDecision(path=resolved, granted=False, reason=error,
                                     run_id=run_id, source=source,
                                     backup_path=backup, checks=checks)
            self._audit(decision)
            return decision

        projects = config.get(PROJECTS_KEY)
        if not isinstance(projects, dict):
            projects = {}
        entry = projects.get(resolved)
        if isinstance(entry, dict) and entry.get(TRUST_KEY) is True:
            decision = TrustDecision(path=resolved, granted=True,
                                     reason="already_trusted", already=True,
                                     run_id=run_id, source=source, checks=checks)
            self._audit(decision)
            return decision

        try:
            backup = self._write_trust(resolved)
        except OSError as exc:
            decision = TrustDecision(path=resolved, granted=False,
                                     reason=CONFIG_UNWRITABLE, run_id=run_id,
                                     source=source, backup_path=backup,
                                     checks={**checks, "error": str(exc)})
            self._audit(decision)
            return decision

        decision = TrustDecision(path=resolved, granted=True, reason="registered",
                                 run_id=run_id, source=source, backup_path=backup,
                                 checks=checks)
        self._audit(decision)
        return decision

    def revoke(self, path: str | os.PathLike[str], *, run_id: str | None = None,
               source: str = "harness") -> TrustDecision:
        """Remove the trust entry for a worktree that no longer exists.

        WHY THIS IS NOT OPTIONAL TIDYING. Harness worktrees are named after
        their task, so the same path recurs every time that task runs. Left
        behind, an entry granted to a directory that has since been deleted
        silently pre-approves whatever appears at that path next -- including
        a directory a person made by hand. Trust must not outlive the thing
        it was granted for.

        Deliberately REFUSES to revoke a path that still exists: this is a
        cleanup for a removed worktree, not a general "untrust" verb, and a
        caller that wants the latter is asking for something this module does
        not do. Only paths under an approved root are touched at all, so it
        can never remove a person's own entry.
        """
        resolved = _real(path)
        checks: dict[str, Any] = {"given": str(path), "resolved": resolved,
                                  "approved_roots": list(self.worktree_roots)}
        matched = next((root for root in self.worktree_roots
                        if _is_strictly_under(resolved, root)), None)
        checks["root_match"] = matched
        if matched is None:
            decision = TrustDecision(path=resolved, granted=False,
                                     reason=OUTSIDE_APPROVED_ROOTS, run_id=run_id,
                                     source=source, checks=checks)
            self._audit(decision, event=TRUST_REVOKED)
            return decision
        if os.path.isdir(resolved):
            decision = TrustDecision(path=resolved, granted=False,
                                     reason=STILL_EXISTS, run_id=run_id,
                                     source=source, checks=checks)
            self._audit(decision, event=TRUST_REVOKED)
            return decision

        config, error, backup = self._read_config()
        if error is not None:
            decision = TrustDecision(path=resolved, granted=False, reason=error,
                                     run_id=run_id, source=source,
                                     backup_path=backup, checks=checks)
            self._audit(decision, event=TRUST_REVOKED)
            return decision
        if resolved not in (config.get(PROJECTS_KEY) or {}):
            decision = TrustDecision(path=resolved, granted=True, reason="not_present",
                                     already=True, run_id=run_id, source=source,
                                     checks=checks)
            self._audit(decision, event=TRUST_REVOKED)
            return decision

        try:
            backup = self._remove_entry(resolved)
        except OSError as exc:
            decision = TrustDecision(path=resolved, granted=False,
                                     reason=CONFIG_UNWRITABLE, run_id=run_id,
                                     source=source, backup_path=backup,
                                     checks={**checks, "error": str(exc)})
            self._audit(decision, event=TRUST_REVOKED)
            return decision
        decision = TrustDecision(path=resolved, granted=True, reason="revoked",
                                 run_id=run_id, source=source, backup_path=backup,
                                 checks=checks)
        self._audit(decision, event=TRUST_REVOKED)
        return decision

    def _remove_entry(self, resolved: str) -> str | None:
        """Drop one projects entry, under the same lock and atomicity as a write."""
        with file_lock(self.config_path):
            backup = self._backup()
            existing = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                raise OSError("config is not a JSON object")
            projects = existing.get(PROJECTS_KEY)
            if isinstance(projects, dict):
                projects.pop(resolved, None)
                existing[PROJECTS_KEY] = projects
            directory = self.config_path.parent
            handle, tmp = tempfile.mkstemp(dir=str(directory),
                                           prefix=f".{self.config_path.name}.",
                                           suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(existing, stream, indent=2, sort_keys=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.config_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        return backup

    # =====================================================================
    # the file
    # =====================================================================
    def _read_config(self) -> tuple[dict[str, Any], str | None, str | None]:
        """(config, error, backup_path).

        A missing file is an empty config and no error -- there is nothing to
        preserve. A corrupt file is backed up and reported as an error, and
        the caller must not write: see the module docstring.
        """
        if not self.config_path.exists():
            return {}, None, None
        try:
            raw = self.config_path.read_text(encoding="utf-8")
        except OSError:
            return {}, CONFIG_UNWRITABLE, None
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            backup = self._backup(suffix="corrupt")
            return {}, CONFIG_CORRUPT, backup
        if not isinstance(parsed, dict):
            backup = self._backup(suffix="corrupt")
            return {}, CONFIG_CORRUPT, backup
        return parsed, None, None

    def _backup(self, *, suffix: str = "bak") -> str | None:
        """A timestamped copy beside the original, before anything changes."""
        if not self.config_path.exists():
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self.config_path.with_name(
            f"{self.config_path.name}.{suffix}-{stamp}")
        try:
            shutil.copy2(self.config_path, target)
        except OSError:
            return None
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return str(target)

    def _write_trust(self, resolved: str) -> str | None:
        """Backup, then one atomic read-modify-write under an advisory lock.

        The whole read-modify-write is inside the lock, not just the write:
        two runs granting trust for two different worktrees at the same
        moment would otherwise each read the file, each add their own entry,
        and the second `os.replace` would drop the first -- losing an entry
        the caller was told had been written.
        """
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.config_path):
            backup = self._backup()
            existing: dict[str, Any] = {}
            if self.config_path.exists():
                try:
                    existing = json.loads(self.config_path.read_text(encoding="utf-8"))
                except (ValueError, UnicodeDecodeError):
                    # Re-checked inside the lock: the file may have been
                    # replaced between the pre-flight read and here. Refuse
                    # rather than overwrite, same reasoning as _read_config.
                    raise OSError("config became unparseable before the write")
                if not isinstance(existing, dict):
                    raise OSError("config is not a JSON object")

            projects = existing.get(PROJECTS_KEY)
            if not isinstance(projects, dict):
                projects = {}
            entry = projects.get(resolved)
            if not isinstance(entry, dict):
                entry = {}
            # Only this one key. Every other key on this entry, and every
            # other entry, is carried through untouched.
            entry[TRUST_KEY] = True
            projects[resolved] = entry
            existing[PROJECTS_KEY] = projects

            directory = self.config_path.parent
            handle, tmp = tempfile.mkstemp(dir=str(directory),
                                           prefix=f".{self.config_path.name}.",
                                           suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(existing, stream, indent=2, sort_keys=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.config_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        return backup

    # =====================================================================
    # the record
    # =====================================================================
    def _audit(self, decision: TrustDecision, *, event: str | None = None) -> None:
        """TRUST_REGISTERED / TRUST_REFUSED, on the run's own event log.

        Written against the run when there is one, because "why did this
        worktree become trusted" belongs beside the run that caused it. With
        no run_id there is nowhere in the harness tables to put it, so the
        decision is returned and the caller records it -- inventing a
        synthetic run to hang an event on would put a row in harness_runs
        that never executed anything.
        """
        if event is None:
            event = TRUST_REGISTERED if decision.granted else TRUST_REFUSED
        if self.audit is not None:
            try:
                self.audit(event, decision.to_dict())
            except Exception:  # noqa: BLE001 -- auditing never fails a grant
                pass
        if self.store is None or not decision.run_id:
            return
        try:
            self.store.record_event(
                decision.run_id, event_type=event, actor=decision.source,
                reason=decision.reason,
                metadata={"path": decision.path, "source": decision.source,
                          "granted": decision.granted,
                          "backup_path": decision.backup_path,
                          "checks": dict(decision.checks)})
        except Exception:  # noqa: BLE001
            pass


def trust_for_worktree(path: str, *, store: Any, worktree_roots: Sequence[str],
                       run_id: str | None = None, source: str = "harness",
                       config_path: str | os.PathLike[str] | None = None
                       ) -> TrustDecision:
    """One-call form, for a caller that has a path and a store and no opinion."""
    return WorkspaceTrust(config_path=config_path, store=store,
                          worktree_roots=worktree_roots).register(
        path, run_id=run_id, source=source)
