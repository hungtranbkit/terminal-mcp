from __future__ import annotations

import shlex
import subprocess
import time
from datetime import datetime, timezone

from .models import SessionInfo


class TmuxError(RuntimeError):
    pass


SEND_TEXT_ENTER_SETTLE_SECONDS = 0.08
"""Delay between the literal-text send-keys call and the Enter send-keys
call in `send_text`. `tmux send-keys` writes bytes to the pane's pty and
returns almost instantly -- it does not wait for the *receiving* program to
finish processing them. Two send-keys calls fired back-to-back with zero
gap can therefore land an Enter keystroke while an interactive TUI's own
async input handling (redraw batching, bracketed-paste detection, a
debounced line editor -- exactly what Codex/Claude-style CLIs use) is still
mid-cycle from the preceding text; some of those debounce/redraw windows
swallow an Enter that arrives too soon rather than queuing it, producing
"the text is fully typed but nothing executes until a human presses Enter
[again]". This fixed, small settle window gives the target process time to
finish consuming the text before Enter is sent as a genuinely separate,
later keystroke. See tests/fixtures/laggy_line_reader.py for a real,
disposable-tmux-pane reproduction of this exact race, and
TerminalService._send_text_and_verify (core.py) for the best-effort
post-send confirmation layered on top of this."""


