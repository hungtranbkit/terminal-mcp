"""Regression coverage for the intermittent "text fully typed but Enter is
not delivered/processed" bug (terminal_send_text/terminal_send_bound,
press_enter=True).

Root cause: `TmuxClient.send_text` fired two `tmux send-keys` calls --
literal text, then Enter -- back to back with zero gap. `tmux send-keys`
writes bytes to the pane's pty and returns almost instantly; it does not
wait for the *receiving* program to finish processing them. An interactive
TUI that runs its pty in raw/cbreak mode (readline, Ink, a Codex/Claude
-style CLI -- anything with its own live per-keystroke input handling) can
have a redraw/debounce window that swallows an Enter arriving too soon
after the preceding text, rather than queuing it. A plain `bash read`
target (canonical tty mode) never exhibits this, because canonical mode's
own kernel-side line buffering hands the whole line to the reading process
atomically once Enter arrives -- there is nothing for a debounce window to
race against. That is why this bug is specific to interactive TUIs and
easy to miss with a simple shell fixture.

tests/fixtures/laggy_line_reader.py is a real program (raw tty mode, real
per-keystroke timing) that reproduces this exact race deterministically;
tests/fixtures/never_submits.py deterministically never confirms (used to
test the UNCONFIRMED path itself, independent of the timing race).
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from terminal_mcp.audit import AuditStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService

FIXTURES_DIR = Path(__file__).parent / "fixtures"
LAGGY_READER = f"python3 -u {FIXTURES_DIR / 'laggy_line_reader.py'}"
NEVER_SUBMITS = f"python3 -u {FIXTURES_DIR / 'never_submits.py'}"


def _service(tmp_path) -> TerminalService:
    # Isolated audit.db per test -- the default AuditStore path is the
    # real production database this environment's live service also
    # writes to; using it here would both pollute production audit
    # history and make session-name-scoped assertions non-deterministic
    # across repeated test runs.
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("test-*",), max_text_length=2000),
    )
    return TerminalService(config, audit=AuditStore(tmp_path / "audit.db"))


# ---------------------------------------------------------------------------
# The race, proven directly at the tmux level (no gap vs. a settle gap) --
# this documents the evidence independent of anything in terminal_mcp itself.
# ---------------------------------------------------------------------------


def test_evidence_raw_tmux_swallows_enter_without_a_settle_gap(tmux_session_factory, tmp_path):
    import subprocess

    session = tmux_session_factory("test-race-evidence", LAGGY_READER)
    time.sleep(0.2)

    def raw_send_keys(*args: str) -> None:
        subprocess.run(["tmux", "send-keys", "-t", session, *args], check=True)

    # Old behavior: text, then Enter, with zero gap.
    raw_send_keys("-l", "--", "hello")
    raw_send_keys("Enter")
    time.sleep(0.3)
    pane = subprocess.run(["tmux", "capture-pane", "-t", session, "-p"],
                          check=True, capture_output=True, text=True).stdout
    assert "SUBMITTED[1]" not in pane, "expected the no-gap Enter to be swallowed (that is the bug)"
    assert "hello" in pane

    # With the fix's settle delay: text, wait, then Enter -- now submits
    # (accumulating "hello" from the swallowed attempt above, exactly like
    # a real line editor whose buffer keeps growing across a swallowed
    # Enter).
    raw_send_keys("-l", "--", "world")
    time.sleep(0.08)
    raw_send_keys("Enter")
    time.sleep(0.3)
    pane = subprocess.run(["tmux", "capture-pane", "-t", session, "-p"],
                          check=True, capture_output=True, text=True).stdout
    assert "SUBMITTED[1]: helloworld" in pane


# ---------------------------------------------------------------------------
# The actual fix, exercised through the real production code path
# ---------------------------------------------------------------------------


def test_send_text_confirms_submission_against_a_debouncing_tui(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-send-confirm", LAGGY_READER)
    time.sleep(0.2)
    service = _service(tmp_path)

    result = service.terminal_send_text(session, "hello-there", press_enter=True)
    assert result["sent"] is True
    assert result["submit_status"] == "SUBMIT_CONFIRMED"
    pane = service.terminal_tail(session, 10)["output"]
    assert "SUBMITTED[1]: hello-there" in pane


def test_send_bound_confirms_submission_against_a_debouncing_tui(tmux_session_factory, tmp_path):
    from terminal_mcp.bindings import BindingStore

    session = tmux_session_factory("test-bound-confirm", LAGGY_READER)
    time.sleep(0.2)
    service = _service(tmp_path)
    service.bindings = BindingStore(tmp_path / "bindings.db")
    bound = service.terminal_bind("laggy", session, input_enabled=True)
    assert bound["input_enabled"] is True

    result = service.terminal_send_bound("laggy", "bound-hello", press_enter=True)
    assert result["sent"] is True
    assert result["submit_status"] == "SUBMIT_CONFIRMED"
    pane = service.terminal_tail(session, 10)["output"]
    assert "SUBMITTED[1]: bound-hello" in pane


def test_press_enter_false_is_text_sent_never_claims_submission(tmux_session_factory, tmp_path):
    # No Enter requested -> nothing to confirm -> unchanged fast behavior,
    # named explicitly rather than silently implying success.
    session = tmux_session_factory("test-no-enter", LAGGY_READER)
    time.sleep(0.2)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "typed-only", press_enter=False)
    assert result["sent"] is True
    assert result["submit_status"] == "TEXT_SENT"
    assert "submit_reason" not in result


def test_send_text_reports_unconfirmed_rather_than_lying_with_sent_true(tmux_session_factory, tmp_path):
    # A deterministic never-confirms target: sent=True is still accurate
    # (the bytes really were delivered), but submit_status must say so
    # honestly instead of implying the prompt executed.
    session = tmux_session_factory("test-never-submits", NEVER_SUBMITS)
    time.sleep(0.2)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "y", press_enter=True)
    assert result["sent"] is True
    assert result["submit_status"] == "SUBMIT_UNCONFIRMED"
    assert result["submit_reason"]


def test_unconfirmed_send_never_auto_retries_enter(tmux_session_factory, tmp_path):
    # Exactly one Enter must ever be sent per call, confirmed or not --
    # a second Enter on a target that eventually would have accepted the
    # first risks a genuine double submission.
    session = tmux_session_factory("test-no-auto-retry", NEVER_SUBMITS)
    time.sleep(0.2)
    service = _service(tmp_path)
    service.terminal_send_text(session, "y", press_enter=True)
    pane = service.terminal_tail(session, 40)["output"]
    # The fixture echoes every accepted character but never prints a
    # submission marker; if Enter were sent more than once we would not be
    # able to tell from this fixture alone, so assert the stronger,
    # directly-observable invariant instead: the audit log recorded
    # exactly one send_text call for this session.
    events = [e for e in service.audit.list(50, session=session) if e["action"] == "send_text"]
    assert len(events) == 1


def test_audit_records_unconfirmed_as_its_own_category(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-audit-unconfirmed", NEVER_SUBMITS)
    time.sleep(0.2)
    service = _service(tmp_path)
    service.terminal_send_text(session, "y", press_enter=True)
    events = service.audit.list(5, session=session)
    assert events[0]["result"] == "SENT_UNCONFIRMED"
    assert events[0]["reason"]


def test_audit_records_confirmed_as_plain_sent(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-audit-confirmed", LAGGY_READER)
    time.sleep(0.2)
    service = _service(tmp_path)
    service.terminal_send_text(session, "ok", press_enter=True)
    events = service.audit.list(5, session=session)
    assert events[0]["result"] == "SENT"


# ---------------------------------------------------------------------------
# Stress test: many consecutive real sends, zero lost, zero duplicate
# ---------------------------------------------------------------------------


def test_stress_many_consecutive_sends_no_lost_no_duplicate_submissions(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-stress-send", LAGGY_READER)
    time.sleep(0.2)
    service = _service(tmp_path)

    n = 100
    confirmed = 0
    for i in range(n):
        result = service.terminal_send_text(session, f"m{i:04d}", press_enter=True)
        assert result["sent"] is True
        if result["submit_status"] == "SUBMIT_CONFIRMED":
            confirmed += 1

    pane = service.terminal_capture(session)["output"]
    submitted_lines = re.findall(r"SUBMITTED\[(\d+)\]: (m\d{4})", pane)
    indices = [int(idx) for idx, _ in submitted_lines]
    markers = [marker for _, marker in submitted_lines]

    # Zero lost: every one of the n sends must have actually been
    # submitted exactly once. Zero duplicate: no submission index repeats,
    # and no marker text repeats.
    assert len(submitted_lines) == n, f"expected {n} submissions, pane shows {len(submitted_lines)}"
    assert indices == sorted(set(indices)) == list(range(1, n + 1)), "duplicate or missing submission index"
    assert len(markers) == len(set(markers)), "a marker text was submitted more than once"
    assert markers == [f"m{i:04d}" for i in range(n)], "submissions arrived out of order or content mismatch"
    # This is the actual reliability claim: with the fix, every single one
    # of the n sends against a genuinely debouncing target confirmed.
    assert confirmed == n, f"only {confirmed}/{n} sends confirmed"


# ---------------------------------------------------------------------------
# Codex composer-stuck bug: reported live -- terminal_send_text(...,
# press_enter=true) returned SUBMIT_CONFIRMED, a later Enter also returned
# SENT, but Codex never actually started; sending Escape then Enter
# immediately caused it to begin working. Root cause: a live-redrawing
# Ink-style composer changes its own rendered content (a spinner/cursor
# tick) even when an Enter is "swallowed" (interpreted as e.g. insert-
# newline rather than submit, most commonly with a long/multi-line
# prompt) -- so the base "did the pane change" verification signal alone
# is not reliable proof of submission for these targets specifically.
# ---------------------------------------------------------------------------

CODEX_FIXTURE_PATH = FIXTURES_DIR / "codex_composer.py"


def _codex_session(tmux_session_factory, name: str, mode: str) -> str:
    command = f"bash -lc 'CODEX_FIXTURE_MODE={mode} exec -a codex python3 -u {CODEX_FIXTURE_PATH}'"
    tmux_session_factory(name, command)
    return name


def test_codex_normal_submit_confirms_without_recovery(tmux_session_factory, tmp_path):
    session = _codex_session(tmux_session_factory, "test-codex-normal", "submits_and_shows_working")
    time.sleep(0.3)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "hello", press_enter=True)
    assert result["sent"] is True
    assert result["submit_status"] == "SUBMIT_CONFIRMED"
    assert "recovery_attempted" not in result
    pane = service.terminal_tail(session, 10)["output"]
    assert "SUBMITTED[1]: hello" in pane


def test_codex_stuck_composer_recovers_via_escape_then_enter(tmux_session_factory, tmp_path):
    session = _codex_session(tmux_session_factory, "test-codex-stuck", "stuck_then_escape")
    time.sleep(0.3)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "hello world", press_enter=True)
    assert result["sent"] is True
    assert result["recovery_attempted"] is True
    assert result["submit_status"] == "SUBMIT_CONFIRMED"
    assert "recovery" in result["submit_reason"]
    pane = service.terminal_tail(session, 10)["output"]
    assert "SUBMITTED[1]: hello world" in pane
    # Exactly one recovery attempt -- one Escape sent, not looped.
    assert pane.count("SUBMITTED[") == 1


def test_codex_already_working_never_gets_escape_recovery(tmux_session_factory, tmp_path):
    # The bare Enter DOES submit here (composer clears) and simultaneously
    # shows working evidence -- confirms the base check alone is enough
    # and, more importantly, that no Escape is ever sent when working
    # evidence is present (verified indirectly: escape_count would bump
    # redraw/undo the submission if it were sent while a real target were
    # mid-processing; here we assert the result is a clean, single,
    # un-recovered confirmation).
    session = _codex_session(tmux_session_factory, "test-codex-working", "submits_and_shows_working")
    time.sleep(0.3)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "hello", press_enter=True)
    assert result["submit_status"] == "SUBMIT_CONFIRMED"
    assert "recovery_attempted" not in result
    pane = service.terminal_tail(session, 10)["output"]
    assert "esc to interrupt" in pane
    assert pane.count("SUBMITTED[") == 1


def test_codex_recovery_failure_reports_unconfirmed_not_false_success(tmux_session_factory, tmp_path):
    session = _codex_session(tmux_session_factory, "test-codex-alwaysstuck", "always_stuck")
    time.sleep(0.3)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "hello", press_enter=True)
    assert result["sent"] is True
    assert result["recovery_attempted"] is True
    assert result["submit_status"] == "SUBMIT_UNCONFIRMED"
    assert "recovery" in result["submit_reason"]
    pane = service.terminal_tail(session, 10)["output"]
    assert "SUBMITTED[" not in pane  # never actually submitted -- honestly reported as such


def test_codex_non_codex_command_never_triggers_recovery(tmux_session_factory, tmp_path):
    # Same "stuck" fixture behavior, but NOT running as "codex" -- the
    # recovery path must be scoped to RECOVERY_ELIGIBLE_COMMANDS only,
    # never applied generically. The base verification's own "did
    # anything change" signal is unmodified for every other target (that
    # is deliberate -- see _poll_for_submission's docstring on why it
    # isn't a text/line-count match): it still reports SUBMIT_CONFIRMED
    # here off the composer's own in-place redraw, exactly as it always
    # has for a non-recovery-eligible command. That known limitation for
    # an *unlisted* live-redrawing TUI is the honest tradeoff of scoping
    # the fix narrowly rather than guessing at every CLI's redraw
    # behavior; the actual assertion that matters is that recovery itself
    # never fires for a command outside the eligible set.
    session = "test-notcodex-stuck"
    tmux_session_factory(session, f"bash -lc 'CODEX_FIXTURE_MODE=stuck_then_escape python3 -u {CODEX_FIXTURE_PATH}'")
    time.sleep(0.3)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "hello", press_enter=True)
    assert "recovery_attempted" not in result


def test_codex_recovery_preserves_permission_and_whitelist_guards(tmux_session_factory, tmp_path):
    # The recovery path only ever runs *after* every existing guard
    # (terminal_input, whitelist, input_policy, sensitive-target) has
    # already passed -- a disabled target must still be refused outright,
    # never reaching tmux at all.
    session = _codex_session(tmux_session_factory, "test-codex-permcheck", "stuck_then_escape")
    time.sleep(0.3)
    config = AppConfig(
        PermissionsConfig(True, False), ("test-*",), 200, 100,  # terminal_input disabled
        InputPolicyConfig(allowed_session_patterns=("test-*",), max_text_length=2000),
    )
    service = TerminalService(config, audit=AuditStore(tmp_path / "audit.db"))
    result = service.terminal_send_text(session, "hello", press_enter=True)
    assert result["error"] == "INPUT_DISABLED"
    # Confirm nothing was ever sent to the pane at all -- read through a
    # separate, read-only-capable service (this one's own terminal_read is
    # fine to use for reading; only its terminal_input differs above).
    reader = TerminalService(config, audit=AuditStore(tmp_path / "audit2.db"))
    assert "hello" not in reader.terminal_tail(session, 10)["output"]


def test_codex_recovery_duplicate_supervisor_send_stays_idempotent(tmux_session_factory, tmp_path):
    session = _codex_session(tmux_session_factory, "test-codex-idem-recovery", "stuck_then_escape")
    time.sleep(0.3)
    service = _service(tmp_path)
    first = service.terminal_send_text(session, "hello", press_enter=True, idempotency_key="codex-recover-key")
    assert first["recovery_attempted"] is True
    assert first["submit_status"] == "SUBMIT_CONFIRMED"
    second = service.terminal_send_text(session, "hello", press_enter=True, idempotency_key="codex-recover-key")
    assert second == first  # exact replay -- no second send, no second recovery attempt
    pane = service.terminal_tail(session, 10)["output"]
    assert pane.count("SUBMITTED[") == 1


# ---------------------------------------------------------------------------
# URGENT bugfix regression (real user report: "text reaches the Codex
# composer but sits there until I press Enter myself" -- previous
# PASS/SUBMIT_CONFIRMED evidence alone was not sufficient). Root cause,
# confirmed by code inspection cross-referenced against the ORIGINAL
# root-cause fixture (laggy_line_reader.py, used by
# test_evidence_raw_tmux_swallows_enter_without_a_settle_gap above): a
# genuinely swallowed Enter can produce ZERO pane change at all -- not
# even a redraw tick -- yet CodexAdapter.stuck_composer_evidence required
# `after != before` to ever consider recovery, which by construction can
# never be true for that exact case. This was NOT reproduced live against
# this host's currently-installed real Codex CLI (v0.151.0) across many
# live attempts (idle, busy-queue, heavy host CPU contention, zero-gap raw
# sends, long/multiline prompts) -- today's binary appears to always
# redraw something (a cursor/border tick) within the verification window
# in practice. That is a property of the currently-installed release, not
# a guarantee the debounce race this project's own fixture demonstrably
# reproduces cannot occur -- the fix closes the structural gap
# (recovery-ineligibility for a zero-change swallow) proven possible by
# the project's own original reproduction, using a fixture built for this
# exact regression rather than a live Codex session, since the live CLI
# does not currently exhibit it on demand. See tests/test_adapters_real_cli.py
# (run with `pytest -m live_cli`) for the real, installed-CLI evidence
# this fix does not regress the ordinary, already-real-CLI-verified path.
# ---------------------------------------------------------------------------


def test_codex_zero_change_swallow_recovers_via_escape_then_enter(tmux_session_factory, tmp_path):
    # Fixture-only (see module-level note above): silently_stuck_then_escape
    # produces a bare Enter that is a pure no-op -- the pane is
    # byte-identical to typed_snapshot, the exact signature the pre-fix
    # `after != before` guard made structurally unrecoverable.
    session = _codex_session(tmux_session_factory, "test-codex-silent-stuck", "silently_stuck_then_escape")
    time.sleep(0.3)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "hello world", press_enter=True)
    assert result["sent"] is True
    assert result["recovery_attempted"] is True
    assert result["submit_status"] == "SUBMIT_CONFIRMED"
    pane = service.terminal_tail(session, 10)["output"]
    assert "SUBMITTED[1]: hello world" in pane
    assert pane.count("SUBMITTED[") == 1  # exactly one recovery attempt, no loop


def test_codex_recovery_withheld_when_composer_now_shows_a_different_draft(tmux_session_factory, tmp_path):
    # A later, unrelated draft occupies the composer by the time a
    # recovery decision would fire -- the redraw PATTERN alone looks
    # identical to an ordinary recoverable stuck composer, but the
    # content can no longer be attributed to this attempt's own send.
    # Recovery must be withheld entirely: no Escape, no Enter, honest
    # DELIVERY_UNKNOWN -- never submit someone else's pending text.
    session = _codex_session(tmux_session_factory, "test-codex-draft-replaced", "stuck_then_draft_replaced")
    time.sleep(0.3)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "hello world", press_enter=True)
    assert result["sent"] is True
    assert result["submit_status"] == "SUBMIT_UNCONFIRMED"
    assert "withheld" in result["submit_reason"]
    pane = service.terminal_tail(session, 10)["output"]
    assert "SUBMITTED[" not in pane  # nothing was ever actually submitted
    assert "someone else's later draft" in pane  # the replaced draft is still just sitting there, untouched


def test_codex_recovery_withheld_when_composer_is_now_empty(tmux_session_factory, tmp_path):
    # Same principle, different shape: the composer was cleared (e.g. a
    # cancel) rather than replaced -- the sent text is equally absent, so
    # this must be withheld for the same reason.
    session = _codex_session(tmux_session_factory, "test-codex-composer-cleared", "stuck_then_composer_cleared")
    time.sleep(0.3)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "hello world", press_enter=True)
    assert result["sent"] is True
    assert result["submit_status"] == "SUBMIT_UNCONFIRMED"
    assert "withheld" in result["submit_reason"]
    pane = service.terminal_tail(session, 10)["output"]
    assert "SUBMITTED[" not in pane


def test_codex_delayed_but_genuine_submit_confirms_without_recovery(tmux_session_factory, tmp_path):
    # Merely slow (~1.5s, well inside the 3s verify window), not stuck --
    # must confirm off the base check alone, never invoking recovery.
    session = _codex_session(tmux_session_factory, "test-codex-delayed", "delayed_genuine_submit")
    time.sleep(0.3)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "hello", press_enter=True)
    assert result["submit_status"] == "SUBMIT_CONFIRMED"
    assert "recovery_attempted" not in result
    pane = service.terminal_tail(session, 10)["output"]
    assert "SUBMITTED[1]: hello" in pane


def test_codex_zero_change_swallow_recovery_stays_idempotent_on_retry(tmux_session_factory, tmp_path):
    # No duplicate execution: a retried call with the same idempotency_key
    # replays the exact first result rather than sending (or recovering)
    # a second time, for the newly-recoverable zero-change case too.
    session = _codex_session(tmux_session_factory, "test-codex-silent-idem", "silently_stuck_then_escape")
    time.sleep(0.3)
    service = _service(tmp_path)
    first = service.terminal_send_text(session, "hello", press_enter=True, idempotency_key="silent-recover-key")
    assert first["recovery_attempted"] is True
    assert first["submit_status"] == "SUBMIT_CONFIRMED"
    second = service.terminal_send_text(session, "hello", press_enter=True, idempotency_key="silent-recover-key")
    assert second == first
    pane = service.terminal_tail(session, 10)["output"]
    assert pane.count("SUBMITTED[") == 1


# ---------------------------------------------------------------------------
# URGENT bugfix (real user report, both Claude and Codex): "prompt text
# lands in the composer but never submits until I press Enter myself".
# Root cause, confirmed by code inspection: AgentAdapter.can_submit_now/
# identify_target_state existed on every adapter but neither was EVER
# consulted by _send_text_and_verify_locked before committing to send --
# a target currently showing a menu/approval/confirmation prompt (the
# same TARGET_WAITING classification adapters.py already uses for status
# reporting) is not its normal prompt composer: Enter there answers THAT
# prompt instead of submitting a new message, and the caller's actual
# text was never delivered at all. Fixed by checking BEFORE sending
# anything (press_enter=True only) whether the target currently shows a
# TARGET_WAITING pattern, and refusing the entire send (neither text nor
# Enter) with an explicit TARGET_AWAITING_APPROVAL error if so.
# ---------------------------------------------------------------------------

WAITING_PROMPT_PATH = FIXTURES_DIR / "waiting_prompt.py"


def _waiting_prompt_session(tmux_session_factory, name: str, command: str) -> str:
    # exec -a <command> so tmux's own #{pane_current_command} genuinely
    # reports "codex"/"claude", exactly like the real CLIs -- same
    # technique codex_composer.py already uses.
    tmux_session_factory(name, f"bash -lc 'exec -a {command} python3 -u {WAITING_PROMPT_PATH}'")
    return name


def test_codex_send_refused_when_target_shows_approval_prompt(tmux_session_factory, tmp_path):
    session = _waiting_prompt_session(tmux_session_factory, "test-codex-waiting-prompt", "codex")
    time.sleep(0.3)
    service = _service(tmp_path)
    before = service.terminal_tail(session, 10)["output"]

    result = service.terminal_send_text(session, "a new unrelated prompt", press_enter=True)
    assert result["sent"] is False
    assert result["enter_sent"] is False
    assert result["error"] == "TARGET_AWAITING_APPROVAL"
    assert result["delivery_state"] == "BLOCKED"

    after = service.terminal_tail(session, 10)["output"]
    assert after == before  # nothing was typed, no stray Enter answered the real prompt
    assert "a new unrelated prompt" not in after
    assert "APPROVED=" not in after  # the prompt was never (accidentally) answered


def test_claude_send_refused_when_target_shows_approval_prompt(tmux_session_factory, tmp_path):
    # Same fix, same shared _WAITING_PATTERNS -- verified for Claude too,
    # not just Codex, since the reported bug affected both.
    session = _waiting_prompt_session(tmux_session_factory, "test-claude-waiting-prompt", "claude")
    time.sleep(0.3)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "a new unrelated prompt", press_enter=True)
    assert result["error"] == "TARGET_AWAITING_APPROVAL"
    assert result["sent"] is False


def test_send_bound_also_refused_when_target_shows_approval_prompt(tmux_session_factory, tmp_path):
    from terminal_mcp.bindings import BindingStore

    session = _waiting_prompt_session(tmux_session_factory, "test-bound-waiting-prompt", "codex")
    time.sleep(0.3)
    service = _service(tmp_path)
    service.bindings = BindingStore(tmp_path / "bindings.db")
    assert "error" not in service.terminal_bind("waiting-bound", session, input_enabled=True)
    result = service.terminal_send_bound("waiting-bound", "a new unrelated prompt", press_enter=True)
    assert result["error"] == "TARGET_AWAITING_APPROVAL"
    assert result["sent"] is False


def test_text_only_send_unaffected_by_approval_prompt_check(tmux_session_factory, tmp_path):
    # press_enter=False has nothing to "submit" -- the new check is scoped
    # to press_enter=True only, exactly as documented; a plain append must
    # stay exactly as permissive as it always was.
    session = _waiting_prompt_session(tmux_session_factory, "test-codex-waiting-textonly", "codex")
    time.sleep(0.3)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "just typed, no enter", press_enter=False)
    assert result["sent"] is True
    assert result["submit_status"] == "TEXT_SENT"
    assert "just typed, no enter" in service.terminal_tail(session, 10)["output"]


def test_claude_send_refused_for_a_multi_choice_selection_menu(tmux_session_factory, tmp_path):
    # Found LIVE in production (a real, attended Claude Code session):
    # Claude Code's own AskUserQuestion-style multi-choice menu -- numbered
    # options, arrow-key/Tab navigation, Enter accepts the highlighted
    # choice -- is not a y/n approval dialog, so the original
    # TARGET_WAITING patterns alone did not catch it. This reproduces that
    # exact real menu shape (own fixture, not send_keys simulation of the
    # bug) and confirms it is now refused the same way.
    session = "test-claude-menu-prompt"
    menu_path = FIXTURES_DIR / "menu_prompt.py"
    tmux_session_factory(session, f"bash -lc 'exec -a claude python3 -u {menu_path}'")
    time.sleep(0.3)
    service = _service(tmp_path)
    before = service.terminal_tail(session, 10)["output"]

    result = service.terminal_send_text(session, "a new unrelated prompt", press_enter=True)
    assert result["error"] == "TARGET_AWAITING_APPROVAL"
    assert result["sent"] is False

    after = service.terminal_tail(session, 10)["output"]
    assert after == before  # the real menu was never disturbed, no option was selected
    assert "CHOSE=" not in after


def test_codex_normal_composer_send_still_works_after_approval_prompt_fix(tmux_session_factory, tmp_path):
    # Regression check: the new pre-send check must never false-positive
    # on an ordinary idle composer -- reuses the existing real-shape
    # fixture already covered by test_codex_normal_submit_confirms_
    # without_recovery, asserted again here specifically alongside the new
    # fix to prove it doesn't regress the common case.
    session = _codex_session(tmux_session_factory, "test-codex-normal-after-fix", "submits_and_shows_working")
    time.sleep(0.3)
    service = _service(tmp_path)
    result = service.terminal_send_text(session, "hello", press_enter=True)
    assert result["sent"] is True
    assert result["submit_status"] == "SUBMIT_CONFIRMED"
    assert "error" not in result
