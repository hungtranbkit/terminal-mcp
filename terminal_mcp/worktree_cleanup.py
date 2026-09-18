"""When an isolated task's worktree becomes a candidate for reclamation.

P1 SCOPE: MARKING ONLY. Nothing here removes anything, and there is no code
path from this module to `rm`, `git worktree remove`, or any other deletion.
`docs/WORKTREE_JANITOR.md` is the contract; this is the first code under it,
and it implements §3 (when cleanup becomes eligible) and the pending/clear
half of §4 (state model). Classification, grace expiry and execution are
later phases.

THE CHOKEPOINT, AND WHY IT IS THE ONLY ONE

`queue_store._transition_locked` is the only place a task's status changes.
The contract says so and the audit behind it names the alternative that looks
right and is not: `on_completed` has **two** call sites (`queue_service` and
`queue_engine`), so hooking either leaves the other path silently unmarked.
Everything in this module is therefore a pure function over a transition,
called from inside that one transaction.

THE PREDICATE THAT IS EASY TO GET WRONG

There is no `FAILED_FINAL` status. `TERMINAL_STATUSES` is
`(COMPLETED, SKIPPED, CANCELLED)`; `FAILED` and `BLOCKED` are explicitly not
terminal, because retry/skip/cancel all still apply to them. A bare `FAILED`
must never mark a worktree for cleanup -- that worktree is the retry's working
directory. `FAILED` only counts once the retry budget is genuinely spent.

IDEMPOTENT AND CRASH-SAFE

The record is written into the task's own metadata in the SAME transaction as
the status change, so a crash between the two is not possible: either both
landed or neither did. Re-entering a terminal state never resets an existing
record, never bumps `attempts`, and never revives a finished one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

METADATA_KEY = "worktree_cleanup"
ISOLATION_KEY = "git_isolation"

# States. P1 only ever writes CLEANUP_PENDING or clears the record; the rest
# are named here because the contract's diagram is the shared vocabulary and a
# later phase must not invent a second spelling for the same state.
CLEANUP_PENDING = "CLEANUP_PENDING"
CLEANUP_REVIEW = "CLEANUP_REVIEW"
CLEANUP_ELIGIBLE = "CLEANUP_ELIGIBLE"
CLEANUP_BLOCKED = "CLEANUP_BLOCKED"
CLEANUP_ABANDONED = "CLEANUP_ABANDONED"
CLEANUP_DONE = "CLEANUP_DONE"

CANCELLABLE_STATES = frozenset({CLEANUP_PENDING, CLEANUP_REVIEW, CLEANUP_ELIGIBLE})
"""States a reopen/retry clears: the worktree is needed again.

`CLEANUP_DONE` is deliberately absent. The worktree is already gone, and
pretending otherwise would send a retry to a directory that no longer exists;
the contract routes that through the Coordinator's `expected_cwd` gate with a
`WORKTREE_REMOVED` reason instead.
"""

# The Coordinator's refusal reason when a task's worktree has already been
# reclaimed. Declared here, beside CANCELLABLE_STATES, because the two encode
# one decision: CLEANUP_DONE is not cancellable, so a retry must be refused
# rather than silently sent to a directory that no longer exists (contract
# failure mode F12).
WORKTREE_REMOVED = "WORKTREE_REMOVED"


def is_removed(metadata: Mapping[str, Any] | None) -> bool:
    """True when this task's worktree has already been reclaimed.

    Only CLEANUP_DONE counts. A PENDING/REVIEW/ELIGIBLE record means the
    worktree is still there -- marking is not removing -- and treating those as
    removed would refuse dispatch for work that is perfectly runnable, which is
    the dangerous false direction in the other direction."""
    record = (metadata or {}).get(METADATA_KEY)
    if not isinstance(record, Mapping):
        return False
    return record.get("state") == CLEANUP_DONE


# Event types recorded on the task's own queue_events trail.
EVENT_MARKED = "WORKTREE_CLEANUP_PENDING"
EVENT_CLEARED = "WORKTREE_CLEANUP_CLEARED"


@dataclass(frozen=True)
class Decision:
    """What a transition means for this task's worktree, and why."""
    action: str                       # "mark" | "clear" | "none"
    state: str | None = None
    reason: str = ""
    event_type: str | None = None

    @property
    def changes_record(self) -> bool:
        return self.action in ("mark", "clear")


