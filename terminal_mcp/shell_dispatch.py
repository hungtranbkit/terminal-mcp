"""Target-aware normalization of a queue dispatch payload, BEFORE any byte is sent.

THE PRODUCTION FAILURE THIS CLOSES (2026-09-26). An `execution_mode=shell`
task passed the coordinator as READY, then every dispatch into its plain-shell
session came back MULTILINE_SHELL_SEND_REFUSED. core.py's refusal is correct
and stays: a shell executes each embedded newline the instant it arrives, so a
multiline paste starts running line by line before any submit key and nothing
can take those lines back. The bug was upstream of it -- queue_engine always
built the multiline agent template (prompt + prose reminder + marker
instruction) and handed it to terminal_send_text regardless of what the target
was, so the guard refused the same payload on every attempt.

The dispatcher now answers "can this target take this payload?" itself, from
the same adapter property the guard uses (`buffers_embedded_newlines`), and
picks exactly one of:

  AGENT     the target owns a composer that buffers newlines (Claude/Codex):
            send the agent template unchanged.
  WRAPPED   an explicit shell task on a POSIX shell: send ONE line that
            decodes the exact script bytes from base64 and evals them in the
            interactive shell, then reports the exit status. No newline ever
            reaches the pty, so the guard has nothing to refuse.
  REFUSED   anything else (fish/csh/PowerShell, an unknown non-agent program,
            an agent task aimed at something that is not an agent): nothing is
            sent, and the caller records an explicit, actionable refusal.

core.py's guard remains the final defense for every other caller and for a
target that changes between this observation and the send.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass

from .adapters import normalize_command, select_adapter

#: Shells whose grammar the wrapper below is written in (POSIX sh + the
#: bash/zsh/ksh supersets). Verified against bash, dash and busybox ash. fish,
#: csh/tcsh and PowerShell/cmd use a different grammar, so a wrapper that is
#: provably safe there does not exist here -- those targets are REFUSED rather
#: than guessed at.
POSIX_SHELL_COMMANDS = frozenset({"bash", "zsh", "sh", "dash", "ash", "ksh", "mksh"})

AGENT = "AGENT"
WRAPPED = "WRAPPED"
REFUSED = "REFUSED"

#: The refusal code a pre-send REFUSED plan records. Distinct from core's own
#: MULTILINE_SHELL_SEND_REFUSED so the audit trail shows the queue declined
#: before sending, rather than the guard having caught it.
UNSUPPORTED_TARGET = "MULTILINE_DISPATCH_UNSUPPORTED_TARGET"

SHELL_EXIT_TAG = "TERMINAL_MCP_SHELL_EXIT"
SHELL_EXIT_RE = re.compile(
    rf"###{SHELL_EXIT_TAG}\s+task_id=(\S+)\s+attempt=(\d+)\s+nonce=(\S+)\s+rc=(\d+)###")


@dataclass(frozen=True)
class DispatchPlan:
    kind: str
    text: str | None = None
    code: str | None = None
    reason: str | None = None


def has_embedded_newline(text: str) -> bool:
    # Same definition as core._has_embedded_newline: a lone CR is Enter too.
    return "\n" in text or "\r" in text


def build_shell_line(script: str, *, task_id: str, attempt: int, nonce: str, summary_sha256: str) -> str:
    """One line that runs `script` byte-for-byte in the current shell.

    - base64 carries the script, so no quote, `$`, backtick or newline in it
      can be interpreted while the line itself is parsed (injection-safe).
    - `eval` in the interactive shell (not a subshell), so `cd`/exports persist
      exactly as if the lines had been typed; the line's own `$?` is the
      script's exit status.
    - exit 0 prints the ordinary nonce-bound completion marker; anything else
      prints a SHELL_EXIT line carrying the status. Both are assembled with
      `printf '###%s ...' TAG` so the echoed command line never contains a
      parseable marker -- only the shell's OUTPUT can.
    - the script blob is the LAST token, so the audit log's bounded preview of
      this text holds the fixed wrapper, not the script.
    """
    blob = base64.b64encode(script.encode("utf-8")).decode("ascii")
    fields = f"task_id={task_id} attempt={attempt} nonce={nonce}"
    exit_fmt = f"'\\n###%s {fields} rc=%s###\\n' {SHELL_EXIT_TAG}"
    line = (
        "__tmcp_run() { "
        "__tmcp_s=$(printf '%s' \"$1\" | base64 -d 2>/dev/null) || "
        "__tmcp_s=$(printf '%s' \"$1\" | base64 -D) || "
        f"{{ printf {exit_fmt} 126; return 126; }}; "
        "eval \"$__tmcp_s\"; __tmcp_rc=$?; "
        "if [ \"$__tmcp_rc\" -eq 0 ]; then "
        f"printf '\\n###%s protocol=terminal-mcp-completion/v1 {fields} status=completion_candidate "
        f"summary_sha256={summary_sha256}###\\n' TERMINAL_MCP_COMPLETION; "
        f"else printf {exit_fmt} \"$__tmcp_rc\"; fi; "
        "return \"$__tmcp_rc\"; }; "
        f"__tmcp_run '{blob}'"
    )
    assert not has_embedded_newline(line)
    return line


def plan_dispatch(*, current_command: str | None, execution_mode: str | None, agent_text: str,
                  script: str, task_id: str, attempt: int, nonce: str,
                  summary_sha256: str) -> DispatchPlan:
    """Decide what may be sent to this target. Pure; sends nothing.

    An UNKNOWN foreground command (a status that omits it) is sent as before:
    refusing on missing data would stall every node that does not report it,
    and core.py's guard still stands between the bytes and the pty."""
    command = normalize_command(current_command or "")
    shell_task = execution_mode == "shell"
    if not command:
        return DispatchPlan(AGENT, text=agent_text)
    if select_adapter(command).buffers_embedded_newlines:
        if shell_task:
            return DispatchPlan(REFUSED, code=UNSUPPORTED_TARGET, reason=(
                f"execution_mode=shell but the target is running {command!r}, an agent composer, "
                "not a shell -- nothing was sent; point the task at a shell session or drop "
                "execution_mode=shell"))
        return DispatchPlan(AGENT, text=agent_text)
    if shell_task and command in POSIX_SHELL_COMMANDS:
        return DispatchPlan(WRAPPED, text=build_shell_line(
            script, task_id=task_id, attempt=attempt, nonce=nonce, summary_sha256=summary_sha256))
    if not has_embedded_newline(agent_text) and not shell_task:
        return DispatchPlan(AGENT, text=agent_text)
    if shell_task:
        reason = (f"target shell {command!r} is not POSIX-compatible, so the payload cannot be "
                  "wrapped into one safe line; nothing was sent. Run the task in a bash/zsh/sh "
                  "session, or send one line at a time")
    else:
        reason = (f"target is running {command!r}, which does not buffer embedded newlines, and "
                  "the agent dispatch is multiline; nothing was sent. Start the intended agent in "
                  "this session, or set metadata.execution_mode=shell for a shell script")
    return DispatchPlan(REFUSED, code=UNSUPPORTED_TARGET, reason=reason)


def parse_shell_exit(output: str, *, task_id: str, attempt: int, nonce: str | None) -> int | None:
    """The nonzero exit status the wrapper reported for THIS attempt, or None."""
    if not output or not nonce:
        return None
    found = None
    for match in SHELL_EXIT_RE.finditer(output):
        if match.group(1) == task_id and match.group(2) == str(attempt) and match.group(3) == nonce:
            found = int(match.group(4))
    return found
