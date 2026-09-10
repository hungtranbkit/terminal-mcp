"""P0 regression coverage for the Claude submit/Enter mechanism
(2026-09-11).

LIVE ROOT CAUSE this file pins down (hp-linux node, real Claude Code
2.1.267, sessions hp1/hp2 -- full write-up in terminal_mcp/composer.py):
Claude renders a DIM ghost suggestion inside an EMPTY composer.
`tmux capture-pane -p` strips SGR, so `> yes, publish the report` on the
pane was indistinguishable from a genuinely pending prompt. An operator/
orchestrator therefore sent a bare Enter, which reached an empty composer,
did nothing, wrote zero bytes, and came back SUBMIT_UNCONFIRMED -- which
reads like "Enter was swallowed" and sends the caller round the loop
again. The real audit row from the incident::

    send_keys hp1 ["Enter"] -> SENT_UNCONFIRMED
    "the pane looked identical to its pre-send state throughout the
     verification window"

The first block below is pure/unit (no tmux); the second exercises real
disposable tmux panes running real programs in real ptys, because the
whole bug lives in what `capture-pane` does and does not preserve -- a
mocked pane cannot reproduce it.

Every integration test asserts the number of Enter keystrokes ACTUALLY
delivered to the pty (the fixture logs each read byte), not just a status
field: "Claude is single-submit" is only meaningfully tested by counting
real keystrokes.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from terminal_mcp.adapters import (CLAUDE_SUBMIT_POLICY, CODEX_SUBMIT_POLICY,
                                   UNKNOWN_AGENT_SUBMIT_POLICY, select_adapter)
from terminal_mcp.audit import AuditStore
from terminal_mcp.composer import (COMPOSER_DRAFT, COMPOSER_EMPTY, COMPOSER_UNKNOWN,
                                   draft_contains, read_composer, strip_ansi)
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService, _extract_composer_text

FIXTURES_DIR = Path(__file__).parent / "fixtures"
GHOST_FIXTURE = FIXTURES_DIR / "claude_ghost_composer.py"
CODEX_FIXTURE = FIXTURES_DIR / "codex_composer.py"

# Verbatim rows captured with `tmux capture-pane -p -e` from the two REAL
# stuck live panes and from a real disposable Claude session on 2026-09-10
# /11. Kept byte-exact on purpose: these are the evidence, not a
# paraphrase of it, and a future Claude Code render change should make
# this file fail loudly rather than silently stop testing anything.
REAL_GHOST_ROW_HP1 = "\x1b[39m❯\xa0\x1b[2myes, publish the report\x1b[0m"
REAL_GHOST_ROW_HP2 = "\x1b[39m❯\xa0\x1b[2mdocker builder prune + gỡ image 2.24.8 đi\x1b[0m"
REAL_DRAFT_ROW = "\x1b[39m❯\xa0REAL_TYPED_DRAFT_XYZ"
REAL_RULE_ROW = "\x1b[38;5;244m" + "─" * 80
REAL_FOOTER_ROW = "\x1b[39m  \x1b[38;5;220m⏵⏵ auto mode on\x1b[38;5;246m (shift+tab to cycle)\x1b[39m"


def _service(tmp_path) -> TerminalService:
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("test-*",), max_text_length=5000),
    )
    return TerminalService(config, audit=AuditStore(tmp_path / "audit.db"))


# ---------------------------------------------------------------------------
# Unit: the ghost-vs-draft discriminator itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("row,ghost", [
    (REAL_GHOST_ROW_HP1, "yes, publish the report"),
    (REAL_GHOST_ROW_HP2, "docker builder prune + gỡ image 2.24.8 đi"),
])
def test_real_live_dim_ghost_row_reads_as_an_empty_composer(row, ghost):
    snapshot = read_composer([REAL_RULE_ROW, row, REAL_RULE_ROW, REAL_FOOTER_ROW])
    assert snapshot.state == COMPOSER_EMPTY, "a dim ghost suggestion is NOT a pending draft"
    assert snapshot.draft_text == "", "ghost text must never be exposed as draft text"
    assert snapshot.ghost_text == ghost
    assert snapshot.has_draft is False


def test_real_typed_draft_row_reads_as_a_draft():
    snapshot = read_composer([REAL_RULE_ROW, REAL_DRAFT_ROW, REAL_RULE_ROW, REAL_FOOTER_ROW])
    assert snapshot.state == COMPOSER_DRAFT
    assert snapshot.draft_text == "REAL_TYPED_DRAFT_XYZ"
    assert draft_contains(snapshot, "REAL_TYPED_DRAFT_XYZ")


def test_ghost_and_draft_are_byte_identical_once_sgr_is_stripped():
    """The whole reason the bug was invisible: without `-e`, the two rows
    this project has to tell apart are the SAME STRING."""
    ghost_plain = strip_ansi("\x1b[39m❯\xa0\x1b[2msame words here\x1b[0m")
    draft_plain = strip_ansi("\x1b[39m❯\xa0same words here")
    assert ghost_plain == draft_plain
    # ...and a stripped capture must therefore report UNKNOWN, never a
    # confident (and 50%-of-the-time wrong) DRAFT.
    assert read_composer([ghost_plain]).state == COMPOSER_UNKNOWN


def test_blank_composer_is_empty_even_without_attributes():
    assert read_composer([REAL_RULE_ROW, "\x1b[39m❯\xa0", REAL_RULE_ROW]).state == COMPOSER_EMPTY
    assert read_composer(["> "]).state == COMPOSER_EMPTY


def test_no_composer_marker_is_unknown_not_empty():
    """"I cannot see a composer" must never be reported as "there is
    nothing to submit" -- that collapse is what the fix exists to undo."""
    assert read_composer(["some scrollback", "more output"]).state == COMPOSER_UNKNOWN
    assert read_composer([]).state == COMPOSER_UNKNOWN


