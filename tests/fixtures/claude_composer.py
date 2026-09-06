#!/usr/bin/env python3
"""Simulates the real, live-reproduced Claude Code Windows bug this
project's own P0 "direct send" investigation found (task: "P0 FIX TRIỆT
ĐỂ DIRECT SEND WINDOWS / CLAUDE INPUT" -- user report: a prompt into
`window` intermittently needed several manual re-sends): a real Claude
Code Ink UI's redraw is NOT atomic on Enter -- captured live against a
disposable dell-5530 session, the footer switches to its "esc to
interrupt" busy indicator BEFORE the just-submitted prompt's own echo
line has rendered into the scrollback above it. A single-shot "check one
capture, right after the first detected change" verifier can land
exactly in that transitional frame -- busy (so the stricter echo-
required path applies), but the echo genuinely isn't there yet -- and
incorrectly report the send as unconfirmed even though it was, and
always is, actually accepted moments later.

Raw tty mode, real per-keystroke reads -- a real program in a real pty,
same posture as codex_composer.py. Launched via `exec -a claude ...` so
tmux's own #{pane_current_command} genuinely reports "claude".

CLAUDE_FIXTURE_MODE env var selects behavior:
  normal_submit (default) -- Enter submits immediately: composer clears,
    echo + "esc to interrupt" busy footer both appear in the SAME write
    (the ordinary, already-covered case).
  busy_footer_before_echo -- THE bug repro: Enter first writes ONLY the
    busy footer (no echo of the sent text anywhere yet), then after a
    real, short delay writes the actual echo line and the response --
    modeling the two-phase Ink redraw this fixture's own module
    docstring describes. A single-shot verifier landing its one capture
    in that gap sees busy=True with no echo and (pre-fix) reports
    DELIVERY_UNKNOWN; the fixed verifier keeps polling within its
    existing budget and correctly confirms once the echo lands.
  never_echoes -- Enter shows the busy footer and NEVER writes an echo
    of the sent text at all (models a genuine failure, not a race) --
    must still correctly report unconfirmed, proving the fix's extra
    polling doesn't turn a real failure into a false positive.
"""
import os
import sys
import termios
import time
import tty

MODE = os.environ.get("CLAUDE_FIXTURE_MODE", "normal_submit")

fd = sys.stdin.fileno()
old_attrs = termios.tcgetattr(fd)
tty.setraw(fd)

buf = ""

try:
    sys.stdout.write("claude composer ready\r\n> ")
    sys.stdout.flush()
    while True:
        ch = sys.stdin.read(1)
        if not ch or ch == "\x03":
            break
        if ch in ("\n", "\r"):
            sent = buf
            buf = ""
            if MODE == "busy_footer_before_echo":
                # Phase 1 (immediate): footer alone switches to busy --
                # composer line itself goes blank (as a real Ink redraw
                # would show mid-transition), no echo anywhere yet.
                sys.stdout.write("\r\x1b[2K> \r\nesc to interrupt\r\n")
                sys.stdout.flush()
                time.sleep(0.15)  # real, short gap -- well inside the verify window
                # Phase 2: the echo + completed response finally land.
                sys.stdout.write(f"> {sent}\r\nSUBMITTED[1]: {sent}\r\ndone\r\n> ")
                sys.stdout.flush()
                continue
            if MODE == "never_echoes":
                sys.stdout.write("\r\x1b[2K> \r\nesc to interrupt\r\n")
                sys.stdout.flush()
                time.sleep(0.15)
                sys.stdout.write("done, but no echo of what was sent\r\n> ")
                sys.stdout.flush()
                continue
            # normal_submit: echo + busy footer together, immediately.
            sys.stdout.write(f"\r\x1b[2K> {sent}\r\nSUBMITTED[1]: {sent}\r\nesc to interrupt\r\n")
            sys.stdout.flush()
            continue
        buf += ch
        sys.stdout.write(ch)
        sys.stdout.flush()
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
