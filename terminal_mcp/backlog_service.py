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

from pathlib import Path
from typing import Any

from . import backlog_store as store
from .backlog_store import (
    OPEN_STATUSES, PRIORITIES, STATUS_BACKLOG, STATUS_BLOCKED, STATUS_DONE,
    STATUS_IN_PROGRESS, STATUS_NEEDS_REVIEW, STATUS_READY, STATUSES, TYPES,
    BacklogError, backlog_path, file_lock, new_item_id, now_iso,
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


class BacklogService:
    def __init__(self, config: Any, *, audit: Any = None, queue: Any = None) -> None:
        self.config = config
        self.audit = audit
        self.queue = queue

    # ---------------------------------------------------------------- paths
    def _resolve(self, path: str | None) -> tuple[ProjectIdentity | None, dict[str, Any] | None]:
        """path (any dir inside a project) -> canonical identity, or an
        error dict. Order matters: the allowed_cwd_roots gate runs BEFORE
        any git introspection, so a traversal attempt is refused without
        this process ever touching the target directory."""
        resolved, error = resolve_cwd(path, self.config)
        if error is not None:
            return None, {"error": "PATH_NOT_ALLOWED", "detail": error, "path": path}
        identity = resolve_project(str(resolved))
        if identity is None:
            return None, {"error": "NOT_A_PROJECT", "path": str(resolved),
                          "detail": "not inside a git repository -- a backlog belongs to a project, "
                                    "so this is refused rather than creating one in an arbitrary directory"}
        # The repo root itself must ALSO be inside an allowed root: a repo
        # can be discovered by walking up out of an allowed subdirectory.
        root_ok, root_error = resolve_cwd(identity.repo_root, self.config)
        if root_error is not None:
            return None, {"error": "PATH_NOT_ALLOWED", "detail": root_error, "path": identity.repo_root}
        return identity, None

    def _audit(self, action: str, *, result: str, reason: str | None = None) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(action=action, session=None, result=result, reason=reason,
                              source_transport="mcp")
        except Exception:  # noqa: BLE001 - auditing must never break the operation
            pass

    # ---------------------------------------------------------------- read
    def get(self, path: str | None = None, *, status: str | list[str] | None = None,
            priority: str | list[str] | None = None, type: str | None = None,
            tag: str | None = None, assignee: str | None = None,
            include_terminal: bool = True, limit: int = 500) -> dict[str, Any]:
        identity, error = self._resolve(path)
        if error is not None:
            return error
        file = backlog_path(identity.repo_root)
        try:
            document, repairs = store.load(file)
        except BacklogError as exc:
            return {"error": "BACKLOG_UNREADABLE", "detail": str(exc), "path": str(file)}

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
        counts: dict[str, int] = {s: 0 for s in STATUSES}
        for item in document["items"]:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return {
            "project": identity.to_dict(),
            "backlog_file": str(file),
            "exists": file.exists(),
            "revision": document["revision"],
            "schema_version": document["schema_version"],
            "counts": counts,
            "open_total": sum(counts.get(s, 0) for s in OPEN_STATUSES),
            "total": len(document["items"]),
            "returned": len(items[:limit]),
            "items": items[:limit],
            "repairs": repairs,
        }

    def validate(self, path: str | None = None) -> dict[str, Any]:
        """Check a possibly hand-edited file and, if anything needed
        normalising, WRITE the normalised form back (task item 3's
        `sync`). Reports exactly what changed rather than silently
        rewriting the user's file."""
        identity, error = self._resolve(path)
        if error is not None:
            return error
        file = backlog_path(identity.repo_root)
        if not file.exists():
            return {"project": identity.to_dict(), "backlog_file": str(file), "exists": False,
                    "valid": True, "repairs": [], "written": False}
        try:
            with file_lock(file):
                document, repairs = store.load(file)
                written = False
                if repairs:
                    document = store.save(file, document)
                    written = True
        except BacklogError as exc:
            return {"error": "BACKLOG_UNREADABLE", "detail": str(exc), "path": str(file), "valid": False}
        self._audit("backlog_validate", result="ok" if not repairs else "repaired")
        return {"project": identity.to_dict(), "backlog_file": str(file), "exists": True,
                "valid": True, "repairs": repairs, "written": written,
                "revision": document["revision"], "total": len(document["items"])}

    # --------------------------------------------------------------- write
    def _mutate(self, path: str | None, expected_revision: int | None,
                mutator: Any, *, action: str) -> dict[str, Any]:
        """One locked read-modify-write for every mutating operation, so
        atomicity/locking/revision/audit are implemented exactly once."""
        identity, error = self._resolve(path)
        if error is not None:
            return error
        file = backlog_path(identity.repo_root)
        try:
            with file_lock(file):
                document, repairs = store.load(file)
                if expected_revision is not None and int(expected_revision) != document["revision"]:
                    self._audit(action, result="conflict")
                    return {"error": "REVISION_CONFLICT", "expected_revision": int(expected_revision),
                            "actual_revision": document["revision"], "backlog_file": str(file),
                            "detail": "another agent wrote this backlog first -- re-read it and retry "
                                      "so their change is not clobbered"}
                if not document.get("project"):
                    document["project"] = identity.to_dict()
                outcome = mutator(document)
                if isinstance(outcome, dict) and "error" in outcome:
                    self._audit(action, result="refused", reason=outcome["error"])
                    return outcome
                if len(document["items"]) > _MAX_ITEMS:
                    return {"error": "BACKLOG_TOO_LARGE", "limit": _MAX_ITEMS}
                document = store.save(file, document)
        except BacklogError as exc:
            self._audit(action, result="error", reason=str(exc)[:200])
            return {"error": "BACKLOG_WRITE_FAILED", "detail": str(exc), "path": str(file)}
        self._audit(action, result="ok")
        result = {"project": identity.to_dict(), "backlog_file": str(file),
                  "revision": document["revision"], "repairs": repairs}
        if isinstance(outcome, dict):
            result.update(outcome)
        return result

    def add(self, path: str | None = None, *, tasks: list[dict[str, Any]],
            expected_revision: int | None = None, source: str = "mcp") -> dict[str, Any]:
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

        return self._mutate(path, expected_revision, _apply, action="backlog_add")

    def update(self, path: str | None = None, *, task_id: str, patch: dict[str, Any],
               expected_revision: int | None = None, actor: str = "mcp") -> dict[str, Any]:
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

        return self._mutate(path, expected_revision, _apply, action="backlog_update")

    def bulk_update(self, path: str | None = None, *, updates: list[dict[str, Any]],
                    expected_revision: int | None = None, actor: str = "mcp") -> dict[str, Any]:
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

        return self._mutate(path, expected_revision, _apply, action="backlog_bulk_update")

    def claim(self, path: str | None = None, *, task_id: str, session: str | None = None,
              node_id: str | None = None, assignee: str | None = None,
              expected_revision: int | None = None) -> dict[str, Any]:
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

        return self._mutate(path, expected_revision, _apply, action="backlog_claim")

    def block(self, path: str | None = None, *, task_id: str, reason: str,
              expected_revision: int | None = None, actor: str = "mcp") -> dict[str, Any]:
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

        return self._mutate(path, expected_revision, _apply, action="backlog_block")

    def complete(self, path: str | None = None, *, task_id: str,
                 commit: str | None = None, test: str | None = None, deploy: str | None = None,
                 note: str | None = None, expected_revision: int | None = None,
                 actor: str = "mcp") -> dict[str, Any]:
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

        return self._mutate(path, expected_revision, _apply, action="backlog_complete")

    def dispatch(self, path: str | None = None, *, task_id: str, session: str | None = None,
                 prompt: str | None = None, expected_revision: int | None = None) -> dict[str, Any]:
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
                    metadata={"backlog_id": item["id"], "backlog_file": str(backlog_path(
                        (document.get("project") or {}).get("repo_root") or ".")),
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

        result = self._mutate(path, expected_revision, _apply, action="backlog_dispatch")
        if "error" not in result:
            result.update(created)
        return result


    # ------------------------------------------------- knowledge-store seam
    def open_items_for_brief(self, path: str | None = None, *, limit: int = 20) -> dict[str, Any]:
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
        result = self.get(path, include_terminal=False, limit=500)
        if "error" in result:
            return result
        items = result["items"]
        unrun = [i for i in items if not i.get("queue_task_id")]
        return {
            "project": result["project"],
            "backlog_file": result["backlog_file"],
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
