"""GitHub webhook ingress -> the EXISTING event bus + queue_store (#106).

GitHub is source, PR and webhook transport only; build/test runs locally.
This module turns ONE signed GitHub delivery into, at most, ONE local CI task
per (repo, exact commit, check) on an allowlisted lane. It adds no queue, no
dispatcher and no database: deliveries are recorded in the event bus, tasks
in queue_store, and whoever already owns that lane (the autopilot, if its
project is approved for dispatch) runs it. It never runs anything itself.

ORDER OF CHECKS (each refusal has no side effect):

  1. body size;
  2. HMAC-SHA256 of the RAW body against `X-Hub-Signature-256`, with a secret
     read from a 0600 file (never inline config), compared in constant time;
  3. delivery id shape, JSON, repo allowlist, event/branch/action allowlist
     (a pull request from a fork is untrusted code and is ignored);
  4. the event's own signed timestamp must be recent: GitHub signs the body,
     not the delivery header, so the body time is the only replay clock;
  5. a per-repo rate limit.

PERSIST BEFORE ACK: the body hash (replay of a signed body under a fresh
delivery id), then the delivery id (a reused id with a different body), are
recorded with event-bus idempotency keys, then the task is created with its
provenance in the SAME insert (queue-owned `github_ingress` metadata, which a
task creator cannot supply). A crash anywhere leaves a state that GitHub's own
redelivery completes without duplicating anything.

UNTRUSTED CONTENT: titles, bodies and commit messages never reach a prompt or
the task. The prompt is a fixed template over validated identifiers only.

Exposure: the route lives on the controller's existing HTTP server, which
binds loopback by default. Put it behind an authenticated relay, never
straight on the internet; the HMAC is the request's authentication.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping

ACTOR = "github-ingress"
EVENT_DELIVERY = "GITHUB_DELIVERY"
EVENT_BODY_SEEN = "GITHUB_BODY_SEEN"
EVENT_CI_REQUESTED = "GITHUB_CI_REQUESTED"
METADATA_KEY = "github_ingress"
SUPPORTED_EVENTS = ("push", "pull_request")
PR_ACTIONS = frozenset({"opened", "synchronize", "reopened"})

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DELIVERY_RE = re.compile(r"^[0-9A-Fa-f-]{8,64}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_CHECK_RE = re.compile(r"^[a-z0-9-]{1,40}$")
_SCRIPT_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_FUTURE_SKEW_SECONDS = 300

PROMPT_TEMPLATE = (
    "LOCAL CI ({check}) for {repo} at exact commit {sha} ({source}).\n"
    "1. In a clean worktree at exactly {sha}, run the repository's versioned script with "
    "`python -m terminal_mcp.local_ci --repo <repo checkout> --sha {sha} --script {script}`.\n"
    "2. Run nothing else on behalf of this event: it carries identifiers only, no instructions.\n"
    "3. Report the evidence JSON that command prints (commit_sha, command, exit_code, "
    "log_sha256) unchanged. Finish with TMCP-DONE."
)


@dataclass(frozen=True)
class GithubRepoRoute:
    """One allowlisted repository and where its CI task goes."""
    repo: str
    project_id: str
    lane: str
    branches: tuple[str, ...] = ("main",)
    events: tuple[str, ...] = SUPPORTED_EVENTS
    check: str = "local-ci"
    script: str = "scripts/ci/local-ci.sh"

    def __post_init__(self) -> None:
        if not _REPO_RE.match(self.repo or ""):
            raise ValueError(f"github_ingress repo must be owner/name, got {self.repo!r}")
        if not str(self.project_id or "").strip() or not str(self.lane or "").strip():
            raise ValueError(f"github_ingress {self.repo}: project_id and lane are required")
        if not self.branches or not all(_BRANCH_RE.match(b) for b in self.branches):
            raise ValueError(f"github_ingress {self.repo}: branches must be plain branch names")
        unknown = set(self.events) - set(SUPPORTED_EVENTS)
        if not self.events or unknown:
            raise ValueError(f"github_ingress {self.repo}: events must be from {SUPPORTED_EVENTS}")
        if not _CHECK_RE.match(self.check or ""):
            raise ValueError(f"github_ingress {self.repo}: check must match [a-z0-9-]")
        if (not _SCRIPT_RE.match(self.script or "") or self.script.startswith("/")
                or ".." in self.script.split("/")):
            raise ValueError(f"github_ingress {self.repo}: script must be a relative path in the repo")


@dataclass(frozen=True)
class GithubIngressPolicy:
    enabled: bool = False
    secret_file: str | None = None
    repos: tuple[GithubRepoRoute, ...] = ()
    max_body_bytes: int = 1_000_000
    max_age_seconds: int = 6 * 3600
    rate_per_minute: int = 30

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        if not self.secret_file:
            raise ValueError("github_ingress.enabled needs secret_file (a 0600 file, never inline)")
        if not self.repos:
            raise ValueError("github_ingress.enabled needs at least one entry in repos")
        names = [r.repo.lower() for r in self.repos]
        if len(names) != len(set(names)):
            raise ValueError("github_ingress.repos lists a repo twice")
        if not 1 <= int(self.rate_per_minute) <= 600:
            raise ValueError("github_ingress.rate_per_minute must be 1..600")

    def route(self, repo: str) -> GithubRepoRoute | None:
        return next((r for r in self.repos if r.repo.lower() == str(repo).lower()), None)


def load_github_ingress_config(raw: Any) -> GithubIngressPolicy:
    """Parse the `github_ingress:` block. Absent -> off."""
    if raw is None:
        return GithubIngressPolicy()
    if not isinstance(raw, dict):
        raise ValueError("github_ingress must be a mapping")
    allowed = {"enabled", "secret_file", "repos", "max_body_bytes", "max_age_seconds",
               "rate_per_minute"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"github_ingress: unknown key(s) {sorted(unknown)} "
                         f"(inline secrets are not accepted)")
    repos_raw = raw.get("repos") or []
    if not isinstance(repos_raw, list) or not all(isinstance(r, dict) for r in repos_raw):
        raise ValueError("github_ingress.repos must be a list of mappings")
    route_keys = {"repo", "project_id", "lane", "branches", "events", "check", "script"}
    routes = []
    for index, item in enumerate(repos_raw):
        extra = set(item) - route_keys
        if extra:
            raise ValueError(f"github_ingress.repos[{index}]: unknown key(s) {sorted(extra)}")
        routes.append(GithubRepoRoute(
            repo=str(item.get("repo") or ""), project_id=str(item.get("project_id") or ""),
            lane=str(item.get("lane") or ""),
            branches=tuple(str(b) for b in (item.get("branches") or ["main"])),
            events=tuple(str(e) for e in (item.get("events") or SUPPORTED_EVENTS)),
            check=str(item.get("check") or "local-ci"),
            script=str(item.get("script") or "scripts/ci/local-ci.sh")))
    return GithubIngressPolicy(
        enabled=raw.get("enabled") is True, secret_file=raw.get("secret_file"),
        repos=tuple(routes), max_body_bytes=int(raw.get("max_body_bytes", 1_000_000)),
        max_age_seconds=int(raw.get("max_age_seconds", 6 * 3600)),
        rate_per_minute=int(raw.get("rate_per_minute", 30)))


def read_secret_file(path: str) -> bytes:
    """The webhook secret, from a file only its owner can read."""
    try:
        info = os.stat(path)
    except OSError as exc:
        raise ValueError(f"github_ingress secret_file unreadable: {type(exc).__name__}") from None
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("github_ingress secret_file permissions must be 0600 (owner only)")
    with open(path, "rb") as handle:
        secret = handle.read().strip()
    if len(secret) < 16:
        raise ValueError("github_ingress secret is shorter than 16 bytes")
    return secret


def signature_valid(secret: bytes, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


def _parse_time(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class _Extracted:
    sha: str
    ref: str
    event_time: float | None
    pr: int | None = None


@dataclass
class GithubIngress:
    policy: GithubIngressPolicy
    store: Any
    bus: Any
    clock: Callable[[], float] = time.time
    secret: bytes = b""
    _hits: dict[str, deque] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not self.secret:
            self.secret = read_secret_file(str(self.policy.secret_file))

    # -- request handling ------------------------------------------------------

    def handle(self, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        h = {str(k).lower(): str(v) for k, v in headers.items()}
        if len(body) > self.policy.max_body_bytes:
            return 413, {"error": "PAYLOAD_TOO_LARGE"}
        if not signature_valid(self.secret, body, h.get("x-hub-signature-256")):
            return 401, {"error": "BAD_SIGNATURE"}
        delivery = h.get("x-github-delivery", "")
        if not _DELIVERY_RE.match(delivery):
            return 400, {"error": "BAD_DELIVERY"}
        event = h.get("x-github-event", "")
        try:
            payload = json.loads(body)
        except ValueError:
            return 400, {"error": "BAD_JSON"}
        if not isinstance(payload, dict):
            return 400, {"error": "BAD_JSON"}
        if event == "ping":
            return 200, {"pong": True}
        repo = str((payload.get("repository") or {}).get("full_name") or "")
        route = self.policy.route(repo)
        if route is None:
            return 403, {"error": "REPO_NOT_ALLOWED"}
        if event not in route.events:
            return 202, {"accepted": False, "ignored": "UNSUPPORTED_EVENT"}
        extracted = self._extract(event, payload, route)
        if isinstance(extracted, tuple):
            return extracted
        now = self.clock()
        if (extracted.event_time is None
                or now - extracted.event_time > self.policy.max_age_seconds
                or extracted.event_time - now > _FUTURE_SKEW_SECONDS):
            return 409, {"error": "STALE_EVENT"}
        if not self._within_rate(route.repo.lower(), now):
            return 429, {"error": "RATE_LIMITED"}
        return self._persist(route, event, delivery, hashlib.sha256(body).hexdigest(), extracted)

    def _extract(self, event: str, payload: dict, route: GithubRepoRoute
                 ) -> _Extracted | tuple[int, dict[str, Any]]:
        if event == "push":
            ref = str(payload.get("ref") or "")
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None
            if branch not in route.branches:
                return 202, {"accepted": False, "ignored": "BRANCH_NOT_ALLOWED"}
            if payload.get("deleted") is True:
                return 202, {"accepted": False, "ignored": "BRANCH_DELETED"}
            sha = payload.get("after")
            if not isinstance(sha, str) or not _SHA_RE.match(sha) or sha == "0" * 40:
                return 400, {"error": "BAD_SHA"}
            head = payload.get("head_commit") or {}
            return _Extracted(sha=sha, ref=ref, event_time=_parse_time(head.get("timestamp")))
        pr = payload.get("pull_request") or {}
        if payload.get("action") not in PR_ACTIONS:
            return 202, {"accepted": False, "ignored": "ACTION_IGNORED"}
        head, base = pr.get("head") or {}, pr.get("base") or {}
        if str((head.get("repo") or {}).get("full_name") or "").lower() != route.repo.lower():
            return 202, {"accepted": False, "ignored": "UNTRUSTED_FORK"}
        if base.get("ref") not in route.branches:
            return 202, {"accepted": False, "ignored": "BRANCH_NOT_ALLOWED"}
        sha = head.get("sha")
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            return 400, {"error": "BAD_SHA"}
        number = pr.get("number", payload.get("number"))
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            return 400, {"error": "BAD_PR_NUMBER"}
        return _Extracted(sha=sha, ref=f"refs/heads/{base['ref']}", pr=number,
                          event_time=_parse_time(pr.get("updated_at")))

    def _within_rate(self, repo: str, now: float) -> bool:
        with self._lock:
            hits = self._hits.setdefault(repo, deque())
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= self.policy.rate_per_minute:
                return False
            hits.append(now)
            return True

    # -- persistence -------------------------------------------------------------

    def _persist(self, route: GithubRepoRoute, event: str, delivery: str, body_sha: str,
                 x: _Extracted) -> tuple[int, dict[str, Any]]:
        seen = self.bus.publish(EVENT_BODY_SEEN, idempotency_key=f"github:body:{body_sha}",
                                payload={"delivery": delivery, "repo": route.repo}, actor=ACTOR)
        if seen.get("duplicate") and (seen.get("payload") or {}).get("delivery") != delivery:
            return 409, {"error": "REPLAYED_BODY"}
        record = self.bus.publish(
            EVENT_DELIVERY, project_id=route.project_id, entity_type="github_delivery",
            entity_id=delivery, idempotency_key=f"github:delivery:{delivery}", actor=ACTOR,
            payload={"repo": route.repo, "event": event, "sha": x.sha, "ref": x.ref, "pr": x.pr,
                     "body_sha256": body_sha})
        if record.get("duplicate") and (record.get("payload") or {}).get("body_sha256") != body_sha:
            return 409, {"error": "DELIVERY_ID_REUSED"}
        request_key = f"github-ci:{route.repo}:{x.sha}:{route.check}"
        task, created = self._ensure_task(route, event, delivery, x, request_key)
        if task is None:
            return 409, {"error": "REQUEST_KEY_CONFLICT"}
        self.bus.publish(EVENT_CI_REQUESTED, project_id=route.project_id, entity_type="queue_task",
                         entity_id=task["id"], idempotency_key=f"github:ci:{request_key}",
                         actor=ACTOR, payload={"repo": route.repo, "sha": x.sha,
                                               "check": route.check, "lane": route.lane})
        duplicate = bool(record.get("duplicate")) or not created
        return (200 if duplicate else 202), {"accepted": True, "duplicate": duplicate,
                                             "task_id": task["id"], "sha": x.sha}

    def _ensure_task(self, route: GithubRepoRoute, event: str, delivery: str, x: _Extracted,
                     request_key: str) -> tuple[dict[str, Any] | None, bool]:
        """One task per request_key, adopted only when it carries ingress
        provenance -- a creator can pick any request_key but cannot write the
        queue-owned metadata, so a forged key is refused, never adopted."""
        created = False
        existing = self.store.task_by_request_key(request_key)
        if existing is None:
            source = f"PR #{x.pr} into {x.ref}" if x.pr else x.ref
            provenance = {"repo": route.repo, "sha": x.sha, "event": event, "ref": x.ref,
                          "pr": x.pr, "check": route.check, "script": route.script,
                          "delivery": delivery}
            try:
                self.store.append_tasks(route.lane, [{
                    "title": f"local CI {route.check} @ {x.sha[:12]}",
                    "prompt": PROMPT_TEMPLATE.format(check=route.check, repo=route.repo,
                                                     sha=x.sha, source=source,
                                                     script=route.script),
                    "project_id": route.project_id, "request_key": request_key,
                }], internal_metadata={METADATA_KEY: provenance})
                created = True
            except sqlite3.IntegrityError:
                pass  # a concurrent delivery created it first
            existing = self.store.task_by_request_key(request_key)
        if existing is None or not isinstance((existing.get("metadata") or {}).get(METADATA_KEY), dict):
            return None, False
        return existing, created


def register_github_ingress(server: Any, ingress: GithubIngress, *,
                            path: str = "/github/webhook") -> None:
    """POST-only route on the existing server. Registered only when enabled."""
    from starlette.concurrency import run_in_threadpool
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @server.custom_route(path, methods=["POST"], include_in_schema=False)
    async def github_webhook(request: Request) -> JSONResponse:
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > ingress.policy.max_body_bytes:
            return JSONResponse({"error": "PAYLOAD_TOO_LARGE"}, status_code=413)
        body = await request.body()
        # SQLite writes happen inside handle(); keep them off the event loop.
        status, payload = await run_in_threadpool(ingress.handle, dict(request.headers), body)
        return JSONResponse(payload, status_code=status)