def test_only_the_bottom_most_marker_is_the_live_composer():
    """A submitted prompt leaves an identical-looking `> prompt` row in
    scrollback; only the last one can be the live composer."""
    snapshot = read_composer([
        "\x1b[39m❯\xa0an older submitted prompt",
        "SUBMITTED[1]: an older submitted prompt",
        REAL_RULE_ROW,
        "\x1b[39m❯\xa0\x1b[2mghost suggestion\x1b[0m",
        REAL_RULE_ROW,
    ])
    assert snapshot.state == COMPOSER_EMPTY
    assert snapshot.ghost_text == "ghost suggestion"


def test_partially_dim_draft_counts_as_a_real_draft():
    """Syntax-highlighted / partially styled input is still real input --
    only an ENTIRELY dim run is a ghost."""
    snapshot = read_composer(["\x1b[39m❯\xa0\x1b[2mdim bit\x1b[22m real bit"])
    assert snapshot.state == COMPOSER_DRAFT


def test_draft_contains_never_matches_a_ghost():
    ghost = read_composer([REAL_GHOST_ROW_HP1])
    assert draft_contains(ghost, "yes, publish the report") is False


def test_extract_composer_text_returns_nothing_for_a_ghost():
    """The exact regression: this value is handed to submit_ack_evidence
    as the text an acceptance must echo. Returning the ghost made a bare
    Enter verifiable against a string that was never submitted."""
    assert _extract_composer_text([REAL_GHOST_ROW_HP1]) == ""
    assert _extract_composer_text([REAL_DRAFT_ROW]) == "REAL_TYPED_DRAFT_XYZ"
    # Attribute-free capture keeps the exact pre-existing heuristic.
    assert _extract_composer_text(["> plain fallback text"]) == "plain fallback text"


def test_claude_and_unknown_agents_are_single_submit_by_policy():
    """The invariant, asserted structurally rather than by convention."""
    assert select_adapter("claude").submit_policy is CLAUDE_SUBMIT_POLICY
    assert CLAUDE_SUBMIT_POLICY.max_enter_attempts == 1
    assert CLAUDE_SUBMIT_POLICY.allow_enter_retry is False
    assert CLAUDE_SUBMIT_POLICY.allow_escape_recovery is False
    assert UNKNOWN_AGENT_SUBMIT_POLICY.allow_enter_retry is False
    # Codex keeps its bounded retry profile.
    assert select_adapter("codex").submit_policy is CODEX_SUBMIT_POLICY
    assert CODEX_SUBMIT_POLICY.allow_enter_retry is True
    assert CODEX_SUBMIT_POLICY.max_enter_attempts > 1


