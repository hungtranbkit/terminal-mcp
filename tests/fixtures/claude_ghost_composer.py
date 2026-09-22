#!/usr/bin/env python3
"""Reproduces the REAL live P0 root cause: Claude Code's dim ghost
suggestion rendered inside an EMPTY composer.

Captured from the actual incident (hp-linux node, Claude Code 2.1.267,
sessions hp1/hp2, 2026-09-10). `tmux capture-pane -p -e` on the two
stuck panes returned, byte for byte, this shape::

    ESC[39m ❯ \\xa0 ESC[2m yes, publish the report ESC[0m

while a genuinely typed draft on the same pane carried NO SGR 2::

    ESC[39m ❯ \\xa0 REAL_TYPED_DRAFT

Without `-e` both are the identical plain string ``❯ <text>``, which is
precisely why every pane-text consumer -- and the operator reading it --
concluded a submitted-and-cleared composer still held a pending prompt.

This fixture is a real program in a real pty in raw tty mode (same
posture as claude_composer.py / codex_composer.py) so the tests exercise
real tmux capture semantics, not a mocked string. Launched via
`exec -a claude` so #{pane_current_command} genuinely reports "claude".

CLAUDE_GHOST_MODE selects behaviour:
  normal        (default) Enter with a real draft submits: echo, clear,
                and re-render the dim ghost suggestion -- the healthy
                path. Enter with an EMPTY composer does NOTHING AT ALL
                and writes ZERO bytes, exactly like the real CLI (this
                is what made the live pane byte-identical across the
                failed verification window).
  swallow       Enter never submits; the real draft stays in the composer
                (models a genuine swallowed Enter, which must classify as
                ACTIVATION_UNCERTAIN and must NOT be retried for Claude).
  redraw_noise  Enter does not submit, but the pane keeps redrawing a
                spinner/elapsed-timer footer -- output changes constantly
                with nothing submitted. Proves OUTPUT_CHANGED alone is
                never accepted as an ACK.
  menu          Renders an open selection menu (Claude's own
                AskUserQuestion-style widget chrome) with an EMPTY text
                composer, so a stray Enter would accept a highlighted
                menu option rather than submit a message.

Every byte received on stdin is appended to $GHOST_KEYLOG (when set) as
one hex line per read, so a test can assert EXACTLY how many Enter (0d)
keystrokes were delivered -- the single-submit policy is only meaningfully
tested by counting real keystrokes, not by reading a status field.
"""
import os
import sys
import termios
import time
import tty

MODE = os.environ.get("CLAUDE_GHOST_MODE", "normal")
KEYLOG = os.environ.get("GHOST_KEYLOG")
GHOST = os.environ.get("GHOST_TEXT", "yes, publish the report")

ESC = "\x1b"
DIM, UNDIM, RESET_FG = f"{ESC}[2m", f"{ESC}[22m", f"{ESC}[39m"
CLEAR_LINE = f"\r{ESC}[2K"


def log_key(data: str) -> None:
    if not KEYLOG:
        return
    with open(KEYLOG, "a", encoding="ascii") as handle:
        handle.write(data.encode("utf-8", "replace").hex() + "\n")


RULE = "─" * 78
FOOTER = "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"


def composer_row(buf: str) -> str:
    """The composer row. An EMPTY buffer renders the DIM ghost suggestion;
    a non-empty one renders the real draft at normal intensity. This one
    difference -- SGR 2 or not -- is the entire root cause, and it is the
    only thing telling "there is something to submit" from "there is
    nothing to submit"."""
    if buf:
        return f"{RESET_FG}❯\xa0{buf}"
    return f"{RESET_FG}❯\xa0{DIM}{GHOST}{UNDIM}"


def draw_block(buf: str) -> None:
    """Draw the whole composer BOX -- rule row, composer row, rule row,
    footer -- then park the cursor back on the composer row for in-place
    edits. Real Claude Code renders exactly this shape (verified against
    live panes: a `───` rule directly above and below the `❯` row), and
    the box matters: it is what lets a reader tell the LIVE composer from
    a submitted prompt's identical-looking `❯ text` echo in scrollback,
    and what bounds a wrapped draft."""
    sys.stdout.write(f"\r\n{RULE}\r\n{composer_row(buf)}\r\n{RULE}\r\n{FOOTER}")
    sys.stdout.write(f"{ESC}[2A\r")  # back up onto the composer row
    sys.stdout.flush()


def redraw_composer_row(buf: str) -> None:
    """In-place rewrite of just the composer row (the cursor already sits
    on it) -- the per-keystroke update path."""
    sys.stdout.write(f"{CLEAR_LINE}{composer_row(buf)}")
    sys.stdout.flush()


def leave_block() -> None:
    """Move off the composer row to the bottom of the box before writing
    new scrollback output."""
    sys.stdout.write(f"{ESC}[2B\r\n")
    sys.stdout.flush()


def draw_menu() -> None:
    sys.stdout.write(
        "\r\n  1. Yes, proceed\r\n  2. No, stop\r\n"
        "Enter to select · Tab/arrow keys to navigate · Esc to cancel"
    )
    sys.stdout.flush()


fd = sys.stdin.fileno()
old_attrs = termios.tcgetattr(fd)
tty.setraw(fd)
buf = ""
submitted = 0

try:
    sys.stdout.write("claude ghost composer ready")
    if MODE == "menu":
        draw_menu()
    draw_block(buf)
    while True:
        ch = sys.stdin.read(1)
        if not ch or ch == "\x03":
            break
        log_key(ch)
        if ch in ("\r", "\n"):
            if not buf:
                # THE live behaviour: Enter into an empty composer is a
                # complete no-op. Not one byte is written, so the pane is
                # byte-identical across any verification window -- exactly
                # what the old verifier misread as "Enter was swallowed"
                # instead of "there was nothing to submit".
                continue
            if MODE == "swallow":
                continue  # draft stays put; no output at all
            if MODE == "redraw_noise":
                # Output churns constantly with nothing submitted: the
                # draft never leaves the composer. Proves OUTPUT_CHANGED
                # alone is never accepted as an ACK.
                for tick in range(10):
                    leave_block()
                    sys.stdout.write(f"✦ Working… ({tick}s · esc to interrupt)")
                    draw_block(buf)
                    time.sleep(0.05)
                continue
            sent, buf = buf, ""
            submitted += 1
            leave_block()
            sys.stdout.write(f"❯\xa0{sent}\r\nSUBMITTED[{submitted}]: {sent}")
            draw_block(buf)
            continue
        if ch in ("\x7f", "\x08"):
            buf = buf[:-1]
            redraw_composer_row(buf)
            continue
        if ch == "\x15":  # C-u
            buf = ""
            redraw_composer_row(buf)
            continue
        if ch == ESC:
            continue  # never let a stray Escape clear a real draft here
        buf += ch
        redraw_composer_row(buf)
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
