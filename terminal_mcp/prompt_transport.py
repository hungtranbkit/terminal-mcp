"""Prompt-submission reliability upgrade -- extension point only (P10).

This module does NOT replace adapters.py/core.py's already-hardened,
live-verified tmux send pipeline (_send_text_and_verify_locked and the
AgentAdapter evidence rules it consults). It defines the higher-level
lifecycle shape a FUTURE, non-tmux transport (a ChatGPT-Web browser
adapter, most concretely) would need to implement to plug into the same
"never claim success without evidence" discipline this project already
enforces for tmux targets -- see docs/prompt-submission.md for the full
architecture writeup and docs/chatgpt-web-adapter-plan.md for what that
future adapter would actually need to do.

Nothing in this module is wired into any live code path today. No
Playwright/browser-automation dependency is introduced anywhere in this
project by this file -- ChatGptWebTransport below is a stub that always
raises NotImplementedError, exactly as intended for this phase.

Lifecycle (mirrors PREPARE -> WRITE -> VERIFY -> ACTIVATE -> PROVE_
ACCEPTED -> OBSERVE -> [COMPLETE|FAIL] -- the tmux pipeline already
performs every one of these steps today, just not as separate method
calls on an object shaped like this):

  prepare()       -- resolve/validate the target, capture whatever
                     baseline state "before" will be compared against.
                     tmux equivalent: resolving session identity/
                     pane_current_command and selecting an AgentAdapter,
                     immediately before the send (core.py's two P0 Part
                     A.3 revalidation points ARE this step, done twice).
  write()         -- deliver the prompt TEXT only, no submission attempt
                     yet. tmux equivalent: tmux.send_text(..., press_
                     enter=False).
  verify()        -- confirm the text actually landed as written (an
                     optional, transport-specific integrity check -- the
                     tmux transport currently trusts the text write and
                     verifies at the ACTIVATE/PROVE_ACCEPTED steps
                     instead; a browser transport reading back its own
                     composer's DOM value is the natural place to use
                     this step for real).
  activate()      -- the actual submission trigger (Enter / send button).
                     tmux equivalent: tmux.send_keys(session, ["Enter"]).
  prove_accepted() -- decide, from real evidence, whether activation was
                     actually processed. tmux equivalent: AgentAdapter.
                     submit_ack_evidence + the bounded Escape+Enter
                     recovery core.py already performs -- NEVER a bare
                     "did anything change" check.
  observe()       -- best-effort, ongoing status after acceptance
                     (running/idle/turn complete). tmux equivalent:
                     status.py's classify_status / AgentAdapter.
                     identify_target_state.
  cancel()        -- best-effort abort of an in-flight submission, where
                     the transport supports one. tmux has no true cancel
                     today (Escape is only ever used for the one, gated
                     stuck-composer recovery path, never as a general
                     "stop this send" primitive) -- TmuxPromptTransport.
                     cancel() below is honest about that (NotImplementedError).

Golden rule (docs/prompt-submission.md): never resend a prompt's TEXT
past the activation-ambiguity boundary unless there is positive evidence
the first activation was not accepted. Every transport implementing this
protocol must uphold that rule itself -- it is not (and cannot be)
enforced generically here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class SubmissionOrigin:
    """Loop-protection metadata (P11) -- see core.py's terminal_send_text/
    _granted `origin`/`trace_id`/`parent_turn_id`/`depth` kwargs and
    config.py's `max_agent_bridge_depth`. Optional, forward-looking: no
    current caller constructs one of these outside tests. `origin` is
    expected to be one of "chatgpt", "dashboard", "codex", "claude",
    "system" (never enforced as an enum here -- validating call sites, if
    any are ever added, should do that; this dataclass only carries the
    value)."""
    origin: str | None = None
    trace_id: str | None = None
    parent_turn_id: str | None = None
    depth: int = 0

    def child(self, *, origin: str, turn_id: str) -> "SubmissionOrigin":
        """Build the metadata for a submission this one's own processing
        causes (e.g. a Codex session asking a hypothetical future
        ChatGPT-Web bridge a question, which then re-enters a Codex/Claude
        session with the answer) -- depth increments by exactly one, never
        reset, so a caller-side max_agent_bridge_depth check can catch an
        unbounded forwarding chain regardless of how many distinct
        transports are involved along the way."""
        return SubmissionOrigin(origin=origin, trace_id=self.trace_id or turn_id,
                                parent_turn_id=turn_id, depth=self.depth + 1)


@dataclass
class SubmissionReceipt:
    """Transport-agnostic shape for P6's receipt -- a superset of what
    core.py's _enrich_receipt already adds to the tmux path's plain dict
    return value. Exists here as the target shape a future non-tmux
    transport should produce; the tmux path itself keeps returning its
    existing plain dict (see core.py) rather than being refactored to
    construct this class -- that would be a real-code-churn risk for zero
    behavior change, exactly what this upgrade's own stability priority
    argues against."""
    submission_id: str
    state: str  # one of the PROMPT_SUBMISSION_STATES below
    agent_type: str
    session: str
    evidence: list[str] = field(default_factory=list)
    activation_attempts: int = 0
    trace_id: str | None = None
    error: str | None = None
    reason: str | None = None


# Coarse states a caller across ANY transport can render (P1/P6) --
# intentionally fewer/broader than the tmux path's own DELIVERY_STATES
# (adapters.py), which stays the authoritative, precisely-evidenced
# vocabulary for the tmux transport specifically. This is the vocabulary
# a UI (P13: "Sending / Accepted / Running / Failed / Unknown") maps to,
# not a replacement for adapters.DELIVERY_STATES.
PROMPT_SUBMISSION_PREPARING = "PREPARING"
PROMPT_SUBMISSION_WRITING = "WRITING"
PROMPT_SUBMISSION_ACTIVATING = "ACTIVATING"
PROMPT_SUBMISSION_ACCEPTED = "ACCEPTED"
PROMPT_SUBMISSION_RUNNING = "RUNNING"
PROMPT_SUBMISSION_COMPLETED = "COMPLETED"
PROMPT_SUBMISSION_FAILED = "FAILED"
PROMPT_SUBMISSION_UNKNOWN = "PROMPT_SUBMISSION_UNKNOWN"
PROMPT_SUBMISSION_STATES = (
    PROMPT_SUBMISSION_PREPARING, PROMPT_SUBMISSION_WRITING, PROMPT_SUBMISSION_ACTIVATING,
    PROMPT_SUBMISSION_ACCEPTED, PROMPT_SUBMISSION_RUNNING, PROMPT_SUBMISSION_COMPLETED,
    PROMPT_SUBMISSION_FAILED, PROMPT_SUBMISSION_UNKNOWN,
)


@runtime_checkable
class PromptTransport(Protocol):
    """The extension point (P10). A concrete transport (tmux today; a
    ChatGPT-Web browser adapter, hypothetically, later) implements this.
    Nothing in this project currently calls through this Protocol --
    core.py's tmux pipeline is untouched and remains the only live path."""

    def prepare(self, session: str, origin: SubmissionOrigin) -> Any: ...
    def write(self, target: Any, text: str) -> Any: ...
    def verify(self, target: Any, written: Any) -> bool: ...
    def activate(self, target: Any) -> Any: ...
    def prove_accepted(self, target: Any, activation: Any, text: str) -> SubmissionReceipt: ...
    def observe(self, target: Any) -> str: ...
    def cancel(self, target: Any) -> bool: ...