# ---------------------------------------------------------------------------
# Integration: real tmux panes, real ptys, real capture-pane semantics
# ---------------------------------------------------------------------------


def _ghost_session(tmux_session_factory, name: str, mode: str, keylog: Path) -> str:
    command = (f"bash -lc 'CLAUDE_GHOST_MODE={mode} GHOST_KEYLOG={keylog} "
               f"exec -a claude python3 -u {GHOST_FIXTURE}'")
    session = tmux_session_factory(name, command)
    time.sleep(0.4)
    return session


def _enter_keystrokes(keylog: Path) -> int:
    """How many Enter (CR/LF) bytes the pty ACTUALLY received."""
    if not keylog.exists():
        return 0
    return sum(1 for line in keylog.read_text().split("\n")
               if line.strip() in ("0d", "0a"))


def test_a_real_submit_clears_the_composer_and_confirms(tmux_session_factory, tmp_path):
    """(a) prompt + Enter genuinely leaves the composer -> SUBMIT_CONFIRMED,
    with exactly one Enter."""
    keylog = tmp_path / "keys.log"
    session = _ghost_session(tmux_session_factory, "test-ghost-normal", "normal", keylog)
    service = _service(tmp_path)

    result = service.terminal_send_text(session, "Reply exactly ENTER_OK", press_enter=True)

    assert result["delivery_state"] == "SUBMIT_CONFIRMED", result
    assert result["submit_status"] == "SUBMIT_CONFIRMED"
    assert result["evidence"] == ["COMPOSER_RELEASED"]
    assert result["composer_before"] == COMPOSER_DRAFT
    assert result["composer_after"] == COMPOSER_EMPTY
    assert _enter_keystrokes(keylog) == 1, "single-submit: exactly one Enter"
    assert "SUBMITTED[1]: Reply exactly ENTER_OK" in service.terminal_tail(session, 20)["output"]


def test_b_swallowed_enter_is_uncertain_and_never_retried(tmux_session_factory, tmp_path):
    """(b) Enter swallowed, draft still in composer -> ACTIVATION_UNCERTAIN,
    exactly ONE Enter delivered, and the prompt text is NOT resent."""
    keylog = tmp_path / "keys.log"
    session = _ghost_session(tmux_session_factory, "test-ghost-swallow", "swallow", keylog)
    service = _service(tmp_path)

    result = service.terminal_send_text(session, "this draft will be swallowed", press_enter=True)

    assert result["delivery_state"] == "ACTIVATION_UNCERTAIN", result
    assert result["submit_status"] == "SUBMIT_UNCONFIRMED"
    assert result["evidence"] == ["COMPOSER_STILL_HOLDS_DRAFT"]
    assert result["composer_after"] == COMPOSER_DRAFT
    assert result["enter_count"] == 1
    assert _enter_keystrokes(keylog) == 1, "Claude must never auto-retry Enter"
    pane = service.terminal_tail(session, 20)["output"]
    assert pane.count("this draft will be swallowed") == 1, "the prompt text must never be resent"


def test_c_redraw_noise_is_never_mistaken_for_an_ack(tmux_session_factory, tmp_path):
    """(c) A pane that redraws constantly with nothing submitted must not
    produce a false SUBMIT_CONFIRMED. OUTPUT_CHANGED is not an ACK."""
    keylog = tmp_path / "keys.log"
    session = _ghost_session(tmux_session_factory, "test-ghost-noise", "redraw_noise", keylog)
    service = _service(tmp_path)

    result = service.terminal_send_text(session, "noisy but never submitted", press_enter=True)

    assert result["delivery_state"] != "SUBMIT_CONFIRMED", result
    assert result["delivery_state"] == "ACTIVATION_UNCERTAIN"
    assert _enter_keystrokes(keylog) == 1


