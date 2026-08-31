#!/usr/bin/env python3
"""Minimal, deterministic reproduction of the exact bug class this fixes:
an interactive TUI whose line editor "debounces" input -- it waits a short
settle window after the last keystroke before it is willing to treat Enter
as a submit. An Enter that arrives *before* that window elapses (i.e.
immediately after the preceding text, with no gap) is swallowed as a no-op
rather than submitting -- reproducing "text fully typed in the terminal but
does not execute until the user manually presses Enter [again]".

Deliberately runs the tty in raw/cbreak mode via `tty.setraw`, exactly like
a real interactive line editor (readline, Ink, a Codex/Claude-style CLI)
does -- that is *why* those programs are vulnerable to this race and a
plain `bash read` (which leaves the pty in canonical mode, so the kernel
itself buffers a whole line and hands it over atomically once Enter
arrives) is not: in canonical mode there is nothing for a debounce window
to race against. This fixture is a real program, run in a real tmux pane
via a real pty, reading one raw byte at a time -- not a stub of tmux or
asyncio behavior.
"""
import sys
import termios
import time
import tty

SETTLE_SECONDS = 0.05  # shorter than the fix's send-then-Enter delay

fd = sys.stdin.fileno()
old_attrs = termios.tcgetattr(fd)
tty.setraw(fd)

buf = ""
last_key_at = None
submitted = 0

try:
    while True:
        ch = sys.stdin.read(1)
        if not ch:
            break
        now = time.time()
        if ch in ("\n", "\r"):
            if last_key_at is not None and (now - last_key_at) < SETTLE_SECONDS:
                # Swallowed: this Enter arrived mid-"redraw", exactly like
                # the real bug -- it is consumed and produces no
                # submission at all (buf is intentionally NOT cleared, so
                # the next real submission includes everything typed so
                # far, matching a real line editor whose buffer keeps
                # growing across a swallowed Enter).
                continue
            submitted += 1
            # \r\n so the pane visibly advances a line under raw mode
            # (which does no output translation on its own).
            sys.stdout.write(f"SUBMITTED[{submitted}]: {buf}\r\n")
            sys.stdout.flush()
            buf = ""
            last_key_at = None
        elif ch == "\x03":  # Ctrl-C: let the fixture be killed cleanly
            break
        else:
            buf += ch
            sys.stdout.write(ch)  # raw mode disables the tty's own echo
            sys.stdout.flush()
            last_key_at = now
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
