"""Inert Codex-like TUI for issue #118 end-to-end tests.

Accepts one prompt (bracketed paste or typed), shows the busy footer for a
moment, then -- like an agent following the supervisor's completion
instruction -- prints `TMCP-DONE:<task id found in the prompt>` and returns to
an idle composer. Counts submissions so a test can prove there was no resend.
Executes nothing from the prompt.
"""
import os
import re
import sys
import termios
import time
import tty

fd = sys.stdin.fileno()
original = termios.tcgetattr(fd)
submissions = 0
log = []


def render(body):
    sys.stdout.write("\x1b[H\x1b[2J\x1b[3Jcodex fixture\r\n" + "\r\n".join(log + [body]) + "\r\n")
    sys.stdout.flush()


try:
    tty.setraw(fd)
    sys.stdout.write("\x1b[?2004h")
    render("› Ask Codex to do anything")
    buffer = b""
    while True:
        buffer += os.read(fd, 65536)
        if b"\x1b[200~" in buffer and b"\x1b[201~" not in buffer:
            continue  # wait for the whole bracketed paste
        text = buffer.replace(b"\x1b[200~", b"").replace(b"\x1b[201~", b"")
        if b"\r" not in text:
            # Composer holds the draft, like Codex: the submit watchdog
            # checks this before its single separate Enter.
            draft = text.decode("utf-8", errors="replace")
            render(f"› [Pasted Content {len(draft)} chars]")
            continue
        text = text.split(b"\r")[0]
        buffer = b""
        if not text.strip():
            continue
        submissions += 1
        prompt = text.decode("utf-8", errors="replace")
        match = re.search(r"dt_[0-9a-f]{16}", prompt)
        log.append(f"SUBMITTED[{submissions}]")
        render("• Working (1s • esc to interrupt)")
        time.sleep(1.5)
        if match:
            log.append(f"TMCP-DONE:{match.group(0)}")
        render("› Ask Codex to do anything")
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, original)
