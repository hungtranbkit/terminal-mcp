"""Lifecycle reconciler for task-owned agent sessions.

WHY THIS EXISTS

The shell reaper (core._idle_reap_candidates, ~/.local/bin/session_guard.py)
only ever closes a pane sitting at a plain shell prompt, because a quiet
claude/codex pane may be an agent thinking. Correct, but it left a gap: once an
agent's work was handed off and merged and its worktree removed, nothing ever
closed the agent session itself. Each one keeps a full agent CLI resident
(hundreds of MB), and on dell-linux they piled up until the host was deep in
swap.

HARD LIFECYCLE RULE -- NO INDEFINITE RETENTION

Every session this reconciler governs ends in one of these lifecycle states,
persisted in session_lifecycle.db with reason and timestamps:

  CONTROLLED         the controller has reliable control or a verified owner:
                     protected, attached, plain shell (shell reaper governs
                     it), active task/run/lease, recent activity, an agent
                     visibly RUNNING, or a declared required service that is
                     verified healthy.
  RECOVERY_REQUIRED  the session is uncontrolled / unbound / uninspectable /
                     UNKNOWN: an idle agent pane with no owning task whose
                     completion cannot be proven (dirty tree, unmerged branch,
                     no repo, unreadable cwd, UNKNOWN or WAITING_INPUT pane,
                     idle primary checkout), or a service pane that is not a
                     verified-healthy declared required service. Each pass
                     attempts stabilization: re-probe the pane, re-check task
                     ownership, checkpoint recoverable dirty/unmerged git work.
                     Any regained control (owner appears, a client attaches,
                     activity resumes, pane output changes, it becomes
                     provably complete) returns it to CONTROLLED and resets the
                     timer.
  CLEANUP_ELIGIBLE   still uncontrolled after recovery_grace_minutes (or
                     provably complete: COMPLETED_CLEAN, ORPHAN_WORKTREE_MISSING).
                     Closed automatically on the next real pass, after a
                     verified checkpoint of any unique git work.
  BLOCKED            fail closed: evidence says unique work would be lost --
                     dirty/unmerged work whose checkpoint failed, or an
                     interactive editor (possible unsaved buffer).
  CLOSED / GONE      closed by this reconciler / disappeared on its own.

The idle_hours pre-filter still applies (a pane active within idle_hours is
CONTROLLED), so the worst-case lifetime of an abandoned session is bounded:
idle_hours + recovery_grace_minutes + one interval.

Closing a tmux session never deletes files on disk; what is lost is the agent
process. "Unique work" therefore means uncommitted changes and commits not
contained in the default branch, which are snapshotted to
refs/terminal-mcp/checkpoints/<session>/<stamp> (plus a .patch file under
scrollback_dir/checkpoints) without touching the worktree or its index.

HOW IT CLOSES

Each close re-reads the pane immediately before acting (classification is
never trusted from an earlier report), checkpoints, captures the scrollback to
scrollback_dir, runs the controller's deletion preflight when one is wired
(queue/journal/supervised-task refusal), then deletes through
TerminalService.terminal_delete_session(confirm=True) -- the same path an
operator's delete uses, so protected-set refusal, attachment/lease/recovery
blockers, grant/binding cleanup, registry and audit all apply unchanged.
Re-running is idempotent: a closed session simply no longer appears.
"""
from __future__ import annotations

import datetime as _dt
import fnmatch
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .status import classify_status

_log = logging.getLogger(__name__)

# Provably complete: closable without a grace period.
CLOSE_CLASSES = frozenset({"COMPLETED_CLEAN", "ORPHAN_WORKTREE_MISSING"})
# Reliable control or a verified owner.
CONTROLLED_CLASSES = frozenset({"PROTECTED", "ATTACHED", "SHELL", "ACTIVE_TASK", "AGENT_RECENT",
                                "AGENT_RUNNING", "REQUIRED_SERVICE", "SERVICE_RECENT"})
# Evidence that closing would lose unsaved work.
FAIL_CLOSED_CLASSES = frozenset({"EDITOR_OPEN"})
# Everything else (DIRTY_WORKTREE, UNMERGED_BRANCH, NO_REPO, NO_BASE_REF,
# UNKNOWN_CWD, AGENT_NOT_IDLE, IDLE_PRIMARY_CHECKOUT, SERVICE) is uncontrolled.

CONTROLLED = "CONTROLLED"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
CLEANUP_ELIGIBLE = "CLEANUP_ELIGIBLE"
BLOCKED = "BLOCKED"
CLOSED = "CLOSED"
GONE = "GONE"
_OPEN_STATES = frozenset({RECOVERY_REQUIRED, CLEANUP_ELIGIBLE, BLOCKED})

