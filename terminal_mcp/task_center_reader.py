"""Read-only adapter for the AI Gateway Task Center (chatgpt-ai-gateway #115).

WHY THIS EXISTS
---------------
The owner turns ChatGPT chats into work, and work gets missed when the only
place it is visible is a terminal that has gone quiet. The AI Gateway Task
Center is the ONE canonical registry for that work. The Văn phòng AI page
(/dashboard/office) shows it next to the live sessions so an idle agent can
never make an unfinished task disappear.

WHAT IT IS NOT
--------------
  * not a second queue or task store: nothing here is persisted; the only
    state is an in-memory last-good copy so a gateway outage shows the last
    known tasks marked STALE instead of an empty (= "nothing to do") list;
  * not a writer: only GET on three whitelisted paths. It never sends a CSRF
    header, never POSTs/PATCHes, and the gateway refuses writes without one;
  * not a ChatGPT-history reader: the gateway itself reports
    `chatgpt_history: not_connected`, and this adapter repeats what the
    gateway says instead of claiming more.

CONFIGURATION (opt-in, both required -- otherwise "Chưa kết nối")
  TMCP_TASK_CENTER_URL           https://<gateway-host>  (http only for loopback)
  TMCP_TASK_CENTER_SESSION_FILE  file holding the approved user's aigw_session
                                 value; must not be group/world readable.
The gateway scopes every row to that approved user, so the owner's tasks are
what this shows; the dashboard additionally limits the task list to the
operator/owner roles (see dashboard.py, /dashboard/api/office).

DERIVED VIEW
------------
Each gateway row is normalized and given `flags` (a task may carry several)
and one `bucket` (the most urgent flag) -- waiting_input, dispatch_uncertain,
blocked, needs_review, unknown and stale are deliberately DISTINCT. A task is
only `verified_done` when the gateway says so AND it carries evidence; an
evidence-free "done" is surfaced as needs_review. IDLE or COMPOSER on a linked
session never finishes anything: it adds the `agent_idle_open` flag instead.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable

URL_ENV = "TMCP_TASK_CENTER_URL"
SESSION_FILE_ENV = "TMCP_TASK_CENTER_SESSION_FILE"
COOKIE_NAME = "aigw_session"
API_PREFIX = "/api/workspace/task-center"
ALLOWED_PATHS = re.compile(r"^/api/workspace/task-center/(summary|tasks|tasks/tc_[A-Za-z0-9]{1,64})$")

PAGE_LIMIT = 100           # the gateway's own maximum
MAX_PAGES = 10             # 1000 tasks; beyond that the payload says `truncated`
MAX_BODY_BYTES = 2_000_000
TIMEOUT_SECONDS = 6.0
DEFAULT_TTL_SECONDS = 20.0
# An unfinished task nobody has touched for this long is STALE.
STALE_AFTER_SECONDS = 12 * 3600

STATES = ("new", "queued", "running", "waiting_input", "blocked", "needs_review",
          "verified_done", "unknown")
OPEN_STATES = tuple(s for s in STATES if s != "verified_done")
# Bucket priority: the first flag present wins. Most urgent for a person first.
BUCKET_ORDER = ("waiting_input", "dispatch_uncertain", "blocked", "needs_review", "unknown",
                "stale", "active", "verified_done")
FLAG_LABELS = {
    "waiting_input": "Chờ input",
    "dispatch_uncertain": "Dispatch chưa chắc",
    "blocked": "Bị chặn",
    "needs_review": "Cần review",
    "unknown": "Không rõ",
    "stale": "Cũ / không cập nhật",
    "active": "Đang làm",
    "verified_done": "Đã xác minh xong",
    "agent_idle_open": "Agent rảnh, task CHƯA xong",
    "done_without_evidence": "Báo xong nhưng thiếu bằng chứng",
}
# A linked session in one of these states is not working on the task.
IDLE_SESSION_STATES = {"IDLE", "DONE", "OFFLINE"}

CONNECTION_LABELS = {
    "connected": "Đã kết nối",
    "not_configured": "Chưa kết nối",
    "unauthorized": "Chưa kết nối (phiên Gateway hết hạn / không được duyệt)",
    "error": "Lỗi kết nối",
    "forbidden": "Không đủ quyền xem",
}

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
_TASK_TOKEN_RE = re.compile(r"\b((?:dt|tc|tq)_[0-9a-fA-F]{8,64})\b")
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_AC_HEADER_RE = re.compile(
    r"^\s*(?:#+\s*)?(?:\*\*)?(acceptance criteria|acceptance|tiêu chí nghiệm thu|tiêu chí|definition of done|AC)\b"
    r"(?:\*\*)?\s*[:\-–]?\s*(.*)$", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)]|\[[ xX]\])\s*(?:\[[ xX]\]\s*)?(.+)$")


class TaskCenterConfigError(ValueError):
    """Raised for a configured-but-unsafe URL; reported as an `error` state."""


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def validate_base_url(raw: str) -> str:
    """https anywhere; plain http only to a loopback host (a local gateway)."""
    parsed = urllib.parse.urlsplit((raw or "").strip())
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise TaskCenterConfigError("Task Center URL must be http(s)://host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise TaskCenterConfigError("Task Center URL must not carry credentials, query or fragment")
    if parsed.scheme == "http":
        host = parsed.hostname
        loopback = host == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if not loopback:
            raise TaskCenterConfigError("plain http is only allowed to a loopback gateway")
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def read_session_token(path: str) -> str:
    """The gateway session value. Refuses a file others can read."""
    info = os.stat(path)
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise TaskCenterConfigError("session file must be private (chmod 600)")
    with open(path, encoding="utf-8") as handle:
        token = handle.read().strip()
    if not token or len(token) > 512 or any(ch in token for ch in ";\r\n "):
        raise TaskCenterConfigError("session file does not hold a single cookie value")
    return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would re-send the session cookie somewhere we did not
    choose. Treat every redirect as an error instead."""

    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class HttpStatusError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def http_get_json(base_url: str, path: str, params: dict[str, Any] | None, token: str,
                  timeout: float = TIMEOUT_SECONDS) -> Any:
    """GET one whitelisted Task Center path. The only network call here."""
    if not ALLOWED_PATHS.match(path):
        raise TaskCenterConfigError(f"path not allowed: {path}")
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    request = urllib.request.Request(base_url + path + query, method="GET", headers={
        "Accept": "application/json",
        "Cookie": f"{COOKIE_NAME}={token}",
        "User-Agent": "terminal-mcp-office/1 (read-only)",
    })
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body = response.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise HttpStatusError(exc.code) from None
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("Task Center response too large")
    return json.loads(body.decode("utf-8"))


