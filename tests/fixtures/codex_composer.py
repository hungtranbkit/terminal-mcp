#!/usr/bin/env python3
"""Simulates the real, reported Codex composer-stuck bug for regression
testing: text + Enter is accepted at the tmux layer (bytes reach the pty)
but the composer does not actually submit -- Escape then Enter is what a
real Codex user found fixes it. Reproduces the *mechanism* believed
responsible: a live-redrawing Ink-style UI re-renders its composer (a
spinner/tick, here) on every keystroke including a "swallowed" Enter, so a
naive "did the pane change" check sees a difference and falsely reports
confirmed even though the typed text is still sitting there unsubmitted.

Raw tty mode, real per-keystroke reads -- a real program in a real pty,
like laggy_line_reader.py/never_submits.py, not a stub of tmux behavior.
Launched via `exec -a codex ...` so tmux's own #{pane_current_command}
genuinely reports "codex", exactly like the real CLI.

CODEX_FIXTURE_MODE env var selects behavior:
  stuck_then_escape (default) -- a bare Enter never submits, only
    redraws the composer (tick increments, typed text stays visible);
    Escape immediately followed by Enter does submit.
  always_stuck -- neither a bare Enter nor Escape+Enter ever submits
    (models a recovery attempt that still fails).
  submits_and_shows_working -- a bare Enter submits immediately and the
    pane then shows "esc to interrupt" (already-working evidence).
  silently_stuck_then_escape -- URGENT bugfix regression: a bare Enter is
    a PURE no-op swallow -- produces literally ZERO output, not even a
    redraw tick (the textbook signature from the original root-cause
    fixture, tests/fixtures/laggy_line_reader.py); Escape immediately
    followed by Enter still submits normally. Exists because the
    pre-fix CodexAdapter.stuck_composer_evidence required `after !=
    before`, which can never be true for this exact case -- this mode
    proves the fix's broadened check (and only the fix) makes this
    specific failure recoverable.
  stuck_then_draft_replaced -- URGENT bugfix regression: a bare Enter
    redraws the composer (matching the stuck-composer *pattern*
    identically to stuck_then_escape) but with a DIFFERENT string, never
    the caller's own sent text -- models a real, later, unrelated draft
    occupying the composer when a recovery decision is made. Proves
    recovery is correctly WITHHELD (never dispatches Escape/Enter) when
    the pending content cannot be positively attributed to this specific
    send attempt, even though the redraw pattern alone looks identical to
    a legitimately recoverable stuck composer.
  delayed_genuine_submit -- a bare Enter DOES submit, but only after a
    real ~1.5s delay (still well inside the verification window) before
    writing SUBMITTED -- models a merely slow, not stuck, acceptance;
    must confirm without ever invoking recovery.
  stuck_then_composer_cleared -- like stuck_then_draft_replaced, but the
    composer is emptied rather than replaced with different text (models
    a cancel) -- the sent text is equally absent, so recovery must be
    withheld for the same reason.
"""
import os
import sys
import termios
import time
import tty

MODE = os.environ.get("CODEX_FIXTURE_MODE", "stuck_then_escape")

fd = sys.stdin.fileno()
old_attrs = termios.tcgetattr(fd)
tty.setraw(fd)

buf = ""
escape_pending = False
redraw_tick = 0
submitted = 0
escape_count = 0


def render_composer() -> None:
    # Overwrites the composer's own line in place (return to column 0,
    # clear it, rewrite) instead of ever appending a new line -- a real
    # bordered composer box has fixed height; a spinner/cursor/elapsed-
    # timer tick redraws *within* it, never growing the pane's line count.
    # This is exactly what makes the naive "did the pane change" check
    # unreliable: the rendered content differs each call, but no new line
    # is ever produced. The cursor is already sitting on the composer's
    # own row whenever this is called (right after the last character
    # typed, or after this function's own previous write) -- no cursor-up
    # needed, just return-and-clear the current row.
    global redraw_tick
    redraw_tick += 1
    sys.stdout.write("\r\x1b[2K")
    sys.stdout.write(f"> {buf} [tick {redraw_tick}]")
    sys.stdout.flush()


try:
    sys.stdout.write("codex composer ready\r\n> ")
    sys.stdout.flush()
    while True:
        ch = sys.stdin.read(1)
        if not ch or ch == "\x03":
            break
        if ch == "\x1b":  # Escape
            escape_pending = True
            escape_count += 1
            continue
        if ch in ("\n", "\r"):
            if MODE == "submits_and_shows_working":
                submitted += 1
                sys.stdout.write(f"\r\nSUBMITTED[{submitted}]: {buf}\r\nesc to interrupt\r\n")
                sys.stdout.flush()
                buf = ""
                escape_pending = False
                continue
            if MODE == "always_stuck":
                render_composer()
                escape_pending = False
                continue
            if MODE == "delayed_genuine_submit":
                time.sleep(1.5)  # merely slow, not stuck -- well inside the 3s verify window
                submitted += 1
                sys.stdout.write(f"\r\nSUBMITTED[{submitted}]: {buf}\r\nesc to interrupt\r\n")
                sys.stdout.flush()
                buf = ""
                escape_pending = False
                continue
            if MODE == "silently_stuck_then_escape":
                # Pure no-op swallow: no render_composer() call at all, no
                # output whatsoever -- the pane is byte-identical to its
                # pre-Enter state, exactly like a real swallowed Enter in a
                # debounced line editor (laggy_line_reader.py).
                if escape_pending:
                    submitted += 1
                    sys.stdout.write(f"\r\nSUBMITTED[{submitted}]: {buf}\r\n")
                    sys.stdout.flush()
                    buf = ""
                escape_pending = False
                continue
            if MODE == "stuck_then_draft_replaced":
                # A later, unrelated draft now occupies the composer --
                # never the caller's own sent text -- but the redraw
                # PATTERN (in-place, no new line) is identical to an
                # ordinary recoverable stuck composer.
                buf = "someone else's later draft, not what was sent"
                render_composer()
                escape_pending = False
                continue
            if MODE == "stuck_then_composer_cleared":
                # The composer is genuinely emptied (e.g. modeling a
                # cancel) rather than replaced with different text -- the
                # sent text is equally absent either way, so this must be
                # withheld for the same reason as stuck_then_draft_replaced.
                buf = ""
                render_composer()
                escape_pending = False
                continue
            # stuck_then_escape
            if escape_pending:
                submitted += 1
                sys.stdout.write(f"\r\nSUBMITTED[{submitted}]: {buf}\r\n")
                sys.stdout.flush()
                buf = ""
            else:
                render_composer()
            escape_pending = False
            continue
        escape_pending = False
        buf += ch
        sys.stdout.write(ch)
        sys.stdout.flush()
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
