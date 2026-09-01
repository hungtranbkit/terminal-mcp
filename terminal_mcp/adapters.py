from __future__ import annotations

import re
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
DELIVERY_STATES = (DELIVERY_TEXT_SENT, DELIVERY_SUBMIT_CONFIRMED, DELIVERY_UNKNOWN,
                    DELIVERY_BLOCKED, DELIVERY_ERROR)

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

    @abstractmethod
    def identify_target_state(self, lines: list[str]) -> str:
        """One of TARGET_STATES, best-effort from the pane's current tail."""

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

    def identify_target_state(self, lines: list[str]) -> str:
        return TARGET_UNKNOWN

    def can_submit_now(self, lines: list[str]) -> bool:
        return True

    def submit_ack_evidence(self, before: list[str], after: list[str], sent_text: str) -> bool:
        return after != before

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
        r"continue\?\s*$", r"\bapprove\b", r"\bpermission\b", r"waiting for input",
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
    )
)


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
    return " ".join(" ".join(line.split()) for line in lines)


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
        if after == before or not _shows_genuine_progress(before, after):
            return False
        was_busy = _match_any(_WORKING_PATTERNS, _tail(before, 6)) or _match_any(_WORKING_PATTERNS, _tail(after, 6))
        if was_busy:
            return _sent_text_echoed(after, sent_text)
        return True

    def stuck_composer_evidence(self, before: list[str], after: list[str]) -> bool:
        return False  # never reproduced for Claude -- no recovery enabled, see docstring

    def safe_recovery_allowed(self, lines: list[str]) -> bool:
        return False  # stuck_composer_evidence is always False, so this is never consulted


_ADAPTERS_BY_COMMAND = {
    "codex": CodexAdapter(),
    "claude": ClaudeAdapter(),
}
_GENERIC = GenericShellAdapter()


def select_adapter(pane_current_command: str) -> AgentAdapter:
    return _ADAPTERS_BY_COMMAND.get((pane_current_command or "").casefold(), _GENERIC)
