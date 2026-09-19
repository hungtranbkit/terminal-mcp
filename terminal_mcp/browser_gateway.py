"""The browser gateway service: node selection, bounded jobs, compact results.

SMALL ON PURPOSE. The whole point of this feature is that ChatGPT keeps
exactly ONE control plane (Terminal MCP) and gains a browser through a
handful of declarative verbs -- not through a raw Python/shell escape
hatch and not through dozens of low-level CDP tools. This service backs
four tools and nothing else:

    browser_status      what can this fleet do, and how did job X end
    browser_verify      run one declarative plan, compactly
    browser_screenshot  capture one page at one viewport
    browser_stop        release the managed browser

THE LATENCY CONTRACT. A verification can legitimately take minutes; a
synchronous tool call may not. Every call returns within a bounded sync
window (<=45s, 30s by default). A plan still running when that window
closes does not fail and is not abandoned -- it becomes PENDING with a
resume handle, and `browser_status(job_id=...)` returns the finished
result later. That is also why the sync budget and the plan timeout are
two different numbers.

AFFINITY, NOT SCATTER. A caller that names a node, or names a session,
gets THAT node -- a verification of a dev server on one box is worthless
if it silently runs on another. Auto-selection happens only when the
caller asked for neither, which is the one case where "any eligible node"
is what they meant.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from .browser_plan import (
    DEFAULT_SYNC_WAIT_SECONDS,
    MAX_SYNC_WAIT_SECONDS,
    BrowserGatewayError,
    Plan,
    UrlPolicy,
    validate_plan,
    validate_screenshot_name,
    validate_url,
    _validate_viewport,
)
from .browser_runner import LocalBrowserRunner, recording_enabled
from .redaction import redact_text

#: The capability name a node advertises when it can run browser plans.
BROWSER_CAPABILITY = "browser-harness"

#: Compact-result caps. A chat client pays for every token of this.
MAX_CHECKS_RETURNED = 20
MAX_ERRORS_RETURNED = 5
MAX_DETAIL_CHARS = 160
MAX_ERROR_CHARS = 240

#: Finished jobs kept for later `browser_status(job_id=...)` reads.
MAX_RETAINED_JOBS = 50

TERMINAL_STATUSES = frozenset({"PASS", "FAIL", "ERROR"})


def _clip(text: Any, limit: int) -> str:
    """Redact, collapse and bound one free-text field.

    Everything that leaves this service passes through here: a harness
    error can quote a page, a URL or an environment value, and none of
    that should reach a transcript unredacted or unbounded.
    """
    value = " ".join(str(text or "").split())
    try:
        value = redact_text(value)
    except Exception:  # noqa: BLE001 -- redaction must never break a result
        pass
    return value if len(value) <= limit else value[: limit - 1] + "…"


class BrowserGateway:
    """One gateway per process. Owns the local runner and the job table."""

    def __init__(
        self,
        *,
        runner: Any | None = None,
        nodes_provider: Callable[[], list[dict[str, Any]]] | None = None,
        session_node_resolver: Callable[[str], str | None] | None = None,
        local_node_id: str = "local",
        url_policy: UrlPolicy | None = None,
        sync_wait_seconds: float = DEFAULT_SYNC_WAIT_SECONDS,
    ) -> None:
        self.runner = runner or LocalBrowserRunner()
        self._nodes_provider = nodes_provider
        self._session_node_resolver = session_node_resolver
        self.local_node_id = local_node_id
        self.url_policy = url_policy or UrlPolicy.from_env()
        self.sync_wait_seconds = min(float(sync_wait_seconds), MAX_SYNC_WAIT_SECONDS)
        self._jobs: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._lock = threading.Lock()

    # -- nodes -----------------------------------------------------------
    def _nodes(self) -> list[dict[str, Any]]:
        if self._nodes_provider is None:
            return []
        try:
            return list(self._nodes_provider() or [])
        except Exception:  # noqa: BLE001 -- a fleet read must not break the browser surface
            return []

    def _node_capable(self, node: dict[str, Any]) -> bool:
        caps = node.get("capabilities") or ()
        return BROWSER_CAPABILITY in set(caps)

    def _is_local(self, node_id: str) -> bool:
        return node_id in {self.local_node_id, "local"}

    def select_node(self, *, node: str | None = None, session: str | None = None) -> str:
        """Resolve which node runs this plan, or raise a typed error.

        Affinity first (explicit node, then the session's node), auto last.
        """
        if node:
            return self._resolve_explicit(node)
        if session and self._session_node_resolver is not None:
            try:
                bound = self._session_node_resolver(session)
            except Exception:  # noqa: BLE001
                bound = None
            if bound:
                return self._resolve_explicit(bound)
        # Auto: local first -- it is the only node with an execution path
        # today, and preferring it avoids a pointless remote hop.
        if self.runner.runtime().available:
            return self.local_node_id
        eligible = [n for n in self._nodes()
                    if self._node_capable(n) and not self._is_local(str(n.get("id", "")))]
        if eligible:
            return str(eligible[0].get("id"))
        raise BrowserGatewayError(
            "BROWSER_UNAVAILABLE",
            "no node in this fleet advertises the browser capability; run "
            "scripts/provision-browser-harness.sh on a node",
            reason="no_eligible_node", capability=BROWSER_CAPABILITY,
        )

    def _resolve_explicit(self, node_id: str) -> str:
        if self._is_local(node_id):
            return self.local_node_id
        known = {str(n.get("id")): n for n in self._nodes()}
        node = known.get(node_id)
        if node is None:
            raise BrowserGatewayError(
                "BROWSER_UNKNOWN_NODE", f"node {node_id!r} is not registered in this fleet",
                node=node_id, known_nodes=sorted(known)[:20],
            )
        # ONE outward code for "you named a node and it cannot run this",
        # with `reason` carrying which of the two it is. A caller needs one
        # branch, not a growing family of near-synonyms.
        if not self._node_capable(node):
            raise BrowserGatewayError(
                "BROWSER_UNAVAILABLE",
                f"node {node_id!r} does not advertise the {BROWSER_CAPABILITY} capability",
                reason="node_capability_missing", node=node_id, capability=BROWSER_CAPABILITY,
            )
        # Phase 1 boundary, stated as a typed error rather than a silent
        # fallback to the wrong host: the node agent has no browser
        # endpoint yet, so a capable REMOTE node is still not executable.
        # Deliberately NOT a fleet/RPC redesign -- see docs/browser-gateway.md.
        raise BrowserGatewayError(
            "BROWSER_UNAVAILABLE",
            f"node {node_id!r} is browser-capable but remote browser execution is not "
            "available in Phase 1; run the plan on the local node",
            reason="remote_execution_unsupported", node=node_id, phase="1",
        )

    # -- artifacts -------------------------------------------------------
    def artifact_path(self, name: str = "") -> Path:
        """Build a screenshot path INSIDE the gateway's artifact directory.

        The caller's `name` was already pattern-checked; this is the second,
        independent guard -- the resolved path must still be contained, so
        even a bug in the first check cannot write outside the directory.
        """
        name = validate_screenshot_name(name)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        filename = f"{name or 'verify'}-{stamp}-{uuid.uuid4().hex[:8]}.png"
        base = Path(self.runner.artifact_dir).expanduser()
        candidate = (base / filename).resolve()
        if base.resolve() not in candidate.parents:
            raise BrowserGatewayError(
                "BROWSER_ARTIFACT_PATH_REJECTED",
                "refusing to write a screenshot outside the artifact directory",
                artifact_dir=str(base),
            )
        return candidate

    # -- status ----------------------------------------------------------
    def status(self, job_id: str | None = None) -> dict[str, Any]:
        if job_id:
            return self._job_status(job_id)
        runtime = self.runner.runtime()
        nodes = [
            {"id": str(n.get("id")), "name": n.get("display_name") or n.get("hostname") or "",
             "browser_capable": self._node_capable(n), "status": n.get("status")}
            for n in self._nodes()
        ]
        with self._lock:
            active = sum(1 for job in self._jobs.values() if job["status"] == "RUNNING")
            recent = [self._compact_job(job, include_checks=False)
                      for job in list(self._jobs.values())[-5:]]
        return {
            "status": "READY" if runtime.available else "DEGRADED",
            "node": self.local_node_id,
            "runtime": runtime.to_dict(),
            "browser": {
                "managed": True, "cdp_url": self.runner.cdp_url,
                "alive": self.runner.browser_alive(),
                "recording": "on" if recording_enabled() else "off",
            },
            "artifact_dir": str(self.runner.artifact_dir),
            "nodes": nodes,
            "active_jobs": active,
            "recent_jobs": recent,
            "remote_execution": "unsupported_phase_1",
        }

    def _job_status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return BrowserGatewayError(
                    "BROWSER_UNKNOWN_JOB", f"no browser job {job_id!r} is known",
                    job_id=job_id,
                ).to_dict()
            return self._compact_job(job)

    # -- verify ----------------------------------------------------------
    def verify(self, payload: dict[str, Any], *, wait_seconds: float | None = None) -> dict[str, Any]:
        try:
            plan = validate_plan(payload, policy=self.url_policy)
            node = self.select_node(node=plan.node, session=plan.session)
        except BrowserGatewayError as exc:
            return exc.to_dict()
        return self._dispatch(plan.to_payload(), plan=plan, node=node, wait_seconds=wait_seconds)

    def screenshot(self, url: str, *, name: str = "", viewport: Any = None,
                   node: str | None = None, session: str | None = None,
                   wait_seconds: float | None = None) -> dict[str, Any]:
        """Capture one page. Deliberately not expressible as a `verify`
        plan: a plan must assert something, and a screenshot asserts
        nothing -- it is evidence, not a check."""
        try:
            url = validate_url(url, self.url_policy)
            width, height = _validate_viewport(viewport)
            name = validate_screenshot_name(name)
            target = self.select_node(node=node, session=session)
        except BrowserGatewayError as exc:
            return exc.to_dict()
        payload = {
            "url": url, "steps": [], "viewport": {"width": width, "height": height},
            "timeout_seconds": 60.0, "screenshot": "always",
        }
        return self._dispatch(payload, plan=None, node=target, wait_seconds=wait_seconds,
                              screenshot_name=name, kind="screenshot")

    def stop(self) -> dict[str, Any]:
        """Release the managed browser. Running jobs are reported, not
        silently orphaned."""
        with self._lock:
            running = [job_id for job_id, job in self._jobs.items() if job["status"] == "RUNNING"]
        result = self.runner.stop_browser()
        return {"status": "STOPPED" if not result.get("alive") else "ALIVE",
                "pid": result.get("pid"), "running_jobs": running}

    # -- dispatch --------------------------------------------------------
    def _dispatch(self, payload: dict[str, Any], *, plan: Plan | None, node: str,
                  wait_seconds: float | None, screenshot_name: str = "",
                  kind: str = "verify") -> dict[str, Any]:
        try:
            if payload.get("screenshot") != "never":
                payload["screenshot_path"] = str(
                    self.artifact_path(screenshot_name or (plan.screenshot_name if plan else "")))
        except BrowserGatewayError as exc:
            return exc.to_dict()

        timeout = float(payload.get("timeout_seconds") or 45.0)
        budget = min(float(wait_seconds if wait_seconds is not None else self.sync_wait_seconds),
                     MAX_SYNC_WAIT_SECONDS)
        job_id = uuid.uuid4().hex
        job: dict[str, Any] = {
            "id": job_id, "kind": kind, "node": node, "status": "RUNNING",
            "url": payload.get("url", ""), "session": plan.session if plan else None,
            "started_at": time.time(), "result": None,
            "mutations": list(plan.mutating) if plan else [],
        }
        with self._lock:
            self._jobs[job_id] = job
            while len(self._jobs) > MAX_RETAINED_JOBS:
                self._jobs.popitem(last=False)

        done = threading.Event()

        def _run() -> None:
            try:
                result = self.runner.execute(payload, timeout_seconds=timeout)
            except Exception as exc:  # noqa: BLE001 -- a worker thread may not raise into nothing
                result = {"status": "ERROR", "checks": [], "errors": [str(exc)],
                          "artifact": "", "elapsed_ms": 0}
            with self._lock:
                job["result"] = result
                job["status"] = result.get("status", "ERROR")
                job["finished_at"] = time.time()
            done.set()

        worker = threading.Thread(target=_run, name=f"browser-{job_id[:8]}", daemon=True)
        worker.start()
        done.wait(timeout=budget)
        with self._lock:
            return self._compact_job(job, budget=budget)

    # -- compaction ------------------------------------------------------
    def _compact_job(self, job: dict[str, Any], *, include_checks: bool = True,
                     budget: float | None = None) -> dict[str, Any]:
        """The ONE result shape every browser tool returns."""
        result = job.get("result") or {}
        status = job.get("status", "RUNNING")
        if status == "RUNNING":
            status = "PENDING"
        elif status == "TIMEOUT":
            status = "FAIL"

        checks = list(result.get("checks") or [])
        passed = sum(1 for c in checks if c.get("ok"))
        out: dict[str, Any] = {
            "status": status,
            "job_id": job["id"],
            "node": job["node"],
            "url": job.get("url", ""),
            "summary": f"{passed}/{len(checks)} checks passed" if checks else
                       ("screenshot captured" if job["kind"] == "screenshot" and status == "PASS"
                        else f"{status.lower()}"),
            "elapsed_ms": int(result.get("elapsed_ms") or
                              ((time.time() - job["started_at"]) * 1000)),
        }
        if job.get("session"):
            out["session"] = job["session"]
        if job.get("mutations"):
            out["mutations"] = job["mutations"]
        if include_checks and checks:
            shown = checks[:MAX_CHECKS_RETURNED]
            out["checks"] = [
                {"i": c.get("i"), "op": c.get("op"), "ok": bool(c.get("ok")),
                 **({"detail": _clip(c.get("detail"), MAX_DETAIL_CHARS)} if c.get("detail") else {})}
                for c in shown
            ]
            if len(checks) > MAX_CHECKS_RETURNED:
                out["checks_truncated"] = len(checks) - MAX_CHECKS_RETURNED
        errors = [_clip(e, MAX_ERROR_CHARS) for e in (result.get("errors") or [])]
        if errors:
            out["errors"] = errors[:MAX_ERRORS_RETURNED]
            if len(errors) > MAX_ERRORS_RETURNED:
                out["errors_truncated"] = len(errors) - MAX_ERRORS_RETURNED
        if result.get("artifact"):
            out["artifact"] = result["artifact"]
        if result.get("page"):
            out["page"] = result["page"]
        if result.get("degraded"):
            out["degraded"] = True
        if status == "PENDING":
            out["resume"] = {
                "job_id": job["id"],
                "tool": "terminal_browser_status",
                "hint": (f"still running after {budget:.0f}s; call "
                         f"terminal_browser_status(job_id=...) for the result"
                         if budget else "call terminal_browser_status(job_id=...) for the result"),
            }
        return out
