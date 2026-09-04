from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionInfo:
    name: str
    attached: bool
    windows: int
    created_epoch: int
    activity_epoch: int
    pane_pid: int
    pane_current_command: str
    pane_dead: bool
    # tmux's own stable identifiers (e.g. "$3", "%7") -- unlike `name`,
    # never reused by a *different* session/pane while the tmux server
    # keeps running, even if a session with the same name is later killed
    # and recreated. See SessionIdentity (core.py) / P0-2 identity pinning.
    session_id: str = ""
    pane_id: str = ""
    # tmux's own "#{pane_in_mode}" -- true while the pane is in copy-mode
    # (a human scrolled it, or an errant key sequence entered it) or any
    # other tmux client mode. In this state tmux itself intercepts
    # keystrokes for its own scrollback/search/selection UI -- they never
    # reach the underlying program's pty at all, regardless of what
    # pane_current_command reports (the foreground process is unaffected
    # and unaware). See _input_guard (core.py) / P0 audit finding #14.
    pane_in_mode: bool = False
    # tmux's own "#{pane_current_path}" -- the pane's current working
    # directory, resolved fresh from tmux each call (never inferred/
    # guessed). Used by the Kill-session-with-reopen-metadata flow
    # (core.py's terminal_kill_session) to opportunistically capture a
    # real, observed cwd at kill time -- still re-validated against
    # config.session_lifecycle.allowed_cwd_roots before ever being
    # trusted for a Reopen, exactly like any other working_directory
    # input (see lifecycle.py's resolve_cwd).
    pane_current_path: str = ""


@dataclass(frozen=True)
class SessionIdentity:
    """Canonical identity a binding/watch pins itself to at creation time,
    and re-resolves + compares against immediately before every send. A
    mismatch (session name recycled onto a different tmux session, or the
    pane replaced within the same session) means the target that was
    originally bound/watched no longer exists -- sending to whatever now
    answers to that session name would be sending to the wrong place."""
    name: str
    session_id: str
    pane_id: str
    created_epoch: int

    @classmethod
    def from_session_info(cls, info: SessionInfo) -> "SessionIdentity":
        return cls(name=info.name, session_id=info.session_id, pane_id=info.pane_id,
                  created_epoch=info.created_epoch)

    def matches(self, other: "SessionIdentity") -> bool:
        # session_id is tmux's own never-reused-while-alive identifier --
        # the authoritative check. pane_id is compared too (a session can
        # keep its session_id while its active pane is killed/replaced);
        # created_epoch is a third, redundant corroboration in case a
        # tmux build/version ever leaves session_id or pane_id blank.
        return (self.session_id == other.session_id and self.pane_id == other.pane_id
                and self.created_epoch == other.created_epoch)