# -- normalization -------------------------------------------------------------

def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def classify_link(url: str) -> str | None:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path
    if host in ("chatgpt.com", "chat.openai.com") and "/c/" in path:
        return "chat"
    if host == "github.com":
        if re.search(r"/pull/\d+", path):
            return "pr"
        if re.search(r"/issues/\d+", path):
            return "issue"
        if re.search(r"/commit/[0-9a-f]{7,40}", path):
            return "commit"
        if "/actions/runs/" in path or "/checks" in path:
            return "tests"
        return "github"
    return "link"


def extract_links(row: dict[str, Any], evidence: list[Any]) -> list[dict[str, str]]:
    seen: set[str] = set()
    links: list[dict[str, str]] = []
    sources = [row.get("external_url") or "", row.get("details") or "", *_strings(evidence)]
    for text in sources:
        for match in _URL_RE.findall(str(text)):
            url = match.rstrip(".,;:")
            if url in seen:
                continue
            seen.add(url)
            kind = classify_link(url)
            label = url
            found = re.search(r"/(issues|pull)/(\d+)", url)
            if found:
                label = ("#" if found.group(1) == "issues" else "PR #") + found.group(2)
            commit = re.search(r"/commit/([0-9a-f]{7,40})", url)
            if commit:
                label = commit.group(1)[:8]
            if kind == "chat":
                label = "ChatGPT chat"
            links.append({"kind": kind or "link", "url": url, "label": label})
    # Bare SHAs reported in commit/test evidence (no URL).
    for entry in evidence:
        if isinstance(entry, dict) and str(entry.get("kind", "")).lower() in ("commit", "sha", "release"):
            sha = str(entry.get("sha") or entry.get("commit") or "")
            if _SHA_RE.fullmatch(sha) and sha not in seen:
                seen.add(sha)
                links.append({"kind": "commit", "url": "", "label": sha[:8]})
    return links[:16]


