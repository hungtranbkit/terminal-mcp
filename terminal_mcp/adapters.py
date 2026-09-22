from __future__ import annotations

import re

from . import composer
from collections.abc import Mapping
from typing import Any
from abc import ABC, abstractmethod

# ---------------------------------------------------------------------------
# P0 Part A: explicit delivery states. Pane-diff/line-growth is no longer
# the primary definition of "the submit worked" -- it is one signal an
# AgentAdapter may consult, but the *authoritative* result a caller sees is
# one of these four states, decided by the adapter's own evidence rules
# below, not by a bare "did the pane change" check at the call site.
#
#   TEXT_SENT         -- press_enter was False; nothing to confirm.
#   SUBMIT_CONFIRMED  -- adapter-specific evidence (see submit_ack_evidence)
#                        ties this exact send attempt to a genuine change of
#                        state in the target, not just a redrawn pane.
#   DELIVERY_UNKNOWN  -- Enter was sent but no adapter evidence confirms or
#                        denies submission within the verification window.
#                        Deliberately distinct from BLOCKED/ERROR: the bytes
#                        really were written to the pty, the outcome is just
#                        unproven -- never silently upgraded to CONFIRMED.
#   BLOCKED           -- the send was refused/aborted before completing (an
#                        identity/pane_current_command mismatch caught
#                        between the text-send and the Enter-send, a policy
#                        guard, or a lease that could not be acquired). No
#                        Enter (or, for BLOCKED before any bytes went out,
#                        no text either) was sent to the target in this case.
#   ERROR             -- the tmux layer itself failed (session vanished,
#                        capture failed, subprocess error) -- distinct from
#                        DELIVERY_UNKNOWN because there is no ambiguity here:
#                        the mechanism itself did not work, not "worked but
#                        unconfirmed".
# ---------------------------------------------------------------------------
DELIVERY_TEXT_SENT = "TEXT_SENT"
DELIVERY_SUBMIT_CONFIRMED = "SUBMIT_CONFIRMED"
DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
DELIVERY_BLOCKED = "BLOCKED"
DELIVERY_ERROR = "ERROR"
# P0 2026-09-14: a bare Enter on an ALREADY-VISIBLE Claude prompt leaves the
# pane byte-identical and nothing starts. That is not DELIVERY_UNKNOWN ("bytes
# went out, outcome unproven") -- it is a specific, stable fact: the prompt is
# still sitting there and no execution began, so a retry is safe. Conflating
# the two is what left callers unable to decide whether to retry.
DELIVERY_STALLED = "SUBMIT_STALLED"
DELIVERY_STATES = (DELIVERY_TEXT_SENT, DELIVERY_SUBMIT_CONFIRMED, DELIVERY_UNKNOWN,
                    DELIVERY_BLOCKED, DELIVERY_ERROR, DELIVERY_STALLED)

# Legacy submit_status vocabulary (pre-dates this module) -- kept as the
# public field every existing caller/test already reads, now *derived* from
# delivery_state rather than independently decided, so it can never drift
# from the new authoritative state. TEXT_SENT and SUBMIT_CONFIRMED keep
# their exact old spelling; DELIVERY_UNKNOWN/BLOCKED/ERROR all map to the
# old catch-all "unconfirmed" bucket, since no pre-existing caller
# distinguishes those three -- they only ever checked for the confirmed
# case or treated anything else as "not proven".
def to_legacy_submit_status(delivery_state: str) -> str:
    if delivery_state in (DELIVERY_TEXT_SENT, DELIVERY_SUBMIT_CONFIRMED):
        return delivery_state
    return "SUBMIT_UNCONFIRMED"


