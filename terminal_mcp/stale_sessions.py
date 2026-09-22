"""Sessions that look retired -- a report, and ONE confirmed action.

WHY THE REPORT NEVER ACTS BY ITSELF

The fleet accumulates idle sessions whose worktrees were deleted weeks ago.
They are noise in every listing and, before the router existed, they were
worse than noise: a dispatcher that picked "any idle session" would pick one
of these and the task would go nowhere. The router already refuses them
(session_matcher.WORKTREE_MISSING), so the remaining problem is purely
operational -- somebody should clean them up.

Somebody, not something. An automatic delete here would be a process that
destroys a real session on the strength of a heuristic, and the failure mode
is unrecoverable: a session killed while its agent was mid-task loses work
that exists nowhere else. Every candidate below is therefore evidence for a
human decision. Nothing sweeps, nothing schedules, nothing runs on a timer.

THE ONE ACTION, AND WHY IT RE-CHECKS EVERYTHING

`cleanup_session` exists so an operator who has read the evidence can act on
it from the same screen instead of copying a session name into a different
tool. It is a named, one-session, explicitly-requested delete -- and it
RE-DERIVES the whole candidacy decision immediately before deleting, from a
freshly refreshed fleet read.

That re-check is the point, not ceremony. The report an operator is looking at
is seconds old at best; a session that was idle when the page rendered may be
running work by the time they click. So the operator's click authorizes
deleting a session that is STILL a candidate, never a session that merely was
one. A session that stopped qualifying comes back NOT_A_CANDIDATE with the
reason, and nothing is deleted.

WHAT MAKES A CANDIDATE

All of these, never any one of them:

  * idle, and not waiting for human input
  * no active and no queued task, and no routing claim
  * demonstrable retirement evidence -- the working directory is GONE (proven,
    not merely unreadable), or the registry has already recorded the session
    as killed/deleted/missing

"Proven" is the load-bearing word. A remote session's cwd cannot be checked
from this host (see session_matcher.default_worktree_probe), so a remote
session is never a candidate on worktree grounds -- unknown is not evidence.
"""
from __future__ import annotations

from typing import Any, Sequence

from . import session_matcher as sm

#: Registry statuses that are themselves retirement evidence.
RETIRED_REGISTRY_STATUSES = ("KILLED", "DELETED", "MISSING")

#: Never proposed, whatever the evidence says. An operator's admin shell is
#: idle and repo-less by design; proposing it every single run would train
#: whoever reads this report to stop reading it.
DEFAULT_PROTECTED = ("window", "window2")


def _protected_names(config: Any, extra: Sequence[str] = ()) -> set[str]:
    names = set(DEFAULT_PROTECTED) | {str(name) for name in extra}
    for attribute in ("protected_sessions", "admin_sessions"):
        values = getattr(getattr(config, "permissions", None), attribute, None) or \
                 getattr(config, attribute, None)
        if values:
            names |= {str(value) for value in values}
    return names


def _evidence_for(candidate: sm.SessionCandidate) -> list[str]:
    evidence: list[str] = []
    if candidate.registry_status in RETIRED_REGISTRY_STATUSES:
        evidence.append(f"registry records this session as {candidate.registry_status}")
    if candidate.worktree_exists is False:
        path = candidate.worktree_path or candidate.cwd
        evidence.append(f"working directory no longer exists ({path})")
    return evidence