def parse_requirement(details: str) -> tuple[str, list[str]]:
    """(requirement text, acceptance criteria) from a task's free-text details."""
    lines = (details or "").splitlines()
    requirement: list[str] = []
    criteria: list[str] = []
    in_ac = False
    for line in lines:
        header = _AC_HEADER_RE.match(line)
        if header:
            in_ac = True
            rest = header.group(2).strip()
            if rest:
                criteria.append(_clip(rest, 240))
            continue
        if in_ac:
            bullet = _BULLET_RE.match(line)
            if bullet:
                criteria.append(_clip(bullet.group(1), 240))
                continue
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                in_ac = False
            else:
                criteria.append(_clip(line, 240))
                continue
        requirement.append(line)
    return _clip("\n".join(requirement), 900), criteria[:20]


def _has_evidence(evidence: list[Any]) -> bool:
    """Something beyond the owner's own state notes."""
    for entry in evidence:
        if isinstance(entry, dict):
            if str(entry.get("kind", "")).lower() not in ("", "user_note"):
                return True
            if any(_URL_RE.search(text) for text in _strings(entry)):
                return True
        elif entry:
            return True
    return False


def _last_value(evidence: list[Any], *keys: str) -> str | None:
    for entry in reversed(evidence):
        if isinstance(entry, dict):
            for key in keys:
                if entry.get(key):
                    return _clip(entry[key], 300)
    return None


def normalize_task(row: dict[str, Any], *, now: float) -> dict[str, Any]:
    raw_evidence = row.get("evidence")
    if raw_evidence is None and isinstance(row.get("evidence_json"), str):
        try:
            raw_evidence = json.loads(row["evidence_json"])
        except ValueError:
            raw_evidence = []
    evidence = raw_evidence if isinstance(raw_evidence, list) else []
    state = str(row.get("state") or "unknown").lower()
    if state not in STATES:
        state = "unknown"
    updated_at = _num(row.get("updated_at"))
    created_at = _num(row.get("created_at"))
    requirement, criteria = parse_requirement(str(row.get("details") or ""))
    links = extract_links(row, evidence)
    flags: list[str] = []
    evidenced = _has_evidence(evidence)
    if state == "verified_done":
        if evidenced:
            flags.append("verified_done")
        else:
            flags += ["needs_review", "done_without_evidence"]
    elif state == "waiting_input":
        flags.append("waiting_input")
    elif state == "blocked":
        flags.append("blocked")
    elif state == "needs_review":
        flags.append("needs_review")
    elif state == "unknown":
        flags.append("unknown")
    else:
        flags.append("active")
    if any(isinstance(e, dict) and str(e.get("kind", "")).lower() == "dispatch_uncertain"
           for e in evidence):
        flags.append("dispatch_uncertain")
    if "verified_done" not in flags and updated_at and now - updated_at > STALE_AFTER_SECONDS:
        flags.append("stale")
    tokens = sorted(set(_TASK_TOKEN_RE.findall(" ".join(
        [str(row.get("external_id") or ""), str(row.get("details") or ""), *_strings(evidence)]))))
    return {
        "task_id": str(row.get("task_id") or ""),
        "title": _clip(row.get("title"), 200),
        "project": _clip(row.get("project"), 120),
        "state": state,
        "state_source": _clip(row.get("state_source"), 40) or "unknown",
        "source": _clip(row.get("source"), 40) or "unknown",
        "external_id": _clip(row.get("external_id"), 200),
        "external_url": str(row.get("external_url") or "")[:500],
        "node": _clip(row.get("node"), 80),
        "requirement": requirement,
        "acceptance_criteria": criteria,
        "links": links,
        "evidence_count": len(evidence),
        "has_evidence": evidenced,
        "next_action": _last_value(evidence, "next_action", "next"),
        "blocker": _last_value(evidence, "blocker", "reason") if state in ("blocked", "waiting_input")
        else _last_value(evidence, "blocker"),
        "last_note": _last_value(evidence, "note"),
        "created_at": created_at,
        "updated_at": updated_at,
        "age_seconds": round(now - updated_at, 1) if updated_at else None,
        "unfinished": "verified_done" not in flags,
        "flags": flags,
        "refs": tokens,
        "linked_sessions": [],
    }


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def finalize(task: dict[str, Any]) -> dict[str, Any]:
    flags = task["flags"]
    task["bucket"] = next((name for name in BUCKET_ORDER if name in flags), "unknown")
    task["flag_labels"] = [FLAG_LABELS.get(flag, flag) for flag in flags]
    return task


