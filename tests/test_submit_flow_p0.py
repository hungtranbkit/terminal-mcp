"""The reported P0, reproduced against a simulated pane.

`test_the_reported_bug_key_only_enter_on_claude_does_not_transition` is the
bug as observed on 2026-09-14: a visible non-empty Claude prompt, one Enter,
pane byte-identical afterwards. Before this module that answered
DELIVERY_UNKNOWN, which a caller cannot act on. It must now be SUBMIT_STALLED
and it must never be reported as sent or accepted.
"""
from __future__ import annotations

import pytest

from terminal_mcp import submit_flow as sf
from terminal_mcp.adapters import select_adapter

CLAUDE = select_adapter("claude")
CODEX = select_adapter("codex")
SHELL = select_adapter("bash")

PANE_ID = "$1:%1"

# A real Claude composer with a prompt sitting in it, submitted by nobody.
PROMPT = "merge feat/x into main and deploy"
IDLE_WITH_PROMPT = [
    "  ⎿  read 3 files",
    "",
    "────────────────────────────────",
    f"> {PROMPT}",
    "────────────────────────────────",
    "  ⏵⏵ auto mode on",
]
# Same pane after a genuine submit: composer emptied.
AFTER_CLEARED = [
    "  ⎿  read 3 files",
    f"  {PROMPT}",
    "────────────────────────────────",
    ">",
    "────────────────────────────────",
]
AFTER_BUSY = IDLE_WITH_PROMPT[:-1] + ["✻ Thinking… (3s · esc to interrupt)"]
# The real approval shape this project's own fixture prints
# (tests/fixtures/waiting_prompt.py), which is what _WAITING_PATTERNS was
# written against -- not a dialog invented for this test.
AFTER_PERMISSION = IDLE_WITH_PROMPT[:-1] + [
    "Allow this command to run?",
    "approve or deny? [y/n] ",
]


# -- the reported bug --------------------------------------------------------

def test_the_reported_bug_key_only_enter_on_claude_does_not_transition():
    """RED before this module: identical pane answered DELIVERY_UNKNOWN.

    Now it is SUBMIT_STALLED -- a specific claim the caller can act on.
    """
    verdict = sf.prove_accepted(before=IDLE_WITH_PROMPT, after=IDLE_WITH_PROMPT,
                                adapter=CLAUDE, composer_text=PROMPT)
    assert verdict.outcome == sf.STALLED
    assert verdict.outcome != sf.ACCEPTED
    assert verdict.evidence["pane_changed"] is False
    assert "still in the composer" in verdict.reason


def test_a_stalled_submission_is_never_reported_as_sent_or_success():
    verdict = sf.prove_accepted(before=IDLE_WITH_PROMPT, after=IDLE_WITH_PROMPT,
                                adapter=CLAUDE, composer_text=PROMPT)
    payload = verdict.to_dict()
    assert payload["outcome"] == sf.STALLED
    for forbidden in ("SENT", "SUCCESS", "SUBMIT_CONFIRMED"):
        assert forbidden not in str(payload["outcome"])


def test_stalled_is_distinct_from_unknown():
    """They mean different things and a caller retries on only one of them."""
    from terminal_mcp.adapters import DELIVERY_UNKNOWN
    assert sf.STALLED != DELIVERY_UNKNOWN


# -- claude gets an activation nudge, codex does not ------------------------

def test_claude_gets_an_activation_nudge_before_the_single_enter():
    plan = sf.plan_submit(snapshot_a=IDLE_WITH_PROMPT, snapshot_b=IDLE_WITH_PROMPT,
                          adapter=CLAUDE, pane_identity=PANE_ID)
    assert plan.send_activation is True
    assert plan.send_enter is True
    assert plan.composer_text == PROMPT


