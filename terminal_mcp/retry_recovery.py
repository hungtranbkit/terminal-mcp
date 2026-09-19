"""How a retry continues work instead of restarting it (TMCP-RETRY-CONTEXT-002).

THE FAILURE THIS EXISTS FOR

Observed in production: when a long Claude/Codex task is retried, the retry
effectively begins a fresh agent turn. An hour of reasoning is gone and the
work restarts from zero. The mechanism is not subtle -- `queue_engine.
build_dispatch_text` embeds `task.prompt` verbatim on EVERY attempt, so
"retry" has always meant "say the whole thing again", and the agent has no way
to tell a retry from a brand-new task.

WHAT THIS MODULE DECIDES

One ordered escalation, first applicable mode wins. The order is the whole
point: the cheapest recovery that preserves the most context is tried first,
and the one that throws context away is reachable only when nothing else is.

  RECOVER_LIVE_CONVERSATION   the session and its agent process are both
                              alive. Send a SHORT continuation into the
                              conversation already in progress. No new
                              session, no relaunch, no /clear, no prompt.
  RESUME_NATIVE_CONVERSATION  the session survived but the agent process
                              died, and we durably hold its conversation id.
                              Relaunch via the agent's OWN resume mechanism
                              (Claude/Codex `--resume <id>`) so the prior
                              conversation comes back, then continue.
  RESUME_FROM_CHECKPOINT      no live conversation and no usable
                              conversation id, but a durable recovery
                              capsule exists. Rebuild context from the
                              capsule -- completed steps, next step,
                              branch/worktree, tests already run -- and
                              continue from there.
  RECOVERY_RESTART            last resort, and the ONLY mode permitted to
                              replay the original prompt. Reached only when
                              there is no live conversation, no resumable
                              conversation id, and no checkpoint.

WHAT IT NEVER DOES

`RetryPlan` carries `clears_history`, `recreate_session`, `relaunch_agent` and
`replays_prompt` as explicit fields rather than leaving them implied, so a
caller cannot perform a history-destroying action by accident and a test can
assert their absence directly. `clears_history` is False in every mode -- no
recovery path has any reason to run /clear, and stating it as a field is what
makes that checkable rather than merely intended.

PURE

No tmux, no queue store, no controller, no I/O. The caller gathers the facts,
this decides, the caller acts. That is what lets the production failure be
reproduced in a unit test instead of only against a live fleet -- the same
reason submit_flow.py is written this way.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

# -- modes, in escalation order ----------------------------------------------
RECOVER_LIVE_CONVERSATION = "RECOVER_LIVE_CONVERSATION"
RESUME_NATIVE_CONVERSATION = "RESUME_NATIVE_CONVERSATION"
RESUME_FROM_CHECKPOINT = "RESUME_FROM_CHECKPOINT"
RECOVERY_RESTART = "RECOVERY_RESTART"

RETRY_MODES = (RECOVER_LIVE_CONVERSATION, RESUME_NATIVE_CONVERSATION,
               RESUME_FROM_CHECKPOINT, RECOVERY_RESTART)
"""Ordered, and the order is load-bearing: `plan_retry` walks it and takes the
first mode whose preconditions hold, so a later mode can never shadow an
earlier one. RECOVERY_RESTART is last because it is the only one that loses
the conversation."""

# Only these modes may relaunch the agent process, and only RECOVERY_RESTART
# may resend the task's own prompt. Named as sets so a caller/test asserts
# against one definition instead of re-deriving the rule.
MODES_THAT_RELAUNCH_AGENT = frozenset({RESUME_NATIVE_CONVERSATION, RESUME_FROM_CHECKPOINT,
                                       RECOVERY_RESTART})
MODES_THAT_REPLAY_PROMPT = frozenset({RECOVERY_RESTART})


@dataclass(frozen=True)
class RecoveryCapsule:
    """The durable recovery record, deliberately independent of any chat UI.

    Written before a requeue/retry/lease-expiry so it survives exactly the
    events that destroy scrollback. Every field is optional because a capsule
    written early in a task genuinely knows less than one written late, and a
    partial capsule is still far better than replaying the prompt.
    """
    completed_steps: tuple[str, ...] = ()
    next_step: str | None = None
    files_changed: tuple[str, ...] = ()
    branch: str | None = None
    commit: str | None = None
    worktree: str | None = None
    tests_run: tuple[str, ...] = ()
    test_results: str | None = None
    blockers: tuple[str, ...] = ()
    last_decision: str | None = None
    conversation_id: str | None = None

    @property
    def is_usable(self) -> bool:
        """Does this capsule carry enough to continue from?

        A capsule holding only a conversation id is NOT usable here: that id
        belongs to RESUME_NATIVE_CONVERSATION, and treating it as checkpoint
        evidence would let this mode claim a recovery it cannot actually
        perform -- the caller would build a continuation that says nothing
        about what was done.
        """
        return bool(self.completed_steps or self.next_step or self.last_decision
                    or self.files_changed or self.commit)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "RecoveryCapsule | None":
        """Tolerant of shape -- a capsule read back from JSON in a queue-task
        metadata blob has whatever the writer had. Unknown keys are ignored
        rather than raising: a newer writer must never make an older reader
        refuse to recover."""
        if not data:
            return None

        def _tuple(value: Any) -> tuple[str, ...]:
            if isinstance(value, str):
                return (value,)
            if isinstance(value, Sequence):
                return tuple(str(item) for item in value if str(item).strip())
            return ()

        def _text(value: Any) -> str | None:
            text = str(value).strip() if value is not None else ""
            return text or None

        return cls(
            completed_steps=_tuple(data.get("completed_steps")),
            next_step=_text(data.get("next_step")),
            files_changed=_tuple(data.get("files_changed")),
            branch=_text(data.get("branch")),
            commit=_text(data.get("commit")),
            worktree=_text(data.get("worktree")),
            tests_run=_tuple(data.get("tests_run")),
            test_results=_text(data.get("test_results")),
            blockers=_tuple(data.get("blockers")),
            last_decision=_text(data.get("last_decision")),
            conversation_id=_text(data.get("conversation_id")),
        )


@dataclass(frozen=True)
class RetryContext:
    """The durable facts a retry decision is made from.

    `session_alive` and `agent_process_alive` are deliberately separate: the
    whole reason RESUME_NATIVE_CONVERSATION exists is that a tmux session
    routinely outlives the agent process inside it, and collapsing the two
    into one "is it up" boolean is what makes that case unreachable.
    """
    task_id: str
    session: str
    request_key: str | None = None
    attempt: int = 1
    session_alive: bool = False
    agent_process_alive: bool = False
    agent_type: str | None = None
    conversation_id: str | None = None
    resume_capable_agent_types: frozenset[str] = frozenset({"claude", "codex"})
    capsule: RecoveryCapsule | None = None
    branch: str | None = None
    worktree: str | None = None

    @property
    def can_resume_natively(self) -> bool:
        """A conversation id is only resumable if the agent it belongs to
        actually supports resuming. A `shell` session has no conversation to
        resume even if something once recorded an id for it."""
        return bool(self.conversation_id
                    and (self.agent_type or "").casefold() in self.resume_capable_agent_types)


@dataclass(frozen=True)
class RetryPlan:
    """What the caller should do, and every context-destroying thing it must
    not do, stated rather than implied."""
    mode: str
    reason: str
    continuation_text: str = ""
    replays_prompt: bool = False
    clears_history: bool = False
    recreate_session: bool = False
    relaunch_agent: bool = False
    resume_conversation_id: str | None = None
    preserved: dict[str, Any] = field(default_factory=dict)

    @property
    def preserves_conversation(self) -> bool:
        return self.mode in (RECOVER_LIVE_CONVERSATION, RESUME_NATIVE_CONVERSATION)

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "reason": self.reason,
                "replays_prompt": self.replays_prompt, "clears_history": self.clears_history,
                "recreate_session": self.recreate_session, "relaunch_agent": self.relaunch_agent,
                "resume_conversation_id": self.resume_conversation_id,
                "preserved": dict(self.preserved)}


def _preserved_identity(ctx: RetryContext) -> dict[str, Any]:
    """The identity that must survive every recovery mode unchanged.

    Carried on the plan itself so a caller writes back the SAME logical task
    rather than minting a new one -- the ownership half of the invariant lives
    in `duplicate_retry_is_noop` below.
    """
    capsule = ctx.capsule
    return {
        "task_id": ctx.task_id,
        "request_key": ctx.request_key,
        "session": ctx.session,
        "attempt": ctx.attempt,
        "branch": ctx.branch or (capsule.branch if capsule else None),
        "worktree": ctx.worktree or (capsule.worktree if capsule else None),
        "conversation_id": ctx.conversation_id,
        "completed_steps": list(capsule.completed_steps) if capsule else [],
        "tests_run": list(capsule.tests_run) if capsule else [],
        "files_changed": list(capsule.files_changed) if capsule else [],
    }


def build_continuation_text(ctx: RetryContext, mode: str) -> str:
    """A SHORT continuation instruction -- never the task's own prompt.

    Deliberately not a summary of the request: a live conversation already
    holds the request, and re-stating it is how a continuation turns back into
    a restart. What the agent cannot know by itself is that a retry happened
    and which attempt this is, so that is what this says. For the checkpoint
    mode the capsule's own contents are included, because there the
    conversation genuinely is gone and the capsule is the only context there
    is.
    """
    head = (f"[terminal-mcp retry: continue the SAME task, do not start over] "
            f"task_id={ctx.task_id} attempt={ctx.attempt}")
    if mode == RECOVER_LIVE_CONVERSATION:
        return (f"{head}\n"
                "Your session and this conversation are intact. Keep going from exactly where "
                "you stopped -- do not re-plan, do not restart, do not repeat completed work. "
                "If you had finished a step, continue with the next one.")
    if mode == RESUME_NATIVE_CONVERSATION:
        return (f"{head}\n"
                "Your agent process was restarted and this conversation was resumed from its "
                "own history. Confirm you can still see the earlier turns of this task, then "
                "continue from where you stopped. If the earlier context is genuinely missing, "
                "say so instead of restarting the work from scratch.")
    if mode == RESUME_FROM_CHECKPOINT:
        capsule = ctx.capsule or RecoveryCapsule()
        lines = [head,
                 "This conversation was lost. Continue the SAME task from the durable checkpoint "
                 "below -- treat the completed steps as done and do not redo them."]
        if capsule.branch or capsule.worktree:
            lines.append(f"branch={capsule.branch or '?'} worktree={capsule.worktree or '?'} "
                         f"commit={capsule.commit or '?'}")
        if capsule.completed_steps:
            lines.append("completed: " + "; ".join(capsule.completed_steps))
        if capsule.files_changed:
            lines.append("files changed: " + ", ".join(capsule.files_changed))
        if capsule.tests_run:
            lines.append("tests already run: " + "; ".join(capsule.tests_run)
                         + (f" -> {capsule.test_results}" if capsule.test_results else ""))
        if capsule.blockers:
            lines.append("blockers: " + "; ".join(capsule.blockers))
        if capsule.last_decision:
            lines.append("last decision: " + capsule.last_decision)
        lines.append("next step: " + (capsule.next_step or "re-derive the next step from the above, "
                                      "then continue"))
        return "\n".join(lines)
    # RECOVERY_RESTART builds no continuation: the caller replays the prompt,
    # which is exactly what makes it the last resort.
    return ""


def plan_retry(ctx: RetryContext) -> RetryPlan:
    """Pick the first mode whose preconditions hold. Never guesses upward: a
    mode is only chosen when the evidence for it is actually present, so an
    absent conversation id or an unusable capsule escalates rather than
    producing a plan the caller cannot carry out."""
    if ctx.session_alive and ctx.agent_process_alive:
        return RetryPlan(
            RECOVER_LIVE_CONVERSATION,
            reason=("the session and its agent process are both alive -- continuing the "
                    "conversation already in progress"),
            continuation_text=build_continuation_text(ctx, RECOVER_LIVE_CONVERSATION),
            preserved=_preserved_identity(ctx),
        )
    if ctx.session_alive and ctx.can_resume_natively:
        return RetryPlan(
            RESUME_NATIVE_CONVERSATION,
            reason=(f"the session is alive but its {ctx.agent_type} process is gone; conversation "
                    f"id is known, so the agent's own resume mechanism restores the conversation"),
            continuation_text=build_continuation_text(ctx, RESUME_NATIVE_CONVERSATION),
            relaunch_agent=True,
            resume_conversation_id=ctx.conversation_id,
            preserved=_preserved_identity(ctx),
        )
    if not ctx.session_alive and ctx.can_resume_natively:
        # The session itself has to be recreated, but the CONVERSATION does not
        # have to be lost with it -- this is the case a bare "session gone ->
        # start over" rule throws away for no reason.
        return RetryPlan(
            RESUME_NATIVE_CONVERSATION,
            reason=(f"the session is gone but the {ctx.agent_type} conversation id is durably "
                    f"known, so it is recreated and the conversation resumed rather than restarted"),
            continuation_text=build_continuation_text(ctx, RESUME_NATIVE_CONVERSATION),
            recreate_session=True,
            relaunch_agent=True,
            resume_conversation_id=ctx.conversation_id,
            preserved=_preserved_identity(ctx),
        )
    if ctx.capsule is not None and ctx.capsule.is_usable:
        return RetryPlan(
            RESUME_FROM_CHECKPOINT,
            reason=("no live or resumable conversation, but a usable durable checkpoint exists -- "
                    "continuing from it rather than replaying the task"),
            continuation_text=build_continuation_text(ctx, RESUME_FROM_CHECKPOINT),
            recreate_session=not ctx.session_alive,
            relaunch_agent=True,
            preserved=_preserved_identity(ctx),
        )
    return RetryPlan(
        RECOVERY_RESTART,
        reason=("no live conversation, no resumable conversation id and no usable checkpoint -- "
                "the original prompt is replayed only because every context-preserving path is "
                "genuinely unavailable"),
        replays_prompt=True,
        recreate_session=not ctx.session_alive,
        relaunch_agent=True,
        preserved=_preserved_identity(ctx),
    )


def duplicate_retry_is_noop(*, task_id: str, request_key: str | None,
                            active_owner: Mapping[str, Any] | None) -> tuple[bool, str]:
    """Is this retry a duplicate of one already in flight?

    One owner at a time, keyed on the LOGICAL task -- task_id, or request_key
    when a caller only has that. Returns (is_noop, reason). A second retry of
    a task somebody already owns is reconciled to a no-op rather than
    dispatched, which is what stops one long task being handed to two agents.
    """
    if not active_owner:
        return False, "no active owner recorded for this task"
    if active_owner.get("task_id") == task_id:
        return True, (f"task {task_id} is already owned by "
                      f"{active_owner.get('owner') or 'another worker'} -- retry is a no-op")
    if request_key and active_owner.get("request_key") == request_key:
        return True, (f"request_key {request_key!r} is already in flight as task "
                      f"{active_owner.get('task_id')} -- retry is a no-op")
    return False, "the active owner belongs to a different logical task"