def is_submission_confirmed(result: Mapping[str, Any]) -> bool:
    """Did this send result PROVE the prompt was submitted?

    A positive allowlist over the vocabulary above, and the distinction matters
    because a caller acting autonomously has to decide whether to advance a
    chain of work on the answer. Only SUBMIT_CONFIRMED carries adapter evidence
    that submission actually happened.

    Everything else is not-proven, including the two cases a denylist misses:

      TEXT_SENT -- the text reached the composer and Enter's effect was never
        established. Legacy `to_legacy_submit_status` deliberately preserves this
        spelling rather than folding it into SUBMIT_UNCONFIRMED, so a consumer
        checking `!= "SUBMIT_UNCONFIRMED"` reads it as success.

      a missing field -- a result shape this function has not met. Treating
        absence as success is how a new transport, a short-circuit path or a
        refactor silently starts advancing autonomous work on no evidence at
        all.

    Named here, beside DELIVERY_STATES, so there is ONE definition of
    "confirmed" for every consumer instead of each one re-deriving it from the
    string constants and getting a slightly different answer."""
    state = result.get("delivery_state")
    if state is not None:
        return state == DELIVERY_SUBMIT_CONFIRMED
    # Older/synthesised results may carry only the legacy field.
    return result.get("submit_status") == DELIVERY_SUBMIT_CONFIRMED


# Target states an adapter reports the pane as currently showing.
TARGET_COMPOSER = "composer"   # text box has focus, nothing submitted/running yet
TARGET_RUNNING = "running"     # actively generating/working
TARGET_WAITING = "waiting"     # blocked on a prompt/confirmation from the user
TARGET_FINAL = "final"         # settled/idle, no pending work
TARGET_UNKNOWN = "unknown"     # no adapter-specific signal either way
TARGET_STATES = (TARGET_COMPOSER, TARGET_RUNNING, TARGET_WAITING, TARGET_FINAL, TARGET_UNKNOWN)


def _tail(lines: list[str], window: int) -> str:
    return "\n".join(lines[-window:])


