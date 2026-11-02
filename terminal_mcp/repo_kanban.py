"""Repo Kanban: a read-only, multi-repo task board over boards that live IN
each repository (`docs/kanban/board.json` + `docs/kanban/live.json`, the
`repo-kanban/v1` contract in docs/repo-kanban.md).

WHY A REPO FILE AND NOT A TABLE HERE. The agents doing the work (Claude,
Codex, on any node) already commit to the repo; a board they update in the
same commit as the work cannot drift from it, survives this controller being
rebuilt, and is reviewed in the PR like any other change. This module only
READS those files and joins them with what this controller already knows:
the session registry (which session is on which repo + branch, on any node).

SECURITY POSTURE.
- Repositories come from config only (`repo_kanban.repos`), each path inside
  repo_read's allowed roots. No request parameter is ever a path or a ref.
- Files are read from a COMMITTED ref with `git show <ref>:<path>`, never
  from a worktree, so an agent's uncommitted edits never reach the page.
- Every card field is whitelisted, length-capped and passed through
  `redact()`; remotes lose any userinfo. Session rows expose name, node,
  agent type, state and branch only -- never cwd, launch command or notes.
- Errors are short codes plus a fixed message, never git stderr.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import RepoKanbanConfig, RepoKanbanRepo

COLUMNS = ("RUNNING", "NEW", "REVIEW", "BLOCKED", "DONE")
OWNERS = ("Claude", "Codex", "Owner", "External")
# Older boards (Startup Pixel before 2026-10-24) used these names.
LEGACY_STATUS = {"TODO": "NEW", "IN_PROGRESS": "RUNNING"}
QA_STATES = ("AT_HEAD", "DOCS_AHEAD", "STALE", "FAIL", "NONE")

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_SECRET_RE = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}"
    r"|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/-]{12,}|(?i:(?:password|passwd|token|secret|api[_-]?key))\s*[=:]\s*\S+)")
_TEXT_LIMIT = 600


def redact(value: Any, limit: int = _TEXT_LIMIT) -> str:
    """Board text is agent-written. Strip anything shaped like a credential
    and cap the length; the page renders it with textContent only."""
    text = _SECRET_RE.sub("[redacted]", str(value if value is not None else ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def normalize_remote(url: str | None) -> str | None:
    """`git@github.com:o/r.git`, `https://user:tok@github.com/o/r` and
    `https://github.com/o/r.git` all become `github.com/o/r` -- the userinfo
    never survives."""
    if not url:
        return None
    url = url.strip()
    match = re.match(r"^[\w.-]+@([\w.-]+):(.+)$", url)
    if match:
        host, path = match.groups()
    else:
        match = re.match(r"^[a-z+]+://(?:[^@/]*@)?([^/:]+)(?::\d+)?/(.+)$", url)
        if not match:
            return None
        host, path = match.groups()
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host.lower()}/{path}" if path else None


class RepoKanbanError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _iso_day(value: Any) -> str:
    return str(value)[:10] if value else ""


def _int_list(values: Any) -> list[int]:
    return [v for v in (values or []) if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 10_000_000][:20]


def card_warnings(card: dict[str, Any], branch: dict[str, Any] | None, prs: list[dict[str, Any]],
                  generated_at: str | None) -> list[str]:
    """Same drift rules as Startup Pixel's tools/kanban.mjs cardFacts: the
    sync never moves a card, it only says where the card and reality differ."""
    warnings: list[str] = []
    status = card["status"]
    if card.get("branch") and generated_at and not (branch or {}).get("exists"):
        warnings.append(f"nhánh {card['branch']} không có trên origin")
    if prs and all(pr.get("state") == "MERGED" for pr in prs) and status != "DONE":
        warnings.append("mọi PR đã merge nhưng thẻ chưa DONE")
    if any(pr.get("state") == "CLOSED" for pr in prs) and status != "DONE":
        warnings.append("có PR đã đóng không merge")
    if status == "RUNNING" and branch and not branch.get("dirtyWorktrees") and generated_at and branch.get("date"):
        try:
            age = datetime.fromisoformat(generated_at.replace("Z", "+00:00")) - datetime.fromisoformat(branch["date"].replace("Z", "+00:00"))
            if age.total_seconds() > 3 * 86_400:
                warnings.append("RUNNING nhưng 3 ngày không có commit")
        except ValueError:
            pass
    qa = (branch or {}).get("qa") or {}
    if status == "DONE" and qa.get("status") == "STALE" and card.get("owner") == "Claude":
        warnings.append("DONE nhưng code đổi sau lần QA PASS cuối")
    if status == "DONE" and qa.get("status") == "FAIL":
        warnings.append("DONE nhưng QA tại head FAIL")
    return warnings


class RepoKanbanService:
    def __init__(self, config: RepoKanbanConfig, *, allowed_roots: Iterable[str] = (),
                 sessions: Callable[[], Iterable[Any]] | None = None,
                 runner: Callable[[list[str], Path, float], subprocess.CompletedProcess] | None = None):
        self.config = config
        self.allowed_roots = tuple(Path(os.path.expanduser(root)).resolve() for root in allowed_roots)
        self._sessions = sessions
        self._run = runner or self._default_run

    @staticmethod
    def _default_run(argv: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"),
               "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False)

    # -- repo access ----------------------------------------------------
    def _repo(self, repo_id: str) -> RepoKanbanRepo:
        for repo in self.config.repos:
            if repo.id == repo_id:
                return repo
        raise RepoKanbanError("REPO_NOT_FOUND", "unknown repo id")

    def _root(self, repo: RepoKanbanRepo) -> Path:
        path = Path(os.path.expanduser(repo.path)).resolve()
        if self.allowed_roots and not any(path == root or root in path.parents for root in self.allowed_roots):
            raise RepoKanbanError("REPO_OUTSIDE_ALLOWED_ROOTS", "repo path is outside repo_read allowed roots")
        if not path.is_dir():
            raise RepoKanbanError("REPO_UNAVAILABLE", "repo path is not present on this controller")
        return path

    def _git(self, root: Path, *args: str) -> str | None:
        try:
            result = self._run(["git", "-C", str(root), *args], root, self.config.timeout_seconds)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        out = result.stdout
        if len(out) > self.config.max_bytes:
            return None
        return out.decode("utf-8", errors="replace") if isinstance(out, bytes) else out

    def _read_json(self, root: Path, ref: str, path: str) -> Any:
        text = self._git(root, "show", f"{ref}:{path}")
        if text is None:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RepoKanbanError("INVALID_JSON", f"{path} at {ref} is not valid JSON") from exc

    # -- sessions -------------------------------------------------------
    def _session_rows(self) -> list[Any]:
        if self._sessions is None:
            return []
        try:
            return list(self._sessions())
        except Exception:  # noqa: BLE001 -- the board must render without the registry
            return []

    @staticmethod
    def _session_view(record: Any) -> dict[str, Any]:
        return {
            "session_name": redact(getattr(record, "session_name", ""), 120),
            "node_id": redact(getattr(record, "node_id", ""), 60),
            "agent_type": redact(getattr(record, "agent_type", "") or "", 30),
            "status": redact(getattr(record, "status", "") or "", 30),
            "state": redact(getattr(record, "last_known_state", "") or "", 30),
            "branch": redact(getattr(record, "git_branch", "") or "", 200),
            "last_activity_at": redact(getattr(record, "last_activity_at", None) or getattr(record, "last_seen_at", "") or "", 40),
        }

    def _sessions_for(self, remote: str | None) -> list[dict[str, Any]]:
        if not remote:
            return []
        rows = [row for row in self._session_rows()
                if normalize_remote(getattr(row, "git_remote", None)) == remote
                and getattr(row, "status", "") not in ("DELETED", "KILLED")]
        return [self._session_view(row) for row in rows]

    # -- board ----------------------------------------------------------
    def _load(self, repo: RepoKanbanRepo) -> dict[str, Any]:
        root = self._root(repo)
        head = self._git(root, "log", "-1", "--format=%H%x00%cI", repo.ref, "--")
        if head is None:
            raise RepoKanbanError("REF_NOT_FOUND", "configured ref does not exist in this checkout")
        sha, date = (head.strip().split("\0") + [""])[:2]
        remote = normalize_remote((self._git(root, "remote", "get-url", "origin") or "").strip())
        board = self._read_json(root, repo.ref, repo.board_path)
        if board is None:
            raise RepoKanbanError("BOARD_MISSING", f"no {repo.board_path} at {repo.ref}")
        if not isinstance(board, dict) or not isinstance(board.get("cards"), list):
            raise RepoKanbanError("INVALID_BOARD", "board.cards must be a list")
        live = self._read_json(root, repo.ref, repo.live_path)
        return {"root": root, "sha": sha, "date": date, "remote": remote, "board": board,
                "live": live if isinstance(live, dict) else None}

    def _cards(self, loaded: dict[str, Any], sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        board, live = loaded["board"], loaded["live"] or {}
        branches = live.get("branches") if isinstance(live.get("branches"), dict) else {}
        prs_live = live.get("prs") if isinstance(live.get("prs"), dict) else {}
        issues_live = live.get("issues") if isinstance(live.get("issues"), dict) else {}
        generated_at = live.get("generatedAt") if isinstance(live.get("generatedAt"), str) else None
        cards = []
        for raw in board["cards"][:500]:
            if not isinstance(raw, dict):
                continue
            status = LEGACY_STATUS.get(raw.get("status"), raw.get("status"))
            card = {
                "id": redact(raw.get("id"), 60), "title": redact(raw.get("title")),
                "owner": raw.get("owner") if raw.get("owner") in OWNERS else "Unknown",
                "status": status if status in COLUMNS else "NEW",
                "status_raw": redact(raw.get("status"), 30),
                "branch": redact(raw.get("branch") or "", 200),
                "evidence": redact(raw.get("evidence") or ""), "next": redact(raw.get("next") or ""),
                "updated": _iso_day(raw.get("updated")),
            }
            problems = []
            if status not in COLUMNS:
                problems.append(f"trạng thái không hợp lệ: {card['status_raw']}")
            if card["owner"] == "Unknown":
                problems.append("owner không hợp lệ")
            if card["status"] in ("DONE", "REVIEW") and not card["evidence"]:
                problems.append(f"{card['status']} nhưng thiếu bằng chứng")
            branch_live = branches.get(raw.get("branch")) if raw.get("branch") else None
            branch = None
            if isinstance(branch_live, dict):
                qa = branch_live.get("qa") if isinstance(branch_live.get("qa"), dict) else None
                branch = {
                    "exists": bool(branch_live.get("exists")),
                    "sha": branch_live.get("sha") if _SHA_RE.match(str(branch_live.get("sha") or "")) else None,
                    "date": redact(branch_live.get("date") or "", 40),
                    "subject": redact(branch_live.get("subject") or "", 200),
                    "dirtyWorktrees": int(branch_live.get("dirtyWorktrees") or 0),
                    "qa": {"status": qa.get("status") if qa.get("status") in QA_STATES else "NONE",
                           "commit": qa.get("commit") if _SHA_RE.match(str(qa.get("commit") or "")) else None,
                           "run": redact(qa.get("run") or "", 80)} if qa else None,
                }
            prs = []
            for number in _int_list(raw.get("prs")):
                pr = prs_live.get(str(number)) or {}
                prs.append({"number": number, "state": redact(pr.get("state") or "UNKNOWN", 20),
                            "draft": bool(pr.get("isDraft")), "updated_at": redact(pr.get("updatedAt") or "", 40)})
            issues = [{"number": n, "state": redact((issues_live.get(str(n)) or {}).get("state") or "UNKNOWN", 20)}
                      for n in _int_list(raw.get("issues"))]
            dates = sorted(d for d in [card["updated"], _iso_day((branch or {}).get("date")),
                                       *(_iso_day(pr["updated_at"]) for pr in prs)] if d)
            card.update({
                "branch_live": branch, "prs": prs, "issues": issues,
                "last_updated": dates[-1] if dates else card["updated"],
                "warnings": problems + card_warnings({**card, "branch": raw.get("branch")}, branch_live if isinstance(branch_live, dict) else None, prs, generated_at),
                "sessions": [s for s in sessions if card["branch"] and s["branch"] == card["branch"]],
            })
            cards.append(card)
        return cards

    def _summary(self, repo: RepoKanbanRepo, loaded: dict[str, Any] | None, cards: list[dict[str, Any]],
                 sessions: list[dict[str, Any]], error: RepoKanbanError | None) -> dict[str, Any]:
        counts = {column: {owner: 0 for owner in (*OWNERS, "Unknown")} for column in COLUMNS}
        for card in cards:
            counts[card["status"]][card["owner"]] += 1
        live = (loaded or {}).get("live") or {}
        integration = live.get("integration") if isinstance(live.get("integration"), dict) else {}
        board = (loaded or {}).get("board") or {}
        remote = (loaded or {}).get("remote")
        return {
            "id": repo.id, "name": repo.name, "ref": repo.ref,
            "status": "ERROR" if error else "OK",
            "error": {"code": error.code, "message": error.message} if error else None,
            "remote": remote,
            "identity": f"git:{remote}" if remote else None,
            "project": repo.project,
            "github_url": f"https://{remote}" if remote and remote.startswith("github.com/") else None,
            "ref_sha": (loaded or {}).get("sha"), "ref_date": (loaded or {}).get("date"),
            "board_updated": _iso_day(board.get("updated")),
            "live_generated_at": redact(live.get("generatedAt") or "", 40) or None,
            "live_generated_by": redact(live.get("generatedBy") or "", 60) or None,
            "integration": {
                "branch": redact(integration.get("branch") or board.get("integrationBranch") or "", 200),
                "sha": integration.get("sha") if _SHA_RE.match(str(integration.get("sha") or "")) else None,
                "qa": (integration.get("qa") or {}).get("status") if isinstance(integration.get("qa"), dict) else None,
            } if (integration or board.get("integrationBranch")) else None,
            "counts": counts, "total": len(cards),
            "warnings": sum(len(card["warnings"]) for card in cards),
            "sessions": {"total": len(sessions), "active": sum(1 for s in sessions if s["status"] == "ACTIVE")},
            "last_updated": max([card["last_updated"] for card in cards if card["last_updated"]] or [_iso_day((loaded or {}).get("date"))]),
        }

    def _build(self, repo: RepoKanbanRepo) -> tuple[dict[str, Any], dict[str, Any] | None]:
        loaded, cards, sessions, error = None, [], [], None
        try:
            loaded = self._load(repo)
            sessions = self._sessions_for(loaded["remote"])
            cards = self._cards(loaded, sessions)
        except RepoKanbanError as exc:
            error = exc
            if loaded is None:
                # Still show the sessions on a repo whose board is missing.
                try:
                    root = self._root(repo)
                    sessions = self._sessions_for(normalize_remote((self._git(root, "remote", "get-url", "origin") or "").strip()))
                except RepoKanbanError:
                    sessions = []
        return self._summary(repo, loaded, cards, sessions, error), {"cards": cards, "sessions": sessions, "loaded": loaded}

    # -- public API -----------------------------------------------------
    def list_projects(self) -> dict[str, Any]:
        if not self.config.enabled:
            return {"enabled": False, "projects": [], "count": 0}
        projects = [self._build(repo)[0] for repo in self.config.repos]
        return {"enabled": True, "projects": projects, "count": len(projects), "columns": list(COLUMNS),
                "owners": list(OWNERS), "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    def get_board(self, repo_id: str) -> dict[str, Any]:
        if not self.config.enabled:
            raise RepoKanbanError("REPO_KANBAN_DISABLED", "repo_kanban is disabled")
        summary, detail = self._build(self._repo(repo_id))
        board = ((detail or {}).get("loaded") or {}).get("board") or {}
        live = ((detail or {}).get("loaded") or {}).get("live") or {}
        runs = []
        for run in (live.get("qaRuns") or [])[:10]:
            if isinstance(run, dict) and _SHA_RE.match(str(run.get("commit") or "")):
                runs.append({"id": redact(run.get("id"), 80), "commit": run["commit"], "passed": run.get("passed") is True,
                             "failed_step": redact(run.get("failedStep") or "", 120) or None,
                             "seconds": run.get("seconds") if isinstance(run.get("seconds"), (int, float)) else None,
                             "completed_at": redact(run.get("completedAt") or "", 40)})
        return {"project": summary, "columns": list(COLUMNS), "owners": list(OWNERS),
                "rules": [redact(rule, 200) for rule in (board.get("rules") or [])[:20] if isinstance(rule, str)],
                "preview": redact(board.get("preview") or "", 300) or None,
                "cards": (detail or {}).get("cards", []), "sessions": (detail or {}).get("sessions", [])[:200], "qa_runs": runs}
