"""Runtime launcher discovery shared by capability reporting and spawning.

Windows services/processes do not receive a user's later environment-block
changes.  In particular, a node-agent that was started before a per-user npm
install cannot see ``%LOCALAPPDATA%\\npm\\codex.cmd`` through its inherited
PATH.  This module reads the current user PATH on Windows and resolves the
configured launcher against that effective path, without claiming a
capability unless the actual launcher file is present.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def _windows_user_path() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            value, _kind = winreg.QueryValueEx(key, "Path")
            return value if isinstance(value, str) else ""
    except (ImportError, OSError):
        return ""


def effective_path() -> str:
    """Return the process PATH plus the current Windows user PATH."""
    process_path = os.environ.get("PATH", "")
    user_path = _windows_user_path()
    if not user_path:
        return process_path
    parts = [part for part in (process_path, user_path) if part]
    return os.pathsep.join(parts)


def resolve_launcher(launcher: str) -> str | None:
    """Resolve one configured launcher using the effective runtime PATH."""
    if not launcher:
        return None
    path = effective_path()
    resolved = shutil.which(launcher, path=path)
    if resolved is None:
        # ``shutil.which`` handles PATHEXT on Windows, but an explicit path
        # is also useful for configs that already name a launcher file.
        candidate = Path(launcher).expanduser()
        if candidate.is_file():
            return str(candidate)
        return None
    return str(Path(resolved))