def test_codex_behaviour_is_untouched():
    """The Claude bug must not change Codex's Enter handling."""
    plan = sf.plan_submit(snapshot_a=IDLE_WITH_PROMPT, snapshot_b=IDLE_WITH_PROMPT,
                          adapter=CODEX, pane_identity=PANE_ID)
    assert plan.send_activation is False
    assert plan.send_enter is True
    assert "codex" not in sf.ACTIVATION_ADAPTERS


def test_a_plain_shell_gets_no_nudge():
    plan = sf.plan_submit(snapshot_a=IDLE_WITH_PROMPT, snapshot_b=IDLE_WITH_PROMPT,
                          adapter=SHELL, pane_identity=PANE_ID)
    assert plan.send_activation is False


def test_the_nudge_cannot_change_composer_content():
    """A cursor move is the nudge precisely because it cannot duplicate or
    corrupt the prompt about to be submitted."""
    assert sf.ACTIVATION_KEY in ("Right", "Left", "End", "Home")


def test_exactly_one_enter_is_ever_planned():
    """Two Enters submit twice. There is no plan that asks for more than one."""
    plan = sf.plan_submit(snapshot_a=IDLE_WITH_PROMPT, snapshot_b=IDLE_WITH_PROMPT,
                          adapter=CLAUDE, pane_identity=PANE_ID)
    assert plan.send_enter is True
    assert isinstance(plan.send_enter, bool), "send_enter is a flag, never a count"


# -- VERIFY_TYPED ------------------------------------------------------------

def test_an_empty_composer_is_refused_before_any_key_is_sent():
    empty = ["────────", ">", "────────"]
    plan = sf.plan_submit(snapshot_a=empty, snapshot_b=empty,
                          adapter=CLAUDE, pane_identity=PANE_ID)
    assert plan.outcome == sf.NOTHING_TO_SUBMIT
    assert plan.send_enter is False and plan.send_activation is False


def test_a_composer_still_being_typed_into_is_refused():
    a = [f"> {PROMPT}"]
    b = [f"> {PROMPT} and also this"]
    plan = sf.plan_submit(snapshot_a=a, snapshot_b=b, adapter=CLAUDE, pane_identity=PANE_ID)
    assert plan.outcome == sf.COMPOSER_UNSTABLE
    assert plan.send_enter is False


def test_a_busy_target_is_not_submitted_into():
    plan = sf.plan_submit(snapshot_a=AFTER_BUSY, snapshot_b=AFTER_BUSY,
                          adapter=CLAUDE, pane_identity=PANE_ID)
    assert plan.outcome == sf.REFUSED_BUSY
    assert plan.send_enter is False


# -- PROVE_ACCEPTED evidence -------------------------------------------------

def test_a_cleared_composer_is_acceptance():
    verdict = sf.prove_accepted(before=IDLE_WITH_PROMPT, after=AFTER_CLEARED,
                                adapter=CLAUDE, composer_text=PROMPT)
    assert verdict.outcome == sf.ACCEPTED
    assert verdict.evidence["composer_cleared"] is True


def test_a_busy_transition_is_acceptance():
    verdict = sf.prove_accepted(before=IDLE_WITH_PROMPT, after=AFTER_BUSY,
                                adapter=CLAUDE, composer_text=PROMPT)
    assert verdict.outcome == sf.ACCEPTED
    assert verdict.evidence["busy"] is True


def test_a_permission_dialog_is_acceptance():
    """The target could only be asking because the prompt landed."""
    verdict = sf.prove_accepted(before=IDLE_WITH_PROMPT, after=AFTER_PERMISSION,
                                adapter=CLAUDE, composer_text=PROMPT)
    assert verdict.outcome == sf.ACCEPTED
    assert verdict.evidence["permission_dialog"] is True


def test_every_acceptance_names_its_evidence():
    for after in (AFTER_CLEARED, AFTER_BUSY, AFTER_PERMISSION):
        verdict = sf.prove_accepted(before=IDLE_WITH_PROMPT, after=after,
                                    adapter=CLAUDE, composer_text=PROMPT)
        assert verdict.reason.startswith("accepted: ") and len(verdict.reason) > 12