class TmuxPromptTransport:
    """Thin PromptTransport-shaped wrapper around the existing, unchanged
    TerminalService send pipeline -- exists to prove the Protocol above
    actually fits the real, live-tested implementation, NOT to replace
    core.py's own calling convention anywhere. No production code
    constructs or calls this class; core.py's MCP tools and dashboard
    routes keep calling TerminalService.terminal_send_text(_granted)
    directly, exactly as before. Each method here is a documentation-
    grade mapping, not independently re-verified against a real CLI --
    see tests/test_send_reliability.py and tests/test_adapters_real_cli.py
    for that (they exercise the real core.py path, not this wrapper)."""

    def __init__(self, terminal: Any) -> None:
        self._terminal = terminal

    def prepare(self, session: str, origin: SubmissionOrigin) -> str:
        return session  # core.py resolves identity/adapter internally, at write/activate time

    def write(self, target: str, text: str) -> dict[str, Any]:
        return self._terminal.terminal_send_text(target, text, press_enter=False)

    def verify(self, target: str, written: dict[str, Any]) -> bool:
        return bool(written.get("sent"))

    def activate(self, target: str) -> dict[str, Any]:
        raise NotImplementedError(
            "TmuxPromptTransport does not split write/activate into two separate calls -- "
            "core.py's _send_text_and_verify_locked performs both as one atomic, pane-locked "
            "operation (text write, then Enter, then verification) specifically so nothing else "
            "can interleave between them. Call TerminalService.terminal_send_text(..., "
            "press_enter=True) directly instead of this wrapper for a real send."
        )

    def prove_accepted(self, target: str, activation: Any, text: str) -> SubmissionReceipt:
        raise NotImplementedError("see activate() -- use TerminalService.terminal_send_text directly")

    def observe(self, target: str) -> str:
        status = self._terminal.terminal_status(target)
        return status.get("state", PROMPT_SUBMISSION_UNKNOWN)

    def cancel(self, target: str) -> bool:
        # Honest "not supported": Escape is only ever used, internally, as
        # part of the one gated stuck-composer recovery sequence core.py
        # already performs -- there is no general "abort this send"
        # primitive for a tmux target today.
        return False


class ChatGptWebTransport:
    """Extension point stub (P10/P15) -- deliberately unimplemented. See
    docs/chatgpt-web-adapter-plan.md for the design this would follow
    (structural composer selectors, plain-text insertion + read-back
    integrity verification, send-button readiness, submission evidence,
    assistant-turn binding, fail-closed on UI drift) and why it is
    explicitly OUT of scope for this phase: no Playwright/browser-
    automation dependency is introduced into this project by this class.
    Every method raises NotImplementedError."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError(
            "ChatGptWebTransport is a design-time extension point (see "
            "docs/chatgpt-web-adapter-plan.md), not an implemented transport -- "
            "no browser automation dependency exists in this project yet."
        )
