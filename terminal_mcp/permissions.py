from __future__ import annotations

import fnmatch
import re

from .config import AppConfig


SENSITIVE_SESSION_WORDS = ("root", "ssh", "password", "secret", "database")
SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def valid_session_name(session: str) -> bool:
    return bool(SAFE_SESSION_RE.fullmatch(session))


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


def require_read(config: AppConfig) -> str | None:
    return None if config.permissions.terminal_read else "READ_DISABLED"


def require_input(config: AppConfig) -> str | None:
    return None if config.permissions.terminal_input else "INPUT_DISABLED"
