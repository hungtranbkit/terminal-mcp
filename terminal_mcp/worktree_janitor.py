"""Worktree Janitor P0 -- the classification engine. AUDIT-ONLY.

Contract: docs/WORKTREE_JANITOR.md. This module implements §5 (classification)
and nothing else.

STRUCTURALLY INCAPABLE OF DELETING ANYTHING. There is no `shutil.rmtree`, no
`os.remove`/`unlink`/`rmdir`, no `git worktree remove`, no `git worktree prune`
and no `--force` anywhere in this file, and `tests/test_worktree_janitor.py`
asserts that by walking this module's AST for call names and by checking the
git subcommand allowlist below. A future edit that tries to add a removal path
fails a test rather than shipping.

The posture that matters most: UNKNOWN IS NOT SAFE. Every predicate answers
True / False / UNKNOWN, and an UNKNOWN can only ever drag a candidate toward
REVIEW or BLOCKED -- never toward AUTO_SAFE. A node that cannot be reached, a
git call that fails, a permission error, a detached HEAD, evidence that has
gone stale: all of them mean "we did not establish this", and the janitor
treats not-established as not-safe. That is the whole reason this is a separate
classification pass instead of a check inside a delete function.

Why classification is its own module, run before any executor exists: the
expensive mistake here is not "we failed to reclaim a worktree", it is "we
deleted work that existed nowhere else". So the deciding logic ships first, is
exercised against real repositories, and can be run in observe_only against a
real fleet for as long as an operator wants, with nothing capable of acting on
its output.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# -- policy classes ------------------------------------------------------
AUTO_SAFE = "AUTO_SAFE"
REVIEW = "REVIEW"
BLOCKED = "BLOCKED"
UNKNOWN = "UNKNOWN"
POLICY_CLASSES = (AUTO_SAFE, REVIEW, BLOCKED, UNKNOWN)
"""UNKNOWN is a real, reportable class, not a synonym for REVIEW. REVIEW means
"we looked and a human should decide"; UNKNOWN means "we could not even
establish the facts" (the whole node was unreachable, the repo would not
answer). Both are non-actionable, but an operator needs to tell a judgement
call apart from a broken probe -- collapsing them hides outages."""

# Ordering for combining predicate outcomes. A candidate lands in the WORST
# class any predicate produced. BLOCKED outranks UNKNOWN outranks REVIEW
# outranks AUTO_SAFE: a positively dangerous fact is more important to report
# than a failure to look.
_SEVERITY = {AUTO_SAFE: 0, REVIEW: 1, UNKNOWN: 2, BLOCKED: 3}

# -- reason codes (contract §5) ------------------------------------------
MAIN_WORKTREE = "MAIN_WORKTREE"
PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
SYMLINK_OR_MOUNT = "SYMLINK_OR_MOUNT"
DIRTY = "DIRTY"
UNMERGED_UNPUSHED = "UNMERGED_UNPUSHED"
PRESERVED_UNMERGED = "PRESERVED_UNMERGED"
DETACHED_HEAD = "DETACHED_HEAD"
VALUABLE_IGNORED_DATA = "VALUABLE_IGNORED_DATA"
PROCESS_IN_USE = "PROCESS_IN_USE"
TMUX_IN_USE = "TMUX_IN_USE"
SESSION_IN_USE = "SESSION_IN_USE"
SERVICE_ROOT = "SERVICE_ROOT"
EVIDENCE_STALE = "EVIDENCE_STALE"
NODE_UNREACHABLE = "NODE_UNREACHABLE"
GRACE_NOT_ELAPSED = "GRACE_NOT_ELAPSED"
ADMIN_ENTRY_STALE = "ADMIN_ENTRY_STALE"
PERMISSION_DENIED = "PERMISSION_DENIED"
TASK_NOT_FINAL = "TASK_NOT_FINAL"
ORPHAN_UNCONFIRMED = "ORPHAN_UNCONFIRMED"
GIT_UNAVAILABLE = "GIT_UNAVAILABLE"
NOT_A_WORKTREE = "NOT_A_WORKTREE"
CLEAN_AND_MERGED = "CLEAN_AND_MERGED"

REASON_CODES: tuple[str, ...] = (
    MAIN_WORKTREE, PATH_NOT_ALLOWED, SYMLINK_OR_MOUNT, DIRTY, UNMERGED_UNPUSHED,
    PRESERVED_UNMERGED, DETACHED_HEAD, VALUABLE_IGNORED_DATA, PROCESS_IN_USE,
    TMUX_IN_USE, SESSION_IN_USE, SERVICE_ROOT, EVIDENCE_STALE, NODE_UNREACHABLE,
    GRACE_NOT_ELAPSED, ADMIN_ENTRY_STALE, PERMISSION_DENIED, TASK_NOT_FINAL,
    ORPHAN_UNCONFIRMED, GIT_UNAVAILABLE, NOT_A_WORKTREE, CLEAN_AND_MERGED,
)

# -- the read-only git boundary ------------------------------------------
# Mirrors repo_read.READ_ONLY_GIT_SUBCOMMANDS' approach. `worktree` is
# deliberately ABSENT even though `git worktree list` is read-only: listing is
# done by git_worktree.list_worktrees(), and allowing the subcommand here would
# put `worktree remove`/`prune` one argument away from a future careless edit.
READ_ONLY_GIT_SUBCOMMANDS = frozenset({
    "rev-parse", "status", "merge-base", "for-each-ref", "log", "rev-list",
    "symbolic-ref", "config",
})

# Ignored paths that are cheap to recreate. Not a security boundary -- purely
# "does losing this cost anything".
SAFE_CACHE_GLOBS: tuple[str, ...] = (
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".ropeproject",
    "node_modules", "dist", "build", ".venv", "venv", "*.pyc", "*.pyo",
    ".tox", ".next", "target", "*.egg-info", ".cache", ".parcel-cache",
)

# Ignored paths whose loss is real. Presence => BLOCKED. Seeded from the list
# this project already maintains for credential files so the two cannot drift.
VALUABLE_IGNORED_GLOBS: tuple[str, ...] = (
    "*.db", "*.sqlite", "*.sqlite3", "*.db-wal", "*.db-shm",
    ".env", ".env.*", "*.env", ".envrc",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", ".netrc", ".pgpass",
    "*credential*", "*.token", "secrets.yaml", "secrets.yml", "secrets.json",
    "node-agent.env", ".git-credentials", ".npmrc", ".pypirc",
    "evidence", "evidence/*", "*.dump", "*.sql",
)


def _credential_names() -> tuple[str, ...]:
    """redaction.CREDENTIAL_FILE_NAMES, if importable. Folded in rather than
    copied so a credential name added there is automatically protected here
    too."""
    try:
        from .redaction import CREDENTIAL_FILE_NAMES

        return tuple(CREDENTIAL_FILE_NAMES)
    except Exception:  # noqa: BLE001 -- never let an import break classification
        return ()


@dataclass(frozen=True)
class JanitorPolicy:
    """The classification boundary. Built from config for the real server and
    inline in tests, so every rule is exercised without a config file.

    `mode` exists here only so a report can state which mode produced it; this
    module never acts, in any mode."""

    mode: str = "observe_only"  # observe_only | suggest_only | auto_execute
    allowed_roots: tuple[str, ...] = ()
    integration_ref: str = "main"
    allow_preserved_unmerged: bool = False
    grace_seconds: int = 86_400
    max_evidence_age_seconds: float = 120.0
    timeout_seconds: float = 20.0
    extra_valuable_globs: tuple[str, ...] = ()
    # Additive only. There is deliberately no key that REMOVES a built-in
    # valuable glob, so no config edit can make a credential file collectable.
    safe_cache_globs: tuple[str, ...] = field(default=SAFE_CACHE_GLOBS, compare=False)
    valuable_globs: tuple[str, ...] = field(default=VALUABLE_IGNORED_GLOBS, compare=False)

    def all_valuable_globs(self) -> tuple[str, ...]:
        return (*self.valuable_globs, *_credential_names(), *self.extra_valuable_globs)

    def resolved_roots(self) -> list[Path]:
        roots: list[Path] = []
        for root in self.allowed_roots:
            try:
                roots.append(Path(root).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                continue
        return roots


@dataclass(frozen=True)
class Predicate:
    """One check's outcome. `value` is True (satisfied), False (violated) or
    None (could not be evaluated) -- the three-way result is the mechanism that
    makes UNKNOWN impossible to lose."""

    name: str
    value: bool | None
    outcome: str          # the policy class this predicate alone implies
    reason: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "outcome": self.outcome,
                "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class Classification:
    policy_class: str
    reasons: tuple[str, ...]
    predicates: tuple[Predicate, ...]
    evidence: dict[str, Any] = field(default_factory=dict)
    worktree_path: str = ""
    branch: str | None = None
    head: str | None = None
    task_id: str | None = None
    node_id: str | None = None
    size_bytes: int | None = None
    size_partial: bool = False

    @property
    def actionable(self) -> bool:
        """AUTO_SAFE alone. Exposed as one property so no caller re-derives
        this from `policy_class` and gets it subtly wrong."""
        return self.policy_class == AUTO_SAFE

    def to_dict(self) -> dict[str, Any]:
        return {
            "worktree_path": self.worktree_path, "policy_class": self.policy_class,
            "actionable": self.actionable, "reasons": list(self.reasons),
            "predicates": [p.to_dict() for p in self.predicates],
            "evidence": self.evidence, "branch": self.branch, "head": self.head,
            "task_id": self.task_id, "node_id": self.node_id,
            "size_bytes": self.size_bytes, "size_partial": self.size_partial,
        }


# -- git plumbing (read-only) -------------------------------------------

def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
                "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat", "LC_ALL": "C"})
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def _git(cwd: str | Path, subcommand: str, *args: str,
         timeout: float = 20.0) -> tuple[int, str, str]:
    """The single choke point for git in this module.

    Raises ValueError on any subcommand outside READ_ONLY_GIT_SUBCOMMANDS --
    loudly, because reaching it means someone added a mutating call to a module
    whose entire contract is that it has none. Returncode -1 means git could
    not be run at all, which callers report as UNKNOWN rather than as a fact
    about the repo."""
    if subcommand not in READ_ONLY_GIT_SUBCOMMANDS:
        raise ValueError(
            f"worktree_janitor refuses the non-read-only git subcommand {subcommand!r}")
    for arg in args:
        if arg == "--force" or arg.startswith("--force="):
            raise ValueError("worktree_janitor never passes --force (invariant I1)")
    try:
        result = subprocess.run(["git", "--no-pager", subcommand, *args], cwd=str(cwd),
                                capture_output=True, text=True, errors="replace",
                                timeout=timeout, check=False, env=_git_env())
    except FileNotFoundError:
        return -1, "", "git is not installed"
    except subprocess.TimeoutExpired:
        return -1, "", f"git {subcommand} timed out after {timeout}s"
    except OSError as exc:
        return -1, "", f"git {subcommand} could not run: {exc}"
    return result.returncode, result.stdout, result.stderr


# -- individual predicates ----------------------------------------------

def predicate_not_main_worktree(worktree_path: str, policy: JanitorPolicy,
                                repo_roots: tuple[str, ...] = ()) -> Predicate:
    """I2. A linked worktree's `--git-dir` points at
    `<repo>/.git/worktrees/<name>` while `--git-common-dir` points at
    `<repo>/.git`; in the MAIN worktree the two are equal. Verified against
    real git before being relied on."""
    for root in repo_roots:
        try:
            if Path(worktree_path).resolve() == Path(root).expanduser().resolve():
                return Predicate("not_main_worktree", False, BLOCKED, MAIN_WORKTREE,
                                 "path is a configured repo root")
        except (OSError, RuntimeError, ValueError):
            continue
    code_a, git_dir, _ = _git(worktree_path, "rev-parse", "--git-dir",
                              timeout=policy.timeout_seconds)
    code_b, common, err = _git(worktree_path, "rev-parse", "--git-common-dir",
                               timeout=policy.timeout_seconds)
    if code_a != 0 or code_b != 0:
        return Predicate("not_main_worktree", None, UNKNOWN,
                         GIT_UNAVAILABLE if code_a == -1 or code_b == -1 else NOT_A_WORKTREE,
                         err.strip()[:200] or "could not resolve git dirs")

    def _abs(value: str) -> Path:
        candidate = Path(value.strip())
        return candidate if candidate.is_absolute() else (Path(worktree_path) / candidate)

    try:
        same = _abs(git_dir).resolve() == _abs(common).resolve()
    except (OSError, RuntimeError, ValueError):
        return Predicate("not_main_worktree", None, UNKNOWN, NOT_A_WORKTREE,
                         "git dirs did not resolve")
    if same:
        return Predicate("not_main_worktree", False, BLOCKED, MAIN_WORKTREE,
                         "--git-dir == --git-common-dir, so this is the main worktree")
    return Predicate("not_main_worktree", True, AUTO_SAFE)


def predicate_path_allowlisted(worktree_path: str, policy: JanitorPolicy) -> Predicate:
    """Symlinks are resolved BEFORE containment is checked, so a symlink inside
    an allowed root that points outside it is caught rather than trusted. A
    worktree path that IS a symlink, or a mount point, is refused outright: its
    identity can change under us, which is not a risk worth taking for a
    reclaim."""
    roots = policy.resolved_roots()
    if not roots:
        return Predicate("path_allowlisted", None, UNKNOWN, PATH_NOT_ALLOWED,
                         "no allowed_roots configured -- nothing is collectable")
    raw = Path(worktree_path)
    try:
        if raw.is_symlink():
            return Predicate("path_allowlisted", False, BLOCKED, SYMLINK_OR_MOUNT,
                             "worktree path is itself a symlink")
        if raw.is_mount():
            return Predicate("path_allowlisted", False, BLOCKED, SYMLINK_OR_MOUNT,
                             "worktree path is a mount point")
        resolved = raw.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        return Predicate("path_allowlisted", None, UNKNOWN, PERMISSION_DENIED, str(exc)[:200])
    if not any(resolved == root or root in resolved.parents for root in roots):
        return Predicate("path_allowlisted", False, BLOCKED, PATH_NOT_ALLOWED,
                         f"{resolved} is outside every allowed root")
    if _is_bind_or_foreign_mount(resolved):
        return Predicate("path_allowlisted", False, BLOCKED, SYMLINK_OR_MOUNT,
                         "path is referenced by a mount entry")
    return Predicate("path_allowlisted", True, AUTO_SAFE)


def _is_bind_or_foreign_mount(path: Path) -> bool:
    try:
        text = Path("/proc/mounts").read_text()
    except OSError:
        return False
    target = str(path)
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and (parts[1] == target or parts[1].startswith(target + "/")):
            return True
    return False


def predicate_clean(worktree_path: str, policy: JanitorPolicy) -> Predicate:
    code, out, err = _git(worktree_path, "status", "--porcelain",
                          timeout=policy.timeout_seconds)
    if code != 0:
        return Predicate("clean", None, UNKNOWN,
                         GIT_UNAVAILABLE if code == -1 else PERMISSION_DENIED,
                         err.strip()[:200])
    lines = [line for line in out.splitlines() if line.strip()]
    if lines:
        return Predicate("clean", False, BLOCKED, DIRTY,
                         f"{len(lines)} uncommitted change(s)")
    return Predicate("clean", True, AUTO_SAFE)


def predicate_merged_or_preserved(worktree_path: str, policy: JanitorPolicy) -> Predicate:
    """F2's guard. Merged into the integration ref is the strong case. Pushed
    to a remote that contains this exact commit is "preserved" -- the work is
    not lost, but it has not been integrated, so it is REVIEW unless an
    operator opted into collecting those. Detached HEAD has no branch identity
    to reason about and is never AUTO_SAFE."""
    code, branch, err = _git(worktree_path, "rev-parse", "--abbrev-ref", "HEAD",
                             timeout=policy.timeout_seconds)
    if code != 0:
        return Predicate("merged_or_preserved", None, UNKNOWN, GIT_UNAVAILABLE,
                         err.strip()[:200])
    branch = branch.strip()
    head_code, head, _ = _git(worktree_path, "rev-parse", "HEAD",
                              timeout=policy.timeout_seconds)
    if head_code != 0:
        return Predicate("merged_or_preserved", None, UNKNOWN, GIT_UNAVAILABLE,
                         "could not resolve HEAD")
    head = head.strip()
    if branch == "HEAD":
        return Predicate("merged_or_preserved", None, REVIEW, DETACHED_HEAD,
                         "detached HEAD -- no branch identity to check merge status against")

    merged_code, _, _ = _git(worktree_path, "merge-base", "--is-ancestor", head,
                             policy.integration_ref, timeout=policy.timeout_seconds)
    if merged_code == 0:
        return Predicate("merged_or_preserved", True, AUTO_SAFE, CLEAN_AND_MERGED,
                         f"HEAD is an ancestor of {policy.integration_ref}")
    if merged_code == -1:
        return Predicate("merged_or_preserved", None, UNKNOWN, GIT_UNAVAILABLE,
                         "merge-base could not run")

    # Not merged. Is it at least preserved on a remote?
    ref_code, refs, _ = _git(worktree_path, "for-each-ref", "--format=%(objectname)",
                             f"refs/remotes/*/{branch}", timeout=policy.timeout_seconds)
    preserved = False
    if ref_code == 0:
        for remote_sha in (line.strip() for line in refs.splitlines() if line.strip()):
            anc, _, _ = _git(worktree_path, "merge-base", "--is-ancestor", head, remote_sha,
                             timeout=policy.timeout_seconds)
            if anc == 0:
                preserved = True
                break
    if preserved:
        if policy.allow_preserved_unmerged:
            return Predicate("merged_or_preserved", True, AUTO_SAFE, PRESERVED_UNMERGED,
                             "not merged, but the exact commit exists on a remote")
        return Predicate("merged_or_preserved", None, REVIEW, PRESERVED_UNMERGED,
                         "pushed to a remote but not merged -- a human should decide")
    return Predicate("merged_or_preserved", False, BLOCKED, UNMERGED_UNPUSHED,
                     "not merged and not present on any remote -- this work exists "
                     "nowhere else")


def predicate_no_live_references(worktree_path: str, policy: JanitorPolicy, *,
                                 process_cwds: list[str] | None = None,
                                 tmux_paths: list[str] | None = None,
                                 session_paths: list[str] | None = None,
                                 service_roots: list[str] | None = None) -> Predicate:
    """Live-reference check. The probes are injectable so tests can assert the
    logic without spawning processes, and so a node-side caller can supply its
    OWN observations -- the controller must never answer this about another
    node's filesystem.

    None from a probe means "could not look", which is UNKNOWN, not "clear"."""
    try:
        target = Path(worktree_path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return Predicate("no_live_references", None, UNKNOWN, PERMISSION_DENIED,
                         "path did not resolve")

    def _inside(candidate: str) -> bool:
        try:
            other = Path(candidate).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        return other == target or target in other.parents

    probes = (("process", process_cwds, PROCESS_IN_USE),
              ("tmux", tmux_paths, TMUX_IN_USE),
              ("session", session_paths, SESSION_IN_USE),
              ("service", service_roots, SERVICE_ROOT))
    unknowns = [name for name, values, _ in probes if values is None]
    for name, values, reason in probes:
        if values is None:
            continue
        hits = [v for v in values if v and _inside(v)]
        if hits:
            return Predicate("no_live_references", False, BLOCKED, reason,
                             f"{name} reference inside the worktree: {hits[0]}")
    if unknowns:
        return Predicate("no_live_references", None, UNKNOWN, NODE_UNREACHABLE,
                         f"could not check: {', '.join(unknowns)}")
    return Predicate("no_live_references", True, AUTO_SAFE)


def collect_process_cwds() -> list[str] | None:
    """Local-only probe. Returns None when /proc is unavailable (non-Linux, or
    unreadable) so the caller reports UNKNOWN instead of a false all-clear."""
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    cwds: list[str] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cwds.append(os.readlink(entry / "cwd"))
        except OSError:
            continue  # a process we may not inspect, or one that just exited
    return cwds


def collect_tmux_paths() -> list[str] | None:
    """Every tmux pane's current path, on THIS host. None when tmux cannot be
    asked (not installed, no server running is distinguishable from a failure:
    "no server" legitimately means zero panes, which is an empty list, not
    UNKNOWN)."""
    try:
        result = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_current_path}"],
                                capture_output=True, text=True, timeout=10, check=False)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        stderr = (result.stderr or "").lower()
        if "no server running" in stderr or "no such file" in stderr:
            return []  # a real, confident "nothing is open"
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def collect_service_roots() -> list[str] | None:
    """WorkingDirectory of every systemd unit visible to this user. None when
    systemctl cannot be asked -- a service rooted in a worktree is exactly the
    kind of reference that must not be guessed at."""
    roots: list[str] = []
    for scope in ("--user", "--system"):
        try:
            result = subprocess.run(
                ["systemctl", scope, "show", "--all", "--property=WorkingDirectory",
                 "--property=Id", "*.service"],
                capture_output=True, text=True, timeout=15, check=False)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0 and not result.stdout.strip():
            continue
        for line in result.stdout.splitlines():
            if line.startswith("WorkingDirectory=") and len(line) > len("WorkingDirectory="):
                value = line.split("=", 1)[1].strip()
                if value and value != "/":
                    roots.append(value)
    return roots


def collect_local_probes(session_registry: Any = None) -> dict[str, Any]:
    """All four liveness probes for THIS host, in one call.

    Exists because three separate callers -- the sweep, the node-agent handler
    and LocalNodeClient -- each need the same set, and each of them originally
    forgot some or all of them. The consequence was not a crash: `classify`
    fail-closed on the missing probe, returned UNKNOWN, and the caller was
    silently inert in production while its tests passed by injecting probes the
    real caller never supplied. One function means one place to forget.

    A probe that cannot look returns None, which classifies UNKNOWN -- that is
    the point, and callers must not paper over it with an empty list.

    `session_paths` comes from the session registry, which is per-host: a
    controller must never answer this about another node's filesystem, which is
    why the registry is passed in rather than opened here."""
    probes: dict[str, Any] = {
        "process_cwds": collect_process_cwds(),
        "tmux_paths": collect_tmux_paths(),
        "service_roots": collect_service_roots(),
        "session_paths": None,
    }
    if session_registry is not None:
        try:
            records = session_registry.list()
            paths: list[str] = []
            for record in records:
                # A DELETED session no longer holds anything; every other status
                # (ACTIVE/MISSING/OFFLINE/KILLED) might still be resumed into
                # its own directory, so its cwd still counts as in use.
                if getattr(record, "status", None) == "DELETED":
                    continue
                for value in (getattr(record, "cwd", None), getattr(record, "repo_root", None)):
                    if value:
                        paths.append(value)
            probes["session_paths"] = paths
        except Exception:  # noqa: BLE001 -- unreadable registry => UNKNOWN, never []
            _LOGGER.warning("could not read the session registry for worktree probes",
                            exc_info=True)
    return probes


def classify_ignored_entries(entries: list[str], policy: JanitorPolicy) -> tuple[list[str], list[str]]:
    """-> (valuable, safe_cache). Paths only. Contents are never read."""
    valuable: list[str] = []
    safe: list[str] = []
    valuable_globs = [g.casefold() for g in policy.all_valuable_globs()]
    cache_globs = [g.casefold() for g in policy.safe_cache_globs]
    for raw in entries:
        rel = raw.strip().rstrip("/")
        if not rel:
            continue
        parts = [p.casefold() for p in Path(rel).parts]
        whole = "/".join(parts)
        if any(fnmatch.fnmatch(part, g) or fnmatch.fnmatch(whole, g)
               for part in parts for g in valuable_globs):
            valuable.append(rel)
        elif any(fnmatch.fnmatch(part, g) or fnmatch.fnmatch(whole, g)
                 for part in parts for g in cache_globs):
            safe.append(rel)
        else:
            # Unclassified ignored data is treated as VALUABLE. F3 is about
            # losing something that mattered, and "we did not recognise it"
            # is not evidence that it was worthless.
            valuable.append(rel)
    return valuable, safe


def predicate_no_valuable_ignored_data(worktree_path: str, policy: JanitorPolicy) -> Predicate:
    code, out, err = _git(worktree_path, "status", "--porcelain", "--ignored=matching",
                          timeout=policy.timeout_seconds)
    if code != 0:
        return Predicate("no_valuable_ignored_data", None, UNKNOWN, GIT_UNAVAILABLE,
                         err.strip()[:200])
    ignored = [line[3:] for line in out.splitlines() if line.startswith("!!")]
    valuable, safe = classify_ignored_entries(ignored, policy)
    if valuable:
        return Predicate("no_valuable_ignored_data", False, BLOCKED, VALUABLE_IGNORED_DATA,
                         # PATHS ONLY -- never contents.
                         f"{len(valuable)} ignored path(s) worth keeping: "
                         + ", ".join(sorted(valuable)[:5]))
    return Predicate("no_valuable_ignored_data", True, AUTO_SAFE, None,
                     f"{len(safe)} ignored path(s), all recreatable cache")


def predicate_evidence_fresh(collected_at: float | None, policy: JanitorPolicy, *,
                             now: float | None = None) -> Predicate:
    """F11. Evidence gathered minutes ago is not evidence about now."""
    if collected_at is None:
        return Predicate("evidence_fresh", None, UNKNOWN, EVIDENCE_STALE,
                         "no collection timestamp")
    age = (time.time() if now is None else now) - collected_at
    if age > policy.max_evidence_age_seconds:
        return Predicate("evidence_fresh", False, REVIEW, EVIDENCE_STALE,
                         f"evidence is {age:.0f}s old (max {policy.max_evidence_age_seconds:.0f}s)")
    return Predicate("evidence_fresh", True, AUTO_SAFE, None, f"age {age:.0f}s")


def predicate_task_final_and_grace_elapsed(task: dict[str, Any] | None, policy: JanitorPolicy, *,
                                           now: float | None = None) -> Predicate:
    """Contract §3. There is NO FAILED_FINAL status: terminal is
    (COMPLETED, SKIPPED, CANCELLED), and FAILED counts only once retries are
    exhausted. A bare FAILED is retryable and its worktree is the retry's
    working directory -- F1."""
    if task is None:
        return Predicate("task_final_and_grace_elapsed", None, REVIEW, ORPHAN_UNCONFIRMED,
                         "no task owns this worktree")
    status = str(task.get("status") or "")
    attempts = int(task.get("attempt_count") or 0)
    max_attempts = int(task.get("max_attempts") or 0)
    final = status in ("COMPLETED", "SKIPPED", "CANCELLED") or (
        status == "FAILED" and max_attempts > 0 and attempts >= max_attempts)
    if not final:
        return Predicate("task_final_and_grace_elapsed", False, BLOCKED, TASK_NOT_FINAL,
                         f"task status {status!r} is not final"
                         + (f" (attempt {attempts}/{max_attempts} -- retryable)"
                            if status == "FAILED" else ""))
    terminal_at = task.get("terminal_at")
    if terminal_at is None:
        return Predicate("task_final_and_grace_elapsed", None, UNKNOWN, GRACE_NOT_ELAPSED,
                         "no terminal transition timestamp")
    elapsed = (time.time() if now is None else now) - float(terminal_at)
    if elapsed < policy.grace_seconds:
        return Predicate("task_final_and_grace_elapsed", False, REVIEW, GRACE_NOT_ELAPSED,
                         f"{elapsed:.0f}s of {policy.grace_seconds}s grace elapsed")
    return Predicate("task_final_and_grace_elapsed", True, AUTO_SAFE)


# -- combining -----------------------------------------------------------

def combine(predicates: list[Predicate]) -> tuple[str, tuple[str, ...]]:
    """Worst outcome wins. UNKNOWN can never produce AUTO_SAFE -- that is the
    fail-closed rule, expressed once, here, rather than at each call site."""
    worst = AUTO_SAFE
    reasons: list[str] = []
    for predicate in predicates:
        if _SEVERITY[predicate.outcome] > _SEVERITY[worst]:
            worst = predicate.outcome
        if predicate.reason and predicate.outcome != AUTO_SAFE:
            if predicate.reason not in reasons:
                reasons.append(predicate.reason)
    if worst == AUTO_SAFE and not reasons:
        reasons.append(CLEAN_AND_MERGED)
    return worst, tuple(reasons)


def classify(worktree: dict[str, Any], policy: JanitorPolicy, *,
             task: dict[str, Any] | None = None,
             repo_roots: tuple[str, ...] = (),
             process_cwds: list[str] | None = None,
             tmux_paths: list[str] | None = None,
             session_paths: list[str] | None = None,
             service_roots: list[str] | None = None,
             evidence_collected_at: float | None = None,
             now: float | None = None) -> Classification:
    """Classify ONE worktree. Pure: reads git and the filesystem, returns a
    verdict, mutates nothing and deletes nothing.

    A worktree git still lists whose directory is gone is ADMIN_ENTRY_STALE --
    reported as REVIEW so the stale admin entry is visible without this module
    being the thing that prunes it (that is P2's job, under a lock, and only
    after positive identification -- F7)."""
    path = str(worktree.get("worktree_path") or "")
    predicates: list[Predicate] = []

    if not path:
        return Classification(UNKNOWN, (NOT_A_WORKTREE,), (), {}, worktree_path="")
    if not Path(path).is_dir():
        stale = Predicate("directory_exists", False, REVIEW, ADMIN_ENTRY_STALE,
                          "git lists this worktree but the directory is gone")
        return Classification(REVIEW, (ADMIN_ENTRY_STALE,), (stale,),
                              {"directory_exists": False}, worktree_path=path,
                              task_id=(task or {}).get("id"))

    predicates.append(predicate_not_main_worktree(path, policy, repo_roots))
    predicates.append(predicate_path_allowlisted(path, policy))
    predicates.append(predicate_clean(path, policy))
    predicates.append(predicate_merged_or_preserved(path, policy))
    predicates.append(predicate_no_live_references(
        path, policy, process_cwds=process_cwds, tmux_paths=tmux_paths,
        session_paths=session_paths, service_roots=service_roots))
    predicates.append(predicate_no_valuable_ignored_data(path, policy))
    predicates.append(predicate_evidence_fresh(evidence_collected_at, policy, now=now))
    predicates.append(predicate_task_final_and_grace_elapsed(task, policy, now=now))

    policy_class, reasons = combine(predicates)
    size, partial = reclaimable_bytes(path)
    code, branch, _ = _git(path, "rev-parse", "--abbrev-ref", "HEAD",
                           timeout=policy.timeout_seconds)
    head_code, head, _ = _git(path, "rev-parse", "HEAD", timeout=policy.timeout_seconds)
    return Classification(
        policy_class=policy_class, reasons=reasons, predicates=tuple(predicates),
        evidence={"mode": policy.mode, "integration_ref": policy.integration_ref,
                  "classified_at": time.time() if now is None else now,
                  "predicates": {p.name: p.value for p in predicates}},
        worktree_path=path, branch=branch.strip() if code == 0 else None,
        head=head.strip() if head_code == 0 else None,
        task_id=(task or {}).get("id"), node_id=str(worktree.get("node_id") or "") or None,
        size_bytes=size, size_partial=partial)


def reclaimable_bytes(path: str, *, max_entries: int = 200_000) -> tuple[int, bool]:
    """Best-effort size. Never follows symlinks, never crosses filesystems, and
    is bounded -- returns (bytes, partial). `partial=True` means the number is a
    floor, not a total, which a caller must not present as exact."""
    total = 0
    seen = 0
    partial = False
    try:
        root_dev = os.stat(path, follow_symlinks=False).st_dev
    except OSError:
        return 0, True
    for current, dirnames, filenames in os.walk(path, followlinks=False, onerror=lambda _: None):
        try:
            if os.stat(current, follow_symlinks=False).st_dev != root_dev:
                dirnames[:] = []
                partial = True
                continue
        except OSError:
            partial = True
            continue
        for name in filenames:
            seen += 1
            if seen > max_entries:
                return total, True
            try:
                stat = os.stat(os.path.join(current, name), follow_symlinks=False)
            except OSError:
                partial = True
                continue
            total += stat.st_size
    return total, partial


def scan(repo_path: str, policy: JanitorPolicy, *,
         tasks_by_worktree: dict[str, dict[str, Any]] | None = None,
         probe_local: bool = True, **overrides: Any) -> dict[str, Any]:
    """Classify every worktree a repo knows about. OBSERVE-ONLY: returns a
    report. Nothing here removes, prunes, or changes anything.

    Reuses git_worktree.list_worktrees rather than re-parsing
    `worktree list --porcelain` a second way."""
    from . import git_worktree

    worktrees = git_worktree.list_worktrees(repo_path)
    if not worktrees:
        return {"repo_path": repo_path, "mode": policy.mode, "error": GIT_UNAVAILABLE,
                "detail": "git listed no worktrees (not a repo, or git failed)",
                "candidates": [], "counts": {}}
    tasks_by_worktree = tasks_by_worktree or {}
    if probe_local:
        # Local probes only. A controller must NEVER answer these about another
        # node's filesystem -- that is P4's node-side endpoint, and a wrong
        # answer here is failure mode F4. A probe that cannot look returns None,
        # which classifies UNKNOWN rather than "clear".
        overrides.setdefault("process_cwds", collect_process_cwds())
        overrides.setdefault("tmux_paths", collect_tmux_paths())
        overrides.setdefault("service_roots", collect_service_roots())
        # session_paths stays caller-supplied: it comes from session_registry,
        # which the scanner does not own. Absent => UNKNOWN, deliberately.
    if "evidence_collected_at" not in overrides:
        overrides["evidence_collected_at"] = time.time()

    results: list[Classification] = []
    for entry in worktrees:
        path = entry.get("worktree_path", "")
        results.append(classify(entry, policy, task=tasks_by_worktree.get(path),
                                repo_roots=(repo_path,), **overrides))
    counts: dict[str, int] = {}
    for result in results:
        counts[result.policy_class] = counts.get(result.policy_class, 0) + 1
    actionable = [r for r in results if r.actionable]
    return {
        "repo_path": repo_path, "mode": policy.mode,
        "observe_only": policy.mode == "observe_only",
        "candidates": [r.to_dict() for r in results],
        "counts": counts, "total": len(results),
        "actionable_count": len(actionable),
        "reclaimable_bytes_if_actioned": sum(r.size_bytes or 0 for r in actionable),
        # Stated explicitly so nobody reads a worktree reclaim as relief for a
        # different filesystem (contract §9).
        "filesystem_note": "reclaim affects only the filesystem holding these "
                           "worktrees; it does not free any other mount",
        "executor_present": False,
    }
