#!/usr/bin/env python3
"""Deterministically never submits on Enter -- prints a normal WAITING_INPUT
-shaped prompt, echoes typed text, but treats every subsequent Enter as a
no-op keystroke, forever. Used to test the UNCONFIRMED path deterministically
(not depending on the timing race tests/fixtures/laggy_line_reader.py
models) -- e.g. Supervisor v2's handling of a submission that can never be
confirmed within the verification window.
"""
import sys
import termios
import tty

print("Continue? [y/N]", flush=True)

fd = sys.stdin.fileno()
old_attrs = termios.tcgetattr(fd)
tty.setraw(fd)
try:
    while True:
        ch = sys.stdin.read(1)
        if not ch or ch == "\x03":
            break
        if ch in ("\n", "\r"):
            continue  # always swallowed, no matter how long the caller waits
        sys.stdout.write(ch)
        sys.stdout.flush()
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)