SHELL_COMMANDS = frozenset({"bash", "sh", "zsh", "fish", "dash"})
EDITOR_COMMANDS = frozenset({"vim", "nvim", "vi", "nano", "emacs", "hx", "micro", "kak"})
DEFAULT_BASE_REFS = ("origin/HEAD", "origin/main", "origin/master", "main", "master")
CHECKPOINT_REF_PREFIX = "refs/terminal-mcp/checkpoints"
# Untracked directories that are regenerable, never unique work: skipped by
# the checkpoint (tracked changes beneath them are still captured).
REGENERABLE_DIRS = frozenset({"node_modules", ".venv", "venv", "__pycache__", ".next", ".nuxt",
                              ".pytest_cache", ".mypy_cache", ".ruff_cache", ".turbo", ".cache",
                              ".parcel-cache", "dist", "build", "target", ".gradle", ".tox"})
ACTOR = "session-reconciler"
_DECIDED_WITHOUT_PROBES = frozenset({"PROTECTED", "ATTACHED", "SHELL", "AGENT_RECENT",
                                     "REQUIRED_SERVICE", "SERVICE_RECENT", "EDITOR_OPEN"})
_RETENTION_SECONDS = 14 * 86400


def _git(path: str, *args: str, timeout: float = 10.0,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                          timeout=timeout, check=False, env=env)