def _match_any(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


class AgentAdapter(ABC):
    """Deterministic, per-target-CLI evidence rules for the input-delivery
    pipeline (core.py's _send_text_and_verify_locked). Every method is a
    pure function of pane content it is handed -- no adapter talks to tmux
    itself, so these are trivially unit-testable against captured fixture
    text and are exercised against real disposable CLI sessions in
    tests/test_send_reliability.py / tests/test_adapters_real_cli.py."""

    name: str

    # Does this target hold embedded newlines in an editable buffer instead
    # of acting on each one as it arrives? An interactive agent CLI owns its
    # own multiline composer and does; a target under ordinary tty line
    # discipline (a shell) does NOT -- every "\n" in the injected text is a
    # line the target runs the instant it arrives, BEFORE any submit key.
    # See core.py's MULTILINE_SHELL_SEND_REFUSED guard for why that is a
    # safety boundary and not a formatting detail.
    buffers_embedded_newlines: bool = False

    @abstractmethod
    def identify_target_state(self, lines: list[str]) -> str:
        """One of TARGET_STATES, best-effort from the pane's current tail."""

    def staged_submit_keys(self, lines: list[str]) -> tuple[str, ...]:
        """The key sequence that submits a buffer this target is currently
        holding STAGED -- typed in full, but parked in an editor whose Enter
        inserts a newline rather than submitting (Claude Code's own
        `ctrl+x ctrl+s to send now` footer). Empty means either "this target
        has no such state" or "it is not in it right now", which is the
        default for every adapter: a submit key is only ever offered on the
        target's own on-screen evidence, never guessed.

        The caller (core.py) must still re-verify, immediately before
        pressing them, that the staged buffer is THIS attempt's own text --
        these keys submit whatever is in the editor, so the same
        attribution rule the Escape+Enter recovery uses applies here."""
        return ()

    @abstractmethod
    def can_submit_now(self, lines: list[str]) -> bool:
        """False means: do not send Enter right now (e.g. the target is
        already actively working and a stray Enter could be misinterpreted,
        or the pane shows no evidence a composer is even ready)."""

    @abstractmethod
    def submit_ack_evidence(self, before: list[str], after: list[str], sent_text: str) -> bool:
        """True only if `after` (captured post-Enter) shows genuine,
        adapter-specific evidence this exact submission was processed --
        never a bare `before != after`, since a live-redrawing Ink-style UI
        changes its own captured snapshot every tick (spinner/cursor/timer)
        with nothing actually submitted. The caller is responsible for
        tying this call to one specific send attempt (fresh `before`/`after`
        captures around exactly that attempt's Enter) -- this method itself
        holds no state across calls, so a correlation id has nothing to
        compare against here; it exists in the caller's audit trail
        instead, where each attempt's before/after pair is what "the exact
        send attempt" means operationally for a tmux-observed target with
        no other acknowledgement channel. `sent_text` (P0 zero-gap
        hardening) is the exact text this attempt typed -- available for an
        adapter that can strengthen its evidence by requiring the target
        to demonstrably echo/acknowledge *this* attempt's own content, not
        just show unrelated progress; an adapter that doesn't need it may
        ignore it."""

    @abstractmethod
    def stuck_composer_evidence(self, before: list[str], after: list[str]) -> bool:
        """True only if `after` looks like the known "Enter landed as
        insert-newline, not submit" failure for this specific adapter --
        i.e. the pane redrew (before != after) but submit_ack_evidence is
        False. Adapters with no known composer-swallow failure mode (shells)
        must always return False here -- this is what keeps Escape-recovery
        scoped to CLIs it has actually been reproduced against, never a
        generic "anything unconfirmed" trigger."""

    @abstractmethod
    def safe_recovery_allowed(self, lines: list[str]) -> bool:
        """False means: never attempt Escape+Enter recovery right now, even
        if stuck_composer_evidence is True -- e.g. the target shows evidence
        of already actively working, so Escape could genuinely interrupt
        real work rather than dismiss a stuck composer."""

    foreground_command_is_identity: bool = True
    """Does #{pane_current_command} identify the TARGET of this send?

    For an agent CLI it does: the pane runs exactly one long-lived program,
    the adapter was selected from its name, and that name changing means the
    program this send was aimed at is gone. For a plain shell it does not --
    see GenericShellAdapter's override. Defaults True so a future adapter is
    strict until it deliberately says otherwise."""


class GenericShellAdapter(AgentAdapter):
    """Fallback for any target with no more specific adapter (plain shells,
    and any command not recognized as an interactive agent CLI). No known
    composer-swallow failure mode -- canonical tty line editing (readline/
    bash) processes Enter synchronously, so there is nothing to recover
    from and no reason to withhold Enter. submit_ack_evidence keeps the
    exact pre-existing base semantics (any pane change counts) so every
    target that was never RECOVERY_ELIGIBLE_COMMANDS-scoped keeps its
    already-tested behavior unchanged."""
    name = "generic"
    # A shell's #{pane_current_command} is the program it is CURRENTLY
    # RUNNING, not the thing being sent to -- it changes as a normal
    # consequence of the shell doing its job, and is back to the shell's own
    # name the moment a command finishes. See enter_is_safe_after_command_
    # change below for the live false block this fixes.
    foreground_command_is_identity = False

    def identify_target_state(self, lines: list[str]) -> str:
        return TARGET_UNKNOWN

    def can_submit_now(self, lines: list[str]) -> bool:
        return True

    def submit_ack_evidence(self, before: list[str], after: list[str], sent_text: str) -> bool:
        # EXPRESS LANE for plain shells (2026-09-21). "Any pane change counts"
        # is necessary but NOT sufficient: a shell echoes the command onto the
        # prompt line while it is being typed, so for a command that prints
        # nothing immediately the pane after Enter is byte-identical to the
        # pane before it. Measured live on hp: `echo XONG` confirmed in 0.74s,
        # while `sleep 3; echo XONG2` came back SUBMIT_UNCONFIRMED ("the pane
        # looked identical to its pre-Enter state throughout the verification
        # window") and send_wait returned FAILED without ever starting the
        # wait, so the caller resent the command. Hits every quiet command:
        # sleep, cd, export, a compile before its first output, a buffered
        # git clone.
        #
        # This class's docstring already states the reasoning: canonical tty
        # line editing processes Enter SYNCHRONOUSLY and there is no composer
        # to swallow it, so the echoed command line is itself proof the shell
        # received the text. Added as an EXTRA positive-evidence path -- the
        # original `after != before` still confirms on its own, so nothing that
        # passed before can start failing.
        if after != before:
            return True
        probe = (sent_text or "").strip().splitlines()
        if not probe:
            return False
        first = probe[0].strip()
        # Only the last few lines: matching anywhere in scrollback would let a
        # previous identical command count as the ack for this one.
        return bool(first) and any(first in line for line in
                                   [ln for ln in after if ln.strip()][-3:])

    def stuck_composer_evidence(self, before: list[str], after: list[str]) -> bool:
        return False

    def safe_recovery_allowed(self, lines: list[str]) -> bool:
        return False


# Shared by both Ink-style interactive-agent adapters below: the same
# conservative, bottom-of-pane-only WORKING_EVIDENCE_PATTERNS this
# repository has already had real-Codex evidence for (see core.py's
# now-superseded module comment, moved here). Claude Code is the same
# Ink-rendered-CLI family and has been directly observed, live, in this
# session (the promptflow verification) to show the identical "esc to
# interrupt" footer while generating -- so it is reused as-is rather than
# invented from scratch, but Claude's composer-swallow failure mode has
# never been reproduced (unlike Codex's, which has a disposable-pane
# regression fixture), so ClaudeAdapter.stuck_composer_evidence stays False
# below: no recovery is enabled for a failure mode never observed.
_WORKING_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (r"esc to interrupt", r"\bworking\b", r"\bthinking\b")
)
_WAITING_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"do you want to continue", r"press enter", r"\[y/n\]", r"\[Y/n\]",
        r"continue\?\s*$", r"waiting for input",
        # URGENT bugfix follow-up: found LIVE, in production, against a
        # real attended Claude Code session (mesflow) -- an interactive
        # multi-choice selection widget (Claude Code's own AskUserQuestion-
        # style menu: numbered options, arrow-key/Tab navigation, Enter to
        # accept the highlighted choice) that the original y/n-shaped
        # patterns above never matched, so a send arriving while one is
        # open would fall through as TARGET_UNKNOWN -- not yet caught by
        # the pre-send TARGET_AWAITING_APPROVAL check -- and Enter would
        # select whichever option the menu currently highlights instead of
        # submitting a new message. These are the menu's own UI-chrome
        # strings (never plausible inside the model's own conversational
        # text), not the y/n phrasing above.
        r"enter to select", r"tab/arrow keys to navigate", r"esc to cancel",
        # REMOVED (2026-09-07, real false positive found LIVE against a
        # real attended session, `window2`): this list used to also
        # include bare r"\bapprove\b" and r"\bpermission\b" word-boundary
        # matches. A real composer line reading "Làm Role/Permission step
        # 2 custom role web đi" -- an entirely ordinary prompt about this
        # project's OWN subject matter (a permissions/roles feature) --
        # matched `\bpermission\b` and caused every send to that session
        # to be wrongly refused as TARGET_AWAITING_APPROVAL, with no real
        # approval/menu prompt on screen at all. Neither bare word was
        # ever exercised by a real fixture/regression test in the first
        # place (tests/fixtures/waiting_prompt.py's own real y/n dialog
        # matches via `\[y/n\]`; tests/fixtures/menu_prompt.py's own real
        # multi-choice widget matches via the menu-chrome strings just
        # above) -- both of Claude Code's actual observed approval shapes
        # (a y/n dialog, and its AskUserQuestion-style numbered-menu
        # permission widget) are already fully covered without these two
        # words. Removed rather than narrowed: this project's own standing
        # rule is to never invent unverified CLI-output phrasing (see
        # e.g. config.py's resume_capable_agent_types docstring) -- there
        # is no real, observed Claude/Codex dialog on record that uses
        # the bare word "approve" or "permission" outside of the menu-
        # chrome shape the patterns above already catch, so no speculative
        # replacement pattern was added in their place.
    )
)


