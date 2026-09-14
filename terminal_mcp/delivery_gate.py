"""PROMPT DELIVERY / ACCEPTANCE GATE -- the one place that decides whether a
prompt was actually delivered.

The rule this module enforces, in full:

  A task is "delivered" ONLY when BOTH hold.
    1. ACTIVATION: the send receipt's own `delivery_state` is
       SUBMIT_CONFIRMED. Not "not an error". Not "not DELIVERY_UNKNOWN".
       Exactly SUBMIT_CONFIRMED.
    2. ACCEPTANCE: a SEPARATE, post-submit observation shows the target
       actually took the prompt -- output moved on past the submit, or the
       target is now composing/running/reading/working, or an adapter
       reports its own accepted-evidence signal.

  Anything short of both is NOT delivered. It must not be reported as
  delivered, and it must not advance a queue task to DISPATCHED/RUNNING.

Why this module exists (audited 2026-09-14, real code, not hypothetical):
every send path made its own decision, and every one of them used a
DENYLIST -- "it's fine unless I recognise a specific failure":

  - queue_engine._dispatch: `if error -> BLOCKED; if delivery_state ==
    "DELIVERY_UNKNOWN" -> DISPATCH_UNCERTAIN;` then falls through to
    `transition_task(RUNNING)` unconditionally. So ANY delivery_state that
    is not that one literal string, present or future, means RUNNING.
  - supervisor2.execute_send: `if submit_status == "SUBMIT_UNCONFIRMED" ->
    blocked;` then falls through to `state='observing'`. Same shape.

Today neither is actively broken -- every BLOCKED/ERROR receipt in core.py
also sets `error`, and TEXT_SENT is only produced when press_enter=False --
so the denylists happen to cover the states that actually occur. That is a
coincidence of the current implementation, not a guarantee: adapters.py
documents DELIVERY_STATES as an extensible set, and the moment a state is
added (or an idempotent replay returns an older receipt shape) both paths
fail OPEN, marking a task RUNNING that was never delivered. A fail-open
default on the question "did the agent get the work?" is the single most
expensive kind of wrong this system can be, because the task then looks
done-and-dusted while nothing is running.

Neither path checked ACCEPTANCE at all. SUBMIT_CONFIRMED means "Enter was
processed and the pane moved past the baseline" -- it does NOT mean the
agent read the prompt and started. This module adds that second check.

Posture:
  - Positive allowlist. `classify_activation` returns CONFIRMED only for
    the one state that means confirmed; everything else is UNCERTAIN or
    REFUSED, including a state this module has never heard of.
  - Pure. Nothing here sends, retries, presses Enter, or mutates a task.
    It reads a receipt plus post-submit observations and returns a verdict.
    Enforcement lives at the call sites; the DECISION lives here, once.
  - Advisory by default. See config.PromptDeliveryConfig: `mode` defaults
    to "advisory", which records the verdict and changes no behaviour at
    all, so this can ship without altering a single existing outcome.
    "enforce" is the operator's explicit opt-in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .adapters import (DELIVERY_BLOCKED, DELIVERY_ERROR, DELIVERY_SUBMIT_CONFIRMED,
                       DELIVERY_TEXT_SENT, DELIVERY_UNKNOWN, TARGET_COMPOSER, TARGET_FINAL,
                       TARGET_RUNNING, TARGET_UNKNOWN, TARGET_WAITING)

# -- verdict kinds -------------------------------------------------------
# DELIVERED     both gates passed. The only kind that may advance a task.
# NOT_ACCEPTED  activation confirmed, acceptance NOT observed. The send
#               really happened -- never retry blindly on this, the prompt
#               may well be sitting in the composer or being read right now.
#               Re-observe; escalate to a human if it persists.
# UNCERTAIN     activation unproven but bytes may have gone out
#               (DELIVERY_UNKNOWN). Never resend without the same
#               idempotency key; inspect terminal_input_context first.
# REFUSED       the send provably did not complete (BLOCKED/ERROR, or no
#               Enter was ever sent). Safe to re-attempt after fixing the
#               cause, still under the same idempotency key.
DELIVERED = "DELIVERED"
NOT_ACCEPTED = "NOT_ACCEPTED"
UNCERTAIN = "UNCERTAIN"
REFUSED = "REFUSED"
VERDICT_KINDS = (DELIVERED, NOT_ACCEPTED, UNCERTAIN, REFUSED)

# Reason codes. Stable strings -- callers, events and audit rows branch on
# these, so they are declared here and asserted in tests.
ACTIVATION_CONFIRMED = "ACTIVATION_CONFIRMED"
ACTIVATION_UNKNOWN = "ACTIVATION_UNKNOWN"
ACTIVATION_REFUSED = "ACTIVATION_REFUSED"
ACTIVATION_TEXT_ONLY = "ACTIVATION_TEXT_ONLY"
ACTIVATION_STATE_UNRECOGNISED = "ACTIVATION_STATE_UNRECOGNISED"
ACTIVATION_MISSING = "ACTIVATION_MISSING"
SEND_ERROR = "SEND_ERROR"

ACCEPTANCE_OUTPUT_ADVANCED = "ACCEPTANCE_OUTPUT_ADVANCED"
ACCEPTANCE_TARGET_WORKING = "ACCEPTANCE_TARGET_WORKING"
ACCEPTANCE_ADAPTER_ACK = "ACCEPTANCE_ADAPTER_ACK"
ACCEPTANCE_NOT_OBSERVED = "ACCEPTANCE_NOT_OBSERVED"
ACCEPTANCE_PROMPT_STILL_IN_COMPOSER = "ACCEPTANCE_PROMPT_STILL_IN_COMPOSER"
ACCEPTANCE_TARGET_AWAITING_HUMAN = "ACCEPTANCE_TARGET_AWAITING_HUMAN"
ACCEPTANCE_NOT_CHECKED = "ACCEPTANCE_NOT_CHECKED"
ACCEPTANCE_UNOBSERVABLE = "ACCEPTANCE_UNOBSERVABLE"

# Target states that are positive acceptance on their own: the agent is
# demonstrably doing something with the prompt.
_WORKING_TARGET_STATES = (TARGET_RUNNING,)


@dataclass(frozen=True)
class DeliveryVerdict:
    """The whole decision, with the evidence that produced it.

    `may_advance` is the ONLY field a caller should branch on to decide
    whether to move a task forward -- deriving that from `kind` at each
    call site is exactly the duplicated-policy mistake this module exists
    to remove."""

    kind: str
    activation: str          # the activation reason code
    acceptance: str          # the acceptance reason code
    evidence: tuple[str, ...] = ()
    detail: str | None = None
    delivery_state: str | None = None
    submission_id: str | None = None

    @property
    def may_advance(self) -> bool:
        return self.kind == DELIVERED

    @property
    def safe_to_retry(self) -> bool:
        """REFUSED only. UNCERTAIN is deliberately NOT retry-safe without a
        human/inspection step: the bytes may already have landed, and a
        blind resend is how a prompt gets delivered twice. NOT_ACCEPTED is
        never retry-safe -- the submit is confirmed, so resending would
        duplicate it."""
        return self.kind == REFUSED

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "activation": self.activation, "acceptance": self.acceptance,
                "evidence": list(self.evidence), "detail": self.detail,
                "delivery_state": self.delivery_state, "submission_id": self.submission_id,
                "may_advance": self.may_advance, "safe_to_retry": self.safe_to_retry}


def classify_activation(send_result: dict[str, Any]) -> tuple[str, str, str | None]:
    """Gate 1. -> (kind, reason_code, detail).

    A POSITIVE allowlist: only DELIVERY_SUBMIT_CONFIRMED confirms. Every
    other value -- including one this module does not recognise -- degrades
    to UNCERTAIN or REFUSED. This is the inversion of the denylists audited
    in queue_engine/supervisor2: a new delivery state added to adapters.py
    can no longer silently mean "delivered"."""
    if not isinstance(send_result, dict):
        return REFUSED, ACTIVATION_MISSING, "no send receipt"
    error = send_result.get("error")
    state = send_result.get("delivery_state")

    if state is None:
        # A receipt with no delivery_state at all: an old/partial shape, or
        # a non-send response. Never assume the best.
        return (REFUSED if error else UNCERTAIN), ACTIVATION_MISSING, (
            f"receipt carries no delivery_state (error={error!r})")

    if state == DELIVERY_SUBMIT_CONFIRMED:
        # An error alongside SUBMIT_CONFIRMED should not happen; if it ever
        # does, the error wins -- fail closed.
        if error:
            return UNCERTAIN, SEND_ERROR, f"SUBMIT_CONFIRMED alongside error={error!r}"
        return DELIVERED, ACTIVATION_CONFIRMED, None
    if state == DELIVERY_UNKNOWN:
        return UNCERTAIN, ACTIVATION_UNKNOWN, (
            "Enter was written but no evidence confirms the target processed it -- "
            "inspect terminal_input_context; never resend without the same idempotency key")
    if state == DELIVERY_TEXT_SENT:
        # Text landed, Enter was never sent. With press_enter=True this
        # means the prompt is sitting unsubmitted in the input box.
        if send_result.get("press_enter"):
            return REFUSED, ACTIVATION_TEXT_ONLY, (
                "text was written but Enter was never sent -- the prompt is most likely "
                "still in the target's input box, unsubmitted")
        return REFUSED, ACTIVATION_TEXT_ONLY, "press_enter was False -- nothing was submitted"
    if state in (DELIVERY_BLOCKED, DELIVERY_ERROR):
        return REFUSED, ACTIVATION_REFUSED, f"delivery_state={state} error={error!r}"
    return UNCERTAIN, ACTIVATION_STATE_UNRECOGNISED, (
        f"unrecognised delivery_state {state!r} -- treated as unproven rather than delivered")


def classify_acceptance(*, before_lines: list[str] | None, after_lines: list[str] | None,
                        target_state: str | None = None, adapter: Any = None,
                        sent_text: str | None = None) -> tuple[bool, str, tuple[str, ...]]:
    """Gate 2. -> (accepted, reason_code, evidence_codes).

    Positive acceptance requires one of:
      - the target reports a working state (running),
      - the adapter's own submit_ack_evidence fires against before/after,
      - output genuinely advanced past the post-submit baseline.

    Explicitly NOT acceptance:
      - the composer still holding the prompt (the exact failure this whole
        gate exists to catch),
      - the target waiting on a human (it has not started our work),
      - no observation available at all -- that is UNOBSERVABLE, and
        unobservable is never a pass.
    """
    evidence: list[str] = []
    if after_lines is None:
        return False, ACCEPTANCE_UNOBSERVABLE, ()

    if target_state == TARGET_WAITING:
        return False, ACCEPTANCE_TARGET_AWAITING_HUMAN, ()
    if target_state in _WORKING_TARGET_STATES:
        evidence.append(ACCEPTANCE_TARGET_WORKING)

    if adapter is not None and before_lines is not None:
        try:
            if adapter.submit_ack_evidence(before_lines, after_lines, sent_text or ""):
                evidence.append(ACCEPTANCE_ADAPTER_ACK)
        except Exception:  # noqa: BLE001 -- an adapter must never break the gate
            pass

    if before_lines is not None and before_lines != after_lines:
        evidence.append(ACCEPTANCE_OUTPUT_ADVANCED)

    if evidence:
        return True, (ACCEPTANCE_TARGET_WORKING if ACCEPTANCE_TARGET_WORKING in evidence
                      else evidence[0]), tuple(evidence)

    # Nothing positive. Distinguish the diagnostic case a human most needs
    # to see -- the prompt still sitting in the composer, unsubmitted.
    if target_state == TARGET_COMPOSER:
        return False, ACCEPTANCE_PROMPT_STILL_IN_COMPOSER, ()
    return False, ACCEPTANCE_NOT_OBSERVED, ()


def evaluate(send_result: dict[str, Any], *, before_lines: list[str] | None = None,
             after_lines: list[str] | None = None, target_state: str | None = None,
             adapter: Any = None, sent_text: str | None = None,
             require_acceptance: bool = True) -> DeliveryVerdict:
    """The single entry point. Activation first; acceptance only if
    activation confirmed (there is nothing to accept otherwise).

    `require_acceptance=False` reproduces the pre-gate behaviour exactly
    (activation alone decides) -- used by the advisory path and by callers
    that genuinely cannot observe the target."""
    kind, activation, detail = classify_activation(send_result)
    submission_id = None
    if isinstance(send_result, dict):
        submission_id = send_result.get("submission_id") or send_result.get("correlation_id")
    delivery_state = send_result.get("delivery_state") if isinstance(send_result, dict) else None

    if kind != DELIVERED:
        return DeliveryVerdict(kind=kind, activation=activation, acceptance=ACCEPTANCE_NOT_CHECKED,
                               evidence=(), detail=detail, delivery_state=delivery_state,
                               submission_id=submission_id)

    if not require_acceptance:
        return DeliveryVerdict(kind=DELIVERED, activation=activation,
                               acceptance=ACCEPTANCE_NOT_CHECKED, evidence=(ACTIVATION_CONFIRMED,),
                               detail="acceptance not required by policy",
                               delivery_state=delivery_state, submission_id=submission_id)

    accepted, acceptance, evidence = classify_acceptance(
        before_lines=before_lines, after_lines=after_lines, target_state=target_state,
        adapter=adapter, sent_text=sent_text)
    if accepted:
        return DeliveryVerdict(kind=DELIVERED, activation=activation, acceptance=acceptance,
                               evidence=(ACTIVATION_CONFIRMED, *evidence), detail=None,
                               delivery_state=delivery_state, submission_id=submission_id)
    return DeliveryVerdict(
        kind=NOT_ACCEPTED, activation=activation, acceptance=acceptance,
        evidence=(ACTIVATION_CONFIRMED,),
        detail=("submit was confirmed but no post-submit evidence shows the target took the "
                "prompt -- re-observe before doing anything; NEVER resend (the submit is "
                "confirmed, a resend would duplicate it)"),
        delivery_state=delivery_state, submission_id=submission_id)
