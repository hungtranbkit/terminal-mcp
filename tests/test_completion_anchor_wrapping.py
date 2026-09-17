"""A wrapped instruction must not be read back as worker evidence.

Live failure this covers, 2026-09-14: three tasks were marked COMPLETED
within ~20 seconds of dispatch while their worktrees were untouched and the
workers were still mid-run. The engine had accepted its OWN dispatched
template as the worker's completion marker.

The earlier anti-echo fix anchored on the instruction sentence as a single
line. A terminal wraps at its pane width, so the sentence arrives split
across lines with indentation, `rfind` found nothing, the anchor failed, and
the code fell back to searching the whole pane -- where our own template sits.
"""

from __future__ import annotations

import pytest

from terminal_mcp.queue_engine import (
    COMPLETION_INSTRUCTION_SENTENCE, worker_output_after_prompt,
)
from terminal_mcp.status import parse_completion_marker

MARKER = ("###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 "
          "task_id=t1 attempt=1 nonce=n1 status=completion_candidate "
          "summary_sha256=abc123###")


def _accepted(pane: str) -> bool:
    return parse_completion_marker(worker_output_after_prompt(pane)) is not None


def test_the_unwrapped_instruction_alone_is_not_evidence():
    assert _accepted(f"{COMPLETION_INSTRUCTION_SENTENCE}\n{MARKER}\n") is False


def test_a_wrapped_instruction_alone_is_not_evidence():
    """The exact shape a real pane produces."""
    pane = ("  When (and only when) the above task is FULLY complete, print exactly one line\n"
            "  in this exact format (once), then stop:\n"
            f"  {MARKER}\n"
            "  I'll start by reading the requirements doc.\n")
    assert _accepted(pane) is False


def test_a_hard_wrapped_instruction_is_not_evidence():
    """Wrapped mid-phrase, as a narrow pane would."""
    pane = ("When (and only when) the above task is\n"
            "FULLY complete, print exactly\n"
            "one line in this exact format\n"
            "(once), then stop:\n"
            f"{MARKER}\n")
    assert _accepted(pane) is False


def test_an_indented_and_double_spaced_instruction_is_not_evidence():
    pane = ("\tWhen  (and   only  when)   the above task is FULLY complete,  print exactly\n"
            "\t\tone line in this exact format (once),  then stop:\n"
            f"\t{MARKER}\n")
    assert _accepted(pane) is False


def test_a_real_worker_marker_after_a_wrapped_instruction_is_accepted():
    pane = ("  When (and only when) the above task is FULLY complete, print exactly one line\n"
            "  in this exact format (once), then stop:\n"
            f"  {MARKER}\n"
            "  ...did the work, ran the tests...\n"
            f"{MARKER}\n")
    assert _accepted(pane) is True


def test_a_lone_marker_with_no_instruction_is_treated_as_the_workers():
    """A deliberate trade-off, recorded so it is a decision and not an accident.

    A marker with no instruction text before it is accepted as the worker's.
    Rejecting it was tried first and is wrong in practice: a long run scrolls
    the instruction out of the capture window, so genuine completions would
    sit in VERIFYING forever waiting for a human.

    The residual risk is narrow -- a window that cuts exactly between the
    instruction sentence and the template it introduces would still be
    misread. It is accepted because the common failure this replaced was the
    opposite and far worse: the instruction fully visible, our own template
    read back as evidence, three tasks COMPLETED against untouched worktrees.
    """
    assert _accepted(f"{MARKER}\n") is True


def test_our_template_is_recognised_by_what_precedes_it_not_by_position():
    """Even with worker chatter after it, our template is still ours."""
    pane = ("  When (and only when) the above task is FULLY complete, print exactly one line\n"
            "  in this exact format (once), then stop:\n"
            f"  {MARKER}\n"
            "  Reading the requirements. Ran 17 shell commands.\n")
    assert _accepted(pane) is False


def test_empty_output_is_not_evidence():
    assert _accepted("") is False


@pytest.mark.parametrize("filler", ["", "\n" * 50, "noise\n" * 30])
def test_worker_text_between_instruction_and_marker_still_verifies(filler):
    pane = ("  When (and only when) the above task is FULLY complete, print exactly one line\n"
            "  in this exact format (once), then stop:\n"
            f"  {MARKER}\n" + filler + f"{MARKER}\n")
    assert _accepted(pane) is True