# Claude Code's MULTILINE/STAGED editor footer. Found LIVE (2026-09-19,
# session terminal-mcp-session-health): terminal_send_text returned
# SUBMIT_CONFIRMED "via adapter ack evidence" while the entire prompt was
# still sitting in Claude's input editor, with the footer reading
# `ctrl+x ctrl+s to send now`. In that state Enter inserts a newline into the
# buffer instead of submitting, so the pane genuinely redraws (rows above the
# composer's own last line move as the buffer grows) -- which is exactly what
# _shows_genuine_progress was built to detect, and why a staged editor could
# masquerade as a real submission.
#
# Matched on the KEY SEQUENCE, which is UI chrome the model's own prose never
# contains, rather than on the surrounding wording (this project's standing
# rule: never invent unverified CLI-output phrasing). Both the `+` and `-`
# spellings and the mac glyph form are accepted because the same hint renders
# differently per platform/terminal; nothing else about the line is assumed.
_STAGED_EDITOR_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"ctrl\s*[+-]\s*x\s+ctrl\s*[+-]\s*s",
        r"⌃\s*x\s+⌃\s*s",
    )
)

# The key sequence that footer names. Sent as tmux key names, one press each,
# never as literal text -- and only ever when the staged footer is actually on
# screen AND this attempt's own text is still visibly staged in it.
CLAUDE_STAGED_SUBMIT_KEYS = ("C-x", "C-s")


