from __future__ import annotations

import fnmatch
import re

from .config import AppConfig


SENSITIVE_SESSION_WORDS = ("root", "ssh", "password", "secret", "database")
SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def valid_session_name(session: str) -> bool:
    return bool(SAFE_SESSION_RE.fullmatch(session))


def valid_new_session_name(session: str) -> bool:
    """Stricter than valid_session_name -- for a NAME BEING CREATED
    (session lifecycle's create only; every other caller of
    valid_session_name is unaffected and unchanged). SAFE_SESSION_RE
    already excludes shell metacharacters/path separators entirely (the
    charset is [A-Za-z0-9_.-] only), so the injection floor is the same
    one every existing session-name check already relies on -- this adds
    two narrower create-time rules on top: a name tmux's own `-t`
    addressing could otherwise misparse (a leading '-' looks like a flag,
    a leading '.' is reserved for tmux's own relative-pane/window syntax
    in some target forms) is refused outright, never passed to `tmux
    new-session -s` at all."""
    return valid_session_name(session) and not session.startswith("-") and not session.startswith(".")


def session_allowed(session: str, config: AppConfig) -> bool:
    if not valid_session_name(session):
        return False
    matched = [pattern for pattern in config.allowed_session_patterns if fnmatch.fnmatchcase(session, pattern)]
    if not matched:
        return False
    lowered = session.casefold()
    if any(word in lowered for word in SENSITIVE_SESSION_WORDS):
        # Sensitive names require an exact, non-glob whitelist entry.
        return session in matched
    return True


def binding_session_allowed(session: str, config: AppConfig) -> bool:
    """Bindings never target sensitive sessions, even with an exact whitelist."""
    lowered = session.casefold()
    if any(word in lowered for word in SENSITIVE_SESSION_WORDS):
        return False
    return session_allowed(session, config)


def input_session_allowed(session: str, config: AppConfig) -> bool:
    if not valid_session_name(session):
        return False
    policy = config.input_policy
    if any(fnmatch.fnmatchcase(session, pattern) for pattern in policy.denied_session_patterns):
        return False
    return any(fnmatch.fnmatchcase(session, pattern) for pattern in policy.allowed_session_patterns)


def session_input_denied_by_pattern(session: str, config: AppConfig) -> bool:
    """The denial half of input_session_allowed, usable independently by
    the dashboard's per-session grant path (core.py's
    grant_session_input/terminal_send_text_granted): a session outside
    input_policy.allowed_session_patterns entirely (the ordinary case for
    a grant target -- that's the whole point of a grant) must still never
    bypass an EXPLICIT deny pattern. Same absolute floor
    input_session_allowed already enforces for the static-whitelist path,
    applied here too -- deliberately NOT the same function, so a change to
    one can never accidentally alter the other's behavior."""
    if not valid_session_name(session):
        return True
    return any(fnmatch.fnmatchcase(session, pattern) for pattern in config.input_policy.denied_session_patterns)


def require_read(config: AppConfig) -> str | None:
    return None if config.permissions.terminal_read else "READ_DISABLED"


def require_input(config: AppConfig) -> str | None:
    return None if config.permissions.terminal_input else "INPUT_DISABLED"


def require_session_lifecycle(config: AppConfig) -> str | None:
    """Own, independent gate for create/detach/delete -- deliberately not
    piggybacked on terminal_read (a deployment can read everything and
    still never be able to spin up new processes) or terminal_input.
    Disabled unless config.session_lifecycle.enabled is explicitly true."""
    return None if config.session_lifecycle.enabled else "SESSION_LIFECYCLE_DISABLED"
