"""Small, versioned, metadata-only node inventory.

Only identifiers are sent: no filesystem paths, file contents, environment
values, prompts, or credentials. Project names come from active session
cwd basenames; skill names come from directory names in well-known local
skill roots, without following symlinks or reading their contents.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

INVENTORY_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_MAX_NAMES = 128


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if _IDENTIFIER.fullmatch(candidate) else None


def sanitize_public_inventory(value: Any) -> dict[str, Any]:
    """Return the exact public schema, filtering untrusted heartbeat data."""
    if not isinstance(value, dict) or value.get("schema_version") != INVENTORY_SCHEMA_VERSION:
        return {"schema_version": INVENTORY_SCHEMA_VERSION, "project_ids": [], "skill_names": []}

    def names(field: str) -> list[str]:
        raw = value.get(field)
        if not isinstance(raw, (list, tuple)):
            return []
        cleaned = {_identifier(item) for item in raw[:_MAX_NAMES]}
        return sorted(item for item in cleaned if item is not None)

    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "project_ids": names("project_ids"),
        "skill_names": names("skill_names"),
    }


def collect_public_inventory(session_rows: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
                             *, home: str | Path | None = None) -> dict[str, Any]:
    """Collect bounded names from active sessions and standard skill roots."""
    projects: set[str] = set()
    for row in session_rows[:_MAX_NAMES]:
        raw_path = row.get("pane_current_path") if isinstance(row, dict) else None
        if not isinstance(raw_path, str) or not raw_path:
            continue
        path = Path(raw_path)
        try:
            if not path.is_dir():
                continue
        except OSError:
            continue
        name = _identifier(path.name)
        if name:
            projects.add(name)

    root = Path(home) if home is not None else Path.home()
    skills: set[str] = set()
    for relative in (".codex/skills", ".claude/skills", ".agents/skills"):
        try:
            with os.scandir(root / relative) as entries:
                for entry in entries:
                    name = _identifier(entry.name)
                    if name and entry.is_dir(follow_symlinks=False):
                        skills.add(name)
        except OSError:
            continue

    return sanitize_public_inventory({
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "project_ids": sorted(projects),
        "skill_names": sorted(skills),
    })