def _shows_staged_editor(lines: list[str]) -> bool:
    """True when the pane's footer says the buffer is staged, not sent.

    Position matters, not just presence. The hint is chrome drawn at the
    bottom of the input box, so anything the target rendered BELOW the last
    occurrence supersedes it: once a turn actually starts, its own output
    (the busy footer) appears under the box. A scrolled-up copy of the hint
    left behind in the scrollback is not evidence of anything -- treating a
    bare "somewhere in the last 6 lines" match as live is how this check
    would otherwise refuse to ever confirm a send that did submit.
    """
    tail = lines[-6:]
    last_hint = None
    for index, line in enumerate(tail):
        if _match_any(_STAGED_EDITOR_PATTERNS, line):
            last_hint = index
    if last_hint is None:
        return False
    return not _match_any(_WORKING_PATTERNS, "\n".join(tail[last_hint + 1:]))


def _shows_genuine_progress(before: list[str], after: list[str]) -> bool:
    """True if content *other than the composer's own last line* has moved
    -- a live-redrawing composer's own spinner/cursor/elapsed-timer tick
    overwrites only that one row in place; every line above it stays byte-
    identical on a pure redraw. Comparing everything-but-the-last-line
    (rather than raw line-count growth, this rule's first version) is what
    makes this correct against a REAL long-running session too: capture_
    lines always returns at most SEND_VERIFY_LINES (20) rows, so on any
    pane with 20+ lines of real scrollback (true of essentially every real,
    already-running Codex/Claude session, as opposed to a fresh synthetic
    test fixture) `before` and `after` are BOTH already capped at exactly
    20 -- length can never grow again, so a length-only check goes blind
    exactly when it matters most. This was caught by live-testing against a
    real, already-booted Codex CLI session (not just the synthetic
    fixture), which is why real-CLI verification is not optional for this
    adapter. Deliberately not a substring/marker match against the sent
    text either -- see the module's prior design note on that, moved here:
    a submission confirmation that quotes the text back would false-
    negative under a marker-suffix check just as easily as it would false-
    positive an ordinary target."""
    return before[:-1] != after[:-1]


