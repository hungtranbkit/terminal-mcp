"""Canonical Work Runtime persistence.

A WorkSession is the durable unit of work. Agent/provider sessions are attempts
that may be replaced without losing task state. This store is intentionally
small and provider-neutral; queue/outcome remain authoritative for scheduling
and acceptance, while this layer records continuity, context fingerprints and
attempt handoffs.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .queue_store import QueueStore, iso_now, new_task_id
from .redaction import redact_text

WORK_OPEN = "OPEN"
WORK_RUNNING = "RUNNING"
WORK_BLOCKED = "BLOCKED"
WORK_VERIFYING = "VERIFYING"
WORK_DONE = "DONE"
WORK_CANCELLED = "CANCELLED"
WORK_STATUSES = (WORK_OPEN, WORK_RUNNING, WORK_BLOCKED, WORK_VERIFYING, WORK_DONE, WORK_CANCELLED)

@dataclass(frozen=True)
class WorkSession:
    id: str; project_id: str; title: str; status: str; objective: str
    outcome_id: str | None; current_attempt_id: str | None
    context_fingerprint: str | None; created_at: str; updated_at: str
    completed_at: str | None
    def to_dict(self) -> dict[str, Any]: return self.__dict__.copy()

class WorkRuntimeStore:
    """Additive tables in queue.db; no second scheduler/state machine."""
    def __init__(self, store: QueueStore | None = None) -> None:
        self.store = store or QueueStore()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        # Runtime-created additive tables are guarded with IF NOT EXISTS so
        # older queue.db files can opt into Work Runtime without changing the
        # existing QueueStore migration numbering.
        with self.store._connection() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS work_sessions (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, outcome_id TEXT,
                title TEXT NOT NULL, objective TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                current_attempt_id TEXT, context_fingerprint TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_work_sessions_project_status ON work_sessions(project_id,status)")
            c.execute("""CREATE TABLE IF NOT EXISTS work_attempts (
                id TEXT PRIMARY KEY, work_session_id TEXT NOT NULL, provider TEXT NOT NULL,
                agent_type TEXT NOT NULL, terminal_session TEXT, node_id TEXT,
                provider_conversation_id TEXT, status TEXT NOT NULL,
                context_fingerprint TEXT, handoff_reason TEXT, summary TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, ended_at TEXT)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_work_attempts_work ON work_attempts(work_session_id,created_at)")
            c.execute("""CREATE TABLE IF NOT EXISTS verified_experiences (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, work_session_id TEXT,
                task_id TEXT, kind TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
                evidence TEXT NOT NULL DEFAULT '{}', confidence REAL NOT NULL DEFAULT 1.0,
                created_at TEXT NOT NULL, last_used_at TEXT)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_verified_experience_project ON verified_experiences(project_id,kind,created_at)")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> WorkSession | None:
        return None if row is None else WorkSession(**dict(row))

    def create(self, project_id: str, title: str, *, objective: str = "", outcome_id: str | None = None) -> WorkSession:
        if not project_id.strip() or not title.strip(): raise ValueError("project_id and title are required")
        now, wid = iso_now(), new_task_id()
        with self.store._connection() as c:
            c.execute("INSERT INTO work_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (wid, project_id.strip(), outcome_id, title.strip(), objective.strip(), WORK_OPEN,
                       None, None, now, now, None))
        return self.get(wid)  # type: ignore[return-value]

    def get(self, work_session_id: str) -> WorkSession | None:
        with self.store._connection() as c:
            return self._row(c.execute("SELECT * FROM work_sessions WHERE id=?", (work_session_id,)).fetchone())

    def start_attempt(self, work_session_id: str, *, provider: str, agent_type: str,
                      terminal_session: str | None = None, node_id: str | None = None,
                      provider_conversation_id: str | None = None,
                      context_fingerprint: str | None = None, handoff_reason: str | None = None) -> dict[str, Any]:
        now, aid = iso_now(), new_task_id()
        with self.store._connection() as c:
            work = c.execute("SELECT * FROM work_sessions WHERE id=?", (work_session_id,)).fetchone()
            if work is None: raise KeyError(f"no such work session: {work_session_id}")
            if work["status"] in (WORK_DONE, WORK_CANCELLED): raise ValueError(f"work session is {work['status']}")
            old = work["current_attempt_id"]
            if old:
                c.execute("UPDATE work_attempts SET status='HANDED_OFF', ended_at=?, updated_at=? WHERE id=? AND ended_at IS NULL", (now, now, old))
            c.execute("INSERT INTO work_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (aid, work_session_id, provider, agent_type, terminal_session, node_id,
                       provider_conversation_id, "ACTIVE", context_fingerprint,
                       redact_text(handoff_reason or ""), "", now, now, None))
            c.execute("UPDATE work_sessions SET status=?, current_attempt_id=?, context_fingerprint=?, updated_at=? WHERE id=?",
                      (WORK_RUNNING, aid, context_fingerprint, now, work_session_id))
        return self.get_attempt(aid)  # type: ignore[return-value]

    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        with self.store._connection() as c:
            row = c.execute("SELECT * FROM work_attempts WHERE id=?", (attempt_id,)).fetchone()
        return dict(row) if row else None

    def attempts(self, work_session_id: str) -> list[dict[str, Any]]:
        with self.store._connection() as c:
            # rowid is the insertion sequence. Timestamps have finite
            # resolution and can tie for fast provider handoffs; UUID order
            # is deterministic but does not preserve attempt chronology.
            rows = c.execute("SELECT * FROM work_attempts WHERE work_session_id=? ORDER BY created_at,rowid", (work_session_id,)).fetchall()
        return [dict(r) for r in rows]

    def add_verified_experience(self, project_id: str, *, kind: str, title: str, content: str,
                                evidence: dict[str, Any], work_session_id: str | None = None,
                                task_id: str | None = None, confidence: float = 1.0) -> dict[str, Any]:
        if not evidence: raise ValueError("verified experience requires evidence")
        eid, now = new_task_id(), iso_now()
        clean_content = redact_text(content)
        clean_evidence = json.loads(redact_text(json.dumps(evidence, ensure_ascii=False)))
        with self.store._connection() as c:
            c.execute("INSERT INTO verified_experiences VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (eid, project_id, work_session_id, task_id, kind, title, clean_content,
                       json.dumps(clean_evidence, ensure_ascii=False), float(confidence), now, None))
        return {"id": eid, "project_id": project_id, "kind": kind, "title": title,
                "content": clean_content, "evidence": clean_evidence, "confidence": float(confidence),
                "created_at": now}

    def list_verified_experience(self, project_id: str, *, kind: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM verified_experiences WHERE project_id=?", [project_id]
        if kind: sql += " AND kind=?"; params.append(kind)
        sql += " ORDER BY confidence DESC, created_at DESC LIMIT ?"; params.append(int(limit))
        with self.store._connection() as c: rows = c.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row); item["evidence"] = json.loads(item["evidence"] or "{}"); result.append(item)
        return result
