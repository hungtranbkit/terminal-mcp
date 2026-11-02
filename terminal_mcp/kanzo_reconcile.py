"""Bounded one-way reconciliation for an isolated local Kanzo home."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .kanzo_projection import write_projection
from .redaction import redact_text


def _write_status(path: Path, value: Mapping[str, Any]) -> None:
    fd, name = tempfile.mkstemp(prefix=".tmcp-reconcile.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def reconcile_once(
    snapshot: Callable[[], Mapping[str, Any]], home: Path | str, *, synced_at: str
) -> dict[str, Any]:
    """Replace the projection only after every source succeeds."""
    root = Path(home).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    status_path = root / "tmcp-reconcile-status.json"
    try:
        previous = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    try:
        result = write_projection(snapshot(), root, synced_at=synced_at)
    except Exception as exc:
        status = {
            "ok": False,
            "stale": True,
            "last_attempt_at": synced_at,
            "last_success_at": previous.get("last_success_at"),
            "last_good_digest": previous.get("last_good_digest"),
            "error": redact_text(str(exc))[:500],
        }
        _write_status(status_path, status)
        return status
    status = {
        "ok": True,
        "stale": False,
        "last_attempt_at": synced_at,
        "last_success_at": synced_at,
        "last_good_digest": result.digest,
        "digest": result.digest,
        "projects": result.projects,
        "tasks": result.tasks,
    }
    _write_status(status_path, status)
    return status