class CodexAdapter(AgentAdapter):
    """Codex CLI: reproduced, disposable-pane-verified composer-swallow
    failure mode (see tests/fixtures/laggy_line_reader.py and the existing
    RECOVERY_ELIGIBLE_COMMANDS history this adapter now encodes)."""
    name = "codex"
    buffers_embedded_newlines = True

    def identify_target_state(self, lines: list[str]) -> str:
        tail = _tail(lines, 6)
        if _match_any(_WAITING_PATTERNS, tail):
            return TARGET_WAITING
        if _match_any(_WORKING_PATTERNS, tail):
            return TARGET_RUNNING
        return TARGET_UNKNOWN

    def can_submit_now(self, lines: list[str]) -> bool:
        return not _match_any(_WORKING_PATTERNS, _tail(lines, 6))

    def submit_ack_evidence(self, before: list[str], after: list[str], sent_text: str) -> bool:
        # Genuine line-count growth, not a bare diff -- a live-redrawing
        # composer's own spinner/cursor/elapsed-timer tick changes the
        # captured snapshot on every keystroke *including a swallowed
        # Enter*, so "the pane changed" alone would false-positive on
        # exactly the failure this adapter exists to catch (see
        # stuck_composer_evidence below, and _shows_genuine_progress).
        return after != before and _shows_genuine_progress(before, after)

    def stuck_composer_evidence(self, before: list[str], after: list[str]) -> bool:
        # URGENT bugfix (real user report: "text reaches the composer but
        # sits there until I press Enter myself"): two known Codex
        # composer-swallow signatures, both meaning "no genuine progress",
        # covered by the single check below --
        #  1. Redrew (something changed -- tick/spinner/cursor) but no
        #     *genuine* growth beyond that -- "Enter became insert-newline"
        #     / partial-consume.
        #  2. The pane is BYTE-IDENTICAL to its pre-Enter state through the
        #     entire verification window -- Enter was a pure no-op swallow.
        #     This is the textbook signature from the ORIGINAL root-cause
        #     reproduction (tests/fixtures/laggy_line_reader.py: a debounced
        #     raw-mode line reader that swallows an Enter arriving mid-
        #     debounce produces literally zero output -- not even a redraw
        #     tick) -- yet the previous `after != before` guard here made
        #     this exact case structurally unrecoverable: a real Codex
        #     composer that swallows Enter without redrawing anything
        #     within the verify window fell through to a bare
        #     DELIVERY_UNKNOWN with no recovery attempt at all, matching
        #     the reported bug precisely. `not _shows_genuine_progress`
        #     alone already covers both cases (it is True whenever
        #     `before[:-1] == after[:-1]`, which includes the exact-match
        #     case), so the extra `after != before` guard was strictly
        #     narrowing, never protective -- removing it only ADDS
        #     eligibility for the recovery attempt safe_recovery_allowed
        #     below still independently gates (never firing while the
        #     target shows active-work evidence).
        return not _shows_genuine_progress(before, after)

    def safe_recovery_allowed(self, lines: list[str]) -> bool:
        return not _match_any(_WORKING_PATTERNS, _tail(lines, 6))


def _normalize_for_match(lines: list[str]) -> str:
    """Collapse each line's internal whitespace and join with a single
    space -- makes a wrapped, multi-line echo of one logical piece of text
    (Claude Code word-wraps a long/multi-line prompt across many terminal
    columns, each continuation line left-padded) match as one continuous
    string, the same way it reads as one logical line to a human looking
    at the pane."""
    # Collapse across line boundaries too. Blank rows in a multiline Codex
    # composer must not introduce extra separators that make the exact same
    # sent prompt fail its own prefix-evidence check.
    return " ".join(" ".join(line.split()) for line in lines if line.split())


def _sent_text_echoed(after: list[str], sent_text: str, *, prefix_chars: int = 80) -> bool:
    """True if a normalized, whitespace-collapsed prefix of `sent_text`
    appears anywhere in the normalized `after` pane content. A bounded
    prefix (not the full text) is deliberate: a long prompt can word-wrap
    across more lines than fit in the bounded capture window this is
    checked against, so requiring the *entire* text to be simultaneously
    visible would make a genuinely long, genuinely landed send
    unverifiable purely due to viewport size -- the same reasoning
    RECOVERY_VERIFY_TIMEOUT_SECONDS-class adapters already apply
    elsewhere in this module. A short/empty sent_text after normalization
    matches trivially true (nothing meaningful to attribute)."""
    normalized_sent = " ".join(sent_text.split())[:prefix_chars]
    if not normalized_sent:
        return True
    return normalized_sent in _normalize_for_match(after)


