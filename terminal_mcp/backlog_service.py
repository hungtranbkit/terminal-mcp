"""Project Backlog -- the operation layer (planning), sitting ABOVE the
Task Queue (execution).

The distinction this feature exists to enforce (task item 4):
  BACKLOG = what the project INTENDS to do. Durable, project-scoped,
            shared by every session on that repo, and safe to write down
            long before anything can run it.
  QUEUE   = what is EXECUTING now (queue_store.queue_tasks). Runtime,
            lane/session-scoped, dispatched, claimed, verified.

A backlog item is NOT a queue task and never becomes one: `dispatch`
CREATES a queue task through the existing canonical
QueueService.create_task and records the link on both sides
(item.queue_task_id, and queue metadata.backlog_id). That is what makes
backlog_id -> queue task_id -> session -> commit/test traceable, without
this feature growing a third task engine (explicitly forbidden by the
audit that preceded it -- queue_store is the one canonical task table,
and planner/PM/incident/release all layer on it via metadata rather than
adding tables of their own).

Two hard gates:
  - PATH SECURITY: every path goes through lifecycle.resolve_cwd, the
    SAME allowed_cwd_roots + symlink-resolution gate session creation
    uses. A backlog can only ever be read/written inside a configured
    project root -- no traversal, no arbitrary filesystem write.
  - VERIFIED DONE: `complete` refuses to mark DONE on an agent's say-so.
    It requires either real evidence (a commit/test/deploy reference) or
    a linked queue task that reached COMPLETED, which queue_store's own
    comment defines as the spec's VERIFIED_DONE.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from . import backlog_store as store
from .backlog_db import BacklogDB
from .backlog_store import (
    OPEN_STATUSES, PRIORITIES, STATUS_BACKLOG, STATUS_BLOCKED, STATUS_DONE,
    STATUS_IN_PROGRESS, STATUS_NEEDS_REVIEW, STATUS_READY, STATUSES, TYPES,
    BacklogError, backlog_path, new_item_id, now_iso,
)
from .lifecycle import resolve_cwd
from .project_identity import ProjectIdentity, resolve_project

# Fields a caller may set directly. `id`/`created_at`/`history` are
# server-owned; `queue_task_id` is set by dispatch, never by hand.
_WRITABLE = frozenset({
    "title", "description", "status", "priority", "type", "order", "source",
    "dependencies", "acceptance_criteria", "tags", "assignee", "session",
    "node_id", "branch", "worktree", "blocked_reason", "evidence",
})
_MAX_TITLE = 300
_MAX_ITEMS = 2000


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _project_lock(project_id: str) -> threading.Lock:
    """One lock per project. The controller is the single writer now, so
    the contention this guards is between its OWN concurrent requests --
    the cross-agent race is still handled by expected_revision."""
    with _LOCKS_GUARD:
        lock = _LOCKS.get(project_id)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[project_id] = lock
        return lock


class BacklogService:
    def __init__(self, config: Any, *, audit: Any = None, queue: Any = None,
                 controller: Any = None, db: BacklogDB | None = None) -> None:
        self.config = config
        self.audit = audit
        self.queue = queue
        # `controller` lets a project be resolved for a session on ANY
        # node (from that node's own registry record) -- without it this
        # service can still resolve a project from a LOCAL path, which is
        # all a single-node deployment needs.
        self.controller = controller
        self.db = db or BacklogDB()

    # ---------------------------------------------------------------- paths
    def _resolve(self, path: str | None) -> tuple[ProjectIdentity | None, dict[str, Any] | None]:
        """LOCAL path -> canonical identity. Still gated by
        lifecycle.resolve_cwd (the same allowed_cwd_roots + symlink check
        session creation uses), and the DISCOVERED repo root is
        re-checked, since walking up out of an allowed subdirectory would
        otherwise escape."""
        resolved, error = resolve_cwd(path, self.config)
        if error is not None:
            return None, {"error": "PATH_NOT_ALLOWED", "detail": error, "path": path}
        identity = resolve_project(str(resolved))
        if identity is None:
            return None, {"error": "NOT_A_PROJECT", "path": str(resolved),
                          "detail": "not inside a git repository -- a backlog belongs to a project, "
                                    "so this is refused rather than creating one in an arbitrary directory"}
        root_ok, root_error = resolve_cwd(identity.repo_root, self.config)
        if root_error is not None:
            return None, {"error": "PATH_NOT_ALLOWED", "detail": root_error, "path": identity.repo_root}
        return identity, None

    def _project(self, *, project_id: str | None = None, path: str | None = None,
                 node_id: str | None = None, session: str | None = None) -> tuple[dict | None, dict | None]:
        """Resolve WHICH project a call is about, in precedence order:

          1. `project_id` -- already canonical, used as-is. This is how a
             caller addresses a project whose checkout is on some OTHER
             machine (or on no machine this controller can see).
          2. `node_id` + `session` -- ask the OWNING node's registry. This
             is the path that makes a remote session's backlog reachable:
             the controller cannot stat a path on another machine, but it
             can read what that node reported about the session.
          3. `path` -- a local checkout, gated by allowed_cwd_roots.
          4. Nothing -- the server's own default root, same as before.

        Deliberately NOT "guess from the first project we know about": an
        ambiguous call must fail loudly rather than edit the wrong
        project's plan."""
        if project_id:
            known = self.db.project(project_id)
            identity = {"project_id": project_id,
                        "name": (known or {}).get("name") or project_id.rsplit("/", 1)[-1],
                        "source": (known or {}).get("source") or "explicit",
                        "git_remote": (known or {}).get("git_remote"),
                        "is_portable": bool((known or {}).get("is_portable", 1)),
                        "repo_root": None}
            return identity, None
        if node_id and session:
            if self.controller is None:
                return None, {"error": "NO_CONTROLLER",
                              "detail": "this service was built without a controller, so a project can "
                                        "only be resolved from a local path"}
            resolved = self.controller.resolve_project_for_session(node_id, session)
            if "error" in resolved:
                return None, resolved
            return resolved, None
        identity, error = self._resolve(path)
        if error is not None:
            return None, error
        return identity.to_dict(), None

    def _audit(self, action: str, *, result: str, reason: str | None = None) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(action=action, session=None, result=result, reason=reason,
                              source_transport="mcp")
        except Exception:  # noqa: BLE001 - auditing must never break the operation
            pass

    # ---------------------------------------------------------------- read
    def get(self, path: str | None = None, *, project_id: str | None = None,
            node_id: str | None = None, session: str | None = None,
            status: str | list[str] | None = None, priority: str | list[str] | None = None,
            type: str | None = None, tag: str | None = None, assignee: str | None = None,
            include_terminal: bool = True, limit: int = 500) -> dict[str, Any]:
        identity, error = self._project(project_id=project_id, path=path,
                                        node_id=node_id, session=session)
        if error is not None:
            return error
        pid = identity["project_id"]
        document = self.db.document(pid)

        wanted_status = _as_set(status)
        wanted_priority = _as_set(priority)
        items = []
        for item in document["items"]:
            if wanted_status and item["status"] not in wanted_status:
                continue
            if not include_terminal and item["status"] not in OPEN_STATUSES:
                continue
            if wanted_priority and item["priority"] not in wanted_priority:
                continue
            if type and item["type"] != type:
                continue
            if tag and tag not in (item.get("tags") or []):
                continue
            if assignee and item.get("assignee") != assignee:
                continue
            items.append(item)
        items.sort(key=lambda i: (PRIORITIES.index(i["priority"]) if i["priority"] in PRIORITIES else 9,
                                  i.get("order", 0), i.get("created_at", "")))
        counts: dict[str, int] = {st: 0 for st in STATUSES}
        for item in document["items"]:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return {
            "project": identity,
            "revision": document["revision"],
            "schema_version": document["schema_version"],
            "counts": counts,
            "open_total": sum(counts.get(st, 0) for st in OPEN_STATUSES),
            "total": len(document["items"]),
            "returned": len(items[:limit]),
            "items": items[:limit],
            "exists": bool(self.db.project(pid)),
            "repairs": [],
        }

    def list_projects(self) -> dict[str, Any]:
        """Every project this controller holds a backlog for, plus (when a
        controller is wired) every git project the FLEET is working on --
        so a project with sessions but no backlog yet is still visible
        rather than invisible until someone remembers it."""
        stored = {p["project_id"]: p for p in self.db.list_projects()}
        discovered: dict[str, Any] = {}
        node_errors: dict[str, str] = {}
        if self.controller is not None:
            found = self.controller.discover_projects()
            node_errors = found.get("node_errors", {})
            for entry in found.get("projects", []):
                discovered[entry["project_id"]] = entry
        rows = []
        for pid in sorted(set(stored) | set(discovered)):
            store_row = stored.get(pid) or {}
            live = discovered.get(pid) or {}
            rows.append({
                "project_id": pid,
                "name": store_row.get("name") or live.get("name"),
                "git_remote": store_row.get("git_remote") or live.get("git_remote"),
                "is_portable": bool(store_row.get("is_portable", live.get("is_portable", True))),
                "has_backlog": pid in stored,
                "total": store_row.get("total", 0),
                "open_total": store_row.get("open_total", 0),
                "revision": store_row.get("revision", 0),
                "nodes": live.get("nodes", []),
                "checkouts": live.get("checkouts", []),
                "session_count": live.get("session_count", 0),
            })
        rows.sort(key=lambda r: (-r["open_total"], -r["session_count"], r["project_id"]))
        return {"projects": rows, "node_errors": node_errors}

    def validate(self, path: str | None = None, *, project_id: str | None = None,
                 node_id: str | None = None, session: str | None = None) -> dict[str, Any]:
        """Re-normalise a project's stored items. With the controller DB as
        the source of truth there is no hand-edited file to repair on the
        hot path -- this now guards against a payload written by an older
        schema, and stays as the explicit "check my backlog" call."""
        identity, error = self._project(project_id=project_id, path=path,
                                        node_id=node_id, session=session)
        if error is not None:
            return error
        pid = identity["project_id"]
        repairs: list[str] = []
        items = [store.normalise_item(i, repairs=repairs) for i in self.db.items(pid)]
        items = [i for i in items if i]
        written = False
        if repairs:
            self.db.replace_items(pid, items)
            written = True
        self._audit("backlog_validate", result="ok" if not repairs else "repaired")
        return {"project": identity, "valid": True, "repairs": repairs, "written": written,
                "revision": self.db.revision(pid), "total": len(items)}

    # --------------------------------------------------------------- write
    def _mutate(self, path: str | None, expected_revision: int | None,
                mutator: Any, *, action: str, project_id: str | None = None,
                node_id: str | None = None, session: str | None = None) -> dict[str, Any]:
        """One serialized read-modify-write for every mutating operation.

        The lock is now a PROCESS-WIDE lock per project rather than an
        fcntl lock on a repo file: the controller is the single writer,
        so contention is between its own threads/requests, and SQLite's
        own transaction makes the swap atomic. `expected_revision` still
        gives cross-AGENT optimistic concurrency -- two ChatGPT sessions
        editing the same project still conflict safely."""
        identity, error = self._project(project_id=project_id, path=path,
                                        node_id=node_id, session=session)
        if error is not None:
            return error
        pid = identity["project_id"]
        lock = _project_lock(pid)
        with lock:
            self.db.ensure_project(identity)
            current = self.db.revision(pid)
            if expected_revision is not None and int(expected_revision) != current:
                self._audit(action, result="conflict")
                return {"error": "REVISION_CONFLICT", "expected_revision": int(expected_revision),
                        "actual_revision": current, "project_id": pid,
                        "detail": "another agent wrote this backlog first -- re-read it and retry "
                                  "so their change is not clobbered"}
            document = {"items": self.db.items(pid), "project": identity}
            outcome = mutator(document)
            if isinstance(outcome, dict) and "error" in outcome:
                self._audit(action, result="refused", reason=outcome["error"])
                return outcome
            if len(document["items"]) > _MAX_ITEMS:
                return {"error": "BACKLOG_TOO_LARGE", "limit": _MAX_ITEMS}
            revision = self.db.replace_items(pid, document["items"])
        self._audit(action, result="ok")
        result = {"project": identity, "revision": revision, "repairs": []}
        if isinstance(outcome, dict):
            result.update(outcome)
        return result

    def add(self, path: str | None = None, *, tasks: list[dict[str, Any]],
            expected_revision: int | None = None, source: str = "mcp",
            project_id: str | None = None, project_node_id: str | None = None,
            project_session: str | None = None) -> dict[str, Any]:
        if not isinstance(tasks, list) or not tasks:
            return {"error": "INVALID_REQUEST", "detail": "tasks must be a non-empty list"}
        for entry in tasks:
            if not isinstance(entry, dict) or not str(entry.get("title", "")).strip():
                return {"error": "INVALID_REQUEST", "detail": "every task needs a non-empty title"}
            if len(str(entry["title"])) > _MAX_TITLE:
                return {"error": "INVALID_REQUEST", "detail": f"title exceeds {_MAX_TITLE} chars"}
            if entry.get("status") and entry["status"] not in STATUSES:
                return {"error": "INVALID_STATUS", "status": entry["status"], "allowed": list(STATUSES)}
            if entry.get("priority") and entry["priority"] not in PRIORITIES:
                return {"error": "INVALID_PRIORITY", "priority": entry["priority"], "allowed": list(PRIORITIES)}

        def _apply(document: dict[str, Any]) -> dict[str, Any]:
            created = []
            base = max([i.get("order", 0) for i in document["items"]] or [0])
            for offset, entry in enumerate(tasks, start=1):
                item = store.normalise_item({
                    **{k: v for k, v in entry.items() if k in _WRITABLE},
                    "id": new_item_id(), "created_at": now_iso(), "updated_at": now_iso(),
                    "source": entry.get("source") or source,
                    "order": entry.get("order", base + offset),
                })
                item["history"] = [{"at": now_iso(), "event": "created", "by": item["source"]}]
                document["items"].append(item)
                created.append(item)
            return {"created": created, "created_ids": [i["id"] for i in created]}

        return self._mutate(path, expected_revision, _apply, action="backlog_add",
                            project_id=project_id, node_id=project_node_id, session=project_session)

    def update(self, path: str | None = None, *, task_id: str, patch: dict[str, Any],
               expected_revision: int | None = None, actor: str = "mcp",
                 project_id: str | None = None, project_node_id: str | None = None,
                 project_session: str | None = None) -> dict[str, Any]:
        if not isinstance(patch, dict) or not patch:
            return {"error": "INVALID_REQUEST", "detail": "patch must be a non-empty object"}
        rejected = sorted(set(patch) - _WRITABLE)
        if rejected:
            return {"error": "FIELD_NOT_WRITABLE", "fields": rejected,
                    "detail": "id/created_at/history are server-owned; queue_task_id is set by dispatch"}
        if patch.get("status") and patch["status"] not in STATUSES:
            return {"error": "INVALID_STATUS", "status": patch["status"], "allowed": list(STATUSES)}
        if patch.get("priority") and patch["priority"] not in PRIORITIES:
            return {"error": "INVALID_PRIORITY", "priority": patch["priority"], "allowed": list(PRIORITIES)}
        if patch.get("status") == STATUS_DONE:
            return {"error": "USE_COMPLETE_TOOL",
                    "detail": "DONE is gated on real evidence -- call backlog_complete, which enforces it"}

        def _apply(document: dict[str, Any]) -> dict[str, Any]:
            item = _find(document, task_id)
            if item is None:
                return {"error": "TASK_NOT_FOUND", "task_id": task_id}
            before = {k: item.get(k) for k in patch}
            item.update({k: v for k, v in patch.items()})
            if patch.get("status") == STATUS_BLOCKED and not (patch.get("blocked_reason") or item.get("blocked_reason")):
                return {"error": "BLOCKED_REASON_REQUIRED",
                        "detail": "a BLOCKED item must say what is blocking it"}
            item["updated_at"] = now_iso()
            item["history"].append({"at": item["updated_at"], "event": "updated", "by": actor,
                                    "changed": sorted(patch)})
            return {"task_id": task_id, "item": item, "before": before}

        return self._mutate(path, expected_revision, _apply, action="backlog_update",
                            project_id=project_id, node_id=project_node_id, session=project_session)

    def bulk_update(self, path: str | None = None, *, updates: list[dict[str, Any]],
                    expected_revision: int | None = None, actor: str = "mcp",
                 project_id: str | None = None, project_node_id: str | None = None,
                 project_session: str | None = None) -> dict[str, Any]:
        """Several patches under ONE lock + ONE revision bump -- reordering
        a board is otherwise N writes and N conflict windows."""
        if not isinstance(updates, list) or not updates:
            return {"error": "INVALID_REQUEST", "detail": "updates must be a non-empty list"}

        def _apply(document: dict[str, Any]) -> dict[str, Any]:
            applied, missing = [], []
            for entry in updates:
                task_id = entry.get("task_id") or entry.get("id")
                patch = {k: v for k, v in (entry.get("patch") or entry).items()
                         if k in _WRITABLE}
                item = _find(document, str(task_id))
                if item is None:
                    missing.append(task_id)
                    continue
                if patch.get("status") == STATUS_DONE:
                    return {"error": "USE_COMPLETE_TOOL", "task_id": task_id}
                if patch.get("status") and patch["status"] not in STATUSES:
                    return {"error": "INVALID_STATUS", "status": patch["status"], "task_id": task_id}
                item.update(patch)
                item["updated_at"] = now_iso()
                item["history"].append({"at": item["updated_at"], "event": "bulk_updated",
                                        "by": actor, "changed": sorted(patch)})
                applied.append(task_id)
            if missing:
                return {"error": "TASK_NOT_FOUND", "task_ids": missing}
            return {"updated_ids": applied}

        return self._mutate(path, expected_revision, _apply, action="backlog_bulk_update",
                            project_id=project_id, node_id=project_node_id, session=project_session)

    def claim(self, path: str | None = None, *, task_id: str, session: str | None = None,
              node_id: str | None = None, assignee: str | None = None,
              expected_revision: int | None = None,
              project_id: str | None = None, project_node_id: str | None = None,
              project_session: str | None = None) -> dict[str, Any]:
        """Take ownership and move to IN_PROGRESS. Refuses to steal an
        item another session already holds unless it is being reassigned
        explicitly (assignee given)."""
        def _apply(document: dict[str, Any]) -> dict[str, Any]:
            item = _find(document, task_id)
            if item is None:
                return {"error": "TASK_NOT_FOUND", "task_id": task_id}
            if item["status"] in (STATUS_DONE,) :
                return {"error": "ALREADY_DONE", "task_id": task_id}
            holder = item.get("session")
            if holder and session and holder != session and not assignee:
                return {"error": "ALREADY_CLAIMED", "task_id": task_id, "held_by": holder,
                        "detail": "pass assignee= to reassign deliberately"}
            item["session"] = session or item.get("session")
            item["node_id"] = node_id or item.get("node_id")
            item["assignee"] = assignee or item.get("assignee") or session
            item["status"] = STATUS_IN_PROGRESS
            item["updated_at"] = now_iso()
            item["history"].append({"at": item["updated_at"], "event": "claimed",
                                    "by": item["assignee"], "session": item["session"],
                                    "node_id": item["node_id"]})
            return {"task_id": task_id, "item": item}

        return self._mutate(path, expected_revision, _apply, action="backlog_claim",
                            project_id=project_id, node_id=project_node_id, session=project_session)

    def block(self, path: str | None = None, *, task_id: str, reason: str,
              expected_revision: int | None = None, actor: str = "mcp",
                 project_id: str | None = None, project_node_id: str | None = None,
                 project_session: str | None = None) -> dict[str, Any]:
        if not str(reason or "").strip():
            return {"error": "BLOCKED_REASON_REQUIRED"}

        def _apply(document: dict[str, Any]) -> dict[str, Any]:
            item = _find(document, task_id)
            if item is None:
                return {"error": "TASK_NOT_FOUND", "task_id": task_id}
            item["status"] = STATUS_BLOCKED
            item["blocked_reason"] = reason
            item["updated_at"] = now_iso()
            item["history"].append({"at": item["updated_at"], "event": "blocked", "by": actor,
                                    "reason": reason})
            return {"task_id": task_id, "item": item}

        return self._mutate(path, expected_revision, _apply, action="backlog_block",
                            project_id=project_id, node_id=project_node_id, session=project_session)

    def complete(self, path: str | None = None, *, task_id: str,
                 commit: str | None = None, test: str | None = None, deploy: str | None = None,
                 note: str | None = None, expected_revision: int | None = None,
                 actor: str = "mcp",
                 project_id: str | None = None, project_node_id: str | None = None,
                 project_session: str | None = None) -> dict[str, Any]:
        """The VERIFIED-DONE gate (task item 7). DONE requires either
        real evidence, or a linked queue task that actually reached
        COMPLETED -- queue_store's own comment defines COMPLETED as the
        spec's VERIFIED_DONE. An agent simply asserting "done" is not
        accepted, which is the entire point of this method existing
        instead of update(status=DONE)."""
        verified_by_queue = False
        queue_state = None

        def _apply(document: dict[str, Any]) -> dict[str, Any]:
            nonlocal verified_by_queue, queue_state
            item = _find(document, task_id)
            if item is None:
                return {"error": "TASK_NOT_FOUND", "task_id": task_id}
            evidence = item.setdefault("evidence", {})
            for key, value in (("commits", commit), ("tests", test), ("deploys", deploy), ("notes", note)):
                if value:
                    evidence.setdefault(key, []).append(value)
            has_evidence = any(evidence.get(k) for k in ("commits", "tests", "deploys"))
            if item.get("queue_task_id") and self.queue is not None:
                queue_state = _queue_state(self.queue, item["queue_task_id"])
                verified_by_queue = queue_state == "COMPLETED"
            if not has_evidence and not verified_by_queue:
                return {"error": "EVIDENCE_REQUIRED", "task_id": task_id,
                        "queue_task_id": item.get("queue_task_id"), "queue_state": queue_state,
                        "detail": "DONE needs a commit/test/deploy reference, or a linked queue task that "
                                  "reached COMPLETED (VERIFIED_DONE). Use status=NEEDS_REVIEW instead if "
                                  "the work is finished but unverified."}
            item["status"] = STATUS_DONE
            item["blocked_reason"] = None
            item["updated_at"] = now_iso()
            item["history"].append({"at": item["updated_at"], "event": "completed", "by": actor,
                                    "verified_by": "queue_task" if verified_by_queue else "evidence",
                                    "queue_task_id": item.get("queue_task_id")})
            return {"task_id": task_id, "item": item,
                    "verified_by": "queue_task" if verified_by_queue else "evidence"}

        return self._mutate(path, expected_revision, _apply, action="backlog_complete",
                            project_id=project_id, node_id=project_node_id, session=project_session)

    def dispatch(self, path: str | None = None, *, task_id: str, session: str | None = None,
                 prompt: str | None = None, expected_revision: int | None = None,
                 project_id: str | None = None, project_node_id: str | None = None,
                 project_session: str | None = None) -> dict[str, Any]:
        """Backlog (intent) -> Queue (execution), the ONE crossing point.
        Creates a real queue task via the existing canonical
        QueueService.create_task and records the link on BOTH sides."""
        if self.queue is None:
            return {"error": "QUEUE_UNAVAILABLE",
                    "detail": "this server was built without a queue service; backlog stays planning-only"}
        created: dict[str, Any] = {}

        def _apply(document: dict[str, Any]) -> dict[str, Any]:
            item = _find(document, task_id)
            if item is None:
                return {"error": "TASK_NOT_FOUND", "task_id": task_id}
            if item.get("queue_task_id"):
                return {"error": "ALREADY_DISPATCHED", "task_id": task_id,
                        "queue_task_id": item["queue_task_id"]}
            if item["status"] in (STATUS_DONE,):
                return {"error": "ALREADY_DONE", "task_id": task_id}
            project = (document.get("project") or {}).get("project_id")
            body = prompt or _default_prompt(item)
            try:
                task = self.queue.create_task(
                    item["title"], body, session=session, project=project,
                    metadata={"backlog_id": item["id"],
                              "backlog_project_id": project,
                              "acceptance_criteria": item.get("acceptance_criteria") or []},
                )
            except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
                return {"error": "QUEUE_CREATE_FAILED", "detail": f"{type(exc).__name__}: {exc}"}
            if isinstance(task, dict) and task.get("error"):
                return {"error": "QUEUE_CREATE_FAILED", "detail": task}
            queue_task_id = _task_id_of(task)
            item["queue_task_id"] = queue_task_id
            item["session"] = session or item.get("session")
            item["status"] = STATUS_IN_PROGRESS if session else STATUS_READY
            item["updated_at"] = now_iso()
            item["history"].append({"at": item["updated_at"], "event": "dispatched",
                                    "queue_task_id": queue_task_id, "session": session})
            created.update({"queue_task": task})
            return {"task_id": task_id, "queue_task_id": queue_task_id, "item": item}

        result = self._mutate(path, expected_revision, _apply, action="backlog_dispatch",
                            project_id=project_id, node_id=project_node_id, session=project_session)
        if "error" not in result:
            result.update(created)
        return result


    # ------------------------------------------------------ export / import
    def export_file(self, path: str | None = None, *, project_id: str | None = None) -> dict[str, Any]:
        """Write a project's backlog out to `<repo>/.terminal-mcp/backlog.json`.

        This is what keeps the portability and git-review benefits now
        that the controller DB is authoritative: the file is a
        PROJECTION, committed deliberately, not the thing agents race on.
        `path` must be a local checkout (allowed_cwd_roots gated) -- the
        controller can only write to a filesystem it actually has."""
        identity, error = self._resolve(path)
        if error is not None:
            return error
        pid = project_id or identity.project_id
        document = self.db.document(pid)
        if not self.db.project(pid):
            return {"error": "NO_BACKLOG_FOR_PROJECT", "project_id": pid}
        document["project"] = identity.to_dict()
        file = backlog_path(identity.repo_root)
        try:
            store.save(file, {**document, "revision": document["revision"] - 1})
        except BacklogError as exc:
            return {"error": "EXPORT_FAILED", "detail": str(exc), "path": str(file)}
        self._audit("backlog_export", result="ok")
        return {"project": identity.to_dict(), "backlog_file": str(file),
                "exported": len(document["items"]), "revision": document["revision"]}

    def import_file(self, path: str | None = None, *, replace: bool = False) -> dict[str, Any]:
        """Read `<repo>/.terminal-mcp/backlog.json` back into the
        controller DB -- how a backlog committed by a teammate (or by an
        older file-based deployment) reaches this controller.

        MERGE by default: an incoming item with a known id updates it, an
        unknown id is added, and nothing local is deleted. `replace=True`
        is the deliberate destructive form."""
        identity, error = self._resolve(path)
        if error is not None:
            return error
        file = backlog_path(identity.repo_root)
        if not file.exists():
            return {"error": "NO_BACKLOG_FILE", "path": str(file)}
        try:
            document, repairs = store.load(file)
        except BacklogError as exc:
            return {"error": "BACKLOG_UNREADABLE", "detail": str(exc), "path": str(file)}
        pid = identity.project_id
        lock = _project_lock(pid)
        with lock:
            self.db.ensure_project(identity.to_dict())
            outcome = self.db.import_document(pid, document, replace=replace)
        self._audit("backlog_import", result="ok")
        return {"project": identity.to_dict(), "backlog_file": str(file),
                "repairs": repairs, "replaced": replace, **outcome}

    # ------------------------------------------------- knowledge-store seam
    def open_items_for_brief(self, path: str | None = None, *, limit: int = 20,
                             project_id: str | None = None, project_node_id: str | None = None,
                             project_session: str | None = None) -> dict[str, Any]:
        """The interface the Project Brief (session_knowledge) should call
        for "Open / Unrun Tasks" instead of keeping its own second list
        (task item 13). Deliberately a THIN projection of `get`, not a new
        query path: the backlog file stays the single source of truth, so
        a brief can never drift from what the backlog actually says.

        `unrun` = never dispatched (no queue_task_id) -- the ones a brief
        most wants to surface, because nothing is executing them and
        nothing else will mention them.

        Kept intentionally small and side-effect-free so the paused
        project-scoped-knowledge work can adopt it later without this
        MVP having to guess that feature's own shape."""
        result = self.get(path, include_terminal=False, limit=500, project_id=project_id,
                          node_id=project_node_id, session=project_session)
        if "error" in result:
            return result
        items = result["items"]
        unrun = [i for i in items if not i.get("queue_task_id")]
        return {
            "project": result["project"],
            "revision": result["revision"],
            "counts": result["counts"],
            "open_total": result["open_total"],
            "unrun_total": len(unrun),
            "open_items": [_brief_row(i) for i in items[:limit]],
            "unrun_items": [_brief_row(i) for i in unrun[:limit]],
        }