def link_sessions(tasks: list[dict[str, Any]], sessions: list[dict[str, Any]]) -> None:
    """Join tasks to live sessions in place. Matching is by evidence only:
    a task id a session reports (task label / supervised task), a dt_/tc_
    reference both sides carry, or an explicit node+session pair in the
    task's evidence. A task is never linked by guesswork on titles."""
    by_ref: dict[str, list[dict[str, Any]]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    by_bare: dict[str, list[dict[str, Any]]] = {}
    for session in sessions:
        refs = set()
        cur = session.get("current_task") or {}
        task = session.get("task") or {}
        for value in (cur.get("task_id"), cur.get("request_key"), task.get("id")):
            if value:
                refs.add(str(value))
        for ref in refs:
            by_ref.setdefault(ref, []).append(session)
        if session.get("qualified"):
            by_name[str(session["qualified"])] = session
        if session.get("node_id") and session.get("session"):
            by_name[f"{session['node_id']}/{session['session']}"] = session
        if session.get("session"):
            by_bare.setdefault(str(session["session"]), []).append(session)
    for task in tasks:
        found: dict[str, dict[str, Any]] = {}
        for ref in [task["task_id"], task["external_id"], *task["refs"]]:
            for session in by_ref.get(ref, []) if ref else []:
                found[session["key"]] = session
        for name in _session_mentions(task):
            if task["node"]:
                session = by_name.get(f"{task['node']}/{name}") or by_name.get(name)
            else:
                # No node claimed: an explicit `session: X` links only when X
                # names exactly one live session across the fleet.
                matches = by_bare.get(name, [])
                session = matches[0] if len(matches) == 1 else None
            if session:
                found[session["key"]] = session
        task["linked_sessions"] = [
            {"key": s.get("key"), "session": s.get("session"), "node_id": s.get("node_id"),
             "state": s.get("state"), "agent": s.get("agent")}
            for s in found.values()]
        if task["unfinished"] and found:
            states = {str(s.get("state") or "") for s in found.values()}
            if states and states <= IDLE_SESSION_STATES:
                task["flags"].append("agent_idle_open")
            if "WAITING" in states and "waiting_input" not in task["flags"]:
                task["flags"].append("waiting_input")
        if task["state"] in ("queued", "running") and task["node"] and not found \
                and "dispatch_uncertain" not in task["flags"] and _session_mentions(task):
            # Claimed onto a node/session that is not live: the dispatch may
            # never have landed. Distinct from waiting/stale on purpose.
            task["flags"].append("dispatch_uncertain")
        finalize(task)


_SESSION_KEY_RE = re.compile(r"\b(?:session|phiên)\s*[:=]\s*`?([A-Za-z0-9._\-/]{2,96})`?", re.IGNORECASE)


def _session_mentions(task: dict[str, Any]) -> list[str]:
    names = _SESSION_KEY_RE.findall(task.get("requirement") or "")
    names += [m for m in _SESSION_KEY_RE.findall(task.get("last_note") or "")]
    return names[:8]


# -- reader --------------------------------------------------------------------

Fetcher = Callable[[str, str, "dict[str, Any] | None", str], Any]


class TaskCenterReader:
    """Cached, GET-only view of the gateway Task Center. Thread-safe."""

    def __init__(self, base_url: str | None = None, session_file: str | None = None, *,
                 fetch: Fetcher | None = None, ttl_seconds: float = DEFAULT_TTL_SECONDS,
                 clock: Callable[[], float] = time.time) -> None:
        self._raw_url = base_url
        self._session_file = session_file
        self._fetch = fetch or http_get_json
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._last_good: dict[str, Any] | None = None

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None, **kwargs: Any) -> "TaskCenterReader":
        env = os.environ if environ is None else environ
        return cls(env.get(URL_ENV) or None, env.get(SESSION_FILE_ENV) or None, **kwargs)

    @property
    def configured(self) -> bool:
        return bool(self._raw_url and self._session_file)

    def snapshot(self, *, force: bool = False) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            if not force and self._cached is not None and now - self._cached_at < self._ttl:
                return {**self._cached, "cached": True}
            payload = self._build(now)
            self._cached, self._cached_at = payload, now
            return payload

    def _base(self, connection: str, detail: str, now: float, host: str | None = None) -> dict[str, Any]:
        return {"connection": connection, "connection_label": CONNECTION_LABELS[connection],
                "detail": detail, "fetched_at": now, "host": host, "read_only": True,
                "sources": {}, "counts": {}, "total": 0, "rows": [], "truncated": False,
                "stale": False, "last_good_at": None}

    def _build(self, now: float) -> dict[str, Any]:
        if not self.configured:
            return self._base("not_configured",
                              f"Đặt {URL_ENV} và {SESSION_FILE_ENV} để đọc Task Center (chỉ đọc).", now)
        try:
            base = validate_base_url(self._raw_url or "")
            host = urllib.parse.urlsplit(base).hostname
            token = read_session_token(self._session_file or "")
        except (TaskCenterConfigError, OSError) as exc:
            detail = str(exc) if isinstance(exc, TaskCenterConfigError) else "session file unreadable"
            return self._with_last_good(self._base("error", detail, now), now)
        try:
            summary = self._fetch(base, API_PREFIX + "/summary", None, token) or {}
            rows: list[dict[str, Any]] = []
            offset: int | None = 0
            pages = 0
            while offset is not None and pages < MAX_PAGES:
                page = self._fetch(base, API_PREFIX + "/tasks",
                                   {"limit": PAGE_LIMIT, "offset": offset}, token) or {}
                rows.extend(r for r in page.get("data") or [] if isinstance(r, dict))
                offset = page.get("next_offset")
                pages += 1
            total = int(summary.get("total") or len(rows))
        except HttpStatusError as exc:
            if exc.status in (401, 403):
                return self._with_last_good(
                    self._base("unauthorized", f"Gateway trả {exc.status}: phiên chưa đăng nhập "
                               "hoặc tài khoản chưa được duyệt.", now, host), now)
            return self._with_last_good(self._base("error", f"Gateway trả HTTP {exc.status}", now, host), now)
        except Exception as exc:  # noqa: BLE001 -- a down gateway never 5xxs the office
            return self._with_last_good(
                self._base("error", f"{type(exc).__name__}: không đọc được Task Center", now, host), now)
        payload = self._base("connected", "Đọc trực tiếp từ AI Gateway Task Center (chỉ đọc).", now, host)
        payload.update({"sources": summary.get("sources") or {}, "counts": summary.get("counts") or {},
                        "total": total, "rows": rows, "truncated": offset is not None or len(rows) < total,
                        "last_good_at": now})
        self._last_good = payload
        return payload

    def _with_last_good(self, payload: dict[str, Any], now: float) -> dict[str, Any]:
        """An outage must not read as "no tasks": keep the last good rows."""
        if self._last_good is not None:
            payload.update({"rows": self._last_good["rows"], "total": self._last_good["total"],
                            "sources": self._last_good["sources"], "counts": self._last_good["counts"],
                            "truncated": self._last_good["truncated"], "stale": True,
                            "last_good_at": self._last_good["last_good_at"]})
        return payload


