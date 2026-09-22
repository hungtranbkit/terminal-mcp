"""The ChatGPT orchestration policy reaches a real client, and says what it must.

Asserted against the REAL built server (`terminal_mcp.server.mcp`, the same
object `mcp.run()` serves) rather than against the constants in isolation --
the thing that can actually regress is the wiring, not the string literal.
The over-the-wire half (a real stdio/HTTP client reading `instructions` out of
its own `initialize` response) is covered in `tests/test_transports.py`.
"""

from __future__ import annotations

import pathlib

import pytest

from terminal_mcp import orchestration_policy as op
from terminal_mcp.server import mcp
from terminal_mcp.mcp_app import ANALYSIS_GATE_BOOTSTRAP


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_server_metadata_carries_the_orchestration_policy():
    """The built server's `instructions` -- what `initialize` returns -- IS the policy."""
    assert mcp.instructions == op.SERVER_INSTRUCTIONS + "\n\n" + ANALYSIS_GATE_BOOTSTRAP


@pytest.mark.parametrize("phrase", op.CRITICAL_INVARIANTS)
def test_every_critical_invariant_survives(phrase: str):
    """One test per invariant so a failure names the rule that was dropped.

    These are the load-bearing rules of the workflow -- who orchestrates, who
    executes, the bounded retry, the UI skill, the Codex commands and the
    review gate being off, one primary runner, and deploy needing a human.
    Rewriting the prose around them is fine; losing one of them is not.
    """
    assert phrase in (mcp.instructions or "")


def test_invariants_cover_each_required_area():
    """Guard the guard: CRITICAL_INVARIANTS must not shrink to something vacuous.

    Without this, a future edit could delete an awkward invariant AND its
    entry in the tuple, and the parametrized test above would still pass with
    fewer cases. Each area below is checked for at least one representative.
    """
    joined = " ".join(op.CRITICAL_INVARIANTS)
    for required in ("ORCHESTRATOR", "CONTROL PLANE", "PRIMARY CODING EXECUTOR",
                     "execution started", "6 attempts", "UI-UX-Pro-Max", "UI V3",
                     "/codex:review", "/codex:rescue", "review gate stays DISABLED",
                     "one primary Claude executor", "EXPLICIT USER APPROVAL"):
        assert required in joined, f"CRITICAL_INVARIANTS no longer covers {required!r}"


def test_instructions_stay_within_the_client_context_budget():
    """Roughly 500-1200 words: enough to state the workflow, not a manual.

    `instructions` is paid for in client context on every connection, so an
    unbounded policy is a real cost, not a style complaint. The lower bound
    catches the opposite failure -- the policy being gutted back to the old
    two-sentence tool hint.
    """
    words = len(op.SERVER_INSTRUCTIONS.split())
    assert 500 <= words <= 1200, f"policy is {words} words"


def test_tool_descriptions_do_not_carry_the_policy():
    """The policy is stated ONCE, at server level -- not appended to 280+ tools."""
    marker = "PRODUCTION DEPLOYMENT AND RELEASE REMAIN"
    offenders = [tool.name for tool in mcp._tool_manager.list_tools()
                 if marker in (tool.description or "")]
    assert offenders == []


def test_checked_in_doc_matches_its_generator():
    """docs/CHATGPT_ORCHESTRATION_POLICY.md is generated, so it cannot drift.

    Regenerate with `python -m terminal_mcp.orchestration_policy > <that file>`.
    """
    doc = REPO_ROOT / op.POLICY_DOC_PATH
    assert doc.is_file(), f"{op.POLICY_DOC_PATH} is missing"
    assert doc.read_text() == op.policy_document()


def test_doc_embeds_the_wire_text_verbatim():
    """The doc quotes the real string rather than paraphrasing it."""
    document = op.policy_document()
    for line in op.SERVER_INSTRUCTIONS.splitlines():
        if line:
            assert f"> {line}" in document


def test_policy_enables_nothing():
    """This change is text. It must not have flipped an autonomous dispatch flag.

    The queue's auto-dispatch loop is gated by `config.queue.enabled`, and the
    shipped default is OFF. A policy that DESCRIBES a workflow must never be
    the thing that STARTS one -- so the default is asserted here, next to the
    policy, rather than trusted to stay put elsewhere.
    """
    from terminal_mcp.config import QueueConfig

    defaults = QueueConfig()
    assert defaults.enabled is False
    assert defaults.drain_enabled is False
    # And the module itself is inert: constants and pure functions only.
    assert isinstance(op.server_instructions(), str)
    assert isinstance(op.policy_document(), str)