# -- idempotent retry --------------------------------------------------------

def test_a_retry_of_an_accepted_submission_is_a_no_op():
    plan = sf.plan_submit(snapshot_a=IDLE_WITH_PROMPT, snapshot_b=IDLE_WITH_PROMPT,
                          adapter=CLAUDE, pane_identity=PANE_ID)
    accepted = {"submission_id": plan.submission_id, "accepted_at": "2026-09-14T00:00:00Z"}

    replay = sf.plan_submit(snapshot_a=IDLE_WITH_PROMPT, snapshot_b=IDLE_WITH_PROMPT,
                            adapter=CLAUDE, pane_identity=PANE_ID,
                            already_accepted=accepted)
    assert replay.outcome == sf.ALREADY_SUBMITTED
    assert replay.send_enter is False and replay.send_activation is False


def test_the_submission_id_is_stable_across_retries():
    a = sf.submission_id(pane_identity=PANE_ID, composer_text=PROMPT)
    b = sf.submission_id(pane_identity=PANE_ID, composer_text=f"  {PROMPT}  ")
    assert a == b, "a retry must derive the same id without having kept anything"


def test_a_different_prompt_is_a_different_submission():
    a = sf.submission_id(pane_identity=PANE_ID, composer_text=PROMPT)
    b = sf.submission_id(pane_identity=PANE_ID, composer_text=PROMPT + "!")
    assert a != b


def test_a_different_pane_is_a_different_submission():
    a = sf.submission_id(pane_identity="$1:%1", composer_text=PROMPT)
    b = sf.submission_id(pane_identity="$2:%7", composer_text=PROMPT)
    assert a != b


# -- retry safety ------------------------------------------------------------

def test_a_stalled_attempt_may_retry_when_nothing_changed():
    previous = {"outcome": sf.STALLED, "composer_text": PROMPT}
    ok, why = sf.may_retry(previous, current_composer_text=PROMPT,
                           adapter=CLAUDE, snapshot=IDLE_WITH_PROMPT)
    assert ok is True and "unchanged" in why


def test_an_accepted_attempt_may_never_retry():
    previous = {"outcome": sf.ACCEPTED, "composer_text": PROMPT}
    ok, why = sf.may_retry(previous, current_composer_text=PROMPT,
                           adapter=CLAUDE, snapshot=IDLE_WITH_PROMPT)
    assert ok is False and "submit twice" in why


def test_a_stalled_attempt_may_not_retry_once_the_target_started():
    """It stalled, then acted. Retrying now is the duplicate submission."""
    previous = {"outcome": sf.STALLED, "composer_text": PROMPT}
    ok, why = sf.may_retry(previous, current_composer_text=PROMPT,
                           adapter=CLAUDE, snapshot=AFTER_BUSY)
    assert ok is False and "started working" in why


def test_a_stalled_attempt_may_not_retry_if_the_prompt_changed():
    previous = {"outcome": sf.STALLED, "composer_text": PROMPT}
    ok, why = sf.may_retry(previous, current_composer_text="something else",
                           adapter=CLAUDE, snapshot=IDLE_WITH_PROMPT)
    assert ok is False and "no longer holds" in why


# -- shape -------------------------------------------------------------------

def test_the_stage_vocabulary_is_the_specified_one():
    assert sf.STAGES == ("PREPARE", "VERIFY_TYPED", "SUBMIT_ONCE", "PROVE_ACCEPTED")


def test_plans_are_json_serialisable():
    import json
    json.dumps(sf.plan_submit(snapshot_a=IDLE_WITH_PROMPT, snapshot_b=IDLE_WITH_PROMPT,
                              adapter=CLAUDE, pane_identity=PANE_ID).to_dict())
    json.dumps(sf.prove_accepted(before=IDLE_WITH_PROMPT, after=IDLE_WITH_PROMPT,
                                 adapter=CLAUDE, composer_text=PROMPT).to_dict())