NONE = Decision("none")


def has_isolated_worktree(metadata: Mapping[str, Any] | None) -> bool:
    """Only tasks that actually own a worktree are ever marked.

    `create_isolated_task` writes `metadata.git_isolation`; a task without it
    has nothing to reclaim, and marking one would put a record on the task
    that a later phase could act on.
    """
    isolation = (metadata or {}).get(ISOLATION_KEY)
    return isinstance(isolation, Mapping) and bool(isolation.get("worktree_path"))


def is_terminal(to_status: str, *, attempt_count: int, max_attempts: int,
                terminal_statuses: tuple[str, ...]) -> bool:
    """§3's trigger predicate, and nothing looser.

    `terminal_statuses` is passed in rather than imported so this module never
    drifts from `queue_store.TERMINAL_STATUSES` -- one source of truth, checked
    by a test that compares them.
    """
    if to_status in terminal_statuses:
        return True
    if to_status == "FAILED":
        # Retryable unless the budget is genuinely spent. `>=` not `==`: a
        # task whose max_attempts was lowered after the fact must not become
        # permanently un-markable.
        try:
            return int(attempt_count) >= int(max_attempts)
        except (TypeError, ValueError):
            return False          # unreadable budget is not an exhausted one
    return False


def decide(*, from_status: str, to_status: str, metadata: Mapping[str, Any] | None,
           attempt_count: int, max_attempts: int,
           terminal_statuses: tuple[str, ...]) -> Decision:
    """What this one transition should do to the cleanup record.

    Pure. Called under the same lock and in the same transaction as the status
    change itself, so the answer and the status can never disagree.
    """
    existing = (metadata or {}).get(METADATA_KEY)
    existing_state = existing.get("state") if isinstance(existing, Mapping) else None

    terminal = is_terminal(to_status, attempt_count=attempt_count,
                           max_attempts=max_attempts,
                           terminal_statuses=terminal_statuses)

    if terminal:
        if not has_isolated_worktree(metadata):
            return NONE
        if existing_state is not None:
            # Idempotent: re-entering a terminal state must not reset an
            # existing record, bump attempts, or revive a finished one.
            return Decision("none", state=existing_state,
                            reason=f"already recorded as {existing_state}")
        return Decision("mark", state=CLEANUP_PENDING,
                        reason=f"{from_status} -> {to_status} is terminal"
                               + ("" if to_status in terminal_statuses else
                                  f" (retry budget spent: {attempt_count}/{max_attempts})"),
                        event_type=EVENT_MARKED)

    # Not terminal. If a record exists and the worktree is needed again, clear
    # it -- this is the reopen/retry case the contract calls out, and it is the
    # reason a grace period can never be the only protection.
    if existing_state in CANCELLABLE_STATES:
        return Decision("clear", state=None,
                        reason=f"reopened: {from_status} -> {to_status} while "
                               f"cleanup was {existing_state}",
                        event_type=EVENT_CLEARED)
    if existing_state == CLEANUP_DONE:
        # The worktree is already gone. Saying nothing here is deliberate:
        # clearing the record would erase the only evidence that the directory
        # a retry is about to use no longer exists.
        return Decision("none", state=CLEANUP_DONE,
                        reason="worktree already removed; record kept so the "
                               "dispatch gate can refuse with WORKTREE_REMOVED")
    return NONE


def apply(metadata: Mapping[str, Any] | None, decision: Decision, *,
          now: str) -> dict[str, Any]:
    """The task's new metadata after `decision`. Never mutates the input."""
    updated = dict(metadata or {})
    if decision.action == "mark":
        updated[METADATA_KEY] = {
            "state": decision.state,
            "classified_at": None,      # P1 does not classify
            "eligible_at": None,        # nor schedule
            "policy": None,
            "reasons": [decision.reason],
            "predicates": {},
            "attempts": 0,
            "last_error": None,
            "removed_at": None,
            "reclaimed_bytes": None,
            "evidence": {},
            "marked_at": now,
        }
    elif decision.action == "clear":
        updated.pop(METADATA_KEY, None)
    return updated
