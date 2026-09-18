"""Which sessions the Work runtime may drive automatically.

THIS IS THE WHOLE OPT-IN GUARANTEE, AND IT LIVES IN ONE PLACE

The rule: a session is eligible for automatic Work scheduling only if its
name ends in `-work`. A session without that suffix is never claimed, never
prompted and never has its state changed by the Work runtime -- regardless of
how idle it looks, regardless of what its queue lane says, and regardless of
what any other component would otherwise permit.

Why one module: the failure this prevents is silent and expensive. The queue
engine that already exists will dispatch into ANY session that has a lane. If
the Work coordinator grew its own copy of this check, or if one scheduling
path forgot to consult it, the first symptom would be an autonomous agent
typing into a human's live terminal. So there is exactly one predicate, every
path calls it, and a test asserts that no scheduling path reaches a dispatch
without it.

Eligibility is deliberately layered, most-stable first:

  1. the NAME says it may be driven          (opt-in, never inferred)
  2. the SESSION is healthy enough to drive  (exists, not dead)
  3. the NODE is reachable and fresh         (stale metadata is not trust)
  4. the PERMISSIONS allow input             (the same grant every send needs)

Each layer answers with a reason, because "not eligible" with no explanation
is how an operator ends up believing the runtime is broken when it is in fact
protecting them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

WORK_SUFFIX = "-work"

# Reasons, as stable strings: they reach an operator through the doctor, the
# UI and the MCP surface, and a caller may branch on them.
NOT_WORK_SESSION = "NOT_WORK_SESSION"
SESSION_MISSING = "SESSION_MISSING"
SESSION_DEAD = "SESSION_DEAD"
NODE_UNREACHABLE = "NODE_UNREACHABLE"
NODE_METADATA_STALE = "NODE_METADATA_STALE"
INPUT_NOT_PERMITTED = "INPUT_NOT_PERMITTED"
ELIGIBLE = "ELIGIBLE"

# A name is a `-work` name only if the suffix is the actual ending, on a
# non-empty base. `-work` alone is not a project's work session, and
# `work-thing` is not one either -- guessing generously here would be
# guessing about which terminals an autonomous agent may type into.
_WORK_NAME = re.compile(r"^(?P<base>.+)" + re.escape(WORK_SUFFIX) + r"$")


def is_work_session(name: str | None) -> bool:
    """The opt-in test. Name only -- deliberately no lookup, no I/O, no
    inference from activity, so it cannot be accidentally satisfied."""
    if not name:
        return False
    # A qualified `node/session` name is matched on its session half, which
    # is the part an operator actually named.
    session = str(name).rsplit("/", 1)[-1]
    return bool(_WORK_NAME.match(session))


def work_base_name(name: str | None) -> str | None:
    """`mesflow-work` -> `mesflow`. The project a work session belongs to,
    used to pair it with the ordinary session of the same project."""
    if not name:
        return None
    match = _WORK_NAME.match(str(name).rsplit("/", 1)[-1])
    return match.group("base") if match else None


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reason: str
    detail: str
    session: str | None = None
    node_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"eligible": self.eligible, "reason": self.reason,
                "detail": self.detail, "session": self.session, "node_id": self.node_id}


def evaluate(session: str, *, status: dict[str, Any] | None = None,
             node: dict[str, Any] | None = None,
             input_allowed: bool | None = None,
             input_denied_reason: str | None = None) -> Eligibility:
    """Can the Work runtime drive this session right now, and if not, why.

    Every caller passes evidence it already has rather than this module going
    and fetching it: the coordinator has just listed sessions and nodes, and a
    second round-trip per candidate per tick would be pure cost.

    Unknown evidence is NOT treated as permission. A missing status or an
    absent node record answers "no", because the alternative is an autonomous
    dispatch decided by a gap in what we happened to look up.
    """
    node_id = (node or {}).get("node_id") if node else None

    if not is_work_session(session):
        return Eligibility(
            False, NOT_WORK_SESSION,
            f"{session!r} does not end in {WORK_SUFFIX!r}; the Work runtime only "
            f"drives opt-in sessions and never converts an existing one",
            session=session, node_id=node_id)

    if status is None or status.get("error"):
        return Eligibility(
            False, SESSION_MISSING,
            (status or {}).get("error") or "no status available for this session",
            session=session, node_id=node_id)
    if status.get("exists") is False:
        return Eligibility(False, SESSION_MISSING,
                           "session no longer exists on its node",
                           session=session, node_id=node_id)
    if status.get("pane_dead") or str(status.get("status") or "").upper() == "DEAD":
        return Eligibility(False, SESSION_DEAD, "session pane is dead",
                           session=session, node_id=node_id)

    if node is not None:
        if str(node.get("status") or "").casefold() == "offline":
            return Eligibility(False, NODE_UNREACHABLE,
                               f"node {node_id} is offline", session=session, node_id=node_id)
        # Stale metadata is not trust. Dispatching on a record that has not
        # been refreshed is how a prompt gets sent to a machine that stopped
        # answering an hour ago.
        if node.get("metadata_stale"):
            age = node.get("metadata_age_seconds")
            return Eligibility(
                False, NODE_METADATA_STALE,
                f"node {node_id} metadata is {int(age) if age else '?'}s old; "
                f"refusing to dispatch on evidence this stale",
                session=session, node_id=node_id)

    if input_allowed is False:
        return Eligibility(False, INPUT_NOT_PERMITTED,
                           input_denied_reason or "input is not permitted for this session",
                           session=session, node_id=node_id)

    return Eligibility(True, ELIGIBLE, "opt-in work session, healthy, reachable and writable",
                       session=session, node_id=node_id)