class ClaudeAdapter(AgentAdapter):
    """Claude Code CLI. Same Ink-rendered working/waiting footer family as
    Codex (directly observed live in this session), so identify_target_
    state/can_submit_now/safe_recovery_allowed reuse the same evidence
    rules -- but stuck_composer_evidence stays False: Claude Code's
    composer-swallow behavior under this exact race has never been
    reproduced against a real session the way Codex's was, so no recovery
    path is enabled for it (see the P0 final report's NOT VERIFIED list).

    submit_ack_evidence (P0 zero-gap hardening): genuine-progress
    (_shows_genuine_progress, same rule as Codex) is necessary but, while
    the target was ALREADY busy at either end of this attempt's window
    (before or after shows WORKING evidence), not sufficient on its own --
    an ordinary spinner/elapsed-timer tick from an EARLIER, unrelated
    in-flight turn can itself change every line but the last within the
    verify window, coincidentally, regardless of whether *this* attempt's
    Enter did anything at all. Real, live, repeated testing (short/long/
    multiline prompts, true zero-gap back-to-back bursts, up to 25
    sequential sends with no artificial delay -- see
    test_adapters_real_cli.py) never produced an actual incorrect
    confirmation from this path, and independently established WHY: Claude
    Code's own UI reliably echoes the just-sent text verbatim, either into
    conversation history or into its own "queued messages" display,
    whenever a send genuinely lands. Requiring that echo specifically (in
    addition to genuine progress) whenever the busy window makes the
    coincidental-tick risk real closes the gap in the *evidentiary*
    reasoning without narrowing the already-real-tested idle-composer case
    at all, and without adding any delay or extra keystroke -- a send this
    stricter check cannot confirm reports DELIVERY_UNKNOWN, the existing
    safe/conservative failure direction, never a false BLOCKED or a
    dropped send."""
    name = "claude"
    buffers_embedded_newlines = True

    def identify_target_state(self, lines: list[str]) -> str:
        tail = _tail(lines, 6)
        if _match_any(_WAITING_PATTERNS, tail):
            return TARGET_WAITING
        # Checked BEFORE the working patterns: a staged editor can coexist
        # with a still-visible footer from the previous turn, and "the
        # prompt is sitting unsent in the editor" is the more specific,
        # more actionable fact of the two.
        if _shows_staged_editor(lines):
            return TARGET_COMPOSER
        if _match_any(_WORKING_PATTERNS, tail):
            return TARGET_RUNNING
        # An idle Claude pane is not UNKNOWN -- it is sitting at its composer,
        # which is a fact this pane states plainly: composer.is_chrome matches
        # the box border and the `⏵⏵ auto mode on ...` footer that Claude Code
        # draws only when it is ready for the next prompt. Returning UNKNOWN
        # here threw that evidence away, and every caller that asks "is this
        # worker free yet" had nothing left but a timer. Measured 2026-09-21 on
        # this host: all ten live nova-claude-* sessions sat idle at their
        # composer and identify_target_state answered "unknown" for every one.
        #
        # Narrow on purpose: the WORKING/WAITING tests above still win, so this
        # can never mask a turn in flight or a permission prompt, and a pane
        # with no composer chrome (a redraw mid-flight, a pager, a crashed CLI)
        # still falls through to UNKNOWN exactly as before.
        if any(composer.is_chrome(line) for line in lines[-6:]):
            return TARGET_COMPOSER
        return TARGET_UNKNOWN

    def can_submit_now(self, lines: list[str]) -> bool:
        return not _match_any(_WORKING_PATTERNS, _tail(lines, 6))

    def submit_ack_evidence(self, before: list[str], after: list[str], sent_text: str) -> bool:
        if after == before or not _shows_genuine_progress(before, after):
            return False
        # Live false positive (2026-09-19, terminal-mcp-session-health): the
        # staged-editor footer is proof of the OPPOSITE of submission -- the
        # buffer is typed and parked, waiting for ctrl+x ctrl+s. Growing that
        # buffer (each swallowed Enter adds a line) moves rows above the
        # composer, so _shows_genuine_progress passes and, on an idle target,
        # the was_busy echo requirement below never even applies: the send
        # confirmed on a redraw caused by its own unsent text. Checked first,
        # and overriding, precisely because the stronger-looking signal is the
        # wrong one here.
        if _shows_staged_editor(after) and _sent_text_echoed(after, sent_text):
            return False
        was_busy = _match_any(_WORKING_PATTERNS, _tail(before, 6)) or _match_any(_WORKING_PATTERNS, _tail(after, 6))
        if was_busy:
            return _sent_text_echoed(after, sent_text)
        return True

    def staged_submit_keys(self, lines: list[str]) -> tuple[str, ...]:
        return CLAUDE_STAGED_SUBMIT_KEYS if _shows_staged_editor(lines) else ()

    def stuck_composer_evidence(self, before: list[str], after: list[str]) -> bool:
        return False  # never reproduced for Claude -- no recovery enabled, see docstring

    def safe_recovery_allowed(self, lines: list[str]) -> bool:
        return False  # stuck_composer_evidence is always False, so this is never consulted