def test_d_bare_enter_into_a_ghost_composer_sends_no_key_at_all(tmux_session_factory, tmp_path):
    """THE live incident, end to end: the composer shows dim ghost text, an
    orchestrator calls terminal_send_keys(["Enter"]). No key may be sent,
    and the verdict must be the actionable NOT_ACTIVATED -- never an
    ambiguous SUBMIT_UNCONFIRMED that invites another blind Enter."""
    keylog = tmp_path / "keys.log"
    session = _ghost_session(tmux_session_factory, "test-ghost-bare-enter", "normal", keylog)
    service = _service(tmp_path)

    # Sanity: the pane really does *look* like it holds a pending prompt.
    assert "yes, publish the report" in service.terminal_tail(session, 20)["output"]

    result = service.terminal_send_keys(session, ["Enter"])

    assert result["delivery_state"] == "NOT_ACTIVATED", result
    assert result["sent"] is False and result["enter_sent"] is False
    assert result["evidence"] == ["COMPOSER_EMPTY"]
    assert result["composer_before"] == COMPOSER_EMPTY
    assert "yes, publish the report" in result["submit_reason"]
    assert "dim suggestion" in result["submit_reason"]
    assert _enter_keystrokes(keylog) == 0, "no Enter may reach a pane with nothing to submit"


def test_d_bare_enter_with_a_real_draft_submits_once_and_confirms(tmux_session_factory, tmp_path):
    """The other half of the gate: a REAL pending draft must still be
    submittable by a bare Enter, and confirmed by the draft leaving."""
    keylog = tmp_path / "keys.log"
    session = _ghost_session(tmux_session_factory, "test-ghost-bare-real", "normal", keylog)
    service = _service(tmp_path)
    service.terminal_send_text(session, "a genuinely pending draft", press_enter=False)
    time.sleep(0.3)

    result = service.terminal_send_keys(session, ["Enter"])

    assert result["delivery_state"] == "SUBMIT_CONFIRMED", result
    assert result["evidence"] == ["COMPOSER_RELEASED"]
    assert _enter_keystrokes(keylog) == 1
    assert "SUBMITTED[1]: a genuinely pending draft" in service.terminal_tail(session, 20)["output"]


def test_e_open_menu_with_empty_text_composer_is_not_submitted_into(tmux_session_factory, tmp_path):
    """(d in the task's numbering) A prompt-composer send must never be
    turned into a menu selection. terminal_send_text refuses outright
    (TARGET_AWAITING_APPROVAL, pre-existing behaviour, re-pinned here);
    nothing is typed and no Enter is sent."""
    keylog = tmp_path / "keys.log"
    session = _ghost_session(tmux_session_factory, "test-ghost-menu", "menu", keylog)
    service = _service(tmp_path)

    result = service.terminal_send_text(session, "should never reach the menu", press_enter=True)

    assert result["error"] == "TARGET_AWAITING_APPROVAL", result
    assert result["sent"] is False and result["enter_sent"] is False
    assert _enter_keystrokes(keylog) == 0


def test_f_codex_keeps_its_bounded_evidence_gated_enter_retry(tmux_session_factory, tmp_path):
    """(e) Codex's existing recovery must survive this change untouched:
    it still retries Enter, still bounded, and still ends up confirmed."""
    session = tmux_session_factory(
        "test-codex-retry-preserved",
        "bash -lc 'CODEX_FIXTURE_MODE=submit_after_n_enters CODEX_REQUIRED_ENTERS=2 "
        f"exec -a codex python3 -u {CODEX_FIXTURE}'")
    time.sleep(0.4)
    service = _service(tmp_path)

    result = service.terminal_send_text(session, "codex retry still works", press_enter=True)

    assert result["agent_type"] == "codex"
    assert result["delivery_state"] == "SUBMIT_CONFIRMED", result
    assert result["enter_count"] >= 2, "Codex's bounded retry must still fire"
    assert result["enter_count"] <= CODEX_SUBMIT_POLICY.max_enter_attempts