def probe_git(path: str) -> dict[str, Any]:
    """Observed git facts for one working directory. Never raises.

    `exists` is False only when the path is proven absent; an unreadable path
    reports exists=None so callers cannot mistake "could not check" for "gone".
    """
    try:
        exists = os.path.lexists(path)
    except OSError:
        return {"exists": None}
    if not exists:
        return {"exists": False}
    try:
        top = _git(path, "rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir")
    except (OSError, subprocess.SubprocessError):
        return {"exists": True, "is_repo": None}
    if top.returncode != 0:
        return {"exists": True, "is_repo": False}
    lines = top.stdout.strip().splitlines()
    if len(lines) < 3:
        return {"exists": True, "is_repo": None}
    toplevel, git_dir, common = lines[0], lines[1], lines[2]
    common_abs = os.path.realpath(common if os.path.isabs(common) else os.path.join(toplevel, common))
    facts: dict[str, Any] = {"exists": True, "is_repo": True, "toplevel": toplevel,
                             "linked_worktree": os.path.realpath(git_dir) != common_abs}
    try:
        branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
        facts["branch"] = branch.stdout.strip() if branch.returncode == 0 else None
        status = _git(path, "status", "--porcelain", "--untracked-files=normal")
        if status.returncode != 0:
            facts["dirty"] = None
        else:
            facts["dirty"] = bool(status.stdout.strip())
            facts["dirty_entries"] = len(status.stdout.strip().splitlines())
        bases = []
        for ref in DEFAULT_BASE_REFS:
            if _git(path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0:
                bases.append(ref)
        facts["base_refs"] = bases
        merged_into = [ref for ref in bases
                       if _git(path, "merge-base", "--is-ancestor", "HEAD", ref).returncode == 0]
        facts["merged_into"] = merged_into
        facts["unmerged"] = bool(bases) and not merged_into
    except (OSError, subprocess.SubprocessError) as exc:
        facts["error"] = f"{type(exc).__name__}: {exc}"
    return facts


def _safe_ref_component(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip(".") or "session"
    return cleaned[:-5] + "_lock" if cleaned.endswith(".lock") else cleaned.replace("..", "_")


def checkpoint_git(path: str, session: str, *, patch_dir: str | Path,
                   max_untracked_bytes: int, previous: dict[str, Any] | None = None,
                   stamp: str | None = None) -> dict[str, Any]:
    """Snapshot unique git work at `path` without touching its worktree/index.

    Dirty tree: a temporary index (read-tree HEAD, add -u, add the untracked
    files outside REGENERABLE_DIRS) -> tree -> commit parented on HEAD, stored at refs/terminal-mcp/checkpoints/<session>/<stamp>
    plus a binary .patch. Clean but unmerged: the same ref points at HEAD so
    the commits survive branch deletion. Returns {"ok": True, ...} or
    {"ok": False, "error": ...}; never raises. `previous` (the last
    checkpoint) is reused when the content is unchanged.
    """
    stamp = stamp or _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    facts = probe_git(path)
    if facts.get("is_repo") is not True:
        return {"ok": False, "error": "NOT_A_REPO"}
    top = facts["toplevel"]
    ref = f"{CHECKPOINT_REF_PREFIX}/{_safe_ref_component(session)}/{stamp}"
    try:
        head = _git(top, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        head_sha = head.stdout.strip() if head.returncode == 0 else None
        if facts.get("dirty"):
            listed = _git(top, "ls-files", "--others", "--exclude-standard", "-z")
            untracked = [rel for rel in filter(None, listed.stdout.split("\0"))
                         if not REGENERABLE_DIRS.intersection(rel.split("/")[:-1])]
            total = 0
            for rel in untracked:
                try:
                    total += os.lstat(os.path.join(top, rel)).st_size
                except OSError:
                    pass
            if total > max_untracked_bytes:
                return {"ok": False, "error": "UNTRACKED_TOO_LARGE", "untracked_bytes": total}
            with tempfile.TemporaryDirectory(prefix="tmcp-ckpt-") as tmp:
                env = {**os.environ, "GIT_INDEX_FILE": os.path.join(tmp, "index")}
                if head_sha:
                    _git(top, "read-tree", head_sha, env=env)
                added = _git(top, "add", "-u", env=env, timeout=120)
                if added.returncode == 0 and untracked:
                    pathspec = os.path.join(tmp, "untracked")
                    Path(pathspec).write_text("\0".join(untracked), encoding="utf-8")
                    added = _git(top, "add", "--pathspec-from-file", pathspec, "--pathspec-file-nul",
                                 env=env, timeout=120)
                if added.returncode != 0:
                    return {"ok": False, "error": "GIT_ADD_FAILED", "detail": added.stderr.strip()[:300]}
                tree = _git(top, "write-tree", env=env).stdout.strip()
            if not tree:
                return {"ok": False, "error": "WRITE_TREE_FAILED"}
            head_tree = _git(top, "rev-parse", f"{head_sha}^{{tree}}").stdout.strip() if head_sha else None
            if tree == head_tree and not facts.get("unmerged"):
                return {"ok": True, "kind": "none", "reason": "only regenerable untracked files"}
            if previous and previous.get("tree") == tree and previous.get("head") == head_sha \
                    and _git(top, "cat-file", "-e", f"{previous.get('commit')}^{{commit}}").returncode == 0:
                return {**previous, "ok": True, "reused": True}
            parents = ["-p", head_sha] if head_sha else []
            made = _git(top, "commit-tree", tree, *parents, "-m",
                        f"terminal-mcp checkpoint of {session} at {stamp}",
                        env={**os.environ, "GIT_AUTHOR_NAME": ACTOR, "GIT_AUTHOR_EMAIL": "noreply@terminal-mcp",
                             "GIT_COMMITTER_NAME": ACTOR, "GIT_COMMITTER_EMAIL": "noreply@terminal-mcp"})
            commit, kind = made.stdout.strip(), "dirty"
            if made.returncode != 0 or not commit:
                return {"ok": False, "error": "COMMIT_TREE_FAILED", "detail": made.stderr.strip()[:300]}
        elif facts.get("unmerged") and head_sha:
            commit, tree, kind = head_sha, None, "unmerged"
            if previous and previous.get("commit") == head_sha:
                return {**previous, "ok": True, "reused": True}
        else:
            return {"ok": True, "kind": "none", "reason": "no unique git work"}
        if _git(top, "update-ref", ref, commit).returncode != 0:
            return {"ok": False, "error": "UPDATE_REF_FAILED"}
        verified = _git(top, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").stdout.strip()
        if verified != commit:
            return {"ok": False, "error": "CHECKPOINT_NOT_VERIFIED"}
        patch_path = None
        if kind == "dirty" and head_sha:
            directory = Path(os.path.expanduser(str(patch_dir)))
            directory.mkdir(parents=True, exist_ok=True)
            patch_path = directory / f"{stamp}-{_safe_ref_component(session)}.patch"
            diff = _git(top, "diff", "--binary", head_sha, commit, timeout=60)
            patch_path.write_text(diff.stdout, encoding="utf-8")
        return {"ok": True, "kind": kind, "ref": ref, "commit": commit, "tree": tree,
                "head": head_sha, "repo": top, "branch": facts.get("branch"),
                "patch": str(patch_path) if patch_path else None}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _match_required(name: str, required: tuple[Any, ...]) -> Any | None:
    for item in required:
        if fnmatch.fnmatchcase(name, item.session):
            return item
    return None


def check_service_health(info: Any, spec: Any, *, timeout: float = 3.0) -> tuple[bool, str]:
    """A declared service is healthy when its pane is alive and still running
    a non-shell process, and (when configured) its health_url answers."""
    if getattr(info, "pane_dead", False):
        return False, "pane is dead"
    if spec.health_url:
        try:
            with urllib.request.urlopen(spec.health_url, timeout=timeout) as response:  # noqa: S310
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except (OSError, ValueError) as exc:
            return False, f"health_url {spec.health_url} failed: {type(exc).__name__}"
        if status >= 500:
            return False, f"health_url {spec.health_url} returned {status}"
    return True, "pane alive" + (f", health_url {spec.health_url} answered" if spec.health_url else "")


def classify(row: dict[str, Any], *, policy: Any, protected: set[str], now: float,
             active_refs: list[str], pane_state: str | None,
             git: dict[str, Any] | None,
             service_health: tuple[bool, str] | None = None) -> dict[str, Any]:
    """Pure decision for one session. `row` keys: name, attached, command,
    cwd, activity_epoch. `service_health` is None when the session is not a
    declared required service. Returns classification/reason/action/evidence;
    `action` here reflects the classification only -- the lifecycle state
    (grace period) is applied by SessionReconciler."""

    def out(classification: str, reason: str, **evidence: Any) -> dict[str, Any]:
        return {"session": row["name"], "classification": classification,
                "action": "close" if classification in CLOSE_CLASSES else "preserve",
                "reason": reason, "command": row.get("command"), "cwd": row.get("cwd"),
                "idle_hours": round(idle_s / 3600, 2), **evidence}

    idle_s = max(0.0, now - float(row.get("activity_epoch") or 0))
    recent = idle_s < policy.idle_hours * 3600
    command = (row.get("command") or "").lower()
    agent_commands = {c.lower() for c in policy.agent_commands}
    if row["name"] in protected:
        return out("PROTECTED", "configured protected session")
    if row.get("attached"):
        return out("ATTACHED", "a client is attached")
    if command not in agent_commands:
        if command in SHELL_COMMANDS:
            return out("SHELL", "plain shell; governed by the shell idle reaper, not this policy")
        if command in EDITOR_COMMANDS:
            return out("EDITOR_OPEN", f"interactive editor {command!r} may hold unsaved buffers; fail closed")
        if service_health is not None and service_health[0]:
            return out("REQUIRED_SERVICE", f"declared required service, verified healthy ({service_health[1]})")
        if active_refs:
            return out("ACTIVE_TASK", "session owns active work: " + ", ".join(active_refs))
        if recent and service_health is None:
            return out("SERVICE_RECENT", f"non-agent process {command or 'unknown'!r} active within "
                       f"the last {policy.idle_hours}h")
        if service_health is not None:
            return out("SERVICE", f"declared required service is unhealthy: {service_health[1]}")
        return out("SERVICE", f"non-agent process {command or 'unknown'!r} idle and not declared in "
                   "agent_cleanup.required_services")
    if active_refs:
        return out("ACTIVE_TASK", "session owns active work: " + ", ".join(active_refs))
    if recent:
        return out("AGENT_RECENT", f"activity within the last {policy.idle_hours}h")
    if pane_state == "RUNNING":
        return out("AGENT_RUNNING", "agent UI reports a turn in flight")
    if pane_state != "IDLE":
        return out("AGENT_NOT_IDLE", f"pane state is {pane_state or 'unknown'}, not IDLE at its composer")
    cwd = row.get("cwd")
    if not cwd:
        return out("UNKNOWN_CWD", "tmux reports no working directory")
    git = git or {}
    if git.get("exists") is False:
        return out("ORPHAN_WORKTREE_MISSING", f"working directory no longer exists ({cwd})")
    if git.get("exists") is None:
        return out("UNKNOWN_CWD", "working directory could not be checked")
    if git.get("is_repo") is not True:
        return out("NO_REPO", "not inside a git repository; completion cannot be proven")
    if git.get("dirty") is not False:
        return out("DIRTY_WORKTREE", "uncommitted changes" if git.get("dirty") else "git status failed",
                   dirty_entries=git.get("dirty_entries"))
    if not git.get("base_refs"):
        return out("NO_BASE_REF", "no default branch (origin/HEAD, main, master) to prove merge against")
    if git.get("unmerged") or not git.get("merged_into"):
        return out("UNMERGED_BRANCH", f"HEAD of {git.get('branch')!r} is not contained in "
                   + "/".join(git.get("base_refs") or []), branch=git.get("branch"))
    if not git.get("linked_worktree") and not policy.close_clean_primary_checkouts:
        return out("IDLE_PRIMARY_CHECKOUT", "clean and merged, but a primary checkout; "
                   "enable close_clean_primary_checkouts to close these without a grace period",
                   branch=git.get("branch"))
    return out("COMPLETED_CLEAN", f"clean tree, {git.get('branch')!r} merged into "
               + ",".join(git.get("merged_into") or []), branch=git.get("branch"),
               linked_worktree=git.get("linked_worktree"))


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).isoformat(timespec="seconds")


def default_lifecycle_db_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_SESSION_LIFECYCLE_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "session_lifecycle.db"


class LifecycleStore:
    """Durable per-session lifecycle state, so grace periods survive restarts."""

    _COLUMNS = ("session", "identity", "state", "classification", "reason", "first_uncontrolled_at",
                "grace_expires_at", "last_observed_at", "recovery_attempts", "last_recovery_at",
                "recovered_at", "pane_hash", "checkpoint_json", "closed_at", "updated_at")

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_lifecycle_db_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS session_lifecycle (
                    session TEXT PRIMARY KEY, identity TEXT, state TEXT NOT NULL,
                    classification TEXT, reason TEXT, first_uncontrolled_at REAL,
                    grace_expires_at REAL, last_observed_at REAL,
                    recovery_attempts INTEGER NOT NULL DEFAULT 0, last_recovery_at REAL,
                    recovered_at REAL, pane_hash TEXT, checkpoint_json TEXT, closed_at REAL,
                    updated_at REAL NOT NULL)""")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, session: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM session_lifecycle WHERE session = ?",
                                     (session,)).fetchone()
        return self._decode(row) if row else None

    def all(self) -> dict[str, dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM session_lifecycle").fetchall()
        return {row["session"]: self._decode(row) for row in rows}

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["checkpoint"] = json.loads(record.pop("checkpoint_json") or "null")
        return record

    def put(self, record: dict[str, Any]) -> None:
        values = dict(record)
        values["checkpoint_json"] = json.dumps(values.pop("checkpoint", None))
        values.setdefault("recovery_attempts", 0)
        values["updated_at"] = values.get("updated_at") or time.time()
        columns = ", ".join(self._COLUMNS)
        with self._connection() as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO session_lifecycle ({columns}) VALUES "
                f"({', '.join('?' for _ in self._COLUMNS)})",
                tuple(values.get(column) for column in self._COLUMNS))

    def mark_gone(self, present: set[str], now: float) -> list[str]:
        """Open records whose session vanished become GONE; old terminal rows are pruned."""
        gone = []
        with self._connection() as connection:
            for row in connection.execute("SELECT session, state FROM session_lifecycle").fetchall():
                if row["session"] not in present and row["state"] not in (CLOSED, GONE):
                    connection.execute("UPDATE session_lifecycle SET state = ?, updated_at = ? "
                                       "WHERE session = ?", (GONE, now, row["session"]))
                    gone.append(row["session"])
            connection.execute("DELETE FROM session_lifecycle WHERE state IN (?, ?) AND updated_at < ?",
                               (CLOSED, GONE, now - _RETENTION_SECONDS))
        return gone


def _checkpoint_blocked(record: dict[str, Any] | None) -> bool:
    """BLOCKED only because a checkpoint failed: retried every real pass."""
    return bool(record) and record.get("state") == BLOCKED \
        and str(record.get("reason") or "").startswith("checkpoint failed")


def public_lifecycle(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """The lifecycle view exposed on inspect/list rows."""
    if record is None:
        return None
    checkpoint = record.get("checkpoint")
    return {"state": record.get("state"), "reason": record.get("reason"),
            "classification": record.get("classification"),
            "first_uncontrolled_at": _iso(record.get("first_uncontrolled_at")),
            "grace_expires_at": _iso(record.get("grace_expires_at")),
            "last_observed_at": _iso(record.get("last_observed_at")),
            "recovery_attempts": record.get("recovery_attempts") or 0,
            "last_recovery_at": _iso(record.get("last_recovery_at")),
            "recovered_at": _iso(record.get("recovered_at")),
            "closed_at": _iso(record.get("closed_at")),
            "checkpoint": ({k: checkpoint.get(k) for k in ("kind", "ref", "commit", "patch", "repo")}
                           if isinstance(checkpoint, dict) else None)}


class SessionReconciler:
    """Classifies this host's tmux sessions, drives the lifecycle state
    machine, and closes CLEANUP_ELIGIBLE sessions."""

    def __init__(self, terminal: Any, *, policy: Any = None,
                 active_refs: Callable[[str], list[str]] | None = None,
                 preflight: Callable[[str], dict[str, Any]] | None = None,
                 after_delete: Callable[[str], Any] | None = None,
                 git_probe: Callable[[str], dict[str, Any]] = probe_git,
                 health_check: Callable[[Any, Any], tuple[bool, str]] = check_service_health,
                 store: LifecycleStore | None = None,
                 last_input: Callable[[str], float | None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.terminal = terminal
        self._policy = policy
        self._active_refs = active_refs
        self._preflight = preflight
        self._after_delete = after_delete
        self._git_probe = git_probe
        self._health_check = health_check
        self._store = store
        self._last_input = last_input
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def policy(self) -> Any:
        return self._policy or self.terminal.config.session_lifecycle.agent_cleanup

    @property
    def store(self) -> LifecycleStore:
        # Lazy: import-time server builds must not create a state file.
        if self._store is None:
            self._store = LifecycleStore()
        return self._store

    def _protected(self) -> set[str]:
        lifecycle = self.terminal.config.session_lifecycle
        names = set(lifecycle.protected_sessions) | {"terminal-mcp"}
        names |= set(getattr(self.terminal.config.permissions, "admin_sessions", ()) or ())
        return names

    def _refs(self, info: Any) -> list[str]:
        name = info.name
        refs: list[str] = []
        if self._active_refs is not None:
            try:
                refs.extend(self._active_refs(name))
            except Exception as exc:  # noqa: BLE001 -- unreadable store must preserve
                refs.append(f"active-work lookup failed ({type(exc).__name__})")
        try:
            if self.terminal._session_delete_runtime_blocker(name, info):
                refs.append("lease/recovery in progress")
        except Exception:  # noqa: BLE001
            pass
        return refs

    def _pane(self, name: str, lines: int) -> str | None:
        try:
            return "\n".join(self.terminal.tmux.capture_lines(name, lines))
        except Exception:  # noqa: BLE001 -- unreadable pane is not IDLE
            return None

    def _last_input_epoch(self, name: str) -> float | None:
        lookup = self._last_input or getattr(getattr(self.terminal, "audit", None), "last_input_epoch", None)
        if lookup is None:
            return None
        try:
            return lookup(name)
        except Exception:  # noqa: BLE001 -- missing evidence is just missing
            return None

    def classify_session(self, info: Any) -> dict[str, Any]:
        # Activity = tmux's own stamp OR the controller's last delivered input
        # (send/create). Ink agent CLIs never advance the tmux stamp, so an
        # operator-driven session would otherwise look idle and be reaped.
        last_input = self._last_input_epoch(info.name)
        row = {"name": info.name, "attached": bool(info.attached),
               "command": info.pane_current_command, "cwd": info.pane_current_path or None,
               "activity_epoch": max(float(info.activity_epoch or 0), float(last_input or 0))}
        policy, now = self.policy, self._clock()
        protected = self._protected()
        spec = _match_required(info.name, tuple(getattr(policy, "required_services", ()) or ()))
        command = (row["command"] or "").lower()
        is_service = command not in {c.lower() for c in policy.agent_commands} \
            and command not in SHELL_COMMANDS and command not in EDITOR_COMMANDS
        health = None
        if spec is not None and is_service and info.name not in protected and not row["attached"]:
            try:
                health = self._health_check(info, spec)
            except Exception as exc:  # noqa: BLE001 -- a failed probe is "not verified healthy"
                health = (False, f"health check failed ({type(exc).__name__})")
        # Cheap checks first: only an idle, detached pane pays for a pane
        # capture and git subprocesses.
        pre = classify(row, policy=policy, protected=protected, now=now, active_refs=[],
                       pane_state="IDLE", git={"exists": True, "is_repo": False},
                       service_health=health)
        if pre["classification"] in _DECIDED_WITHOUT_PROBES:
            return pre
        refs = self._refs(info)
        actions = ["checked_task_ownership"]
        output = self._pane(info.name, 80)
        pane_state = classify_status(info, output, now=int(now))[0] if output is not None else None
        if not is_service and pane_state in (None, "UNKNOWN"):
            # Stabilization: one re-probe with a deeper capture before
            # concluding the pane is uninspectable.
            actions.append("reprobed_pane")
            retry = self._pane(info.name, 200)
            if retry is not None:
                output, pane_state = retry, classify_status(info, retry, now=int(now))[0]
        git = self._git_probe(row["cwd"]) if row["cwd"] and not is_service else None
        result = classify(row, policy=policy, protected=protected, now=now, active_refs=refs,
                          pane_state=pane_state, git=git, service_health=health)
        result["pane_state"] = pane_state
        result["recovery_actions"] = actions
        result["pane_hash"] = (hashlib.sha1(output.encode("utf-8", "replace")).hexdigest()
                               if output is not None else None)
        if git is not None:
            result["git"] = {k: git.get(k) for k in ("exists", "is_repo", "toplevel", "branch", "dirty",
                                                       "dirty_entries", "unmerged", "linked_worktree")}
        return result

    @staticmethod
    def _identity(info: Any) -> str:
        return f"{getattr(info, 'session_id', '')}@{getattr(info, 'created_epoch', '')}"

    def _evaluate(self, row: dict[str, Any], identity: str, record: dict[str, Any] | None,
                  now: float) -> dict[str, Any]:
        """Lifecycle state for one classified row given its stored record.
        Pure: returns the record that should be stored."""
        grace = float(getattr(self.policy, "recovery_grace_minutes", 30.0)) * 60
        if record is not None and record.get("identity") != identity:
            record = None  # same name, different tmux session: start fresh
        previous_state = record.get("state") if record else None
        new = {"session": row["session"], "identity": identity,
               "classification": row["classification"], "reason": row["reason"],
               "last_observed_at": now, "updated_at": now, "pane_hash": row.get("pane_hash"),
               "recovery_attempts": (record or {}).get("recovery_attempts") or 0,
               "last_recovery_at": (record or {}).get("last_recovery_at"),
               "recovered_at": (record or {}).get("recovered_at"),
               "checkpoint": (record or {}).get("checkpoint"),
               "first_uncontrolled_at": None, "grace_expires_at": None, "closed_at": None}
        cls = row["classification"]
        if cls in CLOSE_CLASSES:
            new.update(state=CLEANUP_ELIGIBLE, reason=f"provably complete: {row['reason']}",
                       first_uncontrolled_at=(record or {}).get("first_uncontrolled_at") or now,
                       grace_expires_at=now)
            return new
        if cls in CONTROLLED_CLASSES:
            new["state"] = CONTROLLED
            if previous_state in _OPEN_STATES:
                new["recovered_at"] = now
                new["reason"] = f"recovered from {previous_state}: {row['reason']}"
            return new
        if cls in FAIL_CLOSED_CLASSES:
            new.update(state=BLOCKED, first_uncontrolled_at=(record or {}).get("first_uncontrolled_at") or now)
            return new
        first = now
        if previous_state in _OPEN_STATES and record.get("first_uncontrolled_at"):
            first = record["first_uncontrolled_at"]
            old_hash, new_hash = record.get("pane_hash"), row.get("pane_hash")
            if old_hash and new_hash and old_hash != new_hash:
                # The pane is producing output: someone or something is
                # driving it. Regained evidence of life resets the timer.
                first = now
                new["recovered_at"] = now
        expires = first + grace
        new.update(first_uncontrolled_at=first, grace_expires_at=expires)
        if now < expires:
            new.update(state=RECOVERY_REQUIRED,
                       reason=f"uncontrolled ({cls}): {row['reason']}; cleanup after grace "
                              f"unless control is regained")
        else:
            new.update(state=CLEANUP_ELIGIBLE,
                       reason=f"uncontrolled ({cls}) beyond {grace / 60:.0f}min grace: {row['reason']}")
        if previous_state == BLOCKED and record.get("reason", "").startswith("checkpoint failed"):
            new.update(state=BLOCKED, reason=record["reason"])
        return new

    def _annotate(self, row: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
        state = record["state"]
        row["lifecycle_state"] = state
        row["action"] = {CLEANUP_ELIGIBLE: "close", RECOVERY_REQUIRED: "recover"}.get(state, "preserve")
        row["lifecycle"] = public_lifecycle(record)
        return row

    def inspect(self, session: str | None = None, *, persist: bool = False) -> dict[str, Any]:
        """Classification + lifecycle state. Read-only unless persist=True
        (the reconcile pass), which records observations and timers."""
        sessions = self.terminal.tmux.list_sessions()
        if session is not None:
            sessions = [item for item in sessions if item.name == session]
        now = self._clock()
        try:
            records = self.store.all()
        except Exception:  # noqa: BLE001 -- unreadable store: report, do not guess timers
            _log.exception("session reconciler: lifecycle store unreadable")
            records = {}
        rows = []
        for info in sessions:
            row = self.classify_session(info)
            record = self._evaluate(row, self._identity(info), records.get(info.name), now)
            if persist:
                self.store.put(record)
            rows.append(self._annotate(row, record))
        if persist and session is None:
            self.store.mark_gone({info.name for info in sessions}, now)
        states: dict[str, int] = {}
        for row in rows:
            states[row["lifecycle_state"]] = states.get(row["lifecycle_state"], 0) + 1
        return {"sessions": rows, "session_count": len(rows), "lifecycle_states": states,
                "close_candidates": [r["session"] for r in rows if r["action"] == "close"],
                "recovery_required": [r["session"] for r in rows if r["action"] == "recover"],
                "blocked": [r["session"] for r in rows if r["lifecycle_state"] == BLOCKED]}

    def lifecycle_for(self, session: str) -> dict[str, Any] | None:
        """Stored lifecycle view for list/inspect annotations. Never raises."""
        try:
            return public_lifecycle(self.store.get(session))
        except Exception:  # noqa: BLE001 -- an annotation must never break a read
            return None

    def lifecycle_index(self) -> dict[str, dict[str, Any]]:
        try:
            return {name: public_lifecycle(rec) for name, rec in self.store.all().items()}
        except Exception:  # noqa: BLE001
            return {}

    def _capture(self, name: str) -> str | None:
        try:
            directory = Path(os.path.expanduser(self.policy.scrollback_dir))
            directory.mkdir(parents=True, exist_ok=True)
            lines = self.terminal.tmux.capture_lines(name, 3000)
            stamp = _dt.datetime.now().strftime("%Y-%m-%dT%H%M%S")
            target = directory / f"{stamp}-{name}.log"
            target.write_text("\n".join(lines), encoding="utf-8")
            return str(target)
        except Exception as exc:  # noqa: BLE001 -- backup failure must not block cleanup
            _log.warning("session reconciler: scrollback capture failed for %s: %s", name, exc)
            return None

    def _checkpoint(self, row: dict[str, Any], record: dict[str, Any]) -> dict[str, Any] | None:
        """Checkpoint unique git work when there is any. None = nothing to do."""
        git = row.get("git") or {}
        if not getattr(self.policy, "checkpoint_dirty_work", True):
            return None
        if git.get("is_repo") is not True or not (git.get("dirty") or git.get("unmerged")):
            return None
        return checkpoint_git(row["cwd"], row["session"],
                              patch_dir=Path(os.path.expanduser(self.policy.scrollback_dir)) / "checkpoints",
                              max_untracked_bytes=int(getattr(self.policy, "checkpoint_max_untracked_mb", 200))
                              * 1024 * 1024,
                              previous=record.get("checkpoint"))

    def run(self, *, dry_run: bool | None = None) -> dict[str, Any]:
        """One pass. dry_run=None follows policy.dry_run. Observations and
        timers are recorded even in a dry run (that is what makes the dry
        run's grace accounting honest); checkpoints and closes are not."""
        policy = self.policy
        dry = policy.dry_run if dry_run is None else bool(dry_run)
        with self._lock:
            report = self.inspect(persist=True)
            closed: list[dict[str, Any]] = []
            refused: list[dict[str, Any]] = []
            checkpoints: list[dict[str, Any]] = []
            for row in report["sessions"]:
                if dry or not (row["lifecycle_state"] in (RECOVERY_REQUIRED, CLEANUP_ELIGIBLE)
                               or _checkpoint_blocked(row.get("lifecycle"))):
                    continue
                record = self.store.get(row["session"]) or {}
                if row["lifecycle_state"] == RECOVERY_REQUIRED:
                    # Stabilization attempt: ownership/pane were re-probed in
                    # classify_session; secure recoverable git work now so a
                    # later cleanup can never be the first checkpoint.
                    record["recovery_attempts"] = (record.get("recovery_attempts") or 0) + 1
                    record["last_recovery_at"] = self._clock()
                    checkpoint = self._checkpoint(row, record)
                    if checkpoint is not None:
                        if not checkpoint.get("ok"):
                            row.setdefault("recovery_actions", []).append(
                                f"checkpoint_failed:{checkpoint.get('error')}")
                        elif checkpoint.get("kind") != "none":
                            record["checkpoint"] = checkpoint
                            checkpoints.append({"session": row["session"], **checkpoint})
                    record["updated_at"] = self._clock()
                    self.store.put(record)
                    row["lifecycle"] = public_lifecycle(record)
                    continue
                if len(closed) >= policy.max_closes_per_run:
                    continue
                name = row["session"]
                info = self.terminal.tmux.get_session(name)
                if info is None:
                    continue
                fresh = self.classify_session(info)
                fresh_record = self._evaluate(fresh, self._identity(info), record, self._clock())
                if not (fresh_record["state"] == CLEANUP_ELIGIBLE or _checkpoint_blocked(fresh_record)):
                    self.store.put(fresh_record)
                    refused.append({**self._annotate(fresh, fresh_record), "refused": "NO_LONGER_A_CANDIDATE"})
                    continue
                checkpoint = self._checkpoint(fresh, fresh_record)
                if checkpoint is not None:
                    if not checkpoint.get("ok"):
                        fresh_record.update(state=BLOCKED, reason=f"checkpoint failed "
                                            f"({checkpoint.get('error')}); unique work would be lost")
                        self.store.put(fresh_record)
                        refused.append({**self._annotate(fresh, fresh_record),
                                        "refused": "CHECKPOINT_FAILED", "checkpoint_error": checkpoint})
                        continue
                    if checkpoint.get("kind") != "none":
                        fresh_record["checkpoint"] = checkpoint
                        checkpoints.append({"session": name, **checkpoint})
                if self._preflight is not None:
                    pre = self._preflight(name)
                    if "error" in pre:
                        self.store.put(fresh_record)
                        refused.append({**self._annotate(fresh, fresh_record), "refused": pre["error"]})
                        continue
                scrollback = self._capture(name)
                result = self.terminal.terminal_delete_session(name, confirm=True, requested_by=ACTOR)
                if "error" in result:  # an already-gone session is success-shaped
                    self.store.put(fresh_record)
                    refused.append({**self._annotate(fresh, fresh_record), "refused": result["error"]})
                    continue
                if self._after_delete is not None:
                    try:
                        self._after_delete(name)
                    except Exception:  # noqa: BLE001 -- the session is already gone
                        _log.exception("session reconciler: after_delete failed for %s", name)
                now = self._clock()
                fresh_record.update(state=CLOSED, closed_at=now, updated_at=now,
                                    reason=f"closed: {fresh_record['reason']}")
                self.store.put(fresh_record)
                self.terminal.audit.record(action="reconcile_agent_session", session=name,
                                           result="DELETED",
                                           reason=f"{CLEANUP_ELIGIBLE}:{fresh['classification']}",
                                           actor=ACTOR)
                closed.append({**self._annotate(fresh, fresh_record), "scrollback": scrollback,
                               "checkpoint": checkpoint})
        report.update({"dry_run": dry, "closed": closed, "refused": refused, "checkpoints": checkpoints,
                       "policy": {"enabled": policy.enabled, "idle_hours": policy.idle_hours,
                                  "recovery_grace_minutes": getattr(policy, "recovery_grace_minutes", None),
                                  "agent_commands": list(policy.agent_commands),
                                  "required_services": [item.session for item in
                                                        getattr(policy, "required_services", ()) or ()],
                                  "close_clean_primary_checkouts": policy.close_clean_primary_checkouts,
                                  "max_closes_per_run": policy.max_closes_per_run}})
        return report


class SessionReconcileLoop:
    """Runs SessionReconciler.run on an interval. Started only when enabled."""

    def __init__(self, reconciler: SessionReconciler, interval_seconds: float) -> None:
        self._reconciler = reconciler
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_report: dict[str, Any] | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="terminal-mcp-session-reconcile",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                report = self._reconciler.run()
                self.last_report = {key: report[key] for key in
                                    ("dry_run", "close_candidates", "recovery_required", "blocked")}
                self.last_report["closed"] = [row["session"] for row in report["closed"]]
                if report["closed"] or report["close_candidates"] or report["recovery_required"]:
                    _log.info("session reconciler: dry_run=%s recovery_required=%s candidates=%s "
                              "closed=%s blocked=%s", report["dry_run"], report["recovery_required"],
                              report["close_candidates"], self.last_report["closed"], report["blocked"])
            except Exception:  # noqa: BLE001 -- a failed pass must not kill the loop
                _log.exception("session reconciler pass failed")