def build_task_view(snapshot: dict[str, Any], sessions: list[dict[str, Any]], *,
                    now: float | None = None) -> dict[str, Any]:
    """The `task_center` block of /dashboard/api/office: normalized, joined to
    live sessions and counted per flag. Unfinished tasks sort first and are
    never filtered out server-side."""
    now = time.time() if now is None else now
    tasks = [normalize_task(row, now=now) for row in snapshot.get("rows") or []]
    link_sessions(tasks, sessions)
    tasks.sort(key=lambda t: (not t["unfinished"], BUCKET_ORDER.index(t["bucket"]),
                              -(t["updated_at"] or 0)))
    counts: dict[str, int] = {"unfinished": 0}
    for task in tasks:
        counts["unfinished"] += 1 if task["unfinished"] else 0
        for flag in set(task["flags"]):
            counts[flag] = counts.get(flag, 0) + 1
    by_session: dict[str, list[str]] = {}
    for task in tasks:
        for link in task["linked_sessions"]:
            by_session.setdefault(str(link["key"]), []).append(task["task_id"])
    view = {key: value for key, value in snapshot.items() if key != "rows"}
    sources = dict(view.get("sources") or {})
    view.update({"tasks": tasks, "flag_counts": counts, "tasks_by_session": by_session,
                 "flag_labels": FLAG_LABELS,
                 "history_import": {
                     "status": sources.get("chatgpt_history") or "not_connected",
                     "label": "Chưa kết nối" if (sources.get("chatgpt_history") or "not_connected")
                     != "connected" else "Đã kết nối",
                     "detail": "Gateway chưa có endpoint import conversations.json. Xem trước + đối "
                               "chiếu trùng chạy hoàn toàn trong trình duyệt; không tải lên, không ghi.",
                 }})
    return view