# ---------------------------------------------------------------------------
# (f) Backend parity: the remote Linux node path, the local tmux path, and
# the Windows/ConPTY path must all share ONE set of submit semantics.
# ---------------------------------------------------------------------------


def test_remote_node_path_shares_the_identical_submit_semantics(tmux_session_factory, tmp_path):
    """A remote Linux node is not a second implementation: node_agent.py
    wraps ONE local TerminalService and node_client.LocalNodeClient
    forwards send_text/send_keys straight into it, so the composer
    evidence, the NOT_ACTIVATED gate and the single-Enter policy are the
    same code -- proven here by driving the node client wrapper and
    getting byte-identical verdicts, rather than asserted in prose."""
    from terminal_mcp.node_client import LocalNodeClient

    keylog = tmp_path / "keys.log"
    session = _ghost_session(tmux_session_factory, "test-ghost-node-path", "normal", keylog)
    service = _service(tmp_path)
    node = LocalNodeClient(service)

    # Ghost composer + bare Enter, through the node path.
    blocked = node.send_keys(session, ["Enter"])
    assert blocked["delivery_state"] == "NOT_ACTIVATED", blocked
    assert _enter_keystrokes(keylog) == 0

    # Real draft + Enter, through the node path.
    sent = node.send_text(session, "via the node client", press_enter=True)
    assert sent["delivery_state"] == "SUBMIT_CONFIRMED", sent
    assert sent["evidence"] == ["COMPOSER_RELEASED"]
    assert _enter_keystrokes(keylog) == 1


class _NoAttributeBackend:
    """Deterministic stand-in for a backend whose capture cannot carry SGR
    -- exactly the Windows/ConPTY situation (windows_backend.capture_lines
    documents `ansi` as a no-op, and its pyte screen does not model SGR 2).
    No real Windows node is touched, and nothing is restarted."""

    ansi_capture_supported = False

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.sent_keys: list[list[str]] = []

    def capture_lines(self, session, lines, *, ansi=False):
        return list(self._lines)

    def get_session(self, session):
        from terminal_mcp.models import SessionInfo
        return SessionInfo(name=session, attached=False, windows=1, created_epoch=0,
                           activity_epoch=0, pane_pid=1, pane_current_command="claude",
                           pane_dead=False, session_id="$1", pane_id="%1",
                           pane_in_mode=False, pane_current_path="/tmp")

    def send_keys(self, session, keys):
        self.sent_keys.append(list(keys))

    def send_text(self, session, text, press_enter):  # pragma: no cover - unused here
        raise AssertionError("not used by this test")


def test_windows_conpty_backend_degrades_to_unknown_never_to_not_activated(tmp_path):
    """A backend that cannot report attributes must NEVER produce a
    confident NOT_ACTIVATED (which would wrongly withhold a legitimate
    Enter) and must never produce a false SUBMIT_CONFIRMED either. It
    keeps the pre-existing pane-diff behaviour and reports
    DELIVERY_UNKNOWN. Deterministic mock, no real Windows node."""
    service = _service(tmp_path)
    # The SAME ghost row as the live incident, but with the attributes
    # already resolved away -- indistinguishable from a real draft here.
    backend = _NoAttributeBackend(["claude ready", "❯ yes, publish the report"])
    service.tmux = backend

    assert service._read_composer("test-win").state == COMPOSER_UNKNOWN

    result = service.terminal_send_keys("test-win", ["Enter"])
    assert result["delivery_state"] == "DELIVERY_UNKNOWN", result
    assert result["delivery_state"] != "NOT_ACTIVATED"
    assert backend.sent_keys == [["Enter"]], "the Enter must still be delivered on this backend"


def test_local_tmux_and_node_paths_use_one_composer_reader():
    """Structural parity check: both paths reach composer.read_composer
    through TerminalService._read_composer, and the tmux backend declares
    the attribute capability the reader needs."""
    from terminal_mcp.tmux import TmuxClient
    assert TmuxClient.ansi_capture_supported is True
    from terminal_mcp.windows_backend import WindowsSessionBackend
    assert WindowsSessionBackend.ansi_capture_supported is False
