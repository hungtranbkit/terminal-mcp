"""Raw UTF-8 TUI fixture: preserve paste bytes, then acknowledge a separate Enter."""
import hashlib
import os
import sys
import termios
import tty

fd = sys.stdin.fileno()
original = termios.tcgetattr(fd)
buffer = b""
prompt = b""
in_paste = False
submissions = 0


def render(body):
    sys.stdout.write("\x1b[2J\x1b[Hcodex fixture\r\n" + body + "\r\n")
    sys.stdout.flush()


try:
    tty.setraw(fd)
    sys.stdout.write("\x1b[?2004h")
    render("› ")
    while True:
        buffer += os.read(fd, 65536)
        if not in_paste:
            start = buffer.find(b"\x1b[200~")
            if start >= 0:
                buffer = buffer[start + 6:]
                in_paste = True
        if in_paste:
            end = buffer.find(b"\x1b[201~")
            if end < 0:
                continue
            prompt = buffer[:end]
            buffer = buffer[end + 6:]
            in_paste = False
            # Decode only after the entire paste; chunk boundaries may split
            # Vietnamese combining characters or four-byte emoji.
            text = prompt.decode("utf-8", errors="strict")
            render(f"› [Pasted Content {len(text)} chars]")
        if b"\r" in buffer and prompt:
            submissions += buffer.count(b"\r")
            render(f"SUBMITTED[{submissions}]: sha256={hashlib.sha256(prompt).hexdigest()}\r\nWorking (esc to interrupt)")
            buffer = b""
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, original)