class TmuxClient:
    SESSION_FORMAT = "|".join(
        (
            "#{session_name}",
            "#{session_attached}",
            "#{session_windows}",
            "#{session_created}",
            "#{session_activity}",
            "#{pane_pid}",
            "#{pane_current_command}",
            "#{pane_dead}",
            "#{session_id}",  # tmux's own "$N" id -- P0-2 identity pinning
            "#{pane_id}",     # tmux's own "%N" id -- P0-2 identity pinning
            "#{pane_in_mode}",  # copy-mode/other tmux client mode -- P0 audit finding #14
            "#{pane_current_path}",  # kill-with-reopen-metadata's opportunistic cwd capture
        )
    )

    def __init__(self, binary: str = "tmux") -> None:
        self.binary = binary

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [self.binary, *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TmuxError(f"tmux invocation failed: {type(exc).__name__}") from exc
        if check and result.returncode != 0:
            detail = result.stderr.strip() or "tmux command failed"
            raise TmuxError(detail)
        return result

    def list_sessions(self) -> list[SessionInfo]:
        result = self._run(["list-sessions", "-F", self.SESSION_FORMAT], check=False)
        if result.returncode != 0:
            # "no server running" is tmux's wording once a server has
            # existed and exited (e.g. its last session was killed);
            # "error connecting to ... No such file or directory" is its
            # OTHER wording for the exact same "no server for this user
            # right now" state when NO server has EVER run here yet --
            # e.g. a genuinely fresh node the moment after `tmux` is
            # installed, before anything has ever created a session.
            # Found live onboarding M910: this second wording wasn't
            # matched, so the very first session-create attempt on a
            # brand new node always raised TMUX_ERROR here (get_session's
            # own duplicate-name check, called before new_session ever
            # runs) instead of the plain "no sessions yet" empty list
            # every later call correctly got once *any* tmux server had
            # existed on the host (even briefly) -- a real, permanent,
            # 100%-reproducible first-session bootstrap failure for any
            # freshly provisioned Linux node, not a flaky race.
            if ("no server running" in result.stderr or "failed to connect" in result.stderr
                    or "error connecting to" in result.stderr):
                return []
            raise TmuxError(result.stderr.strip() or "unable to list sessions")
        sessions: list[SessionInfo] = []
        for line in result.stdout.splitlines():
            if line.strip():
                sessions.append(parse_session_line(line))
        return sessions

    def get_session(self, name: str) -> SessionInfo | None:
        return next((item for item in self.list_sessions() if item.name == name), None)

    def capture_lines(self, session: str, lines: int, *, ansi: bool = False) -> list[str]:
        """Return at most `lines` of the most recent real content from the pane.

        tmux's `-S`/`-E` addressing is relative to the visible pane, not a
        literal "last N lines" window: with no `-E`, `-S -{lines}` always
        captures through the bottom of the *current visible pane* on top of
        the requested history offset, so raw output can hold far more than
        `lines` rows (and, when the pane is taller than the real output,
        blank padding rows below the last real line). `rstrip` drops that
        trailing blank padding; the final slice then deterministically bounds
        the result to the requested count so callers get exactly the N most
        recent real lines — or fewer, if the session hasn't produced that
        much output yet — regardless of pane geometry.

        `ansi=True` adds tmux's `-e`, which includes the SGR (colour/style)
        escape sequences tmux's own terminal emulation has already resolved
        for each cell — not raw, unprocessed program output, so this never
        introduces cursor-movement or other control sequences, only `ESC [
        ... m` runs. Defaults to False so every existing caller (terminal_tail,
        terminal_capture, terminal_status) is completely unaffected; only the
        dashboard's terminal-style renderer opts in.
        """
        lines = max(1, lines)
        args = ["capture-pane", "-p", "-J"]
        if ansi:
            args.append("-e")
        args += ["-S", f"-{lines}", "-t", session]
        result = self._run(args)
        captured = result.stdout.rstrip("\n").splitlines()
        return captured[-lines:]

    def send_text(self, session: str, text: str, press_enter: bool) -> None:
        self._run(["send-keys", "-t", session, "-l", "--", text])
        if press_enter:
            # See SEND_TEXT_ENTER_SETTLE_SECONDS above -- this is the fix
            # for the intermittent "typed but not submitted" race, not an
            # incidental pause: never remove it without also removing the
            # reason it exists.
            time.sleep(SEND_TEXT_ENTER_SETTLE_SECONDS)
            self._run(["send-keys", "-t", session, "Enter"])

    def send_keys(self, session: str, keys: list[str]) -> None:
        for key in keys:
            self._run(["send-keys", "-t", session, key])

    def new_session(self, name: str, cwd: str, command: str | None = None, *,
                    show_on_desktop: bool = False) -> tuple[bool, str | None]:
        """Create ONE new detached session -- `-d` (never attaches this
        process itself to it) and `-c cwd` (the session's starting working
        directory, already resolved/allowlist-checked by the caller --
        see lifecycle.py). `command`, when given, is the session's
        initial program (e.g. "claude"/"codex", from config.session_
        lifecycle.launch_commands -- never client-supplied text); omitted,
        the session starts the pane's normal default shell, exactly like
        a bare `tmux new -s NAME`.

        `show_on_desktop` is a Windows-only concept (windows_backend.py's
        own visible-console feature, task: "user nhìn tại máy Windows
        cũng thấy đúng terminal session") -- accepted here purely for
        SessionBackend Protocol shape parity, always a no-op: a real tmux
        session already has a native, always-available way to be viewed
        on a real terminal (`tmux attach -t <name>`, or this dashboard's
        own Open Terminal), so there is nothing extra for this flag to do
        on Linux."""
        args = ["new-session", "-d", "-s", name, "-c", cwd]
        if command:
            args.append(command)
        self._run(args)
        reason = ("tmux sessions have no separate 'visible on desktop' concept -- "
                  "already viewable via `tmux attach -t " + name + "` from any real terminal"
                  ) if show_on_desktop else None
        return False, reason

    def detach_session(self, name: str) -> None:
        """Detach every client currently attached to `name` -- does not
        touch the session/pane/process itself in any way (no output or
        state is lost, nothing is killed). Callers (lifecycle.py) check
        session.attached first and skip calling this at all when nothing
        is attached, so idempotency for the "already detached" case is
        handled one layer up, not by relying on tmux's own exit code
        here."""
        self._run(["detach-client", "-s", name])

    def kill_session(self, name: str) -> None:
        """Terminate exactly ONE session and its process(es) -- `kill-
        session -t`, never `kill-server` (which would tear down every
        session on the host, including this project's own controlling
        one). Callers (lifecycle.py) check the session still exists
        first, so idempotency for the "already gone" case is handled one
        layer up."""
        self._run(["kill-session", "-t", name])

    def ensure_output_capture(self, session: str, log_path: str) -> None:
        """Session Knowledge Store's real capture mechanism for tmux
        (core.py wires this in, never called directly by a client) --
        continuously appends every byte the pane produces to `log_path`
        in real time, from tmux's own side, completely independent of the
        pane's OWN scrollback/history_size. This is the deliberate fix
        for a real, documented artifact (see docs/multi-node.md and this
        project's own memory notes): a Claude Code session's Ink TUI
        keeps tmux's history_size at 0, so polling `capture-pane` alone
        can silently miss content that scrolled past between two polls --
        pipe-pane has no such gap, it sees every byte as tmux itself
        receives it.

        Real bug caught live: `pipe-pane -o` does NOT mean "no-op if a
        pipe is already active" (despite tmux's own doc wording reading
        that way at a glance) -- verified empirically, it TOGGLES the
        pipe on every single invocation once one already exists. Calling
        this once per reconcile pass with `-o` (the natural, obviously-
        wrong-in-hindsight first attempt) silently turned capture back
        OFF on the very next pass, silently losing everything sent after
        that -- caught by this feature's own multi-poll integration test,
        not a hypothetical. Fixed by explicitly checking #{pane_pipe}
        first and only ever issuing `pipe-pane` (no `-o`) when it reports
        not already active -- an unconditional, idempotent-by-construction
        check-then-act, never a toggle."""
        already_piping = self._run(["display-message", "-p", "-t", session, "#{pane_pipe}"],
                                   check=False).stdout.strip()
        if already_piping == "1":
            return
        # `stdbuf -oL` forces line-buffered output regardless of whether
        # cat's stdout is a tty -- verified live (a plain `cat >> file`
        # already flushed promptly in practice here, but stdio's default
        # full-buffering for a non-tty output is real and would otherwise
        # be one coreutils-version/libc's behavior away from silently
        # delaying capture until several KB accumulate).
        self._run(["pipe-pane", "-t", session, f"stdbuf -oL cat >> {shlex.quote(log_path)}"])

    def stop_output_capture(self, session: str) -> None:
        """Cancels an active pipe-pane for `session` (a bare `pipe-pane
        -t <session>` with no command toggles it off if on, or is a
        harmless no-op if already off) -- never deletes the log file
        itself; the caller has already read everything it needs from it
        by the time this is called (session kill/reopen)."""
        self._run(["pipe-pane", "-t", session], check=False)

    def exit_copy_mode(self, session: str) -> None:
        """Cancel tmux's active pane mode without writing a key to the PTY.

        This deliberately uses tmux's ``-X cancel`` mode command rather
        than sending ``q``/Escape (which could reach the underlying process
        if the pane left copy-mode between observation and action).
        Authorization, identity pinning, serialization, and post-action
        verification live in TerminalService; this layer only exposes the
        one narrowly-scoped tmux primitive.
        """
        self._run(["send-keys", "-t", session, "-X", "cancel"])


def parse_session_line(line: str) -> SessionInfo:
    parts = line.split("|", 11)
    if len(parts) != 12:
        raise TmuxError("unexpected tmux session format")
    (name, attached, windows, created, activity, pane_pid, command, pane_dead,
     session_id, pane_id, pane_in_mode, pane_current_path) = parts
    try:
        return SessionInfo(
            name=name,
            attached=bool(int(attached)),
            windows=int(windows),
            created_epoch=int(created),
            activity_epoch=int(activity),
            pane_pid=int(pane_pid),
            pane_current_command=command,
            pane_dead=bool(int(pane_dead)),
            session_id=session_id,
            pane_id=pane_id,
            pane_in_mode=bool(int(pane_in_mode)),
            pane_current_path=pane_current_path,
        )
    except ValueError as exc:
        raise TmuxError("invalid numeric field from tmux") from exc


def iso_timestamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()
