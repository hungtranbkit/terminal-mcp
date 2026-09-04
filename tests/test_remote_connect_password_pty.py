"""remote_connect.py's password-auth pty driver (_run_ssh_with_password /
_run_ssh_with_password_and_stdin) -- this project has no paramiko/sshpass
dependency, so password auth drives the real `ssh` client over a pty and
answers its own password prompt exactly once. Tested here against a
small FAKE `ssh`-like script (never a real sshd/network), so the prompt-
detection logic itself is exercised deterministically regardless of this
host's own sshd configuration -- the real end-to-end SSH mechanics
(key auth, host-key pinning) are covered by test_remote_connect.py's own
real_ssh-marked test instead."""
from __future__ import annotations

import stat
import sys
from pathlib import Path

from terminal_mcp import remote_connect as rc

FAKE_SSH_PASSWORD_PROMPT = """#!{python}
import sys
sys.stdout.write("dell@192.168.1.50's password: ")
sys.stdout.flush()
line = sys.stdin.readline()
if line.strip() == "{expected_password}":
    sys.stdout.write("\\nauthenticated ok\\n")
    sys.exit(0)
sys.stdout.write("\\nPermission denied, please try again.\\n")
sys.exit(1)
""".replace("{python}", sys.executable)


def _write_fake_ssh(tmp_path: Path, expected_password: str) -> Path:
    script = tmp_path / "fake-ssh"
    script.write_text(FAKE_SSH_PASSWORD_PROMPT.format(expected_password=expected_password))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_password_prompt_is_detected_and_answered_exactly_once(tmp_path):
    script = _write_fake_ssh(tmp_path, "correct-horse")
    stdout, stderr, code = rc._run_ssh_with_password([str(script)], "correct-horse", timeout=10)
    assert code == 0
    assert "authenticated ok" in stdout


def test_wrong_password_is_reported_as_a_real_failure(tmp_path):
    script = _write_fake_ssh(tmp_path, "correct-horse")
    stdout, stderr, code = rc._run_ssh_with_password([str(script)], "totally-wrong", timeout=10)
    assert code != 0


FAKE_SSH_STDIN_SCRIPT = """#!{python}
import sys
sys.stdout.write("dell@192.168.1.50's password: ")
sys.stdout.flush()
line = sys.stdin.readline()
if line.strip() != "{expected_password}":
    sys.stdout.write("\\nPermission denied, please try again.\\n")
    sys.exit(1)
sys.stdout.write("\\n")
sys.stdout.flush()
script_bytes = sys.stdin.buffer.read()
# The EOT byte (0x04) our own pty writer sends to end "stdin" is not
# meaningful to a plain read() over a pty -- strip it if present, mirrors
# what a real remote `sh -s` reading until EOF would see.
script_text = script_bytes.decode(errors="replace").rstrip("\\x04")
sys.stdout.write("received script of length " + str(len(script_text)) + "\\n")
if "MARKER_TOKEN_VALUE" in script_text:
    sys.stdout.write("marker found in script\\n")
sys.exit(0)
""".replace("{python}", sys.executable)


def test_password_then_script_is_delivered_via_stdin_after_prompt(tmp_path):
    script = tmp_path / "fake-ssh-stdin"
    script.write_text(FAKE_SSH_STDIN_SCRIPT.format(expected_password="mypassword"))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    fake_script_body = "echo hello; # token=MARKER_TOKEN_VALUE\n"
    stdout, stderr, code = rc._run_ssh_with_password_and_stdin([str(script)], "mypassword", fake_script_body, timeout=10)
    assert code == 0
    assert "marker found in script" in stdout