def _brief_row(item: dict[str, Any]) -> dict[str, Any]:
    """Only the fields a brief needs -- never the full history/evidence
    blob, which would bloat every brief for no benefit."""
    return {"id": item["id"], "title": item["title"], "status": item["status"],
            "priority": item["priority"], "type": item["type"],
            "queue_task_id": item.get("queue_task_id"), "tags": item.get("tags") or [],
            "blocked_reason": item.get("blocked_reason")}


def _default_prompt(item: dict[str, Any]) -> str:
    lines = [item["title"]]
    if item.get("description"):
        lines += ["", item["description"]]
    if item.get("acceptance_criteria"):
        lines += ["", "Acceptance criteria:"] + [f"- {c}" for c in item["acceptance_criteria"]]
    lines += ["", f"(backlog item {item['id']})"]
    return "\n".join(lines)


def _find(document: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    for item in document["items"]:
        if item["id"] == task_id:
            return item
    return None


def _as_set(value: str | list[str] | None) -> set[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return {value}
    return set(value)


def _task_id_of(task: Any) -> str | None:
    if isinstance(task, dict):
        for key in ("task_id", "id"):
            if task.get(key):
                return str(task[key])
        inner = task.get("task")
        if isinstance(inner, dict):
            return _task_id_of(inner)
    return None


def _queue_state(queue: Any, queue_task_id: str) -> str | None:
    """The linked queue task's current status, or None if it cannot be
    read. Tolerant of the response SHAPE on purpose: QueueService.
    task_status returns {"task": {...,"status": ...}, "queue_position":
    ...}, but the field is looked up as both `status` and `state`, at the
    top level and nested, so this keeps working if that surface is
    reshaped -- a wrong answer here would silently weaken the DONE gate,
    so it fails CLOSED (None => no queue verification => evidence still
    required) rather than guessing."""
    for method in ("task_status", "get_task", "status"):
        fn = getattr(queue, method, None)
        if fn is None:
            continue
        try:
            row = fn(queue_task_id)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(row, dict):
            continue
        for candidate in (row, row.get("task") if isinstance(row.get("task"), dict) else None):
            if not candidate:
                continue
            state = candidate.get("status") or candidate.get("state")
            if state:
                return str(state)
    return None
