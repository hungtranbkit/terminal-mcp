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
"""
import os
import sys
import termios
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