_ADAPTERS_BY_COMMAND = {
    "codex": CodexAdapter(),
    "claude": ClaudeAdapter(),
}
_GENERIC = GenericShellAdapter()


# Executable suffixes a Windows foreground process reports and a POSIX one
# never does. windows_backend.py derives pane_current_command as the Win32
# foreground process's basename, to match tmux's own #{pane_current_command}
# semantic ("always a bare process name, e.g. bash") -- but on Windows the
# bare name KEEPS its extension, so Claude Code arrives as "claude.EXE"
# where the same agent on Linux arrives as "claude".
_EXECUTABLE_SUFFIXES = (".exe", ".com", ".bat", ".cmd")


def normalize_command(pane_current_command: str) -> str:
    """The adapter-lookup key for a foreground command name.

    P0 2026-09-15: `select_adapter` looked the raw casefolded command up in
    `_ADAPTERS_BY_COMMAND`, whose keys are "codex"/"claude". On every Windows
    node that meant `"claude.exe"`, which is not a key, so a real Claude Code
    session silently selected `GenericShellAdapter`. Two consequences, both
    observed live on dell-5530 (win1/win2/wtest/win-work): the send result
    reported `agent_type: "generic"`, and -- because
    `submit_flow.ACTIVATION_ADAPTERS` is keyed on the adapter NAME -- the
    Claude activation nudge was never sent, so a bare Enter on an already-
    visible prompt did nothing at all. Upgrading the node agent alone could
    never have fixed those sessions.

    Basename first (defensive only: both backends already report a bare
    name), then at most one executable suffix, then casefold. Deliberately
    NOT a general "strip any extension" rule -- a command genuinely named
    with a dot keeps it, and only the Windows executable family is stripped.
    """
    name = (pane_current_command or "").strip().replace("\\", "/").rsplit("/", 1)[-1].casefold()
    for suffix in _EXECUTABLE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def select_adapter(pane_current_command: str) -> AgentAdapter:
    return _ADAPTERS_BY_COMMAND.get(normalize_command(pane_current_command), _GENERIC)


# Interactive shells, by the bare name tmux reports for a pane running one.
# A pane whose foreground command is one of these is sitting at its OWN
# prompt: there is no other program between the pty and the line editor that
# an Enter could be misdelivered to.
SHELL_COMMANDS = frozenset({"bash", "zsh", "sh", "fish", "dash", "ash", "ksh", "csh", "tcsh"})


def enter_is_safe_after_command_change(adapter: AgentAdapter, command_before: str,
                                      command_at_enter: str) -> bool:
    """May Enter still be sent when #{pane_current_command} moved between the
    text-send and the Enter-send?

    The false block this exists to remove, reproduced live through the real
    ChatGPT connector (2026-09-19, generic shell `tmcp-surface-shell` on
    hp-linux): the shell accepted one multiline command, and the NEXT send came
    back BLOCKED/IDENTITY_CHANGED_MID_SEND with enter_sent=false, agent_type=
    generic. Nothing had been replaced. The pane was simply still running the
    previous command when the text landed and was back at its prompt 80ms later
    when Enter was about to go out, so `command_at_enter != command_before` --
    the shell finishing its work was read as the target disappearing, and the
    typed text was left sitting in the line editor, unexecutable.

    What actually distinguishes the two cases:

      agent CLI (foreground_command_is_identity) -- the pane runs one
        long-lived program and the adapter was selected from its name, so any
        change means the program this send was aimed at is gone. Blocks, with
        exactly the behaviour it has always had.

      shell -- a change INTO the pane's own shell means the previous command
        finished and the pane is at its prompt: Enter is precisely what should
        happen next, and withholding it is the bug. A change into anything
        ELSE means some other program took over the pty between the paste and
        the Enter, which is the one genuinely unsafe shape here (a stray Enter
        would land in that program, not the shell), so it still blocks.

    Deliberately not widened to "any change is fine for a shell": that would
    give up the bash->vim case, which this keeps refusing.
    """
    if command_before == command_at_enter:
        return True
    if adapter.foreground_command_is_identity:
        return False
    return normalize_command(command_at_enter) in SHELL_COMMANDS
