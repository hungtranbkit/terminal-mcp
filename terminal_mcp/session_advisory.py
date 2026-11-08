"""Serialized advisory checks for session creation on MCP and dashboard routes."""
from __future__ import annotations

import os
import threading
from typing import Any, Callable

from .task_labels import summarize_prompt

_CREATE_LOCK = threading.Lock()


def create_session_with_advisories(controller: Any, *, name: str, agent_type: str,
                                   cwd: str | None, initial_prompt: str | None = None,
                                   label_index: Callable[[], dict] | None = None,
                                   on_created: Callable[[dict[str, Any]], None] | None = None,
                                   confirm_advisories: bool = False,
                                   **create_kwargs: Any) -> dict[str, Any]:
    """Create under a shared check lock and attach non-blocking reuse advice.

    Listing is one bounded fleet read. Cwd is available only for sessions the
    caller can read, so missing path data simply suppresses the project count.
    The advisory never attaches to or deletes a session. Incomplete inventory
    blocks creation until an explicit confirmation because duplicate detection
    cannot be trusted while nodes are unverified.
    """
    advisories: list[dict[str, Any]] = []
    with _CREATE_LOCK:
        try:
            listing = controller.terminal_list_sessions(budget_seconds=3.0) or {}
            if listing.get("error"):
                advisories.append({"code": "SESSION_AUDIT_UNAVAILABLE",
                                   "reason": str(listing.get("error")),
                                   "recommendation": "retry after the session inventory is readable"})
            elif listing.get("unreachable_nodes") or listing.get("source_errors"):
                gaps = listing.get("unreachable_nodes") or listing.get("source_errors")
                advisories.append({"code": "SESSION_AUDIT_PARTIAL", "unverified": gaps,
                                   "recommendation": "inventory is partial; retry after nodes are verified, or explicitly confirm intentional creation"})
            sessions = listing.get("sessions", [])
            live_names = {row.get("name") for row in sessions if row.get("name")}
            project = os.path.realpath(cwd) if cwd else None
            wanted = summarize_prompt(initial_prompt) if initial_prompt else None
            if wanted and label_index is not None:
                labels = label_index()
                matches = [session for (_node_id, session), label in labels.items()
                           if project and session in live_names and
                           any(row.get("name") == session and row.get("cwd") and
                               os.path.realpath(row["cwd"]) == project for row in sessions) and
                           str(label.get("summary") or "").casefold() == wanted.casefold()]
                if matches:
                    advisories.append({"code": "SAME_TASK_SESSION_EXISTS",
                                       "sessions": sorted(set(matches)),
                                       "recommendation": "reuse a listed session or explicitly choose parallel work"})
            if project:
                same_project = [row.get("name") for row in sessions
                                if row.get("cwd") and os.path.realpath(row["cwd"]) == project]
                if len(same_project) >= 3:
                    advisories.append({"code": "PROJECT_SESSION_SPRAWL", "project": project,
                                       "existing_sessions": sorted(same_project),
                                       "recommendation": "reuse an existing project session when possible"})
        except Exception as exc:  # noqa: BLE001 -- fail closed on unknown inventory
            advisories = [{"code": "SESSION_AUDIT_UNAVAILABLE",
                           "reason": type(exc).__name__,
                           "recommendation": "retry after the session inventory is readable"}]
        if advisories and not confirm_advisories:
            return {"error": "SESSION_CREATE_ADVISORY", "created": False,
                    "advisories": advisories,
                    "reason": "review reuse/sprawl advice and retry with confirm_advisories=true to create intentionally"}
        result = controller.terminal_create_session(name, agent_type, cwd,
                                                    initial_prompt=initial_prompt,
                                                    **create_kwargs)
        if isinstance(result, dict) and "error" not in result and on_created is not None:
            on_created(result)
    if advisories and isinstance(result, dict) and "error" not in result:
        result["advisories"] = advisories
    return result
