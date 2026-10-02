"""Lifecycle reconciler for task-owned agent sessions.

WHY THIS EXISTS

The shell reaper (core._idle_reap_candidates, ~/.local/bin/session_guard.py)
only ever closes a pane sitting at a plain shell prompt, because a quiet
claude/codex pane may be an agent thinking. Correct, but it left a gap: once an
agent's work was handed off and merged and its worktree removed, nothing ever
closed the agent session itself. Each one keeps a full agent CLI resident
(hundreds of MB), and on dell-linux they piled up until the host was deep in
swap.

WHAT IT CLOSES -- TWO CLASSES, NOTHING ELSE

  COMPLETED_CLEAN          detached agent pane, IDLE at its composer for
                           idle_hours, no active task/run/lease, working tree
                           clean, and HEAD already contained in the repo's
                           default branch -- nothing unmerged or uncommitted
                           can be lost. A primary checkout (not a linked
                           worktree) additionally requires the operator flag
                           close_clean_primary_checkouts.
  ORPHAN_WORKTREE_MISSING  detached agent pane, idle for idle_hours, whose
                           working directory demonstrably no longer exists
                           (the worktree was removed after merge).

Everything else is PRESERVED with a named reason: protected and attached
sessions, every non-agent pane (servers, tunnels, builds -- SERVICE; plain
shells -- SHELL, governed by the shell reaper instead), active work, recent
activity, a pane that is not IDLE, dirty trees, unmerged branches, repos with
no resolvable default branch, and anything outside a git repository. Unknown is
never evidence.

HOW IT CLOSES

Each close re-reads the pane immediately before acting (classification is
never trusted from an earlier report), captures the scrollback to
scrollback_dir, runs the controller's deletion preflight when one is wired
(queue/journal/supervised-task refusal), then deletes through
TerminalService.terminal_delete_session(confirm=True) -- the same path an
operator's delete uses, so protected-set refusal, attachment/lease/recovery
blockers, grant/binding cleanup, registry and audit all apply unchanged.
Re-running is idempotent: a closed session simply no longer appears.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .status import classify_status

_log = logging.getLogger(__name__)

CLOSE_CLASSES = frozenset({"COMPLETED_CLEAN", "ORPHAN_WORKTREE_MISSING"})
SHELL_COMMANDS = frozenset({"bash", "sh", "zsh", "fish", "dash"})
DEFAULT_BASE_REFS = ("origin/HEAD", "origin/main", "origin/master", "main", "master")
ACTOR = "session-reconciler"
_DECIDED_WITHOUT_PROBES = frozenset({"PROTECTED", "ATTACHED", "SHELL", "SERVICE", "AGENT_RECENT"})


def _git(path: str, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                          timeout=timeout, check=False)


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


def classify(row: dict[str, Any], *, policy: Any, protected: set[str], now: float,
             active_refs: list[str], pane_state: str | None,
             git: dict[str, Any] | None) -> dict[str, Any]:
    """Pure decision for one session. `row` keys: name, attached, command,
    cwd, activity_epoch. Returns classification/reason/action/evidence."""

    def out(classification: str, reason: str, **evidence: Any) -> dict[str, Any]:
        return {"session": row["name"], "classification": classification,
                "action": "close" if classification in CLOSE_CLASSES else "preserve",
                "reason": reason, "command": row.get("command"), "cwd": row.get("cwd"),
                "idle_hours": round(idle_s / 3600, 2), **evidence}

    idle_s = max(0.0, now - float(row.get("activity_epoch") or 0))
    command = (row.get("command") or "").lower()
    agent_commands = {c.lower() for c in policy.agent_commands}
    if row["name"] in protected:
        return out("PROTECTED", "configured protected session")
    if row.get("attached"):
        return out("ATTACHED", "a client is attached")
    if command not in agent_commands:
        if command in SHELL_COMMANDS:
            return out("SHELL", "plain shell; governed by the shell idle reaper, not this policy")
        return out("SERVICE", f"non-agent process {command or 'unknown'!r} (server/tunnel/build) is never closed here")
    if active_refs:
        return out("ACTIVE_TASK", "session owns active work: " + ", ".join(active_refs))
    if idle_s < policy.idle_hours * 3600:
        return out("AGENT_RECENT", f"activity within the last {policy.idle_hours}h")
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
                   "enable close_clean_primary_checkouts to close these", branch=git.get("branch"))
    return out("COMPLETED_CLEAN", f"clean tree, {git.get('branch')!r} merged into "
               + ",".join(git.get("merged_into") or []), branch=git.get("branch"),
               linked_worktree=git.get("linked_worktree"))


class SessionReconciler:
    """Classifies this host's tmux sessions and closes the high-confidence ones."""

    def __init__(self, terminal: Any, *, policy: Any = None,
                 active_refs: Callable[[str], list[str]] | None = None,
                 preflight: Callable[[str], dict[str, Any]] | None = None,
                 after_delete: Callable[[str], Any] | None = None,
                 git_probe: Callable[[str], dict[str, Any]] = probe_git,
                 clock: Callable[[], float] = time.time) -> None:
        self.terminal = terminal
        self._policy = policy
        self._active_refs = active_refs
        self._preflight = preflight
        self._after_delete = after_delete
        self._git_probe = git_probe
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def policy(self) -> Any:
        return self._policy or self.terminal.config.session_lifecycle.agent_cleanup

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

    def classify_session(self, info: Any) -> dict[str, Any]:
        row = {"name": info.name, "attached": bool(info.attached),
               "command": info.pane_current_command, "cwd": info.pane_current_path or None,
               "activity_epoch": info.activity_epoch}
        policy, now = self.policy, self._clock()
        protected = self._protected()
        # Cheap checks first: only an idle, detached agent pane pays for a pane
        # capture and git subprocesses.
        pre = classify(row, policy=policy, protected=protected, now=now, active_refs=[],
                       pane_state="IDLE", git={"exists": True, "is_repo": False})
        if pre["classification"] in _DECIDED_WITHOUT_PROBES:
            return pre
        refs = self._refs(info)
        try:
            output = "\n".join(self.terminal.tmux.capture_lines(info.name, 80))
            pane_state = classify_status(info, output)[0]
        except Exception:  # noqa: BLE001 -- unreadable pane is not IDLE
            pane_state = None
        git = self._git_probe(row["cwd"]) if row["cwd"] else None
        return classify(row, policy=policy, protected=protected, now=now, active_refs=refs,
                        pane_state=pane_state, git=git)

    def inspect(self, session: str | None = None) -> dict[str, Any]:
        sessions = self.terminal.tmux.list_sessions()
        if session is not None:
            sessions = [item for item in sessions if item.name == session]
        rows = [self.classify_session(item) for item in sessions]
        return {"sessions": rows, "session_count": len(rows),
                "close_candidates": [r["session"] for r in rows if r["action"] == "close"]}

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

    def run(self, *, dry_run: bool | None = None) -> dict[str, Any]:
        """One pass. dry_run=None follows policy.dry_run."""
        policy = self.policy
        dry = policy.dry_run if dry_run is None else bool(dry_run)
        with self._lock:
            report = self.inspect()
            closed: list[dict[str, Any]] = []
            refused: list[dict[str, Any]] = []
            for row in report["sessions"]:
                if row["action"] != "close" or dry:
                    continue
                if len(closed) >= policy.max_closes_per_run:
                    break
                name = row["session"]
                info = self.terminal.tmux.get_session(name)
                if info is None:
                    continue
                fresh = self.classify_session(info)
                if fresh["action"] != "close":
                    refused.append({**fresh, "refused": "NO_LONGER_A_CANDIDATE"})
                    continue
                if self._preflight is not None:
                    pre = self._preflight(name)
                    if "error" in pre:
                        refused.append({**fresh, "refused": pre["error"]})
                        continue
                scrollback = self._capture(name)
                result = self.terminal.terminal_delete_session(name, confirm=True, requested_by=ACTOR)
                if "error" in result:
                    refused.append({**fresh, "refused": result["error"]})
                    continue
                if self._after_delete is not None:
                    try:
                        self._after_delete(name)
                    except Exception:  # noqa: BLE001 -- the session is already gone
                        _log.exception("session reconciler: after_delete failed for %s", name)
                self.terminal.audit.record(action="reconcile_agent_session", session=name,
                                           result="DELETED", reason=fresh["classification"],
                                           actor=ACTOR)
                closed.append({**fresh, "scrollback": scrollback})
        report.update({"dry_run": dry, "closed": closed, "refused": refused,
                       "policy": {"enabled": policy.enabled, "idle_hours": policy.idle_hours,
                                  "agent_commands": list(policy.agent_commands),
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
                self.last_report = {key: report[key] for key in ("dry_run", "close_candidates")}
                self.last_report["closed"] = [row["session"] for row in report["closed"]]
                if report["closed"] or report["close_candidates"]:
                    _log.info("session reconciler: dry_run=%s candidates=%s closed=%s",
                              report["dry_run"], report["close_candidates"],
                              self.last_report["closed"])
            except Exception:  # noqa: BLE001 -- a failed pass must not kill the loop
                _log.exception("session reconciler pass failed")
