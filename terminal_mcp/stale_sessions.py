"""Sessions that look retired -- as a REPORT, never as an action.

WHY THIS ONLY EVER REPORTS

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
human decision, and this module has no delete path at all -- not a disabled
one, not one behind a flag.

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
