"""Submitting a prompt that is ALREADY sitting in an agent's composer.

THE FAILURE THIS EXISTS FOR

Observed repeatedly on 2026-09-14 against real Claude Code sessions (wtest,
win1, and local m1):

  terminal_input_context           -> effective_input=true
  terminal_send_keys(["Enter"])    -> pane byte-identical, SUBMIT_UNCONFIRMED
  terminal_send_text(full prompt, press_enter=True) -> SUBMIT_CONFIRMED

So the Enter keystroke reaches the pty -- `sent: True` was never a lie -- and
Claude Code does not act on it. A fresh text+Enter through the composition
path does. The consequence for a caller is worse than a failed submit: a
finished task sits with its prompt visible and nobody can tell whether it was
submitted, because the only honest answer available was `DELIVERY_UNKNOWN`,
which is also what a genuinely-ambiguous send returns.

WHAT CHANGES

Key-only Enter on Claude gets an activation nudge before the single Enter, and
the outcome is decided by a four-stage machine whose unproven terminal state is
`SUBMIT_STALLED` -- a stable, specific answer meaning "the prompt is still
sitting there and nothing started", distinct from `DELIVERY_UNKNOWN`'s "bytes
went out, outcome unproven". A caller can retry a STALLED submission safely;
that is the whole point of separating them.

  PREPARE        read the pane, pick the adapter, extract composer text
  VERIFY_TYPED   prove there IS a non-empty prompt to submit, and that it is
                 stable across two reads -- submitting into a composer that is
                 still being written would race the writer
  SUBMIT_ONCE    activation nudge (Claude only), then EXACTLY one Enter
  PROVE_ACCEPTED require real evidence: composer cleared, or busy/thinking, or
                 a permission dialog, or genuine output progress

NEVER MULTIPLE ENTERS, NEVER RETYPED TEXT

Two Enters can submit twice. Retyping the prompt duplicates it. The nudge is a
cursor movement that cannot alter composer content, chosen precisely because it
forces the TUI to process input and re-render without changing what will be
submitted. Codex is untouched: it has its own Enter behaviour, it was never the
reported failure, and widening this to it would be a change nobody asked for
and nobody tested.

PURE

No tmux, no I/O. The caller supplies snapshots and performs the sends this
module tells it to. That is what lets the reported failure be reproduced in a
test against a simulated pane rather than only against a live Windows machine.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from . import composer
from .adapters import (
    TARGET_RUNNING, TARGET_WAITING, AgentAdapter,
)

# -- stages ------------------------------------------------------------------
PREPARE = "PREPARE"
VERIFY_TYPED = "VERIFY_TYPED"
SUBMIT_ONCE = "SUBMIT_ONCE"
PROVE_ACCEPTED = "PROVE_ACCEPTED"
STAGES = (PREPARE, VERIFY_TYPED, SUBMIT_ONCE, PROVE_ACCEPTED)

# -- outcomes ----------------------------------------------------------------
ACCEPTED = "SUBMIT_ACCEPTED"
STALLED = "SUBMIT_STALLED"
NOTHING_TO_SUBMIT = "NOTHING_TO_SUBMIT"
COMPOSER_UNSTABLE = "COMPOSER_UNSTABLE"
ALREADY_SUBMITTED = "ALREADY_SUBMITTED"
REFUSED_BUSY = "REFUSED_BUSY"

# Adapters that need the composer woken before a bare Enter will be acted on.
# Claude only, deliberately: this is the one where the failure was observed,
# and Codex's own Enter handling is not being changed by a Claude bug.
ACTIVATION_ADAPTERS = frozenset({"claude"})

# The nudge. A cursor move cannot change what is in the composer, so it cannot
# duplicate or corrupt the prompt about to be submitted -- which is the whole
# reason it, and not a space/backspace pair or a re-typed prompt, is the nudge.
ACTIVATION_KEY = "Right"


def submission_id(*, pane_identity: str, composer_text: str) -> str:
    """Stable across retries of the SAME prompt in the SAME pane.

    Derived rather than random on purpose: a caller that lost its response and
    retries must land on the same id without having kept anything, which is
    what makes the retry a no-op instead of a second submission.
    """
    digest = hashlib.sha256()
    digest.update(pane_identity.encode("utf-8", "replace"))
    digest.update(b"\x00")
    digest.update(composer_text.strip().encode("utf-8", "replace"))
    return digest.hexdigest()[:32]


@dataclass(frozen=True)
class Plan:
    """What the caller should do next, and why. Never performs it."""
    stage: str
    outcome: str | None = None
    send_activation: bool = False
    send_enter: bool = False
    composer_text: str = ""
    submission_id: str | None = None
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.outcome is not None

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "outcome": self.outcome,
                "send_activation": self.send_activation, "send_enter": self.send_enter,
                "submission_id": self.submission_id, "reason": self.reason,
                "evidence": dict(self.evidence)}


def extract_composer_text(snapshot: Sequence[str]) -> str:
    """The prompt currently sitting in the composer, best effort.

    P0 2026-09-15: this WAS a second implementation that "mirrored"
    core._extract_composer_text. It already knew to look for the composer
    LINE rather than the last non-empty one -- and it was still wrong on a
    real pane, because it required an ASCII space after the marker and real
    Claude Code puts U+00A0 there. So it matched no marker at all, fell
    through to its own last-non-empty-line fallback, and returned the
    footer anyway: the identical wrong answer it existed to prevent. Both
    readers now delegate to the single one in composer.py, which normalises
    Unicode space separators and skips known chrome. See that module's
    docstring for the captured pane.
    """
    return composer.extract(snapshot)


def plan_submit(*, snapshot_a: Sequence[str], snapshot_b: Sequence[str],
                adapter: AgentAdapter, pane_identity: str,
                already_accepted: Mapping[str, Any] | None = None) -> Plan:
    """PREPARE -> VERIFY_TYPED -> SUBMIT_ONCE, as one decision.

    Two snapshots, not one: a composer still being written to is a composer
    whose content would change between the read and the Enter, and submitting
    a half-typed prompt is worse than refusing.
    """
    text_a = extract_composer_text(snapshot_a)
    text_b = extract_composer_text(snapshot_b)

    if not text_b:
        return Plan(VERIFY_TYPED, outcome=NOTHING_TO_SUBMIT,
                    reason="the composer is empty; there is no prompt to submit")
    if text_a != text_b:
        return Plan(VERIFY_TYPED, outcome=COMPOSER_UNSTABLE, composer_text=text_b,
                    reason="composer content changed between two reads; something is "
                           "still typing into it")

    sid = submission_id(pane_identity=pane_identity, composer_text=text_b)

    # Idempotency. A retry of an accepted submission must do nothing at all --
    # not send, not nudge, not report success as though it had just happened.
    if already_accepted and already_accepted.get("submission_id") == sid:
        return Plan(PROVE_ACCEPTED, outcome=ALREADY_SUBMITTED, composer_text=text_b,
                    submission_id=sid,
                    reason="this exact prompt was already submitted from this pane; "
                           "no second Enter was sent",
                    evidence={"accepted_at": already_accepted.get("accepted_at")})

    # Refuse to submit into a target that is mid-turn. The adapter already owns
    # this rule; asking it keeps one definition of "busy".
    if not adapter.can_submit_now(list(snapshot_b)):
        return Plan(SUBMIT_ONCE, outcome=REFUSED_BUSY, composer_text=text_b,
                    submission_id=sid,
                    reason="the target is mid-turn; submitting now would race it")

    return Plan(SUBMIT_ONCE, composer_text=text_b, submission_id=sid,
                send_activation=adapter.name in ACTIVATION_ADAPTERS,
                send_enter=True,
                reason=("activation nudge then one Enter"
                        if adapter.name in ACTIVATION_ADAPTERS else "one Enter"))


def prove_accepted(*, before: Sequence[str], after: Sequence[str],
                   adapter: AgentAdapter, composer_text: str,
                   submission_id_value: str | None = None,
                   consider_adapter_ack: bool = True) -> Plan:
    """PROVE_ACCEPTED. Evidence or STALLED -- never SENT, never SUCCESS.

    Four independent kinds of evidence, any one of which is enough:

      composer cleared      the prompt is gone from the composer
      busy / thinking       the target entered a working state
      permission dialog     the target is asking the user something, which it
                            could only be doing because the prompt landed
      adapter ack           the adapter's own evidence rule is satisfied

    Absence of all four is `SUBMIT_STALLED`: a specific, stable claim that the
    prompt is still sitting there and nothing started. It is not
    `DELIVERY_UNKNOWN` -- that means "bytes went out, outcome unproven", and
    conflating the two is what left a caller unable to decide whether retrying
    was safe.
    """
    before_lines, after_lines = list(before or ()), list(after or ())
    after_text = extract_composer_text(after_lines)

    cleared = bool(composer_text) and after_text != composer_text
    state = adapter.identify_target_state(after_lines)
    busy = state == TARGET_RUNNING
    waiting = state == TARGET_WAITING
    # `consider_adapter_ack=False` means the caller ALREADY evaluated this
    # predicate properly -- core's `_poll_for_ack_evidence` watches intermediate
    # frames against the busy-window rule and a deadline, and reached "no". A
    # fresh single-shot call here on the FINAL snapshots is a weaker test of the
    # same question, and re-running it silently overturned that verdict: the
    # `never_echoes` fixture, built to never acknowledge anything, came back
    # SUBMIT_CONFIRMED. A second opinion from a worse instrument is not
    # evidence, and this is the direction that must never be wrong.
    acked = (adapter.submit_ack_evidence(before_lines, after_lines, composer_text)
             if consider_adapter_ack else False)

    evidence = {"composer_cleared": cleared, "target_state": state,
                "busy": busy, "permission_dialog": waiting,
                "adapter_ack": acked,
                "pane_changed": after_lines != before_lines}

    if cleared or busy or waiting or acked:
        return Plan(PROVE_ACCEPTED, outcome=ACCEPTED, composer_text=composer_text,
                    submission_id=submission_id_value, evidence=evidence,
                    reason=_accepted_reason(cleared, busy, waiting, acked))

    return Plan(PROVE_ACCEPTED, outcome=STALLED, composer_text=composer_text,
                submission_id=submission_id_value, evidence=evidence,
                reason="the prompt is still in the composer and nothing started: "
                       "no clear, no busy state, no permission dialog, no adapter ack")


def _accepted_reason(cleared: bool, busy: bool, waiting: bool, acked: bool) -> str:
    reasons = []
    if cleared:
        reasons.append("composer cleared")
    if busy:
        reasons.append("target entered a working state")
    if waiting:
        reasons.append("target is showing a permission/confirmation dialog")
    if acked:
        reasons.append("adapter ack evidence")
    return "accepted: " + ", ".join(reasons)


def may_retry(previous: Mapping[str, Any] | None, *, current_composer_text: str,
              adapter: AgentAdapter, snapshot: Sequence[str]) -> tuple[bool, str]:
    """Is retrying a previously-STALLED submission safe right now?

    Only when the original prompt is demonstrably still there and nothing has
    started. Retrying against a target that did eventually act is how one
    submission becomes two.
    """
    if not previous:
        return True, "no previous attempt recorded"
    if previous.get("outcome") == ACCEPTED:
        return False, "the previous attempt was accepted; a retry would submit twice"
    if previous.get("outcome") != STALLED:
        return False, f"previous outcome {previous.get('outcome')!r} is not retryable"
    if previous.get("composer_text") != current_composer_text:
        return False, "the composer no longer holds the prompt that stalled"
    if adapter.identify_target_state(list(snapshot)) == TARGET_RUNNING:
        return False, "the target started working after all; retrying would submit twice"
    return True, "the prompt is unchanged and nothing started"