def cleanup_candidates(router: Any, *, config: Any = None, limit: int = 50,
                       protected: Sequence[str] = ()) -> dict[str, Any]:
    """Report only. Returns candidates, and the sessions deliberately excluded.

    The excluded list is not padding: "why is this obviously-dead session not
    in the list" is the first question anyone asks of a report like this, and
    answering it in the same payload is what stops someone deleting by hand
    the thing the report was protecting."""
    protected_names = _protected_names(config, protected)
    try:
        candidates = router.candidates(refresh=True)
    except Exception as exc:  # noqa: BLE001 -- a report must not take the server down
        return {"error": "FLEET_READ_FAILED", "detail": f"{type(exc).__name__}: {exc}",
                "candidates": [], "excluded": []}

    proposed: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for candidate in candidates:
        state = (candidate.state or "").upper()
        if candidate.session in protected_names:
            excluded.append({"session": candidate.session, "reason": "PROTECTED",
                             "detail": "configured admin/protected session"})
            continue
        if state in sm.BLOCKED_STATES:
            excluded.append({"session": candidate.session, "reason": "WAITING_INPUT",
                             "detail": "a human is mid-conversation here"})
            continue
        if state in sm.BUSY_STATES:
            excluded.append({"session": candidate.session, "reason": "BUSY",
                             "detail": "work is running in this session"})
            continue
        if candidate.active_tasks or candidate.queued_tasks or candidate.claimed_by_task:
            excluded.append({"session": candidate.session, "reason": "HAS_TASKS",
                             "detail": f"{candidate.active_tasks} active / "
                                       f"{candidate.queued_tasks} queued task(s)"})
            continue
        evidence = _evidence_for(candidate)
        if not evidence:
            continue
        if state and state not in sm.IDLE_STATES:
            # Neither idle nor busy -- UNKNOWN, most often. Not evidence of
            # anything, and certainly not grounds for proposing a deletion.
            excluded.append({"session": candidate.session, "reason": "STATE_UNKNOWN",
                             "detail": f"state is {state}, not a confirmed idle session"})
            continue
        proposed.append({
            "session": candidate.session, "node_id": candidate.node_id,
            "node_name": candidate.node_name, "state": candidate.state,
            "cwd": candidate.cwd, "worktree_path": candidate.worktree_path,
            "registry_status": candidate.registry_status,
            "evidence": evidence,
            "suggested_action": "terminal_delete_session after confirming with the owner",
        })
    proposed.sort(key=lambda row: row["session"])
    return {
        "candidates": proposed[:limit],
        "candidate_count": len(proposed),
        "excluded": excluded,
        "sessions_examined": len(candidates),
        "report_only": True,
        "note": ("This is a report. Nothing here has been or will be deleted automatically -- "
                 "confirm each session with its owner before removing it."),
    }


#: Why a confirmed cleanup was refused. Each one is the exact exclusion rule
#: from `cleanup_candidates`, re-evaluated at delete time rather than trusted
#: from the report the operator was reading.
CLEANUP_REFUSED = "NOT_A_CANDIDATE"


def cleanup_session(router: Any, session: str, *, controller: Any = None,
                    config: Any = None, protected: Sequence[str] = (),
                    requested_by: str | None = None) -> dict[str, Any]:
    """Delete ONE named session, but only if it is still a stale candidate.

    `router` supplies the fleet view (refreshed, never cached -- a stale view
    is the one input that could make this destructive). `controller` performs
    the delete through the same path every other delete uses, so protected
    sessions, grants, audit and the killed-session record all apply unchanged;
    this function adds a precondition, it does not add a privilege.

    Returns the delete result on success, or `{"error": CLEANUP_REFUSED,
    "reason": ...}` when the session is no longer safe to remove.
    """
    if not session or not str(session).strip():
        return {"error": "SESSION_REQUIRED"}
    name = str(session).strip()
    if controller is None:
        return {"error": "CONTROLLER_UNAVAILABLE",
                "detail": "no controller is wired here, so nothing can be deleted"}

    report = cleanup_candidates(router, config=config, limit=10_000, protected=protected)
    if report.get("error"):
        # A fleet we could not read is a fleet we must not delete from.
        return {"error": "FLEET_READ_FAILED", "detail": report.get("detail"),
                "session": name}
    candidates = {row["session"]: row for row in report.get("candidates", [])}
    if name not in candidates:
        excluded = next((row for row in report.get("excluded", []) if row.get("session") == name), None)
        return {
            "error": CLEANUP_REFUSED,
            "session": name,
            "reason": (f"{excluded['reason']}: {excluded.get('detail')}" if excluded
                       else "this session is not a stale-cleanup candidate right now "
                            "(it may have become busy, taken a task, or its working "
                            "directory may exist after all)"),
            "excluded": excluded,
            "note": "nothing was deleted",
        }

    candidate = candidates[name]
    # The same delete every other caller uses. `requested_by` is passed when
    # the controller accepts one (it is an audit attribution, not a
    # permission) and omitted otherwise -- never a second delete path.
    try:
        result = controller.terminal_delete_session(name, requested_by=requested_by)
    except TypeError:
        result = controller.terminal_delete_session(name)
    if isinstance(result, dict) and result.get("error"):
        return {**result, "session": name, "candidate": candidate}
    return {"deleted": True, "session": name, "node_id": candidate.get("node_id"),
            "evidence": candidate.get("evidence"), "result": result,
            "requested_by": requested_by}
